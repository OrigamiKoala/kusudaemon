"""Live agent thinking out of OpenCode's own on-disk store.

An ``opencode run --format json`` episode streams (almost) nothing to
stdout while it works — the CLI persists every step to its sqlite store
(``~/.local/share/opencode/opencode.db``: ``message``/``part`` tables)
and only prints the final result. The harness's trace file therefore held
nothing but heartbeats for the whole episode, so the dashboard's Chat tab
and main run stream showed no subagent thinking, no tool calls, and no
token usage until the episode ended (often never, on a timeout-resume
loop). The thinking tokens were always on this computer — just in a
database the dashboard never read.

This module translates one opencode session's parts into the harness's
``TraceEntry`` vocabulary (``dashboard/rendering.py``) so
``RunState.trace_entries`` can merge them with the file-backed trace.
Read-only, best-effort, stdlib-only (``sqlite3``): a missing/locked DB or
an unknown part shape yields ``[]``, never an exception.

Per-session results are cached on the caller's dict
(``RunState._opencode_cache``): the cheap ``max(time_updated)`` probe runs
per call, the full part fetch + translate only when the session moved.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .rendering import TraceEntry

import re

# A single part's data can be large (a whole file read). Cap what one
# rendered entry carries so a long episode can't blow up the DOM/JSON.
_TOOL_TEXT_CAP = 8000
# Safety tail when a session somehow grows unbounded (normal LongGenBench
# sessions are hundreds of parts).
_MAX_PARTS_PER_SESSION = 20000

# `#*# Block 12 (1, 1):` — the LongGenBench section separator.
_BLOCK_HEADER_RE = re.compile(r"#\*#\s*Block\s+(\d+)\b")
# `cat >> <file> << 'TAG'\n<body>\nTAG` heredocs (the shell idiom the
# opencode writer uses for incremental appends).
_HEREDOC_RE = re.compile(
    r"<<\s*['\"]?([A-Za-z0-9_]+)['\"]?\s*\n(.*?)(?=\n\1\s*(?:\n|\Z))",
    re.DOTALL,
)


def default_db_path() -> Path:
    """Locate opencode's sqlite store (override via ``OPENCODE_DB_PATH``)."""
    override = os.environ.get("OPENCODE_DB_PATH")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(data_home) / "opencode" / "opencode.db"


def opencode_session_ids(
    events: list[dict[str, Any]], node_id: str
) -> list[str]:
    """Session ids captured for ``node_id`` (opencode resume tokens)."""
    seen: list[str] = []
    for event in events:
        if event.get("node_id") != node_id:
            continue
        session_id = event.get("session_id")
        if session_id and str(session_id) not in seen:
            seen.append(str(session_id))
    return seen


