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
        return [(i, by_index[i]) for i in indices]

    if types:
        wanted = {t.lower() for t in types}
        pool = [(i, item) for i, item in pool if str(item.get("type", "")).lower() in wanted]

    if stratify:
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

    return pool[:limit] if limit else pool


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
) -> str:
    """Read back the text the run produced.

    `bench` reports where it landed on the record; the explicit --output-dir
    path is the fallback. If a run timed out or was killed before copying,
    search the runs directory for out/*.md.
    """
    candidates = []
    reported = record.get("artifact_path")
    if reported:
        candidates.append(Path(reported))
    candidates.append(artifact_path)
    root = runs_root or (Path.home() / ".kusudaemon" / "runs")
    if root.is_dir() and task_id:
        pattern = f"*{task_id}*arm{arm}*s{seed}*"
        for rdir in sorted(root.glob(pattern), reverse=True):
            out_dir = rdir / "out"
            if out_dir.is_dir():
                for out_file in out_dir.glob("*.md"):
                    candidates.append(out_file)
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
        )

    if args.dry_run:
        if cmd:
            print("  " + " ".join(cmd))
        else:
            print(f"  (score-only) {stem}")
        return {"task_id": task_id, "arm": arm, "seed": seed, "dry_run": True}

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

        t0 = time.time()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(_REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout_sec,
                stdin=subprocess.DEVNULL,
            )
            stderr = proc.stderr or ""
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stderr = f"timeout after {args.timeout_sec}s"
            returncode = -1
            # A timeout still leaves whatever the run exported on disk.
            _ = exc
        wall_clock_s = round(time.time() - t0, 3)

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
    )
    blocks = to_output_blocks(raw_text, item)
    parsed = parse_blocks(blocks, str(item.get("type", "")))
    completion = calculate_completion_rate(parsed, int(item.get("number", 0)))

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
        "halt_reason": record.get("halt_reason") or (stderr[-300:] if returncode != 0 else None),
        "artifact_path": str(artifact_path) if artifact_path.is_file() else None,
        "artifact_chars": len(raw_text),
        "blocks_found": len(parsed),
        "blocks_expected": int(item.get("number", 0)),
        "completion_rate": round(completion, 3),
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
            {"runs": 0, "completion_rates": [], "tokens": 0, "wall_clock_s": 0.0,
             "timeouts": 0, "empty_artifacts": 0, "halts": 0},
        )
        bucket["runs"] += 1
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
        summary["arms"][arm] = {
            "runs": bucket["runs"],
            "mean_completion_rate": round(sum(rates) / len(rates), 3) if rates else 0.0,
            "total_tokens": bucket["tokens"],
            "total_wall_clock_s": round(bucket["wall_clock_s"], 1),
            "timeouts": bucket["timeouts"],
            "empty_artifacts": bucket["empty_artifacts"],
            "halts": bucket["halts"],
        }
    # BENCHMARKING.md §0.1: report cost-per-point, not just the pass rate, or a
    # more expensive arm looks better for the wrong reason.
    for arm, stats in summary["arms"].items():
        rate = stats["mean_completion_rate"]
        stats["tokens_per_completion_point"] = (
            round(stats["total_tokens"] / rate, 1) if rate else None
        )
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
    p.add_argument("--work-object", default="text", choices=("text", "workspace"),
                   help="Arm B/C work-object kind (default text, per BENCHMARKING.md §0.2 Shape B).")
    p.add_argument("--budget-tokens", type=int, default=None,
                   help="Arm B/C token ceiling. Unset means unbounded.")
    p.add_argument("--max-rounds", type=int, default=None, help="Arm B/C round ceiling.")
    p.add_argument("--runs-root", default=None, help="Override the kusudaemon runs root.")

    p.add_argument("--timeout-sec", type=int, default=3600,
                   help="Per-(task, arm, seed) wall-clock cap (default 3600).")
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

    preds = write_predictions(records, tasks, results_dir)
    if getattr(args, "score_only", False):
        # The current sweep scored a subset; summarize the whole history
        # (deduped, latest wins) so summary.json never regresses to a
        # partial view and re-scored cells don't double-count.
        summary = summarize(
            _dedupe_records(_read_records_file(records_path) + records)
        )
    else:
        summary = summarize(records)
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
