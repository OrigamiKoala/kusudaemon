"""PLAN-SWEEP-REPAIR.md §J9/§J10 — unit counting, retry framing, parts contract,
and the spine rebuild a tier escalation needs.

All hermetic: no provider calls, no agent binary, no network.

The defect these pin: three private copies of the unit regex spelled the
``#*#`` alternative without a trailing ``\\s*``, so ``#*# Floor 7:`` — the
delimiter LongGenBench actually writes — matched none of them. Every unit
count the round loop and the retry framing computed was therefore 0, which
made the harness tell a writer holding 60 finished floors that it had written
"0 of 100 units" and should "resume at unit 1".
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions
from kusudaemon.pipeline.prompts import _artifact_instruction, _prior_attempt_artifact, segments
from kusudaemon.v0.events import EventLog
from kusudaemon.v1.gates import count_units, evaluate_gates, first_unit_key, unit_pattern
from kusudaemon.v1.reviewer import unit_delimiter_from_gates
from kusudaemon.v1.round_loop import _transition_after_writer
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree
from kusudaemon.pipeline.run_dir import tier_path


def _floors(first: int, last: int) -> str:
    return "".join(
        f"#*# Floor {n}: A floor with facilities, features and design elements.\n\n"
        for n in range(first, last + 1)
    )


class CountUnitsTest(unittest.TestCase):
    """The regex regression itself."""

    def test_counts_longgenbench_delimiter_with_a_space_after_it(self) -> None:
        text = _floors(1, 20)
        # With the run's resolved delimiter...
        self.assertEqual(count_units(text, "#*#"), 20)
        # ...and without it, via the noun list. The old private copy in
        # round_loop returned 0 here, which is the whole defect.
        self.assertEqual(count_units(text), 20)

    def test_counts_delimiter_written_flush_against_the_noun(self) -> None:
        self.assertEqual(count_units("#*#Floor 1: a\n#*#Floor 2: b\n"), 2)

    def test_delimiter_not_at_line_start_still_counts(self) -> None:
        self.assertEqual(count_units("intro #*# one #*# two", "#*#"), 2)

    def test_falls_back_to_markdown_headings(self) -> None:
        self.assertEqual(count_units("# Alpha\n\nbody\n\n# Beta\n\nbody\n"), 2)

    def test_empty_text_counts_nothing(self) -> None:
        self.assertEqual(count_units("", "#*#"), 0)
        self.assertEqual(count_units(""), 0)


class FirstUnitKeyTest(unittest.TestCase):
    """Shrink classification needs to know *which* unit the artifact starts
    at. A bare delimiter compares equal to itself, so the ordinal is the key."""

    def test_ordinal_distinguishes_a_clobbered_artifact(self) -> None:
        self.assertEqual(first_unit_key(_floors(1, 5), "#*#"), "1")
        self.assertEqual(first_unit_key(_floors(41, 45), "#*#"), "41")
        self.assertNotEqual(
            first_unit_key(_floors(1, 5), "#*#"), first_unit_key(_floors(41, 45), "#*#")
        )

    def test_same_starting_unit_compares_equal_after_a_scoped_trim(self) -> None:
        self.assertEqual(
            first_unit_key(_floors(1, 20), "#*#"), first_unit_key(_floors(1, 5), "#*#")
        )

    def test_no_units_gives_empty_key(self) -> None:
        self.assertEqual(first_unit_key("just prose", "#*#"), "")

    def test_unit_pattern_without_delimiter_is_the_noun_pattern(self) -> None:
        self.assertTrue(unit_pattern().search("#*# Floor 3: x"))
        self.assertTrue(unit_pattern("#*#").search("#*# Floor 3: x"))


class UnitsMinGateTest(unittest.TestCase):
    """The gate already counted correctly; it must keep doing so now that it
    delegates to the shared counter."""

    def test_gate_passes_on_a_complete_document(self) -> None:
        results = evaluate_gates(["units_min:100@#*#"], _floors(1, 100))
        self.assertTrue(results[0].passed, results[0].detail)

    def test_gate_reports_the_real_shortfall(self) -> None:
        results = evaluate_gates(["units_min:100@#*#"], _floors(1, 60))
        self.assertFalse(results[0].passed)
        self.assertIn("units_found:60", results[0].detail)

    def test_malformed_limit_still_fails_loudly(self) -> None:
        results = evaluate_gates(["units_min:abc@#*#"], _floors(1, 10))
        self.assertFalse(results[0].passed)
        self.assertIn("malformed", results[0].detail)


class UnitDelimiterFromGatesTest(unittest.TestCase):
    def test_reads_the_delimiter_off_a_hard_gate(self) -> None:
        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["units_min:100@#*#"])
        self.assertEqual(unit_delimiter_from_gates(node), "#*#")

    def test_reads_the_delimiter_off_a_warn_gate(self) -> None:
        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"], warn_gates=["units_min:8@==="])
        self.assertEqual(unit_delimiter_from_gates(node), "===")

    def test_absent_gate_yields_empty_string(self) -> None:
        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"])
        self.assertEqual(unit_delimiter_from_gates(node), "")


class TimeoutDefectNamesRealProgressTest(unittest.TestCase):
    """The end-to-end shape of the defect: a timed-out episode whose parts
    directory holds 60 of 100 floors must be told to resume at 61."""

    def _run(self, run_dir: Path, node: TaskNode) -> EventLog:
        tree_path = run_dir / "tree.json"
        log = EventLog(run_dir / "events.jsonl")
        tree = TaskTree(nodes={node.id: node})
        tree.save(tree_path)
        asyncio.run(
            _transition_after_writer(
                node,
                tree,
                tree_path,
                episode_ok=False,
                gate_results=[],
                max_attempts=3,
                log=log,
                run_dir=run_dir,
                result_status="timeout",
            )
        )
        return log

    def test_resume_point_is_the_next_unwritten_unit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts = run_dir / "out" / "single"
            parts.mkdir(parents=True)
            (parts / "part-01.md").write_text(_floors(1, 20), encoding="utf-8")
            (parts / "part-02.md").write_text(_floors(21, 40), encoding="utf-8")
            (parts / "part-03.md").write_text(_floors(41, 60), encoding="utf-8")
            node = TaskNode(
                id="single",
                brief="b",
                artifact="out/single.md",
                gates=["units_min:100@#*#"],
                status="dispatched",
                budget=NodeBudget(tokens=50_000, calls=15, units_expected=100),
            )
            self._run(run_dir, node)

            self.assertIn("60 of 100 units written", node.last_defect)
            self.assertIn("resume at unit 61", node.last_defect)
            self.assertNotIn("ended with 0 of 100", node.last_defect)

    def test_an_empty_artifact_still_reports_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            (run_dir / "out").mkdir(parents=True)
            node = TaskNode(
                id="single",
                brief="b",
                artifact="out/single.md",
                gates=["units_min:100@#*#"],
                status="dispatched",
                budget=NodeBudget(tokens=50_000, calls=15, units_expected=100),
            )
            self._run(run_dir, node)
            self.assertIn("0 of 100 units written", node.last_defect)


class RetryFramingTest(unittest.TestCase):
    """§L1's append framing is gated on the same count, so it never fired."""

    def _node(self) -> TaskNode:
        return TaskNode(
            id="single",
            brief="b",
            artifact="out/single.md",
            gates=["units_min:100@#*#"],
            budget=NodeBudget(tokens=50_000, calls=15, units_expected=100),
            last_defect="episode_timeout: episode ended with 60 of 100 units written; resume at unit 61",
        )

    def test_partial_artifact_gets_append_framing_not_the_whole_document(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts = run_dir / "out" / "single"
            parts.mkdir(parents=True)
            (parts / "part-01.md").write_text(_floors(1, 60), encoding="utf-8")
            node = self._node()
            retry = dict(segments(node, run_dir)).get("retry", "")

            self.assertIn("units 1–60 of 100", retry)
            self.assertIn("Append units 61–100", retry)
            self.assertIn("Do not rewrite them", retry)
            # The prior artifact is NOT inlined under "fix it in place".
            self.assertNotIn("fix it in place", retry)

    def test_over_cap_anchor_names_the_last_unit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts = run_dir / "out" / "single"
            parts.mkdir(parents=True)
            (parts / "part-01.md").write_text(_floors(1, 100), encoding="utf-8")
            node = self._node()
            notice = _prior_attempt_artifact(node, run_dir, ceiling_tokens=100)

            self.assertIsNotNone(notice)
            self.assertIn("100 units currently on disk", notice)
            self.assertIn("resume at unit 101", notice)
            self.assertIn("Floor 100", notice)


class PartsContractTest(unittest.TestCase):
    """§J10: a writer that complied with the parts offer must not be told to
    merge its parts back into the single file."""

    def _node(self) -> TaskNode:
        return TaskNode(id="single", brief="b", artifact="out/single.md", gates=["units_min:100@#*#"])

    def test_existing_parts_are_named_as_the_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts = run_dir / "out" / "single"
            parts.mkdir(parents=True)
            for name in ("part-01.md", "part-02.md"):
                (parts / name).write_text("x", encoding="utf-8")

            text = _artifact_instruction(self._node(), run_dir)
            self.assertIn("already holds 2", text)
            self.assertIn("part-01.md", text)
            self.assertIn("They are already combined.", text)
            self.assertIn("Do not merge, concatenate or copy", text)
            # The old contradiction, gone.
            self.assertNotIn("That file is the deliverable; nothing else", text)

    def test_no_parts_yet_offers_both_layouts_without_contradiction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            (run_dir / "out").mkdir(parents=True)
            text = _artifact_instruction(self._node(), run_dir)
            self.assertIn("Whichever of those two layouts you choose", text)
            self.assertIn("Do not do both", text)
            self.assertIn("Do not re-emit the whole file", text)

    def test_ascii_punctuation_rule_survives_on_both_branches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts = run_dir / "out" / "single"
            parts.mkdir(parents=True)
            bare = _artifact_instruction(self._node(), run_dir)
            (parts / "part-01.md").write_text("x", encoding="utf-8")
            withparts = _artifact_instruction(self._node(), run_dir)
            for text in (bare, withparts):
                self.assertIn("plain ASCII punctuation", text)


class _NoSurveyDriver(RecursiveDriver):
    """Counts _phase_survey invocations without performing one."""

    def __init__(self, run_dir: Path, *, raises: bool = False, units: int = 0) -> None:
        super().__init__(
            run_dir,
            provider=None,  # type: ignore[arg-type]
            options=RunOptions(goal="Write 100 floors, one per floor"),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
        )
        self.survey_calls = 0
        self._raises = raises
        self._units = units

    async def _phase_survey(self) -> None:  # type: ignore[override]
        self.survey_calls += 1
        if self._raises:
            raise RuntimeError("no source and no declared units")
        from kusudaemon.v2.survey import SpineUnit, save_spine

        save_spine(
            self.run_dir,
            [
                SpineUnit(id=f"u{i}", label=f"Unit {i}", tokens=10, start_chunk=i, end_chunk=i)
                for i in range(self._units)
            ],
        )


def _write_tier(run_dir: Path, tier: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    tier_path(run_dir).write_text(
        json.dumps(
            {
                "tier": tier,
                "measured_tier": tier,
                "override": None,
                "needs_intake": False,
                "needs_explore": False,
                "signals": {},
                "estimate": {},
                "ts": 0,
            }
        ),
        encoding="utf-8",
    )


class SpineRebuiltForPlanTest(unittest.TestCase):
    """§J8: _phase_explore skips the survey while the run is T1. When a size
    defect escalates T1 -> T2 mid-execute, explore has already finished, so
    plan used to arrive with no spine and fall straight through to
    build_single_node_tree — the escalation bought no decomposition."""

    def test_missing_spine_is_built_when_a_plan_phase_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            _write_tier(run_dir, "T2")
            driver = _NoSurveyDriver(run_dir, units=4)

            asyncio.run(driver._ensure_spine_for_plan())

            self.assertEqual(driver.survey_calls, 1)
            self.assertTrue((run_dir / "spine.json").exists())
            events = EventLog(run_dir / "events.jsonl").read_all()
            built = [e for e in events if e.get("type") == "spine_built_for_plan"]
            self.assertEqual(len(built), 1)
            self.assertEqual(built[0]["units"], 4)

    def test_existing_spine_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            _write_tier(run_dir, "T2")
            (run_dir / "spine.json").write_text("[]", encoding="utf-8")
            driver = _NoSurveyDriver(run_dir, units=4)

            asyncio.run(driver._ensure_spine_for_plan())

            self.assertEqual(driver.survey_calls, 0)

    def test_a_tier_with_no_plan_phase_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            _write_tier(run_dir, "T1")
            driver = _NoSurveyDriver(run_dir, units=4)

            asyncio.run(driver._ensure_spine_for_plan())

            self.assertEqual(driver.survey_calls, 0)
            self.assertFalse((run_dir / "spine.json").exists())

    def test_a_corpus_less_goal_falls_back_instead_of_failing(self) -> None:
        """§D4's loud raise must keep landing on the single-node fallback —
        the same place the pre-escalation run landed."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            _write_tier(run_dir, "T2")
            driver = _NoSurveyDriver(run_dir, raises=True)

            asyncio.run(driver._ensure_spine_for_plan())  # must not raise

            self.assertEqual(driver.survey_calls, 1)
            self.assertFalse((run_dir / "spine.json").exists())
            events = EventLog(run_dir / "events.jsonl").read_all()
            skipped = [e for e in events if e.get("phase") == "survey_for_plan"]
            self.assertEqual(len(skipped), 1)
            self.assertIn("RuntimeError", skipped[0]["reason"])


if __name__ == "__main__":
    unittest.main()
