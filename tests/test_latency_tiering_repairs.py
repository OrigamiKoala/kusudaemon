"""Hermetic pins for the 2026-09-18 wall-clock repairs (no provider calls).

The 09-18 LongGenBench wave lost 5 of 9 arm-C attempts to `timeout after
5400s`, not to wrong answers -- every run that finished scored 100%. Four
separate mechanisms were spending the budget:

  1. The tier table had an input-volume axis and no output-volume one, so
     "write 100 floors into one file" classified T1 and bet the whole run on
     a single writer episode. Losing that bet cost ~4000s before the harness
     escalated to the decomposition that works.
  2. A deadline miss escalated the output cap, and `deadline_s` is derived
     FROM the cap -- so each retry had a longer deadline than the last.
  3. Triage's 8192 cap made a pre-filter that fails take four times as long
     to fail.
  4. The 1800s soft-timeout extension let a stalled episode outlive its
     budget by 30 minutes.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderDeadlineError,
)
from kusudaemon.v2.survey import (  # noqa: E402
    declared_tokens_per_unit,
    synthesize_output_spine,
    units_per_leaf_capacity,
)
from kusudaemon.v6.tiering import (  # noqa: E402
    ScopeEstimate,
    Signals,
    classify,
    output_capacity,
)

SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["go", "stop"]}},
    "required": ["action"],
    "additionalProperties": False,
}


def _http_provider(transport=None, stream_transport=None, **kwargs):
    kwargs.setdefault("api_key", "unused")
    kwargs.setdefault("base_retry_delay", 0.0)
    return OpenAICompatibleProvider(
        transport=transport,
        stream_transport=stream_transport,
        sleep=lambda _s: None,
        **kwargs,
    )


def _single_file_signals() -> Signals:
    """The signals every LongGenBench goal produces, copied from the observed
    `tier.json` of longgen_142-floor_armC_seed1: one small prompt file, and
    breadth markers ("each floor...") that already keep it off the T0 row."""
    return Signals(
        breadth_markers=6,
        goal_tokens=436,
        named_paths=[],
        output_markers=1,
        output_targets=2,
        work_files=1,
        work_tokens=436,
    )


def _one_artifact_estimate(**over) -> ScopeEstimate:
    """The estimate that used to route these runs to T1: the model correctly
    reports one file and one artifact, because that IS the output's shape."""
    fields = dict(
        files_touched="1",
        artifacts=1,
        answerable_without_exploration=True,
        ambiguities=[],
        objections=[],
        work_kind="document",
    )
    fields.update(over)
    return ScopeEstimate(**fields)


