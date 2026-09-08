"""Run-directory path helpers layered on top of v0/v1/v2 (PLAN.md §5).

v3 adds the two things assembly/repair need that nothing below it created:

- ``assembly/`` — ``index.md`` (deterministic concatenation order,
  §4.6.1), ``checks.json`` (cross-cutting checks, §4.6.2), a compiled
  output file, and ``compile.log`` (§4.6.3: "exit code and log are the
  gate").
- ``out/.versions/<node_id>/`` — pre-repair snapshots (§5: "pre-repair
  snapshots"; §4.6: "snapshot to out/.versions/ before any repair").

Like ``spine_path``/``contract_path`` in v2's ``run_dir.py``, none of these
are pre-touched by ``create_run_dir`` — they don't exist until assembly or
repair actually runs.
"""

from __future__ import annotations

from pathlib import Path

from ..v0.run_dir import (  # noqa: F401 — re-exported for v3 callers
    create_run_dir,
    ensure_node_scratch_dir,
    ensure_node_trace_path,
    events_path,
    manifest_path,
    node_artifact_path,
    node_scratch_dir,
    node_trace_path,
    spec_path,
)
from ..v1.run_dir import (  # noqa: F401 — re-exported for v3 callers
    audit_dir,
    audit_path,
    ensure_audit_dir,
    ensure_audit_path,
    ensure_orchestrator_dir,
    orchestrator_dir,
    round_trace_path,
    tree_path,
)
from ..v2.run_dir import contract_path, spine_path  # noqa: F401 — re-exported for v3 callers


def assembly_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "assembly"
    path.mkdir(parents=True, exist_ok=True)
    return path


def assembly_index_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "index.md"


def assembly_checks_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "checks.json"


def assembly_output_path(run_dir: str | Path, filename: str = "main.md") -> Path:
    return assembly_dir(run_dir) / filename


def compile_log_path(run_dir: str | Path) -> Path:
    return assembly_dir(run_dir) / "compile.log"


def revalidation_dir(run_dir: str | Path) -> Path:
    path = Path(run_dir) / "audit" / "revalidation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def revalidation_audit_path(run_dir: str | Path, node_id: str) -> Path:
    """Separate from v1's ``audit/<node>.json`` (the normal per-dispatch
    review verdict) so a re-validation pass never clobbers the record of
    the review that originally passed the node."""
    return revalidation_dir(run_dir) / f"{node_id}.json"


def versions_dir(run_dir: str | Path, node_id: str) -> Path:
    path = Path(run_dir) / "out" / ".versions" / node_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def version_snapshot_path(run_dir: str | Path, node_id: str, tag: str) -> Path:
    return versions_dir(run_dir, node_id) / f"{tag}.md"


def _versions_dir_no_create(run_dir: str | Path, node_id: str) -> Path:
    """Read-path variant of versions_dir without the mkdir side effect.

    PLAN-SWEEP-REPAIR.md §A notes versions_dir creates its directory on every
    call, which makes `if v_dir.is_dir()` guards vacuous and litters empty
    .versions dirs on pure reads. Listing/restore paths use this instead."""
    return Path(run_dir) / "out" / ".versions" / node_id


def list_attempt_snapshots(run_dir: str | Path, node_id: str) -> list[Path]:
    """Pre-writer snapshots for a node, oldest-first.

    PLAN-SWEEP-REPAIR.md §B4 option (a): snapshots are either single files
    (attempt_<ts>.md, the legacy single-file layout) or directories
    (attempt_<ts>/, a copy of out/<node>/ part files). Only attempt_*
    entries count — repair outputs (1~repair1.md) and pilot-original.md share
    this directory but are not pre-writer state and must never feed the
    shrink comparison. Sorted by mtime so [-1] is the latest prior state."""
    v_dir = _versions_dir_no_create(run_dir, node_id)
    if not v_dir.is_dir():
        return []
    snaps: list[Path] = []
    try:
        for p in v_dir.glob("attempt_*.md"):
            if p.is_file():
                snaps.append(p)
        for p in v_dir.glob("attempt_*"):
            if p.is_dir():
                snaps.append(p)
    except OSError:
        return []
    try:
        snaps.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    return snaps


def read_attempt_snapshot_text(snap: Path) -> str:
    """Resolved text of one snapshot, mirroring node_artifact_text's rules."""
    if snap.is_dir():
        md_files = [p for p in snap.glob("*.md") if p.is_file()]
        if not md_files:
            return ""
        import re

        def _part_sort_key(p: Path) -> tuple[int, str]:
            nums = re.findall(r"\d+", p.name)
            return (int(nums[0]) if nums else 0, p.name)

        texts = []
        for p in sorted(md_files, key=_part_sort_key):
            try:
                t = p.read_text(encoding="utf-8")
                if t.strip():
                    texts.append(t.rstrip())
            except OSError:
                pass
        return "\n\n".join(texts) + "\n" if texts else ""
    return snap.read_text(encoding="utf-8")


def restore_attempt_snapshot(run_dir: str | Path, node_id: str, snap: Path) -> None:
    """Restore a snapshot taken by _snapshot_pre_writer (option (a)).

    Directory snapshots replace the current parts dir contents (the only
    granularity at which a lost part can come back without overwriting good
    parts with a stale concatenation); file snapshots replace the single
    file. The non-restored layout is cleared so the resolved read
    (parts-win in node_artifact_text) lands on the restored state instead of
    shadowing it into a silent no-op — the exact failure §B4 removes."""
    import contextlib
    import shutil

    from ..v0.run_dir import node_artifact_path, node_parts_dir, write_text_atomic

    run_dir_p = Path(run_dir)
    if snap.is_dir():
        parts_d = node_parts_dir(run_dir_p, node_id)
        parts_d.mkdir(parents=True, exist_ok=True)
        for existing in list(parts_d.glob("*.md")):
            with contextlib.suppress(OSError):
                existing.unlink()
        for src in sorted(snap.glob("*.md")):
            if src.is_file():
                shutil.copy2(src, parts_d / src.name)
        # The single file is shadowed by any non-empty parts dir — remove it
        # so the restored parts are what the next read resolves to.
        with contextlib.suppress(OSError):
            Path(node_artifact_path(run_dir_p, node_id)).unlink()
    else:
        text = snap.read_text(encoding="utf-8")
        parts_d = node_parts_dir(run_dir_p, node_id)
        if parts_d.is_dir():
            for existing in list(parts_d.glob("*.md")):
                with contextlib.suppress(OSError):
                    existing.unlink()
        write_text_atomic(node_artifact_path(run_dir_p, node_id), text)
