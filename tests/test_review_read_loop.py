"""Tests for goal-directed reviewer read loop (PLAN-REVIEW-READ-LOOP.md §R3)."""

import json
import tempfile
import unittest
from pathlib import Path

from kusudaemon.v1.review_loop import (
    build_unit_slices,
    format_outline,
    run_review_read_loop,
)
from kusudaemon.v1.tree import TaskNode
from kusudaemon.v2.survey import extract_spine_unit_noun


class FakeLoopProvider:
    def __init__(self, turns: list[dict]) -> None:
        self.turns = list(turns)
        self.calls: list[list[dict]] = []

    def complete_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        if not self.turns:
            raise AssertionError(f"FakeLoopProvider out of scripted turns (call #{len(self.calls)})")
        return self.turns.pop(0)


class TestReviewReadLoop(unittest.TestCase):
    def test_build_unit_slices_and_flags(self) -> None:
        text = (
            "#*# Unit 1\nThis is a short unit.\n\n"
            "#*# Unit 2\n" + "word " * 30 + "\n\n"
            "#*# Unit 2\n" + "word " * 30 + "\n\n"  # dup-ordinal
            "#*# Unit 5\n" + "word " * 30 + "\n\n"  # missing-before (3, 4)
            "#*# Unit 100\n" + "word " * 30 + "\n\n"  # out-of-range
        )
        slices = build_unit_slices(text, delim="#*#", expected_range=(1, 10))
        self.assertEqual(len(slices), 5)
        self.assertIn("short(8w)", slices[0].flags)
        self.assertIn("dup-ordinal", slices[2].flags)
        self.assertTrue(any("missing-before" in f for f in slices[3].flags))
        self.assertIn("out-of-range", slices[4].flags)

        outline = format_outline(slices)
        self.assertIn("unit 1 |", outline)
        self.assertIn("dup-ordinal", outline)

    def test_read_loop_fail_fast(self) -> None:
        text = "#*# Unit 1\nGood unit.\n\n#*# Unit 2\n" + "word " * 30 + "\n\n"
        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["rubric1"],
            rubric={"rubric1": "check content"},
        )
        turns = [
            # Turn 1: read unit 1
            {"action": "read", "unit": 1},
            # Turn 2: found defect in unit 1, fail fast
            {
                "action": "verdict",
                "verdict": "fail",
                "items": [{"id": "rubric1", "unit": 1, "pass": False, "defect": "Unit 1 is incomplete"}],
            },
        ]
        provider = FakeLoopProvider(turns)
        verdict = run_review_read_loop(
            node,
            text,
            provider,
            effective_judgment=["rubric1"],
            rubric_lines="rubric1: check content",
            unit_delimiter="#*#",
        )
        self.assertEqual(verdict.verdict, "fail")
        self.assertEqual(len(verdict.items), 1)
        self.assertEqual(verdict.items[0]["defect"], "Unit 1 is incomplete")
        self.assertEqual(verdict.items[0]["unit"], 1)

    def test_read_loop_enforces_coverage_before_pass(self) -> None:
        text = "#*# Unit 1\n" + "word " * 30 + "\n\n#*# Unit 2\n" + "word " * 30 + "\n\n"
        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["rubric1"],
            rubric={"rubric1": "check content"},
        )
        turns = [
            # Turn 1: read only unit 1
            {"action": "read", "unit": 1},
            # Turn 2: prematurely attempts verdict pass without reading unit 2
            {"action": "verdict", "verdict": "pass", "items": []},
            # Turn 3: harness nudged for unread unit 2; now read unit 2
            {"action": "read", "unit": 2},
            # Turn 4: now emit verdict pass
            {"action": "verdict", "verdict": "pass", "items": []},
        ]
        provider = FakeLoopProvider(turns)
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_p = Path(tmp_dir)
            verdict = run_review_read_loop(
                node,
                text,
                provider,
                effective_judgment=["rubric1"],
                rubric_lines="rubric1: check content",
                unit_delimiter="#*#",
                run_dir=run_p,
            )
            self.assertEqual(verdict.verdict, "pass")
            self.assertEqual(len(provider.calls), 4)

            # verify audit record
            audit_p = run_p / "audit" / "n1_read_loop.json"
            self.assertTrue(audit_p.exists())
            data = json.loads(audit_p.read_text(encoding="utf-8"))
            self.assertEqual(data["units_total"], 2)
            self.assertEqual(data["units_read"], 2)
            self.assertEqual(data["turns"], 4)

    def test_read_loop_budget_exhaustion_yields_unavailable_never_pass(self) -> None:
        """§R3.4: when the turn budget runs out the result is `unavailable`,
        never `pass` — a reviewer that never finishes reading must not pass."""
        text = "#*# Unit 1\n" + "word " * 30 + "\n\n#*# Unit 2\n" + "word " * 30 + "\n\n"
        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["rubric1"],
            rubric={"rubric1": "check content"},
        )
        # Reread unit 1 forever: coverage of unit 2 never completes, and no
        # verdict is ever emitted, so the ceil(tokens/cap)+4 budget must run out.
        turns = [{"action": "read", "unit": 1}] * 20
        provider = FakeLoopProvider(turns)
        verdict = run_review_read_loop(
            node,
            text,
            provider,
            effective_judgment=["rubric1"],
            rubric_lines="rubric1: check content",
            unit_delimiter="#*#",
        )
        self.assertEqual(verdict.verdict, "unavailable")
        self.assertEqual(verdict.skip_reason, "read_loop_turn_budget_exhausted")

    def test_read_loop_never_passes_with_units_unread(self) -> None:
        """§R3.3: a `pass` verdict with unread units is rejected and re-asked
        every time — there is no escape hatch that passes with units unread."""
        text = "#*# Unit 1\n" + "word " * 30 + "\n\n#*# Unit 2\n" + "word " * 30 + "\n\n"
        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["rubric1"],
            rubric={"rubric1": "check content"},
        )
        turns = [
            {"action": "read", "unit": 1},
            # Two consecutive premature passes: each must be bounced back
            # for the unread unit, and neither may resolve as `pass`.
            {"action": "verdict", "verdict": "pass", "items": []},
            {"action": "verdict", "verdict": "pass", "items": []},
            {"action": "read", "unit": 2},
            {"action": "verdict", "verdict": "pass", "items": []},
        ]
        provider = FakeLoopProvider(turns)
        verdict = run_review_read_loop(
            node,
            text,
            provider,
            effective_judgment=["rubric1"],
            rubric_lines="rubric1: check content",
            unit_delimiter="#*#",
        )
        self.assertEqual(verdict.verdict, "pass")
        # Both premature passes were consumed as re-asks plus the two
        # reads and the final verdict: 5 model turns, not a 2-turn pass.
        self.assertEqual(len(provider.calls), 5)

    def test_read_loop_turn_carries_at_most_one_chunk(self) -> None:
        """§R3: context stays roughly constant — no turn carries more than one
        chunk of artifact text."""
        text = "#*# Unit 1\n" + "word " * 30 + "\n\n#*# Unit 2\n" + "word " * 30 + "\n\n"
        node = TaskNode(
            id="n1",
            brief="test task",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["rubric1"],
            rubric={"rubric1": "check content"},
        )
        turns = [
            {"action": "read", "unit": 1},
            {"action": "read", "unit": 2},
            {"action": "verdict", "verdict": "pass", "items": []},
        ]
        provider = FakeLoopProvider(turns)
        verdict = run_review_read_loop(
            node,
            text,
            provider,
            effective_judgment=["rubric1"],
            rubric_lines="rubric1: check content",
            unit_delimiter="#*#",
        )
        self.assertEqual(verdict.verdict, "pass")
        for messages in provider.calls:
            user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
            self.assertLessEqual(
                user_text.count("=== Unit "), 1,
                f"turn carries more than one chunk:\n{user_text[:500]}",
            )


class TestSpineUnitNoun(unittest.TestCase):
    """§O4: the spine noun comes from the delimiter exemplar first, so
    'Memorial Day'/'Mother's Day' in a menu goal must not relabel Menu Weeks."""

    def test_menu_week_goal_yields_menu_week(self) -> None:
        goal = (
            "Plan menus for 18 menu weeks, one for each week. Include a Memorial Day "
            "picnic, a Mother's Day brunch, and a birthday dinner.\n"
            "Use '#*#' to separate each menu week (e.g. #*# Menu Week 1: Spring)."
        )
        self.assertEqual(extract_spine_unit_noun(goal), "Menu Week")

    def test_week_goal_yields_week_not_entry(self) -> None:
        goal = (
            "Generate a diary for Sophia, 52 entries in total, one entry per week. "
            "Mention her birthday in week 7.\n"
            "Use '#*#' to separate each week (e.g. #*# Week 1: New Year)."
        )
        self.assertEqual(extract_spine_unit_noun(goal), "Week")


if __name__ == "__main__":
    unittest.main()
