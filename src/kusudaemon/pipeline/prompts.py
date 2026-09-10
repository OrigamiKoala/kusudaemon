"""Prompt assembly for writers (PLAN.md §11 "default node view: brief").

A Writer node's prompt is assembled entirely before its bounded episode
starts (§8 context discipline): brief, then the frozen contract (every
artifact must satisfy it — §4.4), then the node's ``inputs`` — materialized
spine-unit file paths under ``spine/`` (PLAN-zeromem.md §7) and, once v4
research ran, finding file paths — the agent is expected to read itself.
Nothing here is a model call; ``inputs`` are file paths the agent resolves
with its own tools. (Pre-§7 or unmaterialized runs fall back to a bare
unit id — see ``v2/survey.py:unit_input_path`` — which renders the same
way here; the agent just has nothing to open.)

If the node declares ``depends_on`` nodes, the promotions of those nodes
(their capped, writer-authored handoffs, ``manifest.jsonl``) are injected
so a downstream node knows what its upstream actually delivered — closing
the loop ``v1/writer.py``'s prompt promises ("the only part of your work
another node will ever see"). No document-order fallback: with
``depends_on=[]`` everywhere (today's trees) this block is simply absent,
and a wrong heuristic is worse than nothing (PLAN-zeromem.md §11.2).

Finally, if the node carries a ``last_defect`` from a prior failed attempt
(PLAN-zeromem.md §9), it's appended as a retry block — always patch framing
on an in-place retry (a mid-series rewrite burns attempts on a fresh
artifact that must re-clear the same gates); regenerate framing applies
only to an operator redispatch, which resets the node to a fresh attempt
budget and stamps ``last_defect`` with the marker
``dashboard/state.py``'s redispatch job writes (§D31, 2026-08-15).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from ..adapters.cli_agent import (
    _hidden_path_exceptions_block,
    _hidden_paths_notice_block,
)
from ..v0.run_dir import spec_path
from ..v1.gates import estimate_tokens
from ..v1.manifest import read_all_manifest_entries
from ..v1.reviewer import DEFAULT_ARTIFACT_CAP_TOKENS, cap_artifact_text
from ..v1.tree import TaskNode
from ..v2.contract import load_contract
from ..v2.retrieval import DEFAULT_TOP_K, retrieve_spans, top_k_for_budget
from ..v2.run_dir import contract_path
from ..v2.survey import load_spine
from ..tokens import count_file_tokens
from .corruption import is_artifact_corrupted
from .run_dir import resolve_stored

_PATCH_RETRY_INSTRUCTION = (
    "Your previous attempt at this node failed with the feedback below. Make "
    "the MINIMAL change necessary to fix it — do not rewrite or restructure "
    "anything else:\n"
)
_REGENERATE_RETRY_INSTRUCTION = (
    "Your previous attempts at this node failed with the feedback below, and "
    "a small patch has not been enough. Rewrite the artifact from scratch to "
    "address it:\n"
)

# PLAN-zeromem.md §11.4: contract.md is frozen by construction and only two
# code paths ever write it, so cache it per (path, stat stamp) instead of
# re-reading on every node's prompt. The stat stamp keeps an amendment from
# serving a stale contract: amend_contract rewrites the file, the stamp
# changes, the next read re-parses. ``_MISSING`` is a sentinel distinct
# from every real stamp (a missing file's stamp is None).
# §11.10.15: bounded — one entry per run directory, FIFO-evicted, and the
# dict is only ever touched under the lock.
_MISSING = object()
_CONTRACT_CACHE_MAX = 64
_contract_cache: dict[str, tuple] = {}
_contract_lock = threading.Lock()


def _load_contract_cached(run_dir: Path) -> str:
    path = contract_path(run_dir)
    key = str(path)
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None
    with _contract_lock:
        cached = _contract_cache.get(key, (_MISSING, _MISSING))
        if cached[0] == stamp:
            return cached[1]
    text = load_contract(run_dir)
    with _contract_lock:
        if key not in _contract_cache and len(_contract_cache) >= _CONTRACT_CACHE_MAX:
            del _contract_cache[next(iter(_contract_cache))]
        _contract_cache[key] = (stamp, text)
    return text


_SPEC_CACHE_MAX = 64
_spec_cache: dict[str, tuple] = {}
_spec_lock = threading.Lock()


def _load_spec_cached(run_dir: Path) -> str:
    """Same stat-stamp cache as ``_load_contract_cached`` above, over
    ``spec.md`` — written once by intake and read by every node's prompt
    (PLAN.md §D1)."""
    path = spec_path(run_dir)
    key = str(path)
    try:
        stat = os.stat(path)
        stamp = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None
    with _spec_lock:
        cached = _spec_cache.get(key, (_MISSING, _MISSING))
        if cached[0] == stamp:
            return cached[1]
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    with _spec_lock:
        if key not in _spec_cache and len(_spec_cache) >= _SPEC_CACHE_MAX:
            del _spec_cache[next(iter(_spec_cache))]
        _spec_cache[key] = (stamp, text)
    return text


def _section(spec_md: str, heading: str) -> str:
    if heading not in spec_md:
        return ""
    return spec_md.split(heading, 1)[1].split("\n##", 1)[0].strip()


def _goal_and_rubric_block(run_dir: Path) -> str:
    """PLAN.md §D1: a node's brief is often derived from a spine label or a
    planner slice and never repeats the operator's actual goal string —
    fatal on a corpus-less run, where the whole brief can otherwise be
    "produce the artifact for The goal" with no clue what that goal was.
    Renders goal + global rubric (and, once §A5 lands, unresolved
    objections) straight from the frozen spec.md."""
    spec_md = _load_spec_cached(run_dir)
    goal = _section(spec_md, "## Goal")
    if not goal:
        return ""
    lines = [f"Overall run goal (the reason this node exists at all): {goal}"]
    rubric = _section(spec_md, "## Global rubric")
    if rubric:
        lines.append(f"Global rubric:\n{rubric}")
    objections = _section(spec_md, "## Unresolved objections")
    if objections:
        lines.append(
            "Unresolved objections raised at intake — weigh these, do not "
            f"silently assume they were resolved:\n{objections}"
        )
    return "\n\n".join(lines)


def _artifact_instruction(
    node: TaskNode,
    run_dir: Path,
    *,
    is_workspace: bool = False,
    workspace_root: str | Path | None = None,
) -> str:
    """PLAN.md §D0: the artifact path appeared in no Writer prompt, in any
    tier, ever — the single file path any Writer was ever given was
    ``promotion.json``. ``node.artifact`` is the single source of truth
    (asserted at tree load, ``v1/tree.py``); render it absolute, not
    relative, because a relative path is only correct while the agent's cwd
    happens to equal the run directory (§D0b — workspace mode breaks that).

    PLAN-WORKSPACE-MODE.md §K3, §R2: Scope workspace deliverable framing to
    single-node fallback (SINGLE_NODE_ID, DIRECT_NODE_ID) behind
    KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT. Planner-built leaves stay untouched."""
    from ..v6.direct import DIRECT_NODE_ID, SINGLE_NODE_ID

    absolute_path = resolve_stored(run_dir, node.artifact)
    if (
        os.getenv("KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT") == "1"
        and is_workspace
        and node.id in (SINGLE_NODE_ID, DIRECT_NODE_ID)
    ):
        ws_display = str(workspace_root) if workspace_root else "<workspace_root>"
        instruction = (
            f"Your deliverables are the files your brief names, written in place under "
            f"{ws_display}. Write them with your file tools; they are what gets read "
            f"next.\n\n"
            f"When the work itself is complete, also write a short summary of what you "
            f"produced to `{absolute_path}` — the harness reads that summary, not "
            f"your workspace, when it assembles the run. The summary never substitutes "
            f"for the deliverables."
        )
    else:
        parts_dir = run_dir / "out" / node.id
        instruction = (
            f"Write your artifact to `{absolute_path}` using your file tools "
            "(e.g. save, patch, write, or edit). That file is the deliverable; nothing "
            "else you write or say is. When producing a long or multi-part document, you may "
            f"write part files under `{parts_dir}` (e.g. `part-01.md`, `units-001-020.md`, etc.), "
            "which will be concatenated in order, or write the single artifact directly. "
            "You may freely edit, revise, or delete sections as the work requires.\n"
            "Use plain ASCII punctuation throughout — straight quotes (' and \"), "
            "hyphens, and \"...\" rather than curly quotes, en/em dashes or a single-"
            "character ellipsis. Your edit tool matches text exactly, and a "
            "typographic character you cannot reproduce byte-for-byte later makes "
            "every edit that targets that line fail (PLAN-SWEEP-REPAIR.md \u00a7F6)."
        )
    if "refs_resolve" in node.gates or "refs_resolve" in node.warn_gates:
        claims_path = absolute_path.with_name(f"{node.id}_claims.jsonl")
        instruction += (
            " When making factual claims, cite them using `[ref:N]` anchors and "
            f"record them in `{claims_path}` as JSON lines with `assertion` and `ref`."
        )
    return instruction



_MANIFEST_CACHE_MAX = 64
_manifest_cache: dict[str, tuple[tuple[int, int] | None, dict[str, str]]] = {}
_manifest_lock = threading.Lock()


def _promotions_of(node: TaskNode, run_dir: Path) -> str:
    if not node.depends_on:
        return ""
    m_path = run_dir / "manifest.jsonl"
    key = str(m_path)
    try:
        stat = os.stat(m_path)
        stamp: tuple[int, int] | None = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        stamp = None

    with _manifest_lock:
        cached = _manifest_cache.get(key)
        if cached is not None and cached[0] == stamp:
            latest_by_node = cached[1]
        else:
            latest_by_node = None

    if latest_by_node is None:
        entries = read_all_manifest_entries(m_path)
        latest_by_node = {}
        for entry in entries:
            node_id = str(entry.get("node") or "").strip()
            promotion = str(entry.get("promotion") or "").strip()
            if node_id and promotion:
                latest_by_node[node_id] = promotion
        with _manifest_lock:
            if key not in _manifest_cache and len(_manifest_cache) >= _MANIFEST_CACHE_MAX:
                del _manifest_cache[next(iter(_manifest_cache))]
            _manifest_cache[key] = (stamp, latest_by_node)

    lines: list[str] = []
    for dep_id in node.depends_on:
        promotion = latest_by_node.get(dep_id)
        if promotion:
            lines.append(f"- [{dep_id}] {promotion}")
    return "\n".join(lines)


def _format_declared_inputs(node: TaskNode, run_dir: Path) -> str:
    """PLAN-TOKEN-ACCOUNTING.md §D: manifest table of declared inputs with token counts
    and ratios against leaf budget."""
    if not node.inputs:
        return ""
    leaf_budget = (
        node.budget.tokens
        if (node.budget and node.budget.tokens and node.budget.tokens > 0)
        else 30_000
    )
    lines = ["Declared inputs:"]
    has_entries = False

    for item in node.inputs:
        p = resolve_stored(run_dir, item)
        if not p.exists():
            if (run_dir / item).exists():
                p = run_dir / item
            elif Path(item).exists():
                p = Path(item)

        rel_name = item
        if p.is_dir():
            files = [f for f in p.rglob("*") if f.is_file()]
            n_files = len(files)
            total_bytes = sum(f.stat().st_size for f in files)
            total_tokens = sum(count_file_tokens(f) for f in files)
            if total_bytes >= 1024 * 1024:
                size_str = f"{total_bytes / (1024 * 1024):.1f} MB"
            elif total_bytes >= 1024:
                size_str = f"{total_bytes / 1024:.1f} KB"
            else:
                size_str = f"{total_bytes} B"
            ratio_str = ""
            if leaf_budget > 0 and total_tokens > 0:
                ratio = total_tokens / leaf_budget
                if ratio >= 1.0:
                    ratio_str = f"   ({ratio:.1f}x your leaf budget)"
                elif ratio >= 0.10:
                    ratio_str = f"   ({int(round(ratio * 100))}% of your leaf budget)"
            dir_label = rel_name if rel_name.endswith("/") else f"{rel_name}/"
            lines.append(f"  {dir_label:<24} {n_files} files, {size_str}  ~{total_tokens:,} tokens{ratio_str}")
            has_entries = True
        elif p.is_file():
            size_bytes = p.stat().st_size
            tokens = count_file_tokens(p)
            if size_bytes >= 1024 * 1024:
                size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
            elif size_bytes >= 1024:
                size_str = f"{size_bytes / 1024:.1f} KB"
            else:
                size_str = f"{size_bytes} B"
            ratio_str = ""
            if leaf_budget > 0 and tokens > 0:
                ratio = tokens / leaf_budget
                if ratio >= 1.0:
                    ratio_str = f"   ({ratio:.1f}x your leaf budget)"
                elif ratio >= 0.10:
                    ratio_str = f"   ({int(round(ratio * 100))}% of your leaf budget)"
            lines.append(f"  {rel_name:<24} {size_str:>8}  ~{tokens:,} tokens{ratio_str}")
            has_entries = True
        else:
            continue

    if not has_entries:
        return ""
    lines.append(f"Leaf budget: ~{leaf_budget:,} tokens.")
    return "\n".join(lines)


def segments(
    node: TaskNode,
    run_dir: str | Path,
    *,
    inline_spans: bool = True,
    top_k: int | None = None,
    hidden_paths: tuple[str, ...] = (),
    hidden_path_exceptions: tuple[str, ...] = (),
    resuming: bool = False,
    is_workspace: bool | None = None,
    workspace_root: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Return the ordered list of (label, text) segments making up a Writer's
    prompt (PLAN-EFFICIENCY-AND-HORIZON.md §L10)."""
    run_dir = Path(run_dir)
    if is_workspace is None:
        try:
            tier_file = run_dir / "tier.json"
            if tier_file.exists():
                tier_info = json.loads(tier_file.read_text(encoding="utf-8"))
                is_workspace = tier_info.get("kind") == "workspace"
                if not workspace_root:
                    workspace_root = tier_info.get("root")
            else:
                is_workspace = False
        except Exception:
            is_workspace = False

    segs: list[tuple[str, str]] = []

    def add(label: str, text: str) -> None:
        text = text.strip()
        if text:
            segs.append((label, text))

    goal_block = _goal_and_rubric_block(run_dir)
    if goal_block:
        add("goal_and_rubric", goal_block)
    contract = _load_contract_cached(run_dir).strip()
    if contract:
        add(
            "contract",
            "Global contract — every artifact you produce must satisfy it:\n" + contract,
        )
    add("hidden_paths", _hidden_paths_notice_block(hidden_paths))
    add("hidden_path_exceptions", _hidden_path_exceptions_block(hidden_path_exceptions))
    add(
        "artifact_instruction",
        _artifact_instruction(
            node,
            run_dir,
            is_workspace=bool(is_workspace),
            workspace_root=workspace_root,
        ),
    )
    if node.judgment and node.rubric:
        rubric_lines = "\n".join(
            f"- {judgment_id}: {node.rubric[judgment_id]}"
            for judgment_id in node.judgment
            if judgment_id in node.rubric
        )
        add("judgment_rubric", f"Judgment rubric the Reviewer will hold you to:\n{rubric_lines}")
    add("brief", f"Your brief: {node.brief}")
    manifest = _format_declared_inputs(node, run_dir)
    if manifest:
        add("declared_inputs", manifest)
    if os.getenv("KUSUDAEMON_CONTEXT_DISCLOSURE") == "1":
        leaf_budget = (
            node.budget.tokens
            if (node.budget and node.budget.tokens and node.budget.tokens > 0)
            else 30_000
        )
        add("context_disclosure", f"Context window usage notice: leaf token budget is ~{leaf_budget:,} tokens.")
    if node.inputs:
        def _abs(item: str) -> str:
            return str(resolve_stored(run_dir, item))

        if inline_spans:
            effective_top_k = (
                top_k
                if top_k is not None
                else top_k_for_budget(node.budget.tokens if node.budget and node.budget.tokens > 0 else 0)
            )
            spans_block = _retrieved_spans_block(node, run_dir, effective_top_k)
            if spans_block is not None:
                finding_paths = _non_unit_inputs(node, run_dir)
                if finding_paths:
                    add(
                        "inputs",
                        "Inputs (read them with your tools before writing, and "
                        "cite them where relevant):\n"
                        + "\n".join(f"- {_abs(item)}" for item in finding_paths),
                    )
                add("spans", spans_block)
            else:
                add(
                    "inputs",
                    "Inputs (read them with your tools before writing, and cite "
                    "them where relevant):\n"
                    + "\n".join(f"- {_abs(item)}" for item in node.inputs),
                )
        else:
            add(
                "inputs",
                "Inputs (read them with your tools before writing, and cite them "
                "where relevant):\n" + "\n".join(f"- {_abs(item)}" for item in node.inputs),
            )
    promotions = _promotions_of(node, run_dir)
    if promotions:
        add(
            "promotions",
            "Upstream nodes' handoffs (what the nodes you depend on actually "
            "delivered — read them before writing):\n" + promotions,
        )
    if node.last_defect:
        # §D31 (2026-08-15): an in-place retry is always patch-framed.
        # A mid-series "rewrite from scratch" burned the remaining attempts
        # on a fresh artifact that had to re-clear the same gates, and the
        # observed retries got *faster* each time, not more thorough.
        # Regenerate framing applies only to an operator redispatch where the
        # artifact is missing, empty, or corrupted (or explicit rewrite requested).
        # When an existing artifact is healthy/uncorrupted, patch framing is
        # retained and the prior artifact is inlined to save tokens.
        is_operator_redispatch = node.last_defect.startswith("redispatch requested by operator")
        corrupted, _ = is_artifact_corrupted(run_dir, node)
        if is_operator_redispatch and corrupted:
            add("retry", _REGENERATE_RETRY_INSTRUCTION + node.last_defect)
        else:
            retry_block = _PATCH_RETRY_INSTRUCTION + node.last_defect
            if not resuming:
                retry_cap = node.budget.tokens if node.budget and node.budget.tokens > 0 else DEFAULT_ARTIFACT_CAP_TOKENS
                prior_artifact = _prior_attempt_artifact(node, run_dir, ceiling_tokens=retry_cap)
                if prior_artifact is not None:
                    _u_re = re.compile(r"(?im)^(?:\#\*\#|===+|---+|###?\s+)?(?:block|entry|item|problem|section|chapter|floor|day|week|scene|step|part)\s+\d+")
                    units_found = len(_u_re.findall(prior_artifact))
                    units_exp = node.budget.units_expected if node.budget else None
                    if prior_artifact.startswith("[ARTIFACT EXCEEDS INLINE CAP:"):
                        retry_block += (
                            f"\n\n{prior_artifact}\n\n"
                            "Do not rewrite earlier units. Open the file or parts on disk and append the remaining units."
                        )
                    elif units_found > 1 and units_exp and units_found < units_exp:
                        # PLAN-TOKEN-ACCOUNTING.md §L1: append framing
                        retry_block += (
                            f"\n\nYour artifact currently contains units 1–{units_found} of {units_exp}. Do not rewrite them. "
                            f"Append units {units_found + 1}–{units_exp} to the end of the file using your editing tools, then stop."
                        )
                    else:
                        retry_block += (
                            "\n\nYour previous artifact (fix it in place; update or append to it using your file tools):\n\n"
                            f"{prior_artifact}"
                        )
            add("retry", retry_block)
    return segs


