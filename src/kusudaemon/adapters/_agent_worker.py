#!/usr/bin/env python3
"""Standalone entrypoint that runs one bounded `claude`/`codex` episode
(Anthropic Claude Code and OpenAI Codex CLI, respectively) as a subprocess
and translates its stdout into the harness's gptme-shaped trace vocabulary
on the way out. Invoked by ``ClaudeCodeAdapter``/``CodexAdapter`` as:

    <env> python _agent_worker.py --format claude -- claude --print ... < {prompt_path}
    <env> python _agent_worker.py --format codex -- codex exec --json ... - < {prompt_path}

Why a translator in front of a CLI the adapter could just as well run raw?
Every consumer of a Writer's ``trace.jsonl`` (dashboard/rendering.py's
``parse_trace_lines``, state.py's ``_summarize_subagent``, v0/runner.py's
``_watch_for_session_id``) speaks one vocabulary — ``type: message/thinking/
logdir/heartbeat`` with the gptme roles — and the two CLIs speak two other
formats (Claude Code's ``stream-json`` records, Codex's exec-json thread
events) that differ from each other and from gptme. Rather than teaching
every consumer about all three formats (the old LongHorizon-Harness
approach: a ``visible_output``/``parse_trajectory`` parser per backend, plus
a second hand-rolled parse inside the dashboard), this worker is the single
place that knows each backend's raw format. The tee'd trace is already
translated, so the dashboard's incremental parser, the subagent-status
deriver, and the session watcher all work for claude/codex with zero
changes.

The raw format's fidelity is preserved in one direction and dropped in one
direction, both deliberately:

- Unknown non-JSON and unknown JSON lines pass through unchanged (they
  render "raw", dim, in the feed rather than vanishing).
- Known noise is dropped: ``system`` records other than ``init`` (Claude),
  and ``item.updated`` streaming snapshots (Codex) — Codex emits its final
  state on ``item.completed`` anyway, so the trace updates once per item
  rather than per token.

The translated lines feed every consumer the same way gptme's do, including
``_emit_assistant_content`` — a Claude/Codex writer that edits files in the
workspace produces the same save/patch diff entries in the Chat tab a gptme
writer does.

Session discovery (v0/runner.py ``_watch_for_session_id``) works through
the same logdir convention gptme's worker uses: a ``{"type": "logdir",
"logdir": ..., "session_id": ...}`` line is emitted once ``init`` (claude)
or ``thread.started`` (codex) arrives, carrying the CLI's own session id.
``logdir`` points at a throwaway tempdir (there is no gptme-style log dir
to point at); nothing ever writes to it — it exists so the dashboard's
"has a live agent" logic sees a logdir the same way it does for gptme.
Interjections (writing to ``<logdir>/prompt-queue.jsonl``) are gptme-only
and are a no-op for these backends — a known limitation, recorded in
``claude_code.py``'s docstring.

Also emits one ``{"type": "heartbeat", ...}`` line per 10 s of silence
(mirroring ``_gptme_worker.py`` §F3): the CLIs can think for minutes with
nothing hitting stdout, and a live surface must be able to tell "thinking"
from "wedged". ``parse_trace_lines`` skips heartbeat lines.

The child runs with this worker's stdin (the prompt file, from the shell's
``< {prompt_path}`` redirect) and inherits stderr, so error text reaches
the harness's stderr pipe exactly as if the CLI had run directly. The
worker's exit code is the child's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any

CLAUDE = "claude"
CODEX = "codex"
OPENCODE = "opencode"
ANTIGRAVITY = "antigravity"
AGY = "agy"

# §D13: an over-limit stdout line must drop, not crash the episode. The
# env override exists so the end-to-end test can exercise the drop path
# with a small ceiling instead of printing 64 MB.
_MAX_LINE_BYTES = int(os.environ.get("KUSUDAEMON_WORKER_MAX_LINE_BYTES", 64 * 1024 * 1024))
_CAP = 300
_HEARTBEAT_SECONDS = 10.0

# Codex items that record an action rather than assistant prose (ported from
# LongHorizon-Harness-main/src/lh_harness/agent_logs.py::_CODEX_TOOL_ITEMS).
_CODEX_TOOL_ITEMS = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "dynamic_tool_call",
    "collab_tool_call",
    "web_search",
    "todo_list",
}

# Record types each backend is known to emit. Anything outside the set is
# from a newer CLI than this translator knows: it passes through unchanged
# (rendering "raw") rather than vanishing.
_CLAUDE_TYPES = {"system", "assistant", "user", "result"}
_CODEX_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "item.started",
    "item.updated",
    "item.completed",
    "error",
}
_OPENCODE_TYPES = {
    "step-start",
    "step_start",
    "step-finish",
    "step_finish",
    "text",
    "message",
    "thinking",
    "reasoning",
    "tool",
    "tool_use",
    "tool_result",
    "error",
    "message.part.updated",
    "message.part.delta",
    "message.part.removed",
}
_ANTIGRAVITY_EVENTS = {
    "init",
    "step_update",
    "result",
    "error",
}


def _cap(text: str) -> str:
    if len(text) <= _CAP:
        return text
    return text[:_CAP] + "…"


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


# ----------------------------------------------------------------------------
# Claude Code (--output-format stream-json)
# ----------------------------------------------------------------------------


def translate_claude(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One stream-json record → trace lines, or None to drop the line."""
    rtype = record.get("type")
    if rtype == "system":
        if record.get("subtype") == "init":
            payload: dict[str, Any] = {
                "type": "logdir",
                "logdir": session_dir,
                "session_id": str(record.get("session_id") or ""),
            }
            model = record.get("model")
            if model:
                payload["model"] = str(model)
            return [json.dumps(payload)]
        # thinking_tokens and the rest of Claude's system chatter is noise.
        return None
    if rtype == "assistant":
        message = record.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return None
        u = message.get("usage") if isinstance(message, dict) else None
        prompt_tokens = None
        completion_tokens = None
        total_tokens = None
        if isinstance(u, dict):
            prompt_tokens = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
            completion_tokens = u.get("output_tokens", 0) or 0
            total_tokens = prompt_tokens + completion_tokens

        out: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    think_payload: dict[str, Any] = {"type": "thinking", "content": text}
                    if completion_tokens and len(blocks) == 1:
                        think_payload["tokens"] = completion_tokens
                        think_payload["reasoning_tokens"] = completion_tokens
                    out.append(json.dumps(think_payload))
            elif btype == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    msg_payload: dict[str, Any] = {"type": "message", "role": "assistant", "content": text}
                    if prompt_tokens is not None:
                        msg_payload["prompt_tokens"] = prompt_tokens
                        msg_payload["completion_tokens"] = completion_tokens
                        msg_payload["tokens"] = total_tokens
                    out.append(json.dumps(msg_payload))
            elif btype == "tool_use":
                name = str(block.get("name") or "tool_use")
                args = block.get("input")
                tool_id = block.get("id")
                content = f"tool_use {name}: {_cap(_compact(args))}"
                tool_payload: dict[str, Any] = {
                    "type": "message",
                    "role": "tool",
                    "tool_name": name,
                    "tool_input": args,
                    "tool_id": tool_id,
                    "content": content,
                }
                if prompt_tokens is not None:
                    tool_payload["prompt_tokens"] = prompt_tokens
                    tool_payload["completion_tokens"] = completion_tokens
                    tool_payload["tokens"] = total_tokens
                out.append(json.dumps(tool_payload))
        return out or None
    if rtype == "user":
        message = record.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            return None
        out = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            text = content if isinstance(content, str) else _compact(content) if content else ""
            tool_id = block.get("tool_use_id")
            is_err = bool(block.get("is_error"))
            out.append(
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "tool_id": tool_id,
                        "tool_output": text,
                        "logs": text,
                        "exit_code": 1 if is_err else 0,
                        "content": f"tool_result: {_cap(text)}",
                    }
                )
            )
        return out or None
    if rtype == "result":
        text = str(record.get("result") or "").strip()
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        out = []
        if text:
            msg = {"type": "message", "role": "assistant", "content": text}
            if usage:
                inp = usage.get("input_tokens", 0) or 0
                outp = usage.get("output_tokens", 0) or 0
                msg["prompt_tokens"] = inp
                msg["completion_tokens"] = outp
                msg["tokens"] = inp + outp
            out.append(json.dumps(msg))
        if usage:
            inp = usage.get("input_tokens", 0) or 0
            outp = usage.get("output_tokens", 0) or 0
            out.append(
                json.dumps(
                    {
                        "type": "usage",
                        "prompt_tokens": inp,
                        "completion_tokens": outp,
                        "total_tokens": inp + outp,
                        "cost_usd": record.get("total_cost_usd") or record.get("cost_usd"),
                    }
                )
            )
        return out or None
    return None


