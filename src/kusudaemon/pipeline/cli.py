"""Command handlers for the ``kusudaemon pipeline`` group (PLAN.md §11's
control surface: run / status / approve / amend / resume / serve).

The run command drives :class:`RecursiveDriver` in the foreground by
default, or detaches a background subprocess for pure ``status`` /
``approve`` / ``serve``-attach workflows. Approve and amend operate purely
on the run directory's ``approvals.jsonl`` and ``contract.md`` — which is
exactly what makes them safe to run from a second terminal while the
driver (or the dashboard server) is still attached to the same run (§11:
the view "can be attached from anywhere").
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..environment.local import LocalEnvironment
from ..v0.events import EventLog
from ..v0.run_dir import write_text_atomic
from ..roles.factory import make_role_provider
from ..v1.tree import TaskTree
from . import approvals as approval_store
from .backends import build_writer_adapter
from .driver import RunOptions, amend_contract_and_estimate, apply_triage, run_amendment_revalidation
from .liveness import run_liveness
from .run_dir import (
    contract_path,
    events_path,
    phase_path,
    read_source_file,
    resolve_runs_root,
    run_spec_path,
    source_path,
    tree_path,
)

_RUNS_ROOT_DEFAULT = "~/.kusudaemon/runs"


def build_pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kusudaemon pipeline",
        description="PLAN.md §11 control surface for the recursive decomposition harness.",
    )
    sub = parser.add_subparsers(dest="pipeline_command")

    run_parser = sub.add_parser("run", help="Run (or resume) the pipeline.")
    run_parser.add_argument(
        "--runs-root",
        default=None,
        help=f"Defaults to {_RUNS_ROOT_DEFAULT} (harness-owned state, never inside the workspace).",
    )
    run_parser.add_argument("--run-id", default=None, help="Reuse an id to resume an existing run dir.")
    run_parser.add_argument("--goal", default="", help="Task goal (or @path).")
    run_parser.add_argument("--source", default="", help="Source document: text, @file, or -.")
    run_parser.add_argument(
        "--workspace",
        default=None,
        help="Path to a real directory the Writer edits directly "
        "(PLAN.md §A3 kind=\"workspace\") instead of a materialized corpus.",
    )
    run_parser.add_argument(
        "--backend", default="gptme", choices=("gptme", "claude", "codex", "opencode", "antigravity", "agy")
    )
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--provider", default=None)
    run_parser.add_argument("--compile-command", default=None)
    run_parser.add_argument("--research-plan", default=None)
    run_parser.add_argument("--max-rounds", type=int, default=100)
    run_parser.add_argument("--max-attempts", type=int, default=3)
    run_parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Writer episodes dispatched concurrently per round (PLAN.md §C2); "
        "1 reproduces today's serial behavior exactly.",
    )
    run_parser.add_argument(
        "--dispatch-policy",
        choices=("model", "document_order"),
        default="model",
        help="document_order skips the per-round orchestrator LLM call and "
        "dispatches the earliest ready node in document order (PLAN-zeromem.md §1)",
    )
    run_parser.add_argument(
        "--document-review",
        action="store_true",
        help="Run PLAN-zeromem.md §8 document-level review passes after "
        "assembly; surface triage counts for approval before any repair "
        "dispatches.",
    )
    run_parser.add_argument(
        "--survey-mode",
        choices=("auto", "deterministic", "structural", "model"),
        default="auto",
        help="Survey boundary detection strategy. 'auto' uses zero-token structural "
        "survey for large corpora and model survey for small inputs; "
        "'deterministic'/'structural' uses zero-token heading/page-break splitting; "
        "'model' uses windowed LLM boundary voting.",
    )
    run_parser.add_argument(
        "--inline-spans",
        action="store_true",
        help="Inline top-k retrieved spans from the node's own spine slice "
        "into its prompt instead of bare input paths (PLAN-zeromem.md §4).",
    )
    run_parser.add_argument(
        "--tier",
        choices=("T0", "T1", "T2", "T3"),
        default=None,
        help="Force a tier floor, never a ceiling (PLAN.md §A4.4 invariant "
        "9): the classifier still runs; the effective tier is "
        "max(measured, this).",
    )
    run_parser.add_argument(
        "--no-intake",
        action="store_true",
        help="Skip intake entirely; with it set, classify also skips its "
        "estimate call when signals already force >= T2 (A5-1).",
    )
    run_parser.add_argument(
        "--disable-review",
        "--no-review",
        dest="disable_review",
        action="store_true",
        help="Disable review agents across all leaves to save tokens (gates still run).",
    )
    run_parser.add_argument(
        "--budget-tokens",
        type=int,
        default=None,
        help="Token budget ceiling for the run.",
    )
    run_parser.add_argument("--detach", action="store_true", help="Run in a background subprocess and return immediately.")

    resume_parser = sub.add_parser("resume", help="Resume a run after a halt or crash.")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    resume_parser.add_argument("--replan-under-budget", action="store_true", help="Replan remaining tasks under a tighter budget when resuming.")

    status_parser = sub.add_parser("status", help="Print phase, tree, and pending approvals.")
    status_parser.add_argument("run_id")
    status_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    status_parser.add_argument("--brief", action="store_true", help="Print the operator resumption brief (§M7).")

    resync_parser = sub.add_parser("resync", help="Re-sync a run with changed source content (§M8).")
    resync_parser.add_argument("run_id")
    resync_parser.add_argument("--source", required=True, help="Path to new source document.")
    resync_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    cache_parser = sub.add_parser("cache", help="Manage episode cache (§M3).")
    cache_sub = cache_parser.add_subparsers(dest="cache_command")
    cache_clear = cache_sub.add_parser("clear", help="Clear all entries in the episode cache.")
    cache_clear.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    eval_parser = sub.add_parser("eval", help="Run the eval suite (§N5).")
    eval_parser.add_argument("--task", default=None, help="Specific task ID to run.")
    eval_parser.add_argument("--runs", type=int, default=1, help="Number of repetitions per task.")

    approve_parser = sub.add_parser("approve", help="Resolve the oldest pending approval.")
    approve_parser.add_argument("run_id")
    approve_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    approve_parser.add_argument("--answer", default="", help="Free-form answer / edited artifact text.")
    approve_parser.add_argument("--file", default=None, help="Read the answer (e.g. a pilot edit) from a file.")
    approve_parser.add_argument("--action", default="answer", help="Option value for option-based approvals.")

    amend_parser = sub.add_parser("amend", help="Amend the contract and run the re-validation pass.")
    amend_parser.add_argument("run_id")
    amend_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    amend_parser.add_argument("--text", required=True, help="New contract rule text.")
    amend_parser.add_argument("--reason", default="", help="Why the rule was added.")
    amend_parser.add_argument("--yes", action="store_true", help="Apply the triage without prompting.")
    amend_parser.add_argument("--max-attempts", type=int, default=3)

    escalate_parser = sub.add_parser(
        "escalate", help="Promote a run's tier by one (PLAN.md §A4.4 operator intervention)."
    )
    escalate_parser.add_argument("run_id")
    escalate_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    serve_parser = sub.add_parser("serve", help="Serve the web dashboard (PLAN.md §11 control surface) over a runs directory.")
    serve_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--run-id", default=None, help="Attach to this run on startup.")
    serve_parser.add_argument("--no-control", action="store_true", help="Read-only view: disable start/attach/halt/approve/amend/reopen/interject.")
    serve_parser.add_argument("--auth-token", default=None, help="PLAN.md §C4: token required for dashboard access when bound to a non-loopback host. Issued as a cookie at /api/attach; sent as Bearer by API clients.")
    serve_parser.add_argument("--max-concurrent-runs", type=int, default=None, help="PLAN.md §C4: cap on concurrently-hosted dashboard runs (429 past this).")

    pause_parser = sub.add_parser("pause", help="Pause a run (write halt.flag).")
    pause_parser.add_argument("run_id")
    pause_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    kill_parser = sub.add_parser("kill", help="Kill a run process.")
    kill_parser.add_argument("run_id")
    kill_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    reopen_parser = sub.add_parser("reopen", help="Reopen a node for repair.")
    reopen_parser.add_argument("run_id")
    reopen_parser.add_argument("node_id")
    reopen_parser.add_argument("--defect", default="")
    reopen_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    bypass_parser = sub.add_parser("bypass", help="Cancel or bypass an in-flight process for a node.")
    bypass_parser.add_argument("run_id")
    bypass_parser.add_argument("node_id")
    bypass_parser.add_argument("--process", default="")
    bypass_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    tier_parser = sub.add_parser("tier", help="Set tier override.")
    tier_parser.add_argument("run_id")
    tier_parser.add_argument("tier", choices=("T0", "T1", "T2", "T3"))
    tier_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    model_parser = sub.add_parser("model", help="Set model override.")
    model_parser.add_argument("run_id")
    model_parser.add_argument("model")
    model_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    # 2026-08-13: choices restrict the CLI the same way the dashboard's
    # POST /api/backend does — an invalid backend is a clean argparse
    # exit-2 error here, never a crash later.
    backend_parser = sub.add_parser(
        "backend", help="Set subagent backend override (gptme|claude|codex|opencode|antigravity)."
    )
    backend_parser.add_argument("run_id")
    backend_parser.add_argument("backend", choices=("gptme", "claude", "codex", "opencode", "antigravity", "agy", "none", "default"))
    backend_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)

    bench_parser = sub.add_parser(
        "bench",
        help="Run a task in a benchmark environment (TESTING.md §2: Shape A integration).",
    )
    bench_parser.add_argument("--goal-file", default=None, help="File containing task goal/instruction.")
    bench_parser.add_argument("--goal", default="", help="Task goal/instruction string.")
    bench_parser.add_argument(
        "--workspace", required=True, help="Path to task workspace/container directory."
    )
    bench_parser.add_argument(
        "--backend",
        default="opencode",
        choices=("opencode", "gptme", "claude", "codex", "antigravity", "agy"),
        help="Backend to use (default: opencode).",
    )
    bench_parser.add_argument("--model", default=None, help="Model override.")
    bench_parser.add_argument("--provider", default=None, help="Named provider.")
    bench_parser.add_argument(
        "--tier",
        default="auto",
        choices=("auto", "T0", "T1", "T2", "T3"),
        help="Tier floor or 'auto' (default: auto).",
    )
    bench_parser.add_argument(
        "--budget-tokens", type=int, default=None, help="Token budget ceiling."
    )
    bench_parser.add_argument(
        "--arm",
        default="C",
        choices=("A", "B", "C", "a", "b", "c"),
        help="Benchmark arm: A (bare backend CLI), B (decomposition only), C (full pipeline, default).",
    )
    bench_parser.add_argument(
        "--benchmark", default="benchmark", help="Benchmark name (e.g. harness-bench)."
    )
    bench_parser.add_argument("--task-id", default=None, help="Task ID.")
    bench_parser.add_argument("--seed", type=int, default=1, help="Run seed (default: 1).")
    bench_parser.add_argument(
        "--session-id",
        default=None,
        help="Stable session id across a benchmark task's rounds. Multi-round "
             "benchmarks (HarnessBench) invoke the agent once per round and "
             "expect it to carry conversation state; this is that thread's id.",
    )
    bench_parser.add_argument(
        "--round",
        type=int,
        default=1,
        help="1-based round index within a multi-round benchmark task. Rounds "
             "after the first continue the prior session instead of starting a "
             "new one.",
    )
    bench_parser.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON summary."
    )
    bench_parser.add_argument("--output", default=None, help="File to write JSON summary to.")
    bench_parser.add_argument("--runs-root", default=_RUNS_ROOT_DEFAULT)
    bench_parser.add_argument("--run-id", default=None)
    bench_parser.add_argument(
        "--attended",
        action="store_true",
        help="Leave approvals for a human to answer via the dashboard. Off by "
             "default: a benchmark run has no operator, and the driver's "
             "approval wait has no timeout, so an unanswered intake question "
             "would block until the benchmark's own wall-clock cap killed it.",
    )
    bench_parser.add_argument("--max-rounds", type=int, default=100)
    bench_parser.add_argument("--max-attempts", type=int, default=3)
    return parser


def _run_dir(root: str, run_id: str) -> Path:
    return resolve_runs_root(root) / run_id


def _require_existing_run(root: str, run_id: str) -> Path | None:
    """§D0b: status/approve/amend/resume must error against a run that
    doesn't exist rather than let ``create_run_dir`` conjure an empty one —
    printing the resolved absolute path is the point: every reported
    instance of the "wrong directory" bug was invisible precisely because
    the path was never shown. ``run --run-id`` is exempt; it is allowed to
    create."""
    run_dir = _run_dir(root, run_id)
    if not (run_dir / "events.jsonl").is_file():
        print(f"no run at {run_dir} (missing events.jsonl)", file=sys.stderr)
        return None
    return run_dir


def _attach_cli_logger() -> None:
    logger = logging.getLogger("kusudaemon.pipeline.run")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def cmd_run(argv: argparse.Namespace) -> int:
    if argv.detach:
        return cmd_run_detach(argv)
    _attach_cli_logger()
    from .run import run_from_args

    return run_from_args(_run_argv(argv, run_id=argv.run_id))


def cmd_serve(argv: argparse.Namespace) -> int:
    from ..dashboard.server import DEFAULT_MAX_CONCURRENT_RUNS, run_forever

    run_forever(
        argv.runs_root,
        argv.host,
        argv.port,
        attach_run_id=argv.run_id,
        control_enabled=not argv.no_control,
        auth_token=argv.auth_token or "",
        max_concurrent_runs=(
            argv.max_concurrent_runs
            if argv.max_concurrent_runs is not None
            else DEFAULT_MAX_CONCURRENT_RUNS
        ),
    )
    return 0


def _expand_source_arg(raw: str) -> str:
    """The corpus text behind a --source value: for ``@path``/``-`` read
    the file/stdin; anything else is inline text. Same rules as
    run.py:_read_text_arg (mirrored here because --detach must resolve the
    corpus *before* spawning its own run.py subprocess)."""
    if raw.startswith("@"):
        return read_source_file(raw[1:])
    if raw == "-":
        return sys.stdin.read().strip()
    return raw


def cmd_run_detach(argv: argparse.Namespace) -> int:
    run_id = argv.run_id or _default_run_id()
    workspace = getattr(argv, "workspace", None)
    # Runs live under ~/.kusudaemon/runs regardless of --workspace (same
    # rule as run.py's own run_from_args, kept in sync here because
    # --detach resolves the run dir itself, below, before run.py ever
    # parses its own argv).
    runs_root_arg = argv.runs_root or _RUNS_ROOT_DEFAULT
    # §11.10.8: the corpus must not ride through argv — an inline (non-@file)
    # corpus hits E2BIG well before "corpus-scale". Write it into the run
    # dir once (source.txt is exactly where driver._write_source_and_spec
    # would put it) and pass @path, which run.py then reads instead of argv.
    source_arg = argv.source
    if source_arg and not source_arg.startswith("@"):
        run_dir = _run_dir(runs_root_arg, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = _expand_source_arg(source_arg)
        source_path(run_dir).write_text(resolved, encoding="utf-8")
        source_arg = f"@{source_path(run_dir)}"
    command = [
        sys.executable,
        "-m",
        "kusudaemon.pipeline.run",
        "--runs-root", runs_root_arg,
        "--run-id", run_id,
        "--goal", argv.goal,
        "--source", source_arg,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
        "--dispatch-policy", argv.dispatch_policy,
        "--max-parallel", str(getattr(argv, "max_parallel", 1)),
    ]
    if workspace:
        command += ["--workspace", workspace]
    if getattr(argv, "tier", None):
        command += ["--tier", argv.tier]
    if argv.document_review:
        command += ["--document-review"]
    if argv.survey_mode != "model":
        command += ["--survey-mode", argv.survey_mode]
    if argv.inline_spans:
        command += ["--inline-spans"]
    if getattr(argv, "no_intake", False):
        command += ["--no-intake"]
    if getattr(argv, "disable_review", False):
        command += ["--disable-review"]
    if getattr(argv, "budget_tokens", None) is not None:
        command += ["--budget-tokens", str(argv.budget_tokens)]
    for flag, value in (
        ("--model", argv.model),
        ("--provider", getattr(argv, "provider", None)),
        ("--compile-command", argv.compile_command),
        ("--research-plan", argv.research_plan),
    ):
        if value:
            command += [flag, value]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print(f"detached pipeline run: run_id={run_id}")
    print(f"watch with: kusudaemon pipeline status {run_id} --runs-root {runs_root_arg}")
    return 0


def cmd_status(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    if getattr(argv, "brief", False):
        resumption_file = run_dir / "assembly" / "resumption.md"
        if resumption_file.exists():
            print(resumption_file.read_text(encoding="utf-8").strip())
            return 0
    import json

    phase: dict = {}
    try:
        phase = json.loads(phase_path(run_dir).read_text(encoding="utf-8"))
    except OSError:
        pass
    print(f"run:      {argv.run_id}")
    print(f"dir:      {run_dir}")
    print(f"phase:    {phase.get('phase', '-')} ({phase.get('status', '-')})")
    if phase.get("detail"):
        print(f"detail:   {phase['detail']}")
    liveness = run_liveness(run_dir)
    if liveness.stalled:
        print(f"STALLED:  {liveness.reason}")
    tree = TaskTree.load(tree_path(run_dir)) if tree_path(run_dir).exists() else None
    if tree is not None:
        counts: dict[str, int] = {}
        for node in tree.nodes.values():
            counts[node.status] = counts.get(node.status, 0) + 1
        print(f"tree:     {len(tree.nodes)} nodes; statuses={counts}")
    pending = approval_store.pending(run_dir)
    print(f"approvals: {len(pending)} pending")
    for item in pending[:10]:
        print(f"  - {item.kind} ({item.approval_id}): {item.title}")
    if contract_path(run_dir).exists():
        print(f"contract: {len(contract_path(run_dir).read_text(encoding='utf-8').splitlines())} lines")
    events = EventLog(events_path(run_dir))
    print(f"events:   {len(events.read_all())} recorded")
    from ..v0.cost import CostLedger
    from ..v0.run_dir import cost_path
    cost_file = cost_path(run_dir)
    if cost_file.exists():
        totals = CostLedger(cost_file).totals()
        print(f"cost:     ${totals['cost_usd']:.4f} ({totals['total_tokens']:,} tokens)")
    return 0


def cmd_resync(argv: argparse.Namespace) -> int:
    """PLAN-EFFICIENCY-AND-HORIZON.md §M8: Re-sync run with changed source."""
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    new_source_path = Path(argv.source)
    if not new_source_path.is_file():
        print(f"source file not found: {new_source_path}", file=sys.stderr)
        return 1
    new_source_text = new_source_path.read_text(encoding="utf-8")

    spine_file = run_dir / "spine.json"
    old_spine = []
    if spine_file.exists():
        from ..v2.survey import SpineUnit
        old_spine = [SpineUnit.from_dict(d) for d in json.loads(spine_file.read_text(encoding="utf-8"))]

    from ..v2.survey import survey_chunks, save_spine
    new_units = survey_chunks(new_source_text)

    old_by_id = {u.id: u for u in old_spine}
    changed_unit_ids = set()
    for u in new_units:
        if u.id not in old_by_id or old_by_id[u.id].tokens != u.tokens or old_by_id[u.id].label != u.label:
            changed_unit_ids.add(u.id)

    t_path = tree_path(run_dir)
    stale_nodes: set[str] = set()
    if t_path.exists():
        tree = TaskTree.load(t_path)
        for node in tree.nodes.values():
            if any(any(uid in inp for uid in changed_unit_ids) for inp in node.inputs):
                stale_nodes.add(node.id)

        changed = True
        while changed:
            changed = False
            for node in tree.nodes.values():
                if node.id not in stale_nodes:
                    if any(dep in stale_nodes for dep in node.depends_on):
                        stale_nodes.add(node.id)
                        changed = True

        for nid in stale_nodes:
            tree.nodes[nid].status = "stale"
        tree.save(t_path)
        print(f"resynced run {argv.run_id}: {len(stale_nodes)} nodes marked stale ({', '.join(sorted(stale_nodes)) if stale_nodes else 'none'})")

    write_text_atomic(source_path(run_dir), new_source_text)
    save_spine(new_units, spine_file)
    events = EventLog(events_path(run_dir))
    events.append({
        "node_id": "-",
        "role": "harness",
        "type": "run_resynced",
        "changed_units": list(sorted(changed_unit_ids)),
        "stale_nodes": list(sorted(stale_nodes)),
        "ts": time.time(),
    })
    return 0


def cmd_cache(argv: argparse.Namespace) -> int:
    """PLAN-EFFICIENCY-AND-HORIZON.md §M3: Manage episode cache."""
    from ..v0.episode_cache import EpisodeCache
    cache_dir = resolve_runs_root(argv.runs_root) / "cache"
    cache = EpisodeCache(cache_dir)
    if getattr(argv, "cache_command", None) == "clear":
        count = cache.clear()
        print(f"cleared {count} entries from {cache_dir}")
        return 0
    print("unknown cache command", file=sys.stderr)
    return 1


def cmd_eval(argv: argparse.Namespace) -> int:
    from ..eval.runner import run_eval
    report = run_eval(task_id=argv.task, runs=argv.runs)
    report.print_summary()
    return 0


def cmd_approve(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    pending = approval_store.pending(run_dir)
    if not pending:
        print("no pending approvals", file=sys.stderr)
        return 1
    target = pending[0]
    answer = Path(argv.file).read_text(encoding="utf-8") if argv.file else argv.answer
    approval_store.append(run_dir, target.resolve(action=argv.action, user_input=answer))
    print(f"resolved {target.kind} approval {target.approval_id}")
    return 0


def cmd_amend(argv: argparse.Namespace) -> int:
    # §11.10.4: the estimate is shown BEFORE the Reviewer pass runs — the
    # §10 approval was theater when the tokens were already spent.
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    options = _load_options(run_dir)
    provider = make_role_provider(options=options, run_dir=run_dir)
    phase1 = amend_contract_and_estimate(
        run_dir, rule_text=argv.text, reason=argv.reason
    )
    estimate = phase1["estimate"]
    print(
        f"amended contract; revalidation would review "
        f"{estimate.get('nodes', 0)} nodes (~{estimate.get('tokens', 0)} tokens, "
        f"{estimate.get('skipped', 0)} pre-filtered)"
    )
    if not argv.yes:
        try:
            choice = input("run the revalidation review? [y/N] ").strip().lower()
        except EOFError:
            choice = "n"
    else:
        choice = "y"
    if choice not in ("y", "yes"):
        print("not running revalidation; the amended contract stands")
        return 0
    phase2 = run_amendment_revalidation(
        run_dir,
        contract_text=phase1["contract"],
        rule_text=argv.text,
        provider=provider,
    )
    counts = phase2["counts"]
    print(
        f"revalidation: clean={counts['clean']} patchable={counts['patchable']} "
        f"regenerate={counts['regenerate']}"
    )
    if not argv.yes:
        try:
            choice = input("apply repairs for non-clean nodes? [y/N] ").strip().lower()
        except EOFError:
            choice = "n"
    else:
        choice = "y"
    if choice not in ("y", "yes"):
        print("not applying; the triage stays recorded under audit/revalidation/")
        return 0
    env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
    repaired = asyncio.run(
        apply_triage(
            run_dir,
            triage=phase2["triage"],
            writer_adapter_factory=_writer_factory(options, run_dir),
            env=env,
            provider=provider,
            max_attempts=argv.max_attempts,
        )
    )
    print(f"repaired: {', '.join(repaired) if repaired else '(none)'}")
    return 0


def cmd_escalate(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    from .driver import escalate_run

    result = escalate_run(run_dir)
    print(f"tier: {result['from']} -> {result['to']}")
    return 0


def cmd_resume(argv: argparse.Namespace) -> int:
    # Resume is exactly "run with an existing run-id": the driver converges
    # from durable state (§10), so there is no separate resume code path —
    # argv contributes nothing but the run id. But unlike `run --run-id`,
    # resume must never *create* a run: an id typo or a stale --runs-root
    # would otherwise silently start a fresh, empty run (§D0b).
    if _require_existing_run(argv.runs_root, argv.run_id) is None:
        return 1
    _attach_cli_logger()
    from .run import run_from_args

    return run_from_args(["--runs-root", argv.runs_root, "--run-id", argv.run_id])


def cmd_pause(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    halt_path(run_dir).touch()
    print(f"paused run {argv.run_id} (wrote halt.flag)")
    return 0


def cmd_kill(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    halt_path(run_dir).touch()
    pid_file = run_dir / "driver.pid.json"
    if not pid_file.is_file():
        print(f"no driver PID found for run {argv.run_id}", file=sys.stderr)
        return 1
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        import os, signal
        os.kill(pid, signal.SIGTERM)
        print(f"killed process {pid} for run {argv.run_id}")
        return 0
    except Exception as exc:
        print(f"failed to kill process: {exc}", file=sys.stderr)
        return 1


def cmd_reopen(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    tree_file = tree_path(run_dir)
    if not tree_file.is_file():
        print(f"no tree.json at {tree_file}", file=sys.stderr)
        return 1
    tree = TaskTree.load(tree_file)
    node = tree.nodes.get(argv.node_id)
    if node is None:
        print(f"no node {argv.node_id} in tree", file=sys.stderr)
        return 1
    node.status = "pending"
    node.last_defect = argv.defect or ""
    tree.save(tree_file)
    print(f"reopened node {argv.node_id} in run {argv.run_id}")
    return 0


def cmd_bypass(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    from .bypass import set_node_bypass

    set_node_bypass(run_dir, argv.node_id, getattr(argv, "process", "") or "")
    print(f"bypassed process for node {argv.node_id} in run {argv.run_id}")
    return 0


def cmd_tier(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    t_file = tier_path(run_dir)
    rec = json.loads(t_file.read_text(encoding="utf-8")) if t_file.is_file() else {}
    rec["override"] = argv.tier.upper()
    write_text_atomic(t_file, json.dumps(rec, indent=2) + "\n")
    print(f"set tier override for run {argv.run_id} to {argv.tier.upper()}")
    return 0


def cmd_model(argv: argparse.Namespace) -> int:
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    ov_file = run_dir / "model_override.json"
    if argv.model.lower() in ("none", "default"):
        try:
            ov_file.unlink()
        except OSError:
            pass
        print(f"cleared model override for run {argv.run_id}")
    else:
        write_text_atomic(ov_file, json.dumps({"model": argv.model.strip(), "ts": time.time()}, indent=2) + "\n")
        print(f"set model override for run {argv.run_id} to {argv.model}")
    return 0


def cmd_backend(argv: argparse.Namespace) -> int:
    """§2026-08-13: CLI twin of the dashboard's backend override — writes
    backend_override.json, read at every dispatch by driver._current_backend().
    Invalid values can't reach this function (argparse choices); "none"/
    "default" clear the override back to run.spec.json's backend."""
    run_dir = _require_existing_run(argv.runs_root, argv.run_id)
    if run_dir is None:
        return 1
    ov_file = run_dir / "backend_override.json"
    if argv.backend.lower() in ("none", "default"):
        try:
            ov_file.unlink()
        except OSError:
            pass
        print(f"cleared backend override for run {argv.run_id}")
    else:
        write_text_atomic(ov_file, json.dumps({"backend": argv.backend.strip(), "ts": time.time()}, indent=2) + "\n")
        print(f"set backend override for run {argv.run_id} to {argv.backend}")
    return 0


