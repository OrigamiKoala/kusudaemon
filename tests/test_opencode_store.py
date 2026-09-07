"""opencode_store: sqlite-backed thinking translation + block recovery.

No network, no agent binary: every DB here is a temp sqlite file with the
same ``part`` shape opencode uses (``id/message_id/session_id/
time_created/time_updated/data``).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.dashboard import opencode_store as store  # noqa: E402


def _make_db(path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(str(path))
    try:
        cur = con.cursor()
        cur.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,"
            " session_id TEXT NOT NULL, time_created INTEGER NOT NULL,"
            " time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        for i, payload in enumerate(rows):
            cur.execute(
                "INSERT INTO part (id, message_id, session_id, time_created,"
                " time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"prt_{i:04d}",
                    f"msg_{i // 4:04d}",
                    "ses_test",
                    1_700_000_000_000 + i * 1000,
                    1_700_000_000_000 + i * 1000,
                    json.dumps(payload),
                ),
            )
        con.commit()
    finally:
        con.close()


class TranslatePartTest(unittest.TestCase):
    def test_reasoning_becomes_thinking(self) -> None:
        entries = store.translate_part(
            {"type": "reasoning", "text": "let me think", "time": {"start": 1_700_000_001_000}},
            row_ts_ms=1_700_000_002_000,
            node_id="n",
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].role, "thinking")
        self.assertEqual(entries[0].text, "let me think")
        self.assertAlmostEqual(entries[0].timestamp, 1_700_000_001.0)
        self.assertEqual(entries[0].node_id, "n")

    def test_empty_reasoning_dropped(self) -> None:
        self.assertEqual(store.translate_part({"type": "reasoning", "text": "  "}, row_ts_ms=1), [])

    def test_text_becomes_assistant(self) -> None:
        entries = store.translate_part({"type": "text", "text": "hello"}, row_ts_ms=1_700_000_000_000)
        self.assertEqual([(e.role, e.text) for e in entries], [("assistant", "hello")])

    def test_tool_emits_call_and_result(self) -> None:
        entries = store.translate_part(
            {
                "type": "tool",
                "tool": "write",
                "callID": "c1",
                "state": {"status": "completed", "input": {"filePath": "out/n.md"}, "output": "ok"},
            },
            row_ts_ms=1_700_000_000_000,
        )
        self.assertEqual([e.role for e in entries], ["tool_call", "tool"])
        self.assertEqual(entries[0].tool_name, "write")
        self.assertEqual(entries[1].exit_code, 0)

    def test_step_finish_becomes_usage(self) -> None:
        entries = store.translate_part(
            {"type": "step-finish", "tokens": {"input": 10, "output": 20, "reasoning": 5}, "cost": 0.01},
            row_ts_ms=1_700_000_000_000,
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.role, "usage")
        self.assertEqual(entry.prompt_tokens, 10)
        self.assertEqual(entry.completion_tokens, 20)
        self.assertEqual(entry.reasoning_tokens, 5)
        self.assertEqual(entry.tokens, 35)

    def test_unknown_shapes_dropped(self) -> None:
        self.assertEqual(store.translate_part({"type": "step-start"}, row_ts_ms=1), [])
        self.assertEqual(store.translate_part({"type": "future-thing"}, row_ts_ms=1), [])

    def test_never_raises(self) -> None:
        entries = store.translate_part({"type": "tool", "state": "bogus"}, row_ts_ms="x")
        self.assertEqual([e.role for e in entries], ["tool_call"])
        self.assertEqual(store.translate_part({"type": "tool"}, row_ts_ms=None), entries[:1])


class ReadSessionEntriesTest(unittest.TestCase):
    def test_read_and_cache_hit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            _make_db(db, [{"type": "reasoning", "text": "a"}, {"type": "text", "text": "b"}])
            cache: dict = {}
            first = store.read_session_entries(db, "ses_test", cache, node_id="n")
            self.assertEqual([e.role for e in first], ["thinking", "assistant"])
            # Second call with no DB change hits the watermark cache.
            second = store.read_session_entries(db, "ses_test", cache, node_id="n")
            self.assertEqual([e.text for e in second], [e.text for e in first])

    def test_grows_monotonically_never_shrinks(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            _make_db(db, [{"type": "reasoning", "text": "keep me"}])
            cache: dict = {}
            first = store.read_session_entries(db, "ses_test", cache, node_id="n")
            self.assertEqual(len(first), 1)
            # Simulate opencode-side compaction pruning the old part.
            con = sqlite3.connect(str(db))
            con.execute("DELETE FROM part")
            con.commit()
            con.close()
            second = store.read_session_entries(db, "ses_test", cache, node_id="n")
            self.assertEqual(
                [e.text for e in second],
                ["keep me"],
                "pruned parts must not vanish from the feed",
            )

    def test_missing_db_returns_empty(self) -> None:
        self.assertEqual(store.read_session_entries("/nonexistent/opencode.db", "s", {}), [])


class RecoverBlocksTest(unittest.TestCase):
    def test_union_of_write_and_heredoc(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            _make_db(
                db,
                [
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "out/s.md", "content": "#*# Block 2 (1, 0):\nsecond"},
                        },
                    },
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "cat >> out/s.md << 'B1'\n#*# Block 1 (0, 0):\nfirst\nB1"},
                        },
                    },
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "out/s.md", "content": "#*# Block 2 (1, 0):\nsecond, longer version here"},
                        },
                    },
                ],
            )
            recovered = store.recover_blocks(db, "ses_test")
            self.assertEqual(sorted(recovered), [1, 2])
            self.assertIn("first", recovered[1])
            self.assertIn("longer version", recovered[2], "longest section wins")

    def test_split_blocks_longest_wins(self) -> None:
        sections = store.split_blocks("#*# Block 7:\nshort\n#*# Block 7:\nshort plus more words here")
        self.assertEqual(list(sections), [7])
        self.assertIn("more words", sections[7])


if __name__ == "__main__":
    unittest.main()