# ----------------------------------------------------------------------------
# Codex (codex exec --json)
# ----------------------------------------------------------------------------


def translate_codex(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One exec-json thread event → trace lines, or None to drop the line."""
    rtype = record.get("type")
    if rtype == "thread.started":
        return [
            json.dumps(
                {
                    "type": "logdir",
                    "logdir": session_dir,
                    "session_id": str(record.get("thread_id") or ""),
                }
            )
        ]
    if rtype == "turn.completed":
        usage = record.get("usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens", 0) or 0
            outp = usage.get("output_tokens", 0) or 0
            reas = usage.get("reasoning_tokens", 0) or 0
            return [
                json.dumps(
                    {
                        "type": "usage",
                        "prompt_tokens": inp,
                        "completion_tokens": outp,
                        "reasoning_tokens": reas,
                        "total_tokens": inp + outp + reas,
                    }
                )
            ]
        return None
    if rtype == "error":
        message = str(record.get("message") or "")
        if message:
            return [json.dumps({"type": "message", "role": "system", "content": f"Error: {message}"})]
        return None
    if rtype == "turn.failed":
        error = record.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return [
            json.dumps(
                {
                    "type": "message",
                    "role": "system",
                    "content": f"Error: turn failed: {message or 'codex turn failed'}",
                }
            )
        ]
    if rtype == "item.updated":
        # Streaming snapshots; the completed record carries the final state.
        return None
    item = record.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if rtype == "item.started":
        if item_type in _CODEX_TOOL_ITEMS:
            inp = item.get("command") or item.get("input") or item.get("parameters") or item.get("args")
            tool_payload: dict[str, Any] = {
                "type": "message",
                "role": "tool",
                "tool_name": item_type,
                "tool_id": item.get("id"),
                "content": f"tool_use {item_type}" if inp is None else f"tool_use {item_type}: {_cap(_compact(inp))}",
            }
            if inp is not None:
                tool_payload["tool_input"] = inp
            return [json.dumps(tool_payload)]
        return None
    if rtype != "item.completed":
        return None
    if item_type == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            return [json.dumps({"type": "message", "role": "assistant", "content": text})]
        return None
    if item_type == "reasoning":
        text = str(item.get("text") or "").strip()
        if not text:
            text = "\n".join(
                part for part in (item.get("summary") or []) if isinstance(part, str)
            ).strip()
        if text:
            think_payload: dict[str, Any] = {"type": "thinking", "content": text}
            tok = item.get("tokens") or item.get("reasoning_tokens")
            if tok:
                think_payload["tokens"] = tok
                think_payload["reasoning_tokens"] = tok
            return [json.dumps(think_payload)]
        return None
    if item_type == "error":
        return [
            json.dumps(
                {"type": "message", "role": "system", "content": f"Error: {item.get('message') or ''}"}
            )
        ]
    if item_type == "command_execution":
        output = str(item.get("aggregated_output") or "").strip()
        exit_code = item.get("exit_code")
        cmd = item.get("command")
        out = []
        if output:
            out.append(f"tool_result: {_cap(output)}")
        if exit_code is not None:
            out.append(f"[exit_code={exit_code}]")
        if not out:
            return None
        return [
            json.dumps(
                {
                    "type": "message",
                    "role": "tool",
                    "tool_name": "command_execution",
                    "tool_input": cmd,
                    "tool_output": output,
                    "logs": output,
                    "exit_code": exit_code,
                    "content": line,
                }
            )
            for line in out
        ]
    if item_type == "file_change":
        changes = item.get("changes") or []
        lines = [
            f"{change.get('kind', '')} {change.get('path', '')}".strip()
            for change in changes
            if isinstance(change, dict)
        ]
        if not lines:
            return None
        return [
            json.dumps(
                {
                    "type": "message",
                    "role": "tool",
                    "tool_name": "file_change",
                    "tool_input": changes,
                    "tool_output": "\n".join(lines),
                    "logs": "\n".join(lines),
                    "content": _cap(line),
                }
            )
            for line in lines
        ]
    if item_type in {"mcp_tool_call", "dynamic_tool_call", "collab_tool_call"}:
        error = item.get("error")
        tool_name = item.get("name") or item_type
        tool_input = item.get("arguments") or item.get("parameters") or item.get("input")
        if isinstance(error, dict) and error.get("message"):
            return [
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "tool_output": error["message"],
                        "logs": error["message"],
                        "exit_code": 1,
                        "content": f"tool_result: {error['message']}",
                    }
                )
            ]
        result = item.get("result")
        if result:
            text_res = _compact(result)
            return [
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "tool_output": text_res,
                        "logs": text_res,
                        "exit_code": 0,
                        "content": f"tool_result: {_cap(text_res)}",
                    }
                )
            ]
        return None
    # web_search, todo_list: nothing to show past the started line.
    return None


# ----------------------------------------------------------------------------
# OpenCode (opencode run --format json)
# ----------------------------------------------------------------------------


def translate_opencode(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One OpenCode json record → trace lines, or None to drop the line."""
    rtype = record.get("type")
    part = record.get("part") if isinstance(record.get("part"), dict) else (record.get("properties", {}).get("part") if isinstance(record.get("properties"), dict) else {})
    if rtype in ("step-start", "step_start"):
        session_id = str(
            record.get("sessionID")
            or record.get("sessionId")
            or record.get("session_id")
            or part.get("sessionID")
            or part.get("sessionId")
            or part.get("session_id")
            or ""
        )
        return [
            json.dumps(
                {
                    "type": "logdir",
                    "logdir": session_dir,
                    "session_id": session_id,
                }
            )
        ]
    if rtype in ("step-finish", "step_finish"):
        tokens = record.get("tokens") or part.get("tokens") or record.get("usage") or part.get("usage")
        if isinstance(tokens, dict):
            inp = tokens.get("input", 0) or tokens.get("prompt_tokens", 0) or 0
            outp = tokens.get("output", 0) or tokens.get("completion_tokens", 0) or 0
            reas = tokens.get("reasoning", 0) or tokens.get("reasoning_tokens", 0) or 0
            return [
                json.dumps(
                    {
                        "type": "usage",
                        "prompt_tokens": inp,
                        "completion_tokens": outp,
                        "reasoning_tokens": reas,
                        "total_tokens": inp + outp + reas,
                    }
                )
            ]
        return None
    if rtype == "text":
        text = str(record.get("text") or record.get("content") or part.get("text") or "").strip()
        if text:
            return [json.dumps({"type": "message", "role": "assistant", "content": text})]
        return None
    if rtype in ("thinking", "reasoning"):
        text = str(
            record.get("thinking")
            or record.get("reasoning")
            or record.get("content")
            or record.get("text")
            or part.get("thinking")
            or part.get("reasoning")
            or part.get("text")
            or ""
        ).strip()
        if text:
            tok = record.get("tokens") or part.get("tokens")
            think_payload: dict[str, Any] = {"type": "thinking", "content": text}
            if tok and isinstance(tok, int):
                think_payload["tokens"] = tok
                think_payload["reasoning_tokens"] = tok
            return [json.dumps(think_payload)]
        return None
    if rtype in ("tool", "tool_use"):
        tool_name = str(record.get("tool") or record.get("name") or part.get("tool") or part.get("name") or "tool")
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        out = []
        inp = state.get("input") if "input" in state else (record.get("input") if "input" in record else part.get("input"))
        if inp is not None:
            out.append(
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "tool_name": tool_name,
                        "tool_input": inp,
                        "content": f"tool_use {tool_name}: {_cap(_compact(inp))}",
                    }
                )
            )
        else:
            out.append(
                json.dumps({
                    "type": "message",
                    "role": "tool",
                    "tool_name": tool_name,
                    "content": f"tool_use {tool_name}",
                })
            )
        output = state.get("output") if "output" in state else (record.get("output") if "output" in record else part.get("output"))
        if output is not None:
            text_out = output if isinstance(output, str) else _compact(output)
            out.append(
                json.dumps(
                    {
                        "type": "message",
                        "role": "tool",
                        "tool_name": tool_name,
                        "tool_input": inp,
                        "tool_output": text_out,
                        "logs": text_out,
                        "content": f"tool_result: {_cap(text_out)}",
                    }
                )
            )
        return out or None
    if rtype == "tool_result":
        output = record.get("output") or record.get("content") or record.get("result") or part.get("output") or part.get("result") or ""
        text = output if isinstance(output, str) else _compact(output)
        tool_name = record.get("tool") or record.get("name") or part.get("tool")
        return [
            json.dumps({
                "type": "message",
                "role": "tool",
                "tool_name": tool_name,
                "tool_output": text,
                "logs": text,
                "content": f"tool_result: {_cap(text)}",
            })
        ]
    if rtype == "message":
        role = str(record.get("role") or "assistant")
        content = record.get("content")
        if isinstance(content, str) and content.strip():
            return [json.dumps({"type": "message", "role": role, "content": content.strip()})]
        if isinstance(content, list):
            out = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type == "text":
                        t = str(item.get("text") or "").strip()
                        if t:
                            out.append(json.dumps({"type": "message", "role": role, "content": t}))
                    elif item_type in ("thinking", "reasoning"):
                        t = str(item.get("thinking") or item.get("text") or "").strip()
                        if t:
                            out.append(json.dumps({"type": "thinking", "content": t}))
            return out or None
        return None
    if rtype in ("message.part.updated", "message.part.delta") or (rtype and str(rtype).startswith("message.part.")):
        if isinstance(part, dict):
            ptype = part.get("type")
            if ptype in ("thinking", "reasoning"):
                t = str(part.get("text") or part.get("thinking") or part.get("reasoning") or part.get("delta") or "").strip()
                if t:
                    return [json.dumps({"type": "thinking", "content": t})]
            elif ptype == "text":
                t = str(part.get("text") or part.get("content") or "").strip()
                if t:
                    return [json.dumps({"type": "message", "role": "assistant", "content": t})]
        delta = record.get("delta") or (record.get("properties", {}).get("delta") if isinstance(record.get("properties"), dict) else None)
        if delta and str(delta).strip():
            return [json.dumps({"type": "thinking", "content": str(delta).strip()})]
        return None
    if rtype == "error":
        msg = str(record.get("message") or record.get("error") or part.get("message") or part.get("error") or "")
        if msg:
            return [json.dumps({"type": "message", "role": "system", "content": f"Error: {msg}"})]
        return None
    return None


