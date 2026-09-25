"""The v1 round loop (PLAN.md §13): Orchestrator/Writer/Reviewer driven one
round at a time, task state kept entirely in ``tree.json``.

Resumability (§10) composes with v0 rather than reimplementing it:

- A node already "passed" is never revisited — ``TaskTree.is_ready`` only
  offers "pending" nodes to the orchestrator.
- A node caught mid-flight by a crash (its ``tree.json`` status is still
  "dispatched" or "awaiting_review" when this process starts) is resumed
  directly, *before* the orchestrator is asked anything this round —
  dispatch decisions are for new work, not for continuing what was already
  underway. The in-flight writer continuation itself is v0's ``run_node``,
  completely unmodified: it replays from ``events.jsonl`` exactly as it
  does for a single-node run.

Invariant enforced here, not by either role (PLAN.md §2 invariant 1): a
node's status only ever becomes "passed" after both its gates (code) and
its reviewer verdict (model, only consulted when the node declares
judgment items) agree.

``_transition_after_writer``/``_transition_after_review`` also record the
located gate or reviewer failure onto ``node.last_defect`` on every failed
attempt, and clear it on success (PLAN-zeromem.md §9) — a retry's prompt
(``pipeline/prompts.py:build_node_prompt``) reads it back, so attempts 2
and 3 carry forward what attempt 1 got wrong instead of resampling an
identical prompt blind.

``dispatch_node``/``review_and_transition_node`` (PLAN.md §B2) are
``run_round_loop``'s per-node dispatch/review bodies, pulled out of their
original inline closures into standalone functions — the *only* change
this made to this module's own behavior is that they're callable directly.
This exists so ``v6/direct.py``'s T0 direct-episode path (one gated
episode, no ``tree.json``) can reuse the exact same Writer-dispatch and
Reviewer-verdict machinery instead of re-implementing it: T0 hands both
functions an in-memory, single-node ``TaskTree`` and a JSON path of its own
choosing (never named ``tree.json`` — PLAN.md §A4.3: "T0 has no
``tree.json``"), and gets the identical gate-cache-write, manifest-line,
and status-transition behavior a real round-loop dispatch gets.

**Runtime split (PLAN.md §A8/§B5) is wired here as two optional, injected
callables — ``split_handler`` and ``on_node_passed`` — never as a direct
import of ``v7/split.py``.** This package's layering is strictly
bottom-up (``v0`` -> ``v1`` -> ``v2`` -> ...; grep any ``v1/*.py`` and none
of them import from ``v2`` or later), and ``v7/split.py`` needs ``TaskTree``/
``TaskNode`` from here plus ``leaf_gate`` from ``v2/planner.py`` — so this
module cannot import it without inverting that rule. Instead
``dispatch_node`` and ``review_and_transition_node`` accept the real
functions as parameters, and ``pipeline/driver.py`` (which already imports
both ``v1`` and ``v7``) is the one place that wires
``v7.split.handle_split_proposal``/``v7.split.maybe_derive_split_parent``
in — and only for the T2/T3 tiers, never for T0/T1 (``v6/direct.py``'s own
calls to these two functions never pass either callable, so its behavior
is byte-for-byte unchanged: PLAN.md §A8.1's mechanism only makes sense
against a real, multi-node-capable ``tree.json``, which T0 has none of and
T1's is a single node the driver builds directly with its own, different,
already-built escalation path — ``v6/direct.py:is_size_defect`` /
``size_defect_retry``). Left ``None`` (every existing caller of these three
functions, and every test of this module before §B5), ``split_handler``/
``on_node_passed`` cost nothing and change nothing: split detection is
purely additive.

``split_handler(run_dir, node, tree, tree_path, log) -> bool`` runs right
after the Writer episode returns, before gate evaluation — a split
proposal is a third kind of episode termination (submit / split / fail,
§A8.1), and gate-evaluating a blank ``out/<node>.md`` from a node that
proposed a split instead of submitting would misread it as a plain
"fail" and burn an attempt PLAN.md explicitly says a rejected proposal
must not cost. Returning ``True`` means a ``split.json`` was present and
fully handled (accepted-and-grafted, or evaluated-and-rejected) — either
way ``dispatch_node`` returns immediately without touching gates, the
manifest, or ``_transition_after_writer``. Returning ``False`` means no
``split.json`` existed at all, and ``dispatch_node`` proceeds exactly as
before this workstream.

``on_node_passed(run_dir, node, tree, tree_path, log) -> None`` runs
inside ``review_and_transition_node`` immediately after a node's status
becomes "passed" — the natural place to ask "did this completion just
finish off a split parent's last outstanding child," mirroring how
dependency-based readiness (``TaskTree.is_ready``) is already re-checked
reactively rather than polled. Re-checking on every passing node (instead
of tracking "is this the last one" explicitly) is deliberately the simple
option: the condition itself (``all(sibling.status == "passed" for
sibling in siblings)``) is cheap and idempotent to recompute, so there is
nothing to get out of sync.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import ensure_leaf_unit_stubs, write_text_atomic
from .gates import (
    GateResult,
    all_passed,
    count_units,
    evaluate_gates,
    first_unit_key,
    unmet,
    write_gate_cache,
)
from .manifest import append_manifest_line
from .orchestrator import (
    DispatchDecision,
    DispatchPolicy,
    decide_next_action_deterministic,
    decide_next_action_with_policy,
)
from ..roles.protocol import RoleProvider
from .reviewer import (
    ReviewVerdict,
    compute_verdict_digest,
    review_node,
    unit_delimiter_from_gates,
)
from .run_dir import (
    audit_path,
    ensure_audit_path,
    ensure_orchestrator_dir,
    events_path,
    manifest_path,
    node_artifact_path,
    orchestrator_dir,
)
from .tree import TaskNode, TaskTree
from .writer import run_writer_node

AdapterFactory = Callable[[TaskNode], AgentAdapter]
PromptBuilder = Callable[[TaskNode], str]
# PLAN.md §A8/§B5 — see module docstring for why these are injected
# callables rather than a direct import of v7/split.py.
SplitHandler = Callable[[Path, TaskNode, TaskTree, Path, EventLog], bool]
NodePassedHook = Callable[[Path, TaskNode, TaskTree, Path, EventLog], None]


def _wants_unit_stubs(node: TaskNode) -> bool:
    """Whether ``node`` gets per-unit stub files (armc-wall-clock-brainstorm
    §A1). Decomposed leaves only: a T1 single node has bash and appends to
    one file (21/21 complete before dcf4874), and ``extract_leaf_unit_range``'s
    budget ``units_expected`` fallback would otherwise stub it too.
    ``KUSUDAEMON_UNIT_STUBS=0`` disables."""
    from ..v6.direct import DIRECT_NODE_ID, SINGLE_NODE_ID

    return (
        os.getenv("KUSUDAEMON_UNIT_STUBS", "1") == "1"
        and node.id not in (SINGLE_NODE_ID, DIRECT_NODE_ID)
    )


async def dispatch_node(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    prompt_for_node: PromptBuilder,
    budget: EpisodeBudget,
    manifest: str | Path,
    max_attempts: int,
    log: EventLog,
    split_handler: SplitHandler | None = None,
    tree_lock: asyncio.Lock | None = None,
    worktree_manager: Any | None = None,
) -> None:
    """One Writer episode + gate evaluation (hard + warn) + manifest line +
    status transition for a single node — ``run_round_loop``'s original
    ``dispatch`` closure, pulled out so ``v6/direct.py``'s T0 path can call
    it directly against its own in-memory single-node ``TaskTree`` (module
    docstring).

    §C1 (warn severity first): ``node.warn_gates`` are evaluated alongside
    ``node.gates`` (same handlers, same single ``evaluate_gates`` call),
    their results are merged into the audit file's gate cache and into the
    manifest's ``warned_gates`` — but they **never participate in
    ``all_passed``** (only ``node.gates`` does), so a failing warn-gate
    cannot block a node from reaching ``"passed"``. The whole "ship at
    warn severity first" semantics live entirely in this separation: no
    new policy knob on the gate handlers themselves, no second pass, no
    separate "send the warning to the writer" plumbing — just a second
    ``list[str]`` evaluated alongside the first, recorded but never gating.
    """
    run_dir = Path(run_dir)
    wt_dir = None
    if worktree_manager is not None:
        try:
            wt_dir = worktree_manager.create_worktree(node.id)
        except Exception:
            wt_dir = None
    try:
        adapter = writer_adapter_factory(node)
        if wt_dir is not None and hasattr(adapter, "workspace_path"):
            adapter.workspace_path = str(wt_dir)
        # A1: Pre-create unit stubs before prompt generation so writer knows
        # exact file targets.
        if _wants_unit_stubs(node):
            ensure_leaf_unit_stubs(run_dir, node)
            if wt_dir is not None:
                ensure_leaf_unit_stubs(wt_dir, node)
        result, promotion = await run_writer_node(
            run_dir, node, prompt_for_node(node), adapter, env, budget
        )
        if split_handler is not None and split_handler(run_dir, node, tree, tree_path, log):
            # A split.json existed and was fully evaluated (accepted-and-grafted
            # or rejected-with-attempt-preserved) — module docstring. Either way
            # this was not a "submit", so gates/manifest/the normal
            # writer-failure transition never apply.
            return

        # PLAN-CONCURRENCY-AND-SHARED-STATE.md §B4: In worktree mode, compute patch and apply sequentially to trunk
        if worktree_manager is not None and result.status == "done":
            patch, modified_files = worktree_manager.compute_patch(node.id)
            applied, conflict_err = worktree_manager.apply_patch_to_trunk(node.id, patch, modified_files)
            if not applied:
                conflict_cnt = worktree_manager.record_conflict(node.id)
                node.last_defect = f"merge_conflict: {conflict_err}"
                log.append(
                    {
                        "node_id": node.id,
                        "role": "harness",
                        "round": 0,
                        "type": "merge_conflict",
                        "conflict_count": conflict_cnt,
                        "detail": conflict_err,
                    }
                )
                node.status = "pending"
                await _save_tree_locked(tree, tree_path, tree_lock)
                return
        artifact_text = _read_artifact(run_dir, node.id)
        gate_results = evaluate_gates(node.gates, artifact_text)
        # §C1: warn-severity gates — evaluated alongside hard gates, recorded
        # alongside them in the audit cache, but a failure does NOT block the
        # node. ``all_passed`` (called below via ``_transition_after_writer``)
        # looks at hard gates only; the warn results are recorded so the
        # dashboard/manifest can surface them as a signal that the semantic
        # bar `problems>=5` etc. is firing without flipping a passing run.
        warn_results = evaluate_gates(list(node.warn_gates), artifact_text)
        audit = ensure_audit_path(run_dir, node.id)
        write_gate_cache(audit, [*gate_results, *warn_results])
        append_manifest_line(
            manifest,
            node_id=node.id,
            artifact_path=str(node_artifact_path(run_dir, node.id)),
            artifact_text=artifact_text,
            gate_results=gate_results,
            promotion=promotion,
            warn_results=warn_results,
        )
        await _transition_after_writer(
            node, tree, tree_path, result.status == "done", gate_results, max_attempts, log,
            tree_lock=tree_lock,
            run_dir=run_dir,
            result_status=result.status,
        )
    finally:
        if worktree_manager is not None and wt_dir is not None:
            worktree_manager.remove_worktree(node.id)


async def review_and_transition_node(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    *,
    provider: RoleProvider,
    max_attempts: int,
    log: EventLog,
    on_node_passed: NodePassedHook | None = None,
    tree_lock: asyncio.Lock | None = None,
    provider_semaphore: asyncio.Semaphore | None = None,
    review_sample_rate: float = 0.0,
    disable_review: bool = False,
    triage_provider: RoleProvider | None = None,
) -> None:
    """One Reviewer verdict + status transition for a single node —
    ``run_round_loop``'s original ``review`` closure, pulled out for the
    same reason as ``dispatch_node`` above.

    §C2: ``provider_semaphore`` bounds concurrent provider calls when
    reviews run gathered in a parallel wave (``run_round_loop(max_parallel
    > 1)``). Today the provider is synchronous urllib, so calls never
    actually overlap on the single event-loop thread — the semaphore is
    the spec'd fence that becomes load-bearing the day the provider goes
    async or an adapter moves calls onto threads.``"""
    run_dir = Path(run_dir)
    artifact_text = _read_artifact(run_dir, node.id)
    contract_text = ""
    try:
        from ..v2.run_dir import contract_path
        cp = contract_path(run_dir)
        if cp.exists():
            contract_text = cp.read_text(encoding="utf-8")
    except Exception:
        contract_text = ""

    verdict_digest = compute_verdict_digest(artifact_text, node.rubric, node.judgment, contract_text, brief=node.brief)
    cached_verdict: ReviewVerdict | None = None
    cached_sections: list[dict[str, Any]] | None = None
    audit_file = audit_path(run_dir, node.id)
    if not disable_review and audit_file.exists():
        try:
            loaded = json.loads(audit_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cached_sections = loaded.get("sections")
                if (
                    loaded.get("verdict_digest") == verdict_digest
                    and loaded.get("verdict") == "pass"
                ):
                    cached_verdict = ReviewVerdict(
                        node_id=node.id,
                        items=list(loaded.get("items", [])),
                        verdict="pass",
                        truncated=bool(loaded.get("truncated", False)),
                        section_verdicts=list(loaded.get("sections", [])),
                    )
                    log.append(
                        {
                            "node_id": node.id,
                            "role": "reviewer",
                            "round": 0,
                            "type": "node_review_cached",
                            "detail": f"verdict_digest {verdict_digest[:12]} matched cached pass",
                        }
                    )
        except Exception:
            cached_verdict = None

    from ..pipeline.bypass import is_node_bypassed
    from ..pipeline.corruption import check_artifact_text_corruption

    bypassed = is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id)

    if disable_review:
        verdict = ReviewVerdict(node_id=node.id, items=[], verdict="pass")
        log.append(
            {
                "node_id": node.id,
                "role": "reviewer",
                "round": 0,
                "type": "node_review_disabled",
                "detail": "review agents disabled; auto-passing review",
            }
        )
    elif bypassed:
        corrupted, reason = check_artifact_text_corruption(artifact_text, node=node)
        if corrupted:
            import warnings
            warnings.warn(
                f"Bypassing review for node {node.id} with empty or corrupted artifact: {reason}",
                UserWarning,
                stacklevel=2,
            )
            log.append(
                {
                    "node_id": node.id,
                    "role": "reviewer",
                    "round": 0,
                    "type": "node_bypass_warning",
                    "detail": f"review bypassed on empty or corrupted artifact: {reason}",
                    "warning": reason,
                    "ts": time.time(),
                }
            )
        verdict = ReviewVerdict(node_id=node.id, items=[], verdict="pass")
        log.append(
            {
                "node_id": node.id,
                "role": "reviewer",
                "round": 0,
                "type": "node_review_bypassed",
                "detail": "review bypassed by operator; auto-passing review",
            }
        )
    elif cached_verdict is not None:
        verdict = cached_verdict
    else:
        # Assemble declared inputs: manifest handoffs, spine structural unit, research findings
        declared_inputs_parts: list[str] = []
        if node.depends_on:
            try:
                from .manifest import read_all_manifest_entries
                m_path = run_dir / "manifest.jsonl"
                if m_path.exists():
                    entries = read_all_manifest_entries(m_path)
                    promotions = {
                        str(e.get("node")): str(e.get("promotion", ""))
                        for e in entries if e.get("node") and e.get("promotion")
                    }
                    for dep in node.depends_on:
                        if dep in promotions:
                            declared_inputs_parts.append(f"Handoff from {dep}: {promotions[dep]}")
            except Exception:
                pass

        spine_file = run_dir / "spine.json"
        if spine_file.exists():
            try:
                spine_data = json.loads(spine_file.read_text(encoding="utf-8"))
                units = spine_data.get("units", [])
                for u in units:
                    if (
                        u.get("id") == getattr(node, "spine_unit", None)
                        or u.get("id") == node.id
                        or u.get("label") == node.brief
                    ):
                        declared_inputs_parts.append(f"Spine unit: {json.dumps(u)}")
                        break
            except Exception:
                pass

        if node.inputs:
            for inp in node.inputs:
                if "research" in str(inp):
                    inp_path = run_dir / inp
                    if inp_path.is_file():
                        try:
                            finding_text = inp_path.read_text(encoding="utf-8")[:2000]
                            declared_inputs_parts.append(f"Research finding ({inp}): {finding_text}")
                        except Exception:
                            pass
        declared_inputs_str = "\n\n".join(declared_inputs_parts)

        def _review_sink(text: str) -> None:
            # PLAN-REVIEW-READ-LOOP.md §R1.1: reviewer/triage reasoning
            # streams live into scratch/<node>/review-trace.jsonl the way
            # phase-classify streams into its trace file.
            if not text:
                return
            try:
                trace_path = run_dir / "scratch" / node.id / "review-trace.jsonl"
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                with trace_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"role": "thinking", "text": text}) + "\n")
            except Exception:
                pass

        async def _do_review() -> ReviewVerdict:
            kwargs: dict[str, Any] = dict(
                contract_text=contract_text,
                declared_inputs=declared_inputs_str,
                brief=node.brief,
                triage_provider=triage_provider,
                cached_sections=cached_sections,
                judgment_classification=getattr(node, "judgment_classification", None),
                on_reasoning=_review_sink,
                on_event=log.append,
                run_dir=run_dir,
            )

            def _call_with_containment() -> ReviewVerdict:
                from .provider import ProviderError, call_scope

                try:
                    with call_scope(role="reviewer", node=node.id):
                        return review_node(node, artifact_text, provider, **kwargs)
                except ProviderError as exc:
                    return ReviewVerdict(
                        node_id=node.id,
                        items=[],
                        verdict="unavailable",
                        skip_reason=f"provider: {exc}",
                    )
                except Exception as exc:
                    return ReviewVerdict(
                        node_id=node.id,
                        items=[],
                        verdict="unavailable",
                        skip_reason=f"provider: {exc}",
                    )

            if provider_semaphore is not None:
                async with provider_semaphore:
                    return await asyncio.to_thread(_call_with_containment)
            return await asyncio.to_thread(_call_with_containment)

        review_task = asyncio.create_task(_do_review())
        while not review_task.done():
            if is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id):
                review_task.cancel()
                bypassed = True
                break
            await asyncio.sleep(0.1)

        if bypassed:
            corrupted, reason = check_artifact_text_corruption(artifact_text, node=node)
            if corrupted:
                import warnings
                warnings.warn(
                    f"Bypassing review for node {node.id} with empty or corrupted artifact: {reason}",
                    UserWarning,
                    stacklevel=2,
                )
                log.append(
                    {
                        "node_id": node.id,
                        "role": "reviewer",
                        "round": 0,
                        "type": "node_bypass_warning",
                        "detail": f"review bypassed on empty or corrupted artifact: {reason}",
                        "warning": reason,
                        "ts": time.time(),
                    }
                )
            verdict = ReviewVerdict(node_id=node.id, items=[], verdict="pass")
            log.append(
                {
                    "node_id": node.id,
                    "role": "reviewer",
                    "round": 0,
                    "type": "node_review_bypassed",
                    "detail": "review bypassed by operator; auto-passing review",
                }
            )
        else:
            verdict = await review_task
            if getattr(verdict, "skip_reason", None):
                log.append(
                    {
                        "node_id": node.id,
                        "role": "reviewer",
                        "round": 0,
                        "type": "review_skipped_no_judgment",
                        "reason": verdict.skip_reason,
                        "detail": f"review skipped with reason: {verdict.skip_reason}",
                        "ts": time.time(),
                    }
                )

    sampled_disagreement = False
    if not disable_review and verdict.verdict == "pass" and review_sample_rate > 0.0 and node.judgment:
        import random
        if random.random() < review_sample_rate:
            def _sampled_call() -> ReviewVerdict:
                from .provider import ProviderError, call_scope

                try:
                    with call_scope(role="reviewer", node=node.id):
                        return review_node(
                            node,
                            artifact_text,
                            provider,
                            contract_text=contract_text,
                            declared_inputs=declared_inputs_str if 'declared_inputs_str' in locals() else "",
                            brief=node.brief,
                            temperature=0.7,
                            on_reasoning=_review_sink,
                            on_event=log.append,
                            run_dir=run_dir,
                        )
                except ProviderError as exc:
                    return ReviewVerdict(node_id=node.id, items=[], verdict="unavailable", skip_reason=f"provider: {exc}")
                except Exception as exc:
                    return ReviewVerdict(node_id=node.id, items=[], verdict="unavailable", skip_reason=f"provider: {exc}")

            if provider_semaphore is not None:
                async with provider_semaphore:
                    sampled = await asyncio.to_thread(_sampled_call)
            else:
                sampled = await asyncio.to_thread(_sampled_call)

            if sampled.verdict not in ("pass", "unavailable"):
                sampled_disagreement = True
                log.append({
                    "node_id": node.id,
                    "role": "reviewer",
                    "round": 0,
                    "type": "reviewer_sampled_disagreement",
                    "sampled_verdict": sampled.verdict,
                    "sampled_items": sampled.items,
                })

    _write_audit(run_dir, node, verdict, artifact_text=artifact_text, contract_text=contract_text, brief=node.brief)
    if sampled_disagreement:
        try:
            audit_data = json.loads(audit_file.read_text(encoding="utf-8"))
            audit_data["sampled_disagreement"] = True
            audit_file.write_text(json.dumps(audit_data, indent=2), encoding="utf-8")
        except Exception:
            pass

    await _transition_after_review(
        node, tree, tree_path, verdict, max_attempts, log, tree_lock=tree_lock, run_dir=run_dir
    )
    if node.status == "passed" and on_node_passed is not None:
        # PLAN.md §A8.3: this node may be the last outstanding child of a
        # split parent — see module docstring for why this is a cheap
        # recheck rather than tracked state.
        on_node_passed(run_dir, node, tree, tree_path, log)


async def run_round_loop(
    run_dir: str | Path,
    tree_path: str | Path,
    *,
    writer_adapter_factory: AdapterFactory,
    env: Environment,
    provider: RoleProvider,
    prompt_for_node: PromptBuilder,
    writer_budget: EpisodeBudget | None = None,
    writer_budget_for: Callable[[TaskNode], EpisodeBudget] | None = None,
    max_rounds: int = 100,
    max_attempts: int = 3,
    dispatch_policy: DispatchPolicy = "deterministic",
    split_handler: SplitHandler | None = None,
    on_node_passed: NodePassedHook | None = None,
    max_parallel: int = 1,
    provider_concurrency: int | None = None,
    should_halt: Callable[[], bool] | None = None,
    reviewer_provider: RoleProvider | None = None,
    review_sample_rate: float = 0.0,
    disable_review: bool = False,
    triage_provider: RoleProvider | None = None,
    admission_controller: Any | None = None,
    worktree_manager: Any | None = None,
) -> TaskTree:
    """Drive the Orchestrator/Writer/Reviewer round loop for ``tree.json``.

    PLAN-AUDIT.md §E15: ``should_halt`` (default ``None``, byte-identical to
    every pre-§E15 caller and test — nothing calls it, this loop never
    halts) lets an operator's Halt control take effect *inside* a long
    ``execute`` phase instead of only at the driver's phase boundaries.
    Checked at three points, none of which interrupt a turn already in
    flight (PLAN.md §10 "never interrupt mid-turn"): (a) before each
    round's orchestrator dispatch decision, (b) before that round's wave is
    marked dispatched and actually sent out, and (c) before each iteration
    of the in-place attempt-retry loop. A hit only ever skips *starting*
    new work — any ``dispatch``/``review`` already awaited runs to
    completion — and the tree is returned exactly as it stood at the halt
    point: nothing is marked failed or blocked on account of halting.

    PLAN.md §C2: ``max_parallel > 1`` runs each round as a **wave** — the
    orchestrator's dispatch decision (one call, exactly as before) names
    the first node, and the wave is filled out to ``max_parallel`` with the
    next ready nodes in tree order, picked by code from the ready set with
    **no additional model calls** (the orchestrator was never the
    bottleneck, and §2 keeps its context bounded by the ready set; writer
    throughput is the thing parallelism buys). The wave's subprocess
    episodes run concurrently via ``asyncio.gather`` in
    ``max_parallel``-sized chunks, then the wave's reviews run the same
    way. ``max_parallel=1`` is the byte-identical event sequence to the
    pre-§C2 loop: chunks of one are sequential awaits in the same order.

    PLAN-SWEEP-REPAIR.md §K1 supersedes the gather for ``max_parallel > 1``:
    ``refill_loop`` runs each node as its own dispatch/review/retry job and
    refills a slot as soon as any job finishes, so a fast failure retries
    immediately and a waiting ready node starts without waiting for the
    slowest episode. The orchestrator is still consulted once per fill and
    the fill still adds ready nodes by code with no extra model calls.

    Concurrency guards (all inert at ``max_parallel=1``):
    - ``EventLog`` serializes appends with its own ``threading.Lock``
    (``v0/events.py``).
    - every ``tree.save`` funnels through one ``asyncio.Lock``
      (``_save_tree_locked``) — the "single-writer discipline".
    - the state mutation + save sequences are synchronous on the single
      event-loop thread (no awaits between them), which is the deeper
      guarantee; the lock is the spec'd fence that holds even if that
      changes.
    - ``provider_concurrency`` (default: unlimited) bounds simultaneous
      provider calls via an ``asyncio.Semaphore`` around review verdicts.
      Inert today — the provider is synchronous urllib, so calls cannot
      overlap on one thread — and load-bearing only once the provider
      goes async or calls move onto threads.
    - an assertion that no two in-flight (wave) nodes share an artifact
      path, so a future @D0 regression (``artifact != out/<id>.md``)
      cannot silently clobber one artifact with another's write.
    """
    tree = TaskTree.load(tree_path)
    log = EventLog(events_path(run_dir))
    manifest = manifest_path(run_dir)
    from ..pipeline.bypass import is_node_bypassed
    from ..pipeline.admission import get_global_admission_controller
    admission = (
        admission_controller
        if admission_controller is not None
        else get_global_admission_controller(provider_concurrency or 4)
    )
    default_budget = writer_budget or EpisodeBudget()
    tree_lock = asyncio.Lock()
    provider_sem = (
        asyncio.Semaphore(provider_concurrency)
        if provider_concurrency is not None and provider_concurrency > 0
        else None
    )

    def chunks(nodes: list[TaskNode]) -> list[list[TaskNode]]:
        return [nodes[i : i + max_parallel] for i in range(0, len(nodes), max_parallel)]

    async def dispatch(node: TaskNode) -> None:
        # §B7.4: Stagger wave starts with jitter to prevent thundering herd
        raw_jitter = os.getenv("KUSUDAEMON_WAVE_JITTER")
        jitter_ceiling = float(raw_jitter) if raw_jitter is not None else (0.0 if "unittest" in sys.modules else 1.5)
        if max_parallel > 1 and jitter_ceiling > 0:
            import random
            await asyncio.sleep(random.uniform(0.0, jitter_ceiling))
        # §B7.2: Acquire from shared admission controller
        await admission.acquire_async()
        try:
            budget = writer_budget_for(node) if writer_budget_for is not None else default_budget
            await dispatch_node(
                run_dir,
                node,
                tree,
                tree_path,
                writer_adapter_factory=writer_adapter_factory,
                env=env,
                prompt_for_node=prompt_for_node,
                budget=budget,
                manifest=manifest,
                max_attempts=max_attempts,
                log=log,
                split_handler=split_handler,
                tree_lock=tree_lock,
                worktree_manager=worktree_manager,
            )
        finally:
            admission.release_async()

    rev_provider = reviewer_provider if reviewer_provider is not None else provider

    async def review(node: TaskNode) -> None:
        await review_and_transition_node(
            run_dir,
            node,
            tree,
            tree_path,
            provider=rev_provider,
            max_attempts=max_attempts,
            log=log,
            on_node_passed=on_node_passed,
            tree_lock=tree_lock,
            provider_semaphore=provider_sem,
            review_sample_rate=review_sample_rate,
            disable_review=disable_review,
            triage_provider=triage_provider,
        )

    # ------------------------------------------------------------------
    # max_parallel > 1: continuous refill (PLAN-SWEEP-REPAIR.md §K1).
    #
    # The §C2 wave used to be ``await asyncio.gather(*wave)`` followed by a
    # serial per-node retry loop, so a node's retry — and every ready node
    # the wave had no room for — waited on the *slowest* episode in the
    # wave, and then on each other's retries one at a time. On
    # longgen_000-week_armC_seed1 unit-01 died in 0.7 s and unit-03 was
    # never admitted, and both sat idle behind unit-02's 1800 s episode.
    #
    # Here each node runs as its own job (dispatch -> review -> in-place
    # retries, exactly the §11.10.5 sequence) and the loop wakes on the
    # FIRST job to finish, refilling the freed slot straight away. The
    # orchestrator is consulted once per refill, on a view of the tree in
    # which every node a job still owns reads as "dispatched" — a job
    # sitting in its retry backoff is "pending" on disk and in memory, and
    # must not be handed to a second job.
    # ------------------------------------------------------------------
    async def refill_loop(first_round: int, max_ready_width: int) -> int:
        running: dict[asyncio.Future, TaskNode] = {}
        stopping = False  # stop starting work, including in-job retries
        no_new_rounds = False  # stop consulting the orchestrator only
        first_error: BaseException | None = None
        rounds_used = 0

        def claimed_ids() -> set[str]:
            return {n.id for n in running.values()}

        def wave_cap() -> int:
            return max(1, min(max_parallel, admission.current_wave_cap))

        def halted() -> bool:
            if stopping or (should_halt is not None and should_halt()):
                return True
            for n in tree.nodes.values():
                if "provider unavailable" in getattr(n, "last_defect", ""):
                    return True
            return False

        async def after_attempt(node: TaskNode) -> None:
            # §B7.3 AIMD, per finished attempt rather than per wave.
            throttled = 1 if "throttled" in (node.last_defect or "") else 0
            admission.record_wave_outcome(throttled, max_parallel)
            if throttled:
                await asyncio.sleep(min(5.0, admission.remaining_closed_seconds() or 2.0))

        async def node_job(node: TaskNode, start: str, delay: float = 0.0) -> None:
            if delay > 0:
                await asyncio.sleep(delay)
            if start == "dispatch":
                if getattr(node, "non_attempts", 0) > 0:
                    backoff = min(600.0, 30.0 * (2 ** (node.non_attempts - 1)))
                    await asyncio.sleep(backoff)
                await dispatch(node)
                await after_attempt(node)
            if node.status == "awaiting_review":
                await review(node)
            # §11.10.5 in-place retry, unchanged except that it no longer
            # waits for the rest of the wave.
            while node.status == "pending" and node.attempts < max_attempts:
                if getattr(node, "non_attempts", 0) >= int(os.getenv("KUSUDAEMON_MAX_NON_ATTEMPTS", "6")):
                    break
                if is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id):
                    node.status = "awaiting_review"
                    await _save_tree_locked(tree, tree_path, tree_lock)
                    await review(node)
                    break
                # §E15 (c)
                if halted():
                    break
                if getattr(node, "non_attempts", 0) > 0:
                    backoff = min(600.0, 30.0 * (2 ** (node.non_attempts - 1)))
                    await asyncio.sleep(backoff)
                elif node.attempts > 0:
                    await asyncio.sleep(min(2 ** (node.attempts - 1), 5))
                if halted():
                    break
                node.status = "dispatched"
                await _save_tree_locked(tree, tree_path, tree_lock)
                await dispatch(node)
                await after_attempt(node)
                if node.status == "awaiting_review":
                    await review(node)

        def start_job(node: TaskNode, start: str, stagger_idx: int = 0) -> None:
            stagger_base = float(os.getenv("KUSUDAEMON_BOOT_STAGGER_S", "0.0"))
            delay = stagger_idx * stagger_base if start == "dispatch" else 0.0
            running[asyncio.ensure_future(node_job(node, start, delay=delay))] = node

        # §C2 resume scan: nodes a crash left mid-flight become jobs first,
        # admitted through the same slots as everything else.
        resume_queue: list[tuple[TaskNode, str]] = [
            (n, "dispatch") for n in tree.nodes.values() if n.status == "dispatched"
        ]
        for n in tree.nodes.values():
            if n.status == "awaiting_review" or (
                n.status in ("pending", "blocked")
                and (is_node_bypassed(run_dir, n.id, "review") or is_node_bypassed(run_dir, n.id))
            ):
                n.status = "awaiting_review"
                resume_queue.append((n, "review"))

        try:
            while True:
                while not stopping and len(running) < wave_cap():
                    if resume_queue:
                        start_job(*resume_queue.pop(0))
                        continue
                    if no_new_rounds or rounds_used >= max_rounds:
                        break
                    # §E15 (a)
                    if should_halt is not None and should_halt():
                        no_new_rounds = True
                        break
                    claimed = claimed_ids()
                    _sync_tree_from_disk(tree, tree_path, skip=claimed)
                    with _claimed_read_as_dispatched(tree, claimed):
                        ready = tree.ready_nodes()
                        max_ready_width = max(max_ready_width, len(ready))
                        if not ready and running:
                            # Nothing new to hand out until a job finishes.
                            break
                        round_index = first_round + rounds_used
                        rounds_used += 1
                        decision = decide_next_action_with_policy(
                            tree,
                            str(manifest),
                            provider,
                            round_index=round_index,
                            policy=dispatch_policy,
                            max_parallel=max_parallel,
                        )
                        _write_round_trace(run_dir, round_index, tree, decision)

                    if decision.action == "halt":
                        no_new_rounds = True
                        break
                    if decision.action == "escalate":
                        log.append(
                            {
                                "node_id": decision.node_id or "-",
                                "role": "orchestrator",
                                "round": round_index,
                                "type": "run_escalated",
                                "reason": decision.reason,
                            }
                        )
                        no_new_rounds = True
                        break
                    # §E15 (b)
                    if should_halt is not None and should_halt():
                        no_new_rounds = True
                        break
                    if decision.node_id in claimed or decision.node_id not in tree.nodes:
                        break

                    free = wave_cap() - len(running)
                    wave = [tree.nodes[decision.node_id]]
                    picked = claimed | {decision.node_id}
                    admission_refused = False
                    for candidate_id in ready:
                        if len(wave) >= free:
                            break
                        if candidate_id in picked:
                            continue
                        # §B4: a node that conflicted twice never joins a fill
                        if worktree_manager is not None and worktree_manager.conflict_count(candidate_id) >= 2:
                            continue
                        # §B8.3: wave-fill hardware admission
                        from ..utils.system_resources import can_admit_episode
                        if not can_admit_episode():
                            admission_refused = True
                            break
                        wave.append(tree.nodes[candidate_id])
                        picked.add(candidate_id)
                    in_flight_artifacts = [n.artifact for n in running.values()] + [n.artifact for n in wave]
                    assert len(set(in_flight_artifacts)) == len(in_flight_artifacts), (
                        f"two in-flight nodes share an artifact path: {in_flight_artifacts}"
                    )

                    for i, node in enumerate(wave):
                        node.status = "dispatched"
                        await _save_tree_locked(tree, tree_path, tree_lock)
                        log.append(
                            {
                                "node_id": node.id,
                                "role": "orchestrator",
                                "round": round_index,
                                "type": "node_dispatch_decided",
                                "reason": (
                                    decision.reason
                                    if node.id == decision.node_id
                                    else f"parallel wave fill (max_parallel={max_parallel})"
                                ),
                            }
                        )
                        start_job(node, "dispatch", stagger_idx=i)
                    if admission_refused:
                        break

                if not running:
                    break
                done, _ = await asyncio.wait(list(running), return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    running.pop(fut, None)
                    exc = fut.exception()
                    if exc is not None and first_error is None:
                        # Let siblings finish the attempt they are in, start
                        # nothing else, then surface the failure — gather()
                        # used to raise at once and orphan them.
                        first_error = exc
                        stopping = True
        except asyncio.CancelledError:
            # The loop itself is being cancelled (process shutdown): take the
            # jobs down with it.
            for fut in running:
                fut.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        except Exception as exc:
            # §L.4: a harness error here must not kill writer episodes
            # mid-turn (§E15). Start nothing new, let in-flight attempts
            # finish and record their outcomes, then surface the error.
            log.append(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "round_loop_error_draining",
                    "detail": f"{type(exc).__name__}: {exc}"[:500],
                    "in_flight": sorted(n.id for n in running.values()),
                }
            )
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        if first_error is not None:
            raise first_error
        return max_ready_width

    # ------------------------------------------------------------------
    # dispatch_policy="orchestrator" (PLAN-SWEEP-REPAIR.md §L): the event-
    # driven orchestrator. It is consulted at the start and whenever a node
    # finishes; it alone decides what to dispatch or redispatch, or to wait.
    # A job here is one attempt (dispatch -> review) — a failed node goes
    # back to the pool and the orchestrator decides when it runs again.
    # ------------------------------------------------------------------
    async def orchestrated_loop(first_round: int, max_ready_width: int) -> int:
        from .orchestrator import (
            ORCHESTRATOR_SCHEMA,
            build_orchestrator_messages,
            classify_node_event,
            fallback_decision_payload,
            orchestrator_deadline_s,
            orchestrator_max_tokens,
            parse_orchestrator_decision,
        )
        from .provider import call_scope

        running: dict[asyncio.Future, TaskNode] = {}
        started_at: dict[str, float] = {}
        events: list[Any] = []
        news = True  # something happened since the last decision
        calls = 0
        # §L.5: the orchestrator is stateless per call, so its own last
        # decision is handed back to it — recovered from events.jsonl on a
        # resume (the filesystem is the state).
        last_decision: dict[str, Any] | None = _last_orchestrator_decision(run_dir)
        # §L.2: a call abandoned at its deadline keeps its worker thread; no
        # second call is started until it returns.
        abandoned: asyncio.Future | None = None
        fresh_rounds = 0  # decisions that started never-attempted work (max_rounds)
        no_new_work = False
        stopping = False
        first_error: BaseException | None = None

        def claimed_ids() -> set[str]:
            return {n.id for n in running.values()}

        def wave_cap() -> int:
            return max(1, min(max_parallel, admission.current_wave_cap))

        async def attempt_job(node: TaskNode, start: str) -> None:
            if start == "dispatch":
                if node.attempts > 0 and (
                    is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id)
                ):
                    node.status = "awaiting_review"
                    await _save_tree_locked(tree, tree_path, tree_lock)
                else:
                    if getattr(node, "non_attempts", 0) > 0:
                        backoff = min(600.0, 30.0 * (2 ** (node.non_attempts - 1)))
                        await asyncio.sleep(backoff)
                    elif node.attempts > 0:
                        # Transient-failure spacing, as the in-place retry had.
                        await asyncio.sleep(min(2 ** (node.attempts - 1), 5))
                    await dispatch(node)
                    throttled = 1 if "throttled" in (node.last_defect or "") else 0
                    admission.record_wave_outcome(throttled, max_parallel)
                    if throttled:
                        await asyncio.sleep(min(5.0, admission.remaining_closed_seconds() or 2.0))
            if node.status == "awaiting_review":
                await review(node)

        def start_job(node: TaskNode, start: str) -> None:
            started_at[node.id] = time.monotonic()
            running[asyncio.ensure_future(attempt_job(node, start))] = node

        resume_queue: list[tuple[TaskNode, str]] = [
            (n, "dispatch") for n in tree.nodes.values() if n.status == "dispatched"
        ]
        for n in tree.nodes.values():
            if n.status == "awaiting_review" or (
                n.status in ("pending", "blocked")
                and (is_node_bypassed(run_dir, n.id, "review") or is_node_bypassed(run_dir, n.id))
            ):
                n.status = "awaiting_review"
                resume_queue.append((n, "review"))

        try:
            while True:
                while resume_queue and len(running) < wave_cap():
                    start_job(*resume_queue.pop(0))

                if not stopping and not no_new_work and not resume_queue and news:
                    # §E15 (a)
                    if (should_halt is not None and should_halt()) or any(
                        "provider unavailable" in getattr(n, "last_defect", "")
                        for n in tree.nodes.values()
                    ):
                        no_new_work = True
                    else:
                        claimed = claimed_ids()
                        _sync_tree_from_disk(tree, tree_path, skip=claimed)
                        with _claimed_read_as_dispatched(tree, claimed):
                            ready = tree.ready_nodes()
                            max_ready_width = max(max_ready_width, len(ready) + len(claimed))
                            free = wave_cap() - len(running)
                            if not ready and not running:
                                # Terminal, and code-decided: all passed, or stuck.
                                terminal = decide_next_action_deterministic(tree, round_index=first_round + calls)
                                _write_round_trace(run_dir, first_round + calls, tree, terminal)
                                if terminal.action == "escalate":
                                    log.append(
                                        {
                                            "node_id": "-",
                                            "role": "orchestrator",
                                            "round": first_round + calls,
                                            "type": "run_escalated",
                                            "reason": terminal.reason,
                                        }
                                    )
                                break
                            messages = None
                            forced_dispatch_id: str | None = None
                            if ready and free > 0:
                                if (
                                    len(ready) == 1
                                    and free == 1
                                    and not running
                                    and os.getenv("KUSUDAEMON_ORCHESTRATOR_SKIP_FORCED", "1") != "0"
                                ):
                                    # PLAN-REVIEW-READ-LOOP.md §O3 (operator decision
                                    # 2026-09-16): forced choice — exactly one
                                    # dispatchable node, one free slot, nothing in
                                    # flight. The only possible answer is that node,
                                    # so no model call is spent.
                                    forced_dispatch_id = ready[0]
                                else:
                                    now = time.monotonic()
                                    previous = None
                                    if last_decision is not None:
                                        previous = dict(last_decision)
                                        if isinstance(previous.get("ts"), (int, float)):
                                            previous["age_s"] = max(0.0, time.time() - previous["ts"])
                                    RubricOrch = build_orchestrator_messages(
                                        tree,
                                        events=events,
                                        ready=ready,
                                        in_flight=[
                                            (n.id, now - started_at.get(n.id, now)) for n in running.values()
                                        ],
                                        free_slots=free,
                                        max_slots=max_parallel,
                                        max_attempts=max_attempts,
                                        manifest_path=str(manifest),
                                        decision_index=first_round + calls,
                                        first_call=(calls == 0),
                                        previous_decision=previous,
                                    )
                                    messages = RubricOrch
                        if forced_dispatch_id is not None:
                            node = tree.nodes[forced_dispatch_id]
                            round_index = first_round + calls
                            calls += 1
                            told = events
                            events = []
                            news = False
                            last_decision = {
                                "round": round_index,
                                "action": "dispatch",
                                "node_ids": [node.id],
                                "wait_on": [],
                                "reason": "forced choice: single dispatchable node and slot",
                                "corrections": [],
                                "ts": time.time(),
                            }
                            log.append(
                                {
                                    "node_id": node.id,
                                    "role": "orchestrator",
                                    "round": round_index,
                                    "type": "orchestrator_call_skipped",
                                    "reason": "forced choice: single dispatchable node and slot",
                                    "events": [
                                        {"node_id": e.node_id, "outcome": e.outcome, "attempts": e.attempts}
                                        for e in told
                                    ],
                                }
                            )
                            node.status = "dispatched"
                            await _save_tree_locked(tree, tree_path, tree_lock)
                            log.append(
                                {
                                    "node_id": node.id,
                                    "role": "orchestrator",
                                    "round": round_index,
                                    "type": "node_dispatch_decided",
                                    "reason": "forced choice: single dispatchable node and slot",
                                    "redispatch": node.attempts > 0,
                                }
                            )
                            if node.attempts == 0:
                                fresh_rounds += 1
                            start_job(node, "dispatch")
                            if fresh_rounds >= max_rounds:
                                no_new_work = True
                        elif messages is None:
                            # Nothing dispatchable, or no free slot: the only
                            # possible answer is "wait", so no call is spent.
                            # The events stay queued for the next real decision.
                            news = False
                        else:
                            round_index = first_round + calls
                            calls += 1
                            told = events
                            events = []
                            news = False
                            payload: Any = None
                            why: str | None = None
                            deadline = orchestrator_deadline_s()
                            if abandoned is not None and not abandoned.done():
                                why = "previous orchestrator call is still running past its deadline"
                            else:
                                abandoned = None
                                cap_tokens = orchestrator_max_tokens()

                                def _call(msgs: list[dict[str, str]] = messages) -> Any:
                                    # §L.2/§L.7: bounded output, and cost stamped
                                    # "orchestrator" — thread-local, so the shared
                                    # provider's other callers are unaffected.
                                    with call_scope(role="orchestrator", max_tokens=cap_tokens):
                                        return provider.complete_json(msgs, ORCHESTRATOR_SCHEMA, streaming=True)

                                call_task = asyncio.ensure_future(asyncio.to_thread(_call))
                                call_task.add_done_callback(
                                    lambda t: None if t.cancelled() else t.exception()
                                )
                                try:
                                    if deadline > 0:
                                        payload = await asyncio.wait_for(asyncio.shield(call_task), deadline)
                                    else:
                                        payload = await call_task
                                except asyncio.TimeoutError:
                                    abandoned = call_task
                                    why = f"no answer within {deadline:.0f}s"
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:  # §L.4: never fatal to the run
                                    why = f"{type(exc).__name__}: {exc}"
                                if why is None and not isinstance(payload, dict):
                                    why = f"non-object answer {type(payload).__name__}"
                            if why is not None:
                                payload = fallback_decision_payload(ready, free, why)
                                log.append(
                                    {
                                        "node_id": "-",
                                        "role": "orchestrator",
                                        "round": round_index,
                                        "type": "orchestrator_call_failed",
                                        "detail": why[:500],
                                    }
                                )
                            # The call took time: re-read the state it is applied to.
                            claimed = claimed_ids()
                            with _claimed_read_as_dispatched(tree, claimed):
                                ready_now = tree.ready_nodes()
                            decision = parse_orchestrator_decision(
                                payload,
                                ready=ready_now,
                                in_flight=sorted(claimed),
                                free_slots=wave_cap() - len(running),
                            )
                            last_decision = {"round": round_index, **decision.as_record(), "ts": time.time()}
                            log.append(
                                {
                                    "node_id": "-",
                                    "role": "orchestrator",
                                    "round": round_index,
                                    "type": "orchestrator_decision",
                                    "action": decision.action,
                                    "node_ids": decision.node_ids,
                                    "wait_on": decision.wait_on,
                                    "reason": decision.reason,
                                    "corrections": decision.corrections,
                                    "fallback": why is not None,
                                    "events": [
                                        {"node_id": e.node_id, "outcome": e.outcome, "attempts": e.attempts}
                                        for e in told
                                    ],
                                    "ready": ready_now,
                                    "in_flight": sorted(claimed),
                                }
                            )
                            _write_round_trace(
                                run_dir,
                                round_index,
                                tree,
                                DispatchDecision(
                                    decision.action,
                                    (decision.node_ids or decision.wait_on or [None])[0],
                                    decision.reason,
                                ),
                                extra={
                                    "node_ids": decision.node_ids,
                                    "wait_on": decision.wait_on,
                                    "corrections": decision.corrections,
                                    "fallback": why is not None,
                                },
                            )
                            # §E15 (b)
                            if should_halt is not None and should_halt():
                                no_new_work = True
                            elif decision.action == "dispatch":
                                if any(tree.nodes[i].attempts == 0 for i in decision.node_ids):
                                    fresh_rounds += 1
                                for node_id in decision.node_ids:
                                    node = tree.nodes[node_id]
                                    node.status = "dispatched"
                                    await _save_tree_locked(tree, tree_path, tree_lock)
                                    log.append(
                                        {
                                            "node_id": node.id,
                                            "role": "orchestrator",
                                            "round": round_index,
                                            "type": "node_dispatch_decided",
                                            "reason": decision.reason,
                                            "redispatch": node.attempts > 0,
                                        }
                                    )
                                    start_job(node, "dispatch")
                                if fresh_rounds >= max_rounds:
                                    no_new_work = True
                                # Slots the orchestrator left free stay free until
                                # the next node finishes.

                if not running:
                    if resume_queue:
                        continue
                    if not stopping and not no_new_work and not news:
                        # Nothing running and nothing decided (the state moved
                        # under a call): look again; the terminal check above
                        # ends the loop if there is truly nothing left.
                        news = True
                        continue
                    break
                done, _ = await asyncio.wait(list(running), return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    node = running.pop(fut)
                    elapsed = time.monotonic() - started_at.pop(node.id, time.monotonic())
                    exc = fut.exception()
                    events.append(classify_node_event(node, duration_s=elapsed, crashed=exc))
                    news = True
                    if exc is not None and first_error is None:
                        first_error = exc
                        stopping = True
        except asyncio.CancelledError:
            # The loop itself is being cancelled (process shutdown): take the
            # jobs down with it.
            for fut in running:
                fut.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        except Exception as exc:
            # §L.4: a harness error here must not kill writer episodes
            # mid-turn (§E15). Start nothing new, let in-flight attempts
            # finish and record their outcomes, then surface the error.
            log.append(
                {
                    "node_id": "-",
                    "role": "harness",
                    "round": 0,
                    "type": "round_loop_error_draining",
                    "detail": f"{type(exc).__name__}: {exc}"[:500],
                    "in_flight": sorted(n.id for n in running.values()),
                }
            )
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        if first_error is not None:
            raise first_error
        return max_ready_width

    if dispatch_policy == "orchestrator":
        max_ready_width = await orchestrated_loop(
            _next_round_index(run_dir), len(tree.ready_nodes())
        )
    elif max_parallel > 1:
        max_ready_width = await refill_loop(
            _next_round_index(run_dir), len(tree.ready_nodes())
        )
    else:
        # max_parallel == 1: the pre-§C2 serial loop, byte-identical event sequence.
        # §C2: "gather the resume scan" — nodes caught mid-flight by a crash
        # resume in max_parallel-sized chunks instead of one big sequential
        # pass. Chunks of one = today's exact sequence.
        in_flight_dispatch = [n for n in tree.nodes.values() if n.status == "dispatched"]
        for chunk in chunks(in_flight_dispatch):
            await asyncio.gather(*(dispatch(n) for n in chunk))
        in_flight_review = [
            n for n in tree.nodes.values()
            if n.status == "awaiting_review"
            or (n.status in ("pending", "blocked") and (is_node_bypassed(run_dir, n.id, "review") or is_node_bypassed(run_dir, n.id)))
        ]
        for chunk in chunks(in_flight_review):
            for n in chunk:
                if n.status != "awaiting_review":
                    n.status = "awaiting_review"
            await asyncio.gather(*(review(n) for n in chunk))

        # §11.10.16: round indices continue across process runs. round_index
        # used to restart at 0 on every resume while the trace files were opened
        # "a", so round 0 of the third resume appended to round 0 of the first —
        # one file, three processes' interleaved rounds, impossible to separate.
        # The orchestrator's own view is stateless per round, so rebasing the
        # numbers is free: this run's rounds are just pick up where the last
        # process left off.
        first_round = _next_round_index(run_dir)
        max_ready_width = len(tree.ready_nodes())
        for offset in range(max_rounds):
            # §E15 (a): before the orchestrator's dispatch decision — a hit
            # here means no call is even made for a round that will never run.
            if should_halt is not None and should_halt():
                break
            _sync_tree_from_disk(tree, tree_path)
            max_ready_width = max(max_ready_width, len(tree.ready_nodes()))
            round_index = first_round + offset
            decision = decide_next_action_with_policy(
                tree,
                str(manifest),
                provider,
                round_index=round_index,
                policy=dispatch_policy,
                max_parallel=max_parallel,
            )
            _write_round_trace(run_dir, round_index, tree, decision)

            if decision.action == "halt":
                break
            if decision.action == "escalate":
                log.append(
                    {
                        "node_id": decision.node_id or "-",
                        "role": "orchestrator",
                        "round": round_index,
                        "type": "run_escalated",
                        "reason": decision.reason,
                    }
                )
                break

            # §E15 (b): before this round's wave is committed (nodes marked
            # "dispatched" and actually sent out) — a hit here leaves the tree
            # exactly as the previous round left it, no partial mutation.
            if should_halt is not None and should_halt():
                break

            # A wave of one: max_parallel > 1 runs through refill_loop above.
            wave = [tree.nodes[decision.node_id]]

            for node in wave:
                node.status = "dispatched"
                await _save_tree_locked(tree, tree_path, tree_lock)
                log.append(
                    {
                        "node_id": node.id,
                        "role": "orchestrator",
                        "round": round_index,
                        "type": "node_dispatch_decided",
                        "reason": (
                            decision.reason
                            if node.id == decision.node_id
                            else f"parallel wave fill (max_parallel={max_parallel})"
                        ),
                    }
                )

            for chunk in chunks(wave):
                await asyncio.gather(*(dispatch(n) for n in chunk))
            for chunk in chunks([n for n in wave if n.status == "awaiting_review"]):
                await asyncio.gather(*(review(n) for n in chunk))

            # §B7.3: AIMD adjustment on wave size
            throttled_count = sum(1 for n in wave if "throttled" in (n.last_defect or ""))
            admission.record_wave_outcome(throttled_count, max_parallel)
            if throttled_count > 0:
                await asyncio.sleep(min(5.0, admission.remaining_closed_seconds() or 2.0))
            # §11.10.5: a gate or review failure that still has attempts left is
            # a retry of the node the harness already knows it wants. Re-dispatch
            # in place instead of round-tripping the orchestrator for a call
            # whose only possible answer is "dispatch the same node again" — the
            # retry's prompt differs (last_defect is carried forward by
            # PLAN-zeromem.md §9), so this is a correction, not a resample.
            for chunk in chunks(wave):
                for node in chunk:
                    while node.status == "pending" and node.attempts < max_attempts:
                        if is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id):
                            node.status = "awaiting_review"
                            await _save_tree_locked(tree, tree_path, tree_lock)
                            await review(node)
                            break
                        # §E15 (c): before starting a new retry attempt — the
                        # node is simply left "pending" (its state from the
                        # just-failed attempt), not marked failed or blocked.
                        if should_halt is not None and should_halt():
                            break
                        if node.attempts > 0:
                            await asyncio.sleep(min(2 ** (node.attempts - 1), 5))
                        node.status = "dispatched"
                        await _save_tree_locked(tree, tree_path, tree_lock)
                        await dispatch(node)
                        if node.status == "awaiting_review":
                            await review(node)

    if max_parallel > 1 and max_ready_width <= 1:
        log.append(
            {
                "node_id": "-",
                "role": "harness",
                "round": 0,
                "type": "max_parallel_inert",
                "phase": "execute",
                "max_parallel": max_parallel,
                "max_ready_width": max_ready_width,
                "detail": (
                    f"max_parallel={max_parallel} was configured, but ready set never exceeded width 1 "
                    f"(max width observed: {max_ready_width})"
                ),
            }
        )

    return tree


def _read_artifact(run_dir: Path, node_id: str) -> str:
    """PLAN-SWEEP-REPAIR.md §B2: resolve via node_artifact_text so part files
    under out/<node>/ and the single-file layout read identically. Gate
    evaluation, the reviewer, and the shrink check all flow through here."""
    from ..v0.run_dir import node_artifact_text
    try:
        return node_artifact_text(run_dir, node_id)
    except (FileNotFoundError, OSError):
        return ""


async def _transition_after_writer(
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    episode_ok: bool,
    gate_results: list[GateResult],
    max_attempts: int,
    log: EventLog,
    tree_lock: asyncio.Lock | None = None,
    run_dir: str | Path | None = None,
    result_status: str | None = None,
) -> None:
    from ..pipeline.bypass import is_node_bypassed

    r_dir = run_dir or Path(tree_path).parent
    bypassed = is_node_bypassed(r_dir, node.id, "review") or is_node_bypassed(r_dir, node.id)
    gates_passed = all_passed(gate_results)

    # PLAN-TOKEN-ACCOUNTING.md §O7 & §L4: artifact shrink check and accidental loss recovery
    # PLAN-SWEEP-REPAIR.md §B2/B4 option (a): current text resolves via
    # node_artifact_text (parts-aware); prior state comes from attempt_*
    # snapshots (file or directory) — never a flattened write into the single
    # file while a parts dir shadows it.
    from ..v3.run_dir import list_attempt_snapshots, read_attempt_snapshot_text
    from ..pipeline.corruption import is_artifact_corrupted
    regressed_accidental = False
    current_text = _read_artifact(r_dir, node.id)
    current_bytes = len(current_text.encode("utf-8"))
    # PLAN-SWEEP-REPAIR.md §J9: count with the run's own resolved delimiter
    # (read back off the node's ``units_min:N@<delim>`` gate) through the
    # same ``gates.count_units`` the gate itself uses. This module used to
    # carry a private copy of the noun regex that was missing ``\s*`` after
    # the ``#*#`` alternative, so every LongGenBench artifact counted zero
    # units — which made the timeout defect below say "0 of 100 units
    # written; resume at unit 1" over a file holding 60 floors, and
    # ``prompts.py`` handed that resume point straight to the writer.
    _u_delim = unit_delimiter_from_gates(node)
    from ..v0.run_dir import node_parts_dir
    parts_d = node_parts_dir(r_dir, node.id)
    has_unit_stubs = parts_d.is_dir() and any(parts_d.glob("u*.md"))
    stubs_count = 0
    if parts_d.is_dir():
        stubs_count = sum(1 for p in parts_d.glob("u*.md") if p.is_file() and p.stat().st_size > 0)
    current_units = max(count_units(current_text, _u_delim), stubs_count)

    prior_units = 0
    prior_bytes = 0
    snaps = list_attempt_snapshots(r_dir, node.id)
    if snaps:
        latest_snap = snaps[-1]
        try:
            prior_text = read_attempt_snapshot_text(latest_snap)
            prior_bytes = len(prior_text.encode("utf-8"))
            prior_units = count_units(prior_text, _u_delim)
            if latest_snap.is_dir():
                snap_stubs = sum(1 for p in latest_snap.glob("u*.md") if p.is_file() and p.stat().st_size > 0)
                prior_units = max(prior_units, snap_stubs)

            if current_bytes < prior_bytes or (prior_units > 0 and current_units < prior_units):
                corrupted, _ = is_artifact_corrupted(r_dir, node)
                classification = "scoped"
                if not current_text.strip() or corrupted:
                    classification = "unscoped"
                elif prior_units > 0 and current_units < prior_units:
                    p_k = first_unit_key(prior_text, _u_delim)
                    c_k = first_unit_key(current_text, _u_delim)
                    if p_k and c_k and p_k != c_k:
                        classification = "unscoped"

                log.append(
                    {
                        "node_id": node.id,
                        "role": "harness",
                        "round": 0,
                        "type": "artifact_shrank",
                        "classification": classification,
                        "prior_bytes": prior_bytes,
                        "current_bytes": current_bytes,
                        "prior_units": prior_units,
                        "current_units": current_units,
                    }
                )

                # §L4: recover proven-accidental loss
                # PLAN-SWEEP-REPAIR.md §B4 option (a): restore the snapshot
                # at its own granularity (parts dir ↔ parts dir, file ↔
                # file) so the restore is not shadowed into a no-op.
                if classification == "unscoped" and not (episode_ok and (gates_passed or bypassed)):
                    from ..v3.run_dir import restore_attempt_snapshot
                    restore_attempt_snapshot(r_dir, node.id, latest_snap)
                    log.append(
                        {
                            "node_id": node.id,
                            "role": "harness",
                            "round": 0,
                            "type": "node_attempt_regressed",
                            "reason": "accidental_loss_restored",
                            "detail": f"restored from {latest_snap.name}",
                        }
                    )
                    regressed_accidental = True
        except Exception:
            pass

    # A writer that finished its slice but never exited (2026-09-16 323-block
    # seed 2, unit-02: "episode ended with 25 of 25 units written") used to be
    # charged an attempt and redispatched over a complete artifact. When the
    # declared unit count is met and every hard gate passes, the work is done:
    # send it to review like any other finished episode.
    units_exp = node.budget.units_expected if node.budget else None
    timeout_salvaged = (
        result_status == "timeout"
        and not regressed_accidental
        and gates_passed
        and bool(units_exp)
        and current_units >= (units_exp or 0)
    )
    # Same for an accidental-loss restore whose restored content is already
    # complete (seed 2, unit-04: 25 units restored, then redispatched — the
    # redispatch is exactly the step that clobbered it the first time).
    restored_complete = False
    if regressed_accidental and units_exp and node.gates:
        restored_text = _read_artifact(r_dir, node.id)
        restored_gates = evaluate_gates(node.gates, restored_text)
        if all_passed(restored_gates) and count_units(restored_text, _u_delim) >= units_exp:
            restored_complete = True
            try:
                write_gate_cache(ensure_audit_path(r_dir, node.id), restored_gates)
            except OSError:
                pass

    if (episode_ok or timeout_salvaged) and (gates_passed or bypassed):
        node.non_attempts = 0
        node.status = "awaiting_review"
        node.last_defect = ""
        if timeout_salvaged:
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_timeout_salvaged",
                    "detail": (
                        f"episode timed out with {current_units} of {units_exp} units "
                        "written and all gates passing; proceeding to review"
                    ),
                }
            )
        if not gates_passed and bypassed:
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_gates_bypassed",
                    "detail": "gates failed but review bypassed by operator; proceeding to review",
                    "unmet": [result.gate for result in gate_results if not result.passed],
                }
            )
    elif result_status in ("throttled", "transport"):
        node.non_attempts = getattr(node, "non_attempts", 0) + 1
        max_non_attempts = int(os.getenv("KUSUDAEMON_MAX_NON_ATTEMPTS", "6"))
        if node.non_attempts >= max_non_attempts:
            node.status = "blocked"
            node.last_defect = f"provider unavailable: hit {node.non_attempts} consecutive non-attempts ({result_status})"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "run_halted",
                    "reason": "provider unavailable",
                    "detail": node.last_defect,
                }
            )
        else:
            node.status = "pending"
            node.last_defect = f"{result_status}: rate limit / provider error (non-attempt {node.non_attempts}/{max_non_attempts})"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": f"node_{result_status}",
                    "attempts": node.attempts,
                    "non_attempts": node.non_attempts,
                    "episode_ok": False,
                    "detail": f"{result_status} from provider",
                }
            )
    elif restored_complete:
        node.non_attempts = 0
        node.status = "awaiting_review"
        node.last_defect = ""
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "node_restored_complete",
                "detail": "restored artifact already meets its gates; proceeding to review",
            }
        )
    elif regressed_accidental:
        # §L4: Do not count proven-accidental loss against max_attempts
        node.status = "pending"
        node.last_defect = (
            f"accidental loss restored: previous content ({prior_bytes}B) was lost; "
            f"continue existing artifact (currently {prior_units} units) rather than restarting"
        )
    elif (has_unit_stubs or os.getenv("KUSUDAEMON_CONTINUATION_PROGRESS", "0") == "1") and current_units > prior_units:
        # A4: Forward progress made on units — continuation without charging an attempt.
        node.status = "pending"
        if result_status == "timeout":
            if units_exp and units_exp > 0:
                node.last_defect = (
                    f"episode_timeout: episode ended with {current_units} of {units_exp} units written; "
                    f"resume at unit {current_units + 1}"
                )
            elif current_units > 0:
                node.last_defect = (
                    f"episode_timeout: episode ended with {current_units} units written; "
                    f"resume at unit {current_units + 1}"
                )
            else:
                node.last_defect = "episode_timeout: episode wall clock exceeded"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_continuation_progress",
                    "attempts": node.attempts,
                    "prior_units": prior_units,
                    "current_units": current_units,
                    "episode_ok": False,
                    "detail": node.last_defect,
                }
            )
        else:
            node.last_defect = "; ".join(
                f"{result.gate}: {result.detail}" for result in unmet(gate_results)
            ) or f"continuation: {current_units} units written"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_continuation_progress",
                    "attempts": node.attempts,
                    "prior_units": prior_units,
                    "current_units": current_units,
                    "episode_ok": episode_ok,
                    "unmet": [result.gate for result in gate_results if not result.passed],
                    "detail": node.last_defect,
                }
            )
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
        # PLAN-BENCH-INTEGRITY.md §2.4 & PLAN-TOKEN-ACCOUNTING.md §L3: artifact-relative timeout defect
        if result_status == "timeout":
            units_exp = node.budget.units_expected if node.budget else None
            if units_exp and units_exp > 0:
                node.last_defect = (
                    f"episode_timeout: episode ended with {current_units} of {units_exp} units written; "
                    f"resume at unit {current_units + 1}"
                )
            elif current_units > 0:
                node.last_defect = (
                    f"episode_timeout: episode ended with {current_units} units written; "
                    f"resume at unit {current_units + 1}"
                )
            else:
                node.last_defect = "episode_timeout: episode wall clock exceeded"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_episode_timeout",
                    "attempts": node.attempts,
                    "episode_ok": False,
                    "detail": node.last_defect,
                }
            )
        else:
            # PLAN-zeromem.md §9: carry the located failure forward so a retry's
            # prompt differs from the first attempt's instead of resampling the
            # same instructions blind.
            node.last_defect = "; ".join(
                f"{result.gate}: {result.detail}" for result in unmet(gate_results)
            ) or "episode did not complete"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_gate_failed",
                    "attempts": node.attempts,
                    "episode_ok": episode_ok,
                    "unmet": [result.gate for result in gate_results if not result.passed],
                }
            )
    await _save_tree_locked(tree, tree_path, tree_lock)


async def _transition_after_review(
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    verdict: ReviewVerdict,
    max_attempts: int,
    log: EventLog,
    tree_lock: asyncio.Lock | None = None,
    run_dir: str | Path | None = None,
) -> None:
    if verdict.verdict == "pass":
        # PLAN.md invariant 1: only the harness writes "passed", and only
        # after gates (checked in _transition_after_writer) and review
        # (checked here) both agree.
        node.status = "passed"
        node.last_defect = ""
    elif verdict.verdict == "unavailable":
        if os.getenv("KUSUDAEMON_REVIEW_UNAVAILABLE") == "retry":
            node.status = "awaiting_review"
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_review_unavailable_retry",
                    "detail": "reviewer unavailable; queued for retry",
                }
            )
        else:
            node.status = "passed"
            node.last_defect = ""
            log.append(
                {
                    "node_id": node.id,
                    "role": "harness",
                    "round": 0,
                    "type": "node_review_unavailable",
                    "detail": f"reviewer unavailable ({getattr(verdict, 'skip_reason', '')}); passing node on gates",
                }
            )
            if run_dir:
                try:
                    audit_p = ensure_audit_path(Path(run_dir), node.id)
                    if audit_p.is_file():
                        ad = json.loads(audit_p.read_text(encoding="utf-8"))
                        ad["review_status"] = "unavailable"
                        audit_p.write_text(json.dumps(ad, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                except Exception:
                    pass
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "pending"
        node.last_defect = _defect_from_verdict(verdict)
        log.append(
            {
                "node_id": node.id,
                "role": "harness",
                "round": 0,
                "type": "node_review_failed",
                "attempts": node.attempts,
            }
        )
    await _save_tree_locked(tree, tree_path, tree_lock)


def _last_orchestrator_decision(run_dir: str | Path, tail_bytes: int = 262_144) -> dict[str, Any] | None:
    """The most recent ``orchestrator_decision`` in events.jsonl (§L.5).

    Reads only the file's tail: events.jsonl is append-only and can be large.
    Returns None when there is none (a fresh run, or an older policy)."""
    path = Path(events_path(run_dir))
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        if '"orchestrator_decision"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "orchestrator_decision":
            continue
        return {
            "round": event.get("round"),
            "action": event.get("action"),
            "node_ids": event.get("node_ids") or [],
            "wait_on": event.get("wait_on") or [],
            "reason": event.get("reason", ""),
            "corrections": event.get("corrections") or [],
            "ts": event.get("ts"),
        }
    return None


def _sync_tree_from_disk(
    tree: TaskTree, tree_path: str | Path, *, skip: set[str] | frozenset[str] = frozenset()
) -> None:
    """Fold operator edits on disk (a redispatch, a manual pass) into memory.

    ``skip`` names nodes a live job owns (``refill_loop``). Their in-memory
    state is authoritative and can be ahead of disk: a job that just bumped
    ``attempts`` may still be waiting on the tree lock to save it, and
    copying the stale on-disk count back would hand it an extra attempt.
    """
    try:
        on_disk = TaskTree.load(tree_path)
    except Exception:
        return
    for node_id, disk_node in on_disk.nodes.items():
        if node_id in skip:
            continue
        mem_node = tree.nodes.get(node_id)
        if mem_node is None:
            tree.nodes[node_id] = disk_node
            continue
        # Preserve external redispatch (node reset to pending on disk)
        if disk_node.status == "pending" and mem_node.status not in ("dispatched", "awaiting_review"):
            mem_node.status = "pending"
            mem_node.attempts = disk_node.attempts
            mem_node.last_defect = disk_node.last_defect
        elif disk_node.status == "passed" and mem_node.status != "passed":
            mem_node.status = "passed"
            mem_node.last_defect = ""
        elif disk_node.status == "blocked" and mem_node.status != "blocked":
            mem_node.status = "blocked"
            mem_node.last_defect = disk_node.last_defect


@contextmanager
def _claimed_read_as_dispatched(tree: TaskTree, claimed: set[str]) -> Iterator[None]:
    """Expose the tree to the orchestrator with live-job nodes reading dispatched."""
    prev: dict[str, NodeStatus] = {}
    for node_id in claimed:
        node = tree.nodes.get(node_id)
        if node is not None and node.status != "dispatched":
            prev[node_id] = node.status
            node.status = "dispatched"
    try:
        yield
    finally:
        for node_id, status in prev.items():
            node = tree.nodes.get(node_id)
            if node is not None:
                node.status = status


async def _save_tree_locked(
    tree: TaskTree, tree_path: str | Path, tree_lock: asyncio.Lock | None = None
) -> None:
    """PLAN.md §C2's "single-writer discipline for tree.json": every
    writer of the shared tree serializes its ``save`` through one lock.
    On the single event-loop thread the mutation+save sequences are
    synchronous (no awaits between them), so the discipline is technically
    redundant today — but it is the invariant in code that a future
    threaded caller (or a refactor that puts an await inside the
    mutation) cannot silently break: two interleaved ``save`` calls of a
    shared in-memory ``TaskTree`` would each serialize *their view* of
    the whole tree, and the later writer's view could be missing another
    task's just-committed status."""
    if tree_lock is not None:
        async with tree_lock:
            _sync_tree_from_disk(tree, tree_path)
            tree.save(tree_path)
    else:
        _sync_tree_from_disk(tree, tree_path)
        tree.save(tree_path)


