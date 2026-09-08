"""Tests for PLAN-CONCURRENCY-AND-SHARED-STATE.md implementation:
- §A4 Arm A token parsing (JSON events + text fallback).
- §A5 Flag recording in bench records.
- §B1 & §B8 Max parallel auto-derivation & hardware memory admission.
- §B7 Rate-limit control: node_throttled non-attempt, RunAdmissionController, AIMD wave cap, wave jitter.
- §B4 Worktrees and environment lock.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.base import ExecResult
from kusudaemon.environment.local import LocalEnvironment, is_env_mutating_command
from kusudaemon.pipeline.admission import RunAdmissionController
from kusudaemon.pipeline.cli import _parse_opencode_usage
from kusudaemon.types import EpisodeBudget, EpisodeResult
from kusudaemon.utils.system_resources import (
    can_admit_episode,
    derive_memory_concurrency,
    get_available_memory_bytes,
)
from kusudaemon.v0.events import EventLog
from kusudaemon.v1.gates import GateResult
from kusudaemon.v1.round_loop import _transition_after_writer
from kusudaemon.v1.tree import TaskNode, TaskTree
from kusudaemon.v1.worktree import WorktreeManager


class TestArmATokenParsing(unittest.TestCase):
    def test_parse_opencode_usage_json_events(self) -> None:
        raw_json_stream = (
            '{"type": "step_start"}\n'
            '{"type": "step-finish", "tokens": {"input": 120, "output": 45, "reasoning": 10}}\n'
            '{"type": "step-finish", "tokens": {"input": 200, "output": 80, "reasoning": 5}}\n'
        )
        usage = _parse_opencode_usage(raw_json_stream)
        self.assertEqual(usage["prompt"], 320)
        self.assertEqual(usage["completion"], 140)
        self.assertEqual(usage["total"], 460)

    def test_parse_opencode_usage_nested_part(self) -> None:
        raw_stream = (
            '{"type": "step_finish", "part": {"tokens": {"input": 50, "output": 25}}}\n'
        )
        usage = _parse_opencode_usage(raw_stream)
        self.assertEqual(usage["prompt"], 50)
        self.assertEqual(usage["completion"], 25)
        self.assertEqual(usage["total"], 75)

    def test_parse_opencode_usage_text_fallback(self) -> None:
        text_stream = (
            "Model finished. Tokens: 400 input, 150 output\n"
            "Total tokens: 550\n"
        )
        usage = _parse_opencode_usage(text_stream)
        self.assertEqual(usage["prompt"], 400)
        self.assertEqual(usage["completion"], 150)
        self.assertEqual(usage["total"], 550)


class TestAdmissionControllerAndRateLimiting(unittest.TestCase):
    def test_admission_controller_backoff(self) -> None:
        ctrl = RunAdmissionController(concurrency=2)
        self.assertEqual(ctrl.remaining_closed_seconds(), 0.0)
        ctrl.record_backoff(2.5)
        self.assertGreater(ctrl.remaining_closed_seconds(), 1.0)
        self.assertLessEqual(ctrl.remaining_closed_seconds(), 2.5)

    def test_admission_controller_aimd(self) -> None:
        ctrl = RunAdmissionController(concurrency=8, initial_wave_cap=2)
        self.assertEqual(ctrl.current_wave_cap, 2)
        # Wave with 0 throttles -> additive increase (+1)
        cap = ctrl.record_wave_outcome(throttled_count=0, max_parallel=8)
        self.assertEqual(cap, 3)
        cap = ctrl.record_wave_outcome(throttled_count=0, max_parallel=8)
        self.assertEqual(cap, 4)
        # Wave with throttles -> multiplicative decrease (halve)
        cap = ctrl.record_wave_outcome(throttled_count=1, max_parallel=8)
        self.assertEqual(cap, 2)
        cap = ctrl.record_wave_outcome(throttled_count=2, max_parallel=8)
        self.assertEqual(cap, 1)

    def test_transition_after_writer_throttled_is_non_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            tree_path = run_dir / "tree.json"
            log = EventLog(run_dir / "events.jsonl")
            node = TaskNode(id="node_a", brief="brief", artifact="out/node_a.md", gates=["non_empty"], status="dispatched", attempts=0)
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
                    result_status="throttled",
                )
            )

            # Attempts should NOT be incremented
            self.assertEqual(node.attempts, 0)
            # Status should return to pending
            self.assertEqual(node.status, "pending")
            self.assertIn("throttled", node.last_defect)

            # Check that node_throttled event was recorded
            events = log.read_all()
            throttled_events = [e for e in events if e.get("type") == "node_throttled"]
            self.assertEqual(len(throttled_events), 1)
            self.assertEqual(throttled_events[0]["node_id"], "node_a")


class TestHardwareAdmissionAndDerivation(unittest.TestCase):
    def test_get_available_memory_bytes(self) -> None:
        mem = get_available_memory_bytes()
        if mem is not None:
            self.assertIsInstance(mem, int)
            self.assertGreater(mem, 0)

    def test_derive_memory_concurrency_clamps(self) -> None:
        # Mock 4GB available with 600MB RSS and 0.75 safety -> 4096 * 0.75 / 600 = ~5
        with patch("kusudaemon.utils.system_resources.get_available_memory_bytes", return_value=4 * 1024 * 1024 * 1024):
            conc = derive_memory_concurrency(safety=0.75, default_episode_rss_mb=600, hard_cap=16)
            self.assertEqual(conc, 5)

        # Mock low memory (300MB) -> clamped to floor 1
        with patch("kusudaemon.utils.system_resources.get_available_memory_bytes", return_value=300 * 1024 * 1024):
            conc = derive_memory_concurrency(safety=0.75, default_episode_rss_mb=600, hard_cap=16)
            self.assertEqual(conc, 1)

    def test_can_admit_episode(self) -> None:
        with patch("kusudaemon.utils.system_resources.get_available_memory_bytes", return_value=1024 * 1024 * 1024):
            self.assertTrue(can_admit_episode(floor_bytes=600 * 1024 * 1024))
        with patch("kusudaemon.utils.system_resources.get_available_memory_bytes", return_value=200 * 1024 * 1024):
            self.assertFalse(can_admit_episode(floor_bytes=600 * 1024 * 1024))


class TestEnvironmentLockAndWorktrees(unittest.TestCase):
    def test_is_env_mutating_command(self) -> None:
        self.assertTrue(is_env_mutating_command("pip install requests"))
        self.assertTrue(is_env_mutating_command("npm install -g something"))
        self.assertTrue(is_env_mutating_command("apt-get update"))
        self.assertTrue(is_env_mutating_command("docker run -d redis"))
        self.assertFalse(is_env_mutating_command("git status"))
        self.assertFalse(is_env_mutating_command("pytest tests/"))
        self.assertFalse(is_env_mutating_command("cat README.md"))

    def test_local_environment_lock_acquired_on_mutating_cmd(self) -> None:
        env_lock = asyncio.Lock()
        env = LocalEnvironment(env_lock=env_lock)

        async def run_test() -> None:
            started = asyncio.Event()
            finish = asyncio.Event()

            async def fake_exec(*args: Any, **kwargs: Any) -> ExecResult:
                started.set()
                await finish.wait()
                return ExecResult(stdout="", stderr="", exit_code=0, duration_ms=10)

            with patch.object(env, "_exec_impl", side_effect=fake_exec):
                task = asyncio.create_task(env.exec("pip install fake-pkg"))
                await started.wait()
                self.assertTrue(env_lock.locked())
                finish.set()
                await task

        asyncio.run(run_test())

    def test_worktree_manager_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ws_dir = Path(td) / "ws"
            ws_dir.mkdir()
            (ws_dir / "base.txt").write_text("initial content\n")

            mgr = WorktreeManager(workspace_path=ws_dir, runs_dir=Path(td) / "runs")
            wt = mgr.create_worktree("node_1")
            self.assertTrue(wt.exists())

            # Modify a file in worktree
            (wt / "base.txt").write_text("modified content\n")
            (wt / "added.txt").write_text("new file\n")

            patch, modified = mgr.compute_patch("node_1")
            self.assertIn("base.txt", modified)
            self.assertIn("added.txt", modified)

            ok, err = mgr.apply_patch_to_trunk("node_1", patch, modified)
            self.assertTrue(ok)
            self.assertEqual((ws_dir / "base.txt").read_text(), "modified content\n")
            self.assertEqual((ws_dir / "added.txt").read_text(), "new file\n")

            mgr.remove_worktree("node_1")
            self.assertFalse(wt.exists())


if __name__ == "__main__":
    unittest.main()
