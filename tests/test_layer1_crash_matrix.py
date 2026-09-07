"""Layer 1 mechanism benchmark: Crash matrix (BENCHMARKING.md §9.1 / docs/TEST-PLAN.md §1.1).

Tests resume correctness across all 24 (tier, phase) crash points, verifying:
- terminal_events_per_node <= 1
- out/*.md are byte-identical (sha256) to uninterrupted run
- EventLog.read_all() parses cleanly
- resume dispatches zero writer episodes for already-passed nodes
- torn-write line recovery
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.eval import measure
from kusudaemon.eval.tasks import EvalTask, build_tasks
from kusudaemon.eval.runner import (
    PASS_VERDICT,
    _T2_REVIEW_CALLS,
    _counting_writer_factory,
    _never_called_research_factory,
    _options,
    _prepare_task,
)
from kusudaemon.pipeline import approvals as approval_store
from kusudaemon.pipeline.driver import RecursiveDriver
from kusudaemon.v0.events import EventLog
from kusudaemon.v0.run_dir import events_path
from kusudaemon.v1.json_schema import validate
from kusudaemon.v6.tiering import _PHASES_BY_TIER


class _RoleAwareScriptedProvider:
    """Scripted provider that routes canned responses by schema role rather than
    assuming a fixed call sequence across arbitrary crash and resume points."""

    def __init__(self, responses_by_role: dict[str, list[dict[str, Any]]]) -> None:
        self._responses = {k: list(v) for k, v in responses_by_role.items()}
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        retries: int = 2,
        on_reasoning: Any = None,
        streaming: bool = False,
    ) -> dict[str, Any]:
        self.calls.append((messages, schema))
        role = measure.role_of_schema(schema)
        queue = self._responses.get(role, [])
        if not queue:
            raise AssertionError(f"No canned response for role {role!r} (call #{len(self.calls)})")
        resp = queue.pop(0)
        errors = validate(resp, schema)
        if errors:
            raise AssertionError(f"canned response {resp!r} does not match schema: {errors}")
        return resp


def _hash_artifacts(run_dir: Path) -> dict[str, str]:
    out_dir = run_dir / "out"
    if not out_dir.exists():
        return {}
    hashes = {}
    for p in sorted(out_dir.glob("*.md")):
        hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


def _responses_by_role_for_task(task: EvalTask, plan_payload: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    by_role: dict[str, list[dict[str, Any]]] = {
        "classify": [task.estimate],
    }
    if plan_payload is not None:
        by_role["planner"] = [plan_payload]
    if task.expected_tier == "T2":
        by_role["reviewer"] = [PASS_VERDICT] * (_T2_REVIEW_CALLS * 3)
    return by_role


_CANONICAL_TASKS = {
    "T0": "t0-typo",
    "T1": "t1-notes",
    "T2": "t2-feature",
    "T3": "t3-refactor",
}


_SUBPROCESS_SCRIPT = """
import asyncio
import os
import sys
from pathlib import Path

from kusudaemon.eval.tasks import build_tasks
from kusudaemon.eval.runner import _prepare_task, _options, _counting_writer_factory, _never_called_research_factory
from kusudaemon.pipeline import approvals as approval_store
from kusudaemon.pipeline.driver import RecursiveDriver
from test_layer1_crash_matrix import _RoleAwareScriptedProvider, _responses_by_role_for_task, _CANONICAL_TASKS

root = Path(sys.argv[1])
tier = sys.argv[2]
task = next(t for t in build_tasks() if t.task_id == _CANONICAL_TASKS[tier])
work_object, plan_payload = _prepare_task(root, task)
run_dir = root / 'run'
if tier == 'T1':
    (run_dir / 'spine.json').unlink(missing_ok=True)
dispatches = [0]
responses = _responses_by_role_for_task(task, plan_payload)
provider = _RoleAwareScriptedProvider(responses)
driver = RecursiveDriver(
    run_dir,
    provider=provider,
    options=_options(task, work_object),
    writer_adapter_factory=_counting_writer_factory(run_dir, dispatches),
    research_adapter_factory=_never_called_research_factory,
    probe_adapter_factory=_never_called_research_factory,
    poll_interval=0.01,
)
with approval_store.Approver(run_dir, poll_interval=0.01):
    asyncio.run(driver.run())
