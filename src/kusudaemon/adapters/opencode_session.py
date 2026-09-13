"""Recover what an ``opencode run`` actually produced, from its session store.

PLAN-SWEEP-REPAIR.md §Q1. Arm A's artifact used to be the bare CLI's stdout and
nothing else. ``opencode run`` prints assistant *text* parts; it does not print
tool results. A coding agent handed a generation prompt does not answer in
prose -- it writes a script, runs it, and reads the file back -- so the finished
document leaves the process through a tool result and stdout keeps only the
narration around it. 301-block armA seed1 wrote a complete, compliant 100-block
document (174 582 chars, ``*** finished``) and was scored on 1 972 chars of
"Let me output the full document" / "The document is complete", i.e. 1 %.

Three channels have to be read, because opencode splits the truth across them:

1. ``part.data`` for ``type="text"`` -- the assistant's own words. When the
   model answers inline (000-week armA seeds 1 and 3) the document is here.
2. ``part.data`` for ``type="tool"`` -- ``state.output``. This is where a
   ``cat``-style hand-off lands.
3. ``tool-output/<id>`` -- opencode truncates a large tool result in the session
   row and replaces the body with ``...output truncated...`` plus
   ``Full output saved to: <path>``. The row for seed1's final ``cat`` held
   50 969 chars covering blocks 72-100; the spool file held all 100 blocks.
   Reading (2) without resolving (3) silently loses 71 % of the document and
   looks like a partial answer rather than a truncated read.

Everything here is read-only and opens the database with ``mode=ro`` (never
``immutable=1``, which hides WAL writes from a run that just finished).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "Candidate",
    "best_artifact",
    "candidates_for_sessions",
    "default_db_path",
    "default_log_path",
    "session_directory",
    "session_ids_from_log",
    "session_ids_from_output",
]

# opencode's data dir. XDG_DATA_HOME wins when set, as opencode itself honours it.
_TRUNCATED_RX = re.compile(r"Full output saved to:\s*(\S+)")
_CREATED_RX = re.compile(r"message=created id=(ses_[A-Za-z0-9]+)")
_RUN_RX = re.compile(r"\brun=([0-9a-f]{6,})")
_INSTANCE_RX = re.compile(r'message="creating instance" directory=(\S+)')


def _data_root() -> Path:
    env = os.getenv("OPENCODE_DATA_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.getenv("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def default_db_path() -> Path:
    return _data_root() / "opencode.db"


def default_log_path() -> Path:
    return _data_root() / "log" / "opencode.log"


def _spool_dir(db_path: Path | None = None) -> Path:
    """Where opencode parks a tool result too large for the session row.

    Derived from the database's own directory, never from ``$HOME``: the spool
    always sits beside ``opencode.db``, and a caller reading another machine's
    store (a mounted home, an archived copy) passes only ``db_path``. Resolving
    this against the reader's home silently disables truncation recovery and
    turns a complete 100-unit document back into the 29 units that happened to
    fit in the row.
    """
    root = Path(db_path).parent if db_path else _data_root()
    return root / "tool-output"


@dataclass
class Candidate:
    """One text blob an opencode session emitted, with where it came from."""

    kind: str            # "text" | "tool"
    source: str          # "assistant_text" | "tool_output" | "tool_output_spool"
    text: str
    session_id: str
    detail: str = ""     # the tool command, truncated, for provenance
    markers: int = 0

    @property
    def spooled(self) -> bool:
        return self.source == "tool_output_spool"


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else default_db_path()
    if not path.is_file():
        raise FileNotFoundError(f"opencode database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def session_ids_from_output(text: str) -> list[str]:
    """Session ids ``opencode run --print-logs`` announced in its own output.

    The authoritative live mapping: the process tells us which session it made,
    so no time-window guessing is needed while the run is still in hand.
    """
    seen: dict[str, None] = {}
    for m in _CREATED_RX.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def session_ids_from_log(
    workspace: str | os.PathLike[str],
    *,
    log_path: Path | None = None,
) -> list[str]:
    """Session ids of runs whose *first* instance bootstrapped in ``workspace``.

    Retrospective mapping for a run whose stdout is gone. opencode groups every
    line of one process under ``run=<id>``; the first ``creating instance``
    names the real cwd, so the cell is identified by its own workspace rather
    than by ``session.directory`` -- which, on a run that escaped its workspace
    (§Q2), is some other project entirely and identical across cells.
    """
    path = Path(log_path) if log_path else default_log_path()
    if not path.is_file():
        return []
    target = str(Path(workspace)).rstrip("/")
    target_name = Path(target).name
    first_dir: dict[str, str] = {}
    sessions: dict[str, list[str]] = {}
    with path.open(errors="replace") as fh:
        for line in fh:
            run = _RUN_RX.search(line)
            if not run:
                continue
            rid = run.group(1)
            inst = _INSTANCE_RX.search(line)
            if inst:
                first_dir.setdefault(rid, inst.group(1).rstrip("/"))
            created = _CREATED_RX.search(line)
            if created:
                sessions.setdefault(rid, []).append(created.group(1))
    out: list[str] = []
    for rid, d in first_dir.items():
        # Full path first; per-cell directory name as the fallback. The log
        # holds the paths as the machine that ran opencode saw them, and a
        # reader whose home is mounted elsewhere (the VM in BENCHMARKING.md
        # §4.1.5) would otherwise map nothing at all. Cell names
        # ("301-block_armA_seed1") are unique within a sweep.
        if d == target or Path(d).name == target_name:
            out.extend(sessions.get(rid, []))
    return out


def session_directory(session_id: str, *, db_path: Path | None = None) -> str | None:
    """The directory opencode actually bound the session to (§Q2 escape check)."""
    con = _connect(db_path)
    try:
        row = con.execute(
            "select directory from session where id = ?", (session_id,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _resolve_truncation(text: str, db_path: Path | None = None) -> tuple[str, bool]:
    """Swap a truncated tool result for its spooled full copy when available."""
    m = _TRUNCATED_RX.search(text[:4096])
    if not m:
        return text, False
    spool = _spool_dir(db_path) / os.path.basename(m.group(1))
    try:
        if spool.is_file():
            return spool.read_text(errors="replace"), True
    except OSError:
        pass
    return text, False


def candidates_for_sessions(
    session_ids: list[str],
    *,
    db_path: Path | None = None,
) -> Iterator[Candidate]:
    """Every text blob these sessions produced, spool indirection resolved."""
    if not session_ids:
        return
    resolved = Path(db_path) if db_path else default_db_path()
    con = _connect(resolved)
    try:
        for sid in session_ids:
            # Assistant messages only. The user turn is stored as a part too,
            # and a LongGenBench prompt ends with the first unit already filled
            # in ("*** started *** #*# Block 1 (0, 0):"), so echoing the prompt
            # back would score as a partial answer on a run that produced
            # nothing at all. An artifact must be something the agent emitted.
            rows = con.execute(
                "select p.data from part p join message m on m.id = p.message_id "
                "where p.session_id = ? and json_extract(m.data, '$.role') = 'assistant' "
                "order by p.time_created",
                (sid,),
            ).fetchall()
            for (data,) in rows:
                try:
                    part = json.loads(data)
                except (TypeError, ValueError):
                    continue
                kind = part.get("type")
                if kind == "text":
                    text = part.get("text") or ""
                    if text.strip():
                        yield Candidate("text", "assistant_text", text, sid)
                elif kind == "tool":
                    state = part.get("state") or {}
                    raw = state.get("output") or ""
                    if not raw.strip():
                        continue
                    inp = state.get("input") or {}
                    detail = str(inp.get("command") or inp.get("filePath") or "")[:120]
                    text, spooled = _resolve_truncation(raw, resolved)
                    yield Candidate(
                        "tool",
                        "tool_output_spool" if spooled else "tool_output",
                        text,
                        sid,
                        detail=detail,
                    )
    finally:
        con.close()


def best_artifact(
    session_ids: list[str],
    *,
    delimiter: str | None = None,
    db_path: Path | None = None,
) -> Candidate | None:
    """The most complete document these sessions emitted.

    Ranked by unit count first and length second when a ``delimiter`` is known,
    so a 100-block document beats a longer dump of source material that carries
    no units at all. Without a delimiter the longest blob wins, which is the
    best available guess and is why the benchmark runner passes one.
    """
    pattern = re.compile(re.escape(delimiter)) if delimiter else None
    best: Candidate | None = None
    for cand in candidates_for_sessions(session_ids, db_path=db_path):
        cand.markers = len(pattern.findall(cand.text)) if pattern else 0
        if pattern and cand.markers == 0:
            continue
        key = (cand.markers, len(cand.text))
        if best is None or key > (best.markers, len(best.text)):
            best = cand
    return best
