"""Scoped repair (PLAN.md §4.5, §4.6, §10).

A repair targets one already-produced node with a **scoped, located**
defect ("§Worked Examples, example 2 omits the intermediate step") and is
instructed to make the minimal change — freeform prose suggestions get
over-applied and drift the artifact away from what already passed review
(§4.5). It then goes back through the same gates + review any writer
dispatch does (§4.6: "which goes back through review"), and only replaces
the live artifact once that succeeds.

**Why this can't just call v0's ``run_node`` again with the node's own
id.** ``run_node`` is keyed by node id in ``events.jsonl``; once a node has
an ``episode_completed`` event (true for anything reaching this module —
repair only ever targets a node that already passed once), calling
``run_node`` again on that same id is defined to be a no-op replay of the
old result (v0/runner.py's whole resumability contract). So a repair
attempt dispatches under a *derived* id
(``"<node id>~repair<attempt>"``, attempt = ``node.attempts + 1``, reusing
the same counter v1's round loop already increments on failed
submits/reviews) — a fresh id v0 has never seen, hence a fresh dispatch —
and then this module copies the resulting text over the real artifact path
once it's confirmed good. This is also what makes the derived id
deterministic across a crash mid-repair: retrying with the same
``node.attempts`` value lands on the same id, so v0's own resume machinery
still applies to the repair dispatch itself.

**The guardrail this preserves** (§4.6: "the assembler's file tools are
read-only over ``out/``... otherwise the assembler 'helpfully' edits
content to make the build green and you ship a passing compile over
corrupted content that already passed review"): the *assembler* (compile.py,
checks.py, assemble.py) never writes into ``out/`` itself. Only a repair
*writer* — dispatched here, through gates and review exactly like any other
writer node — is allowed to change a passed artifact, and only after its
output re-clears both bars.

Snapshotting (§4.6: "snapshot to ``out/.versions/`` before any repair" —
"if the amendment was itself the mistake, you'll only realize it three
chapters in") happens unconditionally, before the repair writer is even
dispatched, so a bad repair or a bad contract amendment is always
recoverable.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..adapters.base import AgentAdapter
from ..environment.base import Environment
from ..types import EpisodeBudget
from ..v0.events import EventLog
from ..v0.run_dir import node_artifact_path, write_text_atomic
from ..v1.gates import GateResult, all_passed, evaluate_gates, write_gate_cache
from ..v1.manifest import append_manifest_line
from ..roles.protocol import RoleProvider
from ..v1.reviewer import ReviewVerdict, review_node
from ..v1.tree import NodeBudget, TaskNode, TaskTree
from ..v1.writer import run_writer_node
from .run_dir import ensure_audit_path, version_snapshot_path

RepairMode = Literal["patch", "regenerate"]

_PATCH_INSTRUCTION = (
    "Make the MINIMAL change necessary to fix the following scoped defect. "
    "Do not rewrite, rephrase, or restructure anything else in the artifact."
)
_REGENERATE_INSTRUCTION = (
    "The artifact below no longer satisfies the current rules and must be "
    "rewritten. Reason:"
)


def repair_node_id(node_id: str, attempt: int) -> str:
    return f"{node_id}~repair{attempt}"


def build_repair_prompt(node: TaskNode, defect: str, current_text: str, mode: RepairMode) -> str:
    instruction = _PATCH_INSTRUCTION if mode == "patch" else _REGENERATE_INSTRUCTION
    return (
        f"Original brief: {node.brief}\n\n"
        f"{instruction}\n{defect}\n\n"
        f"Current artifact:\n---\n{current_text}\n---\n\n"
        "Produce the full corrected artifact text as your final answer."
    )


def _clear_parts_dir(run_dir: str | Path, node_id: str) -> None:
    """Remove part files so a single-file write becomes the resolved artifact.

    PLAN-SWEEP-REPAIR.md §B4 option (a) principle: ``node_artifact_text``
    resolves parts-win, so any full-text write (repair promotion, failed
    repair restore) must clear ``out/<node>/*.md`` first or be shadowed
    into a silent no-op."""
    import contextlib

    from ..v0.run_dir import node_parts_dir

    parts_d = node_parts_dir(run_dir, node_id)
    if parts_d.is_dir():
        for existing in list(parts_d.glob("*.md")):
            with contextlib.suppress(OSError):
                existing.unlink()


def _resolved_text(run_dir: str | Path, node_id: str) -> str:
    """Current artifact text under either layout (single file or parts)."""
    from ..v0.run_dir import node_artifact_text

    try:
        return node_artifact_text(run_dir, node_id)
    except (FileNotFoundError, OSError):
        return ""


def snapshot_artifact(run_dir: str | Path, node_id: str, tag: str) -> Path | None:
    run_dir = Path(run_dir)
    # PLAN-SWEEP-REPAIR.md §B: resolve via node_artifact_text — a snapshot
    # of the single file alone is empty the moment parts exist.
    current = _resolved_text(run_dir, node_id)
    if not current.strip():
        return None
    snapshot_path = version_snapshot_path(run_dir, node_id, tag)
    snapshot_path.write_text(current, encoding="utf-8")
    return snapshot_path


@dataclass
class RepairOutcome:
    node_id: str
    repair_id: str
    mode: RepairMode
    passed: bool
    gate_results: list[GateResult]
    verdict: ReviewVerdict
    snapshot_path: Path | None
    status: str


async def run_repair(
    run_dir: str | Path,
    node: TaskNode,
    tree: TaskTree,
    tree_path: str | Path,
    manifest_path: str | Path,
    defect: str,
    adapter: AgentAdapter,
    env: Environment,
    budget: EpisodeBudget,
    provider: RoleProvider,
    log: EventLog,
    *,
    mode: RepairMode = "patch",
    max_attempts: int = 3,
    disable_review: bool = False,
) -> RepairOutcome:
    """Dispatch one scoped repair for ``node`` and transition it in
    ``tree`` — ``"passed"`` again if the repair clears gates and review,
    ``"stale"`` (retryable) or ``"blocked"`` (attempts exhausted,
    escalate — same threshold v1 uses, §4.5: "Three failed submits →
    escalate") otherwise. Caller is expected to pass ``tree.nodes[node.id]``
    so mutation is visible on the tree it also saves."""
    run_dir = Path(run_dir)
    attempt = node.attempts + 1
    repair_id = repair_node_id(node.id, attempt)

    snapshot_path = snapshot_artifact(run_dir, node.id, repair_id)
    current_text = _resolved_text(run_dir, node.id) if snapshot_path else ""

    repair_node = TaskNode(
        id=repair_id,
        brief=node.brief,
        artifact=f"out/{repair_id}.md",
        gates=list(node.gates),
        type=node.type,
        shape=node.shape,
        tools=list(node.tools),
        budget=NodeBudget(tokens=node.budget.tokens, calls=node.budget.calls),
    )
    prompt = build_repair_prompt(node, defect, current_text, mode)
    result, promotion = await run_writer_node(run_dir, repair_node, prompt, adapter, env, budget)

    candidate_path = node_artifact_path(run_dir, repair_id)
    candidate_text = candidate_path.read_text(encoding="utf-8") if candidate_path.exists() else ""

    from ..pipeline.bypass import is_node_bypassed
    from ..pipeline.corruption import check_artifact_text_corruption

    bypassed = is_node_bypassed(run_dir, node.id, "review") or is_node_bypassed(run_dir, node.id) or is_node_bypassed(run_dir, repair_id)

    episode_ok = result.status == "done" and (bool(candidate_text.strip()) or bypassed)
    gate_results = evaluate_gates(node.gates, candidate_text) if (result.status == "done" and bool(candidate_text.strip())) else []
    gates_ok = episode_ok and (all_passed(gate_results) or bypassed)

    verdict = ReviewVerdict(node_id=node.id, items=[], verdict="fail")
    if gates_ok:
        if bypassed:
            corrupted, reason = check_artifact_text_corruption(candidate_text, node=node)
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
        elif disable_review:
            verdict = ReviewVerdict(node_id=node.id, items=[], verdict="pass")
        else:
            verdict = review_node(node, candidate_text, provider)
        # §11.10.11: the repair re-evaluated gates against a *new* candidate
        # — overwrite the dispatch-time cache, and merge (never clobber) it
        # when writing the verdict to the same audit file.
        audit = ensure_audit_path(run_dir, node.id)
        write_gate_cache(audit, gate_results)
        existing: dict = {}
        try:
            loaded = json.loads(audit.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
        existing.update({"items": verdict.items, "verdict": verdict.verdict, "node": node.id})
        audit.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    passed = gates_ok and verdict.verdict == "pass"
    log.append(
        {
            "node_id": node.id,
            "role": "harness",
            "round": 0,
            "type": "node_repaired",
            "repair_id": repair_id,
            "mode": mode,
            "defect": defect,
            "passed": passed,
        }
    )

    if passed:
        _clear_parts_dir(run_dir, node.id)
        node_artifact_path(run_dir, node.id).write_text(candidate_text, encoding="utf-8")
        node.status = "passed"
        artifact_text = candidate_text
    else:
        node.attempts += 1
        node.status = "blocked" if node.attempts >= max_attempts else "stale"
        if snapshot_path is not None:
            shutil.copy2(snapshot_path, node_artifact_path(run_dir, node.id))
            _clear_parts_dir(run_dir, node.id)
        artifact_text = _resolved_text(run_dir, node.id)

    append_manifest_line(
        manifest_path,
        node_id=node.id,
        artifact_path=str(node_artifact_path(run_dir, node.id)),
        artifact_text=artifact_text,
        gate_results=gate_results,
        promotion=promotion,
    )
    tree.save(tree_path)

    return RepairOutcome(
        node_id=node.id,
        repair_id=repair_id,
        mode=mode,
        passed=passed,
        gate_results=gate_results,
        verdict=verdict,
        snapshot_path=snapshot_path,
        status=node.status,
    )
