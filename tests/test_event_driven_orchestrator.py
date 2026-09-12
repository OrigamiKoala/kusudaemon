"""PLAN-SWEEP-REPAIR.md §L — the event-driven orchestrator.

``dispatch_policy="orchestrator"``: one agent whose only job is deciding when
to dispatch or redispatch. It is called at the start and every time a node
finishes, told what finished and how, and answers ``dispatch`` (up to the free
slots) or ``wait`` (naming the running nodes it waits on). Failed nodes return
to it instead of retrying in place. Terminal states stay code-decided.

All hermetic: scripted in-process writers and a scripted orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.adapters.base import AgentAdapter  # noqa: E402
from kusudaemon.environment.base import Environment  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.pipeline.admission import RunAdmissionController  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v1.json_schema import validate  # noqa: E402
from kusudaemon.v1 import orchestrator as orch_mod  # noqa: E402
from kusudaemon.v1 import provider as provider_mod  # noqa: E402
from kusudaemon.v1.orchestrator import (  # noqa: E402
    ORCHESTRATOR_SCHEMA,
    build_orchestrator_messages,
    parse_orchestrator_decision,
)
from kusudaemon.v1.provider import OpenAICompatibleProvider, ProviderError  # noqa: E402
from kusudaemon.v1.round_loop import _last_orchestrator_decision, run_round_loop  # noqa: E402
from kusudaemon.v1.tree import TaskTree  # noqa: E402
from kusudaemon.v1.run_dir import create_run_dir, events_path, tree_path  # noqa: E402

OK = "a real artifact body.\n"


class _Timeline:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.starts: dict[str, list[float]] = {}
        self.ends: dict[str, list[float]] = {}
        self.order: list[str] = []

    def now(self) -> float:
        return time.monotonic() - self.t0


class _ScriptedAdapter(AgentAdapter):
    has_file_tools = True
    supports_session_resume = False

    def __init__(self, run_dir: Path, node_id: str, script: list[tuple[float, str]], tl: _Timeline) -> None:
        self._path = run_dir / "out" / f"{node_id}.md"
        self._node_id, self._script, self._tl = node_id, script, tl

    async def run_episode(self, prompt: str, env: Environment, budget: EpisodeBudget,
                          live_trajectory_path: Path | None = None, **kwargs: Any) -> EpisodeResult:
        tl, nid = self._tl, self._node_id
        attempt = len(tl.starts.setdefault(nid, []))
        tl.starts[nid].append(tl.now())
        tl.order.append(nid)
        try:
            delay, body = self._script[min(attempt, len(self._script) - 1)]
            await asyncio.sleep(delay)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(body, encoding="utf-8")
            return EpisodeResult(status="done", duration_ms=int(delay * 1000))
        finally:
            tl.ends.setdefault(nid, []).append(tl.now())


def section(prompt: str, title: str) -> list[str]:
    """Node ids listed under one heading of the orchestrator's state message."""
    m = re.search(rf"^{re.escape(title)} \(\d+\):\n((?:- .*\n?)*)", prompt, re.M)
    if not m:
        return []
    return [
        ln[2:].split(" ", 1)[0]
        for ln in m.group(1).splitlines()
        if not ln.startswith("- (none)") and not ln.startswith("- …")
    ]


class ScriptedOrchestrator:
    """Scripted answers. Errors raised inside ``decide`` (including test
    assertions) are recorded: since §L.4 the loop falls back instead of
    failing, so ``_run`` re-raises them to keep tests honest."""

    def __init__(self, decide: Callable[[int, str], dict[str, Any]]) -> None:
        self.decide = decide
        self.prompts: list[str] = []
        self.errors: list[BaseException] = []
        self.scopes: list[tuple[str | None, int | None]] = []

    def complete_json(self, messages, schema, **kwargs) -> dict[str, Any]:
        try:
            assert schema is ORCHESTRATOR_SCHEMA
            self.scopes.append((provider_mod._scoped_role(None), provider_mod._scoped_max_tokens()))
            prompt = messages[-1]["content"]
            self.prompts.append(prompt)
            payload = self.decide(len(self.prompts), prompt)
            assert not validate(payload, schema), validate(payload, schema)
            return payload
        except BaseException as exc:
            self.errors.append(exc)
            raise