def _cap(text: str, limit: int = _TOOL_TEXT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def _ms_to_s(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # opencode stores milliseconds; tolerate seconds just in case.
    return number / 1000.0 if number > 1e11 else number


def _part_timestamp(data: dict[str, Any], row_ts_ms: Any) -> float | None:
    """Prefer the part's own ``time.start``; fall back to the row stamp."""
    time_block = data.get("time")
    if isinstance(time_block, dict):
        for key in ("start", "created"):
            stamp = _ms_to_s(time_block.get(key))
            if stamp is not None:
                return stamp
    return _ms_to_s(row_ts_ms)


def translate_part(
    data: dict[str, Any],
    *,
    row_ts_ms: Any,
    node_id: str | None = None,
) -> list[TraceEntry]:
    """One opencode part-data dict → 0+ trace entries (never raises)."""
    try:
        return _translate_part(data, row_ts_ms=row_ts_ms, node_id=node_id)
    except Exception:
        return []


def _translate_part(
    data: dict[str, Any],
    *,
    row_ts_ms: Any,
    node_id: str | None,
) -> list[TraceEntry]:
    ptype = data.get("type")
    stamp = _part_timestamp(data, row_ts_ms)
    if ptype == "reasoning":
        text = str(data.get("text") or "").strip()
        if not text:
            return []
        return [TraceEntry("thinking", text, timestamp=stamp, node_id=node_id)]
    if ptype == "text":
        text = str(data.get("text") or "")
        if not text.strip():
            return []
        return [TraceEntry("assistant", text, timestamp=stamp, node_id=node_id)]
    if ptype == "tool":
        tool_name = str(data.get("tool") or "tool")
        call_id = data.get("callID")
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        status = str(state.get("status") or "")
        raw_input = state.get("input")
        input_str = (
            json.dumps(raw_input, ensure_ascii=False)
            if isinstance(raw_input, dict)
            else ("" if raw_input is None else str(raw_input))
        )
        entries = [
            TraceEntry(
                "tool_call",
                f"{tool_name}",
                tool_name=tool_name,
                tool_input=_cap(input_str) if input_str else None,
                tool_id=str(call_id) if call_id else None,
                timestamp=stamp,
                node_id=node_id,
            )
        ]
        output = state.get("output")
        if output is not None and status in ("completed", "error", ""):
            output_str = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            entries.append(
                TraceEntry(
                    "tool",
                    f"tool_result: {_cap(output_str)[:300]}",
                    tool_name=tool_name,
                    tool_input=_cap(input_str) if input_str else None,
                    tool_output=_cap(output_str),
                    logs=_cap(output_str),
                    tool_id=str(call_id) if call_id else None,
                    exit_code=1 if status == "error" else 0,
                    timestamp=stamp,
                    node_id=node_id,
                )
            )
        return entries
    if ptype == "step-finish":
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        try:
            prompt_tokens = int(tokens.get("input", 0) or 0)
            completion_tokens = int(tokens.get("output", 0) or 0)
            reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
        except (TypeError, ValueError):
            prompt_tokens = completion_tokens = reasoning_tokens = 0
        total = prompt_tokens + completion_tokens + reasoning_tokens
        cost = data.get("cost")
        try:
            cost_usd: float | None = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost_usd = None
        return [
            TraceEntry(
                "usage",
                f"Tokens: {total:,} (prompt: {prompt_tokens:,}, "
                f"completion: {completion_tokens:,}, reasoning: {reasoning_tokens:,})",
                tokens=total,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=cost_usd,
                timestamp=stamp,
                node_id=node_id,
            )
        ]
    # step-start, compaction summaries, unknown future shapes: nothing to show.
    return []


def read_session_entries(
    db_path: str | Path,
    session_id: str,
    cache: dict[str, Any],
    *,
    node_id: str | None = None,
) -> list[TraceEntry]:
    """Translated entries for one opencode session (cached, never raises).

    ``cache`` is the caller's per-process dict (``RunState._opencode_cache``,
    guarded by its lock): ``{session_id: (watermark, by_part_id, ordered)}``.

    Monotonic by part id: re-fetched rows merge into (never delete from)
    the cached set, so opencode-side compaction/pruning of old parts can
    never shrink what the chat already showed — nothing ever vanishes
    from the feed once displayed.
    """
    key = str(session_id)
    try:
        db_file = Path(db_path)
        if not db_file.exists():
            cached = cache.get(key)
            return list(cached[2]) if cached is not None else []
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT COALESCE(MAX(time_updated), 0) FROM part WHERE session_id = ?",
                (key,),
            )
            row = cur.fetchone()
            watermark = row[0] if row else 0
            cached = cache.get(key)
            if cached is not None and cached[0] == watermark:
                return list(cached[2])
            cur.execute(
                "SELECT id, data, time_created FROM part "
                "WHERE session_id = ? ORDER BY time_created, id "
                "LIMIT ?",
                (key, _MAX_PARTS_PER_SESSION),
            )
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception:
        cached = cache.get(key)
        return list(cached[2]) if cached is not None else []
    by_id: dict[str, tuple[Any, list[TraceEntry]]] = (
        dict(cache[key][1]) if key in cache else {}
    )
    for part_id, data_text, created in rows:
        try:
            data = json.loads(data_text)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        by_id[str(part_id)] = (
            created,
            translate_part(data, row_ts_ms=created, node_id=node_id),
        )
    ordered: list[TraceEntry] = []
    for _, (_, part_entries) in sorted(by_id.items(), key=lambda kv: (kv[1][0], kv[0])):
        ordered.extend(part_entries)
    cache[key] = (watermark, by_id, ordered)
    if len(cache) > 256:
        cache.pop(next(iter(cache)))
    return list(ordered)


def _extract_heredocs(command: str) -> list[str]:
    """Bodies of ``<< TAG`` heredocs inside a shell command."""
    return [match.group(2) for match in _HEREDOC_RE.finditer(command or "")]


def split_blocks(text: str) -> dict[int, str]:
    """``{#*# Block N: ...}`` sections in ``text`` → ``{N: section}``.

    A section runs from its header line to (not including) the next
    header or end of text. On duplicate numbers the longest wins.
    """
    found: dict[int, str] = {}
    matches = list(_BLOCK_HEADER_RE.finditer(text or ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end].strip()
        if not section:
            continue
        try:
            number = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if number not in found or len(section) > len(found[number]):
            found[number] = section
    return found


def iter_write_payloads(
    db_path: str | Path, session_id: str
) -> list[tuple[str, str]]:
    """Every ``(kind, text)`` the session ever wrote toward files.

    ``kind`` is ``"write"`` (``write``/``edit`` tool content — a
    whole-file overwrite), ``"heredoc"`` (a bash ``<< TAG`` append body),
    or ``"bash"`` (other bash commands touching ``.md`` files, kept for
    audit, not block recovery). Oldest first. Read-only, never raises.
    """
    try:
        db_file = Path(db_path)
        if not db_file.exists():
            return []
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT data FROM part WHERE session_id = ? ORDER BY time_created, id "
                "LIMIT ?",
                (str(session_id), _MAX_PARTS_PER_SESSION),
            )
            rows = cur.fetchall()
        finally:
            con.close()
    except Exception:
        return []
    payloads: list[tuple[str, str]] = []
    for (data_text,) in rows:
        try:
            data = json.loads(data_text)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "tool":
            continue
        tool = str(data.get("tool") or "")
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        inputs = state.get("input")
        if tool == "write":
            content = inputs.get("content") if isinstance(inputs, dict) else None
            if isinstance(content, str) and content.strip():
                payloads.append(("write", content))
        elif tool == "edit":
            for key in ("newString", "new_string", "content"):
                content = inputs.get(key) if isinstance(inputs, dict) else None
                if isinstance(content, str) and content.strip():
                    payloads.append(("write", content))
                    break
        elif tool == "bash":
            command = inputs.get("command") if isinstance(inputs, dict) else None
            if not isinstance(command, str):
                continue
            bodies = _extract_heredocs(command)
            if bodies:
                payloads.extend(("heredoc", body) for body in bodies if body.strip())
            elif ".md" in command:
                payloads.append(("bash", command))
    return payloads


def recover_blocks(
    db_path: str | Path, session_id: str
) -> dict[int, str]:
    """Union of every ``#*# Block N`` section the session ever wrote.

    Merges ``write``-tool overwrites (each of which replaced the artifact
    on disk) and bash heredoc appends; on conflicts the longest section
    wins. ``{N: section}`` sorted by N by the caller. Read-only.
    """
    recovered: dict[int, str] = {}
    for kind, text in iter_write_payloads(db_path, session_id):
        if kind == "bash":
            continue
        for number, section in split_blocks(text).items():
            if number not in recovered or len(section) > len(recovered[number]):
                recovered[number] = section
    return recovered
