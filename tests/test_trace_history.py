"""Trace history + resume framing (v0/runner.py, environment/local.py).

The dashboard's main chat is rebuilt from scratch/<node>/trace.jsonl, so a
redispatch must never delete prior attempts' entries — and a resumed
session must be told to continue, not restart (plus prompts must let the
model edit freely while preserving finished work).

No network, no agent binary: stub adapters record prompts and append
scripted lines to the live trajectory path, exactly like a real CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.environment.local import LocalEnvironment  # noqa: E402
from kusudaemon.types import EpisodeBudget, EpisodeResult  # noqa: E402
from kusudaemon.v0.events import EventLog  # noqa: E402
from kusudaemon.v0.run_dir import create_run_dir, events_path  # noqa: E402
from kusudaemon.v0.runner import _watch_for_session_id, run_node  # noqa: E402


class _StubAdapter:
    """Records prompts; appends scripted trace lines; never touches a shell."""

    supports_session_resume = True
    has_file_tools = True

    def __init__(
        self,
        lines: tuple[str, ...] = (),
        session_id: str | None = None,
        resume_support: bool = True,
    ) -> None:
        self.lines = list(lines)
        self.session_id = session_id
        self.supports_session_resume = resume_support
        self.prompts: list[str] = []
        self.resume_ids: list[str | None] = []

    async def run_episode(self, prompt, env, budget, live_trajectory_path=None, **kwargs):
        self.prompts.append(prompt)
        self.resume_ids.append(kwargs.get("resume_session_id"))
        if live_trajectory_path:
            with open(live_trajectory_path, "a", encoding="utf-8") as fh:
                for line in self.lines:
                    fh.write(line + "\n")
                if self.session_id:
                    fh.write(
                        json.dumps(
                            {"type": "logdir", "logdir": "/tmp/fake", "session_id": self.session_id}
                        )
                        + "\n"
                    )
        return EpisodeResult(status="done", actions_log="", duration_ms=1, metadata={})


def _trace_lines(run_dir: Path, node: str) -> list[str]:
    path = run_dir / "scratch" / node / "trace.jsonl"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TraceHistoryTest(unittest.TestCase):
    def test_redispatch_appends_boundary_and_keeps_history(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                env = LocalEnvironment()
                budget = EpisodeBudget(max_duration_seconds=30)
                first = _StubAdapter(
                    lines=(json.dumps({"type": "message", "role": "assistant", "content": "attempt one"}),),
                )
                await run_node(run_dir, "n", "do it", first, env, budget)
                # Force a fresh episode on the next call (a gate failure
                # consumed the completion — see _REPLAY_INVALIDATING_TYPES).
                EventLog(events_path(run_dir)).append(
                    {"node_id": "n", "type": "node_gate_failed", "ts": 9999999999}
                )
                second = _StubAdapter(
                    lines=(json.dumps({"type": "message", "role": "assistant", "content": "attempt two"}),),
                )
                await run_node(run_dir, "n", "do it", second, env, budget)
                return _trace_lines(run_dir, "n")

        lines = asyncio.run(scenario())
        texts = " ".join(lines)
        self.assertIn("attempt one", texts, "first attempt must survive redispatch")
        self.assertIn("attempt two", texts)
        self.assertTrue(
            any("new attempt" in line for line in lines),
            "a boundary marker must separate attempts",
        )

    def test_continuation_prompt_on_resumed_session(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                artifact = run_dir / "out" / "n.md"
                artifact.write_text("partial work here", encoding="utf-8")
                log = EventLog(events_path(run_dir))
                log.append({"node_id": "n", "type": "node_dispatched", "ts": 1})
                log.append(
                    {"node_id": "n", "type": "session_captured", "session_id": "ses_abc", "ts": 2}
                )
                adapter = _StubAdapter()
                env = LocalEnvironment()
                await run_node(run_dir, "n", "original brief", adapter, env, EpisodeBudget())
                return adapter

        adapter = asyncio.run(scenario())
        self.assertEqual(adapter.resume_ids, ["ses_abc"])
        prompt = adapter.prompts[0]
        self.assertIn("CONTINUATION", prompt)
        self.assertIn("out/n.md", prompt)
        self.assertIn("ses_abc", prompt)
        self.assertIn("original brief", prompt)

    def test_continuation_prompt_on_unsupported_resume_redispatch(self) -> None:
        """Fresh session after resume_unsupported: the model must still be
        told to continue the partial artifact, not start over."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                artifact = run_dir / "out" / "n.md"
                artifact.write_text("partial work here", encoding="utf-8")
                log = EventLog(events_path(run_dir))
                log.append({"node_id": "n", "type": "node_dispatched", "ts": 1})
                log.append(
                    {"node_id": "n", "type": "session_captured", "session_id": "ses_abc", "ts": 2}
                )
                adapter = _StubAdapter(resume_support=False)
                env = LocalEnvironment()
                await run_node(run_dir, "n", "original brief", adapter, env, EpisodeBudget())
                return adapter

        adapter = asyncio.run(scenario())
        self.assertEqual(adapter.resume_ids, [None])
        prompt = adapter.prompts[0]
        self.assertIn("CONTINUATION", prompt)
        self.assertIn("fresh session", prompt)
        self.assertIn("out/n.md", prompt)
        self.assertIn("original brief", prompt)

    def test_continuation_prompt_when_no_session_captured(self) -> None:
        """Crashed before any session id hit disk: a prior attempt may still
        have written to the artifact, so frame a continuation anyway."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                artifact = run_dir / "out" / "n.md"
                artifact.write_text("partial work here", encoding="utf-8")
                log = EventLog(events_path(run_dir))
                log.append({"node_id": "n", "type": "node_dispatched", "ts": 1})
                adapter = _StubAdapter()
                env = LocalEnvironment()
                await run_node(run_dir, "n", "original brief", adapter, env, EpisodeBudget())
                return adapter

        adapter = asyncio.run(scenario())
        prompt = adapter.prompts[0]
        self.assertIn("CONTINUATION", prompt)
        self.assertIn("out/n.md", prompt)

    def test_first_dispatch_has_no_continuation_framing(self) -> None:
        """A genuine first dispatch must get the bare prompt — the notice
        is only for redispatches."""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                run_dir = create_run_dir(Path(root_str), "run1")
                adapter = _StubAdapter()
                env = LocalEnvironment()
                await run_node(run_dir, "n", "original brief", adapter, env, EpisodeBudget())
                return adapter

        adapter = asyncio.run(scenario())
        self.assertNotIn("CONTINUATION", adapter.prompts[0])
        self.assertEqual(adapter.prompts[0], "original brief")

    def test_watcher_skips_stale_session_and_captures_new(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as root_str:
                trace = Path(root_str) / "trace.jsonl"
                trace.write_text(
                    json.dumps({"type": "logdir", "logdir": "/tmp/old", "session_id": "ses_old"})
                    + "\n",
                    encoding="utf-8",
                )
                log = EventLog(Path(root_str) / "events.jsonl")
                stop = asyncio.Event()
                watcher = asyncio.create_task(
                    _watch_for_session_id(
                        trace,
                        log,
                        "n",
                        stop,
                        skip_session_ids={"ses_old"},
                        skip_logdirs=set(),
                    )
                )
                await asyncio.sleep(0.1)
                # Stale id alone must not capture anything.
                self.assertIsNone(log.last_event("n", "session_captured"))
                with open(trace, "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps({"type": "logdir", "logdir": "/tmp/new", "session_id": "ses_new"})
                        + "\n"
                    )
                deadline = asyncio.get_event_loop().time() + 5.0
                found = None
                while asyncio.get_event_loop().time() < deadline:
                    found = log.last_event("n", "session_captured")
                    if found is not None:
                        break
                    await asyncio.sleep(0.05)
                stop.set()
                await watcher
                return found

        found = asyncio.run(scenario())
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.get("session_id"), "ses_new")


class PromptPreservationTest(unittest.TestCase):
    def test_artifact_instruction_forbids_whole_file_rewrite_on_first_pass(self) -> None:
        """PLAN-SWEEP-REPAIR.md §E1/§E2.

        This test previously asserted the opposite -- that the first-pass
        instruction says "freely edit" and carries no rewrite prohibition.
        That was the §E1 defect: every "do not rewrite" sentence lived on a
        retry-only path, so the attempt that writes the whole artifact was
        told nothing, and a writer fixing one duplicated heading re-emitted
        34 KB ("Let me fix this by rewriting the file properly").
        """
        from kusudaemon.pipeline.prompts import _artifact_instruction
        from kusudaemon.v1.tree import TaskNode

        node = TaskNode(id="n", brief="b", artifact="out/n.md", gates=["nonempty"])
        with tempfile.TemporaryDirectory() as root_str:
            text = _artifact_instruction(node, Path(root_str))
        lowered = text.lower()
        # The rule is stated, on the first pass, without a retry in sight.
        self.assertIn("do not re-emit the whole file", lowered)
        self.assertIn("incrementally", lowered)
        # Targeted edits and a failed-match recovery that is not "rewrite".
        self.assertIn("targeted edits", lowered)
        self.assertIn("smaller, more distinctive edit", lowered)
        # Parts are the preferred shape for a long document, not a co-equal
        # alternative to "write the single artifact directly".
        self.assertIn("prefer part files", lowered)
        # The blanket permission that read as licence to restructure is gone.
        self.assertNotIn("freely edit", lowered)

    def test_leaf_scope_block_disowns_document_level_markers(self) -> None:
        """A decomposed leaf must not emit the whole document's end marker.

        On ``100-floor_armC_seed1`` two leaves obeyed the goal's "use
        '*** finished' to indicate the end of the document" literally; the
        third leaf's sentinel landed mid-document and the evaluator dropped
        the fourth leaf, scoring a complete artifact 75%.
        """
        from kusudaemon.pipeline.prompts import _leaf_scope_block
        from kusudaemon.v1.tree import TaskNode
        from kusudaemon.v6.direct import SINGLE_NODE_ID

        leaf = TaskNode(id="unit-03", brief="Floors 51 to 75", artifact="out/unit-03.md", gates=["nonempty"])
        text = _leaf_scope_block(leaf).lower()
        self.assertIn("you own only the slice named in your brief", text)
        self.assertIn("end-of-document marker", text)
        self.assertIn("do not emit them", text)

        # A single-node tree really does own the whole document -- no clause.
        whole = TaskNode(
            id=SINGLE_NODE_ID,
            brief="everything",
            artifact=f"out/{SINGLE_NODE_ID}.md",
            gates=["nonempty"],
        )
        self.assertEqual(_leaf_scope_block(whole), "")


if __name__ == "__main__":
    unittest.main()
