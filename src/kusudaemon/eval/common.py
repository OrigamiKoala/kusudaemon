"""Shared benchmarking, quarantine, and accounting utilities (PLAN-BENCH-INTEGRITY.md §4, §5)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

HaltCategory = Literal["ok", "transport", "budget", "agent", "unknown"]

import re

_STATUS_CODE_RE = re.compile(r"\b(?:404|429|500|502|503|504)\b")

_TRANSPORT_PATTERNS = (
    "http", "socket", "timeout", "timed out",
    "connection", "billing", "quota", "auth", "unauthorized",
    "forbidden", "api key", "rate limit", "overloaded",
    "kusudaemon_role_timeout", "role timeout", "remoteprotocolerror",
)

_BUDGET_PATTERNS = (
    "wall_clock_budget", "run budget", "budget exhausted",
)


def classify_halt(halt_reason: str | None) -> HaltCategory:
    """PLAN-BENCH-INTEGRITY.md §4.2: Classify a benchmark halt reason into
    'ok' | 'transport' | 'budget' | 'agent' | 'unknown'.

    PLAN-SWEEP-REPAIR.md §C2: a halt whose detail is literally "no detail"
    (e.g. "escalated in execute: no detail") is not evidence of model
    capability — it carries no signal about what stopped the run — so it
    classifies as "unknown", quarantined like transport/budget but counted
    separately so quarantine reason counts stay honest."""
    if not halt_reason or not str(halt_reason).strip():
        return "ok"
    low = str(halt_reason).strip().lower()
    if low in ("none", "done", "complete", "success", "resolved"):
        return "ok"
    if "no detail" in low:
        return "unknown"
    if any(p in low for p in _BUDGET_PATTERNS):
        return "budget"
    if _STATUS_CODE_RE.search(low) or any(p in low for p in _TRANSPORT_PATTERNS):
        return "transport"
    return "agent"


def check_identical_seeds(
    records: list[dict[str, Any]],
    *,
    key_field: str = "task_id",
    arm_field: str = "arm",
    size_extractor: Callable[[dict[str, Any]], int] | None = None,
) -> list[dict[str, Any]]:
    """PLAN-BENCH-INTEGRITY.md §4.2: Detect (task, arm) groups where every seed
    produced an identical output byte/char size."""
    grouped: dict[tuple[str, str], list[int]] = {}
    for r in records:
        if r.get("dry_run"):
            continue
        key = (str(r.get(key_field, "")), str(r.get(arm_field, "")))
        if size_extractor is not None:
            size = size_extractor(r)
        else:
            size = int(r.get("artifact_chars") or r.get("artifact_bytes") or len(r.get("prediction", "") or ""))
        grouped.setdefault(key, []).append(size)

    suspect = []
    for (task_id, arm), sizes in sorted(grouped.items()):
        if len(sizes) >= 2 and len(set(sizes)) == 1 and sizes[0] > 0:
            suspect.append({
                "task_id": task_id,
                "arm": arm,
                "seeds_count": len(sizes),
                "identical_size": sizes[0],
            })
    return suspect


def parse_flags(flags_arg: str | list[str] | None) -> dict[str, str]:
    """PLAN-BENCH-INTEGRITY.md §5: Parse comma- or whitespace-delimited flags into
    a dict of environment variables."""
    if not flags_arg:
        return {}
    if isinstance(flags_arg, list):
        items = []
        for a in flags_arg:
            items.extend(a.replace(",", " ").split())
    else:
        items = flags_arg.replace(",", " ").split()
    res: dict[str, str] = {}
    for item in items:
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k.startswith("KUSUDAEMON_"):
                k = f"KUSUDAEMON_{k.upper()}"
            res[k] = v
        else:
            k = item
            if not k.startswith("KUSUDAEMON_"):
                k = f"KUSUDAEMON_{k.upper()}"
            res[k] = "1"
    return res


def read_records_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read records from jsonl file, ignoring blank lines and errors."""
    p = Path(path)
    if not p.is_file():
        return []
    records = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def dedupe_records(
    records: list[dict[str, Any]],
    key_fields: tuple[str, ...] = ("task_id", "arm", "seed"),
) -> list[dict[str, Any]]:
    """De-duplicate records keeping the newest entry for each key tuple."""
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in records:
        key = tuple(r.get(f) for f in key_fields)
        seen[key] = r
    return list(seen.values())
