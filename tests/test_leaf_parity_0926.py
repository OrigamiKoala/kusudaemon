"""2026-09-26: decomposed leaves at parity with T1, role calls that resume
instead of restarting, no structural probes over a goal-synthesized spine,
and OpenCode boots spaced so they stop colliding on its sqlite store.

Hermetic: no network, no agent binary."""

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

from kusudaemon.adapters import cli_agent  # noqa: E402
from kusudaemon.adapters.cli_agent import classify_cli_failure  # noqa: E402
from kusudaemon.adapters.gptme_adapter import DEFAULT_TOOL_ALLOWLIST  # noqa: E402
from kusudaemon.pipeline.backends import build_writer_adapter  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.pipeline.run_dir import tier_path  # noqa: E402
from kusudaemon.v1.provider import OpenAICompatibleProvider, ProviderDeadlineError  # noqa: E402
from kusudaemon.v1.tree import TaskNode  # noqa: E402
from kusudaemon.v2.survey import SpineUnit, save_spine  # noqa: E402
from kusudaemon.v6.templates import apply_template_to_node, template_for  # noqa: E402
from kusudaemon.v6.tiering import FULL_SCOPE_SCHEMA  # noqa: E402

_GOOD_SCOPE = json.dumps(
    {
        "files_touched": "1",
        "artifacts": 1,
        "answerable_without_exploration": True,
        "work_kind": "document",
        "questions": [],
        "objections": [],
    }
)
_ACTION_SCHEMA = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {"action": {"type": "string"}},
}


class LeafToolParityTest(unittest.TestCase):
    def _env(self, **extra: str) -> dict[str, str]:
        env = {
            "KUSUDAEMON_PROVIDER_CONFIG": "/nonexistent/provider.json",
            "OPENAI_API_KEY": "k",
            "OPENAI_BASE_URL": "https://test.example.com/v1",
            "OPENAI_MODEL": "m",
        }
        env.update(extra)
        return env

    def test_prose_leaf_gets_the_same_tools_as_a_tool_less_t1_node(self) -> None:
        leaf = TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["nonempty"])
        apply_template_to_node(leaf, template=template_for("prose-dominant"))
        self.assertEqual(leaf.tools, ["read", "save"], "premise: the prose template still narrows")
        single = TaskNode(id="single", brief="b", artifact="out/single.md", gates=["nonempty"])
        with patch.dict(os.environ, self._env(), clear=False):
            os.environ.pop("KUSUDAEMON_LEAF_TEMPLATE_TOOLS", None)
            leaf_adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/p", node=leaf, always_grant_web_search=False
            )
            single_adapter = build_writer_adapter(
                "gptme", workspace_path="/tmp/ws", prompt_dir="/tmp/p", node=single, always_grant_web_search=False
            )
        self.assertEqual(leaf_adapter.tool_allowlist, single_adapter.tool_allowlist)
        self.assertEqual(leaf_adapter.tool_allowlist, DEFAULT_TOOL_ALLOWLIST)

    def test_opencode_leaf_is_allowed_full_bash(self) -> None:
        leaf = TaskNode(id="unit-01", brief="b", artifact="out/unit-01.md", gates=["nonempty"])
        apply_template_to_node(leaf, template=template_for("prose-dominant"))
        with tempfile.TemporaryDirectory() as run_dir, patch.dict(os.environ, self._env(), clear=False):
            os.environ.pop("KUSUDAEMON_LEAF_TEMPLATE_TOOLS", None)
            adapter = build_writer_adapter(
                "opencode", workspace_path="/tmp/ws", prompt_dir="/tmp/p", node=leaf, run_dir=run_dir
            )
        self.assertIn('"bash":"allow"', adapter.command_template)


class _Transport:
    """Records every payload; answers from a scripted list."""

    def __init__(self, answers: list[dict]) -> None:
        self.answers = answers
        self.payloads: list[dict] = []

    def __call__(self, url, payload, headers):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.answers[min(len(self.payloads), len(self.answers)) - 1]


