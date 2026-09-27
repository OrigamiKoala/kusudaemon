"""2026-09-27 repairs: detached agent CLIs die with their episode; the wave cap
starts open and a serial phase no longer pins it at 1.

346-block s2 unit-03 "timed out after 199s" while its opencode process, in a
session of its own under ``_agent_worker.py``, kept writing for 50 minutes.
294-menu-week s1 escalated T1 -> T2 with the process-global wave cap at 1, and
its three leaves ran one at a time.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.pipeline.admission import RunAdmissionController  # noqa: E402
from kusudaemon.utils.process_group import (  # noqa: E402
    descendant_process_groups,
    kill_process_group,
)

_WORKER = _REPO_ROOT / "src" / "kusudaemon" / "adapters" / "_agent_worker.py"


def _detached_cli_script(pid_file: Path) -> str:
    """A stand-in agent CLI: starts a grandchild in its own session, then sleeps."""
    return textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c",
            "import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"],
            start_new_session=True)
        time.sleep(60)
        """
    )


def _wait_for_pid(pid_file: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.05)
    raise AssertionError("grandchild never started")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers signal 0; ps says whether it is really running.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _cleanup(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class DetachedDescendantTest(unittest.TestCase):
    def test_kill_process_group_reaps_detached_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "gc.pid"
            child = subprocess.Popen(
                [sys.executable, "-c", _detached_cli_script(pid_file)], start_new_session=True
            )
            gc = _wait_for_pid(pid_file)
            try:
                self.assertIn(gc, descendant_process_groups(child.pid))
                kill_process_group(child.pid)
                child.wait(timeout=5)
                self.assertTrue(_wait_dead(gc), "detached grandchild outlived kill_process_group")
            finally:
                _cleanup(gc)

    def test_exec_timeout_reaps_detached_grandchild(self) -> None:
        # The live path: LocalEnvironment.exec times the episode out.
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "gc.pid"
            script = Path(td) / "cli.py"
            script.write_text(_detached_cli_script(pid_file))
            env = LocalEnvironment(tmp_dir=td)
            res = asyncio.run(
                env.exec(f"{sys.executable} {script}", timeout=2, grace_period=0, activity_window=1.0)
            )
            self.assertEqual(res.termination_reason, "timeout")
            gc = _wait_for_pid(pid_file, timeout=1)
            try:
                self.assertTrue(_wait_dead(gc), "detached grandchild outlived the episode timeout")
            finally:
                _cleanup(gc)

    def test_agent_worker_forwards_sigterm_to_its_cli(self) -> None:
        # The worker starts the CLI in its own session; SIGTERM to the worker
        # alone (the harness's view of the episode) must still stop the CLI.
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "cli.pid"
            cli = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
            worker = subprocess.Popen(
                [sys.executable, str(_WORKER), "--format", "claude", "--", sys.executable, "-c", cli],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            cli_pid = _wait_for_pid(pid_file)
            try:
                self.assertNotEqual(os.getpgid(cli_pid), os.getpgid(worker.pid))
                worker.send_signal(signal.SIGTERM)
                self.assertTrue(_wait_dead(cli_pid), "CLI outlived its worker's SIGTERM")
                worker.wait(timeout=5)
            finally:
                _cleanup(cli_pid)
                _cleanup(worker.pid)


class WaveCapTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("KUSUDAEMON_WAVE_SLOW_START", None)

    def tearDown(self) -> None:
        os.environ.pop("KUSUDAEMON_WAVE_SLOW_START", None)

    def test_serial_phase_success_does_not_lower_the_cap(self) -> None:
        ctrl = RunAdmissionController(concurrency=4)
        ctrl.seed_wave_cap(3)
        self.assertEqual(ctrl.record_wave_outcome(0, max_parallel=1), 3)

    def test_seed_opens_cap_to_max_parallel(self) -> None:
        ctrl = RunAdmissionController(concurrency=4, initial_wave_cap=1)
        self.assertEqual(ctrl.seed_wave_cap(4), 4)

    def test_seed_keeps_a_cap_learned_from_throttling(self) -> None:
        ctrl = RunAdmissionController(concurrency=4, initial_wave_cap=4)
        ctrl.record_wave_outcome(1, max_parallel=4)
        self.assertEqual(ctrl.seed_wave_cap(4), 2)

    def test_slow_start_flag_keeps_old_behaviour(self) -> None:
        os.environ["KUSUDAEMON_WAVE_SLOW_START"] = "1"
        ctrl = RunAdmissionController(concurrency=4)
        self.assertEqual(ctrl.seed_wave_cap(4), 2)

    def test_round_loop_fills_every_slot_from_a_cap_of_one(self) -> None:
        # 294-menu-week s1's state after T1 -> T2: cap 1, three fresh leaves.
        from test_refill_units_workspace import OK, _run

        with tempfile.TemporaryDirectory() as td:
            _, tree, tl = _run(
                Path(td),
                {"a": [(0.6, OK)], "b": [(0.6, OK)], "c": [(0.6, OK)]},
                max_parallel=3,
                wave_cap=1,
            )
            self.assertTrue(all(tree.nodes[k].status == "passed" for k in "abc"))
            first_end = min(tl.ends[k][0] for k in "abc")
            self.assertTrue(all(tl.starts[k][0] < first_end for k in "abc"))


if __name__ == "__main__":
    unittest.main()
