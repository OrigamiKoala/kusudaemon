"""Regressions from the 2026-09-16 LongGenBench rerun (commit f8f5b31).

- read loop crashed on every turn: ``validate`` hashed a union ``type`` list;
- intake assumptions counted as contract rules, disabling the §F5 vacuous pass;
- triage used the 2048-token small-schema cap and always truncated (raised to
  8192 here, then reverted 2026-09-18 -- see ``TriageCapTest``);
- a timeout (or an accidental-loss restore) over a complete artifact was
  redispatched instead of reviewed;
- ``run_longgen_bench`` resumed stale run dirs instead of starting fresh.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.gates import evaluate_gates  # noqa: E402
from kusudaemon.v1.json_schema import validate  # noqa: E402
from kusudaemon.v1.provider import _scoped_max_tokens  # noqa: E402
from kusudaemon.v1.review_loop import READ_LOOP_ACTION_SCHEMA, run_review_read_loop  # noqa: E402
from kusudaemon.v1.reviewer import (  # noqa: E402
    _call_triage,
    contract_declares_rules,
    ungroundable_judgments,
)
from kusudaemon.v1.round_loop import _transition_after_writer  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402

DELIM = "#*#"
GATES = ["nonempty", f"units_min:3@{DELIM}"]

# The contract 323-block seed 1 actually ran under.
CONTRACT_323 = """# Contract

## Global rubric
(none)

## Assumptions
- Are block coordinates (column, row)? -- assumed: Assume (column, row).
- Bus station direction? -- assumed: Assume downward progression.
"""


def _units(lo: int, hi: int) -> str:
    return "".join(
        f"{DELIM} Block {i} (1, 1):\n" + "word " * 40 + "\n" for i in range(lo, hi + 1)
    )


class UnionTypeValidationTest(unittest.TestCase):
    def test_union_types_validate(self) -> None:
        self.assertEqual(validate({"action": "read", "unit": 3, "verdict": None}, READ_LOOP_ACTION_SCHEMA), [])
        self.assertEqual(validate({"action": "read", "unit": None, "units": [1, 4]}, READ_LOOP_ACTION_SCHEMA), [])

    def test_union_types_still_reject(self) -> None:
        self.assertTrue(validate({"action": "read", "unit": "three"}, READ_LOOP_ACTION_SCHEMA))
        self.assertTrue(validate({"action": "read", "unit": True}, READ_LOOP_ACTION_SCHEMA))
        self.assertTrue(validate(5, {"type": "null"}))


class _ValidatingProvider:
    """Validates every response against the schema, as the real provider does."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.caps: list[int | None] = []

    def complete_json(self, messages, schema, **kwargs):  # noqa: ANN001
        self.caps.append(_scoped_max_tokens())
        out = self.responses.pop(0)
        errors = validate(out, schema)
        if errors:
            raise AssertionError(errors)
        return out


class ReadLoopTest(unittest.TestCase):
    def test_read_loop_completes_with_union_fields(self) -> None:
        node = TaskNode(id="u", brief="b", artifact="out/u.md", gates=list(GATES))
        provider = _ValidatingProvider(
            [
                {"action": "read", "units": [1, 3], "unit": None},
                {"action": "verdict", "verdict": "pass", "unit": None, "items": []},
            ]
        )
        verdict = run_review_read_loop(
            node,
            _units(1, 3),
            provider,
            effective_judgment=["on_topic"],
            rubric_lines="on_topic: stays on topic",
            unit_delimiter=DELIM,
        )
        self.assertEqual(verdict.verdict, "pass", verdict.skip_reason)
        self.assertTrue(all(c and c >= 8192 for c in provider.caps))


class VacuousPassContractTest(unittest.TestCase):
    def test_assumptions_are_not_rules(self) -> None:
        self.assertFalse(contract_declares_rules(CONTRACT_323))
        self.assertEqual(
            ungroundable_judgments(["on_topic", "claims_supported"], CONTRACT_323, "Spine unit: {}"),
            ["claims_supported"],
        )

    def test_placeholder_rubric_is_not_a_rule(self) -> None:
        text = "# Contract\n\n## Rubric\n(no global rubric was elicited; gates are the contract.)\n"
        self.assertFalse(contract_declares_rules(text))

    def test_real_rubric_line_still_counts(self) -> None:
        text = CONTRACT_323.replace("(none)", "- Every block cites its source.")
        self.assertTrue(contract_declares_rules(text))
        self.assertEqual(ungroundable_judgments(["claims_supported"], text, ""), [])