def _cut(reasoning: str, content: str = "") -> dict:
    return {
        "choices": [
            {"finish_reason": "length", "message": {"content": content, "reasoning_content": reasoning}}
        ],
        "usage": {"completion_tokens": 999},
    }


def _ok(content: str) -> dict:
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], "usage": {"completion_tokens": 50}}


class RoleCapTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {k: os.environ.pop(k, None) for k in ("KUSUDAEMON_CLASSIFY_MAX_TOKENS", "KUSUDAEMON_ROLE_MAX_TOKENS")}

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_classify_starts_at_16384(self) -> None:
        t = _Transport([_ok(_GOOD_SCOPE)])
        OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "goal"}], FULL_SCOPE_SCHEMA
        )
        self.assertGreaterEqual(t.payloads[0]["max_tokens"], 16384)

    def test_orchestrator_default_cap_is_16384(self) -> None:
        from kusudaemon.v1.orchestrator import ORCHESTRATOR_SCHEMA
        from kusudaemon.v1.provider import call_scope

        answer = json.dumps({"action": "wait", "node_ids": [], "wait_on": ["a"], "reason": "r"})
        t = _Transport([_ok(answer)])
        with call_scope(role="orchestrator"):
            OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
                [{"role": "user", "content": "x"}], ORCHESTRATOR_SCHEMA
            )
        self.assertEqual(t.payloads[0]["max_tokens"], 16384)


class CarryForwardTest(unittest.TestCase):
    def test_truncated_reasoning_is_carried_into_the_retry(self) -> None:
        t = _Transport([_cut("REASONING-TAIL: the answer is go"), _ok('{"action": "go"}')])
        out = OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "plan"}], _ACTION_SCHEMA
        )
        self.assertEqual(out, {"action": "go"})
        retry = t.payloads[1]["messages"]
        self.assertIn("REASONING-TAIL", retry[-2]["content"])
        self.assertEqual(retry[-2]["role"], "assistant")
        self.assertIn("Emit the JSON now", retry[-1]["content"])
        self.assertEqual(t.payloads[1]["max_tokens"], t.payloads[0]["max_tokens"])

    def test_carry_forward_replaces_rather_than_stacks(self) -> None:
        t = _Transport([_cut("first"), _cut("second"), _ok('{"action": "go"}')])
        OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "plan"}], _ACTION_SCHEMA
        )
        third = t.payloads[2]["messages"]
        self.assertEqual(len(third), len(t.payloads[1]["messages"]))
        joined = " ".join(m["content"] for m in third)
        self.assertIn("second", joined)
        self.assertNotIn("first", joined)

    def test_long_reasoning_is_clipped_to_its_tail(self) -> None:
        t = _Transport([_cut("x" * 50_000 + "THE-END"), _ok('{"action": "go"}')])
        OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "plan"}], _ACTION_SCHEMA
        )
        carried = t.payloads[1]["messages"][-2]["content"]
        self.assertIn("THE-END", carried)
        self.assertLess(len(carried), 13_000)

    def test_deadline_partial_is_carried_forward(self) -> None:
        calls = {"n": 0}
        seen: list[list[dict]] = []

        def fake_call(payload, *, stream=False, on_reasoning=None, deadline_s=None):
            calls["n"] += 1
            seen.append(payload["messages"])
            if calls["n"] == 1:
                raise ProviderDeadlineError("deadline", partial="PARTIAL-THOUGHT")
            return _ok('{"action": "go"}')

        provider = OpenAICompatibleProvider(api_key="k", base_url="http://unused", model="m")
        with patch.object(provider, "_call", side_effect=fake_call):
            out = provider.complete_json([{"role": "user", "content": "plan"}], _ACTION_SCHEMA)
        self.assertEqual(out, {"action": "go"})
        self.assertIn("PARTIAL-THOUGHT", seen[1][-2]["content"])

    def test_truncated_scope_with_fragment_json_is_still_retried(self) -> None:
        fragment = 'thinking... e.g. {"files_touched": "unknown", "artifacts": 1, ' \
            '"answerable_without_exploration": false, "questions": [], "objections": []}'
        t = _Transport([_cut("", fragment), _ok(_GOOD_SCOPE)])
        out = OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "goal"}], FULL_SCOPE_SCHEMA
        )
        self.assertEqual(out["files_touched"], "1")
        self.assertEqual(len(t.payloads), 2)