def cmd_bench(
    argv: argparse.Namespace,
    *,
    driver_factory: Any = None,
    subprocess_runner: Any = None,
) -> int:
    """TESTING.md §2 Shape A benchmark integration entry point.

    Runs a task under a benchmark harness across three arms:
      - Arm A: bare backend CLI alone (opencode run, etc.)
      - Arm B: kusudaemon with review agents disabled (--disable-review)
      - Arm C: full kusudaemon pipeline

    Exits non-zero on halt/failure and emits a machine-readable summary
    per TESTING.md §5.
    """
    goal = ""
    if getattr(argv, "goal_file", None):
        gf = Path(argv.goal_file).expanduser().resolve()
        if not gf.is_file():
            print(f"goal file not found: {gf}", file=sys.stderr)
            return 2
        goal = gf.read_text(encoding="utf-8").strip()
    elif getattr(argv, "goal", ""):
        goal = str(argv.goal).strip()

    if not goal:
        print("either --goal-file or --goal is required for bench", file=sys.stderr)
        return 2

    ws_path = Path(argv.workspace).expanduser().resolve()
    if not ws_path.is_dir():
        print(f"workspace directory not found: {ws_path}", file=sys.stderr)
        return 2

    task_id = getattr(argv, "task_id", None) or ws_path.name
    benchmark = getattr(argv, "benchmark", None) or "benchmark"
    arm = str(getattr(argv, "arm", "C") or "C").upper()
    seed = int(getattr(argv, "seed", 1))
    backend = str(getattr(argv, "backend", "opencode") or "opencode").lower()
    runs_root_arg = getattr(argv, "runs_root", None) or _RUNS_ROOT_DEFAULT

    model = getattr(argv, "model", None)
    if not model:
        try:
            from ..provider_config import read_backend_config
            b_cfg = read_backend_config(backend)
            model = b_cfg.model
        except Exception:
            model = None
    if not model and backend == "opencode":
        model = "opencode/nemotron-3.5-lightning-free"

    commit = "unknown"
    try:
        repo_dir = Path(__file__).resolve().parents[3]
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        pass

    runner = subprocess_runner or subprocess.run

    session_id = getattr(argv, "session_id", None)
    round_index = int(getattr(argv, "round", 1) or 1)
    resuming = round_index > 1

    if arm == "A":
        # Multi-round benchmark tasks invoke the agent once per round and expect
        # it to carry its own conversation across them -- some tasks (e.g.
        # HarnessBench 007-session-memory) exist purely to test that. So rounds
        # after the first must continue the prior session rather than start a
        # fresh one, using each CLI's own resume mechanism.
        cmd: list[str]
        if backend == "opencode":
            cmd = ["opencode", "run"]
            if model:
                cmd.extend(["--model", model])
            if resuming:
                # --session takes an id but is undocumented as to whether it
                # creates one; --continue resumes the last session for this
                # directory, and each task has its own workspace, so it is the
                # safe default. KUSUDAEMON_BENCH_SESSION_FLAG=session opts into
                # the explicit-id form once verified against your CLI version.
                if session_id and os.getenv("KUSUDAEMON_BENCH_SESSION_FLAG") == "session":
                    cmd.extend(["--session", session_id])
                else:
                    cmd.append("--continue")
            cmd.extend(["--auto", goal])
        elif backend == "gptme":
            cmd = ["gptme", "--non-interactive"]
            if model:
                cmd.extend(["--model", model])
            if resuming:
                cmd.append("--resume")
            cmd.append(goal)
        elif backend == "claude":
            cmd = ["claude", "-p", goal]
            if model:
                cmd.extend(["--model", model])
            if resuming:
                cmd.append("--continue")
        elif backend == "codex":
            cmd = ["codex", "exec"]
            if resuming:
                cmd.append("resume")
                cmd.append("--last")
            if model:
                cmd.extend(["--model", model])
            cmd.append(goal)
        elif backend in ("antigravity", "agy"):
            cmd = ["agy", "run"]
            if model:
                cmd.extend(["--model", model])
            cmd.append(goal)
        else:
            cmd = [backend, goal]

        t0 = time.time()
        try:
            res = runner(
                cmd,
                cwd=str(ws_path),
                capture_output=True,
                text=True,
            )
            exit_code = res.returncode
            halt_reason = None if exit_code == 0 else f"bare process exited with code {exit_code}: {res.stderr[:200]}"
            resolved = (exit_code == 0)
        except Exception as exc:
            exit_code = 1
            halt_reason = str(exc)
            resolved = False
        t1 = time.time()
        wall_clock_s = round(t1 - t0, 3)

        record = {
            "benchmark": benchmark,
            "task_id": task_id,
            "arm": "A",
            "seed": seed,
            "session_id": session_id,
            "round": round_index,
            "model": model,
            "backend": backend,
            "score": 1.0 if resolved else 0.0,
            "resolved": resolved,
            "tier_measured": None,
            "tier_final": None,
            "escalations": [],
            "calls_by_role": {"bare": 1},
            "tokens_by_role": {},
            "wall_clock_s": wall_clock_s,
            "halt_reason": halt_reason,
            "commit": commit,
        }
    else:
        from ..v6.work_object import measure_workspace
        from ..pipeline.driver import RunOptions, RecursiveDriver
        from ..roles.factory import make_role_provider
        from ..v1.provider import OpenAICompatibleProvider
        from .run import _log_rate_limit_backoff_for
        from ..v0.cost import CostLedger
        from ..v0.run_dir import cost_path
        from ..eval.measure import escalation_events
        from .approvals import Approver
        from .run_dir import halt_path, tier_path

        work_obj = measure_workspace(str(ws_path))
        run_id = getattr(argv, "run_id", None) or f"bench_{benchmark}_{task_id}_arm{arm}_s{seed}_{int(time.time())}"
        run_dir = resolve_runs_root(runs_root_arg) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        raw_tier = getattr(argv, "tier", "auto")
        tier_override = None if (raw_tier == "auto" or not raw_tier) else raw_tier
        disable_review = (arm == "B")

        options = RunOptions(
            goal=goal,
            backend=backend,
            model=model,
            provider=getattr(argv, "provider", None),
            work_object=work_obj,
            workspace_root=str(ws_path),
            max_rounds=getattr(argv, "max_rounds", 100),
            max_attempts=getattr(argv, "max_attempts", 3),
            tier_override=tier_override,
            max_total_tokens=getattr(argv, "budget_tokens", None),
            disable_review=disable_review,
        )

        env = LocalEnvironment(tmp_dir=str(run_dir / "tmp"))
        provider = make_role_provider(
            options=options,
            run_dir=run_dir,
            env=env,
            on_backoff=_log_rate_limit_backoff_for(run_dir),
            provider_cls=OpenAICompatibleProvider,
        )
        if driver_factory is not None:
            driver = driver_factory(run_dir, provider=provider, options=options, env=env)
        else:
            driver = RecursiveDriver(run_dir, provider=provider, options=options, env=env)

        # A benchmark run is unattended by construction, and three phases block
        # on an operator approval that `wait_for_resolution` waits on forever by
        # design ("the operator is the one control surface that must never be
        # rushed"): intake questions (T1+), pilot artifact sign-off (T3), and
        # document-review triage (T2+). With no surface attached, the first of
        # those hangs until the benchmark's wall-clock cap kills the process --
        # no graded result, no halt reason, 40 minutes gone. Every hermetic test
        # that drives the pipeline end to end already wraps it in this same
        # Approver; only the headless production paths were missing it.
        #
        # Resolving blank is the documented silent-operator path at all three
        # sites: intake ends after round 1 and records its default assumptions
        # into spec.md, pilot approves the artifact as-is, document review
        # proceeds with repairs. Nothing is answered *for* the model -- the
        # assumptions are written down and auditable in the run directory.
        attended = bool(getattr(argv, "attended", False))
        t0 = time.time()
        report = None
        halt_reason = None
        try:
            if attended:
                report = asyncio.run(driver.run())
            else:
                with Approver(run_dir):
                    report = asyncio.run(driver.run())
        except Exception as exc:
            halt_reason = str(exc)
        t1 = time.time()
        wall_clock_s = round(t1 - t0, 3)

        h_file = halt_path(run_dir)
        if h_file.is_file():
            flag_text = h_file.read_text(encoding="utf-8").strip()
            if flag_text:
                halt_reason = flag_text

        if report is not None and report.status != "done" and not halt_reason:
            # A non-"done" report is a recorded outcome, not a silent failure
            # (TESTING.md §4). "halted" keeps its bare detail; every other
            # terminal status (notably "error": provider down, auth refused,
            # rate limit) carries the phase so a sweep is diagnosable after
            # the fact instead of showing up as resolved=false, reason=null.
            if report.status == "halted":
                halt_reason = report.detail or "halted"
            else:
                phase = getattr(report, "phase", None) or "?"
                halt_reason = f"{report.status} in {phase}: {report.detail or 'no detail'}"

        resolved = (report is not None and report.status == "done" and not halt_reason)
        score = 1.0 if resolved else 0.0
        exit_code = 0 if resolved else 1

        tier_measured = None
        tier_final = None
        t_file = tier_path(run_dir)
        if t_file.is_file():
            try:
                t_data = json.loads(t_file.read_text(encoding="utf-8"))
                tier_measured = t_data.get("measured")
                tier_final = t_data.get("final") or t_data.get("effective") or tier_measured
            except Exception:
                pass

        escalations = escalation_events(run_dir)

        c_file = cost_path(run_dir)
        calls_by_role: dict[str, int] = {}
        tokens_by_role: dict[str, int] = {}
        if c_file.is_file():
            records = CostLedger(c_file).read_all()
            for r in records:
                role = str(r.get("role") or "unknown")
                calls_by_role[role] = calls_by_role.get(role, 0) + 1
                t = int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0) + int(r.get("reasoning_tokens") or 0)
                tokens_by_role[role] = tokens_by_role.get(role, 0) + t

        record = {
            "benchmark": benchmark,
            "task_id": task_id,
            "arm": arm,
            "seed": seed,
            "model": model,
            "backend": backend,
            "score": score,
            "resolved": resolved,
            "tier_measured": tier_measured,
            "tier_final": tier_final,
            "escalations": escalations,
            "calls_by_role": calls_by_role,
            "tokens_by_role": tokens_by_role,
            "wall_clock_s": wall_clock_s,
            "halt_reason": halt_reason,
            "commit": commit,
            "attended": attended,
        }

    json_str = json.dumps(record, indent=2)
    out_file = getattr(argv, "output", None)
    if out_file:
        out_p = Path(out_file)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json_str + "\n", encoding="utf-8")

    if getattr(argv, "json", False):
        print(json_str)
    else:
        print(f"benchmark:     {benchmark}")
        print(f"task:          {task_id}")
        print(f"arm:           {arm}")
        print(f"seed:          {seed}")
        print(f"model:         {model} ({backend})")
        print(f"resolved:      {resolved}")
        print(f"score:         {score}")
        print(f"tier:          measured={record['tier_measured']} final={record['tier_final']}")
        print(f"wall clock:    {wall_clock_s}s")
        print(f"calls by role: {record['calls_by_role']}")
        print(f"halt reason:   {halt_reason}")

    return exit_code


