#!/usr/bin/env python3
"""Recover what can be recovered from the 2026-09 LongGenBench sweep and recompute every metric.

No model calls, no network. Reads the run directories under ``~/.kusudaemon/runs``
and OpenCode's session store (read-only: the database is opened ``mode=ro``,
never written or vacuumed), rescores every raw artifact with the fixed scorer
(``longgen_common.to_output_blocks``) and rebuilds ``bench/*.json``,
``records.jsonl``, ``predictions/`` and ``summary.json``.

    python3 scripts/longgen_recompute.py            # dry run: plan + numbers only
    python3 scripts/longgen_recompute.py --apply    # write, with a backup first

See docs/LONGGENBENCH-RESULTS-2026-09.md ("Recovery and recompute 2026-09-30")
for what was fixed and what stays unrecoverable. Strict rule: only content a
run actually produced and left as its deliverable replaces an artifact. Content
that was written somewhere the harness could not read (a /tmp file) goes into a
separate ``recovered_*`` field and never into the headline number.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import longgen_integrity as li  # noqa: E402
import run_longgen_bench as rlb  # noqa: E402
from longgen_common import load_dataset, task_id_for  # noqa: E402

DATE = "2026-09-30"
IGNORED_ARCHIVE_DIRS = ("000-week",)
DEFAULT_DATASET = Path.home() / "LongGenBench" / "Dataset" / "Dataset_short.json"

# Evidence that a document existed: tool output the agent itself printed.
_EVIDENCE_RX = re.compile(
    r"(?:total (?:blocks|floors|weeks|entries)[^\n]{0,25}?|generated )(\d{2,3})\b", re.IGNORECASE
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def git_commit_order(repo: Path = _REPO_ROOT) -> dict[str, int]:
    try:
        out = subprocess.check_output(
            ["git", "rev-list", "--reverse", "HEAD"], cwd=str(repo), text=True, stderr=subprocess.DEVNULL
        ).split()
    except Exception:
        return {}
    return {sha: i for i, sha in enumerate(out)}


def commit_rank(commit: str | None, order: dict[str, int]) -> float:
    if not commit:
        return -1.0  # recorded before commits were recorded: the earliest runs
    for sha, i in order.items():
        if sha.startswith(commit[:7]):
            return float(i)
    return 1e9


def load_item_for(task_id: str, dataset: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    idx = int(task_id.split("-", 1)[0])
    return idx, dataset[idx]


# --------------------------------------------------------------------------
# arm C: re-harvest from the run directory
# --------------------------------------------------------------------------


def recover_arm_c(
    stem: str,
    item: dict[str, Any],
    results_dir: Path,
    runs_root: Path,
) -> dict[str, Any] | None:
    """What the harness would export from this cell's run dir, if it beats raw/.

    Same resolution as ``harvest_artifact`` (assembly, then per-node resolved
    text through ``node_artifact_text``, then ``out/*.md``). Returns a plan
    entry when the run dir yields strictly more units than the raw artifact, or
    when the raw artifact is missing.
    """
    parsed = li.parse_stem(stem)
    assert parsed
    task_id, arm, seed = parsed
    run_id = f"longgen_{stem}"
    rdir = runs_root / run_id
    if not rdir.is_dir():
        return None
    raw_path = results_dir / "raw" / f"{stem}.md"
    current = raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.is_file() else ""
    cur_cr, _ = rlb.score_text(current, item)
    scratch = results_dir / ".recompute_tmp" / f"{stem}.md"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    text = rlb.harvest_artifact("C", scratch, {}, runs_root=runs_root, task_id=task_id, seed=seed, run_id=run_id)
    shutil.rmtree(scratch.parent, ignore_errors=True)
    if not text.strip():
        return None
    new_cr, _ = rlb.score_text(text, item)
    if raw_path.is_file() and new_cr <= cur_cr + 1e-9:
        return None
    return {
        "stem": stem,
        "text": text,
        "completion_rate": new_cr,
        "previous_completion_rate": cur_cr if raw_path.is_file() else None,
        "previous_chars": len(current),
        "run_dir": str(rdir),
    }


# --------------------------------------------------------------------------
# arm A: sessions, tokens, provider errors, lost documents
# --------------------------------------------------------------------------


def arm_a_sessions(
    bench: dict[str, Any],
    stem: str,
    ws_root: Path,
    log_path: Path | None,
    db_path: Path | None = None,
    ref_mtime: float | None = None,
) -> tuple[list[str], str]:
    sids = list(bench.get("bare_session_ids") or [])
    if sids:
        return sids, bench.get("bare_session_ids_source", "bench_record")
    from kusudaemon.adapters.opencode_session import session_ids_from_log

    try:
        sids = session_ids_from_log(ws_root / stem, log_path=log_path)
    except Exception:
        sids = []
    # The log maps a workspace to every opencode run that ever started there
    # (000-week armA also holds the 09-10 run that was archived). Keep only the
    # sessions that finished when this cell's bench record was written.
    if sids and db_path is not None and ref_mtime is not None:
        kept = []
        for sid in sids:
            _, t1 = session_window([sid], db_path)
            if t1 is not None and abs(t1 / 1000.0 - ref_mtime) <= 900:
                kept.append(sid)
        sids = kept
    return sids, ("log_backfill_" + DATE if sids else "none")


def session_window(sids: list[str], db_path: Path) -> tuple[int | None, int | None]:
    import sqlite3

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        t0 = t1 = None
        for sid in sids:
            r = con.execute(
                "select min(time_created), max(time_updated) from message where session_id = ?", (sid,)
            ).fetchone()
            if r and r[0]:
                t0 = r[0] if t0 is None else min(t0, r[0])
                t1 = r[1] if t1 is None else max(t1, r[1])
        return t0, t1
    finally:
        con.close()


_PATH_RX = re.compile(r"(/[\w./\-]+\.(?:txt|md|json))")


def find_lost_document(
    sids: list[str],
    item: dict[str, Any],
    current_cr: float,
    db_path: Path,
) -> dict[str, Any]:
    """Look for a document the run wrote somewhere the harness could not read.

    Candidates are files that still exist, whose path appears in a tool call
    of this cell's sessions, and whose mtime falls inside the session window
    (so an ``/tmp/city_descriptions.txt`` that a *later* cell overwrote is not
    mistaken for this cell's output). The latest-modified qualifying file is
    taken (the agent's last state), never the best-scoring one and never a
    union of writes. Also reports ``evidence``: tool output where the agent
    itself printed a count at or above the expected number, for documents whose
    content is gone.
    """
    import os
    import sqlite3

    result: dict[str, Any] = {}
    if not sids:
        return result
    t0, t1 = session_window(sids, db_path)
    paths: set[str] = set()
    evidence: list[str] = []
    expected = int(item.get("number") or 0)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for sid in sids:
            rows = con.execute(
                "select p.data from part p join message m on m.id = p.message_id "
                "where p.session_id = ? and json_extract(m.data,'$.role') = 'assistant'",
                (sid,),
            ).fetchall()
            for (data,) in rows:
                try:
                    part = json.loads(data)
                except ValueError:
                    continue
                if part.get("type") != "tool":
                    continue
                state = part.get("state") or {}
                for v in (state.get("input") or {}).values():
                    if isinstance(v, str):
                        paths.update(_PATH_RX.findall(v))
                out = state.get("output") or ""
                if isinstance(out, str):
                    m = _EVIDENCE_RX.search(out[:4000])
                    if m and int(m.group(1)) >= expected > 0:
                        evidence.append(out[:160].replace("\n", " "))
    finally:
        con.close()
    if evidence:
        result["lost_document_evidence"] = evidence[-1]
    best: tuple[float, str] | None = None
    for p in sorted(paths):
        fp = Path(p)
        if not fp.is_file() or t0 is None or t1 is None:
            continue
        mtime_ms = fp.stat().st_mtime * 1000
        if not (t0 - 120_000 <= mtime_ms <= t1 + 600_000):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cr, _ = rlb.score_text(text, item)
        if cr > current_cr and (best is None or fp.stat().st_mtime > Path(best[1]).stat().st_mtime):
            best = (cr, p)
    if best:
        result["recovered_path"] = best[1]
        result["recovered_completion_rate"] = round(best[0], 3)
        result["recovered_text"] = Path(best[1]).read_text(encoding="utf-8", errors="replace")
    return result


# --------------------------------------------------------------------------
# attempts from the archive (attempts whose rows were never in records.jsonl)
# --------------------------------------------------------------------------


def archive_attempts(
    stem: str, archive_dir: Path
) -> list[dict[str, Any]]:
    """Earlier attempts of a cell found in ``archive/*``: bench record + raw."""
    found = []
    for bench in sorted(archive_dir.glob(f"*/bench/{stem}.json")) + sorted(archive_dir.glob(f"*/{stem}.json")):
        # archive/000-week/ is the 09-08 pilot, parked before the wave-1
        # repairs and outside the sweep (its records carry no real wall clock
        # and the later commit was stamped over it): not an attempt.
        if bench.relative_to(archive_dir).parts[0] in IGNORED_ARCHIVE_DIRS:
            continue
        try:
            d = json.loads(bench.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = None
        for cand in (
            bench.parent / "raw" / f"{stem}.md",
            bench.parent.parent / "raw" / f"{stem}.md",
            bench.parent / f"{stem}.md",
            bench.parent / f"{stem}.txt",
        ):
            if cand.is_file():
                raw = cand
                break
        found.append({
            "commit": d.get("commit"),
            "wall": float(d.get("wall_clock_s") or 0.0),
            "halt_reason": d.get("halt_reason"),
            "raw": raw,
            "bench": bench,
            "mtime": bench.stat().st_mtime,
        })
    return found


def build_attempts(
    cell: tuple[str, str, int],
    rows: list[dict[str, Any]],
    archive_dir: Path,
    item: dict[str, Any],
    order: dict[str, int],
    final_mtime: float | None = None,
) -> list[dict[str, Any]]:
    """Every attempt of one cell, oldest first: records.jsonl attempts merged
    with archived attempts that never had a row. Each entry carries the CR as
    recorded (legacy scorer) and, when the attempt's artifact survives in the
    archive, the CR under the fixed scorer."""
    by_attempt: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_attempt.setdefault(int(r.get("attempt") or 1), []).append(r)
    attempts: list[dict[str, Any]] = []
    for n, rs in sorted(by_attempt.items()):
        walls = [float(r.get("wall_clock_s") or 0.0) for r in rs if (r.get("wall_clock_s") or 0) > 0]
        last = rs[-1]
        attempts.append({
            "source": "records",
            "commit": next((r.get("commit") for r in rs if r.get("commit")), None),
            "wall": walls[-1] if walls else 0.0,
            "recorded_cr": float(last.get("completion_rate") or 0.0),
            "fixed_cr": None,
            "halt_reason": last.get("halt_reason"),
            "mtime": final_mtime if n == max(by_attempt) else None,
        })
    stem = li.cell_stem(*cell)
    for a in archive_attempts(stem, archive_dir):
        match = None
        for att in attempts:
            same_wall = att["wall"] > 0 and abs(att["wall"] - a["wall"]) < 3.0
            if same_wall and (not a["commit"] or not att["commit"] or a["commit"][:7] == att["commit"][:7]):
                match = att
                break
        fixed = None
        if a["raw"] is not None:
            text = a["raw"].read_text(encoding="utf-8", errors="replace")
            fixed = rlb.score_text(text, item)[0]
        if match is not None:
            match["mtime"] = a["mtime"]
            if fixed is not None:
                match["fixed_cr"] = fixed
                match["fixed_from"] = str(a["raw"].relative_to(archive_dir.parent))
            continue
        if a["raw"] is None:
            continue  # an attempt we know existed but whose artifact is gone
        attempts.append({
            "source": "archive_only",
            "commit": a["commit"],
            "wall": a["wall"],
            "recorded_cr": None,
            "fixed_cr": fixed,
            "fixed_from": str(a["raw"].relative_to(archive_dir.parent)),
            "halt_reason": a["halt_reason"],
            "mtime": a["mtime"],
        })
    # Oldest first: commit order, then the time the attempt ended (the archived
    # bench file keeps the mtime it had when it was moved aside; the final
    # attempt's is the live bench file's), then file order.
    indexed = list(enumerate(attempts))
    indexed.sort(key=lambda ia: (commit_rank(ia[1]["commit"], order), ia[1]["mtime"] if ia[1].get("mtime") else -1.0, ia[0]))
    return [a for _, a in indexed]


# --------------------------------------------------------------------------
# statistics (same definitions as the results doc, section 2)
# --------------------------------------------------------------------------


def _binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    k = min(k, n - k)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * p)


def task_means(final: dict[tuple[str, str, int], dict[str, Any]], arm: str, key: str = "completion_rate") -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for (t, a, s), r in sorted(final.items()):
        if a == arm:
            out.setdefault(t, []).append(float(r.get(key) or 0.0))
    return out


def bootstrap(per_task_a: dict[str, list[float]], per_task_c: dict[str, list[float]], whole: bool, draws: int = 10000) -> dict[str, Any]:
    rng = random.Random(20260930)
    tasks = sorted(set(per_task_a) & set(per_task_c))

    def stat(vals: list[float]) -> float:
        if whole:
            return 100.0 * sum(1 for v in vals if v >= 100.0) / len(vals)
        return sum(vals) / len(vals)

    def draw() -> tuple[float, float]:
        pick = [tasks[rng.randrange(len(tasks))] for _ in tasks]
        va = [v for t in pick for v in per_task_a[t]]
        vc = [v for t in pick for v in per_task_c[t]]
        return stat(va), stat(vc)

    sa, sc, sd = [], [], []
    for _ in range(draws):
        a, c = draw()
        sa.append(a)
        sc.append(c)
        sd.append(c - a)

    def ci(xs: list[float]) -> tuple[float, float]:
        xs = sorted(xs)
        return round(xs[int(0.025 * len(xs))], 1), round(xs[int(0.975 * len(xs)) - 1], 1)

    pa = stat([v for t in tasks for v in per_task_a[t]])
    pc = stat([v for t in tasks for v in per_task_c[t]])
    return {
        "A": {"point": round(pa, 1), "ci95": ci(sa)},
        "C": {"point": round(pc, 1), "ci95": ci(sc)},
        "diff": {"point": round(pc - pa, 1), "ci95": ci(sd)},
    }


def paired_stats(final: dict[tuple[str, str, int], dict[str, Any]], tasks: list[str], seeds: list[int]) -> dict[str, Any]:
    wins = ties = losses = 0
    both = only_c = only_a = neither = ties_both_full = 0
    for t in tasks:
        for s in seeds:
            a = float(final[(t, "A", s)]["completion_rate"])
            c = float(final[(t, "C", s)]["completion_rate"])
            if c > a:
                wins += 1
            elif c < a:
                losses += 1
            else:
                ties += 1
                if a >= 100.0:
                    ties_both_full += 1
            fa, fc = a >= 100.0, c >= 100.0
            both += fa and fc
            only_c += fc and not fa
            only_a += fa and not fc
            neither += (not fa) and (not fc)
    tw = tt = tl = 0
    for t in tasks:
        ma = sum(float(final[(t, "A", s)]["completion_rate"]) for s in seeds) / len(seeds)
        mc = sum(float(final[(t, "C", s)]["completion_rate"]) for s in seeds) / len(seeds)
        if mc > ma + 1e-9:
            tw += 1
        elif mc < ma - 1e-9:
            tl += 1
        else:
            tt += 1
    return {
        "pairs": {"c_higher": wins, "tied": ties, "tied_both_100": ties_both_full, "a_higher": losses,
                  "both_finished": both, "only_c_finished": only_c, "only_a_finished": only_a, "neither_finished": neither,
                  "sign_test_p": _binom_two_sided(losses, wins + losses)},
        "tasks": {"c_higher": tw, "tied": tt, "a_higher": tl, "sign_test_p": _binom_two_sided(tl, tw + tl)},
    }


# --------------------------------------------------------------------------
# tables for the doc
# --------------------------------------------------------------------------


def era_of(commit: str | None) -> int:
    # Final-attempt commit -> era (results doc 3.7). None = recorded before commits were.
    table = [
        (1, ("dd0b7dc", "7d2f08c")),
        (2, ("2d8e87e",)),
        (3, ("f8f5b31",)),
        (4, ("dcf4874", "0e691f2", "c407d1b", "None")),
        (5, ("35480d4", "779ad23", "ff6870f")),
        (6, ("2b6a2ab",)),
    ]
    key = (commit or "None")[:7]
    for era, shas in table:
        if key in shas:
            return era
    return 0


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def block(recs: list[dict[str, Any]]) -> dict[str, Any]:
    rates = [float(r["completion_rate"]) for r in recs]
    n = len(rates)
    srt = sorted(rates)
    med = (srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2) if n else 0.0
    mean = sum(rates) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in rates) / n) if n else 0.0  # population std, as in the results doc
    whole = sum(1 for x in rates if x >= 100.0)
    return {
        "runs": n, "mean": round(mean, 1), "median": round(med, 1), "std": round(sd, 1),
        "whole": whole, "whole_pct": pct(whole, n),
        "ge90": sum(1 for x in rates if x >= 90.0), "lt10": sum(1 for x in rates if x < 10.0),
        "b100": whole,
        "b50_99": sum(1 for x in rates if 50.0 <= x < 100.0),
        "b10_49": sum(1 for x in rates if 10.0 <= x < 50.0),
        "b_lt10": sum(1 for x in rates if x < 10.0),
    }


