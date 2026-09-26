"""2026-09-26: degenerate model output (NIM's nemotron-3.5-lightning).

Covers ``degeneration.py`` against the shapes seen in the run traces, role
sampling (no more greedy role calls), role calls that restart instead of
carrying garbage forward, the agent worker stopping an episode whose output
turned to garbage, the runner refusing to resume such a session, and the
classify schema's ``artifacts`` bound.

Hermetic: no network, no agent binary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_adapter import FakeStreamAgentAdapter  # noqa: E402
from kusudaemon.adapters.cli_agent import classify_cli_failure  # noqa: E402
from kusudaemon.degeneration import DegenerationMonitor, degeneration_reason  # noqa: E402
from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.roles.json_io import _repair_common_schema_omissions  # noqa: E402
from kusudaemon.types import EpisodeBudget  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, ensure_node_trace_path  # noqa: E402
from kusudaemon.v0.runner import run_node  # noqa: E402
from kusudaemon.v1.json_schema import validate  # noqa: E402
from kusudaemon.v1.provider import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderError,
    _carry_forward,
    call_scope,
    role_sampling,
)
from kusudaemon.v6.tiering import ESTIMATE_SCHEMA, FULL_SCOPE_SCHEMA  # noqa: E402

_WORKER = _REPO_ROOT / "src" / "kusudaemon" / "adapters" / "_agent_worker.py"
FAKE_CLI = _REPO_ROOT / "tests" / "fixtures" / "fake_stream_agent.py"

# Shapes taken from the traces (shortened, same character mix).
SOUP = (
    "Let story алatlama should beschäd supplies nil. Nameुल หมาย için tragen, "
    "excess. Note: 11 (used selfsec day lightси са hut. anywayರಿ μικρόبر v9 "
    "min various (Boom handlLead Pain on any rbo di knisbег, s. округبر the_Wrl. "
    "వినxe vηση,J? Iulsionara, minutesA92nrape?fsfhA?: the word uip. nilInstead"
)
THINK_STORM = "I" + "</think>" * 200
ZERO_LOOP = "Actually, the field is maximum: the such the many ways " + "0. " * 600
SENTENCE_LOOP = "I need to check if every floor from 51 to 75 has a section. " * 60
HEALTHY = " ".join(
    f"Week {i}: the menu pairs a braised dish with seasonal greens, and dessert {i * 3} "
    f"uses whatever fruit the market had on day {i + 2}." for i in range(1, 80)
)
JAPANESE = "今日はとても良い天気ですね。カタカナも使います。漢字とひらがなとカタカナ。" * 20
# A PDF stream quoted by a writer that read a binary file.
BINARY = "".join(chr(c) for c in range(1, 32) if c not in (9, 10, 13)) * 10 + "ʚ뤴ѓրᄀ" * 60

_ACTION_SCHEMA = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {"action": {"type": "string"}},
}


def _env_without(*prefixes: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}


class DetectorTest(unittest.TestCase):
    def test_trace_shapes_are_caught(self) -> None:
        self.assertEqual(degeneration_reason(SOUP), "script_soup")
        self.assertEqual(degeneration_reason(THINK_STORM), "think_tag_leak")
        self.assertEqual(degeneration_reason(ZERO_LOOP), "repetition_loop")
        self.assertEqual(degeneration_reason(SENTENCE_LOOP), "repetition_loop")

    def test_ordinary_text_passes(self) -> None:
        for text in (HEALTHY, JAPANESE, "", "short"):
            self.assertIsNone(degeneration_reason(text), text[:40])

    def test_talking_about_think_tags_is_not_a_storm(self) -> None:
        text = HEALTHY + " The worker strips `<think>` spans; a `</think>` without an opener is kept. "
        self.assertIsNone(degeneration_reason(text * 2))

    def test_quoted_binary_is_not_soup(self) -> None:
        self.assertIsNone(degeneration_reason(BINARY))

    def test_only_the_tail_counts(self) -> None:
        self.assertIsNone(degeneration_reason(SOUP + HEALTHY))
        self.assertEqual(degeneration_reason(HEALTHY + SOUP), "script_soup")

    def test_monitor_reports_once_on_a_stream(self) -> None:
        monitor = DegenerationMonitor()
        hits = [monitor.feed(HEALTHY[i:i + 50]) for i in range(0, len(HEALTHY), 50)]
        self.assertEqual([h for h in hits if h], [])
        # A loop is reported once it fills most of the window (~2k chars).
        loop = ZERO_LOOP * 2
        hits = [monitor.feed(loop[i:i + 30]) for i in range(0, len(loop), 30)]
        self.assertEqual([h for h in hits if h], ["repetition_loop"])
        self.assertEqual(monitor.reason, "repetition_loop")

    def test_kill_switch(self) -> None:
        from kusudaemon.degeneration import guard_enabled

        with patch.dict(os.environ, {"KUSUDAEMON_DEGENERATION_GUARD": "0"}):
            self.assertFalse(guard_enabled())
            self.assertEqual(_carry_forward(ZERO_LOOP, "emit")[0]["role"], "assistant")


class _Transport:
    def __init__(self, answers: list[dict]) -> None:
        self.answers = answers
        self.payloads: list[dict] = []

    def __call__(self, url, payload, headers):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.answers[min(len(self.payloads), len(self.answers)) - 1]


class _Scripted:
    """A streaming transport that plays one step per call."""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, url, payload, headers, on_reasoning, deadline_s=None):
        self.payloads.append(json.loads(json.dumps(payload)))
        step = self.steps.pop(0)
        return step(on_reasoning) if callable(step) else step


def _ok(content: str, reasoning: str = "") -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content, "reasoning_content": reasoning}}],
        "usage": {"completion_tokens": 50},
    }


def _streams(text: str, answer: str):
    def play(on_reasoning):
        for i in range(0, len(text), 40):
            if on_reasoning is not None:
                on_reasoning(text[i:i + 40])
        return _ok(answer, text)

    return play


class RoleSamplingTest(unittest.TestCase):
    def test_role_calls_are_no_longer_greedy(self) -> None:
        with patch.dict(os.environ, _env_without("KUSUDAEMON_ROLE_T"), clear=True):
            self.assertEqual(role_sampling(), (0.6, 0.95))
            t = _Transport([_ok('{"action": "go"}')])
            OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
                [{"role": "user", "content": "x"}], _ACTION_SCHEMA
            )
        self.assertEqual(t.payloads[0]["temperature"], 0.6)
        self.assertEqual(t.payloads[0]["top_p"], 0.95)

    def test_explicit_temperature_and_env_overrides(self) -> None:
        env = _env_without("KUSUDAEMON_ROLE_T")
        env.update({"KUSUDAEMON_ROLE_TEMPERATURE": "0.2", "KUSUDAEMON_ROLE_TOP_P": "none"})
        with patch.dict(os.environ, env, clear=True):
            t = _Transport([_ok('{"action": "go"}')])
            provider = OpenAICompatibleProvider(transport=t, api_key="k", model="m")
            provider.complete_json([{"role": "user", "content": "x"}], _ACTION_SCHEMA)
            provider.complete_json([{"role": "user", "content": "x"}], _ACTION_SCHEMA, temperature=0.7)
            provider.complete([{"role": "user", "content": "x"}])
        self.assertEqual([p["temperature"] for p in t.payloads], [0.2, 0.7, 0.2])
        self.assertTrue(all("top_p" not in p for p in t.payloads))


class RoleCallRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.dict(os.environ, _env_without("KUSUDAEMON_DEGENERAT", "KUSUDAEMON_REASONING"), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _provider(self, transport) -> tuple[OpenAICompatibleProvider, list[dict]]:
        provider = OpenAICompatibleProvider(api_key="k", base_url="http://unused")
        provider._stream_transport_impl = transport
        events: list[dict] = []
        provider.set_degenerate_hook(events.append)
        return provider, events

    def test_carry_forward_drops_a_garbage_tail(self) -> None:
        self.assertEqual(_carry_forward(ZERO_LOOP, "emit"), [{"role": "user", "content": "emit"}])
        self.assertEqual(_carry_forward(HEALTHY, "emit")[0]["role"], "assistant")

    def test_streamed_garbage_is_cut_and_restarted_fresh(self) -> None:
        seen: list[str] = []
        t = _Scripted(_streams(ZERO_LOOP * 3, "never"), _ok('{"action": "go"}'))
        provider, events = self._provider(t)
        with call_scope(role="planner"):
            out = provider.complete_json(
                [{"role": "user", "content": "plan"}], _ACTION_SCHEMA, streaming=True, on_reasoning=seen.append
            )
        self.assertEqual(out, {"action": "go"})
        # Cut early: well before the whole 5k-char loop streamed.
        self.assertLess(len("".join(seen)), len(ZERO_LOOP * 3))
        # The retry starts from the original messages: no garbage carried.
        second = " ".join(m["content"] for m in t.payloads[1]["messages"])
        self.assertNotIn("0. 0. 0.", second)
        self.assertEqual(len(t.payloads[1]["messages"]), len(t.payloads[0]["messages"]))
        self.assertEqual([(e["reason"], e["action"], e["role"]) for e in events],
                         [("repetition_loop", "restart", "planner")])

    def test_garbage_content_is_not_reprompted_with(self) -> None:
        t = _Transport([_ok(SOUP), _ok('{"action": "go"}')])
        provider = OpenAICompatibleProvider(transport=t, api_key="k", model="m")
        out = provider.complete_json([{"role": "user", "content": "x"}], _ACTION_SCHEMA)
        self.assertEqual(out, {"action": "go"})
        self.assertNotIn("That did not validate", json.dumps(t.payloads[1]["messages"]))
        self.assertNotIn("beschäd", json.dumps(t.payloads[1]["messages"], ensure_ascii=False))

    def test_valid_json_after_garbage_reasoning_is_kept(self) -> None:
        t = _Transport([_ok('{"action": "go"}', reasoning=SOUP)])
        out = OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "x"}], _ACTION_SCHEMA
        )
        self.assertEqual(out, {"action": "go"})
        self.assertEqual(len(t.payloads), 1)

    def test_restarts_are_bounded(self) -> None:
        with patch.dict(os.environ, {"KUSUDAEMON_DEGENERATE_RETRIES": "1"}):
            t = _Scripted(*[_streams(THINK_STORM * 2, "never") for _ in range(2)])
            provider, events = self._provider(t)
            with self.assertRaises(ProviderError):
                provider.complete_json([{"role": "user", "content": "x"}], _ACTION_SCHEMA, streaming=True)
        self.assertEqual(len(t.payloads), 2)
        self.assertEqual([e["action"] for e in events], ["restart", "gave_up"])

    def test_non_streaming_retry_keeps_reasoning_settings(self) -> None:
        # ``reasoning`` used to be rebound to the response's reasoning text,
        # so the next attempt's make_payload read ``.thinking_off`` off a str.
        answers = [_ok("not json", reasoning="thinking it over"), _ok('{"action": "go"}')]
        t = _Transport(answers)
        seen: list[str] = []
        out = OpenAICompatibleProvider(transport=t, api_key="k", model="m").complete_json(
            [{"role": "user", "content": "x"}], _ACTION_SCHEMA, on_reasoning=seen.append
        )
        self.assertEqual(out, {"action": "go"})
        self.assertEqual(seen, ["thinking it over"])


def _run_worker(code: str) -> subprocess.CompletedProcess:
    child = f'{sys.executable} -c "{code}"'
    return subprocess.run(
        [sys.executable, str(_WORKER), "--format", "opencode", "--", *shlex.split(child)],
        input="",
        capture_output=True,
        text=True,
        timeout=60,
        env=_env_without("KUSUDAEMON_DEGENERATION"),
    )


class WorkerGuardTest(unittest.TestCase):
    def test_garbage_reasoning_stops_the_episode(self) -> None:
        code = (
            "import sys,json,time\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'step-start','sessionID':'ses_x'}), flush=True)\n"
            f"print(json.dumps({{'type':'reasoning','text':{SOUP!r}}}), flush=True)\n"
            "time.sleep(30)\n"
            "print(json.dumps({'type':'text','text':'SHOULD NOT APPEAR'}), flush=True)\n"
        )
        proc = _run_worker(code.replace('"', '\\"'))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("SHOULD NOT APPEAR", proc.stdout)
        self.assertIn("degenerate model output (script_soup)", proc.stderr)
        self.assertEqual(classify_cli_failure(proc.stderr), "transport")
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
        self.assertTrue(any(r.get("type") == "thinking" and "beschäd" in r.get("content", "") for r in rows))

    def test_healthy_episode_and_tool_output_pass(self) -> None:
        code = (
            "import sys,json\n"
            "sys.stdin.read()\n"
            f"print(json.dumps({{'type':'reasoning','text':{HEALTHY[:600]!r}}}))\n"
            f"print(json.dumps({{'type':'tool','tool':'read','state':{{'status':'completed','input':{{}},'output':{SOUP!r}}}}}))\n"
            "print(json.dumps({'type':'text','text':'done'}))\n"
        )
        proc = _run_worker(code.replace('"', '\\"'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("done", proc.stdout)


class ResumeGuardTest(unittest.TestCase):
    def _run(self, root: Path, events: list[dict], trace_rows: list[dict]) -> list[dict]:
        node_id = "writer_1"
        run_dir = create_run_dir(root, "run")
        log = EventLog(run_dir / "events.jsonl")
        for event in [
            {"node_id": node_id, "role": "writer", "round": 0, "type": "node_dispatched"},
            {"node_id": node_id, "role": "writer", "round": 0, "type": "session_captured", "session_id": "ses_old"},
            *events,
        ]:
            log.append(event)
        trace = ensure_node_trace_path(run_dir, node_id)
        trace.write_text("".join(json.dumps(r) + "\n" for r in trace_rows), encoding="utf-8")
        prompt_dir = root / "prompts"
        prompt_dir.mkdir()
        adapter = FakeStreamAgentAdapter(
            script_path=str(FAKE_CLI),
            pidfile=str(root / "w.pid"),
            prompt_dir=str(prompt_dir),
            workspace_path=str(run_dir),
        )
        with patch.dict(os.environ, _env_without("KUSUDAEMON_DEGENERATION", "KUSUDAEMON_WRITER_FRESH"), clear=True):
            result = asyncio.run(
                run_node(
                    run_dir, node_id, "do the task", adapter,
                    LocalEnvironment(tmp_dir=str(prompt_dir)), EpisodeBudget(max_duration_seconds=30),
                )
            )
        self.assertEqual(result.status, "done")
        return [e for e in log.read_all() if e["type"] == "node_redispatched"]

    def test_garbage_tail_dispatches_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            redispatched = self._run(
                Path(td), [], [{"type": "thinking", "content": HEALTHY}, {"type": "thinking", "content": THINK_STORM}]
            )
        self.assertEqual([e["reason"] for e in redispatched], ["session_degenerate"])
        self.assertIn("think_tag_leak", redispatched[0]["detail"])

    def test_worker_kill_dispatches_fresh(self) -> None:
        stopped = {"node_id": "writer_1", "role": "writer", "round": 0, "type": "episode_completed",
                   "status": "transport", "error": "kusudaemon agent worker: degenerate model output (script_soup)"}
        failed = {"node_id": "writer_1", "role": "harness", "round": 0, "type": "node_transport"}
        with tempfile.TemporaryDirectory() as td:
            redispatched = self._run(Path(td), [stopped, failed], [])
        self.assertEqual([e["reason"] for e in redispatched], ["session_degenerate"])

    def test_healthy_tail_still_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            redispatched = self._run(Path(td), [], [{"type": "thinking", "content": HEALTHY}])
        self.assertEqual([e["reason"] for e in redispatched], ["resumed_session"])


class ArtifactsBoundTest(unittest.TestCase):
    def test_fifty_two_weeks_fit_the_schema(self) -> None:
        for schema in (ESTIMATE_SCHEMA, FULL_SCOPE_SCHEMA):
            payload = {"files_touched": "1", "artifacts": 52, "answerable_without_exploration": True,
                       "questions": [], "objections": []}
            if schema is ESTIMATE_SCHEMA:
                payload.pop("questions")
                payload.pop("objections")
            self.assertEqual(validate(payload, schema), [])
            repaired = _repair_common_schema_omissions(dict(payload), schema)
            self.assertEqual(repaired["artifacts"], 52)


if __name__ == "__main__":
    unittest.main()
