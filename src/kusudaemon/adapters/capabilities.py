"""Backend capability mapping and translation (PLAN.md §11 / BACKEND-PARITY-AUDIT).

Translates canonical capability requests (tool allowlists, web search,
context length limits, hidden path restrictions) into backend-native options
and emits structured `capability_unavailable` events when a backend cannot
honor a capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .claude_permissions import ALL_CLAUDE_TOOLS, path_deny_rules
from .tools.searxng_search import SEARXNG_TOOL_PATH
from .tools.workspace_read import WORKSPACE_READ_TOOL_PATH

# Canonical tool names to Claude Code tool names
CANONICAL_TO_CLAUDE: dict[str, tuple[str, ...]] = {
    "read": ("Read", "Glob", "Grep"),
    "write": ("Write", "Edit"),
    "edit": ("Edit", "Write"),
    "save": ("Write", "Edit"),
    "patch": ("Edit", "Write"),
    "shell": ("Bash",),
    "bash": ("Bash",),
    "websearch": ("WebSearch",),
    "web_search": ("WebSearch",),
    "web": ("WebSearch",),
    "list": ("Glob", "Grep"),
    "grep": ("Grep",),
    "glob": ("Glob",),
    str(SEARXNG_TOOL_PATH): ("WebSearch",),
    str(WORKSPACE_READ_TOOL_PATH): ("Read", "Glob", "Grep"),
}

# Canonical tool names to OpenCode permission keys
CANONICAL_TO_OPENCODE: dict[str, tuple[str, ...]] = {
    "read": ("read", "glob", "grep"),
    "write": ("edit", "write"),
    "edit": ("edit", "write"),
    "save": ("edit", "write"),
    "patch": ("edit",),
    "shell": ("bash",),
    "bash": ("bash",),
    "websearch": ("web_search", "websearch", "webfetch"),
    "web_search": ("web_search", "websearch", "webfetch"),
    "web": ("web_search", "websearch", "webfetch"),
    "list": ("read", "glob"),
    "grep": ("read", "grep"),
    "glob": ("read", "glob"),
    str(SEARXNG_TOOL_PATH): ("web_search", "websearch", "webfetch"),
    str(WORKSPACE_READ_TOOL_PATH): ("read", "glob", "grep"),
}

# Canonical tool names to Antigravity tool names
CANONICAL_TO_ANTIGRAVITY: dict[str, tuple[str, ...]] = {
    "read": ("view_file", "list_dir", "grep_search"),
    "write": ("write_to_file", "replace_file_content", "multi_replace_file_content"),
    "edit": ("replace_file_content", "multi_replace_file_content", "write_to_file"),
    "save": ("write_to_file",),
    "patch": ("replace_file_content", "multi_replace_file_content"),
    "shell": ("run_command",),
    "bash": ("run_command",),
    "websearch": ("search_web", "read_url_content"),
    "web_search": ("search_web", "read_url_content"),
    "web": ("search_web", "read_url_content"),
    "list": ("list_dir", "grep_search"),
    "grep": ("grep_search",),
    "glob": ("list_dir",),
    str(SEARXNG_TOOL_PATH): ("search_web", "read_url_content"),
    str(WORKSPACE_READ_TOOL_PATH): ("view_file", "list_dir", "grep_search"),
}


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    supports_context_length: bool
    supports_tool_allowlist: bool
    supports_tool_denylist: bool
    supports_path_deny: bool
    supports_web_search: bool


CAPABILITIES: dict[str, BackendCapabilities] = {
    "gptme": BackendCapabilities(
        name="gptme",
        supports_context_length=True,
        supports_tool_allowlist=True,
        supports_tool_denylist=False,
        supports_path_deny=False,
        supports_web_search=True,
    ),
    "claude": BackendCapabilities(
        name="claude",
        supports_context_length=False,
        supports_tool_allowlist=True,
        supports_tool_denylist=True,
        supports_path_deny=True,
        supports_web_search=True,
    ),
    "codex": BackendCapabilities(
        name="codex",
        supports_context_length=False,
        supports_tool_allowlist=False,
        supports_tool_denylist=False,
        supports_path_deny=False,
        supports_web_search=True,
    ),
    "opencode": BackendCapabilities(
        name="opencode",
        supports_context_length=False,
        supports_tool_allowlist=True,
        supports_tool_denylist=True,
        supports_path_deny=True,
        supports_web_search=True,
    ),
    "antigravity": BackendCapabilities(
        name="antigravity",
        supports_context_length=False,
        supports_tool_allowlist=True,
        supports_tool_denylist=True,
        supports_path_deny=True,
        supports_web_search=True,
    ),
    "agy": BackendCapabilities(
        name="agy",
        supports_context_length=False,
        supports_tool_allowlist=True,
        supports_tool_denylist=True,
        supports_path_deny=True,
        supports_web_search=True,
    ),
}


def get_backend_capabilities(backend: str) -> BackendCapabilities:
    name = str(backend).strip().lower()
    if name not in CAPABILITIES:
        raise ValueError(f"unknown backend: {backend!r}")
    return CAPABILITIES[name]


def emit_capability_event(
    run_dir: str | Path | None,
    node_id: str,
    backend: str,
    capability: str,
    reason: str,
    role: str = "writer",
) -> None:
    """Emit a structured capability_unavailable event to events.jsonl."""
    if run_dir is None:
        return
    try:
        from ..v0.events import EventLog
        from ..v0.run_dir import events_path

        p = events_path(Path(run_dir))
        if p.parent.exists():
            EventLog(p).append(
                {
                    "node_id": node_id or "-",
                    "role": role,
                    "type": "capability_unavailable",
                    "backend": backend,
                    "capability": capability,
                    "reason": reason,
                }
            )
    except Exception:
        pass


def translate_tools_to_claude_disallowed(
    allowed_tools: tuple[str, ...],
    *,
    include_web_search: bool = False,
) -> list[str]:
    """Invert an allowlist of canonical tools into Claude Code's --disallowedTools."""
    allowed_claude: set[str] = set()
    for tool in allowed_tools:
        mapped = CANONICAL_TO_CLAUDE.get(tool)
        if mapped:
            allowed_claude.update(mapped)
        else:
            allowed_claude.add(tool)
    if include_web_search:
        allowed_claude.add("WebSearch")

    disallowed = [t for t in ALL_CLAUDE_TOOLS if t not in allowed_claude]
    return sorted(disallowed)


def translate_tools_to_opencode_permissions(
    allowed_tools: tuple[str, ...],
    *,
    include_web_search: bool = False,
    hidden_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Translate canonical allowlist into OpenCode permissions configuration."""
    allowed_opencode: set[str] = set()
    for tool in allowed_tools:
        mapped = CANONICAL_TO_OPENCODE.get(tool)
        if mapped:
            allowed_opencode.update(mapped)
    if include_web_search:
        allowed_opencode.update({"web_search", "websearch", "webfetch"})

    perms: dict[str, str] = {}
    all_known = (
        "read",
        "edit",
        "write",
        "bash",
        "web_search",
        "websearch",
        "webfetch",
        "task",
        "glob",
        "grep",
        "question",
    )
    for k in all_known:
        if k in allowed_opencode:
            perms[k] = "allow"
        else:
            perms[k] = "deny"
    if not allowed_tools and not include_web_search:
        perms["*"] = "deny"
    return perms
