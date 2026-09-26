"""Delete a run's OpenCode sessions once the harness is done with them.

OpenCode keeps every session forever in ``~/.local/share/opencode/opencode.db``,
and each update to a message or tool part is stored twice (the ``part`` row
and an ``event`` row). On 2026-09-26 the store was 4.3 GB, and 2.6 GB of it
came from two 08-15 corpus runs whose writers read a 165 MB ``source.txt``
whole, several times each; both run dirs had long since been deleted.

The run dir is the source of truth (the reasoning is in
``scratch/<node>/trace.jsonl``), so a node's OpenCode session is disposable once
the node passes, the same rule as ``scratch/``. What is lost is the dashboard's
tool-call detail for that node.

Opt-in (``KUSUDAEMON_OPENCODE_SESSION_CLEANUP=1``): the operator keeps sessions
as debugging records by default.

Deletion goes through ``opencode session delete``, OpenCode's own path, which
removes the session's messages, parts, events and sequence row. Each delete
boots the CLI against the shared sqlite store, so it waits for the same boot
slot as writer episodes (``cli_agent._await_boot_slot``). Only OpenCode ids
(``ses_`` prefix) captured by ``session_captured`` events are touched, and
every deletion is logged as ``opencode_session_deleted`` so
``v0/runner.py`` never tries to resume a deleted session.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

Emit = Callable[[dict[str, Any]], None]

DELETED_EVENT = "opencode_session_deleted"
_DELETE_TIMEOUT_S = 60.0


def cleanup_enabled() -> bool:
    return os.getenv("KUSUDAEMON_OPENCODE_SESSION_CLEANUP", "0") == "1"


def _read_events(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "events.jsonl"
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        pass
    return events


def deleted_session_ids(events: list[dict[str, Any]]) -> set[str]:
    return {
        str(e.get("session_id"))
        for e in events
        if e.get("type") == DELETED_EVENT and e.get("session_id")
    }


def sessions_to_delete(events: list[dict[str, Any]], node_id: str | None = None) -> list[tuple[str, str]]:
    """``(session_id, node_id)`` for captured OpenCode sessions not yet deleted,
    for one node or (``node_id=None``) the whole run, in capture order."""
    gone = deleted_session_ids(events)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in events:
        if e.get("type") != "session_captured":
            continue
        sid = str(e.get("session_id") or "")
        owner = str(e.get("node_id") or "-")
        if not sid.startswith("ses_") or sid in gone or sid in seen:
            continue
        if node_id is not None and owner != node_id:
            continue
        seen.add(sid)
        out.append((sid, owner))
    return out


def _existing(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only sessions present in the OpenCode store. Nothing else is ever
    handed to the CLI, which also keeps hermetic tests (fake ids) from
    starting it. An unreadable store means nothing is deleted."""
    if not candidates:
        return []
    from .opencode_session import session_directory

    kept: list[tuple[str, str]] = []
    for sid, owner in candidates:
        try:
            if session_directory(sid) is not None:
                kept.append((sid, owner))
        except Exception:
            return []
    return kept


async def _delete_one(session_id: str) -> tuple[bool, str]:
    from .cli_agent import _await_boot_slot

    await _await_boot_slot()
    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode", "session", "delete", session_id,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), _DELETE_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, UnboundLocalError):
            pass
        return False, f"timed out after {_DELETE_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return False, str(exc)
    detail = (err or b"").decode("utf-8", errors="replace").strip()
    # A child session is removed with its parent, so "not found" is success.
    if proc.returncode == 0 or "Session not found" in detail:
        return True, ""
    return False, detail[-300:] or f"exit {proc.returncode}"


async def delete_run_sessions(
    run_dir: str | Path, emit: Emit, *, node_id: str | None = None
) -> list[str]:
    """Delete this run's OpenCode sessions (one node's, or all). Never raises."""
    if not cleanup_enabled() or shutil.which("opencode") is None:
        return []
    deleted: list[str] = []
    for sid, owner in _existing(sessions_to_delete(_read_events(run_dir), node_id)):
        try:
            ok, detail = await _delete_one(sid)
        except Exception as exc:  # cleanup must never fail a run
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        event: dict[str, Any] = {"node_id": owner, "role": "harness", "round": 0, "session_id": sid}
        if ok:
            deleted.append(sid)
            event["type"] = DELETED_EVENT
        else:
            event["type"] = "opencode_session_delete_failed"
            event["detail"] = detail
        try:
            emit(event)
        except Exception:
            pass
    return deleted


def schedule_node_cleanup(run_dir: str | Path, emit: Emit, node_id: str) -> threading.Thread | None:
    """Delete one passed node's sessions on a background thread, so the round
    loop never waits on the CLI. Returns the thread (tests join it)."""
    if not cleanup_enabled() or shutil.which("opencode") is None:
        return None
    if not _existing(sessions_to_delete(_read_events(run_dir), node_id)):
        return None
    thread = threading.Thread(
        target=lambda: asyncio.run(delete_run_sessions(run_dir, emit, node_id=node_id)),
        name=f"opencode-cleanup-{node_id}",
        daemon=False,
    )
    thread.start()
    return thread
