"""2026-09-26 role reasoning control, orchestrator traces, empty-stream resume.

Measured across every run dir through 09-26: successful orchestrator calls
emitted 1-8k completion tokens to choose among <= 3 nodes, and classify 4-11k
before a ~100-token estimate, at 6-40 tok/s. Nothing in the request limited
reasoning, the orchestrator's reasoning was never written anywhere, and a
stream that ended after reasoning with no answer (two classify calls and the
first orchestrator call on c407d1b, each at ~600 s) was restarted from scratch.

* ``KUSUDAEMON_REASONING_<ROLE>``: ``off`` sends the thinking switch (dropped
  after a 400), ``budget:N`` cuts streamed reasoning at ~N tokens and resumes
  with it carried forward. Defaults: orchestrator ``off,budget:512``,
  classify ``budget:3072``, every other role ``on``.
* Classify calls are stamped ``role="classify"`` (they recorded "unknown").
* ``orchestrator/reasoning-NNN.txt`` holds each decision's streamed reasoning.
* A stream that ends with reasoning and no answer resumes from that reasoning.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kusudaemon.v1 import provider as provider_mod  # noqa: E402
from kusudaemon.v1.orchestrator import ORCHESTRATOR_SCHEMA  # noqa: E402
from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderHTTPError,
    ProviderReasoningBudgetError,
    ReasoningSettings,
    call_scope,
    role_reasoning,
)
from kusudaemon.v6.tiering import estimate_scope_full  # noqa: E402
from kusudaemon.v6.work_object import work_object_none  # noqa: E402
from test_event_driven_orchestrator import OK, _run, dispatch  # noqa: E402

_ANSWER = {"action": "dispatch", "node_ids": ["a"], "wait_on": [], "reason": "go"}


def _clean_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("KUSUDAEMON_REASONING") and k != "KUSUDAEMON_ORCHESTRATOR_DEADLINE_S"
    }


def _reply(content: str, reasoning: str = "", usage: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {"choices": [{"message": {"content": content, "reasoning_content": reasoning}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 20}
    return body


class _Scripted:
    """A ``_stream_transport_impl`` that plays one step per call."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, url, payload, headers, on_reasoning, deadline_s=None):
        self.payloads.append(json.loads(json.dumps(payload)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(on_reasoning)
        return step


def _provider(transport: _Scripted) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(api_key="k", base_url="http://unused")
    provider._stream_transport_impl = transport
    return provider


def _ask(provider: OpenAICompatibleProvider, role: str) -> dict[str, Any]:
    with call_scope(role=role):
        return provider.complete_json(
            [{"role": "user", "content": "decide"}], ORCHESTRATOR_SCHEMA, streaming=True
        )


class RoleReasoningSettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(role_reasoning("orchestrator"), ReasoningSettings(True, 512))
            self.assertEqual(role_reasoning("classify"), ReasoningSettings(False, 3072))
            self.assertEqual(role_reasoning("reviewer"), ReasoningSettings(False, None))
            self.assertEqual(role_reasoning(None), ReasoningSettings(False, None))

    def test_env_overrides(self) -> None:
        env = {
            **_clean_env(),
            "KUSUDAEMON_REASONING_ORCHESTRATOR": "on",
            "KUSUDAEMON_REASONING_REVIEWER": "off, budget:200",
            "KUSUDAEMON_REASONING_CLASSIFY": "budget:0",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(role_reasoning("orchestrator"), ReasoningSettings(False, None))
            self.assertEqual(role_reasoning("reviewer"), ReasoningSettings(True, 200))
            self.assertEqual(role_reasoning("classify"), ReasoningSettings(False, None))


class ThinkingSwitchTest(unittest.TestCase):
    def test_orchestrator_sends_the_switch_and_other_roles_do_not(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            transport = _Scripted(_reply(json.dumps(_ANSWER)), _reply(json.dumps(_ANSWER)))
            provider = _provider(transport)
            _ask(provider, "orchestrator")
            _ask(provider, "reviewer")
        self.assertEqual(transport.payloads[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("chat_template_kwargs", transport.payloads[1])

    def test_custom_switch_payload(self) -> None:
        env = {**_clean_env(), "KUSUDAEMON_REASONING_OFF_PAYLOAD": '{"reasoning_effort": "low"}'}
        with patch.dict(os.environ, env, clear=True):
            transport = _Scripted(_reply(json.dumps(_ANSWER)))
            _ask(_provider(transport), "orchestrator")
        self.assertEqual(transport.payloads[0]["reasoning_effort"], "low")
        self.assertNotIn("chat_template_kwargs", transport.payloads[0])

    def test_a_400_drops_the_switch_for_the_provider_lifetime(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            transport = _Scripted(
                ProviderHTTPError(400, "unknown field chat_template_kwargs"),
                _reply(json.dumps(_ANSWER)),
                _reply(json.dumps(_ANSWER)),
            )
            provider = _provider(transport)
            self.assertEqual(_ask(provider, "orchestrator")["node_ids"], ["a"])
            _ask(provider, "orchestrator")
        self.assertIn("chat_template_kwargs", transport.payloads[0])
        self.assertNotIn("chat_template_kwargs", transport.payloads[1])
        self.assertNotIn("chat_template_kwargs", transport.payloads[2])
        # The switch, not response_format, was blamed for the 400.
        self.assertIn("response_format", transport.payloads[1])


class ReasoningBudgetTest(unittest.TestCase):
    @staticmethod
    def _deliberate(on_reasoning):
        for i in range(10_000):
            on_reasoning(f"thought {i}. ")
        raise AssertionError("the budget should have cut this stream")

    def test_cut_resumes_with_the_reasoning_carried_forward(self) -> None:
        env = {**_clean_env(), "KUSUDAEMON_REASONING_CLASSIFY": "budget:50"}
        seen: list[str] = []
        with patch.dict(os.environ, env, clear=True):
            transport = _Scripted(self._deliberate, _reply(json.dumps(_ANSWER)))
            provider = _provider(transport)
            with call_scope(role="classify"):
                result = provider.complete_json(
                    [{"role": "user", "content": "decide"}], ORCHESTRATOR_SCHEMA,
                    streaming=True, on_reasoning=seen.append,
                )
        self.assertEqual(result["node_ids"], ["a"])
        # Cut at ~50 tokens (~200 chars), and the caller's sink still saw it.
        self.assertLess(len("".join(seen)), 260)
        resumed = transport.payloads[1]["messages"]
        self.assertEqual(resumed[-2]["role"], "assistant")
        self.assertIn("thought 0.", resumed[-2]["content"])
        self.assertEqual(resumed[-1]["content"], provider_mod._EMIT_NOW_NUDGE)

    def test_second_cut_raises(self) -> None:
        env = {**_clean_env(), "KUSUDAEMON_REASONING_CLASSIFY": "budget:50"}
        with patch.dict(os.environ, env, clear=True):
            transport = _Scripted(self._deliberate, self._deliberate)
            with self.assertRaises(ProviderReasoningBudgetError):
                _ask(_provider(transport), "classify")
        self.assertEqual(len(transport.payloads), 2)

    def test_budget_cut_does_not_spend_the_deadline_retry(self) -> None:
        env = {**_clean_env(), "KUSUDAEMON_REASONING_CLASSIFY": "budget:50"}
        with patch.dict(os.environ, env, clear=True):
            transport = _Scripted(
                self._deliberate,
                provider_mod.ProviderDeadlineError("slow", partial="late thought"),
                _reply(json.dumps(_ANSWER)),
            )
            self.assertEqual(_ask(_provider(transport), "classify")["node_ids"], ["a"])
        self.assertIn("late thought", transport.payloads[2]["messages"][-2]["content"])


class EmptyStreamResumeTest(unittest.TestCase):
    def test_reasoning_without_an_answer_is_carried_forward(self) -> None:
        with patch.dict(os.environ, {**_clean_env(), "KUSUDAEMON_REASONING_CLASSIFY": "on"}, clear=True):
            transport = _Scripted(
                _reply("", reasoning="week 28 is both a Burger Monday and", usage=False),
                _reply(json.dumps(_ANSWER)),
            )
            self.assertEqual(_ask(_provider(transport), "classify")["node_ids"], ["a"])
        resumed = transport.payloads[1]["messages"]
        self.assertIn("week 28 is both a Burger Monday", resumed[-2]["content"])
        self.assertEqual(resumed[-1]["content"], provider_mod._EMIT_NOW_NUDGE)

    def test_truly_empty_stream_still_restarts_plainly(self) -> None:
        with patch.dict(os.environ, {**_clean_env(), "KUSUDAEMON_REASONING_CLASSIFY": "on"}, clear=True):
            transport = _Scripted(_reply("", usage=False), _reply(json.dumps(_ANSWER)))
            _ask(_provider(transport), "classify")
        self.assertEqual(transport.payloads[1]["messages"], transport.payloads[0]["messages"])


class ClassifyRoleStampTest(unittest.TestCase):
    def test_classify_calls_record_the_classify_role(self) -> None:
        roles: list[str | None] = []

        class Recording:
            def complete_json(self, messages, schema, **kwargs):
                roles.append(provider_mod._scoped_role(None))
                return {
                    "files_touched": "1", "artifacts": 1, "answerable_without_exploration": True,
                    "questions": [], "objections": [], "work_kind": "document",
                }

        estimate_scope_full("goal", work_object_none(), Recording())  # type: ignore[arg-type]
        self.assertEqual(roles, ["classify"])


class OrchestratorTraceTest(unittest.TestCase):
    def test_reasoning_is_written_per_decision(self) -> None:
        class Thinking:
            prompts: list[str] = []

            def complete_json(self, messages, schema, on_reasoning=None, **kwargs):
                on_reasoning("a and b are independent; ")
                on_reasoning("start both.")
                return dispatch("a", "b")

        env = {**_clean_env(), "KUSUDAEMON_ORCHESTRATOR_SKIP_FITS": "0"}
        with patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as td:
            run_dir, tree, _, _ = _run(
                Path(td), [{"id": "a"}, {"id": "b"}], {k: [(0.0, OK)] for k in "ab"}, Thinking(),
                max_parallel=2,
            )
            trace = run_dir / "orchestrator" / "reasoning-000.txt"
            self.assertEqual(trace.read_text(encoding="utf-8"), "a and b are independent; start both.")
        self.assertTrue(all(tree.nodes[k].status == "passed" for k in "ab"))

    def test_no_reasoning_writes_no_file(self) -> None:
        class Silent:
            def complete_json(self, messages, schema, **kwargs):
                return dispatch("a", "b")

        env = {**_clean_env(), "KUSUDAEMON_ORCHESTRATOR_SKIP_FITS": "0"}
        with patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as td:
            run_dir, _, _, _ = _run(
                Path(td), [{"id": "a"}, {"id": "b"}], {k: [(0.0, OK)] for k in "ab"}, Silent(),
                max_parallel=2,
            )
            self.assertEqual(list((run_dir / "orchestrator").glob("reasoning-*.txt")), [])


if __name__ == "__main__":
    unittest.main()