# ----------------------------------------------------------------------------
# Google Antigravity (agy --print - --output-format stream-json)
# ----------------------------------------------------------------------------


def translate_antigravity(record: dict[str, Any], session_dir: str) -> list[str] | None:
    """One Antigravity stream-json record → trace lines, or None to drop the line."""
    event = record.get("event")
    if event == "init":
        conversation_id = str(record.get("conversation_id") or "")
        init_data = record.get("init") if isinstance(record.get("init"), dict) else {}
        payload: dict[str, Any] = {
            "type": "logdir",
            "logdir": session_dir,
            "session_id": conversation_id,
        }
        model = init_data.get("model")
        if model:
            payload["model"] = str(model)
        return [json.dumps(payload)]

    if event == "step_update":
        update = record.get("step_update") if isinstance(record.get("step_update"), dict) else {}
        stype = update.get("step_type")
        state = update.get("state")
        tok_usage = update.get("token_usage") or update.get("usage") or {}
        prompt_tok = tok_usage.get("prompt_tokens") or tok_usage.get("input_tokens")
        comp_tok = tok_usage.get("completion_tokens") or tok_usage.get("output_tokens")
        tot_tok = tok_usage.get("total_tokens") or ((prompt_tok or 0) + (comp_tok or 0) if prompt_tok or comp_tok else None)

        if stype == "tool":
            tool_name = str(update.get("tool_name") or (update.get("tool_info") or {}).get("name") or "tool")
            tool_info = update.get("tool_info") if isinstance(update.get("tool_info"), dict) else {}
            tool_id = update.get("tool_id") or tool_info.get("id")
            if state == "ACTIVE":
                params = tool_info.get("parameters")
                if params is not None:
                    content = f"tool_use {tool_name}: {_cap(_compact(params))}"
                else:
                    content = f"tool_use {tool_name}"
                payload = {
                    "type": "message",
                    "role": "tool",
                    "tool_name": tool_name,
                    "tool_input": params,
                    "tool_id": tool_id,
                    "content": content,
                }
                if tot_tok:
                    payload["tokens"] = tot_tok
                    payload["prompt_tokens"] = prompt_tok
                    payload["completion_tokens"] = comp_tok
                return [json.dumps(payload)]
            elif state == "DONE":
                out = tool_info.get("output") if "output" in tool_info else update.get("output")
                logs = update.get("logs")
                exit_code = update.get("exit_code")
                if out is not None:
                    text_out = out if isinstance(out, str) else _compact(out)
                    payload = {
                        "type": "message",
                        "role": "tool",
                        "tool_name": tool_name,
                        "tool_output": text_out,
                        "logs": logs if logs is not None else text_out,
                        "tool_id": tool_id,
                        "exit_code": exit_code if exit_code is not None else 0,
                        "content": f"tool_result: {_cap(text_out)}",
                    }
                    if tot_tok:
                        payload["tokens"] = tot_tok
                        payload["prompt_tokens"] = prompt_tok
                        payload["completion_tokens"] = comp_tok
                    return [json.dumps(payload)]
                return None
            return None

        if stype == "agent_response":
            text = str(update.get("text_delta") or update.get("text") or update.get("content") or "").strip()
            if text:
                payload = {"type": "message", "role": "assistant", "content": text}
                if tot_tok:
                    payload["tokens"] = tot_tok
                    payload["prompt_tokens"] = prompt_tok
                    payload["completion_tokens"] = comp_tok
                return [json.dumps(payload)]
            return None

        if stype in ("thinking", "reasoning"):
            thought = str(
                update.get("thought")
                or update.get("thinking")
                or update.get("reasoning")
                or update.get("content")
                or update.get("text")
                or ""
            ).strip()
            if thought:
                payload = {"type": "thinking", "content": thought}
                if tot_tok:
                    payload["tokens"] = tot_tok
                    payload["reasoning_tokens"] = tot_tok
                return [json.dumps(payload)]
            return None

        if stype == "error":
            msg = str(update.get("error") or update.get("message") or "")
            if msg:
                return [json.dumps({"type": "message", "role": "system", "content": f"Error: {msg}"})]
            return None

        return None

    if event == "result":
        res = record.get("result") if isinstance(record.get("result"), dict) else {}
        status = res.get("status")
        out = []
        if status == "ERROR":
            err = res.get("error") or res.get("response") or "Antigravity execution failed"
            out.append(json.dumps({"type": "message", "role": "system", "content": f"Error: {err}"}))
        tok = res.get("usage") or res.get("token_usage") or record.get("usage") or record.get("token_usage")
        if isinstance(tok, dict):
            inp = tok.get("prompt_tokens") or tok.get("input_tokens") or 0
            out_tok = tok.get("completion_tokens") or tok.get("output_tokens") or 0
            tot = tok.get("total_tokens") or (inp + out_tok)
            out.append(json.dumps({
                "type": "usage",
                "prompt_tokens": inp,
                "completion_tokens": out_tok,
                "total_tokens": tot,
                "cost_usd": tok.get("cost_usd"),
            }))
        return out or None

    if event == "error":
        msg = str(record.get("error") or record.get("message") or "")
        if msg:
            return [json.dumps({"type": "message", "role": "system", "content": f"Error: {msg}"})]
        return None

    return None


# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------


def translate_line(line: str, fmt: str, session_dir: str = "") -> list[str] | None:
    """Translate one raw stdout line into trace lines.

    Returns a list of lines to emit, or None to drop the line. Non-JSON and
    unknown JSON pass through unchanged (returned as ``[line]``).
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return [line]
    try:
        record = json.loads(stripped)
    except json.JSONDecodeError:
        return [line]
    if not isinstance(record, dict):
        return [line]
    if fmt == CLAUDE:
        if record.get("type") not in _CLAUDE_TYPES:
            return [line]
        return translate_claude(record, session_dir)
    if fmt == CODEX:
        if record.get("type") not in _CODEX_TYPES:
            return [line]
        return translate_codex(record, session_dir)
    if fmt == OPENCODE:
        rec_type = str(record.get("type") or "")
        if rec_type not in _OPENCODE_TYPES and not rec_type.startswith("message.part."):
            return [line]
        return translate_opencode(record, session_dir)
    if fmt in (ANTIGRAVITY, AGY):
        if record.get("event") not in _ANTIGRAVITY_EVENTS:
            return [line]
        return translate_antigravity(record, session_dir)
    return [line]


async def _pump(proc: asyncio.subprocess.Process, fmt: str, session_dir: str) -> None:
    # §D13: opencode's CLI emits one step-start per agent step, each
    # translated into a logdir line — without dedupe, a single episode's
    # trace fills with repeated "session started" entries (one per step).
    # The bootstrap logdir line (no session_id) and the first step-start
    # (session_id attached) are the only two that carry information; exact
    # (logdir, session_id) repeats are dropped.
    emitted_sessions: set[tuple[str, str]] = set()
    while True:
        try:
            data = await proc.stdout.readline()  # type: ignore[union-attr]
        except (asyncio.LimitOverrunError, ValueError):
            # §D13: a single line beyond _MAX_LINE_BYTES. StreamReader.
            # readline() converts the over-limit LimitOverrunError into a
            # ValueError (after clearing its buffer) — catching only
            # LimitOverrunError let the ValueError escape and crash the
            # whole worker, and with it the episode, the moment any CLI
            # record exceeded the limit. The line is dropped and the pump
            # carries on; the buffer was already cleared by readline.
            continue
        if not data:
            return
        # Strip only the line terminator: passthrough lines keep their
        # original content, and print() supplies the single newline.
        text = data.decode("utf-8", errors="replace").rstrip("\n")
        for emitted in translate_line(text, fmt, session_dir) or []:
            if emitted.startswith("{"):
                try:
                    rec = json.loads(emitted)
                except json.JSONDecodeError:
                    rec = None
                if isinstance(rec, dict):
                    if rec.get("type") == "logdir":
                        key = (str(rec.get("logdir") or ""), str(rec.get("session_id") or ""))
                        if key in emitted_sessions:
                            continue
                        emitted_sessions.add(key)
                    if "ts" not in rec and "timestamp" not in rec and "time" not in rec and "created_at" not in rec:
                        rec["ts"] = time.time()
                        emitted = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
            print(emitted, flush=True)


_OPENCODE_FATAL_PATTERNS = (
    "Rate limit exceeded",
    "rate limit",
    "AI_APICallError",
)


def _is_opencode_fatal_error(line: str) -> str | None:
    if "level=ERROR" not in line:
        return None
    # Ignore harmless title generation errors unless rate limit / quota
    if "agent=title" in line and not any(p in line for p in ("Rate limit", "rate limit", "quota")):
        return None
    if "stream error" in line or any(p in line for p in _OPENCODE_FATAL_PATTERNS):
        import re
        m = re.search(r'error\.error="([^"]+)"', line)
        if m:
            return m.group(1)
        m = re.search(r'error="([^"]+)"', line)
        if m:
            return m.group(1)
        return line.strip()
    return None


async def _pump_stderr(
    proc: asyncio.subprocess.Process,
    fmt: str,
    fatal_event: asyncio.Event,
    fatal_holder: dict[str, str],
) -> None:
    if proc.stderr is None:
        return
    while True:
        try:
            data = await proc.stderr.readline()
        except Exception:
            break
        if not data:
            break
        line = data.decode("utf-8", errors="replace")
        sys.stderr.write(line)
        sys.stderr.flush()
        if fmt == OPENCODE and not fatal_event.is_set():
            err = _is_opencode_fatal_error(line)
            if err:
                fatal_holder["error"] = err
                fatal_event.set()
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass


async def _watch_opencode_log(
    proc: asyncio.subprocess.Process,
    start_offset: int,
    fatal_event: asyncio.Event,
    fatal_holder: dict[str, str],
) -> None:
    log_path = os.path.expanduser("~/.local/share/opencode/log/opencode.log")
    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start_offset)
            while proc.returncode is None and not fatal_event.is_set():
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    continue
                err = _is_opencode_fatal_error(line)
                if err:
                    fatal_holder["error"] = err
                    fatal_event.set()
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    break
    except Exception:
        pass


async def _fatal_killer(proc: asyncio.subprocess.Process, fatal_event: asyncio.Event) -> None:
    await fatal_event.wait()
    for _ in range(10):
        if proc.returncode is not None:
            return
        await asyncio.sleep(0.1)
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _run(fmt: str, command: list[str], session_dir: str) -> int:
    cmd = list(command)
    start_offset = 0
    if fmt == OPENCODE:
        if "--print-logs" not in cmd:
            if "run" in cmd:
                idx = cmd.index("run")
                cmd.insert(idx + 1, "--print-logs")
            else:
                cmd.append("--print-logs")
        if "--thinking" not in cmd:
            if "run" in cmd:
                idx = cmd.index("run")
                cmd.insert(idx + 1, "--thinking")
            else:
                cmd.append("--thinking")
        log_path = os.path.expanduser("~/.local/share/opencode/log/opencode.log")
        if os.path.exists(log_path):
            try:
                start_offset = os.path.getsize(log_path)
            except OSError:
                start_offset = 0

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=0,  # the prompt file, from the shell's `< {prompt_path}`
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if fmt == OPENCODE else None,
        limit=_MAX_LINE_BYTES,
    )

    fatal_event = asyncio.Event()
    fatal_holder: dict[str, str] = {}

    pump_task = asyncio.ensure_future(_pump(proc, fmt, session_dir))
    stderr_task = None
    log_watch_task = None
    killer_task = None

    if fmt == OPENCODE:
        stderr_task = asyncio.ensure_future(_pump_stderr(proc, fmt, fatal_event, fatal_holder))
        log_watch_task = asyncio.ensure_future(_watch_opencode_log(proc, start_offset, fatal_event, fatal_holder))
        killer_task = asyncio.ensure_future(_fatal_killer(proc, fatal_event))

    # Wait for process exit or fatal error trigger
    wait_proc = asyncio.ensure_future(proc.wait())
    wait_fatal = asyncio.ensure_future(fatal_event.wait())

    done, pending = await asyncio.wait([wait_proc, wait_fatal], return_when=asyncio.FIRST_COMPLETED)

    if fatal_event.is_set():
        wait_fatal.cancel()
        # Give process up to 1.5s to terminate
        try:
            await asyncio.wait_for(asyncio.shield(wait_proc), timeout=1.5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await wait_proc
        pump_task.cancel()
        if stderr_task:
            stderr_task.cancel()
        if log_watch_task:
            log_watch_task.cancel()
        if killer_task:
            killer_task.cancel()
        err = fatal_holder.get("error") or "opencode fatal stream error"
        print(json.dumps({"type": "message", "role": "system", "content": f"Error: {err}"}), flush=True)
        return proc.returncode or 1

    wait_fatal.cancel()
    await wait_proc
    await pump_task
    if stderr_task:
        await stderr_task
    if log_watch_task:
        log_watch_task.cancel()
    if killer_task:
        killer_task.cancel()
    return proc.returncode or 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", required=True, choices=(CLAUDE, CODEX, OPENCODE, ANTIGRAVITY, AGY))
    args, rest = parser.parse_known_args()
    # parse_known_args leaves a standalone "--" in the positional list
    # (a known argparse quirk); it's the separator, not part of the command.
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print("agent worker: missing command after '--'", file=sys.stderr)
        return 2
    session_dir = tempfile.mkdtemp(prefix=f"kusudaemon-{args.format}-")
    # Emit the logdir line up front (before the CLI's first record) so a
    # live surface sees a session the instant the episode starts; the
    # session_id-carrying line lands once init/thread.started arrives.
    print(json.dumps({"type": "logdir", "logdir": session_dir}), flush=True)

    stop = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop.wait(_HEARTBEAT_SECONDS):
            print(json.dumps({"type": "heartbeat", "ts": time.time()}), flush=True)

    heartbeat = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat.start()
    try:
        return asyncio.run(_run(args.format, rest, session_dir))
    except FileNotFoundError:
        print(f"agent worker: command not found: {rest[0]}", file=sys.stderr)
        return 127
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
