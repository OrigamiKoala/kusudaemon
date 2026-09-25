"""2026-09-25 rollback: the dcf4874 regressions on LongGenBench arm C.

Every arm-C run on dcf4874 classified T3 and 6 of 8 escalated partial
(23-77 %). Four mechanisms, each pinned here:

* ``_http_stream_transport`` read ``effective_deadline`` before assigning it,
  so every streaming role call (orchestrator, triage, reviewer) raised
  ``UnboundLocalError`` -- reviews came back ``unavailable`` and the
  orchestrator always fell back to document order.
* ``node_continuation_progress`` (A4) did not invalidate the §E24
  resume-after-complete replay, so each redispatch replayed the previous
  episode in milliseconds, A4 booked it as free "progress" (``prior_units``
  stayed 0 without a fresh snapshot), and the loop burned the round budget
  (``round budget exhausted``) -- up to 2006 cycles in one run.
  ``node_transport`` had the same hole.
* The C1 classify fast path forced every many-unit goal to T3 with a
  synthetic estimate. Opt-in now; the model estimate plus the output-capacity
  axis decide the tier (T2 for these goals).
* The output-capacity axis stays ON, which is only safe with per-unit stubs:
  a bash-less ``_PROSE`` leaf writing one file clobbers it with one ``write``
  per unit (200-menu-week unit-03 kept only week 52 of 16). Stubs are on for
  decomposed leaves and off for T1 single nodes, which append with bash
  (21/21 complete before dcf4874). Filled stubs stay editable, and the
  all-filled prompt no longer suggests ``part-NN.md`` (sorts before u001).
* NIM's ``Failed to generate completions`` classified ``error`` and cost a
  real attempt each; it is transport now.
* B3 pacing raised a 100-unit T1 episode budget from ~2285 s to the 7200 s
  ceiling. Opt-in now.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import tempfile  # noqa: E402

from kusudaemon.adapters.cli_agent import classify_cli_failure  # noqa: E402
from kusudaemon.pipeline.driver import _budget_seconds  # noqa: E402
from kusudaemon.pipeline.prompts import _artifact_instruction  # noqa: E402
from kusudaemon.v0.run_dir import ensure_leaf_unit_stubs  # noqa: E402
from kusudaemon.v1.round_loop import _wants_unit_stubs  # noqa: E402
from kusudaemon.v6.direct import DIRECT_NODE_ID, SINGLE_NODE_ID  # noqa: E402
from kusudaemon.v0.runner import _completion_consumed  # noqa: E402
from kusudaemon.v1.provider import OpenAICompatibleProvider, call_scope  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode  # noqa: E402
from kusudaemon.v6.tiering import ScopeEstimate, Signals, classify  # noqa: E402

_ROLLBACK_FLAGS = (
    "KUSUDAEMON_TIER_OUTPUT_SIGNALS",
    "KUSUDAEMON_CLASSIFY_FAST_PATH",
    "KUSUDAEMON_UNIT_STUBS",
    "KUSUDAEMON_UNIT_PACING",
)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _ROLLBACK_FLAGS}


class _FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *exc) -> None:
        return None


class StreamTransportDeadlineTest(unittest.TestCase):
    def _call(self, deadline_s, role):
        provider = OpenAICompatibleProvider(api_key="k", base_url="http://unused")
        seen: dict[str, object] = {}

        def fake_consume(lines, on_reasoning, deadline_s=None, t0=None):
            seen["deadline_s"] = deadline_s
            return {"choices": [{"message": {"content": "{}"}}]}

        with patch("kusudaemon.v1.provider.urllib.request.urlopen", return_value=_FakeResponse([])), \
             patch("kusudaemon.v1.provider._consume_sse_lines", side_effect=fake_consume), \
             call_scope(role=role):
            provider._http_stream_transport(
                "http://unused/chat/completions", {"max_tokens": 1024}, {}, None, deadline_s=deadline_s
            )
        return seen["deadline_s"]

    def test_no_deadline_resolves_per_role_instead_of_raising(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KUSUDAEMON_ORCHESTRATOR_DEADLINE_S", None)
            self.assertEqual(self._call(None, "orchestrator"), 60.0)

    def test_explicit_deadline_is_passed_through(self) -> None:
        self.assertEqual(self._call(12.5, "reviewer"), 12.5)


class ReplayInvalidationTest(unittest.TestCase):
    def _consumed_by(self, event_type: str) -> bool:
        completed = {"node_id": "unit-01", "type": "episode_completed", "ts": 100.0}
        events = [completed, {"node_id": "unit-01", "type": event_type, "ts": 101.0}]
        return _completion_consumed(events, "unit-01", completed)

    def test_continuation_progress_forces_a_fresh_episode(self) -> None:
        self.assertTrue(self._consumed_by("node_continuation_progress"))

    def test_transport_failure_forces_a_fresh_episode(self) -> None:
        self.assertTrue(self._consumed_by("node_transport"))

    def test_crash_window_still_replays(self) -> None:
        completed = {"node_id": "unit-01", "type": "episode_completed", "ts": 100.0}
        self.assertFalse(_completion_consumed([completed], "unit-01", completed))


class RollbackDefaultsTest(unittest.TestCase):
    def test_many_unit_single_file_goal_decomposes_by_default(self) -> None:
        signals = Signals(
            breadth_markers=6, goal_tokens=436, named_paths=[], output_markers=1,
            output_targets=2, work_files=1, work_tokens=436,
        )
        estimate = ScopeEstimate(
            files_touched="1", artifacts=1, answerable_without_exploration=True,
            ambiguities=[], objections=[], work_kind="document",
        )
        goal = "Write 100 floors, each about 150 words, into one document."
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertEqual(classify(signals, estimate, goal=goal), "T2")

    def test_unit_budget_uses_pre_pacing_formula_by_default(self) -> None:
        node = TaskNode(
            id="single", brief="Write 100 floors", artifact="out/single.md",
            gates=["units_min:100@#*#"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=100),
        )
        with patch.dict(os.environ, _clean_env(), clear=True):
            # 100 units * 400 tok * 2 / 35 tps = 2285 s, not the 7200 s ceiling.
            self.assertEqual(_budget_seconds(node), 2285)


class UnitStubTest(unittest.TestCase):
    def _leaf(self, node_id: str = "unit-02") -> TaskNode:
        return TaskNode(
            id=node_id, brief="Produce the artifact for Floors 26 to 30.",
            artifact=f"out/{node_id}.md",
            gates=["units_min:5@#*#", "units_range:26-30@#*#"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=5),
        )

    def test_decomposed_leaves_get_stubs_single_nodes_do_not(self) -> None:
        with patch.dict(os.environ, _clean_env(), clear=True):
            self.assertTrue(_wants_unit_stubs(self._leaf()))
            self.assertFalse(_wants_unit_stubs(self._leaf(SINGLE_NODE_ID)))
            self.assertFalse(_wants_unit_stubs(self._leaf(DIRECT_NODE_ID)))
        with patch.dict(os.environ, {"KUSUDAEMON_UNIT_STUBS": "0"}):
            self.assertFalse(_wants_unit_stubs(self._leaf()))

    def test_all_filled_stubs_route_fixes_to_edits_not_new_parts(self) -> None:
        node = self._leaf()
        with tempfile.TemporaryDirectory() as td:
            for stub in ensure_leaf_unit_stubs(td, node):
                stub.write_text(f"#*# Floor {stub.stem[1:]}\ntext\n", encoding="utf-8")
            instruction = _artifact_instruction(node, Path(td))
        self.assertIn("All 5 unit files already hold content", instruction)
        self.assertIn("targeted edit inside that file", instruction)
        self.assertNotIn("part-NN", instruction)


class ProviderFailureClassificationTest(unittest.TestCase):
    def test_nim_generation_failure_is_transport(self) -> None:
        self.assertEqual(
            classify_cli_failure("Error: AI_APICallError: Failed to generate completions"),
            "transport",
        )


if __name__ == "__main__":
    unittest.main()
