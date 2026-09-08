"""Tests for PLAN-BENCH-INTEGRITY.md audit repairs."""

from __future__ import annotations

import os
import unittest
import warnings
from unittest.mock import patch

from kusudaemon.eval.common import (
    check_identical_seeds,
    classify_halt,
    dedupe_records,
    parse_flags,
)
from kusudaemon.v1.gates import evaluate_gates
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree
from kusudaemon.v1.writer import _should_offer_split
from kusudaemon.v6.direct import build_direct_node, is_size_defect
from kusudaemon.v6.tiering import (
    ScopeEstimate,
    Signals,
    _declared_target_count,
    classify,
)
from kusudaemon.v7.split import _measured_overrun


class BenchIntegrityCommonTest(unittest.TestCase):
    def test_classify_halt(self) -> None:
        self.assertEqual(classify_halt(None), "ok")
        self.assertEqual(classify_halt(""), "ok")
        self.assertEqual(classify_halt("done"), "ok")
        self.assertEqual(classify_halt("HTTP 404: model not found"), "transport")
        self.assertEqual(classify_halt("status 429: rate limit exceeded"), "transport")
        self.assertEqual(classify_halt("socket read timeout after 180s"), "transport")
        self.assertEqual(classify_halt("KUSUDAEMON_ROLE_TIMEOUT exceeded"), "transport")
        self.assertEqual(classify_halt("cell timeout after 3600s"), "transport")
        self.assertEqual(classify_halt("wall_clock_budget exhausted: 180s remaining"), "budget")
        self.assertEqual(classify_halt("gate failed: max_tokens: 50000 limit"), "agent")
        self.assertEqual(classify_halt("review rejected: rubric unmet"), "agent")

    def test_parse_flags(self) -> None:
        flags = parse_flags("tier_output_signals,KUSUDAEMON_TIER_TRUST_SIGNALS=1 foo=bar")
        self.assertEqual(
            flags,
            {
                "KUSUDAEMON_TIER_OUTPUT_SIGNALS": "1",
                "KUSUDAEMON_TIER_TRUST_SIGNALS": "1",
                "KUSUDAEMON_FOO": "bar",
            },
        )

    def test_check_identical_seeds(self) -> None:
        records = [
            {"task_id": "t1", "arm": "A", "artifact_chars": 100},
            {"task_id": "t1", "arm": "A", "artifact_chars": 100},
            {"task_id": "t2", "arm": "A", "artifact_chars": 200},
            {"task_id": "t2", "arm": "A", "artifact_chars": 205},
        ]
        suspect = check_identical_seeds(records)
        self.assertEqual(len(suspect), 1)
        self.assertEqual(suspect[0]["task_id"], "t1")
        self.assertEqual(suspect[0]["identical_size"], 100)

    def test_dedupe_records(self) -> None:
        recs = [
            {"task_id": "t1", "arm": "A", "seed": 1, "v": 1},
            {"task_id": "t1", "arm": "A", "seed": 1, "v": 2},
            {"task_id": "t1", "arm": "A", "seed": 2, "v": 1},
        ]
        deduped = dedupe_records(recs)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["v"], 2)


class SizeDefectAndTimeoutTest(unittest.TestCase):
    def test_is_size_defect_matches_timeout_and_units(self) -> None:
        self.assertTrue(is_size_defect("max_tokens: 24000 limit exceeded"))
        self.assertTrue(is_size_defect("episode_timeout: episode wall clock exceeded"))
        self.assertTrue(is_size_defect("episode did not complete"))
        self.assertTrue(is_size_defect("units_min: 100 entries expected"))
        self.assertTrue(is_size_defect("units_found:50 < units_expected:100"))
        self.assertFalse(is_size_defect("nonempty: artifact is empty"))
        self.assertFalse(is_size_defect("syntax error"))


class OutputSignalsTieringTest(unittest.TestCase):
    def test_declared_target_count(self) -> None:
        self.assertEqual(_declared_target_count("Generate 100 blocks of text"), 100)
        self.assertEqual(_declared_target_count("Create fifty entries for diary"), 50)
        self.assertEqual(_declared_target_count("Simple task with no counts"), 0)

    def test_tier_output_signals_flag(self) -> None:
        goal = "A benchmark consisting of 100 blocks"
        signals = Signals(
            work_tokens=1000,
            work_files=1,
            goal_tokens=50,
            output_targets=1,
        )
        estimate = ScopeEstimate(
            files_touched="1",
            artifacts=1,
            answerable_without_exploration=True,
            ambiguities=False,
            objections=False,
            work_kind="prose",
        )

        # Default (flag off): classifies as T0
        with patch.dict(os.environ, {"KUSUDAEMON_TIER_OUTPUT_SIGNALS": "0"}):
            tier = classify(signals, estimate, goal=goal)
            self.assertEqual(tier, "T0")

        # Flag on: floors at T2 because output targets 100 >= 8
        with patch.dict(os.environ, {"KUSUDAEMON_TIER_OUTPUT_SIGNALS": "1"}):
            tier = classify(signals, estimate, goal=goal)
            self.assertEqual(tier, "T2")


class NodeBudgetAndGatesTest(unittest.TestCase):
    def test_node_budget_units_expected_serialization(self) -> None:
        node = TaskNode(
            id="test_node",
            brief="Write 100 blocks",
            artifact="out/test_node.md",
            gates=["nonempty"],
            budget=NodeBudget(tokens=50_000, calls=15, units_expected=100),
        )
        tree = TaskTree(nodes={node.id: node})
        d = tree.nodes["test_node"].to_dict()
        self.assertEqual(d["budget"]["units_expected"], 100)

        restored = TaskNode.from_dict(d)
        self.assertEqual(restored.budget.units_expected, 100)

    def test_build_direct_node_extracts_units(self) -> None:
        node = build_direct_node("A book consisting of 25 chapters")
        self.assertEqual(node.budget.units_expected, 25)

    def test_units_min_gate(self) -> None:
        text = "### Block 1\nContent 1\n### Block 2\nContent 2\n"
        pass_res = evaluate_gates(["units_min:2"], text)
        self.assertTrue(pass_res[0].passed)

        fail_res = evaluate_gates(["units_min:5"], text)
        self.assertFalse(fail_res[0].passed)
        self.assertIn("units_found:2 < units_expected:5", fail_res[0].detail)

    def test_should_offer_split_with_units(self, tmp_path_factory: Any = None) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            rdir = Path(td)
            node = TaskNode(
                id="n1",
                brief="100 entries",
                artifact="out/n1.md",
                gates=["nonempty"],
                budget=NodeBudget(units_expected=100),
            )
            self.assertTrue(_should_offer_split(rdir, node))
            self.assertTrue(_measured_overrun(rdir, node))


class CodexAdapterTomliWarningTest(unittest.TestCase):
    def test_mcp_server_overrides_warning(self) -> None:
        from kusudaemon.adapters.codex import mcp_server_overrides
        with patch.dict("sys.modules", {"tomllib": None, "tomli": None}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                res = mcp_server_overrides("/nonexistent/path.toml")
                self.assertEqual(res, [])
                self.assertTrue(any("tomli" in str(warning.message) for warning in w))


if __name__ == "__main__":
    unittest.main()
