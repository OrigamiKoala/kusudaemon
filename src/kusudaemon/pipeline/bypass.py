"""Node and process bypass management.

Provides file-based IPC to dynamically cancel or bypass any in-flight process
(e.g., node review, exploration probe, writer episode) for any node so that
the pipeline can immediately proceed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..v0.events import EventLog
from ..v0.run_dir import events_path


def bypass_dir(run_dir: str | Path) -> Path:
    """Directory holding active bypass flags."""
    return Path(run_dir) / "bypass"


def _safe_token(node_id: str) -> str:
    return node_id.replace("/", "_").replace("\\", "_")


def set_node_bypass(
    run_dir: str | Path,
    node_id: str,
    process: str = "",
    *,
    reason: str = "bypassed by operator",
) -> None:
    """Set a bypass request on disk and append to events.jsonl."""
    run_dir = Path(run_dir)
    b_dir = bypass_dir(run_dir)
    b_dir.mkdir(parents=True, exist_ok=True)

    warning: str | None = None
    if process in ("review", "", "*", "all") and node_id not in ("*", "all"):
        try:
            from .corruption import check_artifact_text_corruption
            from ..v0.run_dir import node_artifact_text
            try:
                text = node_artifact_text(run_dir, node_id)
            except (FileNotFoundError, OSError):
                text = ""
            if not text.strip():
                warning = "artifact is missing or empty"
            else:
                corrupted, warn_reason = check_artifact_text_corruption(text)
                if corrupted:
                    warning = warn_reason
        except Exception as err:
            warning = f"artifact could not be read: {err}"

    if warning:
        import warnings
        warnings.warn(
            f"Bypassing review for node {node_id} with empty or corrupted artifact: {warning}",
            UserWarning,
            stacklevel=2,
        )

    payload = {
        "node_id": node_id,
        "process": process,
        "ts": time.time(),
        "reason": reason,
    }
    if warning:
        payload["warning"] = warning
    flag_file = b_dir / f"{_safe_token(node_id)}.json"
    flag_file.write_text(json.dumps(payload), encoding="utf-8")

    try:
        log = EventLog(events_path(run_dir))
        entry = {
            "node_id": node_id,
            "role": "harness",
            "round": 0,
            "type": "node_bypass_requested",
            "process": process,
            "detail": reason,
            "ts": time.time(),
        }
        if warning:
            entry["warning"] = warning
        log.append(entry)
        if warning:
            log.append(
                {
                    "node_id": node_id,
                    "role": "reviewer",
                    "round": 0,
                    "type": "node_bypass_warning",
                    "detail": f"review bypassed on empty or corrupted artifact: {warning}",
                    "warning": warning,
                    "ts": time.time(),
                }
            )
    except Exception:
        pass


def is_node_bypassed(
    run_dir: str | Path,
    node_id: str,
    process: str | None = None,
) -> bool:
    """Check whether a bypass has been requested for this node or process."""
    run_dir = Path(run_dir)
    b_dir = bypass_dir(run_dir)
    if not b_dir.exists():
        return False

    # Check specific node flag
    flag_file = b_dir / f"{_safe_token(node_id)}.json"
    if flag_file.exists():
        if process is None:
            return True
        try:
            data = json.loads(flag_file.read_text(encoding="utf-8"))
            req_process = data.get("process", "")
            if not req_process or req_process in (process, "*", "all"):
                return True
        except Exception:
            return True

    # Check wildcard / global bypass flags (e.g. "*", "all")
    for wildcard in ("*", "all"):
        w_file = b_dir / f"{wildcard}.json"
        if w_file.exists():
            if process is None:
                return True
            try:
                data = json.loads(w_file.read_text(encoding="utf-8"))
                req_process = data.get("process", "")
                if not req_process or req_process in (process, "*", "all"):
                    return True
            except Exception:
                return True

    return False


def clear_node_bypass(
    run_dir: str | Path,
    node_id: str,
) -> None:
    """Remove a bypass flag for a node (e.g., when a fresh retry or redispatch occurs)."""
    run_dir = Path(run_dir)
    b_dir = bypass_dir(run_dir)
    flag_file = b_dir / f"{_safe_token(node_id)}.json"
    if flag_file.exists():
        try:
            flag_file.unlink()
        except OSError:
            pass
