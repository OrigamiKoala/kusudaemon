from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kusudaemon.adapters.cli_agent import classify_cli_failure
from kusudaemon.pipeline.driver import _budget_seconds, RunOptions, RecursiveDriver
from kusudaemon.pipeline.prompts import _artifact_instruction, build_node_prompt
from kusudaemon.types import EpisodeBudget
from kusudaemon.v0.events import EventLog
from kusudaemon.v0.run_dir import (
    ensure_leaf_unit_stubs,
    extract_leaf_unit_range,
    node_artifact_text,
    node_parts_dir,
)
from kusudaemon.v1.gates import GateResult, evaluate_gates
from kusudaemon.v1.orchestrator import (
    orchestrator_deadline_s,
    orchestrator_max_tokens,
)
from kusudaemon.v1.provider import OpenAICompatibleProvider, call_scope
from kusudaemon.v1.round_loop import _transition_after_writer
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree
from kusudaemon.v2.survey import units_per_leaf_capacity
from kusudaemon.v3.run_dir import restore_attempt_snapshot


class WallClockBrainstormTest(unittest.TestCase):
    def test_a1_extract_leaf_unit_range_and_stubs(self) -> None:
        node = TaskNode(
            id="unit-02",
            brief="Floors 17 to 40",
            artifact="out/unit-02.md",
            gates=["units_min:24@Floor"],
        )
        rng = extract_leaf_unit_range(node)
        self.assertEqual(rng, (17, 40))

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            created = ensure_leaf_unit_stubs(run_dir, node)
            self.assertEqual(len(created), 24)
            self.assertEqual(created[0].name, "u017.md")
            self.assertEqual(created[-1].name, "u040.md")
            for p in created:
                self.assertTrue(p.exists())
                self.assertEqual(p.stat().st_size, 0)

            # Pre-existing non-empty file is not overwritten
            created[0].write_text("# Floor 17\nDone", encoding="utf-8")
            created_again = ensure_leaf_unit_stubs(run_dir, node)
            self.assertEqual(len(created_again), 24)
            self.assertEqual(created[0].read_text(encoding="utf-8"), "# Floor 17\nDone")

    def test_a2_snapshot_restore_unions_units(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            parts_d = node_parts_dir(run_dir, "unit-01")
            parts_d.mkdir(parents=True)
            (parts_d / "u001.md").write_text("# Unit 1", encoding="utf-8")
            (parts_d / "u002.md").write_text("# Unit 2", encoding="utf-8")

            # Snapshot has u001 and u002
            snap_dir = run_dir / "versions" / "unit-01" / "attempt_100"
            snap_dir.mkdir(parents=True)
            (snap_dir / "u001.md").write_text("# Unit 1", encoding="utf-8")
            (snap_dir / "u002.md").write_text("# Unit 2", encoding="utf-8")

            # Current state: u001 was deleted/emptied, but u003 was newly written
            (parts_d / "u001.md").unlink()
            (parts_d / "u003.md").write_text("# Unit 3", encoding="utf-8")

            restore_attempt_snapshot(run_dir, "unit-01", snap_dir)

            # Union: u001 restored from snapshot, u002 restored from snapshot, u003 preserved
            self.assertTrue((parts_d / "u001.md").exists())
            self.assertTrue((parts_d / "u002.md").exists())
            self.assertTrue((parts_d / "u003.md").exists())
            self.assertEqual((parts_d / "u003.md").read_text(encoding="utf-8"), "# Unit 3")

    def test_a3_prompt_forbids_rewriting_and_names_empty_stubs(self) -> None:
        node = TaskNode(
            id="unit-01",
            brief="Floors 1 to 5",
            artifact="out/unit-01.md",
            gates=["units_min:5@Floor"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=5),
        )
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            stubs = ensure_leaf_unit_stubs(run_dir, node)
            stubs[0].write_text("# Floor 1\ncontent", encoding="utf-8")

            instruction = _artifact_instruction(node, run_dir)
            self.assertIn("Empty unit files to fill (4):", instruction)
            self.assertIn("targeted edit inside that file", instruction)
            self.assertNotIn("part-NN", instruction)
            self.assertIn("u001.md", instruction)

            # Retry prompt lists empty stubs without "append"
            node.last_defect = "episode_timeout: episode ended with 1 of 5 units written; resume at unit 2"
            prompt = build_node_prompt(node, run_dir)
            self.assertIn("Continuation: write only the remaining 4 empty unit files", prompt)
            self.assertIn("Do not rewrite finished units", prompt)
            self.assertIn("targeted edit inside a finished unit's file is fine", prompt)

    def test_a4_continuation_progress_does_not_charge_attempts(self) -> None:
        node = TaskNode(
            id="unit-01",
            brief="Floors 1 to 20",
            artifact="out/unit-01.md",
            gates=["units_min:20@Floor"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=20),
        )
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            stubs = ensure_leaf_unit_stubs(run_dir, node)
            # Write 5 units
            for i in range(5):
                stubs[i].write_text(f"# Floor {i+1}\ncontent", encoding="utf-8")

            tree = TaskTree(nodes={node.id: node})
            log = EventLog(run_dir / "events.jsonl")

            asyncio.run(
                _transition_after_writer(
                    node,
                    tree,
                    run_dir / "tree.json",
                    episode_ok=False,
                    gate_results=evaluate_gates(node.gates, node_artifact_text(run_dir, node.id)),
                    max_attempts=3,
                    log=log,
                    run_dir=run_dir,
                    result_status="timeout",
                )
            )

            # 5 units written > 0 prior units -> continuation progress without attempt penalty
            self.assertEqual(node.status, "pending")
            self.assertEqual(node.attempts, 0)
            events = log.read_all()
            self.assertTrue(any(e.get("type") == "node_continuation_progress" for e in events))

    def test_b1_units_per_leaf_capacity_pace_clamp(self) -> None:
        # 500-word units (~650 tokens each)
        goal = "Write 50 sections. Each section must be 500 words and start with ## Section N:."
        cap = units_per_leaf_capacity(goal, episode_budget_seconds=1800)
        # Pace clamp limits units per leaf so each leaf completes in time
        self.assertLessEqual(cap, 15)
        self.assertGreaterEqual(cap, 5)

    @patch.dict(os.environ, {"KUSUDAEMON_UNIT_PACING": "1"})
    def test_b3_budget_seconds_calibrated(self) -> None:
        node = TaskNode(
            id="u1",
            brief="Write 25 units",
            artifact="out/u1.md",
            gates=["units_min:25@Floor"],
            budget=NodeBudget(tokens=4000, calls=10, units_expected=25),
        )
        sec = _budget_seconds(node)
        # 25 units * 50s * 1.3 = 1625s
        self.assertGreaterEqual(sec, 1250)
        self.assertLessEqual(sec, 1800)

    @patch.dict(os.environ, {"KUSUDAEMON_CLASSIFY_FAST_PATH": "1"})
    def test_c1_deterministic_classify_fast_path(self) -> None:
        goal = "Write 20 chapters. Use '=== Chapter N: ===' to separate each chapter."
        options = RunOptions(goal=goal, backend="gptme")
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            driver = RecursiveDriver(run_dir=run_dir, provider=MagicMock(), options=options)
            with patch("kusudaemon.pipeline.driver.estimate_scope_full") as mock_estimate:
                asyncio.run(driver._phase_classify())
                mock_estimate.assert_not_called()
            events = driver.log.read_all()
            self.assertTrue(any(e.get("type") == "scope_estimate_skipped" for e in events))

    def test_c2_orchestrator_caps(self) -> None:
        # 2026-09-26: C2's 1024 was spent on reasoning before any JSON, so the
        # cap is 16384. The deadline is a fixed 90 s for the whole decision
        # (the cap-scaled ~1092 s let one decision take 611 s).
        with patch.dict(os.environ, {}, clear=False):
            for k in ("KUSUDAEMON_ORCHESTRATOR_MAX_TOKENS", "KUSUDAEMON_ORCHESTRATOR_DEADLINE_S", "KUSUDAEMON_MIN_DECODE_TPS"):
                os.environ.pop(k, None)
            self.assertEqual(orchestrator_max_tokens(), 16384)
            self.assertEqual(orchestrator_deadline_s(), 90.0)
            os.environ["KUSUDAEMON_ORCHESTRATOR_DEADLINE_S"] = "60"
            self.assertEqual(orchestrator_deadline_s(), 60.0)

    def test_c3_reviewer_deadline_configured(self) -> None:
        provider = OpenAICompatibleProvider(api_key="fake", base_url="http://fake")
        with call_scope(role="reviewer"):
            # Provider should resolve reviewer deadline to 180s
            deadline = float(os.getenv("KUSUDAEMON_REVIEWER_DEADLINE_S", "180.0"))
            self.assertEqual(deadline, 180.0)

    def test_d1_bench_cmd_passes_wall_clock_budget(self) -> None:
        from run_longgen_bench import build_bench_cmd
        cmd = build_bench_cmd(
            arm="C",
            prompt_file=Path("/tmp/p.txt"),
            workspace=Path("/tmp/ws"),
            artifact_path=Path("/tmp/out.md"),
            record_path=Path("/tmp/rec.json"),
            task_id="t1",
            seed=1,
            backend="gptme",
            model="test-model",
            tier="T2",
            work_object="text",
            budget_tokens=50000,
            max_rounds=20,
            runs_root="/tmp/runs",
            wall_clock_budget=1800,
        )
        self.assertIn("--wall-clock-budget", cmd)
        self.assertIn("1800", cmd)

    def test_d2_partial_record_on_missing(self) -> None:
        from run_longgen_bench import run_one
        import argparse
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs_dir = root / "runs"
            results_dir = root / "results"
            stem = "001-building_armC_seed1"
            run_id = f"longgen_{stem}"
            cell_dir = runs_dir / run_id
            cell_dir.mkdir(parents=True)
            # Create a tree.json with passed nodes
            tree = TaskTree(nodes={
                "unit-01": TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["file_nonempty"], status="passed"),
                "unit-02": TaskNode(id="unit-02", brief="b", artifact="out/unit-02.md", gates=["file_nonempty"], status="passed"),
                "unit-03": TaskNode(id="unit-03", brief="b", artifact="out/unit-03.md", gates=["file_nonempty"], status="dispatched"),
            })
            tree.save(cell_dir / "tree.json")

            args = argparse.Namespace(
                backend="gptme",
                model="test",
                tier="T2",
                work_object="text",
                budget_tokens=50000,
                max_rounds=20,
                runs_root=str(runs_dir),
                workspaces_root=str(root / "ws"),
                timeout_sec=2100,
                dry_run=False,
                score_only=False,
                verbose=False,
                flags="",
                continue_runs=True,
            )
            item = {"prompt": "Write 10 floors", "number": 10, "type": "building"}

            with patch("subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_proc.communicate.return_value = ("", "")
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc

                res = run_one(
                    index=1,
                    item=item,
                    arm="C",
                    seed=1,
                    args=args,
                    results_dir=results_dir,
                )
                rec_path = results_dir / "bench" / f"{stem}.json"
                self.assertTrue(rec_path.is_file())
                rec_data = json.loads(rec_path.read_text(encoding="utf-8"))
                self.assertTrue(rec_data.get("partial"))
                self.assertIn("unit-01", rec_data.get("passed_nodes", []))
                self.assertIn("unit-02", rec_data.get("passed_nodes", []))

    def test_e1_classify_cli_failure_500_and_headers(self) -> None:
        self.assertEqual(classify_cli_failure("Internal Server Error: 500"), "transport")
        self.assertEqual(classify_cli_failure("Error 502 Bad Gateway"), "transport")
        self.assertEqual(classify_cli_failure("Status 503 Service Unavailable"), "transport")
        self.assertEqual(classify_cli_failure("Failed to get response headers from provider"), "transport")
        self.assertEqual(classify_cli_failure("Gateway Timeout 504"), "transport")
        self.assertEqual(classify_cli_failure("Random syntax error"), "error")


if __name__ == "__main__":
    unittest.main()
