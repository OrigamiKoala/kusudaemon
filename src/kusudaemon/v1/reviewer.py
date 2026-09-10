"""Reviewer role (PLAN.md §3, §6, §7).

Sees the artifact plus the node's judgment rubric only — never the writer's
reasoning or scratch (§3: "A reviewer that can see the writer's
justification talks itself into accepting"). Cannot write; it returns
scoped, located defects only (§4.5) via the §6 verdict schema.

If a node declares no judgment items, gates (already machine-checked in
code before review ever runs) are the entire exit condition, so review is
skipped rather than spending a call manufacturing an opinion nobody asked
for.

PLAN.md §A9/§B6: an over-cap artifact fans out by top-level heading into
<=``MAX_FANOUT_SECTIONS`` sections, one call each against the same rubric,
merged by union-of-items / all-pass — replacing the old whole-artifact
truncation (§D5's interim fix), which is kept only as the last-resort
fallback for an artifact with no headings to split on. Bounded, one level,
no recursion — the only place a reviewer fans out at all (§A9: "unbounded
reviewer recursion is explicitly rejected").
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..roles.protocol import RoleProvider
from .gates import estimate_tokens
from .provider import ProviderError
from .tree import TaskNode

# §11.10.13: the reviewer's input side gets the §8 "small outputs
# everywhere" treatment. 50k heuristic tokens is well above any leaf the
# budget gates admit and well below any context window worth paying for —
# the cap exists to keep a runaway artifact from blowing a one-shot call.
DEFAULT_ARTIFACT_CAP_TOKENS = 50_000

# PLAN.md §A9/§B6: "split by top-level heading into <=6 sections... Bounded,
# no recursion beyond this, and it is the only place a reviewer fans out."
# This is a hard ceiling, not a tuning knob like artifact_cap_tokens --
# raising it would reopen the "unbounded reviewer recursion" door §A9
# explicitly rejects.
MAX_FANOUT_SECTIONS = 6

# ATX markdown headings only (``#`` through ``######`` followed by content).
# Matches ``v2/survey.py``'s own ``_HEADING_RE`` in spirit but is kept
# independent: v1 is the base layer here and must not import from v2.
_MD_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+\S.*$")

# §D30 (2026-08-15): the primary defect cap. 300 was repeatedly exceeded by
# real "scoped, located" defects carrying math notation (e.g. "…but the
# evaluation of the non-trivial Gaussian integral ∫₋∞^∞ e^(−mvx²/2kT)dvx
# that yields it is never shown." ≈ 330 chars) — 400 fits the observed
# natural length while keeping reviewer outputs small (invariant 7). The
# relaxed fallback cap below is the harness revising its own constraint,
# not a model override.
DEFECT_MAXLENGTH = 400
RELAXED_DEFECT_MAXLENGTH = 600

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "verdict"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["id", "pass"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "defect": {"type": "string", "maxLength": DEFECT_MAXLENGTH},
                    "class": {"type": "string", "enum": ["patchable", "regenerate"]},
                    "node_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
    },
}

_SYSTEM_PROMPT = (
    "You are the Reviewer in a long-horizon task harness. You judge one "
    "artifact against its rubric only — you have not seen how it was "
    "produced. You cannot rewrite or fix anything; report scoped, located "
    "defects only (e.g. '§Worked Examples, example 2 omits the "
    "intermediate step'), never freeform prose suggestions. "
    "Respond with a single JSON object only."
)


@dataclass
class ReviewVerdict:
    node_id: str
    items: list[dict[str, Any]] = field(default_factory=list)
    verdict: str = "pass"
    truncated: bool = False
    section_verdicts: list[dict[str, Any]] = field(default_factory=list)
    skip_reason: str | None = None


TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["suspect", "reason"],
    "additionalProperties": False,
    "properties": {
        "suspect": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 500},
    },
}

_TRIAGE_SYSTEM_PROMPT = (
    "You are a fast triage Reviewer in a long-horizon task harness. You quickly check whether an "
    "artifact may have defects against its rubric. If there is ANY suspect issue, defect, or doubt, "
    "set suspect=true (prefer false positives over false negatives). Set suspect=false only if clearly clean. "
    "Respond with a single JSON object only."
)

GATE_COVERED_JUDGMENTS: frozenset[str] = frozenset({
    "headers:std",
    "headers_std",
    "headers_standard",
    "headings_hierarchy",
    "latex_balanced",
    "latex_syntax",
    "refs_resolve",
    "citations_resolve",
    "terms_defined",
    "nonempty",
    "claims_resolve",
    "uncited_claim",
})

CROSS_LEAF_JUDGMENTS: frozenset[str] = frozenset({
    "coverage_gap",
    "duplicate_content",
    "register_drift",
    "terminology_drift",
})

# PLAN-SWEEP-REPAIR.md §F5: judgments that ask "where did this come from?"
# and therefore have no determinate answer when there is nowhere for it to
# have come from. `claims_supported`'s rubric text is "Every factual claim
# traces to a declared input, to the contract, or is explicitly marked as an
# assumption" — on a greenfield generative leaf whose contract declares no
# rules and whose only declared input is the harness's own restatement of the
# task, nothing satisfies that, and the writer's only escape is to annotate
# every sentence as an assumption (which then fails `on_topic`).
#
# Observed 2026-09-09 on `longgen_100-floor_armC_seed1`: unit-01 and unit-02
# hold the same class of invented prose under the same `_PROSE` template, and
# the reviewer passed one and failed the other three minutes apart — a coin
# flip on an unanswerable question. `claims_supported` is not a gate, so it
# never blocks `all_passed`, but a failed verdict costs an attempt, and three
# of those lose the leaf. An unevaluatable rubric item must not fail: it is
# recorded as a vacuous pass, the same way `v1/gates._gate_headers_std`
# records "vacuous pass: non-markdown delimiter present".
GROUNDING_JUDGMENTS: frozenset[str] = frozenset({
    "claims_supported",
    "claims_traced",
})

# Lines a rendered contract carries even when it declares nothing (see
# `v2/contract.py::_T2_CONTRACT_SECTIONS`). A contract made only of these
# has no rule for a claim to trace to.
_CONTRACT_BOILERPLATE = re.compile(
    r"^\s*(?:#.*|\(none\)|-?\s*Intake was skipped:.*|)\s*$"
)

# `declared_inputs` is assembled in `v1/round_loop.py` from three kinds of
# part. A "Spine unit:" line is the harness restating this leaf's slice of
# the goal — it is the assignment, not a source — so it does not make a
# grounding judgment answerable. A dependency handoff or a research finding
# does.
_TRACEABLE_INPUT_PREFIXES = ("Handoff from ", "Research finding")


def contract_declares_rules(contract_text: str) -> bool:
    """True when the contract carries at least one substantive line."""
    return any(
        not _CONTRACT_BOILERPLATE.match(line)
        for line in (contract_text or "").splitlines()
    )


def has_traceable_source(declared_inputs: str) -> bool:
    """True when some declared input is upstream *content* rather than the
    harness's restatement of this leaf's own assignment."""
    return any(
        line.lstrip().startswith(_TRACEABLE_INPUT_PREFIXES)
        for line in (declared_inputs or "").splitlines()
    )


def ungroundable_judgments(
    judgment: list[str], contract_text: str, declared_inputs: str
) -> list[str]:
    """The grounding judgments in ``judgment`` that cannot be evaluated,
    because this leaf has nothing for a claim to be traced to. Empty when
    the contract declares rules or a real source is declared — in that case
    the judgment is answerable and is judged as before."""
    if contract_declares_rules(contract_text) or has_traceable_source(declared_inputs):
        return []
    return [j for j in judgment if j in GROUNDING_JUDGMENTS]


def compute_verdict_digest(
    artifact_text: str,
    rubric: dict[str, str],
    judgment: list[str],
    contract_text: str = "",
    brief: str = "",
) -> str:
    """PLAN-EFFICIENCY-AND-HORIZON.md §L6, PLAN-REVIEW-LATENCY-STATUS.md §9.3:
    deterministic digest over artifact, rubric, contract, and brief for caching
    passing review verdicts."""
    import hashlib

    sorted_rubric = "\n".join(
        f"{k}:{rubric.get(k, '')}" for k in sorted(judgment)
    )
    payload = f"{artifact_text}\n{sorted_rubric}\n{contract_text}\n{brief}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cap_artifact_text(text: str, ceiling_tokens: int) -> str:
    """§11.10.13: bound the artifact a Reviewer ever gets. A truncated
    artifact is marked explicitly rather than silently short: a verdict
    reached over a partial artifact must at least say so.

    PLAN-SWEEP-REPAIR.md §F3: this used to cut at ``ceiling_tokens * 0.75``
    words, documented as "the inverse of ``estimate_tokens``". That inverse
    was true of the old whitespace heuristic and is **not** true of the
    recalibrated ``tokens.count_tokens`` that ``estimate_tokens`` now
    delegates to (PLAN-TOKEN-ACCOUNTING.md §A2/§A3) — 50k ceiling → 37,500
    words → ~65.6k *measured* tokens, so the cap silently overshot by ~31%
    on exactly the path (an artifact with no split points) that most needs
    it. Measure instead of assuming a ratio: binary-search the largest word
    prefix that fits, counting the truncation notice against the budget.
    Costs O(log n) tokenizer calls, only ever on an artifact already over
    cap. The search measures the *assembled* string rather than budgeting
    the notice separately: tokenization is not additive across a
    concatenation boundary, and the separate-budget form overshot by a
    token."""
    if ceiling_tokens <= 0:
        return ""
    if estimate_tokens(text) <= ceiling_tokens:
        return text
    notice = (
        f"\n\n[ARTIFACT TRUNCATED at the ~{ceiling_tokens}-token reviewer ceiling; "
        f"judge only what is shown above]"
    )
    if estimate_tokens(notice) > ceiling_tokens:
        # Degenerate ceiling: the marker alone does not fit. Say so rather
        # than returning a prefix that silently exceeds the caller's bound.
        return notice.lstrip("\n")
    words = text.split()
    lo, hi = 0, len(words)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(" ".join(words[:mid]) + notice) <= ceiling_tokens:
            lo = mid
        else:
            hi = mid - 1
    return " ".join(words[:lo]) + notice


def _shallowest_heading_starts(text: str) -> list[int]:
    """Fan-out split points: only headings at the *shallowest* level
    actually present in the artifact. Splitting on every level would carve
    a ``### Worked Examples`` out from under the ``## Chapter 3`` it
    belongs to, producing a section list that doesn't tile the document in
    document order (PLAN.md §A9). If the artifact's only headings are
    ``###``, that becomes the shallowest level and the split point."""
    matches = list(_MD_HEADING_RE.finditer(text))
    if not matches:
        return []
    shallowest = min(len(m.group(1)) for m in matches)
    return [m.start() for m in matches if len(m.group(1)) == shallowest]


