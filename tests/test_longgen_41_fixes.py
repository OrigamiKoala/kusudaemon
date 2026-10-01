"""2026-09-30: regression tests for docs/LONGGENBENCH-RESULTS-2026-09.md §4.1
(items 1-11). Hermetic: no network, no agent binary, no pytest.

Item 7's ``caffeinate`` wrapper lives in scripts/run_longgen_bench.py and is
not covered here; only the driver-side clock-skew half is.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402

from kusudaemon.adapters import _agent_worker  # noqa: E402
from kusudaemon.adapters.cli_agent import classify_cli_failure  # noqa: E402
from kusudaemon.degeneration import DegenerationMonitor, degeneration_reason  # noqa: E402
from kusudaemon.eval.common import classify_halt  # noqa: E402
from kusudaemon.pipeline.cli import _bench_resolved, _derive_termination  # noqa: E402
from kusudaemon.pipeline.driver import (  # noqa: E402
    RecursiveDriver,
    RunOptions,
    _blocked_detail,
    resolve_final_artifact,
)
from kusudaemon.pipeline.prompts import _single_writer_clause  # noqa: E402
from kusudaemon.roles.json_io import _repair_common_schema_omissions  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path  # noqa: E402
from kusudaemon.v0.runner import run_node  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.v1.gates import GateResult  # noqa: E402
from kusudaemon.v1.json_schema import validate  # noqa: E402
from kusudaemon.v1.provider import OpenAICompatibleProvider  # noqa: E402
from kusudaemon.v1.review_loop import READ_LOOP_ACTION_SCHEMA  # noqa: E402
from kusudaemon.v1.reviewer import is_malformed_defect  # noqa: E402
from kusudaemon.v1.round_loop import (  # noqa: E402
    _transition_after_writer,
    non_attempt_cap_reached,
)
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree  # noqa: E402
from kusudaemon.v2.planner import build_tree  # noqa: E402
from kusudaemon.v2.survey import SpineUnit  # noqa: E402
from kusudaemon.v6.direct import SINGLE_NODE_ID  # noqa: E402


def _driver(root: Path, **opts) -> RecursiveDriver:
    options = RunOptions(goal="g", source_text="the corpus", dispatch_policy="document_order", **opts)
    return RecursiveDriver(
        root / "run",
        provider=FakeProvider([]),  # type: ignore[arg-type]
        options=options,
        writer_adapter_factory=lambda node: (_ for _ in ()).throw(AssertionError("no writer expected")),
        research_adapter_factory=lambda node, query: (_ for _ in ()).throw(AssertionError("no research expected")),
        probe_adapter_factory=lambda query: (_ for _ in ()).throw(AssertionError("no probe expected")),
        poll_interval=0.02,
    )


def _events(driver: RecursiveDriver) -> list[dict]:
    return EventLog(events_path(driver.run_dir)).read_all()


# ----------------------------------------------------------------------
# Item 1: parts-layout export
# ----------------------------------------------------------------------
class PartsLayoutExportTest(unittest.TestCase):
    def _run_dir(self, td: str) -> Path:
        return create_run_dir(Path(td), "r")

    def test_parts_layout_with_empty_stub_resolves_to_the_parts_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = self._run_dir(td)
            (run_dir / "out" / "single.md").write_text("", encoding="utf-8")
            parts = run_dir / "out" / "single"
            parts.mkdir()
            (parts / "part-01.md").write_text("#*# Week 1\nalpha\n", encoding="utf-8")
            (parts / "part-02.md").write_text("#*# Week 2\nbeta\n", encoding="utf-8")
            got = resolve_final_artifact(run_dir)
            self.assertIsNotNone(got)
            text = got.read_text(encoding="utf-8")  # type: ignore[union-attr]
            self.assertIn("Week 1", text)
            self.assertIn("Week 2", text)

    def test_parts_layout_with_missing_stub_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = self._run_dir(td)
            parts = run_dir / "out" / "single"
            parts.mkdir()
            (parts / "part-01.md").write_text("only part\n", encoding="utf-8")
            got = resolve_final_artifact(run_dir)
            self.assertEqual(got.read_text(encoding="utf-8").strip(), "only part")  # type: ignore[union-attr]

    def test_exported_file_is_not_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            driver = _driver(root)
            run_dir = driver.run_dir
            (run_dir / "out" / "single.md").write_text("", encoding="utf-8")
            (run_dir / "out" / "single").mkdir()
            (run_dir / "out" / "single" / "part-01.md").write_text("complete document\n", encoding="utf-8")
            out_dir = root / "exported"
            with patch.dict(os.environ, {"KUSUDAEMON_OUTPUT_DIR": str(out_dir), "KUSUDAEMON_NO_NOTIFY": "1"}):
                src = driver._resolve_final_artifact_path()
                dest = driver._resolve_export_path(src)
                driver._export_and_notify_completion(artifact_src=src, export_path=dest)
            self.assertEqual(Path(dest).read_text(encoding="utf-8").strip(), "complete document")  # type: ignore[arg-type]

    def test_plain_single_file_is_returned_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = self._run_dir(td)
            (run_dir / "out" / "single.md").write_text("plain\n", encoding="utf-8")
            self.assertEqual(resolve_final_artifact(run_dir), run_dir / "out" / "single.md")

    def test_assembly_main_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = self._run_dir(td)
            (run_dir / "assembly").mkdir(exist_ok=True)
            (run_dir / "assembly" / "main.md").write_text("assembled\n", encoding="utf-8")
            (run_dir / "out" / "single.md").write_text("stub\n", encoding="utf-8")
            self.assertEqual(resolve_final_artifact(run_dir), run_dir / "assembly" / "main.md")

    def test_bench_resolved_uses_nodes_and_rejects_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "e.md"
            empty.write_text("", encoding="utf-8")
            full = Path(td) / "f.md"
            full.write_text("x", encoding="utf-8")
            self.assertEqual(_bench_resolved(True, 1, 1, str(empty))[0], False)
            self.assertEqual(_bench_resolved(True, 1, 1, str(full)), (True, 1.0, 0))
            self.assertEqual(_bench_resolved(True, 1, 2, str(full))[0], False)


# ----------------------------------------------------------------------
# Item 2: salvage after throttled/transport, progress resets, time-based cap
# ----------------------------------------------------------------------
def _node(units: int = 3) -> TaskNode:
    return TaskNode(
        id="n1",
        brief="b",
        artifact="out/n1.md",
        gates=["nonempty", f"units_min:{units}@#*#"],
        budget=NodeBudget(tokens=50_000, calls=10, units_expected=units, unit_delimiter="#*#"),
    )


def _weeks(n: int) -> str:
    return "".join(f"#*# Week {i}\ntext {i}\n" for i in range(1, n + 1))


class NonAttemptTest(unittest.TestCase):
    def _transition(self, node: TaskNode, text: str, status: str, gates_ok: bool = True) -> list[dict]:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            (run_dir / "out" / "n1.md").write_text(text, encoding="utf-8")
            tree = TaskTree(nodes={"n1": node})
            tree.save(run_dir / "tree.json")
            log = EventLog(events_path(run_dir))
            results = [GateResult(gate=g, passed=gates_ok) for g in node.gates]
            asyncio.run(
                _transition_after_writer(
                    node, tree, run_dir / "tree.json", False, results, 3, log,
                    run_dir=run_dir, result_status=status,
                )
            )
            return log.read_all()

    def test_complete_artifact_is_salvaged_after_a_throttled_episode(self) -> None:
        node = _node(3)
        node.non_attempts = 5
        node.non_attempt_since = 1.0
        events = self._transition(node, _weeks(3), "throttled")
        self.assertEqual(node.status, "awaiting_review")
        self.assertEqual(node.non_attempts, 0)
        self.assertEqual(node.non_attempt_since, 0.0)
        self.assertIn("node_episode_salvaged", [e["type"] for e in events])

    def test_complete_artifact_is_salvaged_after_a_transport_episode(self) -> None:
        node = _node(3)
        self._transition(node, _weeks(3), "transport")
        self.assertEqual(node.status, "awaiting_review")

    def test_incomplete_artifact_is_not_salvaged(self) -> None:
        node = _node(3)
        self._transition(node, _weeks(1), "throttled", gates_ok=False)
        self.assertEqual(node.status, "pending")
        self.assertEqual(node.non_attempts, 1)

    def test_timeout_salvage_keeps_its_event_name(self) -> None:
        node = _node(3)
        events = self._transition(node, _weeks(3), "timeout")
        self.assertIn("node_timeout_salvaged", [e["type"] for e in events])

    def test_progress_restarts_the_streak(self) -> None:
        node = _node(5)
        node.non_attempts = 5
        node.non_attempt_since = 1.0
        self._transition(node, _weeks(2), "throttled", gates_ok=False)
        # 2 units written (no earlier snapshot): not blocked, streak restarted.
        self.assertEqual(node.status, "pending")
        self.assertEqual(node.non_attempts, 1)
        self.assertGreater(node.non_attempt_since, 1.0)

    def test_streak_ends_on_any_other_outcome(self) -> None:
        node = _node(5)
        node.non_attempts = 4
        node.non_attempt_since = 1.0
        self._transition(node, "", "error", gates_ok=False)
        self.assertEqual(node.non_attempts, 0)
        self.assertEqual(node.non_attempt_since, 0.0)

    def test_cap_needs_a_streak_that_has_lasted(self) -> None:
        with patch.dict(os.environ, {"KUSUDAEMON_MAX_NON_ATTEMPTS": "6", "KUSUDAEMON_NON_ATTEMPT_WINDOW_S": "1800"}):
            node = _node()
            node.non_attempts = 6
            now = 10_000.0
            node.non_attempt_since = now - 60  # six 429s in a minute
            self.assertFalse(non_attempt_cap_reached(node, now=now))
            node.non_attempt_since = now - 1900
            self.assertTrue(non_attempt_cap_reached(node, now=now))
            node.non_attempts = 5
            self.assertFalse(non_attempt_cap_reached(node, now=now))
            node.non_attempts = 60
            node.non_attempt_since = now - 1
            self.assertTrue(non_attempt_cap_reached(node, now=now), "hard ceiling")

    def test_legacy_streak_without_a_start_stays_count_based(self) -> None:
        node = _node()
        node.non_attempts = 6
        node.non_attempt_since = 0.0
        with patch.dict(os.environ, {"KUSUDAEMON_MAX_NON_ATTEMPTS": "6"}):
            self.assertTrue(non_attempt_cap_reached(node))

    def test_streak_start_round_trips(self) -> None:
        node = _node()
        node.non_attempt_since = 123.5
        self.assertEqual(TaskNode.from_dict(json.loads(json.dumps(node.to_dict()))).non_attempt_since, 123.5)


# ----------------------------------------------------------------------
# Item 3: phase detail
# ----------------------------------------------------------------------
class PhaseDetailTest(unittest.TestCase):
    def test_blocked_detail_names_the_node_and_defect(self) -> None:
        a = TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["nonempty"])
        a.status = "blocked"
        a.last_defect = "units_min:52: only 30 units"
        b = TaskNode(id="unit-02", brief="b", artifact="out/unit-02.md", gates=["nonempty"])
        b.status = "passed"
        detail = _blocked_detail(TaskTree(nodes={"unit-01": a, "unit-02": b}))
        self.assertEqual(detail, "blocked: unit-01: units_min:52: only 30 units")

    def test_run_phase_returns_the_detail_the_phase_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td))

            async def fake_execute() -> bool:
                driver._set_phase("execute", "escalated", detail="blocked: unit-01: boom")
                return False

            driver._phase_execute = fake_execute  # type: ignore[method-assign]
            report = asyncio.run(driver._run_phase("execute", round_index=1))
            self.assertEqual(report.status, "escalated")
            self.assertEqual(report.detail, "blocked: unit-01: boom")

    def test_classify_halt_no_detail_with_incomplete_document_is_an_agent_failure(self) -> None:
        self.assertEqual(classify_halt("escalated in execute: no detail"), "unknown")
        self.assertEqual(classify_halt("escalated in execute: no detail", completion_pct=54.0), "agent")
        self.assertEqual(classify_halt("escalated in execute: no detail", completion_pct=100.0), "unknown")
        self.assertEqual(classify_halt("escalated in execute: blocked: unit-01: x", completion_pct=4.0), "agent")


# ----------------------------------------------------------------------
# Item 6: halt cause
# ----------------------------------------------------------------------
class HaltCauseTest(unittest.TestCase):
    def test_endgame_is_a_budget_stop_and_logged_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td), wall_clock_budget=1000.0)
            driver._start_time = time.time() - 900  # 100 s left, under the 180 s reserve
            self.assertEqual(driver._halt_cause(), "wall clock budget exhausted")
            self.assertTrue(driver._halted())
            driver._halt_cause()
            endgames = [e for e in _events(driver) if e.get("type") == "wall_clock_endgame"]
            self.assertEqual(len(endgames), 1)

    def test_run_phase_reports_the_budget_not_the_operator(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td), wall_clock_budget=1000.0)
            driver._start_time = time.time() - 900
            report = asyncio.run(driver._run_phase("execute", round_index=1))
            self.assertEqual((report.status, report.detail), ("halted", "wall clock budget exhausted"))

    def test_operator_flag_is_still_the_operator(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td))
            self.assertIsNone(driver._halt_cause())
            (driver.run_dir / "halt.flag").touch()
            self.assertEqual(driver._halt_cause(), "halted by operator")
            report = asyncio.run(driver._run_phase("execute", round_index=1))
            self.assertEqual(report.detail, "halted by operator")

    def test_harness_written_flag_reports_its_own_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td))
            (driver.run_dir / "halt.flag").write_text("cost ceiling", encoding="utf-8")
            self.assertEqual(driver._halt_cause(), "cost ceiling")

    def test_classify_and_termination_read_the_new_wording_as_budget(self) -> None:
        self.assertEqual(classify_halt("wall clock budget exhausted"), "budget")
        self.assertEqual(_derive_termination(False, "wall clock budget exhausted"), "harness_timeout")


# ----------------------------------------------------------------------
# Item 7 (driver half): wall-vs-monotonic skew
# ----------------------------------------------------------------------
class ClockSkewTest(unittest.TestCase):
    def test_sleep_is_excluded_from_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td), wall_clock_budget=1000.0)
            now_wall, now_mono = time.time(), time.monotonic()
            driver._start_time = now_wall - 500       # 500 s of wall clock...
            driver._start_mono = now_mono - 100       # ...but only 100 s awake
            rem = driver._remaining_budget_seconds()
            self.assertAlmostEqual(rem, 900.0, delta=2.0)
            skew = [e for e in _events(driver) if e.get("type") == "clock_skew_detected"]
            self.assertEqual(len(skew), 1)
            self.assertAlmostEqual(skew[0]["skew_seconds"], 400.0, delta=2.0)
            driver._remaining_budget_seconds()
            self.assertEqual(len([e for e in _events(driver) if e.get("type") == "clock_skew_detected"]), 1)

    def test_no_skew_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td), wall_clock_budget=1000.0)
            driver._start_time = time.time() - 500
            driver._start_mono = time.monotonic() - 500
            self.assertAlmostEqual(driver._remaining_budget_seconds(), 500.0, delta=2.0)
            self.assertFalse([e for e in _events(driver) if e.get("type") == "clock_skew_detected"])

    def test_small_gap_is_noise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = _driver(Path(td), wall_clock_budget=1000.0)
            driver._start_time = time.time() - 500
            driver._start_mono = time.monotonic() - 490
            self.assertAlmostEqual(driver._remaining_budget_seconds(), 500.0, delta=2.0)


# ----------------------------------------------------------------------
# Item 4: read-loop action objects survive repair; raw failures are logged
# ----------------------------------------------------------------------
class ReadLoopRepairTest(unittest.TestCase):
    def test_read_action_is_not_replaced_by_a_pass_verdict(self) -> None:
        fixed = _repair_common_schema_omissions({"action": "read", "unit": 3}, READ_LOOP_ACTION_SCHEMA)
        self.assertEqual(fixed["action"], "read")
        self.assertEqual(fixed["unit"], 3)
        self.assertEqual(validate(fixed, READ_LOOP_ACTION_SCHEMA), [])

    def test_missing_action_is_inferred(self) -> None:
        self.assertEqual(
            _repair_common_schema_omissions({"units": [1, 4]}, READ_LOOP_ACTION_SCHEMA)["action"], "read"
        )
        fixed = _repair_common_schema_omissions(
            {"verdict": "fail", "items": [{"id": "x", "defect": "block 7 copies block 6"}]},
            READ_LOOP_ACTION_SCHEMA,
        )
        self.assertEqual(fixed["action"], "verdict")
        self.assertEqual(fixed["items"][0]["pass"], False)
        self.assertEqual(validate(fixed, READ_LOOP_ACTION_SCHEMA), [])

    def test_provider_accepts_a_read_action_and_logs_a_real_schema_failure(self) -> None:
        reads = json.dumps({"action": "read", "unit": 2})
        junk = "I think the artifact is fine."

        def make(content: str):
            def transport(url, payload, headers):
                return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                        "usage": {"completion_tokens": 20}}
            return transport

        provider = OpenAICompatibleProvider(
            api_key="k", transport=make(reads), sleep=lambda _s: None, base_retry_delay=0.0
        )
        self.assertEqual(provider.complete_json([{"role": "user", "content": "x"}], READ_LOOP_ACTION_SCHEMA)["action"], "read")

        seen: list[dict] = []
        bad = OpenAICompatibleProvider(
            api_key="k", transport=make(junk), sleep=lambda _s: None, base_retry_delay=0.0
        )
        bad._on_degenerate = seen.append  # type: ignore[assignment]
        with self.assertRaises(Exception):
            bad.complete_json([{"role": "user", "content": "x"}], READ_LOOP_ACTION_SCHEMA, retries=1)
        failures = [e for e in seen if e.get("type") == "role_schema_failure"]
        self.assertEqual(len(failures), 2)
        self.assertIn(junk, failures[0]["content_head"])


# ----------------------------------------------------------------------
# Item 5: T1 stalls
# ----------------------------------------------------------------------
_HEALTHY = (
    "The harness reads the run directory on every round, so a model that has just been restarted can "
    "pick up exactly where the previous one stopped. It looks at the tree, finds the first node whose "
    "dependencies are satisfied, and dispatches a writer for it. When the writer finishes, the gates "
    "are evaluated by code, and only then does the reviewer see the artifact. If anything fails, the "
    "defect is recorded and the node returns to the queue with that defect attached to its prompt. "
) * 20


class WordSaladTest(unittest.TestCase):
    def _salad(self) -> str:
        import random

        rng = random.Random(7)
        vocab = ["".join(rng.choice("abcdefghilmnoprstu") for _ in range(rng.randint(3, 9))) for _ in range(3000)]
        vocab += ["let", "story", "should", "supplies", "nil", "name"]
        return " ".join(rng.choice(vocab) for _ in range(900))

    def test_latin_word_salad_is_detected(self) -> None:
        self.assertEqual(degeneration_reason(self._salad()), "word_salad")

    def test_monitor_catches_it_on_a_stream(self) -> None:
        mon = DegenerationMonitor()
        salad = self._salad()
        reasons = [mon.feed(salad[i:i + 200]) for i in range(0, len(salad), 200)]
        self.assertIn("word_salad", [r for r in reasons if r])

    def test_healthy_prose_is_not_flagged(self) -> None:
        self.assertIsNone(degeneration_reason(_HEALTHY))

    def test_repo_prose_and_telegraphic_menus_are_not_flagged(self) -> None:
        flagged: list[str] = []
        files = list((_REPO_ROOT / "docs").glob("*.md")) + [_REPO_ROOT / "README.md"]
        files += list((_REPO_ROOT / "src" / "kusudaemon").rglob("*.py"))
        scanned = 0
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i in range(0, max(1, len(text) - 6000), 3000):
                scanned += 1
                if degeneration_reason(text[i:i + 6000]) == "word_salad":
                    flagged.append(f"{path.name}@{i}")
        self.assertGreater(scanned, 50)
        self.assertEqual(flagged, [])
        menu = (
            "#*# Week 5 (Jan 29 - Feb 4): 1. Mains: 1.1) Lamb Chops: smoked paprika grilled until charred "
            "juicy. 3.2) Coleslaw: creamy cabbage crisp apple toasted walnuts light yoghurt dressing. "
        ) * 30
        self.assertIsNone(degeneration_reason(menu))

    def test_short_or_structured_text_is_not_flagged(self) -> None:
        self.assertIsNone(degeneration_reason("alpha beta gamma " * 50))
        self.assertIsNone(degeneration_reason("\n".join(f"word{i} thing{i}" for i in range(400))))


class SingleWriterClauseTest(unittest.TestCase):
    def _node(self) -> TaskNode:
        return TaskNode(
            id=SINGLE_NODE_ID, brief="b", artifact="out/single.md",
            gates=["nonempty", "max_tokens:50000", "units_min:100@#*#"],
            budget=NodeBudget(tokens=50_000, calls=10, units_expected=100, unit_delimiter="#*#"),
        )

    def test_nonempty_file_forbids_whole_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "single.md"
            path.write_text("#*# Week 1\nx\n", encoding="utf-8")
            clause = _single_writer_clause(self._node(), path)
            self.assertIn("Do not use your `write` tool on it", clause)
            self.assertIn("cat >>", clause)

    def test_batch_and_size_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            clause = _single_writer_clause(self._node(), Path(td) / "single.md")
            self.assertIn("at most 3 to 5 units per tool call", clause)
            self.assertIn("100 units", clause)
            self.assertIn("50000 tokens", clause)
            self.assertNotIn("Do not use your `write` tool", clause)

    def test_leaves_get_no_clause(self) -> None:
        leaf = TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["nonempty"])
        self.assertEqual(_single_writer_clause(leaf, Path("/tmp/x.md")), "")


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


class StalledEmptySessionTest(unittest.TestCase):
    def _run(self, artifact: str | None) -> tuple[_ResumeAdapter, list[dict]]:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "r")
            if artifact is not None:
                (run_dir / "out" / "n.md").write_text(artifact, encoding="utf-8")
            log = EventLog(events_path(run_dir))
            log.append({"node_id": "n", "type": "node_dispatched", "ts": 1})
            log.append({"node_id": "n", "type": "session_captured", "session_id": "ses_abc", "ts": 2})
            log.append({"node_id": "n", "type": "episode_completed", "status": "done", "ts": 3})
            log.append({"node_id": "n", "type": "node_gate_failed", "ts": 4})
            adapter = _ResumeAdapter()
            asyncio.run(run_node(run_dir, "n", "the full task prompt", adapter, LocalEnvironment(), EpisodeBudget()))
            return adapter, EventLog(events_path(run_dir)).read_all()

    def test_session_that_left_nothing_is_not_resumed(self) -> None:
        adapter, events = self._run("")
        self.assertEqual(adapter.resume_ids, [None])
        last = [e for e in events if e["type"] == "node_redispatched"][-1]
        self.assertEqual(last["reason"], "session_stalled_empty")
        self.assertIn("at most 3 to 5 units per tool call", adapter.prompts[0])

    def test_session_with_work_is_still_resumed(self) -> None:
        adapter, events = self._run("#*# Week 1\nx\n")
        self.assertEqual(adapter.resume_ids, ["ses_abc"])
        last = [e for e in events if e["type"] == "node_redispatched"][-1]
        self.assertEqual(last["reason"], "resumed_session")


# ----------------------------------------------------------------------
# Item 8: document review cap and role stamp
# ----------------------------------------------------------------------
class DocumentReviewCapTest(unittest.TestCase):
    def _t2_driver(self, td: str, **opts) -> RecursiveDriver:
        driver = _driver(Path(td), **opts)
        (driver.run_dir / "tier.json").write_text(json.dumps({"tier": "T2"}), encoding="utf-8")
        node = TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["nonempty"])
        node.status = "passed"
        TaskTree(nodes={"unit-01": node}).save(driver.run_dir / "tree.json")
        return driver

    def test_review_is_skipped_late_in_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            driver = self._t2_driver(td, wall_clock_budget=1000.0)
            driver._start_time = time.time() - 600  # 400 s left: under 600, over the 180 s endgame
            called: list[int] = []

            async def boom(*a, **k):
                called.append(1)
                raise AssertionError("document review must be skipped")

            driver._document_review_cached_pass = boom  # type: ignore[method-assign]
            asyncio.run(driver._phase_review())
            self.assertFalse(called)
            skipped = [e for e in _events(driver) if e.get("type") == "document_review_skipped"]
            self.assertEqual(len(skipped), 1)

    def test_review_calls_are_stamped_as_reviewer(self) -> None:
        import kusudaemon.pipeline.driver as drv
        from kusudaemon.v1.provider import _scoped_role
        from kusudaemon.v3.document_review import DocumentReviewResult

        with tempfile.TemporaryDirectory() as td:
            driver = self._t2_driver(td)
            seen: list[str | None] = []

            def fake_review(*a, **k):
                seen.append(_scoped_role("unknown"))
                return DocumentReviewResult()

            with patch.object(drv, "run_document_review", fake_review):
                asyncio.run(driver._document_review_cached_pass(driver._load_tree(), keep_depth_pass=False))
            self.assertEqual(seen, ["reviewer"])


# ----------------------------------------------------------------------
# Item 9: location-only defects
# ----------------------------------------------------------------------
class MalformedDefectTest(unittest.TestCase):
    def test_location_only_defects_are_malformed(self) -> None:
        for text in ("§ Blocks 51–75", "§ Blocks 51-75", "unit 3", "Blocks 51 to 75", "Section 2"):
            self.assertTrue(is_malformed_defect("claims_supported", text), text)

    def test_real_defects_are_kept(self) -> None:
        for text in (
            "Block 7 copies block 6",
            "§Worked Examples, example 2 omits the intermediate step",
            "Floor 12 is missing its description",
        ):
            self.assertFalse(is_malformed_defect("claims_supported", text), text)


# ----------------------------------------------------------------------
# Item 10: first-output watchdog (opt-in)
# ----------------------------------------------------------------------
class FirstOutputWatchdogTest(unittest.TestCase):
    def test_error_text_counts_as_transport(self) -> None:
        self.assertEqual(classify_cli_failure("kusudaemon agent worker: no first output within 240s (provider stall)"), "transport")

    def test_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KUSUDAEMON_FIRST_OUTPUT_TIMEOUT_S", None)
            self.assertEqual(_agent_worker._first_output_timeout_s(), 0.0)

    def test_watchdog_fires_without_output_and_stays_quiet_with_it(self) -> None:
        class _Proc:
            returncode = None

        async def scenario(set_output: bool) -> tuple[bool, dict]:
            first, fatal, holder = asyncio.Event(), asyncio.Event(), {}
            if set_output:
                first.set()
            with patch.object(_agent_worker, "_terminate_proc_group", lambda p: None):
                await _agent_worker._first_output_watchdog(_Proc(), first, fatal, holder, 0.05)  # type: ignore[arg-type]
            return fatal.is_set(), holder

        fired, holder = asyncio.run(scenario(False))
        self.assertTrue(fired)
        self.assertIn("no first output", holder["error"])
        fired, _ = asyncio.run(scenario(True))
        self.assertFalse(fired)


# ----------------------------------------------------------------------
# Item 11: one-leaf plan carries the goal
# ----------------------------------------------------------------------
class SingleUnitBriefTest(unittest.TestCase):
    def test_single_leaf_brief_carries_the_goal(self) -> None:
        unit = SpineUnit(id="u1", label="The goal", start_chunk=0, end_chunk=0, tokens=50)
        goal = "Write a persuasive essay about harbours, in a warm tone."
        tree = build_tree([unit], provider=None, default_gates=("nonempty",), goal=goal)  # type: ignore[arg-type]
        (leaf,) = tree.nodes.values()
        self.assertIn(goal, leaf.brief)
        self.assertTrue(leaf.brief.startswith("Produce the artifact for The goal"))

    def test_no_goal_keeps_the_old_brief(self) -> None:
        unit = SpineUnit(id="u1", label="The goal", start_chunk=0, end_chunk=0, tokens=50)
        tree = build_tree([unit], provider=None, default_gates=("nonempty",))  # type: ignore[arg-type]
        (leaf,) = tree.nodes.values()
        self.assertNotIn("Goal:", leaf.brief)


if __name__ == "__main__":
    unittest.main()
