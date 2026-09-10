"""Wave 1 post-mortem repairs (PLAN-SWEEP-REPAIR.md §G Wave 1 readout).

Everything here is pinned to something observed on the 2026-09-09 arm-C
`100-floor` seed-1 run (`~/.kusudaemon/runs/longgen_100-floor_armC_seed1`),
which produced a complete 100/100-floor artifact and was scored 75%.

Hermetic: no network, no agent binary, no API key, no sqlite.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from kusudaemon.adapters.capabilities import (  # noqa: E402
    READONLY_BASH_PATTERNS,
    translate_tools_to_opencode_permissions,
)
from kusudaemon.dashboard.rendering import TraceEntry  # noqa: E402
from kusudaemon.dashboard.state import _merge_by_timestamp  # noqa: E402
from kusudaemon.v1.reviewer import (  # noqa: E402
    ARTIFACT_LABEL,
    _SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
)
from kusudaemon.v3.assemble import strip_interior_terminator  # noqa: E402


class TraceMergeDedupeTest(unittest.TestCase):
    """The dashboard read the same episode from two sources and rendered both.

    `scratch/<node>/trace.jsonl` (written by `_agent_worker.py`) and opencode's
    own sqlite store carry the *same* events. Measured on unit-01: 9 reasoning
    blocks in each, all nine byte-identical, zero unique to either side -- and
    the endpoint served 18. The merge existed for the era when CLI backends
    streamed nothing to stdout and the file trace was empty; it must still
    cover that case, but stop double-counting.
    """

    @staticmethod
    def _thinking(text: str, ts: float) -> TraceEntry:
        return TraceEntry("thinking", text, timestamp=ts)

    def test_identical_entries_from_both_sources_appear_once(self) -> None:
        file_entries = [self._thinking(f"thought {i}", 100.0 + i) for i in range(9)]
        # The store stamps on persist, so timestamps differ slightly.
        store_entries = [self._thinking(f"thought {i}", 100.0 + i + 0.4) for i in range(9)]
        merged = _merge_by_timestamp(file_entries, store_entries)
        self.assertEqual(len(merged), 9)
        self.assertEqual([e.text for e in merged], [f"thought {i}" for i in range(9)])

    def test_store_only_entries_are_kept(self) -> None:
        """The case the merge was built for: an empty file trace."""
        store_entries = [self._thinking("only in the store", 1.0)]
        merged = _merge_by_timestamp([], store_entries)
        self.assertEqual([e.text for e in merged], ["only in the store"])

    def test_partial_overlap_keeps_the_novel_store_entries(self) -> None:
        file_entries = [self._thinking("shared", 1.0)]
        store_entries = [self._thinking("shared", 1.2), self._thinking("novel", 2.0)]
        merged = _merge_by_timestamp(file_entries, store_entries)
        self.assertEqual([e.text for e in merged], ["shared", "novel"])

    def test_genuine_repetition_survives_at_its_true_multiplicity(self) -> None:
        """unit-02 really did think "Let me try a shorter match." four times.

        That is the §F.6 edit-failure loop -- a real signal. A set-based
        dedupe would collapse it to one and hide the bug; a no-op merge would
        show eight. The answer is four.
        """
        text = "Let me try a shorter match."
        file_entries = [self._thinking(text, 10.0 + i) for i in range(4)]
        store_entries = [self._thinking(text, 10.0 + i + 0.3) for i in range(4)]
        merged = _merge_by_timestamp(file_entries, store_entries)
        self.assertEqual(len([e for e in merged if e.text == text]), 4)

    def test_more_copies_in_store_than_file_keeps_the_surplus(self) -> None:
        """Dedupe consumes, it does not truncate: a store copy with no
        unconsumed file counterpart is novel and stays."""
        text = "repeat"
        merged = _merge_by_timestamp(
            [TraceEntry("thinking", text, timestamp=1.0)],
            [TraceEntry("thinking", text, timestamp=1.1) for _ in range(3)],
        )
        self.assertEqual(len([e for e in merged if e.text == text]), 3)

    def test_same_text_different_role_is_not_a_duplicate(self) -> None:
        merged = _merge_by_timestamp(
            [TraceEntry("thinking", "ok", timestamp=1.0)],
            [TraceEntry("assistant", "ok", timestamp=1.1)],
        )
        self.assertEqual(len(merged), 2)

    def test_same_text_different_tool_is_not_a_duplicate(self) -> None:
        merged = _merge_by_timestamp(
            [TraceEntry("tool", "tool_use read", tool_name="read", timestamp=1.0)],
            [TraceEntry("tool", "tool_use read", tool_name="edit", timestamp=1.1)],
        )
        self.assertEqual(len(merged), 2)

    def test_untimestamped_entries_still_sort_to_the_head(self) -> None:
        merged = _merge_by_timestamp(
            [TraceEntry("logdir", "boot", timestamp=None), TraceEntry("thinking", "b", timestamp=5.0)],
            [TraceEntry("thinking", "a", timestamp=1.0)],
        )
        self.assertEqual([e.text for e in merged], ["boot", "a", "b"])


class InteriorTerminatorTest(unittest.TestCase):
    """A leaf is handed the whole goal, so it can emit the whole document's
    end marker. unit-03 ended its slice with `*** finished`; concatenated in
    order that sentinel landed mid-document and hid unit-04's 25 floors."""

    def test_strips_the_observed_sentinel_forms(self) -> None:
        for tail in (
            "*** finished",
            "*** finished ***",
            "***finished***",
            "<!-- END -->",
            "--- THE END ---",
            "[END OF DOCUMENT]",
        ):
            with self.subTest(tail=tail):
                out = strip_interior_terminator(f"Floor 75 body text.\n\n{tail}\n")
                self.assertNotIn("finished", out.lower().replace("finished with", ""))
                self.assertIn("Floor 75 body text.", out)

    def test_leaves_prose_alone(self) -> None:
        for text in (
            "The gym is finished with natural stone tile.",
            "Floor 75 text.\nThis wing is now complete.",
            "Floor 75: the atrium is done in brushed brass.",
            "#*# Floor 75: A complete wellness suite.",
            "Plain trailing prose with no marker at all.",
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(strip_interior_terminator(text), text)

    def test_only_the_final_line_is_considered(self) -> None:
        text = "#*# Floor 51: body.\n*** finished\n#*# Floor 52: more body."
        self.assertEqual(strip_interior_terminator(text), text)


class ConcatenationTest(unittest.TestCase):
    def test_interior_sentinel_stripped_final_one_kept(self) -> None:
        from kusudaemon.v1.tree import TaskNode, TaskTree
        from kusudaemon.v3.assemble import concatenate_artifacts

        with tempfile.TemporaryDirectory() as root:
            run_dir = Path(root)
            (run_dir / "out").mkdir(parents=True)
            nodes = {}
            for i, tail in enumerate(("", "", "\n\n*** finished", "\n\n*** finished ***"), start=1):
                nid = f"unit-0{i}"
                (run_dir / "out" / f"{nid}.md").write_text(
                    f"#*# Floor {i}: body text.{tail}\n", encoding="utf-8"
                )
                nodes[nid] = TaskNode(
                    id=nid, brief=f"slice {i}", artifact=f"out/{nid}.md", gates=["nonempty"]
                )
            tree = TaskTree(nodes=nodes)
            out = concatenate_artifacts(run_dir, tree)

        # unit-03's sentinel is gone; unit-04's body survived it.
        self.assertIn("Floor 4: body text.", out)
        self.assertEqual(out.count("finished"), 1)
        self.assertLess(out.index("Floor 4: body text."), out.index("finished"))


class EvaluatorSentinelTest(unittest.TestCase):
    """`FINISHED_RE` is `.*` under DOTALL, so the original `sub` cut at the
    FIRST sentinel and deleted the rest of the document."""

    def test_last_sentinel_wins(self) -> None:
        import longgen_common as lc

        raw = (
            "#*# Floor 1: a.\n#*# Floor 2: b.\n*** finished\n"
            "#*# Floor 3: c.\n#*# Floor 4: d.\n*** finished ***\n"
        )
        blocks = lc.to_output_blocks(raw, {"type": "Floor", "prefix": "", "number": 4})
        found = lc.parse_blocks(blocks, "Floor")
        self.assertEqual(sorted(found), [1, 2, 3, 4])
        self.assertEqual(lc.calculate_completion_rate(found, 4), 100.0)

    def test_single_sentinel_behaviour_is_unchanged(self) -> None:
        import longgen_common as lc

        raw = "#*# Floor 1: a.\n#*# Floor 2: b.\n*** finished ***\n"
        blocks = lc.to_output_blocks(raw, {"type": "Floor", "prefix": "", "number": 2})
        found = lc.parse_blocks(blocks, "Floor")
        self.assertEqual(sorted(found), [1, 2])
        self.assertNotIn("finished", "".join(blocks).lower())


class ReviewerProvenanceTest(unittest.TestCase):
    """Tell the reviewer the artifact is another agent's work, so it does not
    extend it the benefit of the doubt it would give its own draft."""

    def test_system_prompt_names_a_different_agent(self) -> None:
        low = _SYSTEM_PROMPT.lower()
        self.assertIn("different ai agent", low)
        self.assertIn("not your own work", low)

    def test_triage_prompt_names_a_different_agent(self) -> None:
        low = _TRIAGE_SYSTEM_PROMPT.lower()
        self.assertIn("different ai agent", low)
        self.assertIn("not your own work", low)

    def test_artifact_label_repeats_provenance_next_to_the_artifact(self) -> None:
        low = ARTIFACT_LABEL.lower()
        self.assertTrue(low.startswith("artifact"))
        self.assertIn("another agent", low)


class ReadOnlyBashTest(unittest.TestCase):
    """The writer wanted to inspect bytes to repair a failing exact-match
    edit, had `bash` hard-denied, and rewrote the whole file instead."""

    def test_writer_gets_inspection_commands_and_a_catchall_deny(self) -> None:
        perms = translate_tools_to_opencode_permissions(
            ("read", "save"), include_web_search=True, readonly_bash=True
        )
        bash = perms["bash"]
        self.assertIsInstance(bash, dict)
        self.assertEqual(bash["*"], "deny")
        for pattern in ("cat *", "wc *", "xxd *", "python3 -m json.tool *"):
            self.assertEqual(bash[pattern], "allow")
        self.assertNotIn("rm *", bash)
        self.assertNotIn("curl *", bash)

    def test_patterns_are_read_only_commands(self) -> None:
        mutating = ("rm", "mv", "cp", "curl", "wget", "pip", "npm", "git", "chmod", "dd")
        for pattern in READONLY_BASH_PATTERNS:
            head = pattern.split()[0]
            self.assertNotIn(head, mutating, f"{pattern} is not read-only")

    def test_probes_stay_hard_denied(self) -> None:
        """A research probe summarises what it reads; it never needs a shell."""
        self.assertEqual(translate_tools_to_opencode_permissions(("read",))["bash"], "deny")
        self.assertEqual(
            translate_tools_to_opencode_permissions((), include_web_search=True)["bash"], "deny"
        )

    def test_explicit_shell_grant_still_wins(self) -> None:
        perms = translate_tools_to_opencode_permissions(("read", "shell"), readonly_bash=True)
        self.assertEqual(perms["bash"], "allow")

    def test_env_flag_restores_the_hard_deny(self) -> None:
        import os

        prev = os.environ.get("KUSUDAEMON_WRITER_READONLY_BASH")
        os.environ["KUSUDAEMON_WRITER_READONLY_BASH"] = "0"
        try:
            perms = translate_tools_to_opencode_permissions(("read", "save"), readonly_bash=True)
            self.assertEqual(perms["bash"], "deny")
        finally:
            if prev is None:
                os.environ.pop("KUSUDAEMON_WRITER_READONLY_BASH", None)
            else:
                os.environ["KUSUDAEMON_WRITER_READONLY_BASH"] = prev


if __name__ == "__main__":
    unittest.main()
