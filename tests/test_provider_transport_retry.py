"""Hermetic tests for PLAN-SWEEP-REPAIR.md §F (no provider calls, no network).

§F asked why every 100-floor arm-C seed ended with `error in execute: The read
operation timed out`. The answer had three parts, and these tests pin all three:

  §F/1 — a socket read timeout surfaces from `_http_transport` as a bare
         `ProviderError`, which matched none of `_call`'s retry ladders. One
         slow reviewer call therefore killed the run. It is now retried on the
         same short exponential shape as 5xx, with its own budget so a 429
         ladder earlier in the call cannot consume it.
  §F/2 — 45 s was an *episode* budget (docs/PLAN-REVIEW-LATENCY.md T0-6) that
         landed on urllib as a whole-completion socket timeout.
         `make_role_provider` now takes `http_timeout` separately.
  §F2  — the `[phase role node ...]` context reached the raise site as
         `phase=unknown role=unknown` because the HTTP branch dropped both.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderError,
    ProviderHTTPError,
)

SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["go", "stop"]}},
    "required": ["action"],
    "additionalProperties": False,
}

_OK = {"choices": [{"message": {"content": '{"action": "go"}'}}]}

# The exact shape `_http_transport` raises for a urllib socket read timeout,
# post-§F2 context wrapping. Tests assert against this string rather than a
# bare RuntimeError so a future change to the message that also changed the
# *type* would still be caught.
_TIMEOUT_MSG = (
    "provider request failed [phase=execute role=reviewer node=n1"
    " elapsed=45.1s timeout=45.0s]: The read operation timed out"
)


def _provider(transport, **kwargs) -> OpenAICompatibleProvider:
    kwargs.setdefault("api_key", "unused")
    kwargs.setdefault("base_retry_delay", 0.0)
    return OpenAICompatibleProvider(transport=transport, **kwargs)


class TransportRetryTest(unittest.TestCase):
    def test_read_timeout_is_retried_instead_of_ending_the_run(self) -> None:
        """The §F regression itself: two timeouts then a success is a success."""
        calls: list[int] = []
        slept: list[float] = []

        def transport(url, payload, headers):
            calls.append(1)
            if len(calls) <= 2:
                raise ProviderError(_TIMEOUT_MSG)
            return _OK

        provider = _provider(transport, sleep=slept.append)
        result = provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)

        self.assertEqual(result, {"action": "go"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(slept), 2, "one backoff per transport failure")

    def test_transport_retries_are_bounded_and_reraise(self) -> None:
        """Patience is bounded: a genuinely dead endpoint still surfaces."""
        calls: list[int] = []

        def transport(url, payload, headers):
            calls.append(1)
            raise ProviderError(_TIMEOUT_MSG)

        provider = _provider(transport, sleep=lambda _s: None, max_http_retries=3)
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)

        self.assertIn("The read operation timed out", str(ctx.exception))
        self.assertEqual(len(calls), 4, "initial attempt plus max_http_retries")

    def test_transport_budget_is_separate_from_the_rate_limit_ladder(self) -> None:
        """A 429 earlier in the same call must not spend the transport budget.

        Sharing `attempt` between the two would leave a call that survived two
        rate-limit rungs with only one retry left for a subsequent timeout —
        the failure mode is silent and only shows up under load.
        """
        calls: list[str] = []

        def transport(url, payload, headers):
            calls.append("x")
            if len(calls) <= 2:
                raise ProviderHTTPError(429, "rate limited", retry_after=1.0)
            if len(calls) <= 5:
                raise ProviderError(_TIMEOUT_MSG)
            return _OK

        provider = _provider(transport, sleep=lambda _s: None, max_http_retries=3)
        result = provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)

        self.assertEqual(result, {"action": "go"})
        self.assertEqual(len(calls), 6, "2 rate-limited + 3 timed out + 1 success")

    def test_transport_retry_honors_the_halt_signal(self) -> None:
        """§E16: a halt must not wait out the backoff."""
        calls: list[int] = []

        def transport(url, payload, headers):
            calls.append(1)
            raise ProviderError(_TIMEOUT_MSG)

        provider = _provider(
            transport,
            sleep=lambda _s: None,
            base_retry_delay=30.0,
            should_abort=lambda: True,
        )
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)

        self.assertIn("aborted by halt signal", str(ctx.exception))
        self.assertEqual(len(calls), 1)

    def test_backoff_callback_fires_for_transport_failures(self) -> None:
        """A run stuck retrying must be visible in events.jsonl, not silent."""
        seen: list[tuple[int, float]] = []
        calls: list[int] = []

        def transport(url, payload, headers):
            calls.append(1)
            if len(calls) == 1:
                raise ProviderError(_TIMEOUT_MSG)
            return _OK

        provider = _provider(
            transport,
            sleep=lambda _s: None,
            base_retry_delay=1.0,
            on_backoff=lambda attempt, delay: seen.append((attempt, delay)),
        )
        provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 1)
        self.assertGreater(seen[0][1], 0.0)

    def test_non_429_client_errors_still_surface_immediately(self) -> None:
        """The new branch must not swallow caller bugs into a retry loop."""
        calls: list[int] = []

        def transport(url, payload, headers):
            calls.append(1)
            raise ProviderHTTPError(401, "unauthorized")

        provider = _provider(transport, sleep=lambda _s: None)
        with self.assertRaises(ProviderHTTPError):
            provider.complete_json([{"role": "user", "content": "hi"}], SCHEMA)
        self.assertEqual(len(calls), 1)


class _FakeProvider:
    """Captures what `make_role_provider`'s HTTP branch actually constructs."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


