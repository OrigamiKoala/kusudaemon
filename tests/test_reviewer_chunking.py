"""Reviewer chunking: delimiter-aware fan-out and a measured artifact cap.

Two defects on the same over-cap path, both hermetic:

  1. ``_sections_by_heading`` matched ATX headings only
     (``^#{1,6}[ \\t]+\\S``). An artifact whose units are delimited by
     anything else — LongGenBench's ``#*#``, which has no whitespace after
     its hashes — produced zero sections, so fan-out silently never
     engaged and the whole document went into one truncated call. The
     delimiter was already resolved per run and baked into the node's
     ``units_min:N@<delim>`` gate; the reviewer just never read it back.

  2. ``cap_artifact_text`` cut at ``ceiling * 0.75`` words, documented as
     "the inverse of ``estimate_tokens``". True of the old whitespace
     heuristic, false since ``estimate_tokens`` began delegating to the
     recalibrated ``tokens.count_tokens`` — a 50k ceiling yielded ~65.6k
     measured tokens, ~31% over, on exactly the no-split-points path that
     depends on it.

Both fire only above ``artifact_cap_tokens``; the integration cases below
lower the cap rather than building 50k-token fixtures.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.gates import estimate_tokens  # noqa: E402
from kusudaemon.v1.reviewer import (  # noqa: E402
    DEFAULT_ARTIFACT_CAP_TOKENS,
    MAX_FANOUT_SECTIONS,
    _sections_by_delimiter,
    _split_sections,
    _unit_delimiter_from_gates,
    cap_artifact_text,
    review_node,
)
from kusudaemon.v1.tree import TaskNode  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))
from fake_provider import FakeProvider  # noqa: E402

PASS = {"items": [], "verdict": "pass"}


def _node(gates: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id="a",
        brief="write",
        artifact="out/a.md",
        gates=gates if gates is not None else ["nonempty"],
        judgment=["clarity"],
        rubric={"clarity": "be clear"},
    )


def _floors(n: int, words: int = 30, preamble: str = "") -> str:
    """A LongGenBench-shaped artifact: `#*#`-delimited units, no ATX headings."""
    body = " ".join(["filler"] * words)
    return preamble + "".join(f"#*# Floor {i}:\n{body}\n\n" for i in range(1, n + 1))


class UnitDelimiterFromGatesTest(unittest.TestCase):
    def test_reads_the_delimiter_off_a_units_min_gate(self) -> None:
        node = _node(["nonempty", "units_min:100@#*#"])
        self.assertEqual(_unit_delimiter_from_gates(node), "#*#")

    def test_reads_it_from_warn_gates_too(self) -> None:
        node = _node(["nonempty"])
        node.warn_gates = ["units_min:12@==="]
        self.assertEqual(_unit_delimiter_from_gates(node), "===")

    def test_units_min_without_a_delimiter_yields_nothing(self) -> None:
        self.assertEqual(_unit_delimiter_from_gates(_node(["units_min:100"])), "")

    def test_no_units_min_gate_yields_nothing(self) -> None:
        self.assertEqual(_unit_delimiter_from_gates(_node(["nonempty", "len:200"])), "")


class DelimiterSplitTest(unittest.TestCase):
    def test_splits_a_delimited_artifact_atx_headings_cannot_see(self) -> None:
        text = _floors(100)
        self.assertEqual(_split_sections(text, ""), [], "no ATX headings to find")
        sections = _split_sections(text, "#*#")
        self.assertEqual(len(sections), 100)

    def test_sections_tile_the_document_exactly(self) -> None:
        """PLAN.md §B6: every part of the artifact lands in exactly one
        section — the property that makes deterministic splitting sound
        where model-chosen reads are not."""
        text = _floors(40)
        self.assertEqual("".join(_split_sections(text, "#*#")), text)

    def test_preamble_before_the_first_unit_is_kept_as_its_own_section(self) -> None:
        text = _floors(10, preamble="Title line and a stray note.\n\n")
        sections = _split_sections(text, "#*#")
        self.assertEqual(len(sections), 11)
        self.assertTrue(sections[0].startswith("Title line"))
        self.assertEqual("".join(sections), text)

    def test_indented_delimiters_match_like_the_gate_does(self) -> None:
        text = "  #*# One\nbody\n\n\t#*# Two\nbody\n"
        self.assertEqual(len(_sections_by_delimiter(text, "#*#")), 2)

    def test_delimiter_is_treated_as_a_literal_not_a_regex(self) -> None:
        text = "#*# A\nbody\n\n#*# B\nbody\n"
        self.assertEqual(len(_sections_by_delimiter(text, "#*#")), 2)

    def test_falls_back_to_headings_when_the_delimiter_finds_nothing(self) -> None:
        """A gate naming a delimiter the writer never used must still fan
        out on whatever structure the artifact does have."""
        text = "## One\nbody\n\n## Two\nbody\n"
        self.assertEqual(len(_split_sections(text, "#*#")), 2)

    def test_returns_empty_when_there_is_nothing_to_split_on(self) -> None:
        self.assertEqual(_split_sections("plain prose, no structure", "#*#"), [])


class FanoutEngagesOnDelimitedArtifactsTest(unittest.TestCase):
    """The regression: an over-cap `#*#` artifact used to make exactly one
    truncated whole-document call."""

    def test_delimited_over_cap_artifact_fans_out_instead_of_truncating(self) -> None:
        text = _floors(60)
        cap = estimate_tokens(text) // 4  # comfortably over cap
        provider = FakeProvider([PASS] * MAX_FANOUT_SECTIONS)
        verdict = review_node(
            _node(["nonempty", "units_min:60@#*#"]),
            text,
            provider,
            artifact_cap_tokens=cap,
        )
        self.assertEqual(len(provider.calls), MAX_FANOUT_SECTIONS)
        self.assertFalse(verdict.truncated, "each group fits; nothing should be cut")
        self.assertEqual(verdict.verdict, "pass")

    def test_without_the_gate_the_same_artifact_still_degrades_honestly(self) -> None:
        """Pins the pre-fix behavior as the fallback, so the delimiter path
        is an improvement rather than the only thing holding review up."""
        text = _floors(60)
        cap = estimate_tokens(text) // 4
        provider = FakeProvider([PASS])
        verdict = review_node(_node(["nonempty"]), text, provider, artifact_cap_tokens=cap)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(verdict.truncated)
        self.assertIn("ARTIFACT TRUNCATED", provider.calls[0][0][1]["content"])

    def test_explicit_unit_delimiter_argument_overrides_the_gate(self) -> None:
        text = _floors(60)
        cap = estimate_tokens(text) // 4
        provider = FakeProvider([PASS] * MAX_FANOUT_SECTIONS)
        review_node(
            _node(["nonempty"]),  # no units_min gate at all
            text,
            provider,
            artifact_cap_tokens=cap,
            unit_delimiter="#*#",
        )
        self.assertEqual(len(provider.calls), MAX_FANOUT_SECTIONS)

    def test_every_group_lands_under_the_cap(self) -> None:
        text = _floors(60)
        cap = estimate_tokens(text) // 4
        provider = FakeProvider([PASS] * MAX_FANOUT_SECTIONS)
        review_node(
            _node(["nonempty", "units_min:60@#*#"]),
            text,
            provider,
            artifact_cap_tokens=cap,
        )
        for messages, _schema in provider.calls:
            self.assertLessEqual(estimate_tokens(messages[1]["content"]), cap + 50)


class CapArtifactTextTest(unittest.TestCase):
    def test_measured_cap_holds_at_the_default_ceiling(self) -> None:
        """The §31%-overshoot regression, at the real ceiling."""
        big = " ".join(["word"] * 80_000)
        capped = cap_artifact_text(big, DEFAULT_ARTIFACT_CAP_TOKENS)
        self.assertLessEqual(estimate_tokens(capped), DEFAULT_ARTIFACT_CAP_TOKENS)
        self.assertIn("ARTIFACT TRUNCATED", capped)

    def test_measured_cap_holds_at_a_small_ceiling(self) -> None:
        big = " ".join(["word"] * 80_000)
        self.assertLessEqual(estimate_tokens(cap_artifact_text(big, 1_000)), 1_000)

    def test_under_cap_text_is_returned_byte_identical(self) -> None:
        text = "short enough to pass through untouched"
        self.assertEqual(cap_artifact_text(text, DEFAULT_ARTIFACT_CAP_TOKENS), text)
        self.assertNotIn("TRUNCATED", cap_artifact_text(text, DEFAULT_ARTIFACT_CAP_TOKENS))

    def test_truncation_keeps_the_head_in_document_order(self) -> None:
        text = " ".join(f"w{i}" for i in range(20_000))
        capped = cap_artifact_text(text, 500)
        self.assertTrue(capped.startswith("w0 w1 w2"))

    def test_zero_ceiling_is_empty(self) -> None:
        self.assertEqual(cap_artifact_text("anything at all", 0), "")

    def test_ceiling_too_small_for_the_marker_returns_only_the_marker(self) -> None:
        """Documented degenerate branch: say the artifact was cut rather
        than return a prefix that silently exceeds the caller's bound."""
        capped = cap_artifact_text(" ".join(["word"] * 1_000), 5)
        self.assertIn("ARTIFACT TRUNCATED", capped)
        self.assertNotIn("word", capped)


if __name__ == "__main__":
    unittest.main()
