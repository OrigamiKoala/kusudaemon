"""Tests for review failure containment and malformed defect handling (PLAN-REVIEW-READ-LOOP.md §R2, §R4.2)."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kusudaemon.v0.events import EventLog
from kusudaemon.v1.provider import (
    OpenAICompatibleProvider,
    ProviderDeadlineError,
    ProviderError,
    _consume_sse_lines,
)
from kusudaemon.v1.reviewer import (
    VERDICT_SCHEMA,
    ReviewVerdict,
    _call_reviewer,
    _review_node_judged,
    is_malformed_defect,
)
from kusudaemon.v1.round_loop import _transition_after_review, _transition_after_writer
from kusudaemon.v1.tree import TaskNode, TaskTree


class FakeFlakyProvider:
    def __init__(self, err: Exception | None = None, canned: dict | None = None) -> None:
        self.err = err
        self.canned = canned
        self.calls: list[list[dict]] = []

    def complete_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        if self.err:
            raise self.err
        return self.canned or {"items": [], "verdict": "pass"}


class TestReviewContainment(unittest.TestCase):
    def test_is_malformed_defect(self) -> None:
        self.assertTrue(is_malformed_defect("clarity", ""))
        self.assertTrue(is_malformed_defect("clarity", "none"))
        self.assertTrue(is_malformed_defect("clarity", "None"))
        self.assertTrue(is_malformed_defect("clarity", "clarity"))
        self.assertTrue(is_malformed_defect("clarity", "'clarity'"))
        self.assertTrue(is_malformed_defect("clarity", "CLARITY"))
        self.assertFalse(is_malformed_defect("clarity", "paragraph 3 is missing a verb"))

    def test_call_reviewer_malformed_defect_reask_and_unavailable(self) -> None:
        first_bad = {
            "verdict": "fail",
            "items": [{"id": "rubric1", "pass": False, "defect": "rubric1"}],
        }
        second_bad = {
            "verdict": "fail",
            "items": [{"id": "rubric1", "pass": False, "defect": "none"}],
        }
        responses = [first_bad, second_bad]

        class MultiProvider:
            def __init__(self) -> None:
                self.count = 0

            def complete_json(self, messages, schema, **kwargs):
                resp = responses[self.count]
                self.count += 1
                return resp

        provider = MultiProvider()
        res = _call_reviewer(
            "rubric1: check something",
            "some artifact text",
            provider,
            effective_judgment=["rubric1"],
        )
        self.assertEqual(res.get("verdict"), "unavailable")
        self.assertEqual(res.get("skip_reason"), "malformed_defect")
        self.assertEqual(provider.count, 2)

    def test_transition_after_review_unavailable_passes_node(self) -> None:
        node = TaskNode(id="n1", brief="test", artifact="out/n1.md", gates=["nonempty"])
        node.status = "awaiting_review"
        tree = TaskTree(nodes={"n1": node})
        verdict = ReviewVerdict(node_id="n1", items=[], verdict="unavailable", skip_reason="provider deadline")

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_p = Path(tmp_dir)
            tree_p = run_p / "tree.json"
            tree.save(tree_p)
            log = EventLog(run_p / "events.jsonl")

            # create initial audit
            audit_p = run_p / "audit" / "n1.json"
            audit_p.parent.mkdir(parents=True, exist_ok=True)
            audit_p.write_text(json.dumps({"review_status": "in_progress"}), encoding="utf-8")

            asyncio.run(
                _transition_after_review(
                    node,
                    tree,
                    tree_p,
                    verdict,
                    max_attempts=3,
                    log=log,
                    run_dir=run_p,
                )
            )

            self.assertEqual(node.status, "passed")
            saved_audit = json.loads(audit_p.read_text(encoding="utf-8"))
            self.assertEqual(saved_audit.get("review_status"), "unavailable")
            event_types = [e["type"] for e in log.read_all()]
            self.assertIn("node_review_unavailable", event_types)

    def test_unavailable_never_charges_attempts(self) -> None:
        """§R4.2: a malformed-defect verdict becomes unavailable, which must
        not cost the writer an attempt (attempts only move on fail)."""
        node = TaskNode(id="n1", brief="test", artifact="out/n1.md", gates=["nonempty"])
        node.status = "awaiting_review"
        node.attempts = 2
        tree = TaskTree(nodes={"n1": node})
        verdict = ReviewVerdict(node_id="n1", items=[], verdict="unavailable", skip_reason="malformed_defect")

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_p = Path(tmp_dir)
            tree_p = run_p / "tree.json"
            tree.save(tree_p)
            log = EventLog(run_p / "events.jsonl")
            with patch.dict(os.environ, {"KUSUDAEMON_REVIEW_UNAVAILABLE": ""}):
                asyncio.run(
                    _transition_after_review(
                        node, tree, tree_p, verdict, max_attempts=3, log=log, run_dir=run_p
                    )
                )
            self.assertEqual(node.status, "passed")
            self.assertEqual(node.attempts, 2)

    def test_triage_failure_emits_event_and_continues(self) -> None:
        """§R5: triage failure is a `triage_failed` event, not a silent pass —
        full review proceeds on the outline path's fallback."""

        class BoomProvider:
            def complete_json(self, messages, schema, **kwargs):
                raise ProviderError("provider request failed [phase=execute role=triage node=n1]: boom")

        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["on_topic"],
            rubric={"on_topic": "stays on the brief"},
        )
        events: list[dict] = []
        reviewer = FakeFlakyProvider(canned={"items": [], "verdict": "pass"})
        with self.assertWarns(UserWarning):
            verdict = _review_node_judged(
                node,
                "Some short artifact text about the brief.",
                reviewer,
                triage_provider=BoomProvider(),
                on_event=events.append,
            )
        self.assertEqual(verdict.verdict, "pass")
        self.assertTrue(any(e.get("type") == "triage_failed" for e in events))

    def test_transport_non_attempt_does_not_charge_attempts(self) -> None:
        """§O1: a transport failure is a non-attempt — pending, attempts
        untouched, non_attempts incremented."""
        node = TaskNode(id="n1", brief="test", artifact="out/n1.md", gates=["nonempty"])
        node.status = "dispatched"
        tree = TaskTree(nodes={"n1": node})
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_p = Path(tmp_dir)
            tree_p = run_p / "tree.json"
            tree.save(tree_p)
            log = EventLog(run_p / "events.jsonl")
            with patch.dict(os.environ, {"KUSUDAEMON_MAX_NON_ATTEMPTS": "6"}):
                asyncio.run(
                    _transition_after_writer(
                        node, tree, tree_p, False, [], 3, log, run_dir=run_p,
                        result_status="transport",
                    )
                )
            self.assertEqual(node.status, "pending")
            self.assertEqual(node.attempts, 0)
            self.assertEqual(node.non_attempts, 1)
            event_types = [e["type"] for e in log.read_all()]
            self.assertIn("node_transport", event_types)

    def test_max_non_attempts_halts_run(self) -> None:
        """§O1: after MAX_NON_ATTEMPTS consecutive non-attempts the node blocks
        with `provider unavailable` (classified transport, quarantined)."""
        node = TaskNode(id="n1", brief="test", artifact="out/n1.md", gates=["nonempty"])
        node.status = "dispatched"
        node.non_attempts = 5
        tree = TaskTree(nodes={"n1": node})
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_p = Path(tmp_dir)
            tree_p = run_p / "tree.json"
            tree.save(tree_p)
            log = EventLog(run_p / "events.jsonl")
            with patch.dict(os.environ, {"KUSUDAEMON_MAX_NON_ATTEMPTS": "6"}):
                asyncio.run(
                    _transition_after_writer(
                        node, tree, tree_p, False, [], 3, log, run_dir=run_p,
                        result_status="transport",
                    )
                )
            self.assertEqual(node.status, "blocked")
            self.assertIn("provider unavailable", node.last_defect)
            event_types = [e["type"] for e in log.read_all()]
            self.assertIn("run_halted", event_types)

    def test_pending_tree_is_neither_complete_nor_blocked(self) -> None:
        """§O2 precondition: a tree with a pending node after max_rounds must
        route to the `escalated (round budget exhausted)` branch — i.e. it is
        not complete and not blocked."""
        node = TaskNode(id="single", brief="test", artifact="out/single.md", gates=["nonempty"])
        node.status = "pending"
        tree = TaskTree(nodes={"single": node})
        self.assertFalse(tree.is_complete())
        self.assertFalse(tree.is_blocked())