def dispatch(args: argparse.Namespace) -> int:
    """Route a parsed pipeline group to its handler."""
    command = args.pipeline_command
    if command == "bench":
        return cmd_bench(args)
    if command == "run":
        return cmd_run(args)
    if command == "resume":
        return cmd_resume(args)
    if command == "status":
        return cmd_status(args)
    if command == "resync":
        return cmd_resync(args)
    if command == "cache":
        return cmd_cache(args)
    if command == "eval":
        return cmd_eval(args)
    if command == "approve":
        return cmd_approve(args)
    if command == "amend":
        return cmd_amend(args)
    if command == "escalate":
        return cmd_escalate(args)
    if command == "serve":
        return cmd_serve(args)
    if command == "pause":
        return cmd_pause(args)
    if command == "kill":
        return cmd_kill(args)
    if command == "reopen":
        return cmd_reopen(args)
    if command == "bypass":
        return cmd_bypass(args)
    if command == "tier":
        return cmd_tier(args)
    if command == "model":
        return cmd_model(args)
    if command == "backend":
        return cmd_backend(args)
    raise ValueError(f"unknown pipeline command: {command!r}")


def _load_options(run_dir: Path) -> RunOptions:
    path = run_spec_path(run_dir)
    if not path.exists():
        raise FileNotFoundError(f"no run at {run_dir} (missing run.spec.json)")
    import json

    return RunOptions.from_spec(json.loads(path.read_text(encoding="utf-8")))