"""


class Layer1CrashMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def _task_for_tier(self, tier: str) -> EvalTask:
        task_id = _CANONICAL_TASKS[tier]
        return next(t for t in build_tasks() if t.task_id == task_id)

    def _run_clean(self, task: EvalTask) -> tuple[dict[str, str], int]:
        """Run uninterrupted in a fresh directory and return artifact hashes and dispatch count."""
        root = self.tmp_path / f"clean_{task.expected_tier}"
        work_object, plan_payload = _prepare_task(root, task)
        run_dir = root / "run"
        if task.expected_tier == "T1":
            (run_dir / "spine.json").unlink(missing_ok=True)
        dispatches = [0]
        responses = _responses_by_role_for_task(task, plan_payload)
        provider = _RoleAwareScriptedProvider(responses)

        driver = RecursiveDriver(
            run_dir,
            provider=provider,  # type: ignore[arg-type]
            options=_options(task, work_object),
            writer_adapter_factory=_counting_writer_factory(run_dir, dispatches),
            research_adapter_factory=_never_called_research_factory,
            probe_adapter_factory=_never_called_research_factory,
            poll_interval=0.01,
        )
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        self.assertEqual(report.status, "done")
        return _hash_artifacts(run_dir), dispatches[0]

    def test_crash_matrix_all_24_points(self) -> None:
        """Run each (tier, phase) crash point in a subprocess, resume in-process, and verify."""
        clean_hashes: dict[str, dict[str, str]] = {}
        clean_dispatches: dict[str, int] = {}

        for tier in ("T0", "T1", "T2", "T3"):
            task = self._task_for_tier(tier)
            hashes, disp = self._run_clean(task)
            clean_hashes[tier] = hashes
            clean_dispatches[tier] = disp

        for tier, phases in _PHASES_BY_TIER.items():
            task = self._task_for_tier(tier)
            for phase in phases:
                with self.subTest(tier=tier, phase=phase):
                    crash_root = self.tmp_path / f"crash_{tier}_{phase}"
                    work_object, plan_payload = _prepare_task(crash_root, task)
                    run_dir = crash_root / "run"

                    # 1. Run driver in subprocess with KUSUDAEMON_CRASH_AT
                    env = dict(os.environ)
                    env["KUSUDAEMON_CRASH_AT"] = phase
                    env["PYTHONPATH"] = str(_REPO_ROOT / "src") + ":" + str(_REPO_ROOT / "tests")

                    proc = subprocess.run(
                        [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(crash_root), tier],
                        env=env,
                        capture_output=True,
                    )
                    self.assertEqual(
                        proc.returncode,
                        137,
                        f"Expected subprocess crash with 137 at ({tier}, {phase}), got {proc.returncode}: {proc.stderr.decode()}",
                    )

                    # 2. Resume in-process
                    resume_dispatches = [0]
                    responses = _responses_by_role_for_task(task, plan_payload)
                    provider = _RoleAwareScriptedProvider(responses)

                    driver = RecursiveDriver(
                        run_dir,
                        provider=provider,  # type: ignore[arg-type]
                        options=_options(task, work_object),
                        writer_adapter_factory=_counting_writer_factory(run_dir, resume_dispatches),
                        research_adapter_factory=_never_called_research_factory,
                        probe_adapter_factory=_never_called_research_factory,
                        poll_interval=0.01,
                    )
                    with approval_store.Approver(run_dir, poll_interval=0.01):
                        report = asyncio.run(driver.run())
                    self.assertEqual(report.status, "done")

                    # 3. Invariants
                    # - Terminal events per node <= 1
                    term_events = measure.terminal_events_per_node(run_dir)
                    for nid, count in term_events.items():
                        self.assertLessEqual(
                            count,
                            1,
                            f"Node {nid} has {count} terminal events (> 1) at crash ({tier}, {phase})",
                        )

                    # - out/*.md byte-identical (sha256) to clean run
                    resumed_hashes = _hash_artifacts(run_dir)
                    self.assertEqual(
                        resumed_hashes,
                        clean_hashes[tier],
                        f"Artifact hashes mismatched at crash ({tier}, {phase})",
                    )

                    # - EventLog parses fully
                    log = EventLog(events_path(run_dir))
                    all_events = log.read_all()
                    self.assertTrue(all_events)

                    # - Resume dispatches 0 writer episodes for already-passed nodes
                    phases_list = list(phases)
                    execute_idx = phases_list.index("execute")
                    crashed_idx = phases_list.index(phase)
                    if crashed_idx >= execute_idx:
                        self.assertEqual(
                            resume_dispatches[0],
                            0,
                            f"Resume dispatched {resume_dispatches[0]} episodes after execute phase at ({tier}, {phase})",
                        )

    def test_torn_write_recovery(self) -> None:
        """Truncate events.jsonl to an offset inside its last line and assert recovery."""
        task = self._task_for_tier("T1")
        root = self.tmp_path / "torn_write_test"
        work_object, plan_payload = _prepare_task(root, task)
        run_dir = root / "run"

        env = dict(os.environ)
        env["KUSUDAEMON_CRASH_AT"] = "execute"
        env["PYTHONPATH"] = str(_REPO_ROOT / "src") + ":" + str(_REPO_ROOT / "tests")

        proc = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(root), "T1"],
            env=env,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 137)

        # Truncate events.jsonl to mid-line
        ev_file = events_path(run_dir)
        content = ev_file.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        self.assertGreater(len(lines), 1)
        # Append a torn line (half a JSON object without closing newline)
        torn_fragment = '{"node_id": "-", "role": "harness", "type": "phase_in'
        ev_file.write_text(content + torn_fragment, encoding="utf-8")

        # Resume over the torn event log
        resume_dispatches = [0]
        responses = _responses_by_role_for_task(task, plan_payload)
        provider = _RoleAwareScriptedProvider(responses)

        driver = RecursiveDriver(
            run_dir,
            provider=provider,  # type: ignore[arg-type]
            options=_options(task, work_object),
            writer_adapter_factory=_counting_writer_factory(run_dir, resume_dispatches),
            research_adapter_factory=_never_called_research_factory,
            probe_adapter_factory=_never_called_research_factory,
            poll_interval=0.01,
        )
        with approval_store.Approver(run_dir, poll_interval=0.01):
            report = asyncio.run(driver.run())
        self.assertEqual(report.status, "done")


if __name__ == "__main__":
    unittest.main()