def _http_provider(transport=None, stream_transport=None, **kwargs) -> OpenAICompatibleProvider:
    kwargs.setdefault("api_key", "unused")
    kwargs.setdefault("base_retry_delay", 0.0)
    return OpenAICompatibleProvider(
        transport=transport, stream_transport=stream_transport,
        sleep=lambda _s: None, **kwargs,
    )


class TestReviewerTransportSemantics(unittest.TestCase):
    """§R1 hermetic pins: deadline is not retried, verdict caps never double,
    empty completions are transient."""

    def test_deadline_not_resent_by_transport_branch(self) -> None:
        """§R1.3: ProviderDeadlineError propagates out of _call immediately —
        an identical re-send would only repeat the same wait."""
        calls: list[int] = []

        def stream_transport(url, payload, headers, on_reasoning, deadline_s=None):
            calls.append(1)
            raise ProviderDeadlineError("SSE stream wall-clock deadline exceeded (301.0s > 300.0s)")

        provider = _http_provider(stream_transport=stream_transport)
        with self.assertRaises(ProviderDeadlineError):
            provider._call(
                {"model": "m", "messages": [], "temperature": 0.0},
                stream=True, deadline_s=300.0,
            )
        self.assertEqual(len(calls), 1)

    def test_sse_deadline_enforced_while_streaming(self) -> None:
        """§R1.2: a stream that keeps sending past deadline_s raises."""

        def slow_lines():
            yield 'data: {"choices": [{"delta": {"content": "thinking"}}]}'
            yield ""
            time.sleep(0.3)
            yield 'data: {"choices": [{"delta": {"content": "more"}}]}'

        with self.assertRaises(ProviderDeadlineError):
            _consume_sse_lines(slow_lines(), None, deadline_s=0.05)

    def test_reviewer_deadline_retries_once_at_same_cap_with_emit(self) -> None:
        """§R1.4: reviewer/triage deadline → one retry at the same cap with the
        emit-now turn, never the doubling ladder."""
        payloads: list[dict] = []
        calls = {"n": 0}

        def stream_transport(url, payload, headers, on_reasoning, deadline_s=None):
            payloads.append({"max_tokens": payload.get("max_tokens"), "messages": list(payload["messages"])})
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderDeadlineError("SSE stream wall-clock deadline exceeded")
            return {"choices": [{"message": {"content": '{"verdict": "pass", "items": []}'}}]}

        provider = _http_provider(stream_transport=stream_transport, role="reviewer")
        result = provider.complete_json(
            [{"role": "user", "content": "review this"}],
            VERDICT_SCHEMA,
            streaming=True,
        )
        self.assertEqual(result.get("verdict"), "pass")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["max_tokens"], payloads[1]["max_tokens"])
        self.assertIn("Emit the JSON verdict now", payloads[1]["messages"][-1]["content"])

    def test_reviewer_truncation_never_doubles_cap(self) -> None:
        """§R1.4: a reviewer response truncated at the ceiling is retried once
        at the same cap with the emit-now turn."""
        payloads: list[dict] = []
        calls = {"n": 0}

        def transport(url, payload, headers):
            payloads.append({"max_tokens": payload.get("max_tokens"), "messages": list(payload["messages"])})
            calls["n"] += 1
            if calls["n"] == 1:
                return {"choices": [{"message": {"content": '{"verdict": "fail", "items": ['}, "finish_reason": "length"}]}
            return {"choices": [{"message": {"content": '{"verdict": "pass", "items": []}'}}]}

        provider = _http_provider(transport, role="reviewer")
        result = provider.complete_json([{"role": "user", "content": "review this"}], VERDICT_SCHEMA)
        self.assertEqual(result.get("verdict"), "pass")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["max_tokens"], payloads[1]["max_tokens"])
        self.assertIn("Emit the JSON verdict now", payloads[1]["messages"][-1]["content"])

    def test_empty_completion_retried_without_charging_attempt(self) -> None:
        """§R1.6: completion_tokens <= 1 with empty content is transient — with
        retries=0 a charged attempt would raise instead of succeeding."""
        calls = {"n": 0}
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        def transport(url, payload, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"choices": [{"message": {"content": ""}}], "usage": {"completion_tokens": 0}}
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        provider = _http_provider(transport)
        result = provider.complete_json([{"role": "user", "content": "hi"}], schema, retries=0)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
