"""On-disk run directory for a single-node v0 run — a slice of PLAN.md §5's
full layout, just the pieces a single Writer node needs:

    <root>/<run-id>/
      spec.md
      events.jsonl
      manifest.jsonl
      scratch/<node_id>/trace.jsonl
      out/<node_id>.md
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def write_text_atomic(path: str | Path, text: str) -> None:
    """Truncate-proof write: temp file in the same directory, ``fsync``,
    ``os.replace`` (PLAN.md §11.6).

    ``Path.write_text`` truncates in place, so a kill -9 mid-write leaves a
    truncated tree.json / contract.md / phase.json / audit file — none of
    which has events.jsonl's torn-tail tolerance. ``os.replace`` is atomic
    on POSIX: readers see either the old file or the new one, never half a
    file. ``events.py`` documents the same reasoning for its fsync-per-append
    contract.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def create_run_dir(root: str | Path, run_id: str) -> Path:
    """Idempotent: safe to call again on resume, never wipes existing state."""
    run_dir = Path(root) / run_id
    (run_dir / "scratch").mkdir(parents=True, exist_ok=True)
    (run_dir / "out").mkdir(parents=True, exist_ok=True)
    for name in ("spec.md", "events.jsonl", "manifest.jsonl"):
        path = run_dir / name
        if not path.exists():
            path.touch()
    return run_dir


def spec_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "spec.md"


def events_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "events.jsonl"


def manifest_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "manifest.jsonl"


def cost_path(run_dir: str | Path) -> Path:
    """PLAN-EFFICIENCY-AND-HORIZON.md §M1: <run_dir>/cost.jsonl — append-only cost ledger."""
    return Path(run_dir) / "cost.jsonl"


def glossary_path(run_dir: str | Path) -> Path:
    """§C1: ``<run_dir>/glossary.json`` — the term → defining-location index
    the ``terms_defined`` warn-gate dereferences against (old PLAN §4.6's
    "is every used term in glossary.json?" check, shipped at warn severity).
    Pure getter, same convention as the siblings above: no create side
    effect, only the driver's plan phase ever writes it."""
    return Path(run_dir) / "glossary.json"


def node_scratch_dir(run_dir: str | Path, node_id: str) -> Path:
    """Pure getter — §11.10.14: no mkdir side effect. Read-only surfaces
    (dashboard, checks, assembler) must be able to resolve paths in runs
    they are only inspecting; writers call ``ensure_node_scratch_dir``."""
    return Path(run_dir) / "scratch" / node_id


def ensure_node_scratch_dir(run_dir: str | Path, node_id: str) -> Path:
    """``node_scratch_dir`` plus the mkdir — the variant writers call."""
    path = node_scratch_dir(run_dir, node_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def node_trace_path(run_dir: str | Path, node_id: str) -> Path:
    return node_scratch_dir(run_dir, node_id) / "trace.jsonl"


def ensure_node_trace_path(run_dir: str | Path, node_id: str) -> Path:
    """``node_trace_path`` plus the parent mkdir. The subprocess tees its
    live output into this file (v0/runner.py), so the directory must exist
    before the agent starts, even though the path is also read by the
    dashboard for its logdir discovery."""
    ensure_node_scratch_dir(run_dir, node_id)
    return node_trace_path(run_dir, node_id)


def node_artifact_path(run_dir: str | Path, node_id: str) -> Path:
    return Path(run_dir) / "out" / f"{node_id}.md"


def node_parts_dir(run_dir: str | Path, node_id: str) -> Path:
    """Directory holding part files for multi-part artifacts (PLAN-TOKEN-ACCOUNTING.md §O5a)."""
    return Path(run_dir) / "out" / node_id


def ensure_node_parts_dir(run_dir: str | Path, node_id: str) -> Path:
    d = node_parts_dir(run_dir, node_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def node_artifact_text(run_dir: str | Path, node_or_id: str | Any) -> str:
    """Read a node's artifact text, resolving part files or single-file layout (PLAN-TOKEN-ACCOUNTING.md §O5a)."""
    node_id = getattr(node_or_id, "id", node_or_id)
    parts_d = node_parts_dir(run_dir, node_id)
    if parts_d.is_dir():
        md_files = [p for p in parts_d.glob("*.md") if p.is_file()]
        if md_files:
            import re

            def _part_sort_key(p: Path) -> tuple[int, str]:
                nums = re.findall(r"\d+", p.name)
                return (int(nums[0]) if nums else 0, p.name)

            sorted_parts = sorted(md_files, key=_part_sort_key)
            texts = []
            for p in sorted_parts:
                try:
                    t = p.read_text(encoding="utf-8")
                    # §O10a: Retiring a part is elision; skip empty or whitespace parts
                    if t.strip():
                        texts.append(t.rstrip())
                except OSError:
                    pass
            if texts:
                return "\n\n".join(texts) + "\n"

    path = node_artifact_path(run_dir, node_id)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Artifact missing for node {node_id} at {path} or {parts_d}")


def resolve_stored(run_dir: str | Path, ref: str) -> Path:
    """Turn a path *stored on disk* (``tree.json``'s ``inputs``/``artifact``,
    a manifest promotion path) into a real filesystem path.

    PLAN.md §D0b's rule, stated once: a path stored on disk is relative to
    ``run_dir`` so the run directory stays movable; a path already absolute
    (e.g. hand-authored, or a future workspace-mode input outside the run
    dir) is returned unchanged. Every reader of a stored path — prompt
    building, the dashboard's input-token/exists checks, document review,
    the assembler — must call this instead of re-deriving the rule ad hoc.
    Lives here (not in pipeline/run_dir.py) so v0-v3 modules can use it
    without inverting the dependency direction onto pipeline/.
    """
    ref_path = Path(ref)
    if ref_path.is_absolute():
        return ref_path
    return Path(run_dir) / ref_path
