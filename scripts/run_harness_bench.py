#!/usr/bin/env python3
"""Drive HarnessBench against kusudaemon across arms (BENCHMARKING.md §0.1, §0.5, §0.4).

HarnessBench owns the environment, the prompt and the grader; this script owns
the experimental matrix. For each (task, arm, seed) it shells out to
HarnessBench's own CLI -- so the score is HarnessBench's oracle, never ours --
and merges that verdict with the accounting kusudaemon's `bench` entry point
writes into the sandbox, producing one record per the BENCHMARKING.md §0.4 schema.

HarnessBench runs tasks in a plain filesystem sandbox (`fixtures/` copied into
`sandbox/workspace`). There are no containers and nothing to download beyond
the checkout itself.

Setup (see BENCHMARKING.md §3):

    python3 scripts/run_harness_bench.py --bench-dir ../harness-bench \\
        --write-harness-config

Dry run of the matrix:

    python3 scripts/run_harness_bench.py --bench-dir ../harness-bench \\
        --classes "Long-running Autonomy & State Adaptation" --dry-run

Real sweep:

    python3 scripts/run_harness_bench.py --bench-dir ../harness-bench \\
        --classes "Long-running Autonomy & State Adaptation" \\
        --arms A C --seeds 1 2 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

HARNESS_BENCH_REPO = "https://github.com/Qihoo360/harness-bench"
DEFAULT_BACKEND = "opencode"
DEFAULT_MODEL = "opencode/nemotron-3.5-lightning-free"

# Harness entry names this script registers in the HarnessBench checkout.
ARM_HARNESS_IDS = {"A": "kusudaemon-armA", "B": "kusudaemon-armB", "C": "kusudaemon-armC"}

_TASK_YAML_KEY = re.compile(r'^\s*(task_id|title|class|timeout_sec)\s*:\s*"?(.*?)"?\s*$')
_LEADING_NUM = re.compile(r"^(\d+)-")


# --------------------------------------------------------------------------
# task discovery
# --------------------------------------------------------------------------


def parse_task_yaml(path: Path) -> dict[str, Any]:
    """Read the handful of scalar keys we need out of a task.yaml.

    Deliberately not a YAML parse: this script must run from the kusudaemon
    repo, whose dependency set does not include PyYAML. The keys we read are
    plain top-level scalars in every task in the suite.
    """
    data: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _TASK_YAML_KEY.match(line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            data[key] = int(value) if key == "timeout_sec" and value.isdigit() else value
    return data


def discover_tasks(bench_dir: Path) -> list[dict[str, Any]]:
    """Enumerate `tasks/<NNN-name>/task.yaml` in a HarnessBench checkout."""
    tasks_root = bench_dir / "tasks"
    if not tasks_root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for task_dir in sorted(tasks_root.iterdir()):
        spec = task_dir / "task.yaml"
        if not (task_dir.is_dir() and spec.is_file()):
            continue
        meta = parse_task_yaml(spec)
        task_id = meta.get("task_id") or task_dir.name
        m = _LEADING_NUM.match(str(task_id))
        found.append({
            "task_id": str(task_id),
            "title": meta.get("title", ""),
            "class": meta.get("class", ""),
            "timeout_sec": meta.get("timeout_sec"),
            "num": int(m.group(1)) if m else None,
            "path": str(task_dir),
        })
    found.sort(key=lambda t: (t["num"] if t["num"] is not None else 10**9, t["task_id"]))
    return found


def filter_tasks(
    tasks: list[dict[str, Any]],
    *,
    task_ids: list[str] | None,
    classes: list[str] | None,
    from_num: int | None,
    to_num: int | None,
) -> list[dict[str, Any]]:
    out = list(tasks)
    if task_ids:
        wanted = set(task_ids)
        out = [t for t in out if t["task_id"] in wanted]
        missing = wanted - {t["task_id"] for t in out}
        if missing:
            raise SystemExit(f"unknown task ids: {sorted(missing)}")
    if classes:
        wanted_c = {c.strip().lower() for c in classes}
        out = [t for t in out if str(t["class"]).strip().lower() in wanted_c]
    if from_num is not None:
        out = [t for t in out if t["num"] is not None and t["num"] >= from_num]
    if to_num is not None:
        out = [t for t in out if t["num"] is not None and t["num"] <= to_num]
    return out


# --------------------------------------------------------------------------
# harness config registration
# --------------------------------------------------------------------------


def write_harness_config(bench_dir: Path, arms: list[str], timeout_sec: int) -> Path:
    """Add/refresh the kusudaemon entries in the checkout's config/harness.yaml.

    HarnessBench's `generic_cli` adapter runs `command` with cwd set to the task
    workspace, so `command` must be an absolute path.
    """
    cfg_dir = bench_dir / "config"
    target = cfg_dir / "harness.yaml"
    if not target.is_file():
        example = cfg_dir / "harness.example.yaml"
        if not example.is_file():
            raise SystemExit(f"no config/harness.example.yaml under {bench_dir}")
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    wrapper = (_REPO_ROOT / "scripts" / "hb_adapter.sh").resolve()
    if not wrapper.is_file():
        raise SystemExit(f"wrapper not found: {wrapper}")

    text = target.read_text(encoding="utf-8")
    blocks = []
    for arm in arms:
        hid = ARM_HARNESS_IDS[arm]
        if f'"{hid}"' in text:
            continue
        blocks.append(
            f'  "{hid}": {{\n'
            f'    "adapter": "generic_cli",\n'
            f'    "command": "{wrapper}",\n'
            f'    "session_prefix": "hb-kusu-{arm.lower()}",\n'
            f'    "timeout_sec": {timeout_sec},\n'
            f'    "args": ["{arm}", "{{workspace}}", "{{prompt_file}}", '
            f'"{{session_id}}", "{{task_id}}"]\n'
            f'  }},\n'
        )
    if blocks:
        anchor = '"models": {\n'
        idx = text.index(anchor) + len(anchor)
        text = text[:idx] + "".join(blocks) + text[idx:]
        target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


def _extract_stdout_json(stdout: str) -> dict[str, Any] | None:
    """HarnessBench prints progress lines, then one indented JSON object."""
    start = stdout.find("\n{\n")
    if start == -1:
        start = stdout.find("{\n")
        if start == -1:
            return None
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None



def merge_round_records(sandbox: Path) -> dict[str, Any]:
    """Sum kusudaemon's per-round sidecar records into one accounting view.

    Multi-round HarnessBench tasks invoke the adapter once per round; each
    invocation is a separate kusudaemon run with its own ledger. Counts and
    wall clock add up; tier and commit come from the final round; halt reasons
    are collected in order so a mid-task failure stays visible.
    """
    records: list[dict[str, Any]] = []
    for path in sorted(sandbox.glob("kusudaemon-record-r*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    if not records:
        fallback = sandbox / "kusudaemon-record.json"
        if fallback.is_file():
            try:
                return json.loads(fallback.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        return {}
    if len(records) == 1:
        return records[0]

    merged = dict(records[-1])
    calls: dict[str, int] = {}
    tokens: dict[str, int] = {}
    escalations: list[Any] = []
    halts: list[str] = []
    wall = 0.0
    for i, rec in enumerate(records, start=1):
        for role, n in (rec.get("calls_by_role") or {}).items():
            calls[role] = calls.get(role, 0) + int(n or 0)
        for role, n in (rec.get("tokens_by_role") or {}).items():
            tokens[role] = tokens.get(role, 0) + int(n or 0)
        escalations.extend(rec.get("escalations") or [])
        wall += float(rec.get("wall_clock_s") or 0.0)
        if rec.get("halt_reason"):
            halts.append(f"round {i}: {rec['halt_reason']}")
    merged.update({
        "calls_by_role": calls,
        "tokens_by_role": tokens,
        "escalations": escalations,
        "wall_clock_s": round(wall, 3),
        "halt_reason": "; ".join(halts) or None,
        "rounds": len(records),
    })
    return merged


def run_one(
    *,
    bench_dir: Path,
    bench_python: str,
    task: dict[str, Any],
    arm: str,
    seed: int,
    backend: str,
    model: str,
    budget_tokens: int | None,
    max_rounds: int | None,
    tier: str,
    extra_env: dict[str, str],
    verbose: bool,
    max_parallel: int = 1,
) -> dict[str, Any]:
    task_id = task["task_id"]
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": "src",
        "KUSU_BENCH_BACKEND": backend,
        "KUSU_BENCH_MODEL": model,
        "KUSU_BENCH_SEED": str(seed),
        # Empty means "no ceiling"; the wrapper omits the flag entirely.
        "KUSU_BENCH_BUDGET_TOKENS": "" if budget_tokens is None else str(budget_tokens),
        "KUSU_BENCH_MAX_ROUNDS": "" if max_rounds is None else str(max_rounds),
        "KUSU_BENCH_TIER": tier,
        "KUSU_BENCH_MAX_PARALLEL": str(max_parallel),
        "KUSU_BENCH_NAME": "harness-bench",
        # kusudaemon resolves provider.json/.env from cwd; the wrapper runs
        # with cwd set to the task workspace, so pin both.
        "KUSUDAEMON_PROVIDER_CONFIG": str(_REPO_ROOT / "provider.json"),
        "KUSUDAEMON_ENV_FILE": str(_REPO_ROOT / ".env"),
        "KUSUDAEMON_ROLE_TRANSPORT": os.getenv("KUSUDAEMON_ROLE_TRANSPORT", "backend"),
    })
    env.update(extra_env)

    cmd = [
        bench_python, "-m", "harnessbench.cli", "run-task",
        "--task", task_id,
        "--harness", ARM_HARNESS_IDS[arm],
        "--mode", "live",
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(bench_dir), env=env, text=True, capture_output=True)
    wall = round(time.time() - t0, 3)
    if verbose and proc.stderr.strip():
        print(proc.stderr.strip()[-2000:], file=sys.stderr)

    hb = _extract_stdout_json(proc.stdout) or {}
    scoring = hb.get("scoring") or {}
    oracle = hb.get("oracle_result") or {}
    usage = hb.get("usage_summary") or {}

    # kusudaemon's own accounting, written by `kusudaemon bench --output` --
    # one file per HarnessBench round, merged here so a multi-round task
    # reports the whole task's spend rather than just its last round.
    sandbox = hb.get("sandbox")
    kusu = merge_round_records(Path(sandbox)) if sandbox else {}

    score = scoring.get("combined_score")
    record = {
        "benchmark": "harness-bench",
        "task_id": task_id,
        "task_class": task.get("class", ""),
        "arm": arm,
        "seed": seed,
        "model": kusu.get("model") or model,
        "backend": backend,
        "score": float(score) if isinstance(score, (int, float)) else 0.0,
        "resolved": bool(isinstance(score, (int, float)) and score >= 0.95),
        "tier_measured": kusu.get("tier_measured"),
        "tier_final": kusu.get("tier_final"),
        "escalations": kusu.get("escalations", []),
        "calls_by_role": kusu.get("calls_by_role", {}),
        "tokens_by_role": kusu.get("tokens_by_role", {}),
        "wall_clock_s": kusu.get("wall_clock_s", wall),
        "halt_reason": kusu.get("halt_reason"),
        "commit": kusu.get("commit", "unknown"),
        "rounds": kusu.get("rounds", 1),
        "flags": dict(extra_env),
        "max_parallel": kusu.get("max_parallel", max_parallel),
        "max_parallel_derived": kusu.get("max_parallel_derived"),
        # HarnessBench's own view, kept verbatim so the score is auditable.
        "harness_bench": {
            "outcome_score": oracle.get("outcome_score"),
            "process_effective": scoring.get("process_effective"),
            "security_score": scoring.get("security_score"),
            "combined_score": score,
            "checks": oracle.get("checks", []),
            "notes": scoring.get("notes"),
            "usage_summary": usage,
            "sandbox": sandbox,
            "elapsed_sec": hb.get("elapsed_sec"),
        },
        "bench_exit_code": proc.returncode,
        "harness_wall_clock_s": wall,
    }
    if not hb:
        record["halt_reason"] = (
            record["halt_reason"]
            or f"harnessbench produced no result JSON (exit {proc.returncode}): "
               f"{proc.stderr.strip()[-300:] or proc.stdout.strip()[-300:]}"
        )
    halt_category = classify_halt(record.get("halt_reason"))
    # PLAN-SWEEP-REPAIR.md §C2: "unknown" quarantined like transport/budget.
    is_valid = halt_category not in ("transport", "budget", "unknown")
    record["valid"] = is_valid
    record["invalid_reason"] = halt_category if not is_valid else None
    return record


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _total_tokens(rec: dict[str, Any]) -> int:
    by_role = rec.get("tokens_by_role") or {}
    if by_role:
        return sum(int(v or 0) for v in by_role.values())
    usage = ((rec.get("harness_bench") or {}).get("usage_summary")) or {}
    return int(usage.get("total_tokens") or 0)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-arm aggregates: mean score, resolve rate, spend, cost per solve.
    PLAN-BENCH-INTEGRITY.md §4.2: quarantine transport and budget halts.
    PLAN-SWEEP-REPAIR.md §C2: "unknown" halts quarantined the same way."""
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_arm.setdefault(r["arm"], []).append(r)

    arms: dict[str, Any] = {}
    for arm, recs in sorted(by_arm.items()):
        total_runs = len(recs)
        valid_recs = [
            r for r in recs
            if r.get("valid", True) and classify_halt(r.get("halt_reason")) not in ("transport", "budget", "unknown")
        ]
        invalid_recs = [r for r in recs if r not in valid_recs]
        n = len(valid_recs)
        scores = [float(r.get("score") or 0.0) for r in valid_recs]
        solved = sum(1 for r in valid_recs if r.get("resolved"))
        tokens = sum(_total_tokens(r) for r in valid_recs)
        mean = sum(scores) / n if n else 0.0
        var = sum((s - mean) ** 2 for s in scores) / n if n else 0.0

        excluded_by_reason: dict[str, int] = {}
        for r in invalid_recs:
            reason = r.get("invalid_reason") or classify_halt(r.get("halt_reason"))
            excluded_by_reason[reason] = excluded_by_reason.get(reason, 0) + 1

        arm_stats: dict[str, Any] = {
            "runs": total_runs,
            "valid_runs": n,
            "mean_score": round(mean, 4),
            "score_stdev": round(var ** 0.5, 4),
            "resolved": solved,
            "resolve_rate": round(solved / n, 4) if n else 0.0,
            "total_tokens": tokens,
            "mean_tokens_per_run": round(tokens / n, 1) if n else 0.0,
            "tokens_per_solved_task": round(tokens / solved, 1) if solved else None,
            "mean_wall_clock_s": round(
                sum(float(r.get("wall_clock_s") or 0) for r in recs) / total_runs, 2
            ) if total_runs else 0.0,
            "halts": sum(1 for r in recs if r.get("halt_reason")),
            "excluded": {
                "total": len(invalid_recs),
                "by_reason": excluded_by_reason,
            },
        }
        if total_runs > 0 and (len(invalid_recs) / total_runs) > 0.20:
            arm_stats["excessive_exclusions"] = True
            print(f"\n[WARNING] Arm {arm} has {len(invalid_recs)}/{total_runs} "
                  f"({len(invalid_recs)/total_runs*100:.1f}%) excluded runs! Not a defensible result.",
                  file=sys.stderr)
        arms[arm] = arm_stats

    per_task: dict[str, Any] = {}
    for r in records:
        slot = per_task.setdefault(r["task_id"], {"class": r.get("task_class", ""), "arms": {}})
        slot["arms"].setdefault(r["arm"], []).append(round(float(r.get("score") or 0.0), 4))

    suspect = check_identical_seeds(
        records,
        key_field="task_id",
        arm_field="arm",
        size_extractor=lambda r: int(((r.get("harness_bench") or {}).get("elapsed_sec") or 0) * 100),
    )
    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commit": next((r.get("commit") for r in records if r.get("commit")), "unknown"),
        "arms": arms,
        "per_task_scores": per_task,
        "caveat": (
            "Token spend must be within ~10% across arms for the comparison to "
            "mean anything (BENCHMARKING.md §0.1). Check mean_tokens_per_run before "
            "reading any delta as a harness effect."
        ),
    }
    if suspect:
        summary["suspect_identical_seeds"] = suspect
    return summary


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bench-dir", default=str(_REPO_ROOT.parent / "harness-bench"),
                   help="Path to the harness-bench checkout.")
    p.add_argument("--clone-if-missing", action="store_true",
                   help=f"git clone {HARNESS_BENCH_REPO} into --bench-dir if absent.")
    p.add_argument("--bench-python", default=None,
                   help="Python for HarnessBench (needs 3.11+ and PyYAML). "
                        "Defaults to <bench-dir>/.venv/bin/python if present.")
    p.add_argument("--write-harness-config", action="store_true",
                   help="Register the kusudaemon arm entries in the checkout's "
                        "config/harness.yaml, then exit.")
    p.add_argument("--list-tasks", action="store_true", help="Print the task table and exit.")

    p.add_argument("--tasks", nargs="+", default=None, help="Explicit task ids.")
    p.add_argument("--classes", nargs="+", default=None,
                   help='Task classes, e.g. "Long-running Autonomy & State Adaptation".')
    p.add_argument("--from-num", type=int, default=None, help="Lowest task number (inclusive).")
    p.add_argument("--to-num", type=int, default=None, help="Highest task number (inclusive).")

    p.add_argument("--arms", nargs="+", default=["A", "C"], choices=["A", "B", "C"],
                   help="Arms to run (default: A C -- arm B skipped for cost).")
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3],
                   help="Seeds; N=3 minimum (BENCHMARKING.md §0.1).")
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--tier", default="auto")
    p.add_argument("--max-parallel", type=int, default=1,
                   help="Arm C concurrency cap; 1 runs sequentially (default 1).")
    p.add_argument("--budget-tokens", type=int, default=None,
                   help="Optional per-task token ceiling. Unset by default: "
                        "HarnessBench imposes no token budget, and kusudaemon "
                        "treats an unset ceiling as unbounded. Runs stay bounded "
                        "by --max-rounds and --harness-timeout-sec. Set this only "
                        "when you want a graceful, recorded budget halt.")
    p.add_argument("--max-rounds", type=int, default=None,
                   help="Optional round ceiling. Unset by default, so the "
                        "kusudaemon CLI's own default (100) applies.")
    p.add_argument("--harness-timeout-sec", type=int, default=2400,
                   help="Per-task wall-clock cap written into harness.yaml.")
    p.add_argument("--proxy", action="store_true",
                   help="Route role calls through HarnessBench's usage proxy "
                        "(needs --upstream; not possible for the opencode backend).")
    p.add_argument("--upstream", default=None, help="Upstream base_url for --proxy.")
    p.add_argument("--skip-process-grade", action="store_true", default=True,
                   help="Skip HarnessBench's LLM process rubric (default: on; "
                        "keeps the run free beyond your own model calls).")
    p.add_argument("--with-process-grade", dest="skip_process_grade", action="store_false",
                   help="Enable the LLM process/security rubric. Needs "
                        "RUBRIC_BASE_URL / RUBRIC_API_KEY / RUBRIC_MODEL.")

    p.add_argument("--results-dir", default=str(_REPO_ROOT / "bench_results"))
    p.add_argument("--resume", action="store_true",
                   help="Skip a cell whose bench record already exists.")
    p.add_argument("--flags", default=None,
                   help="Comma- or space-separated env flags to pass through (e.g. KUSUDAEMON_TIER_OUTPUT_SIGNALS=1).")
    p.add_argument("--drop-solved-by-a", action="store_true",
                   help="Skip a task in later arms if every arm-A seed solved it "
                        "(BENCHMARKING.md §0.4).")
    p.add_argument("--dry-run", action="store_true", help="Print the matrix and exit.")
    p.add_argument("--verbose", action="store_true", help="Echo HarnessBench stderr.")
    return p


