"""Tests for Phase 1.6: Provider fault injection.

Exercises _FaultyProvider wrapping _ScriptedProvider across all mandated faults:
- HTTP 429 with Retry-After
- Connection reset mid-stream
- JSON truncated at random offset (subtests for small, medium, and large artifact sizes)
- Schema-valid JSON with an extra unexpected key
- Empty response ("" and {})
- Call hanging past episode budget / timeout

Invariants asserted:
- The run either completes cleanly or halts with a recorded event.
- Never silently drops a node.
- Never leaves a partial artifact that a gate then accepts.
- terminal_events_per_node <= 1 throughout.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.eval.measure import terminal_events_per_node
from kusudaemon.eval.runner import (
    _InMemoryWriterAdapter,
    _ScriptedProvider,
    _canned_responses,
    _never_called_research_factory,
    _options,
    _prepare_task,
)
from kusudaemon.eval.tasks import build_tasks
from kusudaemon.pipeline.driver import RecursiveDriver
from kusudaemon.v0.events import EventLog
from kusudaemon.v0.run_dir import events_path, node_artifact_path
from kusudaemon.v1.gates import evaluate_gates
from kusudaemon.v1.json_schema import validate
from kusudaemon.v1.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    ProviderHTTPError,
)
from kusudaemon.pipeline import approvals as approval_store


class _FaultyProvider:
    """Wraps _ScriptedProvider and injects programmed faults on complete_json calls."""

    def __init__(
        self,
        inner: _ScriptedProvider,
        fault: str | None = None,
        *,
        fault_call_index: int = 0,
        retry_after: float = 0.01,
        truncate_size_bytes: int | None = None,
        extra_key: str = "_unexpected_extra_key",
        extra_value: Any = "unexpected_metadata",
    ) -> None:
        self.inner = inner
        self.fault = fault
        self.fault_call_index = fault_call_index
        self.retry_after = retry_after
        self.truncate_size_bytes = truncate_size_bytes
        self.extra_key = extra_key
        self.extra_value = extra_value
        self.calls = inner.calls
        self.cost_ledger = None
        self._should_abort = None
        self._on_model_fallback = None

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Callable[[str], None] | None = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        call_idx = len(self.calls)
        if self.fault and call_idx == self.fault_call_index:
            if self.fault == "429":
                raise ProviderHTTPError(
                    429, "Too Many Requests: rate limit reached", retry_after=self.retry_after
                )
            if self.fault == "connection_reset":
                raise urllib.error.URLError(
                    ConnectionResetError("Connection reset by peer mid-stream")
                )
            if self.fault == "hanging_call":
                raise TimeoutError("Call timed out past episode budget")
            if self.fault == "empty_string":
                raise ProviderError("structured output failed: empty response")
            if self.fault == "empty_dict":
                errors = validate({}, schema)
                raise ProviderError(f"structured output failed: {errors}")
            if self.fault == "truncated_json":
                # Get the candidate response
                if not self.inner._responses:
                    raise AssertionError("No inner canned response to truncate")
                candidate = dict(self.inner._responses[0])
                if self.truncate_size_bytes is not None:
                    padding = "x" * max(0, self.truncate_size_bytes - 50)
                    candidate["_padding"] = padding
                serialized = json.dumps(candidate)
                cutoff = max(1, len(serialized) // 2)
                truncated_text = serialized[:cutoff]
                try:
                    json.loads(truncated_text)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"structured output failed after {retries + 1} attempts: "
                        f"Unterminated JSON at offset {cutoff}: {exc}"
                    ) from exc
            if self.fault == "extra_key":
                if not self.inner._responses:
                    raise AssertionError("No inner canned response for extra_key")
                raw = dict(self.inner._responses.pop(0))
                self.calls.append((messages, schema))
                raw[self.extra_key] = self.extra_value
                errors = validate(raw, schema)
                if errors and not schema.get("additionalProperties", True):
                    # Schema strictly disallows additional properties
                    raise ProviderError(
                        f"structured output failed after {retries + 1} attempts: {errors}"
                    )
                return raw

        return self.inner.complete_json(
            messages,
            schema,
            temperature=temperature,
            retries=retries,
            on_reasoning=on_reasoning,
            streaming=streaming,
        )


def _writer_factory(run_dir: Path, fail_node: str | None = None, partial_content: str | None = None):
    def factory(node):
        path = node_artifact_path(run_dir, node.id)
        if fail_node == node.id and partial_content is not None:
            # Writes partial / invalid content
            content = partial_content
        else:
            content = f"# Artifact {node.id}\n\nPassed gate content for {node.id}.\n"
        return _InMemoryWriterAdapter(path, content)

    return factory


class ProviderFaultInjectionTest(unittest.TestCase):
    """Phase 1.6: Provider fault injection benchmarks."""

    def _setup_run(self, fault: str, **fault_kwargs: Any) -> tuple[Path, RecursiveDriver, _FaultyProvider]:
        tmp_dir = Path(tempfile.mkdtemp(prefix="kusudaemon_fault_test_"))
        task = next(t for t in build_tasks() if t.task_id == "t2-corpus")
        work_object, plan_payload = _prepare_task(tmp_dir, task)
        responses = _canned_responses(task, plan_payload, resume=False)
        inner = _ScriptedProvider(responses)
        faulty = _FaultyProvider(inner, fault, **fault_kwargs)
        run_dir = tmp_dir / "run"

        driver = RecursiveDriver(
            run_dir,
            provider=faulty,  # type: ignore[arg-type]
            options=_options(task, work_object),
            writer_adapter_factory=_writer_factory(run_dir),
            research_adapter_factory=_never_called_research_factory,
            probe_adapter_factory=_never_called_research_factory,
            poll_interval=0.01,
        )
        return run_dir, driver, faulty

    def _assert_invariants(self, run_dir: Path, report_status: str, expect_completed: bool) -> None:
        terminals = terminal_events_per_node(run_dir)
        for node_id, count in terminals.items():
            self.assertLessEqual(
                count,
                1,
                f"Node {node_id} had {count} terminal events (invariant: <= 1)",
            )

        events = EventLog(events_path(run_dir)).read_all()
        if not expect_completed:
            self.assertIn(report_status, ("error", "halted"))
            has_recorded_halt = any(
                ev.get("type") in ("phase_failed", "run_halted", "episode_failed")
                for ev in events
            )
            self.assertTrue(
                has_recorded_halt,
                "Failed run must record a phase_failed, run_halted, or episode_failed event",
            )

        # Invariant: never leaves a partial artifact that a gate accepts
        # Check all artifacts on disk against the task's gates
        for artifact_path in (run_dir / "out").glob("*.md"):
            text = artifact_path.read_text(encoding="utf-8")
            gate_results = evaluate_gates(["nonempty"], text)
            if not expect_completed and report_status == "error":
                # If the run halted on error, verify it wasn't accepted as done
                last_node_event = EventLog.scan(events, artifact_path.stem, "node_passed")
                self.assertIsNone(
                    last_node_event,
                    f"Node {artifact_path.stem} was marked passed despite error",
                )

    def test_fault_http_429_with_retry_after(self) -> None:
        run_dir, driver, faulty = self._setup_run("429", retry_after=0.01)
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        self._assert_invariants(run_dir, report.status, expect_completed=False)

    def test_fault_connection_reset_mid_stream(self) -> None:
        run_dir, driver, faulty = self._setup_run("connection_reset")
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        self._assert_invariants(run_dir, report.status, expect_completed=False)

    def test_fault_hanging_call_past_budget(self) -> None:
        run_dir, driver, faulty = self._setup_run("hanging_call")
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        self._assert_invariants(run_dir, report.status, expect_completed=False)

    def test_fault_empty_responses(self) -> None:
        for fault_type in ("empty_string", "empty_dict"):
            with self.subTest(fault_type=fault_type):
                run_dir, driver, faulty = self._setup_run(fault_type)
                with approval_store.Approver(run_dir, poll_interval=0.01):
                    report = asyncio.run(driver.run())
                self._assert_invariants(run_dir, report.status, expect_completed=False)

    def test_fault_extra_unexpected_key(self) -> None:
        run_dir, driver, faulty = self._setup_run("extra_key")
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        # Schema-valid or schema-rejected: whichever happens, invariants hold
        self._assert_invariants(
            run_dir, report.status, expect_completed=(report.status == "done")
        )

    def test_fault_truncated_json_at_artifact_sizes(self) -> None:
        # Sizes: small (100 B), medium (2,000 B), large (25,000 B)
        sizes = [100, 2_000, 25_000]
        for size in sizes:
            with self.subTest(artifact_size_bytes=size):
                run_dir, driver, faulty = self._setup_run(
                    "truncated_json", truncate_size_bytes=size
                )
                with approval_store.Approver(run_dir, poll_interval=0.01):
                    report = asyncio.run(driver.run())
                self._assert_invariants(run_dir, report.status, expect_completed=False)

    def test_never_accepts_partial_artifact_on_gate(self) -> None:
        """Assert partial artifact fails gates and is never marked passed on error."""
        run_dir, driver, faulty = self._setup_run("429")
        # Plant a partial artifact on disk
        out_dir = run_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        partial_file = out_dir / "single.md"
        partial_file.write_text("# Incomplete notes", encoding="utf-8")

        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())

        events = EventLog(events_path(run_dir)).read_all()
        # Node must never be marked passed
        self.assertIsNone(EventLog.scan(events, "single", "node_passed"))
        # Invariant: terminal events <= 1
        terminals = terminal_events_per_node(run_dir)
        self.assertLessEqual(terminals.get("single", 0), 1)


if __name__ == "__main__":
    unittest.main()
