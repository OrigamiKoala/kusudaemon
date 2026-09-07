from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.local import (  # noqa: E402
    LocalEnvironment,
    _get_pgid_cputime,
    _has_established_socket,
)


class LocalEnvironmentTest(unittest.TestCase):
    def test_exec_normal_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = LocalEnvironment(tmp_dir=tmp)
            res = asyncio.run(env.exec("echo hello-world", timeout=10))
            self.assertEqual(res.exit_code, 0)
            self.assertEqual(res.stdout.strip(), "hello-world")
            self.assertIsNone(res.termination_reason)

    def test_exec_silent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = LocalEnvironment(tmp_dir=tmp)
            t0 = time.monotonic()
            # Sleep 10s with timeout=1s and 0s grace period
            res = asyncio.run(env.exec("sleep 10", timeout=1, grace_period=0, activity_window=1.0))
            elapsed = time.monotonic() - t0
            self.assertEqual(res.termination_reason, "timeout")
            self.assertLess(elapsed, 4.0)

    def test_exec_stream_activity_grants_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = LocalEnvironment(tmp_dir=tmp)
            t0 = time.monotonic()
            # Command emits output at 0.5s, 1.2s, 1.8s and finishes at 2.2s.
            # With timeout=1s and grace_period=5s, active non-heartbeat output extends past 1s.
            cmd = "python3 -c 'import time, sys; time.sleep(0.4); print(\"line1\", flush=True); time.sleep(0.7); print(\"line2\", flush=True); time.sleep(0.6); print(\"done\", flush=True)'"
            res = asyncio.run(env.exec(cmd, timeout=1, grace_period=5, activity_window=2.0))
            elapsed = time.monotonic() - t0
            self.assertEqual(res.exit_code, 0)
            self.assertIn("done", res.stdout)
            self.assertIsNone(res.termination_reason)
            self.assertGreater(elapsed, 1.4)

    def test_liveness_helpers(self) -> None:
        pid = os.getpid()
        cpu = _get_pgid_cputime(pid)
        self.assertGreaterEqual(cpu, 0.0)
        has_sock = _has_established_socket(pid)
        self.assertIsInstance(has_sock, bool)


if __name__ == "__main__":
    unittest.main()