def resolve_bench_python(bench_dir: Path, override: str | None) -> str:
    if override:
        return override
    venv = bench_dir / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def main() -> int:
    args = build_parser().parse_args()
    bench_dir = Path(args.bench_dir).expanduser().resolve()

    if not bench_dir.is_dir():
        if not args.clone_if_missing:
            print(f"harness-bench not found at {bench_dir}\n"
                  f"  clone it:  git clone {HARNESS_BENCH_REPO} {bench_dir}\n"
                  f"  or pass --clone-if-missing", file=sys.stderr)
            return 2
        bench_dir.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["git", "clone", HARNESS_BENCH_REPO, str(bench_dir)])
        if res.returncode != 0:
            return res.returncode

    if args.write_harness_config:
        target = write_harness_config(bench_dir, args.arms, args.harness_timeout_sec)
        print(f"registered {[ARM_HARNESS_IDS[a] for a in args.arms]} in {target}")
        return 0

    all_tasks = discover_tasks(bench_dir)
    if not all_tasks:
        print(f"no tasks found under {bench_dir / 'tasks'}", file=sys.stderr)
        return 2

    if args.list_tasks:
        for t in all_tasks:
            print(f"{t['task_id']:<42} {t['class']}")
        print(f"\n{len(all_tasks)} tasks", file=sys.stderr)
        return 0

    tasks = filter_tasks(all_tasks, task_ids=args.tasks, classes=args.classes,
                         from_num=args.from_num, to_num=args.to_num)
    if not tasks:
        print("no tasks matched the filters", file=sys.stderr)
        return 2

    # Arm A first: cheapest arm, and it establishes the baseline (BENCHMARKING.md §0.4).
    arms = sorted({a.upper() for a in args.arms}, key=lambda a: {"A": 0, "B": 1, "C": 2}[a])
    total = len(tasks) * len(arms) * len(args.seeds)

    if args.dry_run:
        print(f"bench dir:  {bench_dir}")
        print(f"python:     {resolve_bench_python(bench_dir, args.bench_python)}")
        print(f"backend:    {args.backend}  model: {args.model}")
        print(f"arms:       {arms}   seeds: {args.seeds}")
        budget_txt = ("no token ceiling" if args.budget_tokens is None
                      else f"{args.budget_tokens} tokens/task")
        rounds_txt = ("default rounds" if args.max_rounds is None
                      else f"{args.max_rounds} rounds")
        print(f"budget:     {budget_txt}, {rounds_txt}, "
              f"{args.harness_timeout_sec}s wall-clock cap/task")
        print(f"tasks ({len(tasks)}):")
        for t in tasks:
            print(f"  {t['task_id']:<42} {t['class']}")
        if args.budget_tokens is None:
            print(f"\n{total} runs total -- spend is not capped. Each run is "
                  f"bounded only by the round limit and the "
                  f"{args.harness_timeout_sec}s per-task wall-clock cap.")
        else:
            print(f"\n{total} runs total "
                  f"(worst case {total * args.budget_tokens / 1e6:.1f}M tokens)")
        return 0

    bench_python = resolve_bench_python(bench_dir, args.bench_python)
    results_dir = Path(args.results_dir).expanduser().resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    extra_env: dict[str, str] = {}
    if args.flags:
        extra_env.update(parse_flags(args.flags))
    if args.skip_process_grade:
        extra_env["HARNESSBENCH_SKIP_PROCESS_GRADE"] = "1"
        extra_env["HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM"] = "1"
    if args.proxy:
        if not args.upstream:
            print("--proxy requires --upstream", file=sys.stderr)
            return 2
        extra_env["KUSU_BENCH_PROXY"] = "1"
        extra_env["KUSU_BENCH_UPSTREAM"] = args.upstream

    records: list[dict[str, Any]] = []
    solved_by_a: dict[str, list[bool]] = {}
    done = 0

    for arm in arms:
        print(f"\n=== arm {arm} ===")
        for task in tasks:
            tid = task["task_id"]
            if arm != "A" and args.drop_solved_by_a:
                flags = solved_by_a.get(tid)
                if flags and all(flags):
                    print(f"  skip {tid}: solved by arm A on every seed")
                    done += len(args.seeds)
                    continue
            for seed in args.seeds:
                done += 1
                out = results_dir / f"harness-bench_{tid}_arm{arm}_seed{seed}.json"
                if args.resume and out.is_file() and not args.dry_run:
                    print(f"  [{done}/{total}] {tid} arm={arm} seed={seed}: skipped (--resume)")
                    continue
                print(f"  [{done}/{total}] {tid} arm={arm} seed={seed} ...", flush=True)
                rec = run_one(
                    bench_dir=bench_dir, bench_python=bench_python, task=task,
                    arm=arm, seed=seed, backend=args.backend, model=args.model,
                    budget_tokens=args.budget_tokens, max_rounds=args.max_rounds,
                    tier=args.tier, extra_env=extra_env, verbose=args.verbose,
                    max_parallel=args.max_parallel,
                )
                records.append(rec)
                if arm == "A":
                    solved_by_a.setdefault(tid, []).append(bool(rec["resolved"]))
                out.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                with open(results_dir / "records.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                status = "resolved" if rec["resolved"] else (rec.get("halt_reason") or "unresolved")
                print(f"      score={rec['score']:.3f} tokens={_total_tokens(rec)} "
                      f"{rec['wall_clock_s']}s -- {str(status)[:120]}")

    # PLAN-BENCH-INTEGRITY.md §4.1: Rebuild record list from disk before aggregating
    # PLAN-SWEEP-REPAIR.md §C1: same shape as the longgen script — records.jsonl
    # and the per-cell JSON files are append-only across sweeps, so filter to
    # this invocation's selected tasks; rows for unselected tasks were never
    # part of this matrix and would otherwise contaminate the summary.
    disk_records: list[dict[str, Any]] = []
    for f in sorted(results_dir.glob("harness-bench_*.json")):
        try:
            disk_records.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    selected_task_ids = {t.get("task_id") for t in tasks}
    all_records = [
        r for r in dedupe_records(read_records_jsonl(results_dir / "records.jsonl") + disk_records + records)
        if r.get("task_id") in selected_task_ids
    ]
    summary = summarize(all_records)
    (results_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n=== summary ===")
    for arm, agg in summary["arms"].items():
        print(f"  arm {arm}: mean={agg['mean_score']:.3f} +/-{agg['score_stdev']:.3f}  "
              f"resolved {agg['resolved']}/{agg['runs']}  "
              f"tokens/run={agg['mean_tokens_per_run']}  halts={agg['halts']}")
    print(f"\n{len(all_records)} records -> {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