def build_node_prompt(
    node: TaskNode,
    run_dir: str | Path,
    *,
    inline_spans: bool = True,
    top_k: int | None = None,
    segment_tokens: Callable[[str, int], None] | None = None,
    hidden_paths: tuple[str, ...] = (),
    hidden_path_exceptions: tuple[str, ...] = (),
    resuming: bool = False,
    is_workspace: bool | None = None,
    workspace_root: str | Path | None = None,
) -> str:
    """Assemble a Writer's prompt. ``segment_tokens`` (PLAN.md §C5's
    "mean input tokens per leaf broken down by prompt segment" instrument)
    is an optional callback invoked once per segment with ``(label,
    token_count)`` after the whole prompt is assembled — deterministic,
    zero side effects, and the eval harness uses it to report which part
    of a leaf's prompt actually costs tokens. Default None reproduces
    exactly the pre-instrument behavior.

    §8's ordering is load-bearing for prefix caching (PLAN-AUDIT-COST
    §A6-2): most-stable first. ``goal_and_rubric`` (spec.md) and
    ``contract`` (frozen) are constant across the whole run, and the
    hidden-paths notice's constant half sits with them; everything from
    the per-node exceptions onward is node-specific. ``hidden_paths`` /
    ``hidden_path_exceptions`` are the same two tuples
    ``backends.build_writer_adapter`` hands the adapter — the notice used
    to be appended by ``cli_agent.run_episode`` after ALL of this, which
    put its constant ~120 tokens outside every cached prefix (§A6-1)."""
    segs = segments(
        node,
        run_dir,
        inline_spans=inline_spans,
        top_k=top_k,
        hidden_paths=hidden_paths,
        hidden_path_exceptions=hidden_path_exceptions,
        resuming=resuming,
        is_workspace=is_workspace,
        workspace_root=workspace_root,
    )
    if segment_tokens is not None:
        for label, text in segs:
            segment_tokens(label, estimate_tokens(text))
    return "\n\n".join(text for _, text in segs)


