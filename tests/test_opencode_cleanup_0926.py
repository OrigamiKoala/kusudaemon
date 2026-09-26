"""2026-09-26 OpenCode session cleanup (``adapters/opencode_cleanup.py``).

OpenCode's store had grown to 4.3 GB and never deletes anything; 2.6 GB of it
came from two deleted 08-15 corpus runs. A passed node's session is now
deleted through ``opencode session delete`` (the same rule as ``scratch/``),
and a completed run sweeps every session it captured. Opt-in: sessions are
kept as debugging records unless ``KUSUDAEMON_OPENCODE_SESSION_CLEANUP=1``. Only ``ses_`` ids that
exist in the store are touched, and the runner never resumes a deleted
session.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from kusudaemon.adapters import opencode_cleanup  # noqa: E402
from kusudaemon.adapters.opencode_cleanup import (  # noqa: E402
    delete_run_sessions,
    schedule_node_cleanup,
    sessions_to_delete,
)
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v0.runner import run_node  # noqa: E402

FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"


def _captured(node: str, sid: str) -> dict:
    return {"node_id": node, "role": "writer", "round": 0, "type": "session_captured", "session_id": sid}


class _FakeOpenCode:
    """A temp OpenCode data dir holding ``existing`` sessions, plus an
    ``opencode`` script on PATH that logs each delete and prints ``stderr``."""

    def __init__(self, root: Path, existing: list[str], *, stderr: str = "", code: int = 0) -> None:
        data = root / "opencode-data"
        data.mkdir()
        con = sqlite3.connect(data / "opencode.db")
        con.execute("create table session (id text primary key, directory text)")
        con.executemany("insert into session values (?, ?)", [(s, str(root)) for s in existing])
        con.commit()
        con.close()
        bindir = root / "bin"
        bindir.mkdir()
        self.calls = root / "calls.txt"
        script = bindir / "opencode"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.calls}"\n'
            f"printf '%s' '{stderr}' >&2\n"
            f"exit {code}\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.env = {
            "OPENCODE_DATA_DIR": str(data),
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "KUSUDAEMON_OPENCODE_BOOT_GAP_S": "0",
            "KUSUDAEMON_OPENCODE_SESSION_CLEANUP": "1",
        }
        self.env_clear = {k: v for k, v in os.environ.items() if k != "KUSUDAEMON_OPENCODE_SESSION_CLEANUP"}

    def deleted(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [line.split()[-1] for line in self.calls.read_text().splitlines()]


def _run_dir_with(root: Path, events: list[dict]) -> tuple[Path, EventLog]:
    run_dir = create_run_dir(root, "run")
    log = EventLog(run_dir / "events.jsonl")
    for event in events:
        log.append(event)
    return run_dir, log


class SelectionTest(unittest.TestCase):
    def test_only_undeleted_opencode_ids_for_the_node(self) -> None:
        events = [
            _captured("unit-01", "ses_a"),
            _captured("unit-01", "ses_a"),
            _captured("unit-01", "3f2c-claude-uuid"),
            _captured("unit-02", "ses_b"),
            _captured("unit-01", "ses_c"),
            {"node_id": "unit-01", "type": "opencode_session_deleted", "session_id": "ses_c"},
        ]
        self.assertEqual(sessions_to_delete(events, "unit-01"), [("ses_a", "unit-01")])
        self.assertEqual(
            sessions_to_delete(events), [("ses_a", "unit-01"), ("ses_b", "unit-02")]
        )


class DeleteTest(unittest.TestCase):
    def test_deletes_existing_sessions_and_logs_them(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oc = _FakeOpenCode(root, ["ses_a", "ses_b"])
            run_dir, log = _run_dir_with(
                root, [_captured("unit-01", "ses_a"), _captured("unit-02", "ses_b"), _captured("unit-02", "ses_gone")]
            )
            with patch.dict(os.environ, {**oc.env_clear, **oc.env}, clear=True):
                deleted = asyncio.run(delete_run_sessions(run_dir, log.append))
            self.assertEqual(deleted, ["ses_a", "ses_b"])
            # ses_gone is not in the store, so the CLI never saw it.
            self.assertEqual(oc.deleted(), ["ses_a", "ses_b"])
            logged = [e for e in log.read_all() if e["type"] == "opencode_session_deleted"]
            self.assertEqual([(e["node_id"], e["session_id"]) for e in logged], [("unit-01", "ses_a"), ("unit-02", "ses_b")])
            # A second sweep has nothing left to do.
            with patch.dict(os.environ, {**oc.env_clear, **oc.env}, clear=True):
                self.assertEqual(asyncio.run(delete_run_sessions(run_dir, log.append)), [])

    def test_not_found_counts_as_deleted_and_errors_are_logged(self) -> None:
        for stderr, code, expected in (
            ("Error: Session not found: ses_a", 1, "opencode_session_deleted"),
            ("Error: database is locked", 1, "opencode_session_delete_failed"),
        ):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                oc = _FakeOpenCode(root, ["ses_a"], stderr=stderr, code=code)
                run_dir, log = _run_dir_with(root, [_captured("unit-01", "ses_a")])
                with patch.dict(os.environ, {**oc.env_clear, **oc.env}, clear=True):
                    asyncio.run(delete_run_sessions(run_dir, log.append))
                types = [e["type"] for e in log.read_all() if e.get("session_id") == "ses_a"]
                self.assertEqual(types[-1], expected, stderr)

    def test_off_by_default_and_when_disabled(self) -> None:
        # Sessions are debugging records; nothing is deleted unless asked.
        for value in (None, "0"):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                oc = _FakeOpenCode(root, ["ses_a"])
                run_dir, log = _run_dir_with(root, [_captured("unit-01", "ses_a")])
                env = {**oc.env_clear, **oc.env}
                env.pop("KUSUDAEMON_OPENCODE_SESSION_CLEANUP")
                if value is not None:
                    env["KUSUDAEMON_OPENCODE_SESSION_CLEANUP"] = value
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(asyncio.run(delete_run_sessions(run_dir, log.append)), [])
                    self.assertIsNone(schedule_node_cleanup(run_dir, log.append, "unit-01"))
                self.assertEqual(oc.deleted(), [], value)

    def test_node_cleanup_runs_in_the_background_for_that_node_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oc = _FakeOpenCode(root, ["ses_a", "ses_b"])
            run_dir, log = _run_dir_with(root, [_captured("unit-01", "ses_a"), _captured("unit-02", "ses_b")])
            with patch.dict(os.environ, {**oc.env_clear, **oc.env}, clear=True):
                thread = schedule_node_cleanup(run_dir, log.append, "unit-01")
                self.assertIsNotNone(thread)
                thread.join(timeout=30)
            self.assertEqual(oc.deleted(), ["ses_a"])

    def test_missing_store_deletes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            oc = _FakeOpenCode(root, ["ses_a"])
            run_dir, log = _run_dir_with(root, [_captured("unit-01", "ses_a")])
            env = {**oc.env_clear, **oc.env, "OPENCODE_DATA_DIR": str(root / "nowhere")}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(asyncio.run(delete_run_sessions(run_dir, log.append)), [])
            self.assertEqual(oc.deleted(), [])


class RunnerNeverResumesADeletedSessionTest(unittest.TestCase):
    def test_reopened_node_starts_a_fresh_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node_id = "writer_1"
            run_dir, log = _run_dir_with(
                root,
                [
                    {"node_id": node_id, "role": "writer", "round": 0, "type": "node_dispatched"},
                    _captured(node_id, "ses_old"),
                    {"node_id": node_id, "role": "harness", "round": 0,
                     "type": "opencode_session_deleted", "session_id": "ses_old"},
                ],
            )
            prompt_dir = root / "prompts"
            prompt_dir.mkdir()
            adapter = FakeStreamAgentAdapter(
                script_path=str(FAKE_CLI),
                pidfile=str(root / "w.pid"),
                prompt_dir=str(prompt_dir),
                workspace_path=str(run_dir),
            )
            result = asyncio.run(
                run_node(
                    run_dir, node_id, "do the task", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                )
            )
            self.assertEqual(result.status, "done")
            reasons = [e.get("reason") for e in log.read_all() if e["type"] == "node_redispatched"]
            self.assertEqual(reasons, ["session_deleted"])
            artifact = (run_dir / "out" / f"{node_id}.md").read_text()
            self.assertNotIn("resume_acknowledged", artifact)


if __name__ == "__main__":
    unittest.main()
