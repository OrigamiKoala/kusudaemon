"""Tests for throttle and transport failure classification (PLAN-REVIEW-READ-LOOP.md §O1, §O8)."""

import tempfile
import unittest
from pathlib import Path

from kusudaemon.adapters.cli_agent import classify_cli_failure
from kusudaemon.eval.common import classify_halt
from kusudaemon.pipeline.cli import _bench_resolved


class TestFailureClassification(unittest.TestCase):
    def test_classify_cli_transport_socket_closed(self) -> None:
        out = "AI_APICallError: Cannot connect to API: The socket connection was closed unexpectedly"
        self.assertEqual(classify_cli_failure(out), "transport")

    def test_classify_cli_transport_connection_refused(self) -> None:
        out = "urllib.error.URLError: <urlopen error [Errno 61] Connection refused>"
        self.assertEqual(classify_cli_failure(out), "transport")

    def test_classify_cli_throttled_429(self) -> None:
        out = "error.error=429 rate limit exceeded; please wait"
        self.assertEqual(classify_cli_failure(out), "throttled")

    def test_classify_cli_throttled_status_code(self) -> None:
        out = "HTTP 429 Too Many Requests: Rate limit reached"
        self.assertEqual(classify_cli_failure(out), "throttled")

    def test_classify_cli_bare_429_not_throttled(self) -> None:
        out = "Processed 429 items successfully, but assertion failed on line 10"
        self.assertEqual(classify_cli_failure(out), "error")

    def test_classify_halt_timeout_after_budget(self) -> None:
        self.assertEqual(classify_halt("timeout after 5400s"), "budget")
        self.assertEqual(classify_halt("timeout after 1800s"), "budget")

    def test_classify_halt_provider_unavailable_transport(self) -> None:
        self.assertEqual(
            classify_halt("provider unavailable: hit 6 consecutive non-attempts (transport)"),
            "transport",
        )

    def test_classify_halt_rate_limit_transport(self) -> None:
        self.assertEqual(
            classify_halt("rate limit backoff aborted by halt signal"),
            "transport",
        )


class TestBenchResolved(unittest.TestCase):
    """§O2: an exhausted round budget reports escalation, never success."""

    def _nonempty_artifact(self, tmp: str) -> str:
        p = Path(tmp) / "out.md"
        p.write_text("#*# Unit 1\ntext\n", encoding="utf-8")
        return str(p)

    def test_pending_nodes_are_not_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._nonempty_artifact(tmp)
            resolved, score, exit_code = _bench_resolved(True, 0, 1, artifact)
            self.assertFalse(resolved)
            self.assertEqual(score, 0.0)
            self.assertEqual(exit_code, 1)

    def test_empty_artifact_is_not_resolved(self) -> None:
        """The 287-menu C2 false success: score 1.0 on a 0-byte artifact."""
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.md"
            empty.write_text("", encoding="utf-8")
            resolved, score, exit_code = _bench_resolved(True, 1, 1, str(empty))
            self.assertFalse(resolved)
            self.assertEqual(score, 0.0)
            self.assertEqual(exit_code, 1)

    def test_missing_artifact_is_not_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved, score, _ = _bench_resolved(True, 1, 1, str(Path(tmp) / "nope.md"))
            self.assertFalse(resolved)
            self.assertEqual(score, 0.0)

    def test_complete_nonempty_run_stays_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._nonempty_artifact(tmp)
            resolved, score, exit_code = _bench_resolved(True, 1, 1, artifact)
            self.assertTrue(resolved)
            self.assertEqual(score, 1.0)
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
