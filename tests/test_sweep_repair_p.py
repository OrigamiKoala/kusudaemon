"""PLAN-SWEEP-REPAIR.md §P — the three defects the 2026-09-12 000-week sweep exposed.

All three share a shape: the document the writers produced was correct and
complete, and something downstream of it reported otherwise.

§P1  `harvest_artifact` assembled the right text and then declined to write it,
     because a halted run had already left one unit file at that path.
§P2  `count_units` under-counted a writer that ran its units together on one
     line, failing a gate against a correct artifact.
§P3  A response truncated at the output ceiling was reported as invalid JSON,
     halting a finished run.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from kusudaemon.v1.gates import _gate_units_min, count_units, first_unit_key
from kusudaemon.v1.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    _first_choice_finish_reason,
    _hit_token_ceiling,
)
from kusudaemon.v1.reviewer import _sections_by_delimiter

import tempfile

from run_longgen_bench import constraint_count, harvest_artifact, select_tasks

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


def _glued(n: int) -> str:
    """n units with no newline anywhere and no space before the delimiter —
    the exact shape of 000-week armC seed3's out/unit-01.md."""
    return "".join(f"#*# Week {i} (a - b): prose for week {i} ends here" for i in range(1, n + 1))


def _well_formed(n: int) -> str:
    return "\n\n".join(f"#*# Week {i} (a - b):\nprose for week {i}" for i in range(1, n + 1))


