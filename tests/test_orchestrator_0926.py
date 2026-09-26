"""2026-09-26 orchestrator and writer-length repairs (first live run on c407d1b).

200-menu-week arm C seed 1 scored 2/52. Measured, not guessed:

* The first orchestrator call ran 600 s, and its stream ended with no content.
  The empty-completion retry then answered ``wait`` with nothing running. The
  cap-scaled ~1092 s deadline never fired. Now one fixed budget (90 s) covers
  the whole decision, retries included, and the round loop waits the same
  budget.
* The correction for that answer dispatched ``ready[:1]``, and nothing re-asks
  the orchestrator until a node finishes, so 2 of 3 slots sat idle for 21
  minutes. Corrections now fill every free slot.
* All three nodes were fresh and fit the three free slots, so the call had no
  ordering or retry to judge. Such decisions are now made without a call.
* With bash, a leaf ran ``wc -w`` and rewrote unit 1 toward "200-220 words"
  until its context degenerated (1 of 18 units). The writer prompt now says a
  per-unit length is a target and a finished unit is not rewritten to hit it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kusudaemon.pipeline.prompts import _artifact_instruction  # noqa: E402
from kusudaemon.v0.run_dir import ensure_leaf_unit_stubs  # noqa: E402
from kusudaemon.v1.orchestrator import (  # noqa: E402
    ORCHESTRATOR_SCHEMA,
    orchestrator_deadline_s,
    parse_orchestrator_decision,
)
from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderDeadlineError,
    call_scope,
)
from kusudaemon.v1.tree import NodeBudget, TaskNode  # noqa: E402
from test_event_driven_orchestrator import OK, ScriptedOrchestrator, _run, dispatch  # noqa: E402

_FLAGS = (
    "KUSUDAEMON_ORCHESTRATOR_DEADLINE_S",
    "KUSUDAEMON_ORCHESTRATOR_SKIP_FORCED",
    "KUSUDAEMON_ORCHESTRATOR_SKIP_FITS",
)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _FLAGS}


class CorrectionFillsSlotsTest(unittest.TestCase):
    def test_wait_with_nothing_running_fills_every_free_slot(self) -> None:
        decision = parse_orchestrator_decision(
            {"action": "wait", "node_ids": [], "wait_on": [], "reason": "need results"},
            ready=["unit-01", "unit-02", "unit-03"],
            in_flight=[],
            free_slots=3,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertEqual(decision.node_ids, ["unit-01", "unit-02", "unit-03"])
        self.assertIn("would never wake", decision.corrections[0])

    def test_fill_stops_at_free_slots(self) -> None:
        decision = parse_orchestrator_decision(
            {"action": "wait", "node_ids": [], "wait_on": [], "reason": ""},
            ready=["a", "b", "c"],
            in_flight=[],
            free_slots=2,
        )
        self.assertEqual(decision.node_ids, ["a", "b"])

    def test_empty_dispatch_with_nothing_running_fills_every_free_slot(self) -> None:
        decision = parse_orchestrator_decision(
            dispatch("not-a-node"), ready=["a", "b"], in_flight=[], free_slots=2
        )
        self.assertEqual(decision.node_ids, ["a", "b"])


class FitsSkipTest(unittest.TestCase):
    def test_fresh_nodes_that_fit_spend_no_call(self) -> None:
        class RefusingOrchestrator:
            calls = 0

            def complete_json(self, messages, schema, **kwargs):
                self.calls += 1
                raise AssertionError("every ready node fits a free slot")

        orch = RefusingOrchestrator()
        with patch.dict(os.environ, _clean_env(), clear=True), tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                {k: [(0.2, OK)] for k in "abc"}, orch, max_parallel=3,
            )
        self.assertEqual(orch.calls, 0)
        self.assertTrue(all(tree.nodes[k].status == "passed" for k in "abc"))
        skipped = [e for e in events if e["type"] == "orchestrator_call_skipped"]
        self.assertEqual(skipped[0]["node_ids"], ["a", "b", "c"])
        self.assertEqual(skipped[0]["reason"], "forced choice: every dispatchable node fits a free slot")
        # All three ran at once instead of one after another.
        self.assertLess(max(tl.starts[k][0] for k in "abc"), min(tl.ends[k][0] for k in "abc"))

    def test_a_retry_still_goes_to_the_orchestrator(self) -> None:
        orch = ScriptedOrchestrator(lambda n, p: dispatch("a"))
        with patch.dict(os.environ, _clean_env(), clear=True), tempfile.TemporaryDirectory() as td:
            _, tree, _, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}],
                {"a": [(0.0, ""), (0.0, OK)], "b": [(0.3, OK)]}, orch, max_parallel=2,
            )
        self.assertEqual(tree.nodes["a"].status, "passed")
        # The first decision (two fresh nodes, two slots) is skipped; a's
        # retry is a real call.
        self.assertGreaterEqual(len(orch.prompts), 1)
        self.assertIn("a attempts=1/", orch.prompts[0])

    def test_opt_outs_restore_the_call(self) -> None:
        for flag in ("KUSUDAEMON_ORCHESTRATOR_SKIP_FITS", "KUSUDAEMON_ORCHESTRATOR_SKIP_FORCED"):
            orch = ScriptedOrchestrator(lambda n, p: dispatch("a", "b"))
            env = {**_clean_env(), flag: "0"}
            with patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as td:
                _, tree, _, events = _run(
                    Path(td), [{"id": "a"}, {"id": "b"}], {k: [(0.0, OK)] for k in "ab"}, orch,
                    max_parallel=2,
                )
            self.assertEqual(len(orch.prompts), 1, flag)
            self.assertFalse(any(e["type"] == "orchestrator_call_skipped" for e in events), flag)


class OrchestratorBudgetTest(unittest.TestCase):
    def test_default_is_ninety_seconds_and_env_overrides(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(orchestrator_deadline_s(), 90.0)
        with patch.dict(os.environ, {"KUSUDAEMON_ORCHESTRATOR_DEADLINE_S": "45"}):
            self.assertEqual(orchestrator_deadline_s(), 45.0)

    def _provider(self, fake) -> OpenAICompatibleProvider:
        provider = OpenAICompatibleProvider(api_key="k", base_url="http://unused")
        provider._stream_transport_impl = fake
        return provider

    def test_retries_share_one_budget(self) -> None:
        seen: list[float] = []

        def fake(url, payload, headers, on_reasoning, deadline_s=None):
            seen.append(deadline_s)
            time.sleep(0.15)
            raise ProviderDeadlineError("cut", partial="thinking")

        provider = self._provider(fake)
        with patch.dict(os.environ, {"KUSUDAEMON_ORCHESTRATOR_DEADLINE_S": "0.3"}), \
                call_scope(role="orchestrator"):
            with self.assertRaises(ProviderDeadlineError):
                provider.complete_json([{"role": "user", "content": "x"}], ORCHESTRATOR_SCHEMA, streaming=True)
        self.assertEqual(len(seen), 2)
        self.assertLessEqual(seen[0], 0.3)
        self.assertLess(seen[1], seen[0] - 0.1)

    def test_empty_stream_cannot_restart_past_the_budget(self) -> None:
        # The live case: a stream that ends with no content was retried from
        # scratch as a "transient empty completion", once per 600 s.
        calls = 0

        def fake(url, payload, headers, on_reasoning, deadline_s=None):
            nonlocal calls
            calls += 1
            time.sleep(0.25)
            return {"choices": [{"message": {"content": ""}}]}

        provider = self._provider(fake)
        with patch.dict(os.environ, {"KUSUDAEMON_ORCHESTRATOR_DEADLINE_S": "0.2"}), \
                call_scope(role="orchestrator"):
            with self.assertRaisesRegex(ProviderDeadlineError, "budget"):
                provider.complete_json([{"role": "user", "content": "x"}], ORCHESTRATOR_SCHEMA, streaming=True)
        self.assertEqual(calls, 1)


class LengthTargetPromptTest(unittest.TestCase):
    def _leaf(self) -> TaskNode:
        return TaskNode(
            id="unit-01", brief="Produce Menu Weeks 1 to 5.", artifact="out/unit-01.md",
            gates=["units_min:5@#*#"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=5),
        )

    def test_every_layout_says_length_is_a_target(self) -> None:
        node = self._leaf()
        with tempfile.TemporaryDirectory() as td:
            single = _artifact_instruction(node, Path(td))
            ensure_leaf_unit_stubs(td, node)
            stubs = _artifact_instruction(node, Path(td))
            for stub in sorted((Path(td) / "out" / "unit-01").glob("u*.md")):
                stub.unlink()
            (Path(td) / "out" / "unit-01" / "part-01.md").write_text("x", encoding="utf-8")
            parts = _artifact_instruction(node, Path(td))
        for name, text in (("single", single), ("stubs", stubs), ("parts", parts)):
            self.assertIn("is a target, not an exact requirement", text, name)
            self.assertIn("Do not count a finished unit's words", text, name)


if __name__ == "__main__":
    unittest.main()