@patch.dict(os.environ, {"KUSUDAEMON_TIER_OUTPUT_SIGNALS": "1"})
class TestOutputVolumeAxis(unittest.TestCase):
    """The tier table's missing axis: output VOLUME, not output SHAPE.
    Opt-in since 2026-09-25, so these tests pin the flag on."""

    def test_one_file_of_many_units_is_not_a_single_node_run(self) -> None:
        """The 142-floor shape: `files_touched="1"`, `artifacts=1`, and 100
        declared units. Shape says single node; volume says otherwise."""
        goal = "Write 100 floors, each about 150 words, into one document."
        self.assertEqual(
            classify(_single_file_signals(), _one_artifact_estimate(), goal=goal),
            "T2",
        )

    def test_output_that_fits_one_leaf_still_classifies_low(self) -> None:
        """The axis is a capacity question, not a unit-count threshold. Eight
        short sections genuinely fit one leaf and must not be decomposed --
        a `declared >= 8` floor would have wrongly caught this."""
        goal = "Write a document with 8 sections, each about 200 words."
        declared, per_leaf = output_capacity(goal)
        self.assertEqual(declared, 8)
        self.assertGreaterEqual(per_leaf, 8)
        self.assertIn(
            classify(_single_file_signals(), _one_artifact_estimate(), goal=goal),
            ("T0", "T1"),
        )

    def test_few_but_enormous_units_do_not_fit_one_leaf(self) -> None:
        """Ten units is a small count, but at 2000 words each the volume
        exceeds a leaf -- which a count threshold could never see."""
        goal = "Write a document with 10 sections, each about 2000 words."
        declared, per_leaf = output_capacity(goal)
        self.assertEqual(declared, 10)
        self.assertLess(per_leaf, 10)
        self.assertEqual(
            classify(_single_file_signals(), _one_artifact_estimate(), goal=goal),
            "T2",
        )

    def test_goal_without_declared_units_is_untouched(self) -> None:
        """No declared unit count -> the axis abstains and the table answers
        exactly as it did before this existed."""
        goal = "Fix the typo in the README."
        self.assertEqual(output_capacity(goal), (None, None))
        quiet = Signals(
            breadth_markers=0, goal_tokens=40, named_paths=[], output_markers=0,
            output_targets=0, work_files=1, work_tokens=40,
        )
        self.assertEqual(classify(quiet, _one_artifact_estimate(), goal=goal), "T0")

    def test_axis_never_lowers_a_tier(self) -> None:
        """Monotone only: a goal the table already put at T3 stays there."""
        wide = Signals(
            breadth_markers=4, goal_tokens=900, named_paths=[], output_markers=2,
            output_targets=2, work_files=400, work_tokens=900_000,
        )
        goal = "Write 100 floors, each about 150 words."
        self.assertEqual(
            classify(wide, _one_artifact_estimate(artifacts=50), goal=goal), "T3"
        )

    def test_kill_switch_restores_the_old_answer(self) -> None:
        goal = "Write 100 floors, each about 150 words, into one document."
        with patch.dict(os.environ, {"KUSUDAEMON_TIER_OUTPUT_SIGNALS": "0"}):
            self.assertEqual(
                classify(_single_file_signals(), _one_artifact_estimate(), goal=goal),
                "T1",
            )

    def test_classify_emits_the_arithmetic_it_used(self) -> None:
        events: list[dict] = []
        goal = "Write 100 floors, each about 150 words, into one document."
        classify(
            _single_file_signals(),
            _one_artifact_estimate(),
            goal=goal,
            on_event=events.append,
        )
        [ev] = [e for e in events if e["type"] == "tier_output_capacity"]
        self.assertEqual(ev["declared_units"], 100)
        self.assertEqual(ev["tier_without_axis"], "T1")
        self.assertEqual(ev["tier"], "T2")
        self.assertEqual(
            ev["leaves_implied"],
            -(-ev["declared_units"] // ev["units_per_leaf"]),
        )


class TestCapacitySharedWithTiler(unittest.TestCase):
    """classify and the tiler must not hold two copies of this arithmetic: if
    classify says "needs more than one leaf", the spine it routes to has to
    actually produce more than one leaf."""

    GOALS = (
        "Write 100 floors, each about 150 words. Use '#*#' to separate each floor.",
        "Write 52 weeks, each about 200 words. Use '#*#' to separate each week.",
        "Write a document with 10 sections, each about 2000 words.",
    )

    def test_leaves_implied_matches_leaves_synthesized(self) -> None:
        for goal in self.GOALS:
            with self.subTest(goal=goal[:40]):
                declared, per_leaf = output_capacity(goal)
                _chunks, units = synthesize_output_spine(goal)
                self.assertEqual(len(units), -(-declared // per_leaf))
                self.assertGreater(len(units), 1)

    def test_unit_size_is_read_from_the_goal_not_assumed(self) -> None:
        self.assertGreater(
            declared_tokens_per_unit("Write sections of 2000 words each."),
            declared_tokens_per_unit("Write sections of 100 words each."),
        )
        self.assertEqual(declared_tokens_per_unit("Write some sections."), 200)

    def test_capacity_is_clamped_to_a_completable_leaf(self) -> None:
        """However tiny the units, a leaf stops being reliably completable in
        one episode past 25 of them."""
        self.assertLessEqual(units_per_leaf_capacity("Write items of 2 words each."), 25)
        self.assertGreaterEqual(
            units_per_leaf_capacity("Write items of 9000 words each."), 1
        )


class TestDeadlineDoesNotEscalateTheCap(unittest.TestCase):
    """A deadline means the model spent the clock deliberating, not that it
    ran out of room. Escalating the cap hands the next attempt a LONGER
    deadline (`deadline_s = max(300, cap / min_tps)`) -- the ladder that made
    one bounded classify call take 1751s."""

    def test_non_verdict_deadline_retries_at_same_cap_with_emit(self) -> None:
        payloads: list[dict] = []
        calls = {"n": 0}

        def stream_transport(url, payload, headers, on_reasoning, deadline_s=None):
            payloads.append(
                {
                    "max_tokens": payload.get("max_tokens"),
                    "messages": list(payload["messages"]),
                }
            )
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProviderDeadlineError("SSE stream wall-clock deadline exceeded")
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        provider = _http_provider(stream_transport=stream_transport, role="orchestrator")
        result = provider.complete_json(
            [{"role": "user", "content": "classify this"}], SCHEMA, streaming=True
        )

        self.assertEqual(result.get("action"), "go")
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["max_tokens"], payloads[1]["max_tokens"])
        self.assertIn("Emit the JSON now", payloads[1]["messages"][-1]["content"])

    def test_repeated_deadlines_stop_instead_of_laddering(self) -> None:
        """Budget is one retry, not four doublings: a persistently slow role
        raises after two attempts rather than spending 300+300+546+1092+2185s."""
        calls = {"n": 0}

        def stream_transport(url, payload, headers, on_reasoning, deadline_s=None):
            calls["n"] += 1
            raise ProviderDeadlineError("SSE stream wall-clock deadline exceeded")

        provider = _http_provider(stream_transport=stream_transport, role="orchestrator")
        with self.assertRaises(ProviderDeadlineError):
            provider.complete_json(
                [{"role": "user", "content": "classify"}], SCHEMA, streaming=True
            )
        self.assertEqual(calls["n"], 2)

    def test_truncation_still_escalates_the_cap(self) -> None:
        """Running out of room still earns a larger cap -- but since
        2026-09-26 only on the second cut-off; the first resumes at the same
        cap with the partial reasoning carried forward."""
        payloads: list[dict] = []
        calls = {"n": 0}

        def transport(url, payload, headers):
            payloads.append({"max_tokens": payload.get("max_tokens")})
            calls["n"] += 1
            if calls["n"] <= 2:
                return {
                    "choices": [
                        {"message": {"content": '{"action": "g'}, "finish_reason": "length"}
                    ]
                }
            return {"choices": [{"message": {"content": '{"action": "go"}'}}]}

        provider = _http_provider(transport=transport, role="planner")
        result = provider.complete_json(
            [{"role": "user", "content": "plan"}], SCHEMA
        )
        self.assertEqual(result.get("action"), "go")
        self.assertEqual(payloads[1]["max_tokens"], payloads[0]["max_tokens"])
        self.assertGreater(payloads[2]["max_tokens"], payloads[1]["max_tokens"])


class TestBudgetDefaults(unittest.TestCase):
    """Defaults that decide how much of a run's wall clock a single stuck step
    may consume. Pinned because each regressed silently once already."""

    def test_triage_cap_is_small(self) -> None:
        src = (_REPO_ROOT / "src/kusudaemon/v1/reviewer.py").read_text()
        self.assertIn('_env_int("KUSUDAEMON_TRIAGE_MAX_TOKENS", 2048)', src)

    def test_soft_timeout_extension_is_a_grace_not_a_second_budget(self) -> None:
        src = (_REPO_ROOT / "src/kusudaemon/environment/local.py").read_text()
        self.assertIn('"KUSUDAEMON_SOFT_TIMEOUT_EXTENSION", "300"', src)


if __name__ == "__main__":
    unittest.main()