def _defect_from_verdict(verdict: ReviewVerdict) -> str:
    """Located, scoped feedback (PLAN-zeromem.md §9) rather than a bare
    "fail" — the same join v3/revalidate.py already does for repair
    prompts, applied one layer earlier so an ordinary retry gets it too."""
    lines = []
    for item in verdict.items:
        if not item.get("pass", True):
            id_ = item.get("id", "?")
            defect = item.get("defect", "")
            unit = item.get("unit")
            if unit is not None:
                lines.append(f"unit {unit} — {id_}: {defect}".rstrip(": "))
            else:
                lines.append(f"{id_}: {defect}".rstrip(": "))
    return "\n".join(lines) if lines else "reviewer verdict: fail"


def _write_audit(
    run_dir: Path,
    node: TaskNode,
    verdict: ReviewVerdict,
    artifact_text: str = "",
    contract_text: str = "",
    brief: str = "",
) -> None:
    path = ensure_audit_path(run_dir, node.id)
    gates: dict | None = None
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                gates = loaded
            elif isinstance(loaded, list):
                # §11.10.11: the dispatch-time gate cache is the bare list
                # write_gate_cache produced; carry it under "gates".
                gates = {"gates": loaded}
        except ValueError:
            gates = None
    existing = gates or {}
    # §11.10.11: merge, never replace — the gate cache written at dispatch
    # must survive the reviewer's write of items/verdict.
    existing.update(
        {
            "node": node.id,
            "items": verdict.items,
            "verdict": verdict.verdict,
            "truncated": verdict.truncated,
            "verdict_digest": compute_verdict_digest(artifact_text, node.rubric, node.judgment, contract_text, brief=brief or getattr(node, "brief", "")),
            "sections": getattr(verdict, "section_verdicts", []),
            "skip_reason": getattr(verdict, "skip_reason", None),
        }
    )
    if getattr(verdict, "verdict", None) == "unavailable" or getattr(verdict, "review_status", None) == "unavailable":
        existing["review_status"] = "unavailable"
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _next_round_index(run_dir: Path) -> int:
    """First round number for this process run: one past the highest
    ``round-NNN.jsonl`` already on disk. §11.10.16 — see the caller."""
    trace_dir = orchestrator_dir(run_dir)
    if not trace_dir.exists():
        return 0
    highest = -1
    for path in trace_dir.glob("round-*.jsonl"):
        stem = path.stem
        try:
            index = int(stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        highest = max(highest, index)
    return highest + 1


def _write_round_trace(
    run_dir: Path,
    round_index: int,
    tree: TaskTree,
    decision: DispatchDecision,
    *,
    extra: dict[str, Any] | None = None,
) -> None:
    """``extra`` (§L.7) carries the orchestrator's list fields; ``node_id``
    stays a single id (the first) so older readers keep working."""
    line = {
        "round": round_index,
        "ts": time.time(),
        "ready_nodes": tree.ready_nodes(),
        "decision": {
            "action": decision.action,
            "node_id": decision.node_id,
            "reason": decision.reason,
            **(extra or {}),
        },
    }
    with open(ensure_orchestrator_dir(run_dir) / f"round-{round_index:03d}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
