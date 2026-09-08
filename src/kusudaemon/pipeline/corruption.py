"""Artifact corruption detection for smart retry and redispatch framing.

Deterministic (0 tokens) analysis of an artifact to decide whether it can be
safely patched in place or must be completely regenerated from scratch.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..v1.gates import _count_words
from ..v1.tree import TaskNode

_MIN_SUBSTANTIVE_WORDS = 15
_MAX_CONSECUTIVE_IDENTICAL_LINES = 5
_REWRITE_MARKERS = ("[rewrite]", "--rewrite", "mode=rewrite", "mode=regenerate")


def check_artifact_text_corruption(
    text: str,
    node: TaskNode | None = None,
    defect: str | None = None,
) -> tuple[bool, str]:
    """Inspect artifact text for severe corruption or degeneracy.

    Returns (is_corrupted, reason).
    """
    if defect:
        defect_lower = defect.lower()
        if any(marker in defect_lower for marker in _REWRITE_MARKERS):
            return True, "explicit rewrite requested by operator"
        if "unrecoverable structural corruption" in defect_lower or "fatal corruption" in defect_lower:
            return True, "unrecoverable corruption flagged in defect"

    if not text or not text.strip():
        return True, "artifact is empty"

    if "\x00" in text:
        return True, "artifact contains null bytes / binary data"

    words = _count_words(text)
    if words < _MIN_SUBSTANTIVE_WORDS:
        return True, f"artifact is a stub ({words} words < {_MIN_SUBSTANTIVE_WORDS})"

    if node is not None:
        for gate in node.gates:
            if gate.startswith("len:"):
                arg = gate[4:]
                low_str, _, _ = arg.partition("-")
                try:
                    low = int(low_str)
                    if low >= 50 and words < int(low * 0.2):
                        return True, f"artifact far below minimum length ({words} words, gate requires {low})"
                except ValueError:
                    pass

    # Degenerate loop detection (repeated consecutive lines).
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        repeat_count = 1
        prev_line = lines[0]
        for line in lines[1:]:
            if line == prev_line and len(line) > 5:
                repeat_count += 1
                if repeat_count >= _MAX_CONSECUTIVE_IDENTICAL_LINES:
                    return True, f"degenerate line repetition loop detected ({repeat_count} consecutive identical lines)"
            else:
                repeat_count = 1
                prev_line = line

    return False, "healthy"


def is_artifact_corrupted(run_dir: str | Path, node: TaskNode) -> tuple[bool, str]:
    """Read node artifact from disk and evaluate whether it is corrupted.
    PLAN-TOKEN-ACCOUNTING.md §O10b: evaluated on concatenated artifact, never per part."""
    from ..v0.run_dir import node_artifact_text
    try:
        text = node_artifact_text(run_dir, node)
    except OSError as err:
        return True, f"artifact missing or unreadable ({err})"
    except UnicodeDecodeError:
        return True, "artifact is not valid UTF-8"

    return check_artifact_text_corruption(text, node=node, defect=node.last_defect)