def _unit_delimiter_from_gates(node: TaskNode) -> str:
    """PLAN-SWEEP-REPAIR.md §F3: recover the artifact's unit delimiter from
    the node's own ``units_min:N@<delim>`` gate (PLAN-TOKEN-ACCOUNTING.md
    §I3's syntax, parsed identically by ``v1/gates._gate_units_min``).

    The delimiter is already resolved once per run by
    ``tokens.extract_unit_delimiter`` and baked into the gate string, so
    reading it back off the node needs no new plumbing and cannot disagree
    with what the gate counts. Returns ``""`` when no such gate is present.
    """
    for gate in list(node.gates) + list(node.warn_gates):
        name, _, arg = gate.partition(":")
        if name != "units_min" or "@" not in arg:
            continue
        _, _, delim = arg.partition("@")
        if delim:
            return delim
    return ""


def _sections_by_delimiter(text: str, delim: str) -> list[str]:
    """Slice ``text`` at each line-leading occurrence of ``delim``, with the
    same contract as ``_sections_by_heading``: document order, preamble kept
    as its own leading section, every byte in exactly one section, ``[]``
    when there is nothing to split on.

    Matching mirrors ``_gate_units_min``'s ``(?m)^\\s*<delim>`` so the
    reviewer and the gate agree about where a unit begins. Without this,
    an artifact delimited by anything that is not an ATX heading — the
    LongGenBench ``#*#`` marker is the case in hand, since ``#*#`` has no
    whitespace after its hashes — yields zero sections, fan-out silently
    never engages, and the whole document goes into one call.
    """
    if not delim:
        return []
    starts = [m.start() for m in re.finditer(r"(?m)^[ \t]*" + re.escape(delim), text)]
    if not starts:
        return []
    sections = []
    if starts[0] > 0:
        sections.append(text[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        sections.append(text[start:end])
    return sections


def _split_sections(text: str, delim: str = "") -> list[str]:
    """Fan-out split points: an explicit unit delimiter when the node
    declares one, ATX headings otherwise. Delimiter first because it is the
    artifact's *declared* unit boundary — the same one the ``units_min``
    gate counts — while headings are an inference. Falls through to headings
    when the delimiter yields nothing, so a node whose gate names a
    delimiter its writer did not use still fans out on structure."""
    if delim:
        sections = _sections_by_delimiter(text, delim)
        if sections:
            return sections
    return _sections_by_heading(text)


def _sections_by_heading(text: str) -> list[str]:
    """Slice ``text`` at each shallowest-level heading start, in document
    order. Text before the first split point (a preamble with no heading
    of its own) becomes its own leading section rather than being
    dropped — "every part of the artifact must land in exactly one of the
    <=6 groups" (PLAN.md §B6). Returns ``[]`` when the artifact has no
    markdown headings at all, the signal for the plain-truncation
    fallback."""
    starts = _shallowest_heading_starts(text)
    if not starts:
        return []
    sections = []
    if starts[0] > 0:
        sections.append(text[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        sections.append(text[start:end])
    return sections


def _group_sections(sections: list[str], max_groups: int) -> list[str]:
    """Merge adjacent sections — never reorder, never drop — so the group
    count fits ``max_groups``. Splits the section list into
    ``min(max_groups, len(sections))`` contiguous, near-equal-*count* runs.
    Balancing by section count rather than token size is deliberate: the
    hard requirement here is only the call-count ceiling (PLAN.md §A9:
    "bounded, no recursion"), and a group that's still over cap after this
    is caught by ``review_node``'s own per-group truncation fallback below
    — a size-balanced bin-pack (like ``v6/work_object.py``'s unit
    splitter) would be machinery this call site doesn't need."""
    if len(sections) <= max_groups:
        return sections
    groups: list[str] = []
    for g in range(max_groups):
        lo = g * len(sections) // max_groups
        hi = (g + 1) * len(sections) // max_groups
        if lo >= hi:
            continue
        groups.append("".join(sections[lo:hi]))
    return groups


import concurrent.futures


def _call_reviewer(
    rubric_lines: str,
    artifact_text: str,
    provider: RoleProvider,
    *,
    contract_text: str = "",
    declared_inputs: str = "",
    brief: str = "",
    on_reasoning: Callable[[str], None] | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    content_parts = [f"Rubric:\n{rubric_lines}"]
    if contract_text:
        content_parts.append(f"Contract:\n{contract_text}")
    if declared_inputs:
        content_parts.append(f"Declared Inputs:\n{declared_inputs}")
    if brief:
        content_parts.append(f"Brief:\n{brief}")
    content_parts.append(f"Artifact:\n{artifact_text}")
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "\n\n".join(content_parts),
        },
    ]
    try:
        return provider.complete_json(
            messages,
            VERDICT_SCHEMA,
            temperature=temperature,
            on_reasoning=on_reasoning,
        )
    except ProviderError as exc:
        if "maxLength" not in str(exc):
            raise
        relaxed = copy.deepcopy(VERDICT_SCHEMA)
        defect = relaxed["properties"]["items"]["items"]["properties"]["defect"]
        defect["maxLength"] = RELAXED_DEFECT_MAXLENGTH
        return provider.complete_json(
            messages,
            relaxed,
            temperature=temperature,
            on_reasoning=on_reasoning,
        )


def _call_triage(
    rubric_lines: str,
    artifact_text: str,
    provider: RoleProvider,
    *,
    contract_text: str = "",
    declared_inputs: str = "",
    brief: str = "",
    on_reasoning: Callable[[str], None] | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    content_parts = [f"Rubric:\n{rubric_lines}"]
    if contract_text:
        content_parts.append(f"Contract:\n{contract_text}")
    if declared_inputs:
        content_parts.append(f"Declared Inputs:\n{declared_inputs}")
    if brief:
        content_parts.append(f"Brief:\n{brief}")
    content_parts.append(f"Artifact:\n{artifact_text}")
    messages = [
        {"role": "system", "content": _TRIAGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "\n\n".join(content_parts),
        },
    ]
    return provider.complete_json(
        messages,
        TRIAGE_SCHEMA,
        temperature=temperature,
        on_reasoning=on_reasoning,
    )


def effective_judgment_for(
    node: TaskNode, judgment_classification: dict[str, str] | None = None
) -> list[str]:
    """The judgment items a per-node review actually evaluates.

    T1-4 strips cross-leaf items (the windowed document review owns those);
    T2-2 strips items classified ``open`` (open-world truth is not this
    reviewer's job). Extracted so ``review_node`` and its §F5 vacuous-pass
    wrapper derive the list from one place instead of two.
    """
    items = [j for j in node.judgment if j not in CROSS_LEAF_JUDGMENTS]
    jc = (
        judgment_classification
        if judgment_classification is not None
        else getattr(node, "judgment_classification", None)
    )
    if jc:
        items = [j for j in items if jc.get(j, "closed") != "open"]
    return items


def _review_node_judged(
    node: TaskNode,
    artifact_text: str,
    provider: RoleProvider,
    *,
    artifact_cap_tokens: int = DEFAULT_ARTIFACT_CAP_TOKENS,
    contract_text: str = "",
    declared_inputs: str = "",
    brief: str = "",
    on_reasoning: Callable[[str], None] | None = None,
    temperature: float = 0.0,
    triage_provider: RoleProvider | None = None,
    cached_sections: list[dict[str, Any]] | None = None,
    all_gates_passed: bool | None = None,
    gate_results: dict[str, Any] | None = None,
    judgment_classification: dict[str, str] | None = None,
    parallel: bool = True,
    unit_delimiter: str | None = None,
) -> ReviewVerdict:
    """PLAN-REVIEW-LATENCY.md: Grounded, delta-cached, parallel review."""
    brief_text = brief
    if not node.judgment:
        return ReviewVerdict(node_id=node.id, items=[], verdict="pass", skip_reason="empty_judgment")

    # T1-4 (cross-leaf) + T2-2 (open items) — see effective_judgment_for.
    effective_judgment = effective_judgment_for(node, judgment_classification)

    if not effective_judgment:
        return ReviewVerdict(node_id=node.id, items=[], verdict="pass", skip_reason="all_filtered")

    # PLAN-SWEEP-REPAIR.md §F5: a grounding judgment this leaf cannot answer is
    # not judged at all. ``review_node`` (the wrapper below) records each one as
    # a vacuous pass in the returned items; here they are simply removed from
    # what the model is asked about.
    ungroundable = set(
        ungroundable_judgments(effective_judgment, contract_text, declared_inputs)
    )
    if ungroundable:
        effective_judgment = [j for j in effective_judgment if j not in ungroundable]
    if not effective_judgment:
        return ReviewVerdict(
            node_id=node.id, items=[], verdict="pass", skip_reason="ungroundable_filtered"
        )

    # T1-1: Deterministic pre-filter: skip model call if all gates pass and all judgments are gate-covered
    gates_passed = all_gates_passed
    if gates_passed is None and gate_results is not None:
        gates_passed = all(isinstance(v, dict) and v.get("passed", False) for v in gate_results.values())
    if gates_passed is None and node.gates:
        from .gates import all_passed, evaluate_gates
        gates_passed = all_passed(evaluate_gates(node.gates, artifact_text))
    if gates_passed and all(j in GATE_COVERED_JUDGMENTS or j in node.gates for j in effective_judgment):
        return ReviewVerdict(
            node_id=node.id,
            items=[{"id": j, "pass": True} for j in effective_judgment],
            verdict="pass",
            truncated=False,
            skip_reason="gate_covered_prefilter",
        )

    rubric_lines = "\n".join(
        f"{judgment_id}: {node.rubric.get(judgment_id, '(no rubric text given)')}"
        for judgment_id in effective_judgment
    )

    # T1-2: Stage 1 Triage call if triage_provider is supplied
    if triage_provider is not None:
        try:
            triage_res = _call_triage(
                rubric_lines,
                artifact_text,
                triage_provider,
                contract_text=contract_text,
                declared_inputs=declared_inputs,
                brief=brief_text,
                on_reasoning=on_reasoning,
                temperature=temperature,
            )
            if not triage_res.get("suspect", True):
                return ReviewVerdict(
                    node_id=node.id,
                    items=[{"id": j, "pass": True} for j in effective_judgment],
                    verdict="pass",
                    truncated=False,
                )
        except Exception:
            pass

    cached_map: dict[str, dict[str, Any]] = {}
    if cached_sections:
        for cs in cached_sections:
            if isinstance(cs, dict) and cs.get("digest"):
                cached_map[cs["digest"]] = cs

    capped_artifact = cap_artifact_text(artifact_text, artifact_cap_tokens)
    if capped_artifact == artifact_text:
        digest = compute_verdict_digest(artifact_text, node.rubric, effective_judgment, contract_text, brief=brief_text)
        if digest in cached_map and cached_map[digest].get("verdict") == "pass":
            cached_entry = cached_map[digest]
            return ReviewVerdict(
                node_id=node.id,
                items=list(cached_entry.get("items", [])),
                verdict=str(cached_entry.get("verdict", "pass")),
                truncated=False,
                section_verdicts=[cached_entry],
            )
        payload = _call_reviewer(
            rubric_lines,
            artifact_text,
            provider,
            contract_text=contract_text,
            declared_inputs=declared_inputs,
            brief=brief_text,
            on_reasoning=on_reasoning,
            temperature=temperature,
        )
        items = list(payload.get("items", []))
        verdict_str = str(payload.get("verdict", "fail"))
        return ReviewVerdict(
            node_id=node.id,
            items=items,
            verdict=verdict_str,
            truncated=False,
            section_verdicts=[{"digest": digest, "items": items, "verdict": verdict_str}],
        )

    effective_delim = unit_delimiter if unit_delimiter is not None else _unit_delimiter_from_gates(node)
    sections = _group_sections(_split_sections(artifact_text, effective_delim), MAX_FANOUT_SECTIONS)
    if not sections:
        digest = compute_verdict_digest(capped_artifact, node.rubric, effective_judgment, contract_text, brief=brief_text)
        if digest in cached_map and cached_map[digest].get("verdict") == "pass":
            cached_entry = cached_map[digest]
            return ReviewVerdict(
                node_id=node.id,
                items=list(cached_entry.get("items", [])),
                verdict=str(cached_entry.get("verdict", "pass")),
                truncated=True,
                section_verdicts=[cached_entry],
            )
        payload = _call_reviewer(
            rubric_lines,
            capped_artifact,
            provider,
            contract_text=contract_text,
            declared_inputs=declared_inputs,
            brief=brief_text,
            on_reasoning=on_reasoning,
            temperature=temperature,
        )
        items = list(payload.get("items", []))
        verdict_str = str(payload.get("verdict", "fail"))
        return ReviewVerdict(
            node_id=node.id,
            items=items,
            verdict=verdict_str,
            truncated=True,
            section_verdicts=[{"digest": digest, "items": items, "verdict": verdict_str}],
        )

    prepared_sections: list[tuple[int, str, str, bool]] = []
    section_verdicts: list[dict[str, Any] | None] = [None] * len(sections)
    truncated = False

    for idx, section_text in enumerate(sections):
        capped_section = cap_artifact_text(section_text, artifact_cap_tokens)
        sec_truncated = capped_section != section_text
        if sec_truncated:
            truncated = True
            section_text = capped_section
        sec_digest = compute_verdict_digest(section_text, node.rubric, effective_judgment, contract_text, brief=brief_text)
        if sec_digest in cached_map and cached_map[sec_digest].get("verdict") == "pass":
            section_verdicts[idx] = cached_map[sec_digest]
        else:
            prepared_sections.append((idx, section_text, sec_digest, sec_truncated))

    def _eval_section(sec_idx: int, sec_text: str, sec_digest: str) -> tuple[int, dict[str, Any], str]:
        payload = _call_reviewer(
            rubric_lines,
            sec_text,
            provider,
            contract_text=contract_text,
            declared_inputs=declared_inputs,
            brief=brief_text,
            on_reasoning=on_reasoning,
            temperature=temperature,
        )
        return sec_idx, payload, sec_digest

    items: list[dict[str, Any]] = []
    overall_verdict = "pass"

    is_fake_canned = hasattr(provider, "_responses")
    if is_fake_canned or not parallel or len(prepared_sections) <= 1:
        for idx, sec_text, sec_digest, _ in prepared_sections:
            sec_idx, payload, s_digest = _eval_section(idx, sec_text, sec_digest)
            sec_items = list(payload.get("items", []))
            sec_verdict = str(payload.get("verdict", "fail"))
            section_verdicts[sec_idx] = {
                "digest": s_digest,
                "items": sec_items,
                "verdict": sec_verdict,
            }
            if sec_verdict != "pass":
                overall_verdict = "fail"
                if any(it.get("class") == "regenerate" for it in sec_items if not it.get("pass", True)):
                    break
    else:
        workers = min(len(prepared_sections), MAX_FANOUT_SECTIONS)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_info = {
                executor.submit(_eval_section, idx, sec_text, sec_digest): idx
                for idx, sec_text, sec_digest, _ in prepared_sections
            }
            cancelled = False
            for future in concurrent.futures.as_completed(future_to_info):
                if cancelled:
                    continue
                try:
                    sec_idx, payload, s_digest = future.result()
                    sec_items = list(payload.get("items", []))
                    sec_verdict = str(payload.get("verdict", "fail"))
                    section_verdicts[sec_idx] = {
                        "digest": s_digest,
                        "items": sec_items,
                        "verdict": sec_verdict,
                    }
                    if sec_verdict != "pass":
                        overall_verdict = "fail"
                        if any(it.get("class") == "regenerate" for it in sec_items if not it.get("pass", True)):
                            cancelled = True
                            for f in future_to_info:
                                f.cancel()
                except Exception:
                    overall_verdict = "fail"

    final_sections: list[dict[str, Any]] = []
    for sv in section_verdicts:
        if sv is not None:
            final_sections.append(sv)
            items.extend(sv.get("items", []))
            if sv.get("verdict") != "pass":
                overall_verdict = "fail"

    return ReviewVerdict(
        node_id=node.id,
        items=items,
        verdict=overall_verdict,
        truncated=truncated,
        section_verdicts=final_sections,
    )


def review_node(
    node: TaskNode,
    artifact_text: str,
    provider: RoleProvider,
    *,
    artifact_cap_tokens: int = DEFAULT_ARTIFACT_CAP_TOKENS,
    contract_text: str = "",
    declared_inputs: str = "",
    brief: str = "",
    on_reasoning: Callable[[str], None] | None = None,
    temperature: float = 0.0,
    triage_provider: RoleProvider | None = None,
    cached_sections: list[dict[str, Any]] | None = None,
    all_gates_passed: bool | None = None,
    gate_results: dict[str, Any] | None = None,
    judgment_classification: dict[str, str] | None = None,
    parallel: bool = True,
    unit_delimiter: str | None = None,
) -> ReviewVerdict:
    """Per-node review (PLAN-REVIEW-LATENCY.md), with PLAN-SWEEP-REPAIR.md §F5's
    vacuous-pass rule applied to grounding judgments.

    A judgment that cannot be evaluated must not fail. ``_review_node_judged``
    removes such items from what the model is asked about; this wrapper puts
    them back into the returned ``items`` as explicit vacuous passes, so
    ``audit/<node>.json`` records that the item existed, that it passed, and
    why it was not judged — rather than the item silently vanishing (which
    would read as "never in the rubric") or failing (which costs the leaf an
    attempt for an unanswerable question).

    The verdict itself is untouched: a vacuous pass can never turn a fail into
    a pass, because the items it adds are only ever the ones the inner call was
    not asked to judge.
    """
    verdict = _review_node_judged(
        node,
        artifact_text,
        provider,
        artifact_cap_tokens=artifact_cap_tokens,
        contract_text=contract_text,
        declared_inputs=declared_inputs,
        brief=brief,
        on_reasoning=on_reasoning,
        temperature=temperature,
        triage_provider=triage_provider,
        cached_sections=cached_sections,
        all_gates_passed=all_gates_passed,
        gate_results=gate_results,
        judgment_classification=judgment_classification,
        parallel=parallel,
        unit_delimiter=unit_delimiter,
    )
    vacuous = ungroundable_judgments(
        effective_judgment_for(node, judgment_classification),
        contract_text,
        declared_inputs,
    )
    if not vacuous:
        return verdict
    already = {i.get("id") for i in verdict.items if isinstance(i, dict)}
    verdict.items = list(verdict.items) + [
        {
            "id": j,
            "pass": True,
            "defect": "none",
            "detail": (
                "vacuous pass: nothing to trace to — the contract declares no "
                "rules and no source is declared for this leaf, so there is no "
                "determinate answer to where a claim came from"
            ),
        }
        for j in vacuous
        if j not in already
    ]
    return verdict
