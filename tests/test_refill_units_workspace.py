"""PLAN-SWEEP-REPAIR.md §K1-§K3 — found live on longgen_000-week_armC_seed1.

All hermetic: no provider calls, no agent binary, no network.

§K1  Continuous wave refill. ``max_parallel > 1`` used to gather the whole wave
     and only then run retries, one node at a time, so a node that failed in
     0.7 s and a ready node the wave had no room for both sat behind the
     slowest episode. These tests pin: a fast failure retries while a slow
     sibling is still running; a freed slot starts the next ready node at
     once; and a node in its retry backoff (``pending``) is never handed to a
     second job.
§K2  A leaf's ``units_expected`` comes from the spine's own counts. The brief
     "Entrys 21 to 40" missed the planner's range pattern (no ``entry`` in its
     noun list) and fell through to the named-ordinal rule, which read the
     range START as the count: 1/21/41 for leaves of 20/20/12.
§K3  Arm-A workspaces live outside any git work tree. OpenCode resolved its
     project upward from ``bench_results/longgen/workspaces/...`` to the
     kusudaemon checkout and wrote the diary there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.adapters.base import AgentAdapter  # noqa: E402
from kusudaemon.environment.base import Environment  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.pipeline.admission import RunAdmissionController  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.round_loop import run_round_loop  # noqa: E402
from kusudaemon.v1.run_dir import create_run_dir, events_path, tree_path  # noqa: E402
from kusudaemon.v1.tree import TaskTree  # noqa: E402
from kusudaemon.v2.planner import UNIT_RANGE_RE, build_tree, leaf_units_expected  # noqa: E402
from kusudaemon.v2.survey import (  # noqa: E402
    OUTPUT_UNIT_NOUNS,
    SpineUnit,
    pluralize_unit_noun,
    synthesize_output_spine,
)


# --------------------------------------------------------------------------
# §K1 — continuous refill
# --------------------------------------------------------------------------


class _Timeline:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.starts: dict[str, list[float]] = {}
        self.ends: dict[str, list[float]] = {}
        self.live: dict[str, int] = {}
        self.max_live: dict[str, int] = {}

    def now(self) -> float:
        return time.monotonic() - self.t0


class _ScriptedAdapter(AgentAdapter):
    """One scripted (delay_s, body) per attempt; an empty body fails ``nonempty``."""

    has_file_tools = True
    supports_session_resume = False

    def __init__(self, run_dir: Path, node_id: str, script: list[tuple[float, str]], tl: _Timeline) -> None:
        self._path = run_dir / "out" / f"{node_id}.md"
        self._node_id = node_id
        self._script = script
        self._tl = tl

    async def run_episode(
        self,
        prompt: str,
        env: Environment,
        budget: EpisodeBudget,
        live_trajectory_path: Path | None = None,
        **kwargs: Any,
    ) -> EpisodeResult:
        tl, nid = self._tl, self._node_id
        attempt = len(tl.starts.setdefault(nid, []))
        tl.starts[nid].append(tl.now())
        tl.live[nid] = tl.live.get(nid, 0) + 1
        tl.max_live[nid] = max(tl.max_live.get(nid, 0), tl.live[nid])
        try:
            delay, body = self._script[min(attempt, len(self._script) - 1)]
            await asyncio.sleep(delay)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(body, encoding="utf-8")
            return EpisodeResult(status="done", duration_ms=int(delay * 1000))
        finally:
            tl.live[nid] -= 1
            tl.ends.setdefault(nid, []).append(tl.now())


def _run(root: Path, scripts: dict[str, list[tuple[float, str]]], *, max_parallel: int, wave_cap: int):
    run_dir = create_run_dir(root, "run")
    tree_path(run_dir).write_text(
        json.dumps(
            [{"id": nid, "brief": nid, "artifact": f"out/{nid}.md", "gates": ["nonempty"]} for nid in scripts]
        ),
        encoding="utf-8",
    )
    prompt_dir = root / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    tl = _Timeline()
    tree = asyncio.run(
        run_round_loop(
            run_dir,
            tree_path(run_dir),
            writer_adapter_factory=lambda node: _ScriptedAdapter(run_dir, node.id, scripts[node.id], tl),
            env=LocalEnvironment(tmp_dir=str(prompt_dir)),
            provider=FakeProvider([]),
            prompt_for_node=lambda node: f"do {node.id}",
            writer_budget=EpisodeBudget(max_duration_seconds=60),
            max_parallel=max_parallel,
            dispatch_policy="document_order",
            admission_controller=RunAdmissionController(concurrency=8, initial_wave_cap=wave_cap),
        )
    )
    return run_dir, tree, tl


OK = "a real artifact body.\n"


class RefillTest(unittest.TestCase):
    def test_fast_failure_retries_while_slow_sibling_still_running(self) -> None:
        # The 000-week shape: unit-01 dies instantly, unit-02 runs long.
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl = _run(
                Path(td),
                {"fast": [(0.0, ""), (0.0, OK)], "slow": [(3.0, OK)]},
                max_parallel=2,
                wave_cap=2,
            )
            self.assertEqual(tree.nodes["fast"].status, "passed")
            self.assertEqual(tree.nodes["slow"].status, "passed")
            self.assertEqual(len(tl.starts["fast"]), 2)
            # The retry (after its 1 s backoff) began before "slow" finished.
            self.assertLess(tl.starts["fast"][1], tl.ends["slow"][0])

    def test_freed_slot_starts_next_ready_node_without_waiting_for_wave(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir, tree, tl = _run(
                Path(td),
                {"a": [(2.0, OK)], "b": [(0.05, OK)], "c": [(0.05, OK)]},
                max_parallel=2,
                wave_cap=2,
            )
            for nid in ("a", "b", "c"):
                self.assertEqual(tree.nodes[nid].status, "passed")
            # Only two slots: c was not in the first fill, and it still ran
            # to completion while a was in flight.
            self.assertLess(tl.ends["c"][0], tl.ends["a"][0])
            events = EventLog(events_path(run_dir)).read_all()
            decided = [e["node_id"] for e in events if e["type"] == "node_dispatch_decided"]
            self.assertEqual(sorted(decided), ["a", "b", "c"])

    def test_node_in_retry_backoff_is_neither_redispatched_nor_blocking(self) -> None:
        # "x" fails, then spends 1 s "pending" in backoff; "z" finishes during
        # that window and frees a slot. Document order would name x (first
        # pending node): it must not start a second job, and the slot must go
        # to "w" instead of idling until the next job finishes.
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl = _run(
                Path(td),
                {"x": [(0.0, ""), (0.5, OK)], "y": [(2.5, OK)], "z": [(0.2, OK)], "w": [(0.1, OK)]},
                max_parallel=3,
                wave_cap=3,
            )
            for nid in ("x", "y", "z", "w"):
                self.assertEqual(tree.nodes[nid].status, "passed")
            self.assertEqual(len(tl.starts["x"]), 2)
            self.assertEqual(tl.max_live["x"], 1)
            self.assertEqual(tree.nodes["x"].attempts, 1)
            self.assertLess(tl.starts["w"][0], tl.starts["x"][1])

    def test_attempt_cap_still_holds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl = _run(
                Path(td),
                {"bad": [(0.0, "")], "good": [(0.1, OK)]},
                max_parallel=2,
                wave_cap=2,
            )
            self.assertEqual(tree.nodes["good"].status, "passed")
            self.assertEqual(tree.nodes["bad"].status, "blocked")
            self.assertEqual(len(tl.starts["bad"]), 3)
            self.assertEqual(tl.max_live["bad"], 1)


# --------------------------------------------------------------------------
# §K2 — units_expected from the spine
# --------------------------------------------------------------------------

_WEEK_GOAL = (
    "Generate a diary for Sophia covering every week of 2018, 52 entries in total.\n"
    "1) Each weekly entry should be at least 200 words.\n"
    "2) Attend a parenting workshop every 5 weeks starting from week 13.\n"
    "3) Use '#*#' to separate each weekly entry "
    "(e.g. #*# Week 1 (January 1st - January 7th): ...).\n"
)


class LeafUnitsExpectedTest(unittest.TestCase):
    def test_week_spine_budgets_counts_not_range_starts(self) -> None:
        _, units = synthesize_output_spine(_WEEK_GOAL)
        self.assertEqual(sum(u.units_expected for u in units), 52)
        tree = build_tree(units, provider=None, default_gates=("nonempty",))
        leaves = list(tree.nodes.values())
        self.assertEqual([n.budget.units_expected for n in leaves], [u.units_expected for u in units])
        for node, unit in zip(leaves, units):
            # A hard gate with the leaf's own count, not a warning with its start.
            self.assertIn(f"units_min:{unit.units_expected}@#*#", node.gates)
            self.assertFalse(any(g.startswith("units_min") for g in node.warn_gates))

    def test_spine_counts_beat_the_brief(self) -> None:
        unit = SpineUnit(id="unit-02", label="Entrys 21 to 40", start_chunk=1, end_chunk=1, tokens=1, units_expected=20)
        self.assertEqual(leaf_units_expected("Produce the artifact for Entrys 21 to 40.", [unit]), (20, "declared"))

    def test_multi_unit_slice_sums_spine_counts(self) -> None:
        units = [
            SpineUnit(id=f"unit-{i}", label="x", start_chunk=i, end_chunk=i, tokens=1, units_expected=n)
            for i, n in enumerate((20, 20, 12))
        ]
        self.assertEqual(leaf_units_expected("anything", units), (52, "declared"))

    def test_brief_range_without_spine_counts(self) -> None:
        bare = [SpineUnit(id="u", label="u", start_chunk=0, end_chunk=0, tokens=1)]
        self.assertEqual(leaf_units_expected("Produce Entries 21 to 40.", bare), (20, "declared"))
        self.assertEqual(leaf_units_expected("Produce Entrys 41 to 52.", bare), (12, "declared"))
        self.assertEqual(leaf_units_expected("Floors 26 to 50", bare), (25, "declared"))
        self.assertEqual(leaf_units_expected("days 3-9", bare), (7, "declared"))

    def test_every_survey_label_reads_back_through_the_planner(self) -> None:
        for noun in OUTPUT_UNIT_NOUNS:
            label = f"{pluralize_unit_noun(noun).capitalize()} 21 to 40"
            m = UNIT_RANGE_RE.search(f"Produce the artifact for {label}.")
            self.assertIsNotNone(m, label)
            self.assertEqual((m.group(1), m.group(2)), ("21", "40"), label)

    def test_pluralize(self) -> None:
        self.assertEqual(pluralize_unit_noun("entry"), "entries")
        self.assertEqual(pluralize_unit_noun("day"), "days")
        self.assertEqual(pluralize_unit_noun("floor"), "floors")

    def test_spine_label_is_spelled_entries(self) -> None:
        goal = (
            "Generate a diary for Sophia, 52 entries in total.\n"
            "Each entry should be at least 200 words.\n"
            "Use '#*#' to separate each entry (e.g. #*# Entry 1: ...).\n"
        )
        chunks, units = synthesize_output_spine(goal)
        self.assertTrue(all(u.label.startswith("Entries ") for u in units))
        self.assertNotIn("Entrys", chunks[0].text)


# --------------------------------------------------------------------------
# §K3 — arm-A workspaces outside any git work tree
# --------------------------------------------------------------------------


class WorkspaceIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        import run_longgen_bench as rlb

        self.rlb = rlb
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name).resolve()
        if rlb.enclosing_git_root(self.tmp) is not None:
            self.skipTest("the system temp dir is itself inside a git work tree")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_default_root_is_outside_this_repo(self) -> None:
        root = self.rlb.DEFAULT_WORKSPACES_ROOT.expanduser().resolve()
        self.assertFalse(root.is_relative_to(_REPO_ROOT.resolve()), root)

    def test_old_in_repo_layout_is_detected(self) -> None:
        if not (_REPO_ROOT / ".git").exists():
            self.skipTest("not running from a git checkout")
        old_ws = _REPO_ROOT / "bench_results" / "longgen" / "workspaces" / "000-week_armA_seed1"
        self.assertEqual(self.rlb.enclosing_git_root(old_ws), _REPO_ROOT.resolve())

    def test_git_dir_and_git_file_both_count(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        self.assertEqual(self.rlb.enclosing_git_root(repo / "a" / "b"), repo)
        wt = self.tmp / "worktree"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
        self.assertEqual(self.rlb.enclosing_git_root(wt / "ws"), wt)
        self.assertIsNone(self.rlb.enclosing_git_root(self.tmp / "plain" / "ws"))

    def _args(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(
            backend="opencode", model=None, tier="auto", work_object="text",
            budget_tokens=None, max_rounds=None, runs_root=None, max_parallel=1,
            dry_run=True, score_only=False, workspaces_root=str(root),
        )

    def test_run_one_refuses_a_workspace_inside_a_repo(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        with self.assertRaises(SystemExit) as ctx:
            self.rlb.run_one(
                index=0, item={"type": "Week", "prompt": "p"}, arm="A", seed=1,
                args=self._args(repo / "workspaces"), results_dir=self.tmp / "results",
            )
        self.assertIn(str(repo), str(ctx.exception))

    def test_run_one_uses_the_workspaces_root(self) -> None:
        import contextlib
        import io

        root = self.tmp / "ws_root"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rec = self.rlb.run_one(
                index=0, item={"type": "Week", "prompt": "p"}, arm="A", seed=1,
                args=self._args(root), results_dir=self.tmp / "results",
            )
        self.assertTrue(rec["dry_run"])
        self.assertIn(str(root / "000-week_armA_seed1"), buf.getvalue())
        self.assertFalse((self.tmp / "results" / "workspaces").exists())


if __name__ == "__main__":
    unittest.main()