class RoleProviderConstructionTest(unittest.TestCase):
    def _build(self, **kwargs):
        from kusudaemon.roles.factory import make_role_provider

        _FakeProvider.last_kwargs = {}
        with patch(
            "kusudaemon.roles.factory._resolve_role_transport",
            return_value=("gptme", "http"),
        ):
            make_role_provider(backend="gptme", provider_cls=_FakeProvider, **kwargs)
        return _FakeProvider.last_kwargs

    def test_http_timeout_overrides_the_episode_budget(self) -> None:
        """T0-6's 45 s is an episode budget; the socket must not inherit it."""
        kwargs = self._build(timeout=45.0, http_timeout=180.0, role="reviewer")
        self.assertEqual(kwargs["timeout"], 180.0)

    def test_timeout_still_applies_when_no_http_timeout_is_given(self) -> None:
        kwargs = self._build(timeout=120.0)
        self.assertEqual(kwargs["timeout"], 120.0)

    def test_default_is_unchanged_when_neither_is_given(self) -> None:
        kwargs = self._build()
        self.assertEqual(kwargs["timeout"], 300.0)

    def test_role_and_phase_reach_the_http_provider(self) -> None:
        """§F2's context was landing as `phase=unknown role=unknown`."""
        kwargs = self._build(role="reviewer", phase="execute")
        self.assertEqual(kwargs["role"], "reviewer")
        self.assertEqual(kwargs["phase"], "execute")

    def test_missing_role_and_phase_degrade_to_unknown(self) -> None:
        kwargs = self._build()
        self.assertEqual(kwargs["role"], "unknown")
        self.assertEqual(kwargs["phase"], "unknown")


class RoleHttpTimeoutTest(unittest.TestCase):
    def test_default(self) -> None:
        from kusudaemon.pipeline.driver import _role_http_timeout

        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("KUSUDAEMON_ROLE_HTTP_TIMEOUT", None)
            self.assertEqual(_role_http_timeout(), 180.0)

    def test_env_override(self) -> None:
        from kusudaemon.pipeline.driver import _role_http_timeout

        with patch.dict("os.environ", {"KUSUDAEMON_ROLE_HTTP_TIMEOUT": "420"}):
            self.assertEqual(_role_http_timeout(), 420.0)

    def test_garbage_and_nonpositive_values_fall_back(self) -> None:
        from kusudaemon.pipeline.driver import _role_http_timeout

        for raw in ("not-a-number", "0", "-5"):
            with patch.dict("os.environ", {"KUSUDAEMON_ROLE_HTTP_TIMEOUT": raw}):
                self.assertEqual(_role_http_timeout(), 180.0, raw)


if __name__ == "__main__":
    unittest.main()
