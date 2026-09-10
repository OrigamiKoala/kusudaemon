"""PLAN-SWEEP-REPAIR.md F5 and F6, both hermetic (no provider calls).

F5 - an unevaluatable rubric item must not fail. `claims_supported` asks
     "where did this claim come from?"; on a greenfield generative leaf whose
     contract declares no rules and whose only declared input is the harness
     restating the assignment, there is no answer. Observed live: unit-01 and
     unit-02 of the same run, same template, same class of invented prose,
     passed and failed three minutes apart. It is not a gate, so it never
     blocks all_passed - but a failed verdict costs an attempt, and three of
     those lose the leaf.

F6 - typographic punctuation the writer types itself, then cannot match with
     its exact-match edit tool. From the writer's own trace: "the file has
     `skyscraper's` with a right single quotation mark ... when I try to match
     with a regular apostrophe it doesn't work", then "let me just use the
     write tool to write the entire file" - the whole-file clobber.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v0.run_dir import (  # noqa: E402
    node_artifact_path,
    node_artifact_text,
    node_parts_dir,
    normalize_artifact_punctuation,
    normalize_typographic_punctuation,
)
from kusudaemon.v1.reviewer import (  # noqa: E402
    contract_declares_rules,
    effective_judgment_for,
    has_traceable_source,
    review_node,
    ungroundable_judgments,
)
from kusudaemon.v1.tree import TaskNode  # noqa: E402

sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))
from fake_provider import FakeProvider  # noqa: E402

# The real contract this run rendered: sections present, no rules in them.
EMPTY_CONTRACT = (
    "# Contract\n\n## Global rubric\n(none)\n\n## Assumptions\n"
    "- Intake was skipped: the PLAN.md A4.2 scope estimate reported no "
    "ambiguities or objections, so no clarifying questions were asked (B2).\n"
)
RULED_CONTRACT = EMPTY_CONTRACT.replace(
    "(none)", "- Every floor description names its intended use."
)
SPINE_ONLY = 'Spine unit: {"id": "unit-02", "brief": "Floors 26 to 50"}'
WITH_RESEARCH = SPINE_ONLY + "\n\nResearch finding (src/brief.md): geothermal exchange"
WITH_HANDOFF = SPINE_ONLY + "\n\nHandoff from unit-01: floors 1-25 established the atrium"


def _node(judgment=("on_topic", "claims_supported")) -> TaskNode:
    return TaskNode(
        id="unit-02",
        brief="Produce the artifact for Floors 26 to 50.",
        artifact="out/unit-02.md",
        gates=["nonempty", "units_min:25@#*#"],
        judgment=list(judgment),
        judgment_classification={j: "closed" for j in judgment},
        rubric={j: f"rubric for {j}" for j in judgment},
    )


def _floors(lo: int, hi: int) -> str:
    return "".join(
        f"#*# Floor {i}:\nInvented prose about floor {i}. " + "filler " * 20 + "\n\n"
        for i in range(lo, hi + 1)
    )


class GroundingPredicateTest(unittest.TestCase):
    def test_rendered_but_empty_contract_declares_no_rules(self) -> None:
        self.assertFalse(contract_declares_rules(EMPTY_CONTRACT))

    def test_a_contract_with_a_rule_declares_rules(self) -> None:
        self.assertTrue(contract_declares_rules(RULED_CONTRACT))

    def test_spine_unit_alone_is_not_a_traceable_source(self) -> None:
        """The spine slice is the harness restating the assignment, not a
        source to trace a claim to."""
        self.assertFalse(has_traceable_source(SPINE_ONLY))

    def test_research_finding_is_a_traceable_source(self) -> None:
        self.assertTrue(has_traceable_source(WITH_RESEARCH))

    def test_dependency_handoff_is_a_traceable_source(self) -> None:
        self.assertTrue(has_traceable_source(WITH_HANDOFF))

    def test_ungroundable_only_names_grounding_items(self) -> None:
        self.assertEqual(
            ungroundable_judgments(
                ["on_topic", "claims_supported"], EMPTY_CONTRACT, SPINE_ONLY
            ),
            ["claims_supported"],
        )

    def test_nothing_is_ungroundable_once_a_source_exists(self) -> None:
        self.assertEqual(
            ungroundable_judgments(
                ["on_topic", "claims_supported"], EMPTY_CONTRACT, WITH_RESEARCH
            ),
            [],
        )

    def test_nothing_is_ungroundable_once_the_contract_has_rules(self) -> None:
        self.assertEqual(
            ungroundable_judgments(
                ["on_topic", "claims_supported"], RULED_CONTRACT, SPINE_ONLY
            ),
            [],
        )


class VacuousPassTest(unittest.TestCase):
    def test_ungroundable_item_is_not_sent_to_the_model(self) -> None:
        provider = FakeProvider(
            [{"items": [{"id": "on_topic", "pass": True, "defect": "none"}], "verdict": "pass"}]
        )
        review_node(
            _node(), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=SPINE_ONLY,
        )
        sent = provider.calls[0][0][1]["content"]
        rubric_block = sent.split("Contract:")[0]
        self.assertIn("on_topic", rubric_block)
        self.assertNotIn("claims_supported", rubric_block)

    def test_ungroundable_item_is_recorded_as_a_vacuous_pass(self) -> None:
        """Not silently dropped: the audit must still show the item, that it
        passed, and why it was not judged."""
        provider = FakeProvider(
            [{"items": [{"id": "on_topic", "pass": True, "defect": "none"}], "verdict": "pass"}]
        )
        verdict = review_node(
            _node(), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=SPINE_ONLY,
        )
        by_id = {i["id"]: i for i in verdict.items}
        self.assertIn("claims_supported", by_id)
        self.assertTrue(by_id["claims_supported"]["pass"])
        self.assertIn("vacuous pass", by_id["claims_supported"]["detail"])
        self.assertEqual(verdict.verdict, "pass")

    def test_the_live_failure_would_now_pass(self) -> None:
        """The exact shape that failed unit-02: reviewer would have flagged
        claims_supported, but it is never asked, so the leaf keeps its attempt."""
        provider = FakeProvider(
            [{"items": [{"id": "on_topic", "pass": True, "defect": "none"}], "verdict": "pass"}]
        )
        verdict = review_node(
            _node(), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=SPINE_ONLY,
        )
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(len(provider.calls), 1)

    def test_a_grounded_leaf_still_gets_judged_and_can_still_fail(self) -> None:
        """The rule must not disable the item wherever it is answerable."""
        provider = FakeProvider([{
            "items": [
                {"id": "on_topic", "pass": True, "defect": "none"},
                {"id": "claims_supported", "pass": False, "defect": "untraced claim"},
            ],
            "verdict": "fail",
        }])
        verdict = review_node(
            _node(), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=WITH_RESEARCH,
        )
        self.assertEqual(verdict.verdict, "fail")
        sent = provider.calls[0][0][1]["content"]
        self.assertIn("claims_supported", sent.split("Contract:")[0])

    def test_a_vacuous_pass_never_rescues_a_real_failure(self) -> None:
        provider = FakeProvider([{
            "items": [{"id": "on_topic", "pass": False, "defect": "off topic"}],
            "verdict": "fail",
        }])
        verdict = review_node(
            _node(), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=SPINE_ONLY,
        )
        self.assertEqual(verdict.verdict, "fail")

    def test_a_leaf_whose_only_item_is_ungroundable_skips_the_call(self) -> None:
        provider = FakeProvider([])  # any call raises
        verdict = review_node(
            _node(judgment=("claims_supported",)), _floors(26, 50), provider,
            contract_text=EMPTY_CONTRACT, declared_inputs=SPINE_ONLY,
        )
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.skip_reason, "ungroundable_filtered")
        self.assertEqual(len(provider.calls), 0)
        self.assertIn("vacuous pass", verdict.items[0]["detail"])

    def test_effective_judgment_helper_matches_the_documented_filters(self) -> None:
        node = _node(judgment=("on_topic", "claims_supported", "coverage_gap"))
        node.judgment_classification = {
            "on_topic": "closed", "claims_supported": "open", "coverage_gap": "closed",
        }
        # coverage_gap is cross-leaf (T1-4), claims_supported is open (T2-2)
        self.assertEqual(effective_judgment_for(node), ["on_topic"])


class TypographicNormalizationTest(unittest.TestCase):
    def test_the_exact_character_from_the_live_trace(self) -> None:
        self.assertEqual(
            normalize_typographic_punctuation("the skyscraper’s core"),
            "the skyscraper's core",
        )

    def test_quotes_dashes_ellipsis_and_exotic_spaces(self) -> None:
        raw = "“walls” — fine… a b – c"
        self.assertEqual(normalize_typographic_punctuation(raw), '"walls" -- fine... a b - c')

    def test_letters_and_non_latin_text_are_untouched(self) -> None:
        """Punctuation-only: an artifact that is supposed to contain accented
        or non-Latin text keeps it."""
        for text in ("café naïve", "你好世界", "αβγ"):
            self.assertEqual(normalize_typographic_punctuation(text), text)

    def test_ascii_text_is_returned_unchanged(self) -> None:
        text = "already 'plain' -- ASCII... text"
        self.assertEqual(normalize_typographic_punctuation(text), text)

    def test_normalizes_a_single_file_artifact_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            path = node_artifact_path(run_dir, "n1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("the floor’s “walls”\n", encoding="utf-8")
            self.assertEqual(normalize_artifact_punctuation(run_dir, "n1"), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), 'the floor\'s "walls"\n')

    def test_normalizes_part_files_without_collapsing_the_layout(self) -> None:
        """A concatenate-then-write would destroy the parts layout the
        artifact-identity work (B) depends on."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            parts = node_parts_dir(run_dir, "n1")
            parts.mkdir(parents=True, exist_ok=True)
            (parts / "part-01.md").write_text("floor’s one\n", encoding="utf-8")
            (parts / "part-02.md").write_text("floor’s two\n", encoding="utf-8")
            self.assertEqual(normalize_artifact_punctuation(run_dir, "n1"), 2)
            self.assertTrue((parts / "part-01.md").is_file())
            self.assertTrue((parts / "part-02.md").is_file())
            self.assertNotIn("’", node_artifact_text(run_dir, "n1"))
            self.assertFalse(node_artifact_path(run_dir, "n1").exists())

    def test_reports_zero_when_there_is_nothing_to_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            path = node_artifact_path(run_dir, "n1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("plain ascii only\n", encoding="utf-8")
            self.assertEqual(normalize_artifact_punctuation(run_dir, "n1"), 0)

    def test_missing_artifact_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(normalize_artifact_punctuation(Path(td) / "run", "nope"), 0)


if __name__ == "__main__":
    unittest.main()
