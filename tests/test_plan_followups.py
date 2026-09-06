"""Follow-up fixes for PLAN-REVIEW-LATENCY(-STATUS).md and PLAN-WORKSPACE-MODE.md.

Covers the leftovers closed after the STATUS audit (verified 2026-09-06
against 214e78a + working tree):

- T0-6: make_role_provider threads timeout into BackendRoleProvider
- K4b: probe_finding_path_unreachable logged on the sibling-run-dir path
- K1: PLAN_MIN_WORKSPACE_TOKENS constant shared by work_object + driver
- K6: workspace-kind runs force DEFAULT_TOOL_ALLOWLIST (bash restored)
- §8.4b: needs_research in tier.json + logged research no-ops
- §8.4a: work_kind evidence (schemas, parsing, monotone T1 floor)

Stdlib unittest, no network, no agent binary, no API key (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402

from kusudaemon.pipeline.backends import (  # noqa: E402
    _hidden_paths_and_exceptions_for_probe,
    build_writer_adapter,
)
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import tier_path  # noqa: E402
from kusudaemon.roles.backend_provider import BackendRoleProvider  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir  # noqa: E402
from kusudaemon.v1.tree import NodeBudget, TaskNode  # noqa: E402
from kusudaemon.v4.research import research_raw_finding_path  # noqa: E402
from kusudaemon.v6.tiering import (  # noqa: E402
    ESTIMATE_SCHEMA,
    FULL_SCOPE_SCHEMA,
    WORK_KIND_VALUES,
    ScopeEstimate,
    _normalize_work_kind,
    classify,
    estimate_scope,
    estimate_scope_full,
    measure_signals,
)
from kusudaemon.v6.work_object import (  # noqa: E402
    PLAN_MIN_WORKSPACE_TOKENS,
    work_object_none,
)


def _events_of(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


class BackendRoleProviderTimeoutTest(unittest.TestCase):
    """T0-6: an explicit timeout wins; None preserves env/defaults."""

    def _provider(self, tmp: str, **kwargs) -> BackendRoleProvider:
        return BackendRoleProvider(backend="opencode", run_dir=Path(tmp) / "run", **kwargs)

    def test_explicit_timeout_wins_over_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {"KUSUDAEMON_REVIEWER_TIMEOUT": "999", "KUSUDAEMON_ROLE_TIMEOUT": "999"},
                clear=False,
            ):
                provider = self._provider(td, role="reviewer", timeout=45.0)
                self.assertEqual(provider.budget.max_duration_seconds, 45)

    def test_explicit_budget_wins_over_timeout(self) -> None:
        from kusudaemon.types import EpisodeBudget

        with tempfile.TemporaryDirectory() as td:
            provider = self._provider(
                td, role="reviewer", budget=EpisodeBudget(max_duration_seconds=17), timeout=45.0
            )
            self.assertEqual(provider.budget.max_duration_seconds, 17)

    def test_none_preserves_reviewer_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {k: v for k, v in os.environ.items() if k not in ("KUSUDAEMON_REVIEWER_TIMEOUT", "KUSUDAEMON_ROLE_TIMEOUT")}
            with mock.patch.dict(os.environ, env, clear=True):
                reviewer = self._provider(td, role="reviewer")
                other = self._provider(td, role="planner")
                self.assertEqual(reviewer.budget.max_duration_seconds, 120)
                self.assertEqual(other.budget.max_duration_seconds, 300)

    def test_none_preserves_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"KUSUDAEMON_ROLE_TIMEOUT": "60"}, clear=False):
                provider = self._provider(td, role="planner")
                self.assertEqual(provider.budget.max_duration_seconds, 60)

    def test_factory_threads_timeout_to_backend(self) -> None:
        from kusudaemon.roles.factory import make_role_provider

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            env = {
                "KUSUDAEMON_ROLE_TRANSPORT": "backend",
                "KUSUDAEMON_ROLE_BACKEND": "opencode",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                provider = make_role_provider(run_dir=run_dir, role="reviewer", timeout=45.0)
                assert isinstance(provider, BackendRoleProvider)
                self.assertEqual(provider.budget.max_duration_seconds, 45)

    def test_factory_none_keeps_backend_default(self) -> None:
        from kusudaemon.roles.factory import make_role_provider

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            run_dir.mkdir()
            base = {k: v for k, v in os.environ.items() if k not in ("KUSUDAEMON_REVIEWER_TIMEOUT", "KUSUDAEMON_ROLE_TIMEOUT")}
            base.update(
                    {"KUSUDAEMON_ROLE_TRANSPORT": "backend", "KUSUDAEMON_ROLE_BACKEND": "opencode"}
            )
            with mock.patch.dict(os.environ, base, clear=True):
                provider = make_role_provider(run_dir=run_dir, role="reviewer")
                assert isinstance(provider, BackendRoleProvider)
                self.assertEqual(provider.budget.max_duration_seconds, 120)


class ProbeCarveoutLogTest(unittest.TestCase):
    """K4b item 2: the sibling-run-dir absolute fallback is logged."""

    def test_sibling_layout_logs_unreachable_and_stays_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            workspace = Path(td) / "repo"
            run_dir.mkdir()
            workspace.mkdir()
            raw = research_raw_finding_path(run_dir, "node-1", "q1")
            hidden, exceptions = _hidden_paths_and_exceptions_for_probe(run_dir, workspace, raw)
            self.assertEqual(hidden, ())
            self.assertEqual(exceptions, (raw.resolve().as_posix(),))
            logged = [e for e in _events_of(run_dir) if e.get("type") == "probe_finding_path_unreachable"]
            self.assertEqual(len(logged), 1)
            self.assertIn("absolute", logged[0].get("reason", ""))

    def test_nested_layout_stays_silent_with_relative_carveout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "repo"
            nested_run = workspace / ".kusudaemon" / "run"
            nested_run.mkdir(parents=True)
            nested_raw = research_raw_finding_path(nested_run, "node-1", "q1")
            hidden, exceptions = _hidden_paths_and_exceptions_for_probe(
                nested_run, workspace, nested_raw
            )
            self.assertEqual(hidden, (".kusudaemon/run/",))
            self.assertEqual(exceptions, (nested_raw.relative_to(workspace).as_posix(),))
            logged = [e for e in _events_of(nested_run) if e.get("type") == "probe_finding_path_unreachable"]
            self.assertEqual(logged, [])


class WorkspaceFloorConstantTest(unittest.TestCase):
    """K1: the small-workspace floor is one shared constant."""

    def test_floor_value_and_sharing(self) -> None:
        from kusudaemon.pipeline import driver as driver_module
        from kusudaemon.v6 import tiering as tiering_module

        self.assertEqual(PLAN_MIN_WORKSPACE_TOKENS, 2_000)
        self.assertEqual(PLAN_MIN_WORKSPACE_TOKENS, tiering_module._T1_WORK_TOKENS_CEILING)
        self.assertIs(driver_module.PLAN_MIN_WORKSPACE_TOKENS, PLAN_MIN_WORKSPACE_TOKENS)


class WorkspaceToolsFallbackTest(unittest.TestCase):
    """K6: workspace-kind runs keep shell even under a prose template."""

    def _prose_node(self) -> TaskNode:
        return TaskNode(
            id="single",
            brief="Do the thing",
            artifact="out/single.md",
            budget=NodeBudget(tokens=12000),
            gates=["nonempty"],
            tools=["read", "save"],
            shape="prose-dominant",
        )

    def _adapter(self, *, is_workspace: bool, shape: str = "prose-dominant"):
        from kusudaemon.adapters.gptme_adapter import GptmeAdapter

        node = self._prose_node()
        node.shape = shape
        with mock.patch.dict(
            os.environ,
            {"NVIDIA_API_KEY": "test-key-123", "OPENAI_API_KEY": "test-key-123"},
            clear=False,
        ):
            adapter = build_writer_adapter(
                "gptme",
                workspace_path="/tmp/ws",
                prompt_dir="/tmp/prompts",
                node=node,
                run_dir="/tmp/ws",
                is_workspace=is_workspace,
                always_grant_web_search=False,
            )
        assert isinstance(adapter, GptmeAdapter)
        return adapter

    def test_workspace_forces_default_allowlist(self) -> None:
        from kusudaemon.adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST

        adapter = self._adapter(is_workspace=True)
        self.assertIn("shell", adapter.tool_allowlist)
        self.assertEqual(tuple(adapter.tool_allowlist[:4]), tuple(DEFAULT_TOOL_ALLOWLIST[:4]))

    def test_non_workspace_keeps_template_opinion(self) -> None:
        adapter = self._adapter(is_workspace=False)
        self.assertNotIn("shell", adapter.tool_allowlist)
        self.assertEqual(list(adapter.tool_allowlist), ["read", "save"])

    def test_code_shape_keeps_shell_without_workspace_flag(self) -> None:
        adapter = self._adapter(is_workspace=False, shape="code-dominant")
        self.assertIn("shell", adapter.tool_allowlist)


class NeedsResearchTest(unittest.TestCase):
    """§8.4b: needs_research decided at classify, read back, logged."""

    def _driver(self, run_dir: Path, **options_kwargs) -> RecursiveDriver:
        opts = dict(goal="test goal")
        opts.update(options_kwargs)
        return RecursiveDriver(
            run_dir,
            provider=None,  # type: ignore[arg-type]
            options=RunOptions(**opts),  # type: ignore[arg-type]
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(
                AssertionError("no writer dispatch expected")
            ),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(
                AssertionError("no research dispatch expected")
            ),
            poll_interval=0.02,
        )

    def _write_tier(self, run_dir: Path, **fields) -> None:
        record = {"tier": "T3", "ts": 0}
        record.update(fields)
        tier_path(run_dir).write_text(json.dumps(record), encoding="utf-8")

    def test_disabled_probing_logs_skip_without_dispatch(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                self._write_tier(run_dir, needs_research=False)
                driver = self._driver(run_dir)
                await driver._phase_research()
                skips = [
                    e
                    for e in _events_of(run_dir)
                    if e.get("type") == "phase_skipped" and e.get("phase") == "research"
                ]
                self.assertEqual(len(skips), 1)
                self.assertIn("needs_research=false", skips[0].get("reason", ""))

        asyncio.run(scenario())

    def test_zero_probes_logs_skip(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                # Old run records lack the key: default True, then the empty
                # plan logs its own skip instead of returning silently.
                self._write_tier(run_dir)
                driver = self._driver(run_dir, auto_probe_plan=False)
                await driver._phase_research()
                skips = [
                    e
                    for e in _events_of(run_dir)
                    if e.get("type") == "phase_skipped" and e.get("phase") == "research"
                ]
                self.assertEqual(len(skips), 1)
                self.assertIn("zero probes", skips[0].get("reason", ""))

        asyncio.run(scenario())

    def test_classify_records_needs_research(self) -> None:
        async def scenario() -> None:
            canned = {
                "files_touched": "many",
                "artifacts": 50,
                "answerable_without_exploration": False,
                "questions": [],
                "objections": [],
            }
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = Path(root_str) / "run"
                create_run_dir(run_dir.parent, run_dir.name)
                driver = self._driver(
                    run_dir,
                    goal="rebuild everything everywhere",
                    auto_probe_plan=True,
                )
                driver.provider = FakeProvider([canned])  # type: ignore[assignment]
                await driver._phase_classify()
                record = json.loads(tier_path(run_dir).read_text(encoding="utf-8"))
                self.assertEqual(record["tier"], "T3")
                self.assertTrue(record["needs_research"])

                run_dir2 = Path(root_str) / "run2"
                create_run_dir(run_dir2.parent, run_dir2.name)
                driver2 = self._driver(
                    run_dir2,
                    goal="rebuild everything everywhere",
                    auto_probe_plan=False,
                )
                driver2.provider = FakeProvider([dict(canned)])  # type: ignore[assignment]
                await driver2._phase_classify()
                record2 = json.loads(tier_path(run_dir2).read_text(encoding="utf-8"))
                self.assertEqual(record2["tier"], "T3")
                self.assertFalse(record2["needs_research"])

        asyncio.run(scenario())


class WorkKindTest(unittest.TestCase):
    """§8.4a: work_kind evidence — schemas, parsing, monotone floor."""

    def test_values_and_normalization(self) -> None:
        self.assertEqual(set(WORK_KIND_VALUES), {"document", "procedure", "code-edit", "unknown"})
        self.assertEqual(_normalize_work_kind("procedure"), "procedure")
        self.assertEqual(_normalize_work_kind("Procedure"), "procedure")
        self.assertEqual(_normalize_work_kind("nonsense"), "unknown")
        self.assertEqual(_normalize_work_kind(None), "unknown")
        self.assertIn("work_kind", ESTIMATE_SCHEMA["properties"])
        self.assertIn("work_kind", FULL_SCOPE_SCHEMA["properties"])
        self.assertNotIn("work_kind", ESTIMATE_SCHEMA["required"])
        self.assertNotIn("work_kind", FULL_SCOPE_SCHEMA["required"])

    def test_estimate_scope_parses_work_kind(self) -> None:
        canned = {
            "files_touched": "few",
            "artifacts": 2,
            "answerable_without_exploration": False,
            "work_kind": "code-edit",
        }
        estimate = estimate_scope("goal", work_object_none(), FakeProvider([canned]))  # type: ignore[arg-type]
        self.assertEqual(estimate.work_kind, "code-edit")

    def test_estimate_scope_defaults_unknown(self) -> None:
        canned = {
            "files_touched": "few",
            "artifacts": 2,
            "answerable_without_exploration": False,
        }
        estimate = estimate_scope("goal", work_object_none(), FakeProvider([canned]))  # type: ignore[arg-type]
        self.assertEqual(estimate.work_kind, "unknown")

    def test_estimate_scope_full_parses_work_kind(self) -> None:
        canned = {
            "files_touched": "1",
            "artifacts": 1,
            "answerable_without_exploration": True,
            "questions": [],
            "objections": [],
            "work_kind": "procedure",
        }
        estimate, _ = estimate_scope_full("goal", work_object_none(), FakeProvider([canned]))  # type: ignore[arg-type]
        self.assertEqual(estimate.work_kind, "procedure")

    def _t0_fixture(self, work_kind: str = "unknown") -> tuple:
        signals = measure_signals("fix typo", work_object_none())
        estimate = ScopeEstimate(
            files_touched="1",
            artifacts=1,
            answerable_without_exploration=True,
            work_kind=work_kind,
        )
        return signals, estimate

    def test_unknown_preserves_table(self) -> None:
        signals, estimate = self._t0_fixture("unknown")
        self.assertEqual(classify(signals, estimate, goal="fix typo"), "T0")

    def test_document_and_code_edit_preserve_table(self) -> None:
        for kind in ("document", "code-edit"):
            signals, estimate = self._t0_fixture(kind)
            self.assertEqual(classify(signals, estimate, goal="fix typo"), "T0")

    def test_procedure_floors_at_t1_with_event(self) -> None:
        seen: list[dict] = []
        signals, estimate = self._t0_fixture("procedure")
        tier = classify(signals, estimate, goal="restart the service", on_event=seen.append)
        self.assertEqual(tier, "T1")
        floors = [e for e in seen if e.get("type") == "tier_work_kind_floor"]
        self.assertEqual(len(floors), 1)
        self.assertEqual(floors[0]["tier_raised_to"], "T1")

    def test_procedure_never_lowers(self) -> None:
        signals = measure_signals("fix typo", work_object_none())
        estimate = ScopeEstimate(files_touched="unknown", artifacts=2, work_kind="procedure")
        self.assertEqual(classify(signals, estimate, goal="fix typo"), "T2")

    def test_json_io_repair_normalizes_work_kind(self) -> None:
        from kusudaemon.roles.json_io import _repair_common_schema_omissions

        repaired = _repair_common_schema_omissions({"files_touched": "1"}, ESTIMATE_SCHEMA)
        self.assertEqual(repaired.get("work_kind"), "unknown")
        repaired = _repair_common_schema_omissions(
            {"files_touched": "1", "work_kind": "bogus"}, ESTIMATE_SCHEMA
        )
        self.assertEqual(repaired.get("work_kind"), "unknown")
        repaired = _repair_common_schema_omissions(
            {"files_touched": "1", "work_kind": "Procedure"}, ESTIMATE_SCHEMA
        )
        self.assertEqual(repaired.get("work_kind"), "procedure")


if __name__ == "__main__":
    unittest.main()
