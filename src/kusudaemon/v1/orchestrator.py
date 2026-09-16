"""Orchestrator role (PLAN.md §3, §13 v1 scope).

Stateless per round: fresh context every round, rebuilt from disk, one
small schema-constrained JSON decision, discarded. Cost is bounded by the
ready-set size, **not** the tree size (PLAN-zeromem.md §1.8): ``_compact_state``
lists every ready node in full and only per-status counts of the rest, so a
400-node tree costs the same per round as a 20-node one — a few K tokens,
not ~40K.

This is *not* where "done" gets decided. PLAN.md invariant 1: only the
harness writes ``status: passed``, and only after gates evaluate — so a
"dispatch" decision naming a node id outside the harness-computed ready set
is corrected by code, not trusted (invariant 2: "gated by code, not by
model judgment").

PLAN-zeromem.md §1 makes that whole call optional: ``decide_next_action_*
deterministic`` performs the same halt/escalate arbitration (already
code-side) and picks the first ready node — which, by construction
(``v2/planner.py`` writes tree.json in spine order and leaves carry
``depends_on=[]``), is the earliest unfinished node in document order.

PLAN-SWEEP-REPAIR.md §L adds the event-driven orchestrator
(``dispatch_policy="orchestrator"``, the default): the one agent whose only
job is deciding when to dispatch or redispatch. It is consulted every time a
node finishes (and once at the start), is told what finished and how, sees
what is in flight and what is dispatchable, and answers ``dispatch`` (up to
the free slots) or ``wait`` (naming the in-flight nodes it is waiting on).
Failed nodes come back to it instead of retrying in place. Terminal states —
all passed, or nothing ready and nothing in flight — stay code-decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..roles.protocol import RoleProvider
from .manifest import read_manifest_tail
from .tree import TaskTree

DispatchPolicy = Literal["orchestrator", "model", "document_order", "deterministic"]

DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "reason"],
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["dispatch", "halt", "escalate"]},
        "node_id": {"type": "string"},
        "reason": {"type": "string", "maxLength": 400},
    },
}

_SYSTEM_PROMPT = (
    "You are the Orchestrator in a long-horizon task harness. You never see "
    "node content, only compact state: node ids, status, one-line briefs, "
    "and a short manifest tail. Each round you pick exactly one ready node "
    "to dispatch next, or halt (nothing new to do), or escalate (nothing is "
    "ready and nothing is in flight — the run is stuck). You do not decide "
    "whether a node's work is correct; gates and the reviewer do that. "
    "Respond with a single JSON object only."
)


@dataclass
class DispatchDecision:
    action: str
    node_id: str | None
    reason: str


def _arbitrate_empty_ready(tree: TaskTree) -> DispatchDecision:
    """The three no-ready branches shared by the model and deterministic
    dispatch paths (PLAN-zeromem.md §1.3): all of this arbitration was
    already code-side, never model judgment."""
    if tree.is_complete():
        return DispatchDecision(action="halt", node_id=None, reason="all nodes passed")
    if tree.is_blocked():
        return DispatchDecision(
            action="escalate",
            node_id=None,
            reason="no ready nodes and nothing in flight",
        )
    # Nodes are mid-flight (round_loop resolves these synchronously
    # before ever reaching this call in the current single-process
    # loop) — nothing new to hand out this round.
    return DispatchDecision(
        action="halt", node_id=None, reason="nodes in flight; nothing new to dispatch"
    )


def decide_next_action(
    tree: TaskTree,
    manifest_path: str,
    provider: RoleProvider,
    *,
    round_index: int,
    max_parallel: int = 1,
) -> DispatchDecision:
    ready = tree.ready_nodes()
    if not ready:
        return _arbitrate_empty_ready(tree)
    if len(ready) == 1:
        # PLAN-AUDIT.md §E18: with exactly one ready node, dispatch is the
        # only legal outcome (§11.5, enforced below for the multi-ready
        # case too) and there is only one node id it could legally name —
        # the model call's answer is already fully determined by code, so
        # spending it is pure waste. For a serial T2/T3 tree that's one
        # provider call per leaf whose outcome was never in question.
        # Purely additive: the multi-ready path below is unchanged.
        return DispatchDecision(
            action="dispatch",
            node_id=ready[0],
            reason="code-decided: single ready node, no model call needed",
        )
    if max_parallel >= len(ready):
        return DispatchDecision(
            action="dispatch",
            node_id=ready[0],
            reason=f"code-decided: wave consumes the entire ready set (max_parallel={max_parallel} >= ready={len(ready)})",
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _compact_state(tree, ready, manifest_path, round_index)},
    ]
    payload = provider.complete_json(messages, DISPATCH_SCHEMA)
    action = payload.get("action", "dispatch")
    node_id = payload.get("node_id")
    reason = str(payload.get("reason", ""))

    if action == "dispatch" and node_id not in ready:
        fallback = ready[0]
        reason = (
            f"orchestrator named non-ready node {node_id!r}; "
            f"harness fell back to {fallback!r} ({reason})".strip()
        )
        node_id = fallback
    elif action != "dispatch":
        # §11.5: with ready nodes on the table, dispatch is the only legal
        # action — a model answering "halt"/"escalate" ends the execute
        # phase early and the driver reports a fake "done". Coerce, and
        # record the coercion exactly like the non-ready id fallback.
        fallback = ready[0]
        reason = (
            f"orchestrator answered {action!r} with ready nodes present; "
            f"harness coerced to dispatch of {fallback!r} ({reason})".strip()
        )
        action = "dispatch"
        node_id = fallback

    return DispatchDecision(action=action, node_id=node_id, reason=reason)


def decide_next_action_deterministic(
    tree: TaskTree,
    *,
    round_index: int,
) -> DispatchDecision:
    """Zero-token dispatch (PLAN-zeromem.md §1): the same halt/escalate
    arbitration ``decide_next_action`` already performs in code, plus
    document-order selection instead of a model call.

    ``ready_nodes()`` returns ids in ``TaskTree.nodes`` iteration order,
    which is ``tree.json`` array order, which ``v2/planner.py`` wrote in
    spine order — so ``ready[0]`` is the earliest unfinished node in
    document order. Leaves carry ``depends_on=[]`` by construction, so no
    dependency information is being discarded by not asking a model.
    """
    ready = tree.ready_nodes()
    if not ready:
        return _arbitrate_empty_ready(tree)
    return DispatchDecision(
        "dispatch", ready[0], f"document-order policy (round {round_index})"
    )


def decide_next_action_with_policy(
    tree: TaskTree,
    manifest_path: str,
    provider: RoleProvider | None,
    *,
    round_index: int,
    policy: DispatchPolicy = "model",
    max_parallel: int = 1,
) -> DispatchDecision:
    """The single dispatcher both the round loop and tests go through: the
    ``"document_order"`` path spends zero tokens, the ``"model"`` path is
    today's call unchanged. Any other spelling is a real error (the old
    fallback silently spent the per-round calls ``document_order`` exists
    to avoid), so it raises instead of dispatching (§11.11).

    PLAN-AUDIT.md §E5: the dashboard's new-run modal offers ``"deterministic"``
    as a dispatch-policy choice (readable label for the zero-token path), and
    an already-on-disk ``run.spec.json`` may carry that spelling from before
    this was noticed. Treated as a backward-compatible alias for
    ``"document_order"`` — identical zero-call code path, not just a
    string-coercion that happens to avoid the ``ValueError``."""
    if policy in ("document_order", "deterministic"):
        return decide_next_action_deterministic(tree, round_index=round_index)
    if policy != "model":
        raise ValueError(
            f"unrecognized dispatch policy {policy!r} (model | document_order | deterministic)"
        )
    if provider is None:
        raise ValueError("policy='model' requires a provider")
    return decide_next_action(
        tree, manifest_path, provider, round_index=round_index, max_parallel=max_parallel
    )


def _compact_state(tree: TaskTree, ready: list[str], manifest_path: str, round_index: int) -> str:
    """Round state bounded by the ready-set size, not the tree size
    (PLAN-zeromem.md §1.8): every ready node in full, per-status counts for
    everything else. The model only needs to choose among ready nodes — the
    global picture beyond status counts buys nothing and costs ~40K tokens
    per round at ``DEFAULT_NODE_CAP = 400``."""
    lines = [f"round: {round_index}", ""]
    lines.append("ready nodes (pick one):")
    for node_id in ready:
        node = tree.nodes[node_id]
        lines.append(
            f"- {node.id} deps={node.depends_on} attempts={node.attempts} "
            f":: {node.brief[:80]}"
        )
    counts = _count_statuses(tree)
    lines += ["", f"rest of tree: {counts}"]
    tail = read_manifest_tail(manifest_path, n=5)
    if tail:
        lines.append("")
        lines.append("recent manifest:")
        for entry in tail:
            promotion = str(entry.get("promotion", ""))[:120]
            lines.append(f"- {entry.get('node')}: gates={entry.get('gates')} promotion={promotion}")
    return "\n".join(lines)


def _count_statuses(tree: TaskTree) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in tree.nodes.values():
        counts[node.status] = counts.get(node.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# PLAN-SWEEP-REPAIR.md §L — the event-driven orchestrator
# ---------------------------------------------------------------------------

import os as _os


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _os.getenv(name)
    try:
        return max(minimum, int(raw)) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _os.getenv(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def orchestrator_max_tokens() -> int:
    """§L.2 output cap. ``provider._default_max_tokens`` gives any schema with a
    non-scalar property 4096; the legacy scalar dispatch schema got 1024.

    §P3: raised 1024 -> 4096 to match that peer value. ORCHESTRATOR_SCHEMA has
    arrays, so 1024 was never the right bucket for it, and on a reasoning model
    the cap covers the thinking trace too: every orchestrator call in the
    2026-09-12 000-week sweep returned exactly 1024 completion tokens and no
    parseable decision, surfacing as "no answer within 90s" and a document-order
    fallback in all three seeds. ``complete_json`` escalates from here if a
    response still stops at the ceiling; the deadline
    (KUSUDAEMON_ORCHESTRATOR_DEADLINE_S, 90s) bounds the wall clock either way.
    """
    return _env_int("KUSUDAEMON_ORCHESTRATOR_MAX_TOKENS", 4096)


def orchestrator_deadline_s() -> float:
    """§L.2: how long a free slot may wait on one decision before the harness
    dispatches in document order instead. ``<= 0`` disables the deadline."""
    if _os.getenv("KUSUDAEMON_ORCHESTRATOR_DEADLINE_S"):
        return _env_float("KUSUDAEMON_ORCHESTRATOR_DEADLINE_S", 90.0)
    cap = orchestrator_max_tokens()
    min_tps = float(_os.getenv("KUSUDAEMON_MIN_DECODE_TPS", "15"))
    return max(90.0, cap / min_tps)


def orchestrator_max_listed() -> int:
    """§L.3: per-section cap on listed nodes/events, so a call's input is
    bounded by a constant instead of by the ready set (PLAN-zeromem.md §1.8)."""
    return _env_int("KUSUDAEMON_ORCHESTRATOR_MAX_LISTED", 20)


# §L.2: every property is required and no string-length keywords are used, so
# the schema is valid under ``response_format: json_schema, strict: true``
# (strict endpoints reject optional properties). Unused arrays are sent empty.
ORCHESTRATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "node_ids", "wait_on", "reason"],
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["dispatch", "wait"]},
        "node_ids": {"type": "array", "items": {"type": "string"}},
        "wait_on": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}

_REASON_CAP = 600

ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are the Orchestrator of a long-horizon task harness. Your only job is "
    "deciding when to dispatch or redispatch nodes. Writers do the work; gates "
    "and a reviewer judge it; you never see node content and never judge "
    "correctness.\n\n"
    "You are called at the start of execution and again every time a node "
    "finishes. Each call tells you what just finished and how, your previous "
    "decision, what is running, and which nodes can be dispatched now. Answer "
    "with exactly one JSON object, always including all four fields:\n"
    '- {"action": "dispatch", "node_ids": [...], "wait_on": [], "reason": "..."} '
    "— start these nodes now. Name only nodes listed as dispatchable, at most "
    "as many as there are free slots.\n"
    '- {"action": "wait", "node_ids": [], "wait_on": [...], "reason": "..."} — '
    "start nothing now. Name the running node(s) whose result you need first. "
    "You cannot wait when nothing is running: nothing would ever wake you.\n\n"
    "Rules and guidance:\n"
    "- Free slots are filled unless you say why not. If you dispatch fewer "
    "nodes than there are free slots while other nodes are dispatchable, put "
    "the running or just-dispatched node(s) you are waiting for in wait_on; "
    "with wait_on empty the harness fills the remaining slots in document order.\n"
    "- A failed node reappears as dispatchable with its defect and attempt "
    "count. Redispatch it, run other work first, or wait for a running node "
    "whose output it needs. The harness stops a node at its attempt limit.\n"
    "- Declared dependencies are already enforced: nodes held by them are "
    "listed separately and are not dispatchable. You may also hold a "
    "dispatchable node back for a dependency nobody declared, but only by "
    "naming the running node it waits on.\n"
    "- Long lists are truncated; unlisted dispatchable nodes come later in "
    "document order and remain valid choices.\n"
    "Respond with the JSON object only."
)


@dataclass
class NodeEvent:
    """One node finishing, as the orchestrator is told about it."""

    node_id: str
    outcome: str  # passed | failed | blocked | throttled | split | crashed | stopped
    attempts: int
    defect: str = ""
    duration_s: float | None = None


@dataclass
class OrchestratorDecision:
    action: str  # "dispatch" | "wait"
    node_ids: list[str]
    wait_on: list[str]
    reason: str
    corrections: list[str]

    def as_record(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "node_ids": list(self.node_ids),
            "wait_on": list(self.wait_on),
            "reason": self.reason,
            "corrections": list(self.corrections),
        }


def classify_node_event(
    node: Any, *, duration_s: float | None = None, crashed: BaseException | None = None
) -> NodeEvent:
    if crashed is not None:
        outcome = "crashed"
        defect = f"{type(crashed).__name__}: {crashed}"
    else:
        status = getattr(node, "status", "")
        defect = getattr(node, "last_defect", "") or ""
        if status == "passed":
            outcome, defect = "passed", ""
        elif status == "split":
            outcome = "split"
        elif status == "blocked":
            outcome = "blocked"
        elif status == "pending" and "throttled" in defect:
            outcome = "throttled"
        elif status == "pending":
            outcome = "failed"
        else:
            outcome = "stopped"
    return NodeEvent(
        node_id=node.id,
        outcome=outcome,
        attempts=int(getattr(node, "attempts", 0) or 0),
        defect=defect,
        duration_s=duration_s,
    )


def _describe_event(event: NodeEvent, max_attempts: int) -> str:
    took = f" after {event.duration_s:.0f}s" if event.duration_s is not None else ""
    if event.outcome == "passed":
        return f"- {event.node_id} PASSED{took}"
    if event.outcome == "failed":
        return (
            f"- {event.node_id} FAILED attempt {event.attempts} of {max_attempts}{took}: "
            f"{event.defect[:300]}"
        )
    if event.outcome == "blocked":
        return (
            f"- {event.node_id} BLOCKED — attempt limit ({max_attempts}) reached{took}; "
            f"last defect: {event.defect[:300]}"
        )
    if event.outcome == "throttled":
        return f"- {event.node_id} THROTTLED by the provider{took} (not counted as an attempt)"
    if event.outcome == "split":
        return f"- {event.node_id} SPLIT into child nodes{took}"
    if event.outcome == "crashed":
        return f"- {event.node_id} CRASHED in the harness{took}: {event.defect[:300]}"
    return f"- {event.node_id} stopped{took} with status {event.outcome}"


def _describe_previous(previous: dict[str, Any] | None) -> str:
    if not previous:
        return "- none in this run yet"
    age = previous.get("age_s")
    when = f" ({age:.0f}s ago)" if isinstance(age, (int, float)) else ""
    head = f"- decision {previous.get('round', '?')}{when}: {previous.get('action')}"
    if previous.get("node_ids"):
        head += f" {previous['node_ids']}"
    if previous.get("wait_on"):
        head += f", waiting on {previous['wait_on']}"
    lines = [head, f"  reason: {str(previous.get('reason', ''))[:300]}"]
    if previous.get("corrections"):
        lines.append(f"  harness corrections: {'; '.join(map(str, previous['corrections']))[:300]}")
    return "\n".join(lines)


def build_orchestrator_messages(
    tree: TaskTree,
    *,
    events: list[NodeEvent],
    ready: list[str],
    in_flight: list[tuple[str, float]],
    free_slots: int,
    max_slots: int,
    max_attempts: int,
    manifest_path: str,
    decision_index: int,
    first_call: bool,
    previous_decision: dict[str, Any] | None = None,
    max_listed: int | None = None,
) -> list[dict[str, str]]:
    """Compact state for one orchestrator call, bounded by a constant.

    §L.3: each list is capped at ``max_listed`` entries (retry candidates
    first, then document order), with the overflow stated as a count, so the
    input per call no longer grows with the ready set and a run's total
    orchestrator input is O(completions), not O(completions x ready).
    """
    cap = max_listed if max_listed is not None else orchestrator_max_listed()
    lines = [f"decision: {decision_index}", f"free slots: {free_slots} of {max_slots}", ""]

    lines.append("what just happened:")
    if events:
        shown = events[-cap:]
        omitted = events[:-cap] if len(events) > cap else []
        if omitted:
            tally: dict[str, int] = {}
            for e in omitted:
                tally[e.outcome] = tally.get(e.outcome, 0) + 1
            lines.append(f"- … {len(omitted)} earlier completions not listed: {tally}")
        lines += [_describe_event(e, max_attempts) for e in shown]
    elif first_call:
        lines.append("- execution is starting; nothing has run yet in this process")
    else:
        lines.append("- no node has finished since your last decision")
    lines.append("")

    lines.append("your previous decision:")
    lines.append(_describe_previous(previous_decision))
    lines.append("")

    lines.append(f"running now ({len(in_flight)}):")
    if in_flight:
        for node_id, elapsed in in_flight[:cap]:
            node = tree.nodes.get(node_id)
            attempt = (node.attempts + 1) if node is not None else "?"
            brief = node.brief[:120] if node is not None else ""
            lines.append(f"- {node_id} running {elapsed:.0f}s, attempt {attempt} :: {brief}")
        if len(in_flight) > cap:
            lines.append(f"- … {len(in_flight) - cap} more running")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"dispatchable now ({len(ready)}):")
    retries = [i for i in ready if tree.nodes[i].attempts > 0]
    fresh = [i for i in ready if tree.nodes[i].attempts == 0]
    listed = (retries + fresh)[:cap]
    for node_id in listed:
        node = tree.nodes[node_id]
        defect = f' last_defect="{node.last_defect[:200]}"' if node.last_defect else ""
        lines.append(
            f"- {node.id} attempts={node.attempts}/{max_attempts}{defect} :: {node.brief[:160]}"
        )
    if len(ready) > len(listed):
        lines.append(
            f"- … {len(ready) - len(listed)} more dispatchable, not listed (later in document order)"
        )
    if not ready:
        lines.append("- (none)")

    held = [
        n for n in tree.nodes.values()
        if n.status == "pending" and n.id not in ready and n.depends_on
    ]
    if held:
        lines.append("")
        lines.append(f"held by declared dependencies ({len(held)}):")
        for node in held[:cap]:
            deps = ", ".join(
                f"{d} ({tree.nodes[d].status if d in tree.nodes else 'missing'})" for d in node.depends_on
            )
            lines.append(f"- {node.id} waits on {deps} :: {node.brief[:80]}")
        if len(held) > cap:
            lines.append(f"- … {len(held) - cap} more held")

    lines += ["", f"all nodes by status: {_count_statuses(tree)}"]
    tail = read_manifest_tail(manifest_path, n=5)
    if tail:
        lines.append("")
        lines.append("recent manifest:")
        for entry in tail:
            lines.append(f"- {entry.get('node')}: gates={entry.get('gates')}")
    return [
        {"role": "system", "content": ORCHESTRATOR_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def fallback_decision_payload(ready: list[str], free_slots: int, why: str) -> dict[str, Any]:
    """Document order for one decision, when the orchestrator did not answer."""
    return {
        "action": "dispatch",
        "node_ids": list(ready[: max(0, free_slots)]),
        "wait_on": [],
        "reason": f"harness fallback (document order): {why}",
    }


def parse_orchestrator_decision(
    payload: dict[str, Any],
    *,
    ready: list[str],
    in_flight: list[str],
    free_slots: int,
) -> OrchestratorDecision:
    """Validate a decision against the harness-computed state; correct by code.

    Invariant 2 ("gated by code, not by model judgment") applied to the two
    actions: a dispatch may only name dispatchable nodes and only fill free
    slots; a wait must name running nodes, and waiting with nothing running
    is refused because nothing would ever wake the orchestrator again. §L.6:
    a dispatch that leaves slots free while work is dispatchable must name
    what it is waiting on, or code fills the slots. Every correction is
    recorded on the decision.
    """
    action = str(payload.get("action", "dispatch"))
    reason = str(payload.get("reason", ""))[:_REASON_CAP]
    corrections: list[str] = []
    free_slots = max(0, free_slots)
    named_waits = [str(x) for x in (payload.get("wait_on") or [])]

    def first_ready() -> OrchestratorDecision:
        return OrchestratorDecision("dispatch", ready[:1], [], reason, corrections)

    if action == "wait":
        if payload.get("node_ids"):
            corrections.append(f"ignored node_ids {payload.get('node_ids')!r} on a wait")
        valid = [x for x in dict.fromkeys(named_waits) if x in in_flight]
        if not in_flight:
            if ready and free_slots > 0:
                corrections.append(
                    "wait with nothing running would never wake; harness dispatched "
                    f"{ready[0]!r}"
                )
                return first_ready()
            corrections.append("wait with nothing running and nothing dispatchable")
            return OrchestratorDecision("wait", [], [], reason, corrections)
        if not valid:
            corrections.append(
                f"wait named no running node ({named_waits!r}); harness waits on all running nodes"
            )
            valid = list(in_flight)
        elif len(valid) != len(set(named_waits)):
            corrections.append(f"dropped non-running wait targets {sorted(set(named_waits) - set(valid))!r}")
        return OrchestratorDecision("wait", [], valid, reason, corrections)

    if action != "dispatch":
        corrections.append(f"unknown action {action!r} treated as dispatch")
    named = [str(x) for x in (payload.get("node_ids") or [])]
    unique = list(dict.fromkeys(named))
    valid = [x for x in unique if x in ready]
    if len(valid) != len(unique):
        corrections.append(f"dropped non-dispatchable node ids {sorted(set(unique) - set(valid))!r}")
    if len(valid) > free_slots:
        corrections.append(f"trimmed dispatch to {free_slots} free slot(s); dropped {valid[free_slots:]!r}")
        valid = valid[:free_slots]
    if not valid:
        if in_flight:
            corrections.append("dispatch named nothing dispatchable; harness waits on all running nodes")
            return OrchestratorDecision("wait", [], list(in_flight), reason, corrections)
        if ready and free_slots > 0:
            corrections.append(
                f"dispatch named nothing dispatchable and nothing is running; harness dispatched {ready[0]!r}"
            )
            return first_ready()
        return OrchestratorDecision("wait", [], [], reason, corrections)

    # §L.6: slots left free while work is dispatchable need a stated reason.
    remaining = [x for x in ready if x not in valid]
    idle = free_slots - len(valid)
    wait_targets = [x for x in dict.fromkeys(named_waits) if x in in_flight or x in valid]
    if idle > 0 and remaining:
        if wait_targets:
            return OrchestratorDecision("dispatch", valid, wait_targets, reason, corrections)
        fill = remaining[:idle]
        corrections.append(
            f"left {idle} slot(s) free with no wait_on; harness filled them in document order with {fill!r}"
        )
        return OrchestratorDecision("dispatch", valid + fill, [], reason, corrections)
    return OrchestratorDecision("dispatch", valid, [], reason, corrections)
