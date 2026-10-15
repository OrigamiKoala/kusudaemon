"""Tests for the dashboard's additive routes (dashboard/extras.py + server.py):
per-request run scope, git status/diff/log, file browser and search, cost
breakdown, CLI jobs, capabilities. Real loopback server and real temporary
git repositories; no network, no agent binary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kusudaemon.dashboard import extras  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import events_path  # noqa: E402
from test_dashboard_server import _ServerTestCase, _write_scripted_run  # noqa: E402

HAVE_GIT = shutil.which("git") is not None


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


def _make_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "b.py").write_text("print('hi')\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")


class UnifiedDiffParserTest(unittest.TestCase):
    def test_parses_files_hunks_and_counts(self) -> None:
        text = (
            "diff --git a/a.txt b/a.txt\nindex 1..2 100644\n--- a/a.txt\n+++ b/a.txt\n"
            "@@ -1,3 +1,3 @@ ctx\n one\n-two\n+TWO\n three\n"
            "diff --git a/new.md b/new.md\nnew file mode 100644\n--- /dev/null\n+++ b/new.md\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        )
        files = extras.parse_unified_diff(text)
        self.assertEqual([f["path"] for f in files], ["a.txt", "new.md"])
        self.assertEqual((files[0]["added"], files[0]["deleted"]), (1, 1))
        self.assertEqual(files[1]["status"], "added")
        lines = files[0]["hunks"][0]["lines"]
        self.assertEqual([l["kind"] for l in lines], ["context", "remove", "add", "context"])
        self.assertEqual((lines[1]["old"], lines[1]["new"]), (2, None))
        self.assertEqual((lines[2]["old"], lines[2]["new"]), (None, 2))
        self.assertEqual(files[0]["hunks"][0]["context"], "ctx")


class FilesHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "n.txt").write_text("alpha\nNeedle here\n", encoding="utf-8")
        (self.tmp / "bin.dat").write_bytes(b"\x00\x01\x02" * 10)
        (self.tmp / ".git").mkdir()
        self.outside = Path(tempfile.mkdtemp())
        (self.outside / "secret.txt").write_text("nope", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_safe_join_blocks_traversal_and_symlink_escape(self) -> None:
        self.assertIsNone(extras.safe_join(self.tmp, "../x"))
        self.assertIsNone(extras.safe_join(self.tmp, "sub/../../x"))
        (self.tmp / "link").symlink_to(self.outside)
        self.assertIsNone(extras.safe_join(self.tmp, "link/secret.txt"))
        self.assertIsNotNone(extras.safe_join(self.tmp, "sub/n.txt"))

    def test_list_read_and_search(self) -> None:
        listing = extras.list_dir(self.tmp, "")
        self.assertEqual(listing["path"], "")
        self.assertEqual([e["name"] for e in listing["entries"]], ["sub", "bin.dat"])  # .git is hidden
        self.assertEqual(extras.list_dir(self.tmp, "sub")["path"], "sub")
        data = extras.read_file(self.tmp, "sub/n.txt")
        self.assertIn("Needle", data["text"])
        self.assertFalse(data["binary"])
        self.assertTrue(extras.read_file(self.tmp, "bin.dat")["binary"])
        self.assertIsNone(extras.read_file(self.tmp, "nope.txt"))
        hits = extras.search_text(self.tmp, "needle")["hits"]
        self.assertEqual([(h["path"], h["line"]) for h in hits], [("sub/n.txt", 2)])
        self.assertEqual(extras.search_text(self.tmp, "n")["hits"], [])  # too short

    def test_read_file_truncates_large_files(self) -> None:
        (self.tmp / "big.txt").write_text("x" * 1000, encoding="utf-8")
        data = extras.read_file(self.tmp, "big.txt", limit=100)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["text"]), 100)


@unittest.skipUnless(HAVE_GIT, "git not installed")
class GitHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        _make_repo(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_diff_and_log(self) -> None:
        (self.repo / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
        (self.repo / "fresh.txt").write_text("brand\nnew\n", encoding="utf-8")
        status = extras.git_status(self.repo)
        self.assertTrue(status["repo"])
        by = {f["path"]: f for f in status["files"]}
        self.assertEqual((by["a.txt"]["added"], by["a.txt"]["deleted"]), (1, 1))
        self.assertTrue(by["fresh.txt"]["untracked"])
        diff = extras.git_diff(self.repo, path="a.txt")
        self.assertEqual(diff["files"][0]["path"], "a.txt")
        self.assertEqual(diff["files"][0]["added"], 1)
        untracked = extras.git_diff(self.repo, path="fresh.txt")
        self.assertEqual(untracked["files"][0]["added"], 2)
        self.assertEqual(untracked["files"][0]["path"], "fresh.txt")
        log = extras.git_log(self.repo, 5)
        self.assertEqual([c["subject"] for c in log], ["first"])
        shown = extras.git_diff(self.repo, ref=log[0]["sha"])
        self.assertEqual({f["path"] for f in shown["files"]}, {"a.txt", "b.py"})

    def test_rejects_bad_ref_and_path(self) -> None:
        self.assertIn("error", extras.git_diff(self.repo, ref="--output=/tmp/x"))
        self.assertIn("error", extras.git_diff(self.repo, path="../etc/passwd"))

    def test_non_repo(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(extras.git_status(plain)["repo"], False)
        self.assertEqual(extras.git_log(plain), [])


class CostBreakdownTest(unittest.TestCase):
    def test_groups_and_series(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            run_dir = tmp / "r"
            run_dir.mkdir()
            rows = [
                {"ts": 3, "role": "writer", "phase": "execute", "model": "m1", "node": "n1", "prompt_tokens": 100, "completion_tokens": 10, "reasoning_tokens": 0, "cost_usd": 0.5},
                {"ts": 1, "role": "classify", "phase": "classify", "model": "m1", "node": "-", "prompt_tokens": 10, "completion_tokens": 1, "reasoning_tokens": 4, "cost_usd": 0.1, "cached": True},
            ]
            (run_dir / "cost.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            out = extras.cost_breakdown(run_dir)
            self.assertEqual(out["totals"]["calls"], 2)
            self.assertEqual(out["totals"]["cached"], 1)
            self.assertAlmostEqual(out["totals"]["cost"], 0.6)
            self.assertEqual([g["name"] for g in out["groups"]["role"]], ["writer", "classify"])
            self.assertEqual([p["ts"] for p in out["series"]], [1, 3])  # chronological
            self.assertEqual(out["series"][-1]["tokens"], 125)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CliJobArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = extras.CliJobs(Path("/tmp/runs-root"))

    def test_builds_structured_argv(self) -> None:
        self.assertEqual(self.jobs._build("status", {"brief": True}, "r1"), ["status", "r1", "--runs-root", "/tmp/runs-root", "--brief"])
        self.assertEqual(self.jobs._build("eval", {"task": "t", "runs": "2"}, ""), ["eval", "--task", "t", "--runs", "2"])
        self.assertEqual(self.jobs._build("cache-clear", {}, ""), ["cache", "clear", "--runs-root", "/tmp/runs-root"])

    def test_rejects_unknown_or_incomplete(self) -> None:
        with self.assertRaises(ValueError):
            self.jobs._build("rm", {}, "")
        with self.assertRaises(ValueError):
            self.jobs._build("resync", {}, "r1")
        with self.assertRaises(ValueError):
            self.jobs._build("bench", {}, "")
        with self.assertRaises(ValueError):
            self.jobs.start("status", {}, "../../etc")


class DashboardExtrasServerTest(_ServerTestCase):
    def test_capabilities(self) -> None:
        status, caps = self._get("/api/capabilities")
        self.assertEqual(status, 200)
        self.assertIn("opencode", caps["backends"])
        self.assertEqual(caps["tiers"], ["T0", "T1", "T2", "T3"])
        self.assertTrue(caps["control_enabled"])

    def test_run_scope_isolates_views_from_the_global_attachment(self) -> None:
        run_b = _write_scripted_run(self.runs_root, "run-b")
        (run_b / "run.spec.json").write_text(json.dumps({"goal": "second goal", "backend": "gptme"}), encoding="utf-8")
        EventLog(events_path(run_b)).append({"type": "phase_started", "phase": "classify", "node_id": "-", "ts": 1.0})
        self._post("/api/attach", {"run_id": "run-a"})
        status, snap = self._get("/api/snapshot?run=run-b")
        self.assertEqual(status, 200)
        self.assertEqual(snap["run_id"], "run-b")
        self.assertEqual(snap["goal"], "second goal")
        status, snap = self._get("/api/snapshot")
        self.assertEqual(snap["run_id"], "run-a")  # global attachment untouched
        status, events = self._get("/api/events?run=run-b")
        self.assertEqual([e["type"] for e in events["events"]], ["phase_started"])

    def test_unknown_or_unsafe_run_scope_is_404(self) -> None:
        self.assertEqual(self._get("/api/snapshot?run=nope")[0], 404)
        self.assertEqual(self._get("/api/snapshot?run=..")[0], 404)

    def test_files_read_and_search_inside_the_run_dir(self) -> None:
        status, listing = self._get("/api/files?run=run-a&path=out")
        self.assertEqual(status, 200)
        self.assertIn("1.md", [e["name"] for e in listing["entries"]])
        status, data = self._get("/api/file?run=run-a&path=out/1.md")
        self.assertIn("Hello.", data["text"])
        self.assertEqual(self._get("/api/file?run=run-a&path=../../etc/passwd")[0], 404)
        self.assertEqual(self._get("/api/files?run=run-a&path=..")[0], 404)
        status, found = self._get("/api/search?run=run-a&q=Hello")
        self.assertEqual(found["hits"][0]["path"], "out/1.md")

    def test_workspace_root_is_none_without_a_workspace(self) -> None:
        status, st = self._get("/api/git/status?run=run-a")
        self.assertEqual(status, 200)
        self.assertEqual(st["repo"], False)
        self.assertEqual(self._get("/api/files?run=run-a&root=workspace")[0], 404)

    @unittest.skipUnless(HAVE_GIT, "git not installed")
    def test_git_routes_follow_the_runs_workspace(self) -> None:
        ws = self.tmp / "ws"
        _make_repo(ws)
        (ws / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
        spec = json.loads((self.run_dir / "run.spec.json").read_text(encoding="utf-8"))
        spec["workspace_root"] = str(ws)
        (self.run_dir / "run.spec.json").write_text(json.dumps(spec), encoding="utf-8")
        status, st = self._get("/api/git/status?run=run-a")
        self.assertTrue(st["repo"])
        self.assertEqual([f["path"] for f in st["files"]], ["a.txt"])
        status, diff = self._get("/api/git/diff?run=run-a&path=a.txt")
        self.assertEqual(diff["files"][0]["added"], 1)
        status, log = self._get("/api/git/log?run=run-a&limit=3")
        self.assertEqual(log["commits"][0]["subject"], "first")
        status, files = self._get("/api/files?run=run-a&root=workspace")
        self.assertIn("a.txt", [e["name"] for e in files["entries"]])
        self.assertEqual(self._get("/api/git/diff?run=run-a&ref=--output%3D/tmp/x")[1]["files"], [])

    def test_cost_route(self) -> None:
        (self.run_dir / "cost.jsonl").write_text(
            json.dumps({"ts": 1, "role": "writer", "phase": "execute", "model": "m", "node": "1", "prompt_tokens": 5, "completion_tokens": 5, "cost_usd": 0.25}) + "\n",
            encoding="utf-8",
        )
        status, cost = self._get("/api/cost?run=run-a")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(cost["totals"]["cost"], 0.25)
        self.assertEqual(cost["groups"]["node"][0]["name"], "1")

    def test_cli_job_lifecycle(self) -> None:
        status, job = self._post("/api/cli", {"command": "cache-clear", "args": {}})
        self.assertEqual(status, 200)
        deadline = time.time() + 30
        tail = {}
        while time.time() < deadline:
            status, tail = self._get(f"/api/cli/{job['id']}")
            if tail["status"] != "running":
                break
            time.sleep(0.2)
        self.assertEqual(tail["status"], "done", tail)
        self.assertIn("cleared", tail["text"])
        status, listed = self._get("/api/cli")
        self.assertEqual([j["id"] for j in listed["jobs"]], [job["id"]])

    def test_cli_job_rejects_unlisted_commands(self) -> None:
        status, payload = self._post("/api/cli", {"command": "serve", "args": {}})
        self.assertEqual(status, 400)
        status, payload = self._post("/api/cli", {"command": "resync", "args": {}, "run_id": "run-a"})
        self.assertEqual(status, 400)


class DashboardExtrasReadOnlyTest(_ServerTestCase):
    control_enabled = False

    def test_cli_jobs_need_control(self) -> None:
        status, payload = self._post("/api/cli", {"command": "cache-clear", "args": {}})
        self.assertEqual(status, 403)
        self.assertEqual(self._get("/api/cli")[0], 200)  # listing stays readable
        self.assertFalse(self._get("/api/capabilities")[1]["control_enabled"])


if __name__ == "__main__":
    unittest.main()
