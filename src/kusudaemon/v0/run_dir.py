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


# PLAN-SWEEP-REPAIR.md §F6: typographic punctuation an agent's own writing
# introduces but its edit tool cannot then match.
#
# Observed live 2026-09-09 in `scratch/unit-02/trace.jsonl`, in the writer's own
# words: "the file has `skyscraper\u2019s` with a right single quotation mark
# (Unicode), and when I try to match with a regular apostrophe it doesn't work"
# — followed by "let me just use the write tool to write the entire file". That
# is the §O whole-file clobber, and this is what drives the model to it: an
# exact-match `edit` whose `oldString` cannot be reproduced byte-for-byte
# leaves rewriting as the only apparent way forward. The edit tool belongs to
# the backend CLI (OpenCode) and cannot be patched here, so remove the
# characters it trips over instead.
#
# Mapping is punctuation-only and meaning-preserving: quotes, dashes, ellipsis
# and exotic spaces. Nothing here touches letters, accents, or any non-Latin
# script — an artifact that is *supposed* to contain such text keeps it.
_TYPOGRAPHIC_PUNCTUATION = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u2032": "'", "\u02bc": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2033": '"', "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
    "\u2014": "--", "\u2015": "--", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ", "\u2007": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ",
    "\u200b": "", "\ufeff": "",
}
_TYPOGRAPHIC_TABLE = str.maketrans(_TYPOGRAPHIC_PUNCTUATION)


def normalize_typographic_punctuation(text: str) -> str:
    """Replace smart quotes, en/em dashes, ellipses and exotic spaces with
    their ASCII equivalents. Pure; safe to call on already-ASCII text."""
    return text.translate(_TYPOGRAPHIC_TABLE)


def normalize_artifact_punctuation(run_dir: str | Path, node_id: str) -> int:
    """Rewrite this node's artifact in place with ASCII punctuation.

    Handles both artifact layouts — the single ``out/<node>.md`` and any
    ``out/<node>/*.md`` part files — normalizing each file separately so the
    parts layout is preserved (a concatenate-then-write would collapse it).
    Returns the number of files actually changed; 0 when there was nothing to
    do, which is the common case. Never raises: a normalization failure must
    not prevent the episode from running.
    """
    changed = 0
    targets: list[Path] = []
    single = node_artifact_path(run_dir, node_id)
    if single.is_file():
        targets.append(single)
    parts = node_parts_dir(run_dir, node_id)
    if parts.is_dir():
        targets.extend(sorted(parts.glob("*.md")))
    for path in targets:
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        normalized = normalize_typographic_punctuation(original)
        if normalized == original:
            continue
        try:
            path.write_text(normalized, encoding="utf-8")
            changed += 1
        except OSError:
            continue
    return changed


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