def _writer_factory(options: RunOptions, run_dir: Path):
    from .driver import PipelineDriver

    driver = PipelineDriver(run_dir, options=options)
    return driver._default_writer_factory()



def _run_argv(argv: argparse.Namespace, *, run_id: str | None) -> list[str]:
    parts = [
        "--goal", argv.goal,
        "--source", argv.source,
        "--backend", argv.backend,
        "--max-rounds", str(argv.max_rounds),
        "--max-attempts", str(argv.max_attempts),
        "--dispatch-policy", argv.dispatch_policy,
        "--max-parallel", str(getattr(argv, "max_parallel", 1)),
    ]
    if run_id:
        parts += ["--run-id", run_id]
    if getattr(argv, "runs_root", None):
        parts += ["--runs-root", argv.runs_root]
    if getattr(argv, "workspace", None):
        parts += ["--workspace", argv.workspace]
    if getattr(argv, "tier", None):
        parts += ["--tier", argv.tier]
    if getattr(argv, "document_review", False):
        parts += ["--document-review"]
    if getattr(argv, "survey_mode", "model") != "model":
        parts += ["--survey-mode", argv.survey_mode]
    if getattr(argv, "inline_spans", False):
        parts += ["--inline-spans"]
    if getattr(argv, "disable_review", False):
        parts += ["--disable-review"]
    if getattr(argv, "budget_tokens", None) is not None:
        parts += ["--budget-tokens", str(argv.budget_tokens)]
    for flag, value in (
        ("--model", argv.model),
        ("--provider", getattr(argv, "provider", None)),
        ("--compile-command", argv.compile_command),
        ("--research-plan", argv.research_plan),
    ):
        if value:
            parts += [flag, value]
    return parts


def _default_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"