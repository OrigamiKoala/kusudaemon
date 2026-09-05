"""Unit tests for the `kusudaemon bench` CLI entry point (TESTING.md §2).

Hermetic tests: no network, no live model calls, no external agent binary.
Verifies:
  1. CLI argument parsing across all flags.
  2. Missing argument validation (goal, workspace).
  3. Arm A (bare CLI) invocation and JSON output.
  4. Arm B (decomposition only: disable review) wiring.
  5. Arm C (full pipeline) wiring.
  6. Token budget ceiling and halt reason tracking.
  7. Output file and stdout formatting.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline.cli import build_pipeline_parser, cmd_bench, dispatch
from kusudaemon.pipeline.driver import RunReport


class DummyCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class DummyDriver:
    def __init__(self, run_dir: Path, *, status: str = "done", detail: str = "", options=None) -> None:
        self.run_dir = run_dir
        self.status = status
        self.detail = detail
        self.options = options

    async def run(self) -> RunReport:
        return RunReport(
            status=self.status,
            phase="assemble",
            detail=self.detail,
            tree_counts={"done": 1},
        )


class TestBenchCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="kusudaemon_bench_test_")
        self.ws_dir = (Path(self.tmp_dir) / "workspace").resolve()
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.ws_dir / "main.py").write_text("print('hello')\n", encoding="utf-8")
        self.runs_root = (Path(self.tmp_dir) / "runs").resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_bench_parser_options(self) -> None:
        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Fix bug in main.py",
            "--backend", "opencode",
            "--model", "opencode/nemotron-3.5-lightning-free",
            "--arm", "B",
            "--tier", "auto",
            "--budget-tokens", "50000",
            "--benchmark", "harness-bench",
            "--task-id", "task_01",
            "--seed", "2",
            "--json",
        ])
        self.assertEqual(args.pipeline_command, "bench")
        self.assertEqual(args.workspace, str(self.ws_dir))
        self.assertEqual(args.goal, "Fix bug in main.py")
        self.assertEqual(args.backend, "opencode")
        self.assertEqual(args.model, "opencode/nemotron-3.5-lightning-free")
        self.assertEqual(args.arm, "B")
        self.assertEqual(args.tier, "auto")
        self.assertEqual(args.budget_tokens, 50000)
        self.assertEqual(args.benchmark, "harness-bench")
        self.assertEqual(args.task_id, "task_01")
        self.assertEqual(args.seed, 2)
        self.assertTrue(args.json)

    def test_missing_goal_or_workspace_fails(self) -> None:
        parser = build_pipeline_parser()
        args = parser.parse_args(["bench", "--workspace", str(self.ws_dir)])
        buf = io.StringIO()
        with unittest.mock.patch("sys.stderr", buf):
            code = cmd_bench(args)
        self.assertEqual(code, 2)
        self.assertIn("either --goal-file or --goal is required", buf.getvalue())

        args_bad_ws = parser.parse_args(["bench", "--workspace", "/nonexistent/ws", "--goal", "do it"])
        buf2 = io.StringIO()
        with unittest.mock.patch("sys.stderr", buf2):
            code2 = cmd_bench(args_bad_ws)
        self.assertEqual(code2, 2)
        self.assertIn("workspace directory not found", buf2.getvalue())

    def test_goal_file_resolution(self) -> None:
        goal_file = Path(self.tmp_dir) / "instruction.txt"
        goal_file.write_text("Instruction from file\n", encoding="utf-8")

        captured_options = []

        def fake_driver_factory(run_dir, provider, options, env):
            captured_options.append(options)
            return DummyDriver(run_dir, status="done", options=options)

        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal-file", str(goal_file),
            "--runs-root", str(self.runs_root),
        ])
        code = cmd_bench(args, driver_factory=fake_driver_factory)
        self.assertEqual(code, 0)
        self.assertEqual(len(captured_options), 1)
        self.assertEqual(captured_options[0].goal, "Instruction from file")

    def test_arm_a_bare_execution(self) -> None:
        executed_cmds = []

        def fake_runner(cmd, cwd, capture_output, text):
            executed_cmds.append((cmd, cwd))
            return DummyCompletedProcess(returncode=0, stdout="All tests passed")

        parser = build_pipeline_parser()
        output_file = Path(self.tmp_dir) / "result_arm_a.json"
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Solve task bare",
            "--backend", "opencode",
            "--model", "opencode/nemotron-3.5-lightning-free",
            "--arm", "A",
            "--output", str(output_file),
            "--json",
        ])

        stdout_buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout_buf):
            code = cmd_bench(args, subprocess_runner=fake_runner)

        self.assertEqual(code, 0)
        self.assertEqual(len(executed_cmds), 1)
        cmd, cwd = executed_cmds[0]
        self.assertEqual(cmd, [
            "opencode", "run",
            "--model", "opencode/nemotron-3.5-lightning-free",
            "--auto", "Solve task bare"
        ])
        self.assertEqual(cwd, str(self.ws_dir))

        self.assertTrue(output_file.is_file())
        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertEqual(data["arm"], "A")
        self.assertEqual(data["backend"], "opencode")
        self.assertEqual(data["model"], "opencode/nemotron-3.5-lightning-free")
        self.assertTrue(data["resolved"])
        self.assertEqual(data["score"], 1.0)
        self.assertIsNone(data["halt_reason"])

    def test_arm_b_decomposition_only(self) -> None:
        captured_options = []

        def fake_driver_factory(run_dir, provider, options, env):
            captured_options.append(options)
            return DummyDriver(run_dir, status="done", options=options)

        parser = build_pipeline_parser()
        output_file = Path(self.tmp_dir) / "result_arm_b.json"
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Decompose and execute",
            "--arm", "B",
            "--backend", "opencode",
            "--runs-root", str(self.runs_root),
            "--output", str(output_file),
        ])

        code = cmd_bench(args, driver_factory=fake_driver_factory)
        self.assertEqual(code, 0)
        self.assertEqual(len(captured_options), 1)
        # Arm B must disable review agents
        self.assertTrue(captured_options[0].disable_review)

        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertEqual(data["arm"], "B")
        self.assertTrue(data["resolved"])

    def test_arm_c_full_pipeline(self) -> None:
        captured_options = []

        def fake_driver_factory(run_dir, provider, options, env):
            captured_options.append(options)
            return DummyDriver(run_dir, status="done", options=options)

        parser = build_pipeline_parser()
        output_file = Path(self.tmp_dir) / "result_arm_c.json"
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Full pipeline with review",
            "--arm", "C",
            "--backend", "opencode",
            "--runs-root", str(self.runs_root),
            "--output", str(output_file),
        ])

        code = cmd_bench(args, driver_factory=fake_driver_factory)
        self.assertEqual(code, 0)
        self.assertEqual(len(captured_options), 1)
        # Arm C must NOT disable review agents
        self.assertFalse(captured_options[0].disable_review)

        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertEqual(data["arm"], "C")
        self.assertTrue(data["resolved"])

    def test_budget_tokens_halt(self) -> None:
        def fake_driver_factory(run_dir, provider, options, env):
            # Simulate token ceiling halt by writing halt.flag
            from kusudaemon.pipeline.run_dir import halt_path
            halt_path(run_dir).write_text("token ceiling", encoding="utf-8")
            return DummyDriver(run_dir, status="halted", detail="token ceiling", options=options)

        parser = build_pipeline_parser()
        output_file = Path(self.tmp_dir) / "result_halt.json"
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Exceed budget",
            "--budget-tokens", "1000",
            "--runs-root", str(self.runs_root),
            "--output", str(output_file),
            "--json",
        ])

        stdout_buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout_buf):
            code = cmd_bench(args, driver_factory=fake_driver_factory)

        # Must exit non-zero on halt per TESTING.md §2
        self.assertEqual(code, 1)
        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertFalse(data["resolved"])
        self.assertEqual(data["score"], 0.0)
        self.assertEqual(data["halt_reason"], "token ceiling")


    def test_arm_c_error_status_records_diagnosable_halt_reason(self) -> None:
        """A driver error must not surface as resolved=false with no reason.

        Regression: cmd_bench previously mapped only status == "halted" onto
        halt_reason, so a provider outage or auth failure (status == "error")
        produced a record with halt_reason: null -- indistinguishable from a
        clean but unresolved run when reading a sweep afterwards.
        """
        def fake_driver_factory(run_dir, provider, options, env):
            return DummyDriver(
                run_dir,
                status="error",
                detail="provider request failed: [Errno 111] Connection refused",
                options=options,
            )

        output_file = Path(self.tmp_dir) / "arm_c_error.json"
        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Solve task",
            "--arm", "C",
            "--backend", "gptme",
            "--runs-root", str(self.runs_root),
            "--output", str(output_file),
            "--json",
        ])

        stdout_buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout_buf):
            code = cmd_bench(args, driver_factory=fake_driver_factory)

        self.assertEqual(code, 1)
        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertFalse(data["resolved"])
        self.assertIsNotNone(data["halt_reason"])
        self.assertIn("error", data["halt_reason"])
        self.assertIn("assemble", data["halt_reason"])
        self.assertIn("Connection refused", data["halt_reason"])


if __name__ == "__main__":
    unittest.main()