def analysis(
    final: dict[tuple[str, str, int], dict[str, Any]],
    first: dict[tuple[str, str, int], float],
    tasks: list[str],
    seeds: list[int],
    types: dict[str, str],
    recovered: dict[str, float],
    lost_evidence: dict[str, float] | None = None,
    outside_matrix: dict[tuple[str, str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    arms = ("A", "C")
    cells = {a: [final[(t, a, s)] for t in tasks for s in seeds] for a in arms}
    out["headline"] = {a: block(cells[a]) for a in arms}
    pa, pc = task_means(final, "A"), task_means(final, "C")
    out["bootstrap_mean"] = bootstrap(pa, pc, whole=False)
    out["bootstrap_whole"] = bootstrap(pa, pc, whole=True)
    out["paired"] = paired_stats(final, tasks, seeds)
    out["tasks_all_seeds_finished"] = {
        a: sum(1 for t in tasks if all(final[(t, a, s)]["completion_rate"] >= 100.0 for s in seeds)) for a in arms
    }
    out["tasks_no_seed_finished"] = {
        a: sum(1 for t in tasks if not any(final[(t, a, s)]["completion_rate"] >= 100.0 for s in seeds)) for a in arms
    }
    # by type
    by_type: dict[str, Any] = {}
    for typ in sorted(set(types.values())):
        ts = [t for t in tasks if types[t] == typ]
        by_type[typ] = {a: block([final[(t, a, s)] for t in ts for s in seeds]) for a in arms}
        by_type[typ]["tasks"] = len(ts)
    out["by_type"] = by_type
    out["by_task"] = {
        t: {a: [round(float(final[(t, a, s)]["completion_rate"]), 1) for s in seeds] for a in arms} for t in tasks
    }
    # cost
    def wall(r: dict[str, Any]) -> float:
        return float(r.get("wall_clock_s") or r.get("harness_wall_clock_s") or 0.0)

    cost = {}
    for a in arms:
        w = [wall(r) for r in cells[a]]
        full = out["headline"][a]["whole"]
        tok = [sum(int(v or 0) for v in (r.get("tokens_by_role") or {}).values()) for r in cells[a]]
        srt = sorted(w)
        cost[a] = {
            "wall_mean_min": round(sum(w) / len(w) / 60, 1),
            "wall_median_min": round((srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2 / 60, 1),
            "wall_total_h": round(sum(w) / 3600, 1),
            "wall_per_full_doc_min": round(sum(w) / full / 60, 1) if full else None,
            "wall_max_min": round(max(w) / 60, 1),
            "tokens_total_M": round(sum(tok) / 1e6, 2),
            "tokens_mean_M": round(sum(tok) / len(tok) / 1e6, 2),
            "tokens_per_full_doc_M": round(sum(tok) / full / 1e6, 2) if full else None,
        }
    out["cost"] = cost
    # tiers (arm C)
    tiers: dict[str, list[dict[str, Any]]] = {}
    for r in cells["C"]:
        tiers.setdefault(r.get("tier_final") or "(not recorded)", []).append(r)
    out["tiers"] = {k: block(v) for k, v in sorted(tiers.items())}
    # clean finish (arm C): termination gates_satisfied is not recorded per row; use halt_reason empty
    out["arm_c_clean_finish"] = sum(1 for r in cells["C"] if not (r.get("halt_reason") or "").strip())
    # eras
    eras: dict[int, list[dict[str, Any]]] = {}
    for r in cells["C"]:
        eras.setdefault(era_of(r.get("commit")), []).append(r)
    out["eras"] = {e: block(v) for e, v in sorted(eras.items())}
    out["eras_late"] = {
        a: block([r for r in cells[a] if era_of(r.get("commit")) in (5, 6)]) if a == "C" else None for a in arms
    }
    # first attempt (arm A cells were never re-run: its first attempt is its final)
    fa = [float(first[(t, "C", s)]) for t in tasks for s in seeds]
    out["arm_c_first_attempt"] = {
        "mean": round(sum(fa) / len(fa), 1),
        "whole": sum(1 for x in fa if x >= 100.0),
        "whole_pct": pct(sum(1 for x in fa if x >= 100.0), len(fa)),
        "runs": len(fa),
    }
    out["first_attempt"] = {
        "A": block([{"completion_rate": first[(t, "A", s)]} for t in tasks for s in seeds]),
        "C": block([{"completion_rate": v} for v in fa]),
    }
    # sensitivity
    sens: dict[str, Any] = {}
    sens["arm_c_excluding_247_s3"] = block([r for r in cells["C"] if (r["task_id"], r["seed"]) != ("247-menu-week", 3)])
    best_a = []
    for r in cells["A"]:
        key = li.cell_stem(r["task_id"], "A", r["seed"])
        best_a.append({**r, "completion_rate": 100.0 if key in recovered else r["completion_rate"]})
    sens["arm_a_recovered_file_only"] = block(best_a)
    sens["arm_a_recovered_file_cells"] = {k: v for k, v in sorted(recovered.items())}
    # best case: also count the cells where the agent printed a full count for a
    # document that is gone, at 100 %. Evidence only, not content.
    best_all = []
    for r in cells["A"]:
        key = li.cell_stem(r["task_id"], "A", r["seed"])
        hit = key in recovered or key in (lost_evidence or {})
        best_all.append({**r, "completion_rate": 100.0 if hit else r["completion_rate"]})
    sens["arm_a_best_case"] = block(best_all)
    sens["arm_a_best_case_cells"] = sorted(set(recovered) | set(lost_evidence or {}))
    out["sensitivity"] = sens
    # late eras by commit (A rows carry the HEAD commit of the sweep at the time)
    late = lambda r: (r.get("commit") or "")[:7] in ("35480d4", "2b6a2ab")
    out["late_era"] = {a: block([r for r in cells[a] if late(r)]) for a in arms}
    # every run that exists, including the three tasks outside the final matrix
    if outside_matrix is not None:
        allr = {**final, **outside_matrix}
        out["all_runs_that_exist"] = {
            a: block([r for k, r in allr.items() if k[1] == a]) for a in arms
        }
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results-dir", default=str(_REPO_ROOT / "bench_results" / "longgen"))
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--runs-root", default=str(Path.home() / ".kusudaemon" / "runs"))
    ap.add_argument("--workspaces-root", default=str(Path.home() / ".kusudaemon" / "bench_workspaces" / "longgen"))
    ap.add_argument("--db", default=None, help="opencode.db (opened read-only)")
    ap.add_argument("--log", default=None, help="opencode.log")
    ap.add_argument("--exclude-tasks", nargs="*", default=list(li.DEFAULT_EXCLUDED_TASKS))
    ap.add_argument("--apply", action="store_true", help="Write files (a backup is taken first).")
    ap.add_argument("--no-opencode", action="store_true", help="Skip the OpenCode store (tokens, errors, lost documents).")
    ap.add_argument("--stats-out", default=None, help="Where to write the analysis JSON.")
    args = ap.parse_args()

    results_dir = Path(args.results_dir)
    raw_dir, bench_dir = results_dir / "raw", results_dir / "bench"
    archive_root = results_dir / "archive"
    runs_root = Path(args.runs_root)
    ws_root = Path(args.workspaces_root)
    dataset = load_dataset(args.dataset)
    order = git_commit_order()

    from kusudaemon.adapters.opencode_session import default_db_path, default_log_path

    db_path = Path(args.db) if args.db else default_db_path()
    log_path = Path(args.log) if args.log else default_log_path()
    use_oc = (not args.no_opencode) and db_path.is_file()

    task_ids = sorted(
        {p.name.split("_arm")[0] for p in raw_dir.iterdir() if "_arm" in p.name}
        - set(args.exclude_tasks)
    )
    task_ids = [t for t in task_ids if t.split("-", 1)[0].isdigit()]
    seeds = list(li.DEFAULT_SEEDS)
    cells = li.expected_cells(task_ids, li.DEFAULT_ARMS, seeds)
    print(f"matrix: {len(task_ids)} tasks x {len(li.DEFAULT_ARMS)} arms x {len(seeds)} seeds = {len(cells)} cells")
    types = {t: str(load_item_for(t, dataset)[1]["type"]) for t in task_ids}

    backup_dir = archive_root / f"recompute_{DATE}"
    pre_dir = backup_dir / "pre"
    if args.apply:
        pre_dir.mkdir(parents=True, exist_ok=True)
        for name in ("records.jsonl", "summary.json"):
            src = results_dir / name
            if src.is_file() and not (pre_dir / name).exists():
                shutil.copy2(src, pre_dir / name)
        for name in ("bench", "predictions"):
            src = results_dir / name
            if src.is_dir() and not (pre_dir / name).exists():
                shutil.copytree(src, pre_dir / name)
        print(f"backup: {pre_dir}")

    # ---- load history -------------------------------------------------
    # Idempotent: rows written by a previous run of this script are replaced,
    # never stacked (they carry rescored.scorer ending in "(2026-09-30)").
    old_rows = [
        r for r in read_jsonl(results_dir / "records.jsonl")
        if not str((r.get("rescored") or {}).get("scorer", "")).endswith(f"({DATE})")
    ]
    li.assign_attempts(old_rows)
    rows_by_cell: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for r in old_rows:
        rows_by_cell.setdefault(li.rec_cell(r), []).append(r)

    notes: dict[str, list[str]] = {"arm_c_recovered": [], "arm_a_sessions_backfilled": [], "recovered_sensitivity": [], "lost_evidence": [], "synthesized_rows": []}
    new_rows: list[dict[str, Any]] = []
    final_rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    recovered_for_sens: dict[str, float] = {}
    replaced_raw: list[tuple[Path, str]] = []
    pending_raw_writes: list[tuple[Path, str]] = []
    pending_bench_updates: list[tuple[Path, dict[str, Any]]] = []

    for cell in cells:
        task_id, arm, seed = cell
        stem = li.cell_stem(*cell)
        idx, item = load_item_for(task_id, dataset)
        raw_path = raw_dir / f"{stem}.{'txt' if arm == 'A' else 'md'}"
        bench_path = bench_dir / f"{stem}.json"
        bench = json.loads(bench_path.read_text(encoding="utf-8")) if bench_path.is_file() else {}
        prev_rows = rows_by_cell.get(cell, [])
        prev_final = prev_rows[-1] if prev_rows else None

        extras: dict[str, Any] = {}
        # --- arm C: re-harvest the run dir (parts layout, orphan writes) ---
        if arm == "C":
            plan = recover_arm_c(stem, item, results_dir, runs_root)
            if plan:
                notes["arm_c_recovered"].append(
                    f"{stem}: raw {plan['previous_completion_rate']} -> {plan['completion_rate']:.1f} "
                    f"({plan['previous_chars']} -> {len(plan['text'])} chars) from {plan['run_dir']}"
                )
                if raw_path.is_file():
                    replaced_raw.append((raw_path, "raw artifact replaced by run-dir harvest"))
                pending_raw_writes.append((raw_path, plan["text"]))
                extras["artifact_recovered_from"] = plan["run_dir"]
                extras["artifact_recovered_note"] = (
                    "re-harvested from the run directory on " + DATE + " (what harvest_artifact "
                    "returns for it); no artifact had been exported"
                    if plan["previous_completion_rate"] is None
                    else "re-harvested from the run directory on " + DATE
                )
        text = (
            dict(pending_raw_writes).get(raw_path)
            if raw_path in dict(pending_raw_writes)
            else (raw_path.read_text(encoding="utf-8", errors="replace") if raw_path.is_file() else "")
        )
        completion, parsed = rlb.score_text(text or "", item)

        # --- arm A: sessions, tokens, provider errors, lost documents ------
        tokens_by_role = (prev_final or {}).get("tokens_by_role") or bench.get("tokens_by_role") or {}
        bench_updates: dict[str, Any] = {}
        halt_reason = (prev_final or {}).get("halt_reason") or bench.get("halt_reason")
        if arm == "A" and use_oc:
            ref_file = pre_dir / "bench" / f"{stem}.json"
            if not ref_file.is_file():
                ref_file = bench_path
            ref_mtime = ref_file.stat().st_mtime if ref_file.is_file() else None
            sids, src = arm_a_sessions(bench, stem, ws_root, log_path, db_path, ref_mtime)
            if sids and not bench.get("bare_session_ids"):
                bench_updates["bare_session_ids"] = sids
                bench_updates["bare_session_ids_source"] = src
                notes["arm_a_sessions_backfilled"].append(f"{stem}: {len(sids)} session(s) from the log")
            if sids:
                tk = li.opencode_session_tokens(sids, db_path)
                if tk["total"] > 0:
                    tokens_by_role = {"bare": tk["total"]}
                bench_updates["tokens_detail"] = tk
                bench_updates["tokens_by_role"] = tokens_by_role
                try:
                    perr = li.opencode_provider_errors(sids, log_path)
                except FileNotFoundError:
                    perr = None
                if perr is not None:
                    # How long before the agent's last message was the last error?
                    # An error minutes before a self-chosen stop is weak evidence
                    # that it caused the stop; reported, not used to decide.
                    if perr.get("last_error_ts"):
                        import datetime as _dt
                        _, t_end = session_window(sids, db_path)
                        if t_end:
                            last = _dt.datetime.fromisoformat(str(perr["last_error_ts"]).replace("Z", "+00:00"))
                            perr["last_error_to_session_end_s"] = round(t_end / 1000.0 - last.timestamp())
                    bench_updates["provider_errors"] = perr
                    extras["provider_errors"] = perr
                    if completion < 100.0 and perr["total"] and not halt_reason:
                        halt_reason = li.provider_error_halt_reason(perr)
                if completion < 100.0:
                    lost = find_lost_document(sids, item, completion, db_path)
                    if lost.get("recovered_completion_rate") is not None:
                        rec_text = lost.pop("recovered_text")
                        dest = results_dir / "recovered" / f"{stem}.txt"
                        notes["recovered_sensitivity"].append(
                            f"{stem}: {lost['recovered_path']} -> {lost['recovered_completion_rate']} % (as left)"
                        )
                        recovered_for_sens[stem] = float(lost["recovered_completion_rate"])
                        extras.update(
                            recovered_completion_rate=lost["recovered_completion_rate"],
                            recovered_from=lost["recovered_path"],
                            recovered_artifact=str(dest.relative_to(results_dir)),
                            recovered_note=(
                                "file the run wrote outside its workspace and that is still on disk; "
                                "NOT the exported artifact and NOT in the headline"
                            ),
                        )
                        if args.apply:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_text(rec_text, encoding="utf-8")
                    if lost.get("lost_document_evidence"):
                        notes["lost_evidence"].append(f"{stem}: {lost['lost_document_evidence']}")
                        extras["lost_document_evidence"] = lost["lost_document_evidence"]
                        if completion < 100.0 and stem not in recovered_for_sens:
                            extras["lost_document_note"] = (
                                "the agent printed a full count but the document is gone "
                                "(deleted or overwritten); unrecoverable, scored as exported"
                            )

        # --- final row for this cell --------------------------------------
        base = dict(prev_final) if prev_final else {
            "benchmark": "longgenbench",
            "task_id": task_id,
            "dataset_index": idx,
            "type": item.get("type"),
            "number": item.get("number"),
            "arm": arm,
            "seed": seed,
            "backend": bench.get("backend", "opencode"),
            "model": bench.get("model"),
            "tier_measured": bench.get("tier_measured"),
            "tier_final": bench.get("tier_final"),
            "calls_by_role": bench.get("calls_by_role", {}),
            "wall_clock_s": bench.get("wall_clock_s", 0.0),
            "harness_wall_clock_s": 0.0,
            "returncode": None,
            "timed_out": False,
            "commit": bench.get("commit"),
            "attempt": 1,
        }
        if not prev_final:
            notes["synthesized_rows"].append(f"{stem}: no record in records.jsonl; row built from bench/ + raw/")
            base["synthesized_from"] = "bench+raw"
        tier_f, tier_m = bench.get("tier_final"), bench.get("tier_measured")
        if tier_f and not base.get("tier_final"):
            base["tier_final"], base["tier_measured"] = tier_f, tier_m
        attempt = int(base.get("attempt") or 1)
        row = {
            **base,
            "attempt": attempt,
            "tokens_by_role": tokens_by_role,
            "halt_reason": halt_reason,
            "artifact_path": str(raw_path) if raw_path.is_file() or raw_path in dict(pending_raw_writes) else None,
            "artifact_chars": len(text),
            "blocks_found": len(parsed),
            "blocks_expected": int(item.get("number", 0)),
            "completion_rate": round(completion, 3),
            "halt_after_complete": bool(halt_reason) and completion >= 100.0,
            "rescored": {"date": DATE, "scorer": "longgen_common.to_output_blocks (2026-09-30)",
                         "previous_completion_rate": (prev_final or {}).get("completion_rate")},
            **{k: v for k, v in extras.items() if k != "provider_errors"},
            **({"provider_errors": extras["provider_errors"]} if extras.get("provider_errors") else {}),
        }
        if not text.strip() and not raw_path.is_file():
            row["artifact_absent"] = True
        valid, reason = li.record_validity(row)
        row["valid"], row["invalid_reason"] = valid, reason
        for k, v in li.UNKNOWN_PROVENANCE.items():
            row.setdefault(k, v)
        new_rows.append(row)
        final_rows[cell] = row

        # --- bench/<stem>.json write-back ---------------------------------
        extra_bench = dict(bench_updates)
        if raw_path in dict(pending_raw_writes):
            extra_bench["artifact_path"] = str(raw_path)
        for k in ("recovered_completion_rate", "recovered_from", "recovered_artifact", "recovered_note",
                  "lost_document_evidence", "lost_document_note", "artifact_recovered_from", "artifact_recovered_note"):
            if k in extras:
                extra_bench[k] = extras[k]
        pending_bench_updates.append((bench_path, {
            "arm": arm, "completion_rate": completion, "blocks_found": len(parsed),
            "blocks_expected": int(item.get("number", 0)), "halt_reason": halt_reason,
            "timed_out": bool(row.get("timed_out")), "extras": extra_bench,
            "stem": stem, "bench_missing": not bench_path.is_file(), "bench_seed": bench,
        }))

    # ---- first-attempt table ---------------------------------------------
    all_old_by_cell = rows_by_cell
    first: dict[tuple[str, str, int], float] = {}
    attempt_log: dict[str, Any] = {}
    for cell in cells:
        stem = li.cell_stem(*cell)
        idx, item = load_item_for(cell[0], dataset)
        if cell[1] == "C":
            ref_bench = pre_dir / "bench" / f"{stem}.json"
            if not ref_bench.is_file():
                ref_bench = bench_dir / f"{stem}.json"
            atts = build_attempts(
                cell, all_old_by_cell.get(cell, []), archive_root, item, order,
                final_mtime=ref_bench.stat().st_mtime if ref_bench.is_file() else None,
            )
            if not atts:
                first[cell] = float(final_rows[cell]["completion_rate"])
                continue
            a0 = atts[0]
            val = a0["fixed_cr"] if a0.get("fixed_cr") is not None else a0["recorded_cr"]
            if len(atts) == 1 and a0["source"] == "records":
                val = float(final_rows[cell]["completion_rate"])
            first[cell] = float(val)
            if len(atts) > 1:
                attempt_log[stem] = [
                    {k: (round(v, 3) if isinstance(v, float) else v) for k, v in a.items() if k in
                     ("source", "commit", "wall", "recorded_cr", "fixed_cr", "fixed_from")}
                    for a in atts
                ]
        else:
            first[cell] = float(final_rows[cell]["completion_rate"])

    lost_ev_cells = {
        li.cell_stem(*c): 100.0
        for c, r in final_rows.items()
        if r.get("lost_document_evidence") and r["completion_rate"] < 100.0
        and li.cell_stem(*c) not in recovered_for_sens
    }
    # Runs of the three tasks outside the final matrix: the latest record of each
    # cell, with the completion rate re-derived from raw/ where an artifact exists.
    outside: dict[tuple[str, str, int], dict[str, Any]] = {}
    for key, r in li.latest_per_cell(old_rows).items():
        if key[0] in args.exclude_tasks:
            outside[key] = {"completion_rate": float(r.get("completion_rate") or 0.0)}
    for path in sorted(raw_dir.iterdir()):
        parsed_stem = li.parse_stem(path.stem)
        if not parsed_stem or path.suffix not in (".txt", ".md") or parsed_stem[0] not in args.exclude_tasks:
            continue
        _, o_item = load_item_for(parsed_stem[0], dataset)
        o_cr, _ = rlb.score_text(path.read_text(encoding="utf-8", errors="replace"), o_item)
        outside[parsed_stem] = {"completion_rate": o_cr}
    stats = analysis(final_rows, first, task_ids, seeds, types, recovered_for_sens, lost_ev_cells, outside)

    print("\n== recovery ==")
    for key, lines in notes.items():
        print(f"{key}: {len(lines)}")
        for ln in lines:
            print("   ", ln)
    print("\n== headline (fixed scorer, final attempt, all cells, nothing quarantined) ==")
    for arm in ("A", "C"):
        h = stats["headline"][arm]
        print(f"  arm {arm}: mean {h['mean']}  whole {h['whole']}/{h['runs']} = {h['whole_pct']}%  median {h['median']}")
    print(f"  arm C first attempt: {stats['arm_c_first_attempt']}")
    print("\n== attempts of re-run arm C cells (oldest first; fixed_cr = rescored from the archived artifact) ==")
    for stem, atts in sorted(attempt_log.items()):
        print(f"  {stem}: " + " | ".join(
            f"{(a['commit'] or 'none')[:7]} {a['source'][:3]} rec={a['recorded_cr']} fix={a['fixed_cr']}" for a in atts))

    # persist what the dry run computed
    summary_path = Path(args.stats_out) if args.stats_out else results_dir / "analysis_2026-09-30.json"
    if not args.apply:
        if args.stats_out:
            summary_path.write_text(json.dumps({**stats, "attempt_log": attempt_log, "notes": notes}, indent=2, default=str), encoding="utf-8")
            print(f"\n(dry run: analysis written to {summary_path}; nothing else written. Pass --apply.)")
        else:
            print("\n(dry run: nothing written. Pass --apply.)")
        return 0

    # ---- apply -------------------------------------------------------------
    # 1. raw artifacts (move replaced ones to the archive first)
    for path, why in replaced_raw:
        dest = backup_dir / "replaced_raw" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(path, dest)
    for path, text in pending_raw_writes:
        path.write_text(text, encoding="utf-8")
        prov = path.with_suffix(".recovered.json")
        prov.write_text(json.dumps({
            "recovered_on": DATE,
            "method": "scripts/longgen_recompute.py::recover_arm_c (run-dir harvest, same as harvest_artifact)",
            "stem": path.stem,
            "chars": len(text),
        }, indent=2) + "\n", encoding="utf-8")
    # 2. bench write-back (synthesize a bench file for a cell that has none)
    for bench_path, spec in pending_bench_updates:
        if spec["bench_missing"] and not bench_path.is_file():
            continue
        li.write_back_scored_record(
            bench_path, arm=spec["arm"], completion_rate=spec["completion_rate"],
            blocks_found=spec["blocks_found"], blocks_expected=spec["blocks_expected"],
            halt_reason=spec["halt_reason"] if spec["arm"] == "A" else None,
            timed_out=spec["timed_out"], extras=spec["extras"] or None,
        )
    # 3. records.jsonl: full history (tagged) + the recompute rows
    history = []
    for r in old_rows:
        for k, v in li.UNKNOWN_PROVENANCE.items():
            r.setdefault(k, v)
        history.append(r)
    out_rows = history + new_rows
    with open(results_dir / "records.jsonl", "w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    # 4. predictions
    tasks = [(i, dataset[i]) for i in sorted({int(t.split("-", 1)[0]) for t in task_ids})]
    rlb.write_predictions(list(final_rows.values()), tasks, results_dir)
    # 5. reconcile + summary
    report = li.reconcile_cells(results_dir, cells, out_rows)
    print("\n" + li.format_reconcile(report))
    summary = li.summarize_matrix(out_rows, cells)
    summary["first_attempt"] = {
        arm: {k: stats["first_attempt"][arm][k] for k in ("runs", "mean", "median", "whole", "whole_pct")}
        for arm in ("A", "C")
    }
    summary["cells_rerun"] = {"count": len(attempt_log), "cells": sorted(attempt_log),
                              "note": "records.jsonl attempts plus archived attempts that never had a row"}
    summary["first_attempt_detail"] = {
        "note": "attempts = records.jsonl attempts plus archived attempts that never had a row; "
                "an archived artifact is rescored with the fixed scorer, others use the recorded rate",
        "attempts": attempt_log,
    }
    summary["reconcile"] = report
    summary["rescored"] = {"date": DATE, "note": "all cells rescored from raw/ with the fixed scorer; see docs/LONGGENBENCH-RESULTS-2026-09.md"}
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps({**stats, "attempt_log": attempt_log, "notes": notes}, indent=2, default=str), encoding="utf-8")
    print(f"wrote records.jsonl ({len(out_rows)} rows), summary.json, predictions/, {summary_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
