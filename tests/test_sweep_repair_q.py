"""PLAN-SWEEP-REPAIR.md §Q — arm A was scored on narration, in the wrong directory.

§Q1  Arm A's artifact was the bare CLI's stdout and nothing else. `opencode run`
     prints assistant text, not tool results, so a coding agent that answers a
     generation prompt by writing a script and reading the file back hands the
     document over through a channel stdout never sees. 301-block armA seed1
     wrote a complete 100-block document and scored 1 % on 1 972 chars of "Let
     me output the full document". Recovery has to resolve opencode's
     truncated-output spool, must not accept the echoed user prompt as an
     answer, and must refuse a `grep`-style excerpt that carries unit headers
     without their bodies.
§Q2  `cwd=` alone did not keep the bare CLI in its workspace: the inherited
     $PWD still named the sweep's launch directory, and opencode bound the
     session there instead.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from kusudaemon.adapters.opencode_session import (
    best_artifact,
    candidates_for_sessions,
    session_ids_from_log,
    session_ids_from_output,
)
from kusudaemon.pipeline.cli import (
    _qualifies,
    _richest_arm_a_artifact,
    _unit_count,
    _workspace_artifact,
)

DELIM = "#*#"


def unit(n: int, body_chars: int = 1000) -> str:
    return f"{DELIM} Block {n}: " + ("word " * (body_chars // 5))


def document(count: int) -> str:
    return "\n\n".join(unit(i) for i in range(1, count + 1)) + "\n*** finished"


def make_db(path: Path, sessions: dict[str, dict]) -> None:
    """Minimal stand-in for opencode.db: session / message / part."""
    con = sqlite3.connect(path)
    con.executescript(
        "create table session (id text primary key, directory text);"
        "create table message (id text primary key, session_id text, "
        " time_created integer, data text);"
        "create table part (id text primary key, message_id text, session_id text,"
        " time_created integer, data text);"
    )
    seq = 0
    for sid, spec in sessions.items():
        con.execute("insert into session values (?,?)", (sid, spec.get("directory", "/ws")))
        for mi, msg in enumerate(spec["messages"]):
            mid = f"{sid}_m{mi}"
            con.execute(
                "insert into message values (?,?,?,?)",
                (mid, sid, mi, json.dumps({"role": msg["role"]})),
            )
            for part in msg["parts"]:
                seq += 1
                con.execute(
                    "insert into part values (?,?,?,?,?)",
                    (f"p{seq}", mid, sid, seq, json.dumps(part)),
                )
    con.commit()
    con.close()


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_part(output: str, command: str = "cat out.md") -> dict:
    return {"type": "tool", "tool": "bash",
            "state": {"input": {"command": command}, "output": output}}


class ExcerptRejection(unittest.TestCase):
    """§Q1: a unit header without its body is not a unit."""

    def test_document_qualifies(self):
        doc = document(100)
        self.assertEqual(_unit_count(doc, DELIM), 100)
        self.assertEqual(_qualifies(doc, DELIM), 100)

    def test_grep_excerpt_is_rejected(self):
        # 301-block armA seed3's best candidate: 10 headers in 482 chars. Scoring
        # it would report 10 % on a document the harness never captured.
        excerpt = "\n".join(f"{DELIM} Block {n} (1, 7):" for n in (18, 28, 32, 42, 51,
                                                                   72, 78, 80, 86, 98))
        self.assertEqual(_unit_count(excerpt, DELIM), 10)
        self.assertEqual(_qualifies(excerpt, DELIM), 0)

    def test_narration_has_no_units(self):
        self.assertEqual(_qualifies("Let me output the full document.", DELIM), 0)


class WorkspaceHarvest(unittest.TestCase):
    """§Q1: a file the agent left behind beats stdout when it holds more."""

    def test_prefers_largest_qualifying_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "scratch.md").write_text(document(3), encoding="utf-8")
            (ws / "answer.md").write_text(document(52), encoding="utf-8")
            (ws / "notes.txt").write_text("no units here", encoding="utf-8")
            text, units = _workspace_artifact(ws, DELIM)
            self.assertEqual(units, 52)
            self.assertIn("Block 52", text)

    def test_stdout_loses_to_workspace_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "answer.md").write_text(document(100), encoding="utf-8")
            text, source = _richest_arm_a_artifact(
                "Let me output the full document.",
                ws_path=ws,
                backend="gptme",          # no session store involved
                combined_output="",
                delimiter=DELIM,
                session_ids_out=[],
            )
            self.assertEqual(source, "workspace_file")
            self.assertEqual(_unit_count(text, DELIM), 100)

    def test_no_delimiter_means_stdout_is_kept(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            text, source = _richest_arm_a_artifact(
                "stdout only",
                ws_path=Path(td),
                backend="gptme",
                combined_output="",
                delimiter="",
                session_ids_out=[],
            )
            self.assertEqual((text, source), ("stdout only", "stdout"))


class SessionMapping(unittest.TestCase):
    """§Q1/§Q2: find the session a cell ran in, without trusting its directory."""

    def test_session_id_from_process_output(self):
        out = (
            'timestamp=2026-09-12T19:53:31.349Z level=INFO run=a8ebea7e '
            'message=created id=ses_f68d15c2affeH3Xp3m0VzGvK2X slug=jolly-squid\n'
        )
        self.assertEqual(session_ids_from_output(out), ["ses_f68d15c2affeH3Xp3m0VzGvK2X"])

    def test_log_maps_by_first_instance_not_session_directory(self):
        import tempfile
        log = (
            'timestamp=1 run=aaaaaaaa message="creating instance" '
            'directory=/Users/x/.kusudaemon/bench_workspaces/longgen/301-block_armA_seed1\n'
            # §Q2: opencode then rebinds to the launch directory. The cell is
            # still identified by the FIRST instance, which is its real cwd.
            'timestamp=2 run=aaaaaaaa message="creating instance" directory=/Users/x/kusudaemon\n'
            'timestamp=3 run=aaaaaaaa message=created id=ses_AAA projectID=deadbeef\n'
            'timestamp=4 run=bbbbbbbb message="creating instance" '
            'directory=/Users/x/.kusudaemon/bench_workspaces/longgen/301-block_armA_seed2\n'
            'timestamp=5 run=bbbbbbbb message=created id=ses_BBB\n'
        )
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "opencode.log"
            lp.write_text(log, encoding="utf-8")
            self.assertEqual(
                session_ids_from_log("/anywhere/301-block_armA_seed1", log_path=lp),
                ["ses_AAA"],
            )
            self.assertEqual(
                session_ids_from_log("/anywhere/301-block_armA_seed2", log_path=lp),
                ["ses_BBB"],
            )
            self.assertEqual(session_ids_from_log("/anywhere/nope", log_path=lp), [])


class SessionStoreHarvest(unittest.TestCase):
    """§Q1: the three channels opencode splits the truth across."""

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.db = self.root / "opencode.db"
        (self.root / "tool-output").mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_prompt_echo_is_never_the_artifact(self):
        # A LongGenBench prompt ends with the first unit already filled in, so
        # echoing it back must not score as a partial answer.
        prompt = "Design a city...\n*** started ***\n" + unit(1)
        make_db(self.db, {"ses_A": {"messages": [
            {"role": "user", "parts": [text_part(prompt)]},
            {"role": "assistant", "parts": [text_part("Let me start.")]},
        ]}})
        self.assertIsNone(best_artifact(["ses_A"], delimiter=DELIM, db_path=self.db))

    def test_tool_result_beats_assistant_narration(self):
        make_db(self.db, {"ses_A": {"messages": [
            {"role": "assistant", "parts": [
                text_part("Let me output the full document."),
                tool_part(document(100)),
                text_part("The document is complete."),
            ]},
        ]}})
        cand = best_artifact(["ses_A"], delimiter=DELIM, db_path=self.db)
        self.assertIsNotNone(cand)
        self.assertEqual(cand.source, "tool_output")
        self.assertEqual(cand.markers, 100)

    def test_truncated_tool_result_resolves_to_its_spool(self):
        # The row held blocks 72-100 of a 100-block document; the spool held all
        # of it. Reading the row alone loses 71 % and looks like a partial answer.
        spool_id = "tool_09737f8bf0013c8DaqRrrcyx4o"
        (self.root / "tool-output" / spool_id).write_text(document(100), encoding="utf-8")
        row = (
            "...output truncated...\n\n"
            f"Full output saved to: /Users/x/.local/share/opencode/tool-output/{spool_id}\n\n"
            + document(29)
        )
        make_db(self.db, {"ses_A": {"messages": [
            {"role": "assistant", "parts": [tool_part(row, "cat /tmp/city_design.md")]},
        ]}})
        cand = best_artifact(["ses_A"], delimiter=DELIM, db_path=self.db)
        self.assertEqual(cand.source, "tool_output_spool")
        self.assertTrue(cand.spooled)
        self.assertEqual(cand.markers, 100)

    def test_spool_is_resolved_beside_the_database_not_the_reader_home(self):
        # Reading another machine's store (a mounted home) must still find the
        # spool, or truncation recovery silently switches itself off.
        spool_id = "tool_deadbeef"
        (self.root / "tool-output" / spool_id).write_text(document(52), encoding="utf-8")
        row = f"...output truncated...\n\nFull output saved to: /elsewhere/{spool_id}\n\n" + unit(1)
        make_db(self.db, {"ses_A": {"messages": [
            {"role": "assistant", "parts": [tool_part(row)]},
        ]}})
        cand = best_artifact(["ses_A"], delimiter=DELIM, db_path=self.db)
        self.assertEqual(cand.markers, 52)

    def test_candidates_skip_empty_output(self):
        make_db(self.db, {"ses_A": {"messages": [
            {"role": "assistant", "parts": [tool_part(""), text_part("   ")]},
        ]}})
        self.assertEqual(list(candidates_for_sessions(["ses_A"], db_path=self.db)), [])


class BareProcessEnvironment(unittest.TestCase):
    """§Q2: $PWD must agree with cwd, or opencode rebinds to the launch dir."""

    def test_bench_arm_a_exports_workspace_as_pwd(self):
        import tempfile
        from types import SimpleNamespace
        from kusudaemon.pipeline import cli

        seen: dict[str, object] = {}

        class Res:
            returncode = 0
            stdout = "done"
            stderr = ""

        def fake_runner(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["cwd"] = kwargs.get("cwd")
            seen["env"] = kwargs.get("env")
            return Res()

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "301-block_armA_seed1"
            ws.mkdir()
            goal = Path(td) / "goal.txt"
            goal.write_text("write a document", encoding="utf-8")
            argv = SimpleNamespace(
                arm="A", benchmark="longgenbench", task_id="301-block", seed=1,
                workspace=str(ws), goal_file=str(goal), goal=None, backend="opencode",
                model=None, output_dir=None, output=None, json=True, round=1,
                session_id=None, run_id="longgen_301-block_armA_seed1",
                artifact_delimiter="#*#", runs_root=None,
            )
            cli.cmd_bench(argv, subprocess_runner=fake_runner)

        self.assertEqual(seen["cwd"], str(ws.resolve()))
        self.assertIsNotNone(seen["env"])
        self.assertEqual(seen["env"]["PWD"], str(ws.resolve()))
        self.assertNotIn("OLDPWD", seen["env"])


if __name__ == "__main__":
    unittest.main()