class TestP1HarvestOverwrites(unittest.TestCase):
    def _run_dir(self, root: Path, name: str) -> Path:
        rdir = root / "runs" / name
        (rdir / "out").mkdir(parents=True)
        return rdir

    def test_assembled_text_overwrites_a_stale_single_unit_file(self) -> None:
        """The regression itself: raw/<stem>.md already holds one unit file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rdir = self._run_dir(root, "longgen_000-week_armC_seed1")
            (rdir / "out" / "unit-01.md").write_text(_well_formed(20), encoding="utf-8")
            (rdir / "out" / "unit-02.md").write_text(_well_formed(20), encoding="utf-8")
            (rdir / "out" / "unit-03.md").write_text(_well_formed(12), encoding="utf-8")

            raw = root / "raw" / "000-week_armC_seed1.md"
            raw.parent.mkdir(parents=True)
            # What the halt path left behind: the last unit only.
            raw.write_text(_well_formed(12), encoding="utf-8")
            stale = raw.read_text(encoding="utf-8")

            text = harvest_artifact(
                "C", raw, {"artifact_path": str(raw)},
                runs_root=root / "runs", task_id="000-week", seed=1,
            )

            self.assertEqual(count_units(text, "#*#"), 52)
            self.assertNotEqual(raw.read_text(encoding="utf-8"), stale)
            self.assertEqual(
                count_units(raw.read_text(encoding="utf-8"), "#*#"), 52,
                "the file write_predictions re-reads must hold the assembled document",
            )

    def test_unit_files_are_concatenated_in_ordinal_not_lexical_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rdir = self._run_dir(root, "longgen_x_armC_seed1")
            for i, label in ((1, "A"), (2, "B"), (10, "C")):
                (rdir / "out" / f"unit-{i:02d}.md").write_text(f"#*# Week {i}: {label}", encoding="utf-8")
            raw = root / "raw" / "x.md"
            raw.parent.mkdir(parents=True)
            text = harvest_artifact("C", raw, {}, runs_root=root / "runs", task_id="x", seed=1)
            self.assertLess(text.index("A"), text.index("B"))
            self.assertLess(text.index("B"), text.index("C"), "unit-10 must not sort before unit-02")


class TestP2UnitCounting(unittest.TestCase):
    def test_glued_one_liner_counts_every_unit(self) -> None:
        text = _glued(20)
        self.assertEqual(text.count("\n"), 0)
        self.assertEqual(count_units(text, "#*#"), 20)

    def test_the_gate_that_escalated_seed3_now_passes(self) -> None:
        result = _gate_units_min("units_min", "20@#*#", _glued(20))
        self.assertTrue(result.passed, result.detail)

    def test_well_formed_artifact_is_unchanged(self) -> None:
        self.assertEqual(count_units(_well_formed(52), "#*#"), 52)

    def test_partial_artifact_still_fails_its_gate(self) -> None:
        """The fix must not turn under-production into a pass."""
        result = _gate_units_min("units_min", "20@#*#", _glued(7))
        self.assertFalse(result.passed)
        self.assertIn("units_found:7", result.detail)

    def test_a_quoted_mention_is_not_a_unit(self) -> None:
        prose = "Use `#*#` as the separator at the start of each entry."
        self.assertEqual(count_units(prose, "#*#"), 0)

    def test_word_delimiters_stay_line_anchored(self) -> None:
        text = "Chapter 1\nsee the Chapter above for context\nChapter 2\n"
        self.assertEqual(count_units(text, "Chapter"), 2)

    def test_first_unit_key_reads_the_glued_ordinal(self) -> None:
        self.assertEqual(first_unit_key(_glued(20), "#*#"), "1")

    def test_reviewer_fans_out_a_glued_artifact(self) -> None:
        sections = _sections_by_delimiter(_glued(20), "#*#")
        self.assertEqual(len(sections), 20, "fan-out must see units, not one blob")


class TestP3TokenCeiling(unittest.TestCase):
    def test_finish_reason_length_is_detected(self) -> None:
        raw = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        self.assertEqual(_first_choice_finish_reason(raw), "length")
        self.assertTrue(_hit_token_ceiling(raw, 4096))

    def test_usage_at_the_cap_is_detected_without_finish_reason(self) -> None:
        """The seed1 shape: the host reported no finish_reason at all."""
        raw = {"choices": [{"message": {"content": ""}}], "usage": {"completion_tokens": 4096}}
        self.assertTrue(_hit_token_ceiling(raw, 4096))
        self.assertFalse(_hit_token_ceiling(raw, 8192))

    def test_a_truncated_response_is_retried_at_a_larger_cap(self) -> None:
        seen: list[int] = []

        def transport(url, payload, headers):
            seen.append(payload.get("max_tokens"))
            if len(seen) == 1:
                return {
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                    "usage": {"completion_tokens": payload["max_tokens"]},
                }
            return {"choices": [{"finish_reason": "stop",
                                 "message": {"content": json.dumps({"verdict": "ok"})}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        out = provider.complete_json([{"role": "user", "content": "hi"}], _SCHEMA)
        self.assertEqual(out, {"verdict": "ok"})
        self.assertEqual(len(seen), 2)
        self.assertGreater(seen[1], seen[0], "the retry must raise the ceiling")

    def test_the_retry_does_not_grow_the_prompt(self) -> None:
        """Seed1 re-asked at the same cap and carried the failure forward:
        1312 -> 5448 -> 9585 prompt tokens for three identical questions."""
        lengths: list[int] = []

        def transport(url, payload, headers):
            lengths.append(len(payload["messages"]))
            if len(lengths) < 3:
                return {
                    "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                    "usage": {"completion_tokens": payload["max_tokens"]},
                }
            return {"choices": [{"message": {"content": json.dumps({"verdict": "ok"})}}]}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        provider.complete_json([{"role": "user", "content": "hi"}], _SCHEMA)
        self.assertEqual(len(set(lengths)), 1, f"message count grew across retries: {lengths}")

    def test_exhausting_the_ladder_names_the_ceiling_not_the_json(self) -> None:
        def transport(url, payload, headers):
            return {
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"completion_tokens": payload["max_tokens"]},
            }

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], _SCHEMA)
        message = str(ctx.exception)
        self.assertIn("output ceiling", message)
        self.assertNotIn("Expecting value", message)

    def test_genuinely_invalid_json_still_reports_as_invalid_json(self) -> None:
        def transport(url, payload, headers):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}],
                    "usage": {"completion_tokens": 3}}

        provider = OpenAICompatibleProvider(transport=transport, api_key="unused", model="m")
        with self.assertRaises(ProviderError) as ctx:
            provider.complete_json([{"role": "user", "content": "hi"}], _SCHEMA)
        self.assertNotIn("output ceiling", str(ctx.exception))


def _dataset() -> list[dict]:
    """Four types x 20 instances, constraint counts 1..20 per type."""
    data = []
    for type_name in ("Week", "Floor", "Menu Week", "Block"):
        for k in range(1, 21):
            data.append({
                "type": type_name,
                "number": 52,
                "checks_once": {str(i): "x" for i in range(k)},
                "checks_range": {},
                "checks_periodic": {},
            })
    return data


class TestPerTypeSelection(unittest.TestCase):
    def _select(self, **kw):
        return select_tasks(
            _dataset(), limit=None, indices=None, types=None, stratify=True, **kw
        )

    def test_five_of_every_type(self) -> None:
        picked = self._select(per_type=5)
        counts: dict[str, int] = {}
        for _, item in picked:
            counts[item["type"]] = counts.get(item["type"], 0) + 1
        self.assertEqual(counts, {"Week": 5, "Floor": 5, "Menu Week": 5, "Block": 5})

    def test_picks_span_the_difficulty_range(self) -> None:
        picked = self._select(per_type=5)
        weeks = sorted(constraint_count(i) for x, i in picked if i["type"] == "Week")
        self.assertEqual(weeks, [1, 6, 11, 15, 20], "min/Q1/median/Q3/max over 1..20")

    def test_selection_is_deterministic(self) -> None:
        self.assertEqual(
            [i for i, _ in self._select(per_type=5)],
            [i for i, _ in self._select(per_type=5)],
        )

    def test_pinned_indices_are_kept_and_count_against_the_quota(self) -> None:
        picked = self._select(per_type=5, pin=[3])
        indices = [i for i, _ in picked]
        self.assertIn(3, indices, "a pinned index must survive its rank")
        weeks = [i for i, item in picked if item["type"] == "Week"]
        self.assertEqual(len(weeks), 5, "pinning must not add a sixth pick")

    def test_order_round_robins_so_a_partial_sweep_stays_stratified(self) -> None:
        picked = self._select(per_type=5)
        first_four = {item["type"] for _, item in picked[:4]}
        self.assertEqual(len(first_four), 4)

    def test_exclude_still_applies(self) -> None:
        picked = self._select(per_type=5)
        victim = picked[0][0]
        after = self._select(per_type=5, exclude_tasks=[str(victim)])
        self.assertNotIn(victim, [i for i, _ in after])

    def test_explicit_tasks_override_per_type(self) -> None:
        picked = select_tasks(
            _dataset(), limit=None, indices=[0, 25], types=None,
            stratify=True, per_type=5,
        )
        self.assertEqual([i for i, _ in picked], [0, 25])


if __name__ == "__main__":
    unittest.main()
