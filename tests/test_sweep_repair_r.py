"""PLAN-SWEEP-REPAIR.md §R — a truncated classify call scored 5 of 100 floors as 1.0.

longgen_137-floor_armC_seed3 (2026-09-13, commit 7d2f08c) recorded
completion_rate 5.0 alongside score 1.0, resolved true, termination
gates_satisfied, nodes_passed 1/1. Not a halt -- a false success, produced by
four independent defects in series:

§R1  ``_repair_common_schema_omissions`` rewrites every ESTIMATE_SCHEMA field it
     does not like, so handed an object that is not an estimate it fabricates a
     schema-valid one out of defaults. On a truncated classify response the
     outer object never closes and ``extract_last_json_object``'s scanner
     recovers some complete inner fragment -- a single ``questions[]`` entry --
     which became ``files_touched "unknown"``. seed3's tier.json estimate is
     that all-defaults object field for field, on a response whose JSON
     literally read ``files_touched "1"``.
§R2  ``complete_json``'s §P3 ceiling escalation was guarded by ``parsed is None
     and truncated``, so it only covered a response too short to hold any JSON.
     A response truncated mid-object never escalated and never retried: seed3's
     classify was a single call of exactly 4096 completion tokens, which is
     ``_default_max_tokens(FULL_SCOPE_SCHEMA)``.
§R3  ``files_touched == "unknown"`` forces T2, and the T2 planner derived a
     leaf's unit count only from the spine, never from the goal. A structurally
     chunked spine over a short prompt is one unit labelled "Opening section"
     with units_expected=None, so the leaf shipped gates ``["nonempty",
     "max_tokens:50000"]`` -- no ``units_min``. The T1 path
     (``v6/direct.build_single_node``) has always read the goal.
§R4  ``KUSUDAEMON_OUTPUT_SPINE`` defaulted to "0", so ``synthesize_output_spine``
     -- which would have built ten leaves of ten floors, each with its own
     count -- never ran.

The writer then did exactly as briefed: it wrote the "Opening section" (Floors
1-5) and handed off to downstream nodes that did not exist.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.roles.json_io import extract_last_json_object, validate
from kusudaemon.v1.provider import OpenAICompatibleProvider
from kusudaemon.v2.planner import build_tree, leaf_units_expected
from kusudaemon.v2.survey import SpineUnit
from kusudaemon.v6.tiering import ESTIMATE_SCHEMA, FULL_SCOPE_SCHEMA

# The goal verbatim from longgen_137-floor_armC_seed3/run.spec.json, trimmed to
# the parts the unit-count and delimiter extractors actually read.
SEED3_GOAL = (
    "Construct a skyscraper with 100 floors. Please follow the detailed floor "
    "assignments below:\n"
    "1) Specific floors requirements:\n  - Designate Floor 89 for small cinema use.\n"
    "4) Document each floor independently with detailed descriptions.\n"
    "5) Use '#*#' to separate the documentation for each floor (e.g. #*# Floor 1: \n"
    "This entry-level floor serves as the primary lobby.).\n"
    " Ensure that the document consists of 100 entries, each containing at least "
    "150 words. When the design of all 100 floors is complete, use '*** finished' "
    "to indicate the end of the document. *** started ***\n#*# Floor 1:"
)

# A classify response that stopped at the output ceiling mid-object. The model's
# actual answer is in there and says files_touched "1" / answerable true.
TRUNCATED_CLASSIFY = (
    'Let me size this up.\n\n'
    '{"files_touched": "1", "artifacts": 1, "answerable_without_exploration": true,\n'
    ' "work_kind": "document",\n'
    ' "questions": [\n'
    '   {"id": "q1", "text": "Which floors get restaurants?", "default_assumption": "50, 80"}\n'
    ' ],\n'
    ' "objections": ['
)


class TruncatedEstimateIsNotFabricated(unittest.TestCase):
    """§R1."""

    def test_truncated_response_does_not_validate_as_an_estimate(self) -> None:
        parsed, _ = extract_last_json_object(TRUNCATED_CLASSIFY, schema=FULL_SCOPE_SCHEMA)
        errors = ["parsed is None"] if parsed is None else validate(parsed, FULL_SCOPE_SCHEMA)
        self.assertTrue(
            errors,
            "a truncated classify response must not come back as a schema-valid "
            f"estimate; got {parsed!r}",
        )

    def test_truncated_response_does_not_report_unknown_files_touched(self) -> None:
        """The specific fabrication: 'unknown' is what forces T2."""
        parsed, _ = extract_last_json_object(TRUNCATED_CLASSIFY, schema=FULL_SCOPE_SCHEMA)
        if parsed is not None and not validate(parsed, FULL_SCOPE_SCHEMA):
            self.assertNotEqual(parsed.get("files_touched"), "unknown")

    def test_full_scope_schema_does_not_fall_through_to_the_intake_branch(self) -> None:
        """FULL_SCOPE_SCHEMA carries questions/objections as well as the estimate
        fields; declining the estimate repair must not hand the object to the
        INTAKE branch, which would return {"questions": [], "objections": []}."""
        parsed, _ = extract_last_json_object(TRUNCATED_CLASSIFY, schema=FULL_SCOPE_SCHEMA)
        if isinstance(parsed, dict):
            self.assertNotEqual(set(parsed), {"questions", "objections"})

    def test_complete_response_is_untouched(self) -> None:
        payload = {
            "files_touched": "1",
            "artifacts": 1,
            "answerable_without_exploration": True,
            "work_kind": "document",
            "questions": [],
            "objections": [],
        }
        parsed, _ = extract_last_json_object(json.dumps(payload), schema=FULL_SCOPE_SCHEMA)
        self.assertEqual(parsed["files_touched"], "1")
        self.assertTrue(parsed["answerable_without_exploration"])
        self.assertEqual(parsed["work_kind"], "document")

    def test_genuine_omissions_still_repair(self) -> None:
        """A real but partial estimate keeps the old default-filling behavior --
        the fix narrows what counts as an estimate, it does not disable repair."""
        parsed, _ = extract_last_json_object('{"artifacts": 2}', schema=FULL_SCOPE_SCHEMA)
        self.assertEqual(parsed["artifacts"], 2)
        self.assertEqual(parsed["files_touched"], "unknown")
        self.assertFalse(parsed["answerable_without_exploration"])
        self.assertFalse(validate(parsed, FULL_SCOPE_SCHEMA))

    def test_integer_files_touched_still_coerces(self) -> None:
        parsed, _ = extract_last_json_object(
            '{"files_touched": 3, "artifacts": 1, "answerable_without_exploration": true}',
            schema=ESTIMATE_SCHEMA,
        )
        self.assertEqual(parsed["files_touched"], "few")


class CeilingEscalationCoversPartialParses(unittest.TestCase):
    """§R2: the escalation must key on truncation, not on total parse failure."""

    GOOD = json.dumps(
        {
            "files_touched": "1",
            "artifacts": 1,
            "answerable_without_exploration": True,
            "work_kind": "document",
            "questions": [],
            "objections": [],
        }
    )

    def test_truncated_but_parseable_response_escalates_the_cap(self) -> None:
        """seed3's shape: one call, finish_reason "length", and enough JSON in
        the content for the scanner to latch onto. Before §R2 this returned on
        the first call with a fabricated estimate."""
        seen: list[int] = []

        def transport(url, payload, headers):
            seen.append(payload.get("max_tokens"))
            if len(seen) == 1:
                return {
                    "choices": [
                        {"finish_reason": "length", "message": {"content": TRUNCATED_CLASSIFY}}
                    ],
                    "usage": {"completion_tokens": payload["max_tokens"]},
                }
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": self.GOOD}}],
                "usage": {"completion_tokens": 300},
            }

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        out = provider.complete_json([{"role": "user", "content": "hi"}], FULL_SCOPE_SCHEMA)
        self.assertEqual(out["files_touched"], "1", "the model's real answer must survive")
        self.assertNotEqual(out["files_touched"], "unknown")
        self.assertEqual(len(seen), 2, "a truncated response must be retried")
        # 2026-09-26: the first retry resumes at the same ceiling with the
        # cut-off reasoning carried forward; only a second cut-off raises it.
        self.assertEqual(seen[1], seen[0])

    def test_an_untruncated_response_is_not_retried(self) -> None:
        seen: list[int] = []

        def transport(url, payload, headers):
            seen.append(payload.get("max_tokens"))
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": self.GOOD}}],
                "usage": {"completion_tokens": 300},
            }

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        out = provider.complete_json([{"role": "user", "content": "hi"}], FULL_SCOPE_SCHEMA)
        self.assertEqual(out["files_touched"], "1")
        self.assertEqual(len(seen), 1)


class GoalDerivedUnitGate(unittest.TestCase):
    """§R3: the T2 single-leaf path must reach the same gate as the T1 path."""

    OPENING_BRIEF = "Produce the artifact for Opening section (single unit, cannot split further)."

    def _seed3_spine(self) -> list[SpineUnit]:
        return [SpineUnit(id="unit-01", label="Opening section", start_chunk=0, end_chunk=0, tokens=431)]

    def test_without_a_goal_the_count_is_still_unavailable(self) -> None:
        self.assertEqual(leaf_units_expected(self.OPENING_BRIEF, self._seed3_spine()), (None, None))

    def test_goal_supplies_the_count_for_a_whole_spine_leaf(self) -> None:
        self.assertEqual(
            leaf_units_expected(self.OPENING_BRIEF, self._seed3_spine(), goal=SEED3_GOAL),
            (100, "declared"),
        )

    def test_spine_counts_outrank_the_goal(self) -> None:
        """A partitioned plan must never inherit the whole document's count."""
        unit = SpineUnit(
            id="unit-01",
            label="Floors 1 to 25",
            start_chunk=0,
            end_chunk=0,
            tokens=5000,
            units_expected=25,
            unit_delimiter="#*#",
        )
        self.assertEqual(
            leaf_units_expected("Floors 1 to 25", [unit], goal=SEED3_GOAL), (25, "declared")
        )

    def test_build_tree_attaches_the_gate_on_the_seed3_spine(self) -> None:
        tree = build_tree(self._seed3_spine(), object(), goal=SEED3_GOAL)
        nodes = tree.nodes if isinstance(tree.nodes, list) else list(tree.nodes.values())
        self.assertEqual(len(nodes), 1)
        self.assertIn("units_min:100@#*#", nodes[0].gates)
        self.assertEqual(nodes[0].budget.units_expected, 100)
        self.assertEqual(nodes[0].budget.unit_delimiter, "#*#")

    def test_build_tree_without_a_goal_is_unchanged(self) -> None:
        tree = build_tree(self._seed3_spine(), object())
        nodes = tree.nodes if isinstance(tree.nodes, list) else list(tree.nodes.values())
        self.assertFalse([g for g in nodes[0].gates if g.startswith("units_min")])

    def test_a_partitioned_plan_gets_per_leaf_counts_not_the_goal_count(self) -> None:
        units = [
            SpineUnit(
                id=f"unit-{i:02d}",
                label=f"Floors {i * 25 - 24} to {i * 25}",
                start_chunk=i - 1,
                end_chunk=i - 1,
                tokens=5000,
                units_expected=25,
                unit_delimiter="#*#",
            )
            for i in range(1, 5)
        ]
        tree = build_tree(units, object(), goal=SEED3_GOAL)
        nodes = tree.nodes if isinstance(tree.nodes, list) else list(tree.nodes.values())
        self.assertEqual(len(nodes), 4)
        for node in nodes:
            self.assertIn("units_min:25@#*#", node.gates)
            self.assertNotIn("units_min:100@#*#", node.gates)


class OutputSpineDefaultsOn(unittest.TestCase):
    """§R4."""

    def test_driver_defaults_the_flag_to_on(self) -> None:
        source = (_REPO_ROOT / "src" / "kusudaemon" / "pipeline" / "driver.py").read_text()
        self.assertIn('os.getenv("KUSUDAEMON_OUTPUT_SPINE", "1") == "1"', source)
        self.assertNotIn('os.getenv("KUSUDAEMON_OUTPUT_SPINE", "0")', source)

    def test_synthesized_spine_carries_a_count_per_unit(self) -> None:
        from kusudaemon.v2.survey import synthesize_output_spine

        _chunks, units = synthesize_output_spine(SEED3_GOAL)
        self.assertGreater(len(units), 1, "100 floors must not collapse to one leaf")
        self.assertEqual(sum(u.units_expected for u in units), 100)
        for unit in units:
            self.assertEqual(unit.unit_delimiter, "#*#")


if __name__ == "__main__":
    unittest.main()