class TriageCapTest(unittest.TestCase):
    def test_triage_uses_small_cap(self) -> None:
        """Superseded 2026-09-18. This test used to pin `>= 8192`, on the
        theory that triage was failing because the 2048 default was being
        spent on reasoning before the JSON came out. The 09-17/18 wave
        measured the outcome: it still ran out at 8192 ("hit output ceiling
        (8192 tokens, the maximum) before JSON was complete" after 3
        attempts, longgen_343-block_armC_seed1), took the emit-now retry,
        ran out again and fell through to full review -- so the larger cap
        bought no triage verdicts and roughly quadrupled the cost of not
        getting one. Triage is a pre-filter answering a small fixed schema
        from an outline and a gate summary; when it cannot, it must say so
        cheaply."""
        provider = _ValidatingProvider([{"suspect": False, "reason": "fine"}])
        try:
            _call_triage("unit 1", "", 10, "on_topic: x", provider)
        except AssertionError:
            pass  # schema shape is not under test here; the cap is
        self.assertLessEqual(provider.caps[0], 2048)


class TimeoutSalvageTest(unittest.TestCase):
    def _run(self, text: str, *, status: str, snapshot: str | None = None) -> tuple[TaskNode, list[dict]]:
        td = tempfile.mkdtemp()
        run_dir = Path(td)
        (run_dir / "out").mkdir()
        if snapshot is not None:
            vdir = run_dir / "out" / ".versions" / "u"
            vdir.mkdir(parents=True)
            (vdir / "attempt_1.md").write_text(snapshot)
        (run_dir / "out" / "u.md").write_text(text)
        node = TaskNode(
            id="u", brief="b", artifact="out/u.md", gates=list(GATES),
            status="dispatched", budget=NodeBudget(units_expected=3),
        )
        tree = TaskTree(nodes={node.id: node})
        tree.save(run_dir / "tree.json")
        log = EventLog(run_dir / "events.jsonl")
        asyncio.run(
            _transition_after_writer(
                node, tree, run_dir / "tree.json",
                episode_ok=(status == "done"),
                gate_results=evaluate_gates(node.gates, text),
                max_attempts=3, log=log, run_dir=run_dir, result_status=status,
            )
        )
        return node, log.read_all()

    def test_timeout_with_complete_artifact_goes_to_review(self) -> None:
        node, events = self._run(_units(1, 3), status="timeout")
        self.assertEqual(node.status, "awaiting_review")
        self.assertEqual(node.attempts, 0)
        self.assertIn("node_timeout_salvaged", [e["type"] for e in events])

    def test_timeout_with_partial_artifact_still_retries(self) -> None:
        node, _ = self._run(_units(1, 2), status="timeout")
        self.assertEqual(node.status, "pending")
        self.assertEqual(node.attempts, 1)

    def test_restored_complete_artifact_goes_to_review(self) -> None:
        node, events = self._run("oops", status="done", snapshot=_units(1, 3))
        types = [e["type"] for e in events]
        self.assertIn("node_attempt_regressed", types)
        self.assertIn("node_restored_complete", types)
        self.assertEqual(node.status, "awaiting_review")

    def test_restored_partial_artifact_is_redispatched(self) -> None:
        node, _ = self._run("oops", status="done", snapshot=_units(1, 2))
        self.assertEqual(node.status, "pending")
        self.assertEqual(node.attempts, 0)


class StaleCellArchiveTest(unittest.TestCase):
    def test_run_one_archives_previous_attempt(self) -> None:
        import run_longgen_bench as rlb

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runs = root / "runs"
            stale = runs / "longgen_t_armC_seed1"
            stale.mkdir(parents=True)
            (stale / "events.jsonl").write_text("{}\n")
            results = root / "results"
            (results / "raw").mkdir(parents=True)
            (results / "raw" / "t_armC_seed1.md").write_text("old artifact")

            moved = rlb.archive_stale_cell(
                run_dir=stale,
                artifact_path=results / "raw" / "t_armC_seed1.md",
                record_path=results / "bench" / "t_armC_seed1.json",
                ws_dir=None,
                archive_dir=results / "archive",
            )
            self.assertEqual(len(moved), 2)
            self.assertFalse(stale.exists())
            self.assertFalse((results / "raw" / "t_armC_seed1.md").exists())
            archived = list(runs.glob("_archived_*/longgen_t_armC_seed1"))
            self.assertEqual(len(archived), 1)

    def test_harvest_ignores_parked_sibling(self) -> None:
        import run_longgen_bench as rlb

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            live = root / "longgen_t_armC_seed1" / "out"
            parked = root / "longgen_t_armC_seed1.pre-fix" / "out"
            live.mkdir(parents=True)
            parked.mkdir(parents=True)
            (live / "unit-01.md").write_text("LIVE")
            (parked / "unit-01.md").write_text("PARKED")
            text = rlb.harvest_artifact(
                "C", root / "missing.md", {}, runs_root=root,
                task_id="t", seed=1, run_id="longgen_t_armC_seed1",
            )
            self.assertIn("LIVE", text)
            self.assertNotIn("PARKED", text)


if __name__ == "__main__":
    unittest.main()
