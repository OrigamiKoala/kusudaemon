"""The resumable single-node runner (PLAN.md §13 v0).

One idempotent entrypoint, ``run_node``, handles both first-run and resume:
calling it again after a `kill -9` just continues correctly from whatever
events are already durable on disk. There is no separate resume code path —
that convergence *is* the thing this module exists to prove.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget, EpisodeResult
from .events import EventLog
from .run_dir import ensure_node_trace_path, events_path, node_artifact_path

_SESSION_POLL_INTERVAL_SECONDS = 0.05


# §E24 (2026-08-13): events that consume a node's episode_completed,
# invalidating the resume-after-complete replay. The replay exists for one
# window only: the episode ended but the harness never processed the result
# (kill -9 between the completion append and the tree save). Any of these
# events AFTER the completion means the harness (or the operator) already
# acted on it — the node was transitioned or deliberately reset — so a
# later dispatch must run a FRESH episode. Before this check every gate
# failure poisoned the node: retries and operator redispatches replayed the
# old completion instead of running an episode (observed live: a T1 writer
# that 429'd once then "failed" its remaining attempts in ~4 ms each).
_REPLAY_INVALIDATING_TYPES = frozenset(
    {
        "node_gate_failed",           # round loop transitioned the node
        "node_episode_timeout",       # round loop transitioned the node on timeout
        "node_throttled",             # round loop transitioned the node on rate limit
        "node_review_failed",         # review consumed the completion
        "node_redispatch_requested",  # operator redispatch — new attempt series
        "node_reopened",              # operator direct reset — new attempt series
    }
)


def _completion_consumed(
    events: list[dict[str, Any]], node_id: str, completed: dict[str, Any]
) -> bool:
    base = completed.get("ts") or 0
    for event in events:
        if event.get("node_id") != node_id:
            continue
        if event.get("type") in _REPLAY_INVALIDATING_TYPES and (event.get("ts") or 0) > base:
            return True
    return False


def _snapshot_pre_writer(run_dir: str | Path, node_id: str, max_keep: int = 3) -> Path | None:
    """PLAN-TOKEN-ACCOUNTING.md §L5: Snapshot prior artifact to out/.versions/<node>/ before writer overwrite.

    PLAN-SWEEP-REPAIR.md §B3/B4 option (a): snapshot the resolved artifact,
    not the single file. When out/<node>/ holds part files, the snapshot is
    a directory copy (out/.versions/<node>/attempt_<ts>/) so §L4 can restore
    the parts layout instead of writing a concatenation into out/<node>.md
    where the parts dir would shadow it into a silent no-op. Single-file
    artifacts keep the legacy attempt_<ts>.md file snapshot."""
    try:
        from ..v3.run_dir import versions_dir
        from .run_dir import node_artifact_text, node_parts_dir
        try:
            resolved = node_artifact_text(run_dir, node_id)
        except (FileNotFoundError, OSError):
            return None
        if not resolved.strip():
            return None
        v_dir = versions_dir(run_dir, node_id)
        import shutil
        import time
        ts = int(time.time() * 1000)
        parts_d = node_parts_dir(run_dir, node_id)
        part_files = (
            [p for p in parts_d.glob("*.md") if p.is_file()]
            if parts_d.is_dir()
            else []
        )
        non_empty_parts = [p for p in part_files if p.stat().st_size > 0]
        if non_empty_parts:
            snap_p = v_dir / f"attempt_{ts}"
            snap_p.mkdir(parents=True, exist_ok=True)
            for src in non_empty_parts:
                shutil.copy2(src, snap_p / src.name)
        else:
            from .run_dir import node_artifact_path
            art_p = node_artifact_path(run_dir, node_id)
            if not art_p.exists() or art_p.stat().st_size == 0:
                return None
            snap_p = v_dir / f"attempt_{ts}.md"
            shutil.copy2(art_p, snap_p)
        # Prune oldest attempt_* snapshots (files and directories) to max_keep.
        try:
            from ..v3.run_dir import list_attempt_snapshots
            snaps = list_attempt_snapshots(run_dir, node_id)
            if len(snaps) > max_keep:
                for old in snaps[:-max_keep]:
                    with contextlib.suppress(OSError):
                        if old.is_dir():
                            shutil.rmtree(old)
                        else:
                            old.unlink()
        except Exception:
            pass
        return snap_p
    except Exception:
        return None


def _continuation_prompt(
    run_dir: Path, node_id: str, prompt: str, resume_session_id: str | None
) -> str:
    """Frame ANY redispatch as a continuation, not a restart.

    Every redispatch prompt is otherwise byte-identical to the first
    attempt's — with no continue-framing the model reads it as "do it
    again" and rewrites the artifact from scratch (observed live: a long
    doc back at block 1 after a timeout-resume, each whole-file `write`
    wiping all previously appended progress). Appended last so it has the
    final word.

    ``resume_session_id`` is set on the resumed-session path (the CLI
    session keeps full conversation history); on the fresh-session paths
    (``resume_unsupported``, ``no_session_captured``) it is None, and the
    framing instead points the model at the partial artifact already on
    disk — a previous attempt may still have written to it.
    """
    try:
        from .run_dir import node_artifact_path, node_artifact_text
        artifact = node_artifact_path(run_dir, node_id)
        try:
            existing_text = node_artifact_text(run_dir, node_id)
            existing_bytes = len(existing_text.encode("utf-8"))
        except (FileNotFoundError, OSError):
            existing_bytes = 0
    except Exception:
        try:
            from .run_dir import node_artifact_path as _nap
            artifact = _nap(run_dir, node_id)
        except Exception:
            artifact = Path(run_dir) / "out" / f"{node_id}.md"
        existing_bytes = 0
    if resume_session_id is not None:
        opening = (
            f"This episode continues your previous session ({resume_session_id}), "
            "which ended before the artifact was complete."
        )
    else:
        opening = (
            "This is a fresh session, but a previous attempt at this same "
            "task already ran before it."
        )
    if existing_bytes > 0:
        state_line = (
            f"Your partial artifact at `{artifact}` already holds "
            f"{existing_bytes} bytes of prior work. Read its tail to see exactly "
            "where the previous attempt left off, then continue with the "
            "remaining sections."
        )
        overwrite_rule = (
            "You may edit, revise, or delete sections as the work requires, "
            "but preserve the finished sections you are not deliberately "
            "changing: if your file tools replace whole-file content on "
            "write, read the current content first and carry it forward, "
            "rather than restarting the document from scratch."
        )
    else:
        state_line = (
            f"Your artifact path is `{artifact}` (currently empty — nothing "
            "usable survived the prior attempt)."
        )
        overwrite_rule = (
            "Write incrementally and append as you go so a later "
            "interruption never loses finished sections."
        )
    return (
        f"{prompt}\n\n[Harness notice — CONTINUATION, not a fresh task]\n"
        f"{opening} {state_line}\n"
        f"{overwrite_rule}"
    )


async def run_node(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    run_dir = Path(run_dir)
    log = EventLog(events_path(run_dir))

    # One parse of events.jsonl for the whole dispatch; every per-node query
    # scans the parsed list in memory. events.jsonl is append-only and
    # fsync'd per record (EventLog's contract), so a single read sees a
    # consistent prefix of the durable log.
    events = log.read_all()
    completed = EventLog.scan(events, node_id, "episode_completed")
    if completed is not None and not _completion_consumed(events, node_id, completed):
        # Resume-after-complete is a pure no-op: replay the recorded result
        # instead of re-dispatching, so calling run_node twice never produces
        # two artifacts or two terminal events for the same node. §E24: only
        # in the crash window — a completion that a gate failure, review
        # failure, or operator reset already consumed must not be replayed
        # (a retry or redispatch is a new attempt and needs a real episode).
        return _result_from_completed_event(completed)

    dispatched = EventLog.scan(events, node_id, "node_dispatched")
    session = EventLog.scan(events, node_id, "session_captured")
    supports_resume = bool(getattr(adapter, "supports_session_resume", False))

    # Sessions/logdirs already captured by prior attempts of this node. The
    # trace file is append-only across attempts (below), so the watcher must
    # skip these stale lines rather than re-capturing them as fresh.
    known_session_ids = {
        str(event.get("session_id"))
        for event in events
        if event.get("node_id") == node_id and event.get("session_id")
    }
    known_logdirs = {
        str(event.get("logdir"))
        for event in events
        if event.get("node_id") == node_id and event.get("logdir")
    }

    resume_session_id: str | None = None
    dispatch_reason = "dispatched"
    if session is not None and session.get("session_id"):
        if supports_resume:
            resume_session_id = session.get("session_id")
            dispatch_reason = "resumed_session"
            log.append(
                {
                    "node_id": node_id,
                    "role": "writer",
                    "round": 0,
                    "type": "node_redispatched",
                    "reason": "resumed_session",
                }
            )
        else:
            # A session id was captured last time but this adapter (e.g.
            # Codex today) has no continuation mechanism. Falling back to a
            # fresh redispatch is the documented behavior rather than an
            # error — see ClaudeCodeAdapter.supports_session_resume.
            dispatch_reason = "resume_unsupported"
            log.append(
                {
                    "node_id": node_id,
                    "role": "writer",
                    "round": 0,
                    "type": "node_redispatched",
                    "reason": "resume_unsupported",
                }
            )
    elif dispatched is not None:
        # Crashed before any output ever hit disk: nothing to continue from,
        # so redispatch fresh. "The original prompt" is the caller's
        # framing, not this module's: run_node has no memory of what it was
        # given before and just dispatches whatever prompt it's handed this
        # call — a caller may legitimately supply a different one on a
        # redispatch (PLAN-zeromem.md §9: a retry's prompt carries the prior
        # attempt's located defect forward, so it differs from the first).
        dispatch_reason = "no_session_captured"
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "node_redispatched",
                "reason": "no_session_captured",
            }
        )
    else:
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "node_dispatched",
            }
        )

    if dispatch_reason != "dispatched":
        # Every redispatch — resumed session or fresh — gets
        # continue-where-you-left-off framing. Without it the model reads
        # the byte-identical prompt as "do it again" and rewrites the
        # artifact from scratch (observed live: a 300-block doc back at
        # block 1 after a timeout-resume, each whole-file `write` wiping
        # all prior appended progress).
        prompt = _continuation_prompt(run_dir, node_id, prompt, resume_session_id)

    trace_path = ensure_node_trace_path(run_dir, node_id)
    # The trace is append-only across attempts: a prior attempt's entries
    # stay on disk and the new episode's lines are appended after a visible
    # boundary marker. Truncating here (the old behavior) wiped the
    # dashboard's main chat on every retry/timeout-resume, and the
    # incremental parse cache treated the rewritten file as a reset.
    # LocalEnvironment exec opens the tee in append mode to match; the
    # session watcher below skips already-captured ids so stale lines can
    # never be mistaken for a fresh capture.
    if trace_path.exists() and trace_path.stat().st_size > 0:
        try:
            with open(trace_path, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "type": "message",
                            "role": "system",
                            "content": (
                                f"── new attempt ({dispatch_reason}): "
                                "continuing below; everything above is "
                                "the prior attempt's history ──"
                            ),
                            "ts": time.time(),
                        }
                    )
                    + "\n"
                )
        except OSError:
            pass

    # LocalEnvironment.exec tees stdout to trace_path live, line by line, as
    # the subprocess runs — so this tail can see session_id the instant the
    # agent CLI emits it, without waiting for run_episode() to return. A
    # kill -9 can land any time after that line hits disk.
    #
    # PLAN-AUDIT.md §E13: this watcher exists to power session-resume
    # bookkeeping, and only an adapter with supports_session_resume=True can
    # ever act on the session it captures (see the resume_session_id branch
    # above). The only real Writer backend, gptme, has this False — so for
    # it the poll loop below used to run for the entire episode duration,
    # every ~50ms, finding nothing, for no functional benefit: no code path
    # reads a gptme session_captured event, and the dashboard's own
    # `_last_logdir` re-derives the logdir straight off the trace file
    # itself rather than depending on this event. Skip starting the task
    # entirely rather than starting it and having it find nothing.
    from ..pipeline.bypass import is_node_bypassed

    if is_node_bypassed(run_dir, node_id, "writer") or is_node_bypassed(run_dir, node_id):
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "node_execution_bypassed",
                "detail": "execution bypassed by operator before start",
            }
        )
        artifact_path = node_artifact_path(run_dir, node_id)
        if not artifact_path.exists():
            artifact_path.write_text("", encoding="utf-8")
        log.append(
            {
                "node_id": node_id,
                "role": "writer",
                "round": 0,
                "type": "episode_completed",
                "status": "done",
                "artifact_path": str(artifact_path),
                "error": None,
                "duration_ms": 0,
            }
        )
        return EpisodeResult(status="done", actions_log="bypassed by operator", metadata={"bypassed": True})

    stop_watching = asyncio.Event()
    watcher: asyncio.Task[None] | None = None
    if supports_resume:
        watcher = asyncio.create_task(
            _watch_for_session_id(
                trace_path,
                log,
                node_id,
                stop_watching,
                skip_session_ids=known_session_ids,
                skip_logdirs=known_logdirs,
            )
        )
    episode_kwargs: dict[str, Any] = {"live_trajectory_path": str(trace_path)}
    if resume_session_id is not None:
        episode_kwargs["resume_session_id"] = resume_session_id

    # PLAN-TOKEN-ACCOUNTING.md §L5: snapshot before writer overwrite
    _snapshot_pre_writer(run_dir, node_id)

    episode_task = asyncio.create_task(adapter.run_episode(prompt, env, budget, **episode_kwargs))

    async def _watch_for_bypass() -> None:
        while not stop_watching.is_set():
            if is_node_bypassed(run_dir, node_id, "writer") or is_node_bypassed(run_dir, node_id):
                episode_task.cancel()
                break
            await asyncio.sleep(0.1)

    bypass_watcher = asyncio.create_task(_watch_for_bypass())
    bypassed_mid_flight = False
    try:
        result = await episode_task
    except asyncio.CancelledError:
        if is_node_bypassed(run_dir, node_id, "writer") or is_node_bypassed(run_dir, node_id):
            bypassed_mid_flight = True
            result = EpisodeResult(status="done", actions_log="bypassed by operator", metadata={"bypassed": True})
            log.append(
                {
                    "node_id": node_id,
                    "role": "writer",
                    "round": 0,
                    "type": "node_execution_bypassed",
                    "detail": "execution bypassed by operator",
                }
            )
        else:
            raise
    finally:
        stop_watching.set()
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if bypass_watcher is not None:
            bypass_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bypass_watcher

    artifact_path = node_artifact_path(run_dir, node_id)
    # gptme writes the real artifact itself, mid-episode, via its own
    # save/patch tool calls against this exact path — now that the prompt
    # actually states that path (PLAN.md §D0; it didn't before) — so if the
    # file already has content, that *is* the artifact and must never be
    # clobbered by anything derived from the episode's raw output after the
    # fact. Only fall back when the agent never actually wrote anything
    # (crash, or an adapter with no file tools that relies on "last message
    # becomes the artifact").
    # PLAN-SWEEP-REPAIR.md §B: the artifact may live in out/<node>/ part
    # files — resolve via node_artifact_text so a parts-compliant writer is
    # never mistaken for an empty one here.
    from .run_dir import node_artifact_text
    try:
        existing = node_artifact_text(run_dir, node_id)
    except (FileNotFoundError, OSError):
        existing = ""
    if not existing.strip():
        if getattr(adapter, "has_file_tools", False):
            # PLAN.md §D0: an adapter that can write files (gptme) had every
            # opportunity to save the real artifact. An empty file here is a
            # genuine failure, not raw log noise to paper over — write "" so
            # the `nonempty` gate fails cleanly instead of a chat sentence
            # (or a stray save-fence) masquerading as a passed node.
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("", encoding="utf-8")
        else:
            visible_output = result.metadata.get("assistant_visible_output") or ""
            # actions_log_diagnostics_only (cli_agent.py) is set when an
            # adapter *has* a structured visible-output parser (gptme's
            # --output-format json) but it found no real assistant message —
            # meaning actions_log is raw protocol/tool-call JSON, not prose,
            # and must not be written as if it were content (this is exactly
            # how a bare ``{"type": "logdir", ...}`` bootstrap line —
            # everything printed before a crash mid-episode — used to end up
            # masquerading as a node's artifact). An adapter with no parser
            # at all (fake/test adapters) still falls back to the raw log,
            # unchanged.
            diagnostics_only = bool(result.metadata.get("actions_log_diagnostics_only"))
            artifact_text = visible_output or ("" if diagnostics_only else result.actions_log)
            artifact_path.write_text(artifact_text, encoding="utf-8")

    # v0 does not write manifest.jsonl: a single Writer node has no gates to
    # evaluate and no way to derive the PLAN.md §6 manifest schema. That
    # line is written by the caller once gates have actually run — v1's
    # round loop (src/kusudaemon/v1/manifest.py) is the first caller that
    # can do so correctly.
    log.append(
        {
            "node_id": node_id,
            "role": "writer",
            "round": 0,
            "type": "episode_completed",
            "status": result.status,
            "artifact_path": str(artifact_path),
            "error": result.error,
            "duration_ms": result.duration_ms,
        }
    )
    try:
        from .cost import CostLedger
        from .run_dir import cost_path
        cost_ledger = CostLedger(cost_path(run_dir))
        prompt_tokens = int(result.metadata.get("prompt_tokens", 0) or 0)
        completion_tokens = int(result.metadata.get("completion_tokens", 0) or 0)
        reasoning_tokens = int(result.metadata.get("reasoning_tokens", 0) or 0)
        cost_usd = result.metadata.get("cost_usd")
        cost_ledger.record(
            role="writer",
            phase="execute",
            node=node_id,
            model=str(getattr(adapter, "model", "") or result.metadata.get("model", "")),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=float(cost_usd) if cost_usd is not None else None,
            prompt_text=prompt if prompt_tokens == 0 else "",
            completion_text=(existing or result.actions_log) if completion_tokens == 0 else "",
        )
    except Exception:
        pass
    return result


async def run(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    """First-run-facing alias. Identical to ``resume`` — see module docstring."""
    return await run_node(run_dir, node_id, prompt, adapter, env, budget)


async def resume(
    run_dir: str | Path,
    node_id: str,
    prompt: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
) -> EpisodeResult:
    """Resume-facing alias. Identical to ``run`` — see module docstring."""
    return await run_node(run_dir, node_id, prompt, adapter, env, budget)


async def _watch_for_session_id(
    trace_path: Path,
    log: EventLog,
    node_id: str,
    stop: asyncio.Event,
    *,
    skip_session_ids: set[str] | None = None,
    skip_logdirs: set[str] | None = None,
) -> None:
    while not trace_path.exists():
        if stop.is_set():
            return
        await asyncio.sleep(_SESSION_POLL_INTERVAL_SECONDS)

    offset = 0
    # The trace is append-only across attempts, so a redispatch re-scans the
    # prior attempt's lines: ids/logdirs already captured (seeded here) are
    # skipped instead of re-emitted, and only a genuinely new capture ends
    # the watch. (Session ids share `captured_logdirs` below — pre-existing
    # conflation, kept so the skip check covers both.)
    captured_logdirs: set[str] = set(skip_logdirs or ()) | set(skip_session_ids or ())
    captured_sessions: set[str] = set(skip_session_ids or ())
    while True:
        try:
            # Binary mode and byte offsets: text-mode seek only accepts
            # opaque cookies from tell(), and readlines()+tell() consumes a
            # partial trailing line — the offset advances past the bytes
            # that will be written next, so a session_id that lands in a
            # torn line is never re-read (§11.9). Advance only past the last
            # complete line instead.
            with open(trace_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            data = b""
        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            new_lines = []
        else:
            offset += last_newline + 1
            new_lines = data[: last_newline + 1].decode("utf-8", errors="replace").splitlines()
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("session_id")
            if session_id:
                if session_id not in captured_logdirs:
                    captured_logdirs.add(session_id)
                    log.append(
                        {
                            "node_id": node_id,
                            "role": "writer",
                            "round": 0,
                            "type": "session_captured",
                            "session_id": session_id,
                        }
                    )
                    return
                # Stale line from a prior attempt (append-only trace):
                # skip it and keep polling for a genuinely new capture.
                continue
            # PLAN-AUDIT.md §E13: don't hardcode session_id as the only
            # event shape a resume-capable adapter can emit. A
            # `{"type": "logdir", ...}` line (the shape _gptme_worker.py
            # actually emits, though gptme itself has no session-resume
            # support today) is captured the same way so a future
            # resume-capable adapter whose continuity token is a logdir
            # rather than a session id doesn't need a second watcher.
            # Non-terminal update: keep polling for a real session_id.
            if record.get("type") == "logdir" and record.get("logdir"):
                ld = str(record.get("logdir"))
                if ld not in captured_logdirs:
                    captured_logdirs.add(ld)
                    log.append(
                        {
                            "node_id": node_id,
                            "role": "writer",
                            "round": 0,
                            "type": "session_captured",
                            "logdir": ld,
                        }
                    )
        if stop.is_set():
            return
        await asyncio.sleep(_SESSION_POLL_INTERVAL_SECONDS)


def _result_from_completed_event(event: dict[str, Any]) -> EpisodeResult:
    status = event.get("status")
    if status not in ("done", "timeout", "error", "cancelled"):
        status = "done"
    return EpisodeResult(
        status=status,
        actions_log="",
        error=event.get("error"),
        duration_ms=int(event.get("duration_ms") or 0),
        metadata={
            "artifact_path": event.get("artifact_path"),
            "replayed_from_event_log": True,
        },
    )