def _run(
    root: Path,
    nodes: list[dict],
    scripts: dict[str, list[tuple[float, str]]],
    orch,
    *,
    max_parallel: int,
    allow_fallback: bool = False,
):
    run_dir = create_run_dir(root, "run")
    tree_path(run_dir).write_text(
        json.dumps([{"artifact": f"out/{n['id']}.md", "brief": n["id"], "gates": ["nonempty"], **n} for n in nodes]),
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
            provider=orch,
            prompt_for_node=lambda node: f"do {node.id}",
            writer_budget=EpisodeBudget(max_duration_seconds=60),
            max_parallel=max_parallel,
            dispatch_policy="orchestrator",
            admission_controller=RunAdmissionController(concurrency=8, initial_wave_cap=max_parallel),
        )
    )
    events = EventLog(events_path(run_dir)).read_all()
    for err in getattr(orch, "errors", []):
        raise err
    if not allow_fallback:
        failed = [e for e in events if e["type"] == "orchestrator_call_failed"]
        assert not failed, failed
    return run_dir, tree, tl, events


def dispatch(*ids: str, reason: str = "go", wait_on: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"action": "dispatch", "node_ids": list(ids), "wait_on": list(wait_on), "reason": reason}


def wait(*ids: str, reason: str = "hold") -> dict[str, Any]:
    return {"action": "wait", "node_ids": [], "wait_on": list(ids), "reason": reason}


class EventDrivenLoopTest(unittest.TestCase):
    def test_called_on_every_completion_and_its_choice_is_used(self) -> None:
        # It picks the LAST dispatchable node each time: reverse document order.
        orch = ScriptedOrchestrator(lambda n, p: dispatch(section(p, "dispatchable now")[-1]))
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, _ = _run(
                Path(td), [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                {k: [(0.0, OK)] for k in "abc"}, orch, max_parallel=1,
            )
        self.assertEqual(tl.order, ["c", "b", "a"])
        self.assertTrue(all(tree.nodes[k].status == "passed" for k in "abc"))
        self.assertEqual(len(orch.prompts), 3)
        self.assertIn("execution is starting", orch.prompts[0])
        self.assertIn("- c PASSED", orch.prompts[1])
        self.assertIn("- b PASSED", orch.prompts[2])

    def test_wait_holds_work_until_the_named_node_finishes(self) -> None:
        def decide(n: int, p: str) -> dict[str, Any]:
            if n == 1:
                return dispatch("a", "b")
            if n == 2:
                self.assertIn("- b PASSED", p)
                self.assertEqual(section(p, "running now"), ["a"])
                return wait("a", reason="c continues a")
            self.assertIn("your previous decision:", p)
            self.assertIn("wait, waiting on ['a']", p)
            self.assertIn("reason: c continues a", p)
            return dispatch("c")

        orch = ScriptedOrchestrator(decide)
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                {"a": [(0.8, OK)], "b": [(0.05, OK)], "c": [(0.05, OK)]}, orch, max_parallel=2,
            )
        self.assertEqual(tree.nodes["c"].status, "passed")
        self.assertGreaterEqual(tl.starts["c"][0], tl.ends["a"][0])
        self.assertEqual(len(orch.prompts), 3)
        waits = [e for e in events if e["type"] == "orchestrator_decision" and e["action"] == "wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["wait_on"], ["a"])

    def test_failed_node_returns_to_the_orchestrator_instead_of_retrying_in_place(self) -> None:
        def decide(n: int, p: str) -> dict[str, Any]:
            if n == 1:
                return dispatch("x", "y")
            if n == 2:
                self.assertIn("- x FAILED attempt 1 of 3", p)
                self.assertIn('x attempts=1/3 last_defect="nonempty', p)
                return wait("y", reason="retry x after y")
            return dispatch("x")

        orch = ScriptedOrchestrator(decide)
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(
                Path(td), [{"id": "x"}, {"id": "y"}],
                {"x": [(0.0, ""), (0.0, OK)], "y": [(1.5, OK)]}, orch, max_parallel=2,
            )
        self.assertEqual(tree.nodes["x"].status, "passed")
        self.assertEqual(len(tl.starts["x"]), 2)
        self.assertGreaterEqual(tl.starts["x"][1], tl.ends["y"][0])
        redispatched = [e for e in events if e["type"] == "node_dispatch_decided" and e.get("redispatch")]
        self.assertEqual([e["node_id"] for e in redispatched], ["x"])

    def test_attempt_cap_and_terminal_escalation_stay_code_decided(self) -> None:
        orch = ScriptedOrchestrator(lambda n, p: dispatch(*section(p, "dispatchable now")))
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(Path(td), [{"id": "bad"}], {"bad": [(0.0, "")]}, orch, max_parallel=1)
        self.assertEqual(tree.nodes["bad"].status, "blocked")
        self.assertEqual(len(tl.starts["bad"]), 3)
        self.assertEqual(len(orch.prompts), 3)  # start + two failures; the cap is not asked
        self.assertTrue(any(e["type"] == "run_escalated" for e in events))

    def test_no_call_when_the_only_possible_answer_is_wait(self) -> None:
        # b declares a dependency on a: while a runs nothing is dispatchable.
        orch = ScriptedOrchestrator(lambda n, p: dispatch(*section(p, "dispatchable now")))
        with tempfile.TemporaryDirectory() as td:
            _, tree, _, _ = _run(
                Path(td), [{"id": "a"}, {"id": "b", "depends_on": ["a"]}],
                {"a": [(0.1, OK)], "b": [(0.0, OK)]}, orch, max_parallel=2,
            )
        self.assertEqual(tree.nodes["b"].status, "passed")
        self.assertEqual(len(orch.prompts), 2)
        self.assertIn("held by declared dependencies (1):", orch.prompts[0])
        self.assertIn("- b waits on a (", orch.prompts[0])

    def test_wait_with_nothing_running_is_corrected_to_a_dispatch(self) -> None:
        orch = ScriptedOrchestrator(lambda n, p: wait("ghost"))
        with tempfile.TemporaryDirectory() as td:
            _, tree, _, events = _run(Path(td), [{"id": "a"}], {"a": [(0.0, OK)]}, orch, max_parallel=1)
        self.assertEqual(tree.nodes["a"].status, "passed")
        decision = next(e for e in events if e["type"] == "orchestrator_decision")
        self.assertEqual(decision["action"], "dispatch")
        self.assertTrue(any("never wake" in c for c in decision["corrections"]))

    def test_provider_failure_falls_back_to_document_order(self) -> None:
        class Down:
            def complete_json(self, *a, **k):
                raise ProviderError("connection refused")

        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}], {"a": [(0.0, OK)], "b": [(0.0, OK)]}, Down(), max_parallel=1,
                allow_fallback=True,
            )
        self.assertEqual(tl.order, ["a", "b"])
        self.assertTrue(any(e["type"] == "orchestrator_call_failed" for e in events))