class ExploreOnOutputSpineTest(unittest.TestCase):
    def _driver(self, run_dir: Path, goal: str) -> RecursiveDriver:
        def no_probe(query):  # pragma: no cover - assertion helper
            raise AssertionError("no structural probe expected over a goal-synthesized spine")

        return RecursiveDriver(
            run_dir,
            provider=FakeProvider([]),  # type: ignore[arg-type]
            options=RunOptions(goal=goal),
            writer_adapter_factory=lambda node: (_ for _ in ()).throw(AssertionError("no writer")),
            research_adapter_factory=lambda node, query: (_ for _ in ()).throw(AssertionError("no research")),
            probe_adapter_factory=no_probe,
        )

    @staticmethod
    def _seed_tier(run_dir: Path, **extra) -> None:
        tier_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
        record = {
            "tier": "T2", "measured_tier": "T2", "override": None,
            "needs_intake": False, "needs_explore": True, "signals": {}, "estimate": {}, "ts": 0,
        }
        record.update(extra)
        tier_path(run_dir).write_text(json.dumps(record), encoding="utf-8")

    def test_output_spine_skips_structural_probes(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root:
                driver = self._driver(Path(root) / "run", "Write 52 weekly menus.")
                self._seed_tier(driver.run_dir, spine_source="output")
                save_spine(
                    driver.run_dir,
                    [SpineUnit(id=f"unit-0{i}", label=f"Weeks {i}", start_chunk=i, end_chunk=i, tokens=4000) for i in (1, 2, 3)],
                )
                await driver._phase_explore()
                events = [
                    json.loads(line)
                    for line in (driver.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                skipped = [e for e in events if e.get("type") == "phase_skipped" and e.get("phase") == "structural_exploration"]
                self.assertEqual(len(skipped), 1)
                self.assertIn("synthesized", skipped[0]["reason"])

        asyncio.run(scenario())

    def test_survey_marks_a_synthesized_spine(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root:
                goal = (
                    "Write a weekly diary. Ensure that the diary consists of 52 entries, "
                    "each at least 200 words. Use '#*#' to separate each entry."
                )
                driver = self._driver(Path(root) / "run", goal)
                self._seed_tier(driver.run_dir)
                with patch.dict(os.environ, {"KUSUDAEMON_OUTPUT_SPINE": "1"}):
                    await driver._phase_survey()
                record = json.loads(tier_path(driver.run_dir).read_text(encoding="utf-8"))
                self.assertEqual(record.get("spine_source"), "output")

        asyncio.run(scenario())


class OpenCodeBootTest(unittest.TestCase):
    def test_database_locked_is_transport(self) -> None:
        self.assertEqual(
            classify_cli_failure("Error: Unexpected error\n\ndatabase is locked\n"), "transport"
        )

    def test_boot_slots_are_spaced(self) -> None:
        async def scenario() -> list[float]:
            stamps: list[float] = []

            async def boot() -> None:
                await cli_agent._await_boot_slot()
                stamps.append(time.monotonic())

            await asyncio.gather(boot(), boot(), boot())
            return sorted(stamps)

        with patch.dict(os.environ, {"KUSUDAEMON_OPENCODE_BOOT_GAP_S": "0.2"}):
            cli_agent._LAST_BOOT[0] = 0.0
            stamps = asyncio.run(scenario())
        self.assertGreaterEqual(stamps[1] - stamps[0], 0.18)
        self.assertGreaterEqual(stamps[2] - stamps[1], 0.18)

    def test_only_opencode_waits(self) -> None:
        from kusudaemon.adapters.opencode import OpenCodeAdapter

        self.assertTrue(OpenCodeAdapter.boot_gate)
        self.assertFalse(cli_agent.CommandAgentAdapter.boot_gate)


if __name__ == "__main__":
    unittest.main()
