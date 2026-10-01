#!/usr/bin/env python3
"""Data-integrity helpers for the LongGenBench sweep.

docs/LONGGENBENCH-RESULTS-2026-09.md §4.3 listed nine benchmark and
data-integrity bugs. The logic that fixes them lives here so the sweep runner
(``run_longgen_bench.py``), the recompute script (``longgen_recompute.py``) and
the tests share one implementation:

- artifact path resolution that survives a dead VM path (§4.3.3)
- attempt numbering, first-attempt vs final selection (§4.3.6)
- bench / records / raw reconciliation (§4.3.4)
- a summary that refuses an incomplete matrix and reports the headline with
  and without quarantine (§4.3.5)
- code provenance with a dirty flag and a diff hash (§4.3.7)
- arm-A tokens and provider errors read from the OpenCode store (§4.3.8, §4.3.9)

Everything that touches OpenCode's database or log is read-only: the database
is opened with ``mode=ro``, never written, never vacuumed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import classify_halt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# The final 20-task matrix of the 2026-09 sweep: the 23 tasks in the results
# folder minus the three that sit outside it (see the results doc, §6).
DEFAULT_EXCLUDED_TASKS = ("300-block", "100-floor", "287-menu-week")
DEFAULT_ARMS = ("A", "C")
DEFAULT_SEEDS = (1, 2, 3)

_STEM_RE = re.compile(r"^(?P<task>.+)_arm(?P<arm>[A-Z])_seed(?P<seed>\d+)$")


# --------------------------------------------------------------------------
# cell identity and artifact paths
# --------------------------------------------------------------------------


def cell_stem(task_id: str, arm: str, seed: int) -> str:
    return f"{task_id}_arm{arm}_seed{seed}"


def parse_stem(stem: str) -> tuple[str, str, int] | None:
    m = _STEM_RE.match(stem)
    if not m:
        return None
    return m.group("task"), m.group("arm"), int(m.group("seed"))


def rec_cell(rec: dict[str, Any]) -> tuple[str, str, int]:
    return str(rec.get("task_id")), str(rec.get("arm")), int(rec.get("seed") or 0)


def expected_cells(
    task_ids: Iterable[str],
    arms: Iterable[str] = DEFAULT_ARMS,
    seeds: Iterable[int] = DEFAULT_SEEDS,
) -> list[tuple[str, str, int]]:
    return [(t, a, int(s)) for t in sorted(set(task_ids)) for a in arms for s in seeds]


def resolve_artifact_path(rec: dict[str, Any], results_dir: Path) -> Path | None:
    """Find the artifact file behind a record.

    ``artifact_path`` in a record is whatever path the machine that scored it
    saw. Rows written from a Linux VM point at ``/sessions/rcw-.../mnt/...``,
    which does not exist on the machine that reads them: 28 prediction files
    were built from nothing because of that. The file itself always lives in
    ``<results_dir>/raw/``, named after the cell, so resolve in that order:
    the recorded path if it exists, the recorded basename under ``raw/``, then
    ``raw/<stem>.{txt,md}``.
    """
    results_dir = Path(results_dir)
    raw_dir = results_dir / "raw"
    recorded = rec.get("artifact_path")
    if recorded:
        p = Path(str(recorded))
        if p.is_file():
            return p
        alt = raw_dir / p.name
        if alt.is_file():
            return alt
    try:
        stem = cell_stem(str(rec["task_id"]), str(rec["arm"]), int(rec["seed"]))
    except (KeyError, TypeError, ValueError):
        return None
    for suffix in (".txt", ".md"):
        cand = raw_dir / f"{stem}{suffix}"
        if cand.is_file():
            return cand
    return None


# --------------------------------------------------------------------------
# validity / quarantine (reads classify_halt, never changes it)
# --------------------------------------------------------------------------


def is_complete(rec: dict[str, Any]) -> bool:
    expected = int(rec.get("blocks_expected") or rec.get("number") or 0)
    found = int(rec.get("blocks_found") or 0)
    if expected > 0 and found >= expected:
        return True
    return float(rec.get("completion_rate") or 0.0) >= 100.0


def record_validity(rec: dict[str, Any]) -> tuple[bool, str | None]:
    """(valid, invalid_reason) for one record.

    ``transport`` and ``budget`` halts are quarantined (the provider or the
    clock stopped the run, so it is not a capability measurement) unless the
    document was already complete. ``unknown`` ("escalated in execute: no
    detail") is NOT quarantined: it carries no evidence of a provider fault,
    and three runs excluded that way were real failures (177-s3 54 %,
    200-s1 4 %, 294-s3 33 %), which moved arm C's mean from 93.9 % to 99.0 %.
    A run with an unexplained halt and an incomplete document counts against
    the arm.
    """
    category = classify_halt(rec.get("halt_reason"), float(rec.get("completion_rate") or 0.0))
    quarantined = category in ("transport", "budget")
    if quarantined and not is_complete(rec):
        return False, category
    return True, None


# --------------------------------------------------------------------------
# attempts
# --------------------------------------------------------------------------


def assign_attempts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tag each record with ``attempt`` (1-based, per cell), in place.

    ``records.jsonl`` is append-only and holds two kinds of rows for a cell:
    a real run (the harness spent wall clock: ``harness_wall_clock_s > 0``) and
    a ``--score-only`` rescore of an artifact that already exists
    (``harness_wall_clock_s == 0``). A new attempt starts at each real run; a
    rescore belongs to the attempt before it. The first row of a cell always
    starts attempt 1. A row that already carries ``attempt`` keeps it and later
    untagged rows continue from it.
    """
    current: dict[tuple[str, str, int], int] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        key = rec_cell(rec)
        if rec.get("attempt"):
            current[key] = int(rec["attempt"])
            continue
        real_run = float(rec.get("harness_wall_clock_s") or 0.0) > 0.0
        if key not in current:
            current[key] = 1
        elif real_run:
            current[key] += 1
        rec["attempt"] = current[key]
    return records


def latest_per_cell(records: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """The final row of the final attempt of each cell (file order = time)."""
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        key = rec_cell(rec)
        prev = out.get(key)
        if prev is None or int(rec.get("attempt") or 1) >= int(prev.get("attempt") or 1):
            out[key] = rec
    return out


def first_attempt_per_cell(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    """The final row of attempt 1 of each cell (a rescore of attempt 1 wins)."""
    out: dict[tuple[str, str, int], dict[str, Any]] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        if int(rec.get("attempt") or 1) != 1:
            continue
        out[rec_cell(rec)] = rec
    return out


def attempt_counts(records: list[dict[str, Any]]) -> dict[tuple[str, str, int], int]:
    counts: dict[tuple[str, str, int], int] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        key = rec_cell(rec)
        counts[key] = max(counts.get(key, 0), int(rec.get("attempt") or 1))
    return counts


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------


def reconcile_cells(
    results_dir: Path,
    cells: Iterable[tuple[str, str, int]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Check that bench/, records.jsonl and raw/ agree for the selected matrix.

    ``missing_*`` lists name cells that are in the matrix but absent from that
    source; ``unresolvable_paths`` names records whose artifact cannot be found
    by ``resolve_artifact_path`` although they claim one; ``extra_*`` lists
    name what a source holds beyond the matrix (informational, not an error).
    A raw artifact is also considered present when the record says
    ``artifact_absent`` (the run left nothing at all; documented, not hidden).
    """
    results_dir = Path(results_dir)
    cells = list(cells)
    wanted = {cell_stem(*c) for c in cells}
    bench_dir = results_dir / "bench"
    raw_dir = results_dir / "raw"
    bench = {p.stem for p in bench_dir.glob("*.json")} if bench_dir.is_dir() else set()
    raw = (
        {
            p.stem
            for p in raw_dir.iterdir()
            if p.is_file() and p.suffix in (".txt", ".md") and parse_stem(p.stem)
        }
        if raw_dir.is_dir()
        else set()
    )
    latest = latest_per_cell(records)
    rec_stems = {cell_stem(*k) for k in latest}

    absent_ok = {cell_stem(*k) for k, r in latest.items() if r.get("artifact_absent")}
    unresolvable: list[str] = []
    for key, rec in sorted(latest.items()):
        stem = cell_stem(*key)
        if stem not in wanted:
            continue
        if rec.get("artifact_absent"):
            continue
        if rec.get("artifact_path") or (rec.get("artifact_chars") or 0) > 0:
            if resolve_artifact_path(rec, results_dir) is None:
                unresolvable.append(stem)

    report = {
        "expected_cells": len(wanted),
        "missing_bench": sorted(wanted - bench),
        "missing_records": sorted(wanted - rec_stems),
        "missing_raw": sorted(wanted - raw - absent_ok),
        "unresolvable_paths": unresolvable,
        "extra_bench": sorted(bench - wanted),
        "extra_records": sorted(rec_stems - wanted),
        "extra_raw": sorted(raw - wanted),
    }
    report["ok"] = not (
        report["missing_bench"]
        or report["missing_records"]
        or report["missing_raw"]
        or report["unresolvable_paths"]
    )
    return report


def format_reconcile(report: dict[str, Any]) -> str:
    lines = [f"reconcile: {report['expected_cells']} expected cells, "
             f"{'OK' if report['ok'] else 'MISMATCH'}"]
    for key in ("missing_bench", "missing_records", "missing_raw", "unresolvable_paths"):
        if report.get(key):
            lines.append(f"  {key} ({len(report[key])}): {', '.join(report[key])}")
    for key in ("extra_bench", "extra_records", "extra_raw"):
        if report.get(key):
            lines.append(f"  {key} (outside the matrix, informational): {len(report[key])}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# code provenance
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(cwd), stderr=subprocess.DEVNULL, text=True
        )
    except Exception:
        return None


def code_provenance(repo: Path = REPO_ROOT, code_paths: tuple[str, ...] = ("src", "scripts")) -> dict[str, Any]:
    """Which code a run really used: HEAD, a dirty flag and a diff hash.

    ``commit`` alone is HEAD, and the harness was patched constantly with the
    fixes sitting uncommitted, so cells ran new code under an old hash. This
    records ``git describe --always --dirty`` (``code_version``), the HEAD
    sha, whether the code paths differ from HEAD, and a sha256 over the
    tracked diff plus the contents of untracked files under those paths.
    ``code_dirty`` is None and the hash None when git is unavailable.
    """
    describe = _git(["describe", "--always", "--dirty"], repo)
    head = _git(["rev-parse", "HEAD"], repo)
    if describe is None or head is None:
        return {"code_version": None, "code_commit": None, "code_dirty": None, "code_diff_sha256": None}
    diff = _git(["diff", "HEAD", "--", *code_paths], repo) or ""
    untracked = (_git(["ls-files", "--others", "--exclude-standard", "--", *code_paths], repo) or "").split()
    h = hashlib.sha256()
    h.update(diff.encode("utf-8", "replace"))
    for name in sorted(untracked):
        h.update(b"\0" + name.encode())
        try:
            h.update((repo / name).read_bytes())
        except OSError:
            pass
    dirty = bool(diff.strip() or untracked)
    return {
        "code_version": describe.strip(),
        "code_commit": head.strip(),
        "code_dirty": dirty,
        "code_diff_sha256": h.hexdigest() if dirty else None,
    }


UNKNOWN_PROVENANCE = {
    "code_version": "unknown",
    "code_commit": None,
    "code_dirty": "unknown",
    "code_diff_sha256": None,
}


# --------------------------------------------------------------------------
# OpenCode store (read-only): tokens and provider errors
# --------------------------------------------------------------------------


def _default_db() -> Path:
    from kusudaemon.adapters.opencode_session import default_db_path

    return default_db_path()


def _default_log() -> Path:
    from kusudaemon.adapters.opencode_session import default_log_path

    return default_log_path()


def opencode_session_tokens(
    session_ids: Iterable[str],
    db_path: Path | None = None,
) -> dict[str, int]:
    """Sum ``message.data.tokens`` over assistant messages of these sessions.

    ``opencode run --print-logs`` prints no usage the parser can read, so arm
    A's ``tokens_by_role`` was always empty. The session store holds exact
    per-message counts: ``total`` (which includes cache reads), ``input``,
    ``output``, ``reasoning`` and ``cache.{read,write}``. Read-only.
    """
    sids = [s for s in dict.fromkeys(session_ids) if s]
    totals = {
        "total": 0, "input": 0, "output": 0, "reasoning": 0,
        "cache_read": 0, "cache_write": 0, "messages": 0, "sessions": 0,
    }
    if not sids:
        return totals
    path = Path(db_path) if db_path else _default_db()
    if not path.is_file():
        raise FileNotFoundError(f"opencode database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for sid in sids:
            rows = con.execute(
                "select json_extract(data,'$.tokens.total'), json_extract(data,'$.tokens.input'),"
                " json_extract(data,'$.tokens.output'), json_extract(data,'$.tokens.reasoning'),"
                " json_extract(data,'$.tokens.cache.read'), json_extract(data,'$.tokens.cache.write')"
                " from message where session_id = ? and json_extract(data,'$.role') = 'assistant'"
                " and json_extract(data,'$.tokens') is not null",
                (sid,),
            ).fetchall()
            if rows:
                totals["sessions"] += 1
            for tot, inp, out, rea, cr, cw in rows:
                totals["messages"] += 1
                totals["total"] += int(tot or 0)
                totals["input"] += int(inp or 0)
                totals["output"] += int(out or 0)
                totals["reasoning"] += int(rea or 0)
                totals["cache_read"] += int(cr or 0)
                totals["cache_write"] += int(cw or 0)
    finally:
        con.close()
    return totals


_SESSION_RX = re.compile(r"\bsession\.id=(ses_[A-Za-z0-9]+)")


def classify_provider_error(line: str) -> str:
    low = line.lower()
    if "too many requests" in low or "rate limit" in low or re.search(r"\b429\b", low):
        return "http_429"
    if "headertimeout" in low or "headers timed out" in low or "header timeout" in low:
        return "header_timeout"
    if (
        "internal server error" in low
        or "internal_server_error" in low
        or re.search(r"error\.error\.code=5\d\d|\bstatus(?:code)?[= :]5\d\d\b|\b50[0234]\b", low)
    ):
        return "http_5xx"
    return "other"


def opencode_provider_errors(
    session_ids: Iterable[str],
    log_path: Path | None = None,
) -> dict[str, int]:
    """Count provider ``stream error`` lines opencode logged for these sessions.

    Returns ``{"http_429": n, "http_5xx": n, "header_timeout": n, "other": n,
    "total": n}`` plus ``last_error_ts`` (ISO time of the last such line) when
    there was one. Read-only. A missing log yields zeros (the caller records
    that the check could not run, rather than that no error happened, via
    ``log_available``).
    """
    wanted = {s for s in session_ids if s}
    counts = {"http_429": 0, "http_5xx": 0, "header_timeout": 0, "other": 0, "total": 0}
    if not wanted:
        return counts
    path = Path(log_path) if log_path else _default_log()
    if not path.is_file():
        raise FileNotFoundError(f"opencode log not found: {path}")
    with path.open(errors="replace") as fh:
        for line in fh:
            if "level=ERROR" not in line or "stream error" not in line:
                continue
            m = _SESSION_RX.search(line)
            if not m or m.group(1) not in wanted:
                continue
            counts[classify_provider_error(line)] += 1
            counts["total"] += 1
            ts = re.search(r"timestamp=(\S+)", line)
            if ts:
                counts["last_error_ts"] = ts.group(1)  # type: ignore[assignment]
    return counts


def provider_error_halt_reason(errors: dict[str, int]) -> str | None:
    """A ``halt_reason`` that ``classify_halt`` files as ``transport``."""
    if not errors or not errors.get("total"):
        return None
    parts = []
    if errors.get("http_429"):
        parts.append(f"{errors['http_429']}x HTTP 429 Too Many Requests")
    if errors.get("http_5xx"):
        parts.append(f"{errors['http_5xx']}x HTTP 500 Internal server error")
    if errors.get("header_timeout"):
        parts.append(f"{errors['header_timeout']}x provider response headers timed out")
    if errors.get("other"):
        parts.append(f"{errors['other']}x other provider stream error")
    return "provider unavailable during run: " + ", ".join(parts)


# --------------------------------------------------------------------------
# arm A record write-back
# --------------------------------------------------------------------------


def arm_a_termination(
    completion_rate: float, halt_reason: str | None, timed_out: bool = False
) -> str:
    """The real ending of a bare-agent run, from what it produced.

    ``complete``: every unit present. ``harness_timeout``: the wall clock ended
    it. ``provider_error``: the provider failed and the document is short.
    ``stopped_early``: the agent ended its own turn with an incomplete
    document (the usual case; opencode exits 0 whatever happened).
    """
    if completion_rate >= 100.0:
        return "complete"
    if timed_out:
        return "harness_timeout"
    category = classify_halt(halt_reason, completion_rate)
    if category == "transport":
        return "provider_error"
    if category == "budget":
        return "harness_timeout"
    return "stopped_early"


def write_back_scored_record(
    record_path: Path,
    *,
    arm: str,
    completion_rate: float,
    blocks_found: int,
    blocks_expected: int,
    halt_reason: str | None = None,
    timed_out: bool = False,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write the scored outcome back into ``bench/<stem>.json``.

    ``kusudaemon bench --arm A`` fills ``score``/``resolved``/``termination``
    from the process exit code (opencode exits 0 after any turn). The runner
    knows the real completion rate only after scoring the artifact, so it
    writes back ``completion_rate``, ``score = CR / 100``, ``resolved = CR >=
    100`` and a real ``termination`` (arm A). For arm C the harness's own
    ``score``/``resolved``/``termination`` stay (they describe the harness's
    view, and 24 of 60 runs at 100 % carry ``resolved: false`` because of
    harness bugs); arm C gets ``completion_rate`` and ``resolved_by_score``.
    Returns the updated dict, or None when the record file is absent.
    """
    record_path = Path(record_path)
    if not record_path.is_file():
        return None
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    resolved = completion_rate >= 100.0
    data["completion_rate"] = round(completion_rate, 3)
    data["blocks_found"] = blocks_found
    data["blocks_expected"] = blocks_expected
    if arm == "A":
        # Keep what the old fields really were (the process exit code) before
        # overwriting them. Old records: resolved true == exit code 0.
        if "process_exit_code" not in data:
            data["process_exit_code"] = 0 if data.get("resolved") is True else None
        data["score"] = round(completion_rate / 100.0, 5)
        data["resolved"] = resolved
        if halt_reason and not data.get("halt_reason"):
            data["halt_reason"] = halt_reason
        data["termination"] = arm_a_termination(
            completion_rate, data.get("halt_reason") or halt_reason, timed_out
        )
        data["score_basis"] = "completion_rate"
    else:
        data["resolved_by_score"] = resolved
        data.setdefault("score_basis", "harness_gates")
    if extras:
        data.update(extras)
    record_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


class IncompleteMatrixError(RuntimeError):
    """Raised when a summary is asked for over a matrix with uncovered cells."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(
            f"refusing to summarize an incomplete matrix: {len(missing)} cell(s) have no record "
            f"({', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}). "
            "Pass allow_partial=True (--allow-partial) to summarize anyway; the summary is then "
            "flagged partial and lists the missing cells."
        )


def _arm_block(recs: list[dict[str, Any]]) -> dict[str, Any]:
    rates = [float(r.get("completion_rate") or 0.0) for r in recs]
    n = len(rates)
    srt = sorted(rates)
    median = (srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2) if n else 0.0
    whole = sum(1 for x in rates if x >= 100.0)
    return {
        "runs": n,
        "mean_completion_pct": round(sum(rates) / n, 3) if n else 0.0,
        "median_completion_pct": round(median, 3),
        "whole_task_runs": whole,
        "whole_task_rate_pct": round(100.0 * whole / n, 3) if n else 0.0,
    }


def summarize_matrix(
    records: list[dict[str, Any]],
    cells: Iterable[tuple[str, str, int]],
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    """Summary over a fixed matrix, never over "whatever has a record".

    Refuses (``IncompleteMatrixError``) when a cell has no record unless
    ``allow_partial``; the old summary silently covered 57 arm-A and 56 arm-C
    runs and read 46.4 % / 99.0 % against the true 44.2 % / 93.9 %. The
    headline is reported over all cells (``headline_all``, nothing dropped), and
    again with quarantined runs excluded (``headline_excluding_quarantined``),
    with every quarantined run listed. ``first_attempt`` uses attempt 1 of each
    cell; ``final`` the latest attempt.
    """
    cells = list(cells)
    assign_attempts(records)
    final = latest_per_cell(records)
    first = first_attempt_per_cell(records)
    missing = [cell_stem(*c) for c in cells if c not in final]
    if missing and not allow_partial:
        raise IncompleteMatrixError(missing)

    arms = sorted({c[1] for c in cells})
    covered_cells = [c for c in cells if c in final]

    headline_all: dict[str, Any] = {}
    headline_valid: dict[str, Any] = {}
    first_block: dict[str, Any] = {}
    quarantined: list[dict[str, Any]] = []
    for arm in arms:
        recs = [final[c] for c in covered_cells if c[1] == arm]
        headline_all[arm] = _arm_block(recs)
        valid_recs = []
        for r in recs:
            ok, reason = record_validity(r)
            if ok:
                valid_recs.append(r)
            else:
                quarantined.append({
                    "cell": cell_stem(*rec_cell(r)),
                    "arm": arm,
                    "reason": reason,
                    "completion_rate": r.get("completion_rate"),
                    "halt_reason": (r.get("halt_reason") or "")[:160],
                })
        block = _arm_block(valid_recs)
        block["excluded_runs"] = len(recs) - len(valid_recs)
        headline_valid[arm] = block
        first_recs = [first.get(c) or final[c] for c in covered_cells if c[1] == arm]
        first_block[arm] = _arm_block(first_recs)

    attempts = attempt_counts(records)
    rerun = sorted(cell_stem(*k) for k, v in attempts.items() if v > 1 and k in set(cells))

    tokens: dict[str, int] = {}
    wall: dict[str, float] = {}
    for arm in arms:
        recs = [final[c] for c in covered_cells if c[1] == arm]
        tokens[arm] = sum(
            int(v or 0) for r in recs for v in (r.get("tokens_by_role") or {}).values()
        )
        wall[arm] = round(
            sum(float(r.get("harness_wall_clock_s") or r.get("wall_clock_s") or 0.0) for r in recs), 1
        )

    return {
        "matrix": {
            "cells_expected": len(cells),
            "cells_covered": len(covered_cells),
            "partial": bool(missing),
            "missing_cells": missing,
            "tasks": sorted({c[0] for c in cells}),
            "arms": arms,
            "seeds": sorted({c[2] for c in cells}),
        },
        "headline_all": headline_all,
        "headline_excluding_quarantined": headline_valid,
        "quarantined": sorted(quarantined, key=lambda q: q["cell"]),
        "first_attempt": first_block,
        "final": headline_all,
        "cells_rerun": {"count": len(rerun), "cells": rerun},
        "total_tokens": tokens,
        "total_wall_clock_s": wall,
    }
