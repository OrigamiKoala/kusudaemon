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
            stdout_data = json.dumps({"usage": {"input_tokens": 120, "output_tokens": 45}}) + "\n"
            return DummyCompletedProcess(returncode=0, stdout=stdout_data)

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
            "--print-logs",
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
        self.assertEqual(data["tokens_by_role"], {"bare": 165})

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


    def _run_arm_a(self, extra_argv: list[str]) -> list[str]:
        """Run arm A with a fake runner and return the command it built."""
        executed: list[list[str]] = []

        def fake_runner(cmd, cwd, capture_output, text):
            executed.append(cmd)
            return DummyCompletedProcess(returncode=0)

        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Round goal",
            "--arm", "A",
            "--output", str(Path(self.tmp_dir) / "arm_a_round.json"),
            "--json",
        ] + extra_argv)
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            cmd_bench(args, subprocess_runner=fake_runner)
        return executed[0]

    def test_arm_a_first_round_starts_a_fresh_session(self) -> None:
        cmd = self._run_arm_a(["--backend", "opencode", "--model", "m", "--round", "1"])
        self.assertNotIn("--continue", cmd)
        self.assertNotIn("--session", cmd)

    def test_arm_a_later_round_continues_the_session(self) -> None:
        """Multi-round benchmark tasks require the bare CLI to carry its own
        conversation across rounds -- HarnessBench 007-session-memory grades
        exactly that. Without this the arm can only pass by being handed the
        answer in its prompt."""
        cmd = self._run_arm_a([
            "--backend", "opencode", "--model", "m",
            "--session-id", "sess-abc", "--round", "2",
        ])
        self.assertIn("--continue", cmd)

    def test_arm_a_explicit_session_id_flag_is_opt_in(self) -> None:
        with unittest.mock.patch.dict(
            "os.environ", {"KUSUDAEMON_BENCH_SESSION_FLAG": "session"}
        ):
            cmd = self._run_arm_a([
                "--backend", "opencode", "--model", "m",
                "--session-id", "sess-abc", "--round", "2",
            ])
        self.assertIn("--session", cmd)
        self.assertIn("sess-abc", cmd)
        self.assertNotIn("--continue", cmd)

    def test_arm_a_record_carries_session_and_round(self) -> None:
        executed: list[list[str]] = []

        def fake_runner(cmd, cwd, capture_output, text):
            executed.append(cmd)
            return DummyCompletedProcess(returncode=0)

        output_file = Path(self.tmp_dir) / "arm_a_meta.json"
        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "g",
            "--arm", "A", "--backend", "opencode", "--model", "m",
            "--session-id", "sess-xyz", "--round", "3",
            "--output", str(output_file), "--json",
        ])
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            cmd_bench(args, subprocess_runner=fake_runner)
        data = json.loads(output_file.read_text(encoding="utf-8"))
        self.assertEqual(data["session_id"], "sess-xyz")
        self.assertEqual(data["round"], 3)


    def _approval_driver_factory(self, timeout: float):
        """A driver that posts a real approval and waits for it, exactly as the
        intake/pilot/document-review phases do."""
        from kusudaemon.pipeline import approvals as approval_store

        class ApprovalDriver:
            def __init__(self, run_dir, provider=None, options=None, env=None):
                self.run_dir = run_dir

            async def run(self):
                record = approval_store.Approval.create(
                    "intake_questions",
                    title="Intake round 1",
                    message="questions",
                    input_label="",
                    context={"round": 1},
                    questions=[{"id": "q1", "text": "scope?", "default_assumption": "all"}],
                )
                approval_store.append(self.run_dir, record)
                approval_store.wait_for_resolution(
                    self.run_dir, record.approval_id, poll_interval=0.02, timeout=timeout
                )
                return RunReport(status="done", phase="assemble", detail="",
                                 tree_counts={"done": 1})

        return lambda run_dir, provider=None, options=None, env=None: ApprovalDriver(run_dir)

    def _bench_with_approval_driver(self, timeout: float, extra: list[str]) -> dict:
        output_file = Path(self.tmp_dir) / f"approval_{'-'.join(extra) or 'default'}.json"
        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench",
            "--workspace", str(self.ws_dir),
            "--goal", "Solve task",
            "--arm", "C", "--backend", "gptme",
            "--runs-root", str(self.runs_root),
            "--output", str(output_file), "--json",
        ] + extra)
        buf = io.StringIO()
        with unittest.mock.patch("sys.stdout", buf):
            cmd_bench(args, driver_factory=self._approval_driver_factory(timeout))
        return json.loads(output_file.read_text(encoding="utf-8"))

    def test_unattended_bench_resolves_approvals_instead_of_hanging(self) -> None:
        """A benchmark run has no operator, and the driver's approval wait has
        no timeout by design. Three phases block on one (intake T1+, pilot T3,
        document-review triage T2+), so without a resolver the run hangs until
        the benchmark's own wall-clock cap kills it -- no graded result and no
        halt reason. Every hermetic driver test already supplies this Approver;
        the headless bench path must too."""
        data = self._bench_with_approval_driver(10.0, [])
        self.assertTrue(data["resolved"])
        self.assertIsNone(data["halt_reason"])
        self.assertFalse(data["attended"])

    def test_attended_bench_leaves_approvals_for_a_human(self) -> None:
        """Counterpart proving the test above is not vacuous: with --attended
        nobody answers, and the wait expires."""
        data = self._bench_with_approval_driver(0.5, ["--attended"])
        self.assertFalse(data["resolved"])
        self.assertIsNotNone(data["halt_reason"])
        self.assertTrue(data["attended"])


if __name__ == "__main__":
    unittest.main()
