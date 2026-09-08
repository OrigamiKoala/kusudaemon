#!/usr/bin/env python3
"""Drive LongGenBench against kusudaemon across arms (BENCHMARKING.md §0.1, §0.2 Shape B).

LongGenBench (github.com/mozhu621/LongGenBench, ICLR 2025) hands you a prompt
demanding N structurally-delimited entries -- 52 weekly diary entries, 100
skyscraper floors -- and grades whether they are all there and whether the
scheduled constraints landed in the right ones. It is the cheapest external
signal kusudaemon has: the data ships in the repo, and the primary metric
(completion rate) is a regex, so a sweep costs only the generations themselves.

This script owns the experimental matrix. For each (task, arm, seed) it shells
out to `kusudaemon bench`, harvests the generated document, and writes it into
upstream's own prediction format so `scripts/eval_longgen_free.py` can score it.

    # 1. one-time: clone the dataset
    git clone https://github.com/mozhu621/LongGenBench.git ~/LongGenBench

    # 2. see the matrix without spending anything
    python3 scripts/run_longgen_bench.py --longgen-dir ~/LongGenBench \\
        --limit 8 --dry-run

    # 3. real sweep
    python3 scripts/run_longgen_bench.py --longgen-dir ~/LongGenBench \\
        --limit 8 --arms A C --seeds 1 2 3

    # 4. score artifacts already on disk (finished or parked runs) without
    #    spending anything on generation
    python3 scripts/run_longgen_bench.py --tasks 300 --arms C --seeds 2 3 \\
        --score-only

Arms follow BENCHMARKING.md §0.1: A is the bare backend CLI, C is the full pipeline.
Arm B (decomposition only) is skipped per the user note in BENCHMARKING.md §0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import (
    check_identical_seeds,
    classify_halt,
    dedupe_records,
    parse_flags,
    read_records_jsonl,
)
from longgen_common import (  # noqa: E402
    build_prediction_record,
    calculate_completion_rate,
    load_dataset,
    parse_blocks,
    task_id_for,
    to_output_blocks,
)

LONGGEN_REPO = "https://github.com/mozhu621/LongGenBench.git"
DEFAULT_BACKEND = "opencode"
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

DATASETS = {
    "short": "Dataset/Dataset_short.json",
    "long": "Dataset/Dataset_long.json",
}


# --------------------------------------------------------------------------
# task selection
# --------------------------------------------------------------------------


def select_tasks(
    data: list[dict[str, Any]],
    *,
    limit: int | None,
    indices: list[int] | None,
    exclude_tasks: list[str] | None = None,
    types: list[str] | None,
    stratify: bool,
) -> list[tuple[int, dict[str, Any]]]:
    """Choose the subset to run, deterministically.

    Selection is by position, never by sampling: a benchmark subset that
    changes between arms or between reruns is not a benchmark. `--stratify`
    round-robins across the four scenario types so a small --limit does not
    silently become "100 diary tasks and nothing else".
    """
    pool = list(enumerate(data))
    if indices:
        by_index = {i: item for i, item in pool}
        missing = [i for i in indices if i not in by_index]
        if missing:
            raise SystemExit(f"--tasks out of range for this dataset: {missing}")
        pool = [(i, by_index[i]) for i in indices]

    if types:
        wanted = {t.lower() for t in types}
        pool = [(i, item) for i, item in pool if str(item.get("type", "")).lower() in wanted]

    if stratify and not indices:
        buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for i, item in pool:
            buckets.setdefault(str(item.get("type", "")), []).append((i, item))
        ordered: list[tuple[int, dict[str, Any]]] = []
        round_index = 0
        while any(len(b) > round_index for b in buckets.values()):
            for key in sorted(buckets):
                bucket = buckets[key]
                if len(bucket) > round_index:
                    ordered.append(bucket[round_index])
            round_index += 1
        pool = ordered

    if limit and not indices:
        pool = pool[:limit]

    if exclude_tasks:
        excluded = {str(x).strip().lower() for x in exclude_tasks}
        pool = [
            (i, item)
            for i, item in pool
            if str(i) not in excluded and task_id_for(i, item).lower() not in excluded
        ]

    return pool


# --------------------------------------------------------------------------
# running one (task, arm, seed)
# --------------------------------------------------------------------------


def build_bench_cmd(
    *,
    arm: str,
    prompt_file: Path,
    workspace: Path,
    artifact_path: Path,
    record_path: Path,
    task_id: str,
    seed: int,
    backend: str,
    model: str | None,
    tier: str,
    work_object: str,
    budget_tokens: int | None,
    max_rounds: int | None,
    runs_root: str | None,
    run_id: str | None = None,
    max_parallel: int = 1,
) -> list[str]:
    cmd = [
        sys.executable, "-m", "kusudaemon.cli", "bench",
        "--benchmark", "longgenbench",
        "--task-id", task_id,
        "--arm", arm,
        "--seed", str(seed),
        "--goal-file", str(prompt_file),
        "--workspace", str(workspace),
        "--backend", backend,
        "--output-dir", str(artifact_path),
        "--output", str(record_path),
        "--json",
    ]
    if run_id:
        cmd += ["--run-id", run_id]
    if model:
        cmd += ["--model", model]
    if runs_root:
        cmd += ["--runs-root", runs_root]
    if arm != "A":
        # BENCHMARKING.md §0.2 Shape B: source material and a rubric, no container.
        # The prompt is the whole corpus, so it is also the --source; without
        # this the run measures an empty workspace, which forces the shell
        # tool allowlist onto a prose writer and collapses the spine to a
        # single synthetic no-files unit.
        cmd += ["--work-object", work_object, "--tier", tier]
        if max_parallel > 1:
            cmd += ["--max-parallel", str(max_parallel)]
        if work_object == "text":
            cmd += ["--source", f"@{prompt_file}"]
        if budget_tokens is not None:
            cmd += ["--budget-tokens", str(budget_tokens)]
        if max_rounds is not None:
            cmd += ["--max-rounds", str(max_rounds)]
    return cmd


def harvest_artifact(
    arm: str,
    artifact_path: Path,
    record: dict[str, Any],
    *,
    runs_root: Path | None = None,
    task_id: str = "",
    seed: int = 1,
    run_id: str = "",
) -> str:
    """Read back the text the run produced.

    `bench` reports where it landed on the record; the explicit --output-dir
    path is the fallback. If a run timed out or was killed before copying,
    search the runs directory for out/*.md.
    PLAN-BENCH-INTEGRITY.md §4.3: restrict glob to run_id when provided.
    PLAN-SWEEP-REPAIR.md §B5: parts-aware — prefer assembly/main.md, then
    per-node resolution via node_artifact_text (out/<node>/ part files),
    then the bare out/*.md glob as the last fallback. bench_common already
    puts src on sys.path so the accessor imports directly.
    """
    candidates = []
    reported = record.get("artifact_path")
    if reported:
        candidates.append(Path(reported))
    candidates.append(artifact_path)
    run_dirs: list[Path] = []
    root = runs_root or (Path.home() / ".kusudaemon" / "runs")
    if root.is_dir():
        if run_id:
            pattern = f"*{run_id}*"
            run_dirs = sorted(root.glob(pattern), reverse=True)
            for rdir in run_dirs:
                out_dir = rdir / "out"
                if out_dir.is_dir():
                    for out_file in out_dir.glob("*.md"):
                        candidates.append(out_file)
        elif task_id:
            pattern = f"*{task_id}*arm{arm}*s{seed}*"
            run_dirs = sorted(root.glob(pattern), reverse=True)
            for rdir in run_dirs:
                out_dir = rdir / "out"
                if out_dir.is_dir():
                    for out_file in out_dir.glob("*.md"):
                        candidates.append(out_file)
    # §B5: assembly first, then per-node resolved text, then file candidates.
    for rdir in run_dirs:
        try:
            main_md = rdir / "assembly" / "main.md"
            if main_md.is_file():
                text = main_md.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    if not artifact_path.is_file():
                        try:
                            artifact_path.write_text(text, encoding="utf-8")
                        except OSError:
                            pass
                    return text
        except OSError:
            pass
    for rdir in run_dirs:
        try:
            sys.path.insert(0, str(_REPO_ROOT / "src"))
            from kusudaemon.v0.run_dir import node_artifact_text
            # Node ids from tree.json when available, else part-dir names.
            node_ids: list[str] = []
            try:
                import json as _json
                tree_file = rdir / "tree.json"
                if tree_file.is_file():
                    data = _json.loads(tree_file.read_text(encoding="utf-8"))
                    items = data if isinstance(data, list) else data.get("nodes", [])
                    if isinstance(items, list):
                        node_ids = [str(n.get("id")) for n in items if isinstance(n, dict) and n.get("id")]
                    elif isinstance(items, dict):
                        node_ids = [str(k) for k in items.keys()]
            except Exception:
                node_ids = []
            if not node_ids:
                out_dir = rdir / "out"
                if out_dir.is_dir():
                    node_ids = sorted(p.name for p in out_dir.iterdir() if p.is_dir() and not p.name.startswith("."))
            parts_texts: list[str] = []
            for nid in node_ids:
                try:
                    t = node_artifact_text(rdir, nid)
                    if t.strip():
                        parts_texts.append(t.rstrip())
                except (FileNotFoundError, OSError):
                    continue
            if parts_texts:
                text = "\n\n".join(parts_texts) + "\n"
                if not artifact_path.is_file():
                    try:
                        artifact_path.write_text(text, encoding="utf-8")
                    except OSError:
                        pass
                return text
        except Exception:
            pass
    for path in candidates:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    if not artifact_path.is_file():
                        try:
                            artifact_path.write_text(text, encoding="utf-8")
                        except OSError:
                            pass
                    return text
        except OSError:
            continue
    return ""


def run_one(
    *,
    index: int,
    item: dict[str, Any],
    arm: str,
    seed: int,
    args: argparse.Namespace,
    results_dir: Path,
) -> dict[str, Any]:
    task_id = task_id_for(index, item)
    stem = f"{task_id}_arm{arm}_seed{seed}"

    prompts_dir = results_dir / "prompts"
    raw_dir = results_dir / "raw"
    bench_dir = results_dir / "bench"
    ws_dir = results_dir / "workspaces" / stem
    for d in (prompts_dir, raw_dir, bench_dir, ws_dir):
        d.mkdir(parents=True, exist_ok=True)

    prompt_file = prompts_dir / f"{task_id}.txt"
    if not prompt_file.is_file():
        prompt_file.write_text(item["prompt"], encoding="utf-8")

    # Arm A's artifact is captured stdout (plain text); arm C's is the
    # exported markdown. Distinct suffixes keep the two from colliding.
    artifact_path = raw_dir / (f"{stem}.txt" if arm == "A" else f"{stem}.md")
    record_path = bench_dir / f"{stem}.json"

    run_id = f"longgen_{stem}"
    # --score-only: the artifact is already on disk (a finished or parked
    # run) — skip the model subprocess entirely and go straight to
    # harvest + score. Same record/prediction/summary shape as a real run.
    score_only = bool(getattr(args, "score_only", False))
    if score_only:
        cmd: list[str] = []
    else:
        cmd = build_bench_cmd(
            arm=arm,
            prompt_file=prompt_file,
            workspace=ws_dir,
            artifact_path=artifact_path,
            record_path=record_path,
            task_id=task_id,
            seed=seed,
            backend=args.backend,
            model=args.model,
            tier=args.tier,
            work_object=args.work_object,
            budget_tokens=args.budget_tokens,
            max_rounds=args.max_rounds,
            runs_root=args.runs_root,
            run_id=run_id,
            max_parallel=int(getattr(args, "max_parallel", 1) or 1),
        )

    if args.dry_run:
        if cmd:
            print("  " + " ".join(cmd))
        else:
            print(f"  (score-only) {stem}")
        return {"task_id": task_id, "arm": arm, "seed": seed, "dry_run": True}

    resolved_flags = parse_flags(getattr(args, "flags", None))
    if score_only:
        wall_clock_s = 0.0
        timed_out = False
        stderr = ""
        returncode = 0
    else:
        env = dict(os.environ)
        env.setdefault("KUSUDAEMON_NO_NOTIFY", "1")
        env.setdefault("KUSUDAEMON_PROVIDER_CONFIG", str(_REPO_ROOT / "provider.json"))
        env.setdefault("KUSUDAEMON_ENV_FILE", str(_REPO_ROOT / ".env"))
        env.setdefault("PYTHONPATH", str(_REPO_ROOT / "src"))
        for k, v in resolved_flags.items():
            env[k] = v

        t0 = time.time()
        timed_out = False
        import signal
        proc = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            _, proc_stderr = proc.communicate(timeout=args.timeout_sec)
            stderr = proc_stderr or ""
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            stderr = f"timeout after {args.timeout_sec}s"
            returncode = -1
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pgid = None
            try:
                _, proc_stderr = proc.communicate(timeout=5)
                stderr = (proc_stderr or "") + "\n" + stderr
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                proc.kill()
                proc.wait()

        wall_clock_s = round(time.time() - t0, 3)

        # PLAN-BENCH-INTEGRITY.md §4.4: After killing proc group, wait up to 5s for record_path
        if timed_out and not record_path.is_file():
            t_wait = 0.0
            while t_wait < 5.0 and not record_path.is_file():
                time.sleep(0.5)
                t_wait += 0.5

    if args.verbose and stderr:
        print(stderr[-2000:], file=sys.stderr)

    record: dict[str, Any] = {}
    if record_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            record = {}

    raw_text = harvest_artifact(
        arm,
        artifact_path,
        record,
        runs_root=Path(args.runs_root) if getattr(args, "runs_root", None) else None,
        task_id=task_id,
        seed=seed,
        run_id=run_id,
    )
    blocks = to_output_blocks(raw_text, item)
    parsed = parse_blocks(blocks, str(item.get("type", "")))
    completion = calculate_completion_rate(parsed, int(item.get("number", 0)))

    halt_reason = record.get("halt_reason") or (stderr[-300:] if returncode != 0 else None)
    halt_category = classify_halt(halt_reason)
    # PLAN-SWEEP-REPAIR.md §C2: "unknown" (escalation with no detail) is
    # quarantined like transport/budget — not a capability measurement.
    is_valid = halt_category not in ("transport", "budget", "unknown")
    invalid_reason = halt_category if not is_valid else None

    return {
        "benchmark": "longgenbench",
        "task_id": task_id,
        "dataset_index": index,
        "type": item.get("type"),
        "number": item.get("number"),
        "arm": arm,
        "seed": seed,
        "backend": args.backend,
        "model": record.get("model") or args.model,
        "tier_measured": record.get("tier_measured"),
        "tier_final": record.get("tier_final"),
        "calls_by_role": record.get("calls_by_role", {}),
        "tokens_by_role": record.get("tokens_by_role", {}),
        "wall_clock_s": record.get("wall_clock_s", wall_clock_s),
        "harness_wall_clock_s": wall_clock_s,
        "returncode": returncode,
        "timed_out": timed_out,
        "halt_reason": halt_reason,
        "valid": is_valid,
        "invalid_reason": invalid_reason,
        "flags": resolved_flags,
        "artifact_path": str(artifact_path) if artifact_path.is_file() else None,
        "artifact_chars": len(raw_text),
        "blocks_found": len(parsed),
        "blocks_expected": int(item.get("number", 0)),
        "completion_rate": round(completion, 3),
        "max_parallel": record.get("max_parallel", int(getattr(args, "max_parallel", 1) or 1)),
        "max_parallel_derived": record.get("max_parallel_derived"),
        "commit": record.get("commit"),
    }


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------


def _read_records_file(path: Path) -> list[dict[str, Any]]:
    """Prior run records (records.jsonl is append-only across sweeps)."""
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest wins per (task_id, arm, seed) so a re-scored cell never
    double-counts in the summary."""
    merged: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    for rec in records:
        merged[(rec.get("task_id"), rec.get("arm"), rec.get("seed"))] = rec
    return list(merged.values())


def _total_tokens(rec: dict[str, Any]) -> int:
    return sum(int(v or 0) for v in (rec.get("tokens_by_role") or {}).values())


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        arm = rec["arm"]
        bucket = by_arm.setdefault(
            arm,
            {
                "runs": 0,
                "valid_runs": 0,
                "completion_rates": [],
                "tokens": 0,
                "wall_clock_s": 0.0,
                "timeouts": 0,
                "empty_artifacts": 0,
                "halts": 0,
                "excluded_by_reason": {},
            },
        )
        bucket["runs"] += 1
        is_valid = rec.get("valid", True)
        if not is_valid:
            reason = rec.get("invalid_reason") or classify_halt(rec.get("halt_reason"))
            bucket["excluded_by_reason"][reason] = bucket["excluded_by_reason"].get(reason, 0) + 1
        else:
            bucket["valid_runs"] += 1
            bucket["completion_rates"].append(rec.get("completion_rate", 0.0))
            bucket["tokens"] += _total_tokens(rec)

        bucket["wall_clock_s"] += float(rec.get("harness_wall_clock_s") or 0.0)
        if rec.get("timed_out"):
            bucket["timeouts"] += 1
        if not rec.get("artifact_chars"):
            bucket["empty_artifacts"] += 1
        if rec.get("halt_reason"):
            bucket["halts"] += 1

    summary: dict[str, Any] = {"arms": {}}
    for arm, bucket in sorted(by_arm.items()):
        rates = bucket["completion_rates"]
        valid_n = bucket["valid_runs"]
        total_n = bucket["runs"]
        excluded_count = sum(bucket["excluded_by_reason"].values())
        mean_pct = round(sum(rates) / len(rates), 3) if rates else 0.0
        arm_stats: dict[str, Any] = {
            "runs": total_n,
            "valid_runs": valid_n,
            "mean_completion_pct": mean_pct,
            "total_tokens": bucket["tokens"],
            "total_wall_clock_s": round(bucket["wall_clock_s"], 1),
            "timeouts": bucket["timeouts"],
            "empty_artifacts": bucket["empty_artifacts"],
            "halts": bucket["halts"],
            "excluded": {
                "total": excluded_count,
                "by_reason": bucket["excluded_by_reason"],
            },
        }
        if total_n > 0 and (excluded_count / total_n) > 0.20:
            arm_stats["excessive_exclusions"] = True
            print(f"\n[WARNING] Arm {arm} has {excluded_count}/{total_n} "
                  f"({excluded_count/total_n*100:.1f}%) excluded runs! Not a defensible result.",
                  file=sys.stderr)
        summary["arms"][arm] = arm_stats

    # BENCHMARKING.md §0.1: report cost-per-point, not just the pass rate
    for arm, stats in summary["arms"].items():
        pct = stats["mean_completion_pct"]
        stats["tokens_per_completion_point"] = (
            round(stats["total_tokens"] / pct, 1) if pct else None
        )

    suspect = check_identical_seeds(records, key_field="task_id", arm_field="arm")
    if suspect:
        summary["suspect_identical_seeds"] = suspect
    return summary


def write_predictions(
    records: list[dict[str, Any]],
    tasks: list[tuple[int, dict[str, Any]]],
    results_dir: Path,
) -> list[Path]:
    """Write one upstream-format prediction file per (arm, seed)."""
    by_index = {i: item for i, item in tasks}
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("dry_run"):
            continue
        # PLAN-SWEEP-REPAIR.md §C1: a resumed matrix's records.jsonl may hold
        # rows for tasks this invocation did not select — skip them here too
        # so a stale row can never crash prediction writing even if the
        # caller forgot to filter upstream.
        if rec.get("dataset_index") not in by_index:
            continue
        grouped.setdefault((rec["arm"], rec["seed"]), []).append(rec)

    written: list[Path] = []
    pred_dir = results_dir / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)
    for (arm, seed), recs in sorted(grouped.items()):
        entries = []
        for rec in sorted(recs, key=lambda r: r["dataset_index"]):
            item = by_index[rec["dataset_index"]]
            path = rec.get("artifact_path")
            raw = ""
            if path:
                try:
                    raw = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    raw = ""
            entry = build_prediction_record(item, raw)
            entry["task_id"] = rec["task_id"]
            entries.append(entry)
        out = pred_dir / f"predictions_arm{arm}_seed{seed}.json"
        out.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(out)
    return written


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--longgen-dir", default=str(Path.home() / "LongGenBench"),
                   help="Path to a LongGenBench checkout (default: ~/LongGenBench).")
    p.add_argument("--clone-if-missing", action="store_true",
                   help="git clone the dataset repo if --longgen-dir does not exist.")
    p.add_argument("--dataset", default="short",
                   help="'short' (16K targets), 'long' (32K targets), or a path to a dataset JSON.")

    p.add_argument("--limit", type=int, default=8,
                   help="Number of tasks to run (default 8). 0 means all 400.")
    p.add_argument("--tasks", type=int, nargs="+", default=None,
                   help="Explicit dataset indices, overriding --limit/--stratify.")
    p.add_argument("--exclude-tasks", nargs="+", default=None,
                   help="Dataset indices or task IDs to exclude (e.g. 300 or 300-block).")
    p.add_argument("--types", nargs="+", default=None,
                   help="Restrict to scenario types, e.g. Week Floor 'Menu Week' Block.")
    p.add_argument("--no-stratify", dest="stratify", action="store_false", default=True,
                   help="Take the first N tasks instead of round-robining across types.")

    p.add_argument("--arms", nargs="+", default=["A", "C"], choices=["A", "B", "C"],
                   help="Arms to run (default A C; BENCHMARKING.md §0.1 skips B).")
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3],
                   help="Seeds per (task, arm) (default 1 2 3).")

    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--tier", default="auto", choices=("auto", "T0", "T1", "T2", "T3"),
                   help="Arm B/C tier floor (default auto).")
    p.add_argument("--max-parallel", type=int, default=1,
                   help="Arm C concurrency cap; 1 runs sequentially (default 1).")
    p.add_argument("--work-object", default="text", choices=("text", "workspace"),
                   help="Arm B/C work-object kind (default text, per BENCHMARKING.md §0.2 Shape B).")
    p.add_argument("--budget-tokens", type=int, default=None,
                   help="Arm B/C token ceiling. Unset means unbounded.")
    p.add_argument("--max-rounds", type=int, default=None, help="Arm B/C round ceiling.")
    p.add_argument("--runs-root", default=None, help="Override the kusudaemon runs root.")

    p.add_argument("--timeout-sec", type=int, default=5400,
                   help="Per-(task, arm, seed) wall-clock cap (default 5400).")
    p.add_argument("--flags", default=None,
                   help="Comma- or space-separated env flags to pass through (e.g. KUSUDAEMON_TIER_OUTPUT_SIGNALS=1).")
    p.add_argument("--results-dir", default=str(_REPO_ROOT / "bench_results" / "longgen"))
    p.add_argument("--resume", action="store_true",
                   help="Skip a cell whose bench record already exists.")
    p.add_argument("--score-only", action="store_true",
                   help="Skip the model subprocess per cell; harvest the "
                        "artifact already on disk (finished or parked run) "
                        "and run the identical harvest + score + predictions "
                        "+ summary path. No generation, no API calls.")
    p.add_argument("--list-tasks", action="store_true", help="Print the selection and exit.")
    p.add_argument("--dry-run", action="store_true", help="Print the matrix and exit.")
    p.add_argument("--verbose", action="store_true", help="Echo subprocess stderr.")
    return p


def resolve_dataset(args: argparse.Namespace) -> Path:
    if args.dataset in DATASETS:
        root = Path(args.longgen_dir).expanduser()
        if not root.is_dir():
            if not args.clone_if_missing:
                raise SystemExit(
                    f"LongGenBench checkout not found at {root}.\n"
                    f"  git clone {LONGGEN_REPO} {root}\n"
                    f"or pass --clone-if-missing."
                )
            print(f"cloning {LONGGEN_REPO} -> {root}")
            subprocess.run(["git", "clone", "--depth", "1", LONGGEN_REPO, str(root)], check=True)
        path = root / DATASETS[args.dataset]
    else:
        path = Path(args.dataset).expanduser()
    if not path.is_file():
        raise SystemExit(f"dataset not found: {path}")
    return path


def main() -> int:
    args = build_parser().parse_args()

    dataset_path = resolve_dataset(args)
    data = load_dataset(dataset_path)
    tasks = select_tasks(
        data,
        limit=(args.limit or None),
        indices=args.tasks,
        exclude_tasks=args.exclude_tasks,
        types=args.types,
        stratify=args.stratify,
    )
    if not tasks:
        raise SystemExit("no tasks selected")

    results_dir = Path(args.results_dir).expanduser()
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"dataset:  {dataset_path}  ({len(data)} tasks)")
    print(f"selected: {len(tasks)} tasks x {len(args.arms)} arms x {len(args.seeds)} seeds "
          f"= {len(tasks) * len(args.arms) * len(args.seeds)} runs")
    for index, item in tasks:
        print(f"  [{index:3d}] {task_id_for(index, item)}  "
              f"{item.get('number')} x {item.get('type')!r} entries")
    if args.list_tasks:
        return 0

    records: list[dict[str, Any]] = []
    records_path = results_dir / "records.jsonl"
    total = len(tasks) * len(args.arms) * len(args.seeds)
    done = 0

    for index, item in tasks:
        for arm in args.arms:
            for seed in args.seeds:
                done += 1
                task_id = task_id_for(index, item)
                stem = f"{task_id}_arm{arm}_seed{seed}"
                existing = results_dir / "bench" / f"{stem}.json"
                if args.resume and existing.is_file() and not args.dry_run:
                    print(f"[{done}/{total}] {stem}: skipped (--resume)")
                    continue
                if not args.dry_run:
                    print(f"[{done}/{total}] {stem}: running", flush=True)
                rec = run_one(
                    index=index, item=item, arm=arm, seed=seed,
                    args=args, results_dir=results_dir,
                )
                records.append(rec)
                if not args.dry_run:
                    with open(records_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"    completion={rec['completion_rate']}% "
                          f"blocks={rec['blocks_found']}/{rec['blocks_expected']} "
                          f"chars={rec['artifact_chars']} "
                          f"wall={rec['harness_wall_clock_s']}s "
                          f"halt={rec['halt_reason'] or '-'}")

    if args.dry_run:
        return 0

    # PLAN-BENCH-INTEGRITY.md §4.1: Rebuild record list from disk before aggregating
    # so earlier completed cells are never discarded.
    # PLAN-SWEEP-REPAIR.md §C1: records.jsonl is append-only across sweeps and
    # still holds rows for tasks this invocation did not select (e.g. the
    # Sep-6 300-block rows under a --tasks 100 run). Filter to this matrix's
    # selected indices — §4.1 is about not discarding earlier cells *of this
    # matrix*, and rows for unselected tasks were never part of it. Without
    # this, write_predictions dies with KeyError on the foreign index and
    # summarize silently folds another task's runs into this arm's stats.
    selected = {i for i, _ in tasks}
    all_records = [
        r for r in dedupe_records(read_records_jsonl(records_path) + records)
        if r.get("dataset_index") in selected
    ]
    preds = write_predictions(all_records, tasks, results_dir)
    summary = summarize(all_records)
    (results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))
    print("\nprediction files (feed these to scripts/eval_longgen_free.py):")
    for path in preds:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