def _prior_attempt_artifact(node: TaskNode, run_dir: Path, ceiling_tokens: int = DEFAULT_ARTIFACT_CAP_TOKENS) -> str | None:
    """PLAN-TOKEN-ACCOUNTING.md §L2 & §O5a: failed attempt's artifact text read via node_artifact_text.
    Never inline a truncated artifact; return unit count and anchor instead."""
    from ..v0.run_dir import node_artifact_text
    try:
        text = node_artifact_text(run_dir, node)
    except OSError:
        return None
    if not text.strip():
        return None
    if estimate_tokens(text) > ceiling_tokens:
        _u_re = re.compile(r"(?im)^(?:\#\*\#|===+|---+|###?\s+)?(?:block|entry|item|problem|section|chapter|floor|day|week|scene|step|part)\s+\d+")
        matches = list(_u_re.finditer(text))
        units_found = len(matches)
        resume_point = units_found + 1
        anchor = text[matches[-1].start():].strip()[:300] if matches else text[-300:].strip()
        return (
            f"[ARTIFACT EXCEEDS INLINE CAP: {units_found} units currently on disk; "
            f"resume at unit {resume_point}. Last unit anchor:\n{anchor}]"
        )
    return text


def _non_unit_inputs(node: TaskNode, run_dir: Path) -> list[str]:
    """The subset of ``node.inputs`` that are not spine units — v4 research
    finding paths, which stay as file paths in inline-spans mode
    (PLAN-zeromem.md §4.4). Unit entries (bare ids, or ``spine/<id>.md``
    paths) are excluded — their content is supplied by the spans block."""

    known = {unit.id for unit in load_spine(run_dir)}
    kept: list[str] = []
    for entry in node.inputs:
        if entry in known:
            continue
        candidate = Path(entry).name
        if candidate.endswith(".md"):
            candidate = candidate[:-3]
        if candidate in known:
            continue
        kept.append(entry)
    return kept


_SPANS_HEADER = (
    "Source material (retrieved spans, in document order — these are the "
    "relevant excerpts from your assigned units; you do not need to read "
    "source.txt):"
)


def _retrieved_spans_block(node: TaskNode, run_dir: Path, top_k: int) -> str | None:
    """Render the node's top spans as an inline block, or None when the
    index is missing or retrieval found nothing (caller falls back to the
    path list). The query is the node's brief plus its rubric text — both
    already on the node, so no model call is needed to formulate it
    (§4.3)."""

    rubric_texts = " ".join(node.rubric.get(jid, "") for jid in node.judgment)
    query = " ".join(filter(None, [node.brief, rubric_texts])).strip()
    if not query:
        query = node.brief
    spans = retrieve_spans(run_dir, node, query, top_k=top_k)
    if not spans:
        return None
    lines = [_SPANS_HEADER]
    for span in spans:
        lines.append(f"\n[{span.unit_id} · chunk {span.chunk_index}]\n{span.text}")
    return "\n".join(lines)