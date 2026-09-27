"""2026-09-27: same-session nudge for a writer that ends its turn early, and
unit-progress credit on the single-writer path.

294-menu-week s1's T1 writer wrote one week and ended its turn; the next
attempt announced "Let me write the complete file now" and ended its turn
with no tool call. Both were charged, the retry after a resumed episode even
started a fresh session (the newest session_captured had no id), and the
node escalated T1 -> T2 with one week on disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, ensure_leaf_unit_stubs, events_path, node_artifact_text  # noqa: E402
from kusudaemon.v0.runner import (  # noqa: E402
    _completion_consumed,
    _last_captured_session,
    _pending_nudge,
    run_node,
)
from kusudaemon.v1.gates import evaluate_gates  # noqa: E402
from kusudaemon.v1.round_loop import _transition_after_writer, _writer_stall_nudge  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402
from kusudaemon.v6.direct import SINGLE_NODE_ID  # noqa: E402


def _single(units: int = 5) -> TaskNode:
    return TaskNode(
        id=SINGLE_NODE_ID,
        brief="write the weeks",
        artifact=f"out/{SINGLE_NODE_ID}.md",
        gates=["nonempty", f"units_min:{units}@#*#"],
        budget=NodeBudget(tokens=50_000, calls=10, units_expected=units, unit_delimiter="#*#"),
    )


def _weeks(n: int) -> str:
    return "".join(f"#*# Week {i}\nmenu {i}\n" for i in range(1, n + 1))


def _transition(run_dir: Path, node: TaskNode, *, status: str = "done", max_attempts: int = 2) -> list[dict]:
    log = EventLog(events_path(run_dir))
    text = node_artifact_text(run_dir, node.id)
    asyncio.run(
        _transition_after_writer(
            node,
            TaskTree(nodes={node.id: node}),
            run_dir / "tree.json",
            episode_ok=status == "done",
            gate_results=evaluate_gates(node.gates, text),
            max_attempts=max_attempts,
            log=log,
            run_dir=run_dir,
            result_status=status,
        )
    )
    return log.read_all()


def _snapshot(run_dir: Path, node_id: str, text: str, ts: int) -> None:
    v = run_dir / "out" / ".versions" / node_id
    v.mkdir(parents=True, exist_ok=True)
    (v / f"attempt_{ts}.md").write_text(text, encoding="utf-8")


class StallNudgeRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("KUSUDAEMON_WRITER_NUDGES", None)

    def tearDown(self) -> None:
        os.environ.pop("KUSUDAEMON_WRITER_NUDGES", None)

    def _rule(self, **kw):
        base = dict(
            prior_nudges=0, episode_ok=True, result_status="done", gates_passed=False,
            units_expected=52, prior_units=0, current_units=1,
        )
        base.update(kw)
        return _writer_stall_nudge(_single(), **base)

    def test_first_stall_is_nudged_even_without_progress(self) -> None:
        msg = self._rule(current_units=0)
        self.assertIsNotNone(msg)
        self.assertIn("0 of 52 units", msg)
        self.assertIn("from unit 1", msg)

    def test_later_nudge_needs_progress(self) -> None:
        self.assertIsNone(self._rule(prior_nudges=1, prior_units=3, current_units=3))
        self.assertIsNotNone(self._rule(prior_nudges=1, prior_units=3, current_units=4))

    def test_cap_and_kill_switch(self) -> None:
        self.assertIsNone(self._rule(prior_nudges=3, prior_units=3, current_units=9))
        os.environ["KUSUDAEMON_WRITER_NUDGES"] = "0"
        self.assertIsNone(self._rule())

    def test_only_a_voluntary_end_with_measurable_shortfall(self) -> None:
        self.assertIsNone(self._rule(result_status="timeout", episode_ok=False))
        self.assertIsNone(self._rule(result_status="transport", episode_ok=False))
        self.assertIsNone(self._rule(gates_passed=True))
        self.assertIsNone(self._rule(units_expected=None))
        self.assertIsNone(self._rule(current_units=52))


class TransitionTest(unittest.TestCase):
    def test_stall_is_nudged_without_charging_an_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            node = _single()
            (run_dir / "out" / f"{node.id}.md").write_text(_weeks(1), encoding="utf-8")
            events = _transition(run_dir, node)
            self.assertEqual((node.status, node.attempts, node.nudges), ("pending", 0, 1))
            nudged = [e for e in events if e["type"] == "node_nudged"]
            self.assertEqual(len(nudged), 1)
            self.assertEqual((nudged[0]["current_units"], nudged[0]["units_expected"]), (1, 5))
            self.assertIn("from unit 2", nudged[0]["message"])

    def test_nudge_without_progress_is_charged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            node = _single()
            node.nudges = 1
            (run_dir / "out" / f"{node.id}.md").write_text(_weeks(2), encoding="utf-8")
            _snapshot(run_dir, node.id, _weeks(2), 1)
            events = _transition(run_dir, node)
            self.assertEqual((node.attempts, node.nudges), (1, 0))
            self.assertFalse(any(e["type"] == "node_nudged" for e in events))

    def test_single_writer_progress_is_credited_once_nudges_run_out(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            node = _single()
            node.nudges = 3
            _snapshot(run_dir, node.id, _weeks(1), 1)
            (run_dir / "out" / f"{node.id}.md").write_text(_weeks(3), encoding="utf-8")
            events = _transition(run_dir, node)
            self.assertEqual((node.status, node.attempts, node.nudges), ("pending", 0, 0))
            progress = [e for e in events if e["type"] == "node_continuation_progress"]
            self.assertEqual((progress[0]["prior_units"], progress[0]["current_units"]), (1, 3))

    def test_single_writer_progress_credit_kill_switch(self) -> None:
        os.environ["KUSUDAEMON_SINGLE_PROGRESS_CREDIT"] = "0"
        try:
            with tempfile.TemporaryDirectory() as td:
                run_dir = create_run_dir(Path(td), "r")
                node = _single()
                _snapshot(run_dir, node.id, _weeks(1), 1)
                (run_dir / "out" / f"{node.id}.md").write_text(_weeks(3), encoding="utf-8")
                _transition(run_dir, node, status="timeout")
                self.assertEqual(node.attempts, 1)
        finally:
            os.environ.pop("KUSUDAEMON_SINGLE_PROGRESS_CREDIT", None)

    def test_single_writer_without_declared_units_is_not_credited(self) -> None:
        # Without a declared count a heading counts as a "unit"; that is not
        # progress (eval t2-misclassified: it delayed the size escalation).
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            node = TaskNode(
                id=SINGLE_NODE_ID, brief="b", artifact=f"out/{SINGLE_NODE_ID}.md",
                gates=["nonempty", "max_tokens:10"],
            )
            (run_dir / "out" / f"{node.id}.md").write_text("# Single\n\n" + "word " * 50, encoding="utf-8")
            events = _transition(run_dir, node)
            self.assertEqual(node.attempts, 1)
            self.assertFalse(any(e["type"] in ("node_nudged", "node_continuation_progress") for e in events))

    def test_timeout_with_progress_is_credited_not_nudged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            node = _single()
            (run_dir / "out" / f"{node.id}.md").write_text(_weeks(2), encoding="utf-8")
            events = _transition(run_dir, node, status="timeout")
            self.assertEqual((node.attempts, node.nudges), (0, 0))
            self.assertTrue(any(e["type"] == "node_continuation_progress" for e in events))
            self.assertFalse(any(e["type"] == "node_nudged" for e in events))

    def test_nudges_survive_a_tree_round_trip(self) -> None:
        node = _single()
        node.nudges = 2
        self.assertEqual(TaskNode.from_dict(json.loads(json.dumps(node.to_dict()))).nudges, 2)


class _ResumeAdapter:
    supports_session_resume = True
    has_file_tools = True

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.resume_ids: list[str | None] = []

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs):
        self.prompts.append(prompt)
        self.resume_ids.append(kwargs.get("resume_session_id"))
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def _seed_events(run_dir: Path, *, nudge: bool) -> None:
    log = EventLog(events_path(run_dir))
    log.append({"node_id": "n", "type": "node_dispatched", "ts": 1})
    log.append({"node_id": "n", "type": "session_captured", "session_id": "ses_abc", "ts": 2})
    log.append({"node_id": "n", "type": "episode_completed", "status": "done", "ts": 3})
    log.append({"node_id": "n", "type": "node_gate_failed", "ts": 4})
    log.append({"node_id": "n", "type": "node_redispatched", "reason": "resumed_session", "ts": 5})
    # A resumed episode re-captures only its logdir.
    log.append({"node_id": "n", "type": "session_captured", "session_id": None, "logdir": "/tmp/x", "ts": 6})
    log.append({"node_id": "n", "type": "episode_completed", "status": "done", "ts": 7})
    if nudge:
        log.append({"node_id": "n", "type": "node_nudged", "nudge": 1, "message": "KEEP GOING", "ts": 8})
    else:
        log.append({"node_id": "n", "type": "node_gate_failed", "ts": 8})


class RunnerTest(unittest.TestCase):
    def _run(self, *, nudge: bool, stubs: bool = False) -> tuple[_ResumeAdapter, list[dict]]:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            if stubs:
                leaf = TaskNode(
                    id="n", brief="Weeks 1 to 3", artifact="out/n.md", gates=["units_min:3@#*#"],
                    budget=NodeBudget(units_expected=3, unit_delimiter="#*#"),
                )
                ensure_leaf_unit_stubs(run_dir, leaf)[0].write_text("#*# Week 1\nx\n", encoding="utf-8")
            else:
                (run_dir / "out" / "n.md").write_text("#*# Week 1\nx\n", encoding="utf-8")
            _seed_events(run_dir, nudge=nudge)
            adapter = _ResumeAdapter()
            asyncio.run(run_node(run_dir, "n", "the full task prompt", adapter, LocalEnvironment(), EpisodeBudget()))
            return adapter, EventLog(events_path(run_dir)).read_all()

    def test_session_lookup_skips_logdir_only_capture(self) -> None:
        adapter, events = self._run(nudge=False)
        self.assertEqual(adapter.resume_ids, ["ses_abc"])
        self.assertIn("the full task prompt", adapter.prompts[0])
        reasons = [e.get("reason") for e in events if e["type"] == "node_redispatched"]
        self.assertEqual(reasons[-1], "resumed_session")

    def test_nudge_resumes_the_session_with_only_the_nudge(self) -> None:
        adapter, events = self._run(nudge=True)
        self.assertEqual(adapter.resume_ids, ["ses_abc"])
        self.assertEqual(adapter.prompts, ["KEEP GOING"])
        last = [e for e in events if e["type"] == "node_redispatched"][-1]
        self.assertEqual((last["reason"], last["nudge"]), ("nudged_session", 1))

    def test_nudge_resumes_a_unit_stub_leaf_too(self) -> None:
        adapter, _ = self._run(nudge=True, stubs=True)
        self.assertEqual((adapter.resume_ids, adapter.prompts), (["ses_abc"], ["KEEP GOING"]))

    def test_stub_leaf_without_nudge_still_starts_fresh(self) -> None:
        adapter, _ = self._run(nudge=False, stubs=True)
        self.assertEqual(adapter.resume_ids, [None])

    def test_nudge_consumes_the_completion_and_is_sent_once(self) -> None:
        events = [
            {"node_id": "n", "type": "episode_completed", "ts": 1},
            {"node_id": "n", "type": "node_nudged", "message": "m", "ts": 2},
        ]
        self.assertTrue(_completion_consumed(events, "n", events[0]))
        self.assertIsNotNone(_pending_nudge(events, "n"))
        events.append({"node_id": "n", "type": "node_redispatched", "ts": 3})
        self.assertIsNone(_pending_nudge(events, "n"))
        self.assertIsNone(_last_captured_session(events, "n"))


if __name__ == "__main__":
    unittest.main()