class RegressionFixesTest(unittest.TestCase):
    """PLAN-SWEEP-REPAIR.md §L.2-§L.7."""

    def tearDown(self) -> None:
        for name in ("KUSUDAEMON_ORCHESTRATOR_DEADLINE_S", "KUSUDAEMON_ORCHESTRATOR_MAX_TOKENS"):
            import os
            os.environ.pop(name, None)

    def test_schema_is_strict_mode_valid(self) -> None:
        def walk(schema: dict[str, Any]) -> None:
            if schema.get("type") == "object":
                self.assertFalse(schema.get("additionalProperties", True))
                self.assertEqual(set(schema.get("required", [])), set(schema.get("properties", {})))
                for sub in schema["properties"].values():
                    walk(sub)
            for banned in ("minLength", "maxLength", "pattern", "format"):
                self.assertNotIn(banned, schema)
            if "items" in schema:
                walk(schema["items"])

        walk(ORCHESTRATOR_SCHEMA)

    def test_call_is_scoped_to_orchestrator_role_and_bounded_tokens(self) -> None:
        import os
        os.environ["KUSUDAEMON_ORCHESTRATOR_MAX_TOKENS"] = "700"
        orch = ScriptedOrchestrator(lambda n, p: dispatch(*section(p, "dispatchable now")))
        with tempfile.TemporaryDirectory() as td:
            _run(Path(td), [{"id": "a"}], {"a": [(0.0, OK)]}, orch, max_parallel=1)
        self.assertEqual(orch.scopes, [("orchestrator", 700)])
        # The scope is thread-local and closed: this thread sees nothing.
        self.assertIsNone(provider_mod._scoped_max_tokens())

    def test_http_provider_honours_the_scope(self) -> None:
        sent: list[dict[str, Any]] = []
        ledger_rows: list[dict[str, Any]] = []

        class Ledger:
            def record(self, **kw):
                ledger_rows.append(kw)

        def transport(url, payload, headers):
            sent.append(payload)
            content = json.dumps({"action": "wait", "node_ids": [], "wait_on": [], "reason": "r"})
            return {"choices": [{"message": {"content": content}}]}

        prov = OpenAICompatibleProvider(transport=transport, api_key="unused", base_retry_delay=0.0,
                                        base_url="http://unused.invalid/v1", model="m",
                                        role="unknown", cost_ledger=Ledger())
        with provider_mod.call_scope(role="orchestrator", max_tokens=512):
            prov.complete_json([{"role": "user", "content": "hi"}], ORCHESTRATOR_SCHEMA)
        prov.complete_json([{"role": "user", "content": "hi"}], ORCHESTRATOR_SCHEMA)
        self.assertEqual(sent[0]["max_tokens"], 512)
        self.assertEqual(ledger_rows[0]["role"], "orchestrator")
        self.assertEqual(ledger_rows[-1]["role"], "unknown")

    def test_deadline_falls_back_and_never_stacks_calls(self) -> None:
        import os
        os.environ["KUSUDAEMON_ORCHESTRATOR_DEADLINE_S"] = "0.3"

        class Slow:
            def __init__(self) -> None:
                self.calls = 0

            def complete_json(self, messages, schema, **kw):
                self.calls += 1
                time.sleep(1.5)
                return dispatch("a")

        slow = Slow()
        with tempfile.TemporaryDirectory() as td:
            started = time.monotonic()
            _, tree, tl, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}], {"a": [(0.0, OK)], "b": [(0.0, OK)]}, slow,
                max_parallel=1, allow_fallback=True,
            )
        self.assertEqual(tl.order, ["a", "b"])
        self.assertEqual(slow.calls, 1)  # b's decision did not start a second call
        failed = [e for e in events if e["type"] == "orchestrator_call_failed"]
        self.assertEqual(len(failed), 2)
        self.assertIn("no answer within", failed[0]["detail"])
        self.assertIn("still running", failed[1]["detail"])
        # a was dispatched ~0.3 s in, not after the 1.5 s call.
        self.assertLess(tl.starts["a"][0], 1.0)

    def test_any_exception_from_the_call_falls_back(self) -> None:
        class Broken:
            def complete_json(self, *a, **k):
                raise KeyError("choices")

        with tempfile.TemporaryDirectory() as td:
            _, tree, _, events = _run(
                Path(td), [{"id": "a"}], {"a": [(0.0, OK)]}, Broken(), max_parallel=1, allow_fallback=True,
            )
        self.assertEqual(tree.nodes["a"].status, "passed")
        self.assertIn("KeyError", next(e for e in events if e["type"] == "orchestrator_call_failed")["detail"])

    def test_loop_error_drains_writers_instead_of_cancelling_them(self) -> None:
        real = orch_mod.build_orchestrator_messages
        count = {"n": 0}

        def flaky(*a, **k):
            count["n"] += 1
            if count["n"] == 2:
                raise RuntimeError("bug in the state renderer")
            return real(*a, **k)

        orch = ScriptedOrchestrator(lambda n, p: dispatch("a", "b"))
        orch_mod.build_orchestrator_messages = flaky
        try:
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(RuntimeError):
                    # c keeps a decision pending when b finishes, so the
                    # renderer runs (and breaks) while a is still writing.
                    _run(Path(td), [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                         {"a": [(0.8, OK)], "b": [(0.05, OK)], "c": [(0.0, OK)]}, orch, max_parallel=2)
                run_dir = Path(td) / "run"
                events = EventLog(events_path(run_dir)).read_all()
                tree = TaskTree.load(tree_path(run_dir))
        finally:
            orch_mod.build_orchestrator_messages = real
        drained = [e for e in events if e["type"] == "round_loop_error_draining"]
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["in_flight"], ["a"])
        self.assertEqual(tree.nodes["a"].status, "passed")  # finished, not killed

    def test_prompt_is_bounded_and_lists_retries_first(self) -> None:
        nodes = [
            {"id": f"n{i:02d}", "brief": f"n{i:02d}", "artifact": f"out/n{i:02d}.md", "gates": ["nonempty"]}
            for i in range(50)
        ]
        nodes[40]["attempts"] = 1
        nodes[40]["last_defect"] = "nonempty: artifact is empty"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tree.json"
            path.write_text(json.dumps(nodes), encoding="utf-8")
            tree = TaskTree.load(path)
            ready = tree.ready_nodes()
            msgs = build_orchestrator_messages(
                tree, events=[], ready=ready, in_flight=[], free_slots=3, max_slots=3,
                max_attempts=3, manifest_path=str(Path(td) / "manifest.jsonl"), decision_index=0,
                first_call=True, max_listed=5,
            )
        prompt = msgs[-1]["content"]
        listed = section(prompt, "dispatchable now")
        self.assertEqual(listed, ["n40", "n00", "n01", "n02", "n03"])
        self.assertIn("… 45 more dispatchable, not listed", prompt)
        self.assertIn("dispatchable now (50):", prompt)

    def test_previous_decision_is_recovered_from_events_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run_dir = create_run_dir(Path(td), "run")
            log = EventLog(events_path(run_dir))
            log.append({"type": "orchestrator_decision", "round": 4, "action": "wait", "node_ids": [],
                        "wait_on": ["unit-02"], "reason": "weeks follow", "corrections": [], "node_id": "-",
                        "role": "orchestrator"})
            log.append({"type": "node_dispatched", "node_id": "unit-02", "role": "writer"})
            last = _last_orchestrator_decision(run_dir)
        self.assertEqual((last["round"], last["action"], last["wait_on"]), (4, "wait", ["unit-02"]))

    def test_unexplained_idle_slots_are_filled(self) -> None:
        orch = ScriptedOrchestrator(lambda n, p: dispatch("a") if n == 1 else dispatch(*section(p, "dispatchable now")))
        with tempfile.TemporaryDirectory() as td:
            _, tree, tl, events = _run(
                Path(td), [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                {k: [(0.3, OK)] for k in "abc"}, orch, max_parallel=3,
            )
        first = next(e for e in events if e["type"] == "orchestrator_decision")
        self.assertEqual(first["node_ids"], ["a", "b", "c"])
        self.assertTrue(any("filled them in document order" in c for c in first["corrections"]))
        self.assertLess(tl.starts["c"][0], tl.ends["a"][0])


class ParseDecisionTest(unittest.TestCase):
    def test_partial_dispatch_with_wait_on_keeps_slots_free(self) -> None:
        d = parse_orchestrator_decision(
            dispatch("a", wait_on=("a",)), ready=["a", "b", "c"], in_flight=[], free_slots=3
        )
        self.assertEqual((d.action, d.node_ids, d.wait_on, d.corrections), ("dispatch", ["a"], ["a"], []))

    def test_partial_dispatch_without_wait_on_is_filled(self) -> None:
        d = parse_orchestrator_decision(dispatch("b"), ready=["a", "b", "c"], in_flight=["r"], free_slots=2)
        self.assertEqual(d.node_ids, ["b", "a"])
        self.assertTrue(d.corrections)

    def test_dispatch_trims_to_free_slots_and_drops_unknown_ids(self) -> None:
        d = parse_orchestrator_decision(
            dispatch("z", "a", "b", "c"), ready=["a", "b", "c"], in_flight=[], free_slots=2
        )
        self.assertEqual((d.action, d.node_ids), ("dispatch", ["a", "b"]))
        self.assertEqual(len(d.corrections), 2)

    def test_dispatch_of_nothing_valid_waits_when_something_runs(self) -> None:
        d = parse_orchestrator_decision(dispatch("q"), ready=["a"], in_flight=["r"], free_slots=1)
        self.assertEqual((d.action, d.wait_on), ("wait", ["r"]))

    def test_wait_on_unknown_node_waits_on_all_running(self) -> None:
        d = parse_orchestrator_decision(wait("q"), ready=["a"], in_flight=["r", "s"], free_slots=1)
        self.assertEqual((d.action, d.wait_on), ("wait", ["r", "s"]))
        self.assertTrue(d.corrections)

    def test_valid_wait_is_untouched(self) -> None:
        d = parse_orchestrator_decision(wait("r"), ready=["a"], in_flight=["r", "s"], free_slots=1)
        self.assertEqual((d.action, d.wait_on, d.corrections), ("wait", ["r"], []))


if __name__ == "__main__":
    unittest.main()
