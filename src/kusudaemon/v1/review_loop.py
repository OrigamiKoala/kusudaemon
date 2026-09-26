"""Goal-directed read loop for artifact review (PLAN-REVIEW-READ-LOOP.md §R3).

Replaces arbitrary whole-document or fan-out review for large artifacts (>8000 tokens)
with an active, turn-based inspection loop. Tracks a coverage ledger, bounds per-turn
context, fails fast on defects, and enforces coverage before passing.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..roles.protocol import RoleProvider
from .gates import estimate_tokens, unit_matches
from .provider import ProviderDeadlineError, ProviderError, call_scope
from .reviewer import (
    ARTIFACT_LABEL,
    DEFECT_MAXLENGTH,
    RELAXED_DEFECT_MAXLENGTH,
    ReviewVerdict,
    _MD_HEADING_RE,
    is_malformed_defect,
)
from .tree import TaskNode

DEFAULT_READ_TOKENS = 6000
DEFAULT_READ_TURN_MAX_TOKENS = 8192

READ_LOOP_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["read", "verdict"]},
        "unit": {"type": ["integer", "null"]},
        "units": {
            "type": ["array", "null"],
            "items": {"type": "integer"},
        },
        "findings": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["id", "pass", "defect"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "unit": {"type": ["integer", "null"]},
                    "pass": {"type": "boolean"},
                    "defect": {"type": "string", "maxLength": DEFECT_MAXLENGTH},
                },
            },
        },
        "items": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["id", "pass", "defect"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "unit": {"type": ["integer", "null"]},
                    "pass": {"type": "boolean"},
                    "defect": {"type": "string", "maxLength": DEFECT_MAXLENGTH},
                    "class": {"type": "string", "enum": ["patchable", "regenerate"]},
                },
            },
        },
        "verdict": {"type": ["string", "null"], "enum": ["pass", "fail", None]},
    },
}

_READ_LOOP_SYSTEM_PROMPT = (
    "You are the Reviewer in a long-horizon task harness, conducting a goal-directed "
    "read loop over an artifact written by another agent. You do not rewrite or fix anything. "
    "You inspect units to verify compliance with the rubric.\n\n"
    "In each turn, respond with a single JSON action object:\n"
    "- To inspect units: {\"action\": \"read\", \"unit\": <N>} or {\"action\": \"read\", \"units\": [<start>, <end>]}, "
    "along with optional findings from the previously read chunk.\n"
    "- When you find a fatal defect or finish reading all units: {\"action\": \"verdict\", \"verdict\": \"pass\"|\"fail\", "
    "\"items\": [{\"id\": \"<rubric_id>\", \"unit\": <N>, \"pass\": bool, \"defect\": \"<description>\"}]}.\n"
    "Fail-fast: if any unit fails a rubric item, emit verdict fail immediately with the located defect."
)


@dataclass
class UnitSlice:
    ordinal: int
    text: str
    words: int
    first_line: str
    flags: list[str] = field(default_factory=list)


def build_unit_slices(
    text: str, delim: str = "", expected_range: tuple[int, int] | None = None
) -> list[UnitSlice]:
    """Parse artifact into indexed UnitSlices with structural flags."""
    slices: list[UnitSlice] = []
    seen_ordinals: set[int] = set()

    if delim:
        matches = list(unit_matches(text, delim))
        if matches:
            prev_ord: int | None = None
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                chunk = text[start:end]
                words = len(chunk.split())
                lines = chunk.strip().splitlines()
                first_line = lines[0][:80] if lines else ""

                ord_str = m.group("ord") if "ord" in m.groupdict() and m.group("ord") else None
                if ord_str is None and first_line:
                    m_ord = re.search(r"\b(\d+)\b", first_line)
                    if m_ord:
                        ord_str = m_ord.group(1)
                try:
                    ord_val = int(ord_str) if ord_str is not None else i + 1
                except ValueError:
                    ord_val = i + 1

                flags: list[str] = []
                if ord_val in seen_ordinals:
                    flags.append("dup-ordinal")
                seen_ordinals.add(ord_val)

                if expected_range is not None:
                    lo, hi = expected_range
                    if ord_val < lo or ord_val > hi:
                        flags.append("out-of-range")

                if prev_ord is not None and ord_val > prev_ord + 1:
                    flags.append(f"missing-before({prev_ord+1}..{ord_val-1})")
                prev_ord = ord_val

                if words < 20:
                    flags.append(f"short({words}w)")

                slices.append(
                    UnitSlice(
                        ordinal=ord_val,
                        text=chunk,
                        words=words,
                        first_line=first_line,
                        flags=flags,
                    )
                )
            return slices

    # Fallback to headings
    headings = list(_MD_HEADING_RE.finditer(text))
    if headings:
        for i, m in enumerate(headings):
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            chunk = text[start:end]
            words = len(chunk.split())
            heading_line = m.group(0).strip()[:80]
            slices.append(
                UnitSlice(
                    ordinal=i + 1,
                    text=chunk,
                    words=words,
                    first_line=heading_line,
                    flags=[] if words >= 20 else [f"short({words}w)"],
                )
            )
        return slices

    # Last resort: fixed 4k token (~3k words) chunks
    words = text.split()
    chunk_size = 3000
    for i in range(0, max(1, len(words)), chunk_size):
        chunk_words = words[i : i + chunk_size]
        chunk_text = " ".join(chunk_words)
        ord_val = (i // chunk_size) + 1
        slices.append(
            UnitSlice(
                ordinal=ord_val,
                text=chunk_text,
                words=len(chunk_words),
                first_line=f"Chunk {ord_val} ({len(chunk_words)} words)",
                flags=[],
            )
        )
    return slices


def format_outline(slices: list[UnitSlice]) -> str:
    """Format slices into compact outline: ordinal | first line | words | flags."""
    lines = []
    for s in slices:
        flag_str = f" [{', '.join(s.flags)}]" if s.flags else ""
        lines.append(f"unit {s.ordinal} | {s.first_line} | {s.words} words{flag_str}")
    return "\n".join(lines)


def _format_ranges(ordinals: set[int]) -> str:
    """Format sorted ints as range intervals, e.g. 1-40, 55-60."""
    if not ordinals:
        return "(none)"
    sorted_ords = sorted(ordinals)
    ranges: list[str] = []
    start = sorted_ords[0]
    prev = start
    for x in sorted_ords[1:]:
        if x == prev + 1:
            prev = x
        else:
            ranges.append(f"{start}–{prev}" if prev > start else f"{start}")
            start = prev = x
    ranges.append(f"{start}–{prev}" if prev > start else f"{start}")
    return ", ".join(ranges)


def run_review_read_loop(
    node: TaskNode,
    artifact_text: str,
    provider: RoleProvider,
    *,
    effective_judgment: list[str],
    rubric_lines: str,
    unit_delimiter: str = "",
    contract_text: str = "",
    declared_inputs: str = "",
    brief: str = "",
    gate_results: dict[str, Any] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
    temperature: float | None = None,
    run_dir: str | Path | None = None,
) -> ReviewVerdict:
    """Execute the goal-directed read loop over the artifact."""
    read_cap = int(os.getenv("KUSUDAEMON_REVIEW_READ_TOKENS", str(DEFAULT_READ_TOKENS)))
    read_turn_cap = int(os.getenv("KUSUDAEMON_REVIEW_READ_MAX_TOKENS", str(DEFAULT_READ_TURN_MAX_TOKENS)))
    total_tokens = estimate_tokens(artifact_text)
    turn_budget = math.ceil(total_tokens / max(1, read_cap)) + 4

    # Extract expected range from node gates if present
    expected_range: tuple[int, int] | None = None
    for g in list(node.gates) + list(node.warn_gates):
        if g.startswith("units_range:"):
            spec = g.partition(":")[2].partition("@")[0]
            lo, _, hi = spec.partition("-")
            try:
                expected_range = (int(lo), int(hi))
            except ValueError:
                pass
            break

    slices = build_unit_slices(artifact_text, delim=unit_delimiter, expected_range=expected_range)
    slice_by_ord: dict[int, UnitSlice] = {s.ordinal: s for s in slices}
    all_ordinals = set(slice_by_ord.keys())
    total_units = len(all_ordinals)

    outline_str = format_outline(slices)
    gate_summary = ""
    if gate_results is not None:
        gate_summary = "\n".join(
            f"- {k}: {'PASS' if (isinstance(v, dict) and v.get('passed', False)) else 'FAIL'}"
            for k, v in gate_results.items()
        )

    base_context_parts = [
        f"Brief:\n{brief}" if brief else "",
        f"Contract:\n{contract_text}" if contract_text else "",
        f"Declared Inputs:\n{declared_inputs}" if declared_inputs else "",
        f"Rubric:\n{rubric_lines}",
        f"Gate Results:\n{gate_summary}" if gate_summary else "",
        f"Document Outline ({total_units} units):\n{outline_str}",
    ]
    base_context = "\n\n".join([p for p in base_context_parts if p])

    covered: set[int] = set()
    reread_count = 0
    accumulated_findings: list[dict[str, Any]] = []
    newest_chunk_text = ""
    coverage_prompts = 0

    schema = copy.deepcopy(READ_LOOP_ACTION_SCHEMA)

    turns_taken = 0
    t_start = time.time()

    for turn in range(1, turn_budget + 1):
        turns_taken = turn
        unread_ordinals = all_ordinals - covered
        cov_summary = (
            f"Coverage: read units: {_format_ranges(covered)}; unread: {_format_ranges(unread_ordinals)}"
        )

        findings_lines = []
        for f_item in accumulated_findings:
            u_str = f"unit {f_item.get('unit')} " if f_item.get("unit") is not None else ""
            status = "PASS" if f_item.get("pass") else f"FAIL ({f_item.get('defect', '')})"
            findings_lines.append(f"- {u_str}{f_item.get('id')}: {status}")
        findings_str = (
            "Accumulated Findings:\n" + "\n".join(findings_lines)
            if findings_lines
            else "Accumulated Findings: (none yet)"
        )

        turn_prompt_parts = [
            base_context,
            cov_summary,
            findings_str,
        ]
        if newest_chunk_text:
            turn_prompt_parts.append(f"Newly Read Unit Content:\n{newest_chunk_text}")
        else:
            turn_prompt_parts.append(
                "Inspect units by emitting action 'read' with unit or units. "
                "Or if you have identified a defect or finished inspection, emit action 'verdict'."
            )

        messages = [
            {"role": "system", "content": _READ_LOOP_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(turn_prompt_parts)},
        ]

        try:
            # §R3.6: read turns use a fixed output cap (no escalation —
            # findings are ≤8 items). Threaded via call_scope so it applies on
            # the HTTP path without changing the RoleProvider protocol. The cap
            # covers the reasoning trace too, so it defaults to 8192 rather
            # than 4096 (triage hit its 2048 cap on every call, see
            # reviewer._call_triage).
            with call_scope(max_tokens=read_turn_cap):
                action_obj = provider.complete_json(
                    messages,
                    schema,
                    temperature=temperature,
                    on_reasoning=on_reasoning,
                    streaming=True,
                )
        except ProviderDeadlineError as exc:
            return ReviewVerdict(
                node_id=node.id,
                items=[],
                verdict="unavailable",
                skip_reason=f"deadline: {exc}",
            )
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
                skip_reason=f"read_loop_error: {exc}",
            )

        if not isinstance(action_obj, dict):
            return ReviewVerdict(
                node_id=node.id,
                items=[],
                verdict="unavailable",
                skip_reason="malformed_action_object",
            )

        # Ingest findings
        new_findings = action_obj.get("findings", [])
        if isinstance(new_findings, list):
            for f_item in new_findings:
                if isinstance(f_item, dict) and "id" in f_item:
                    accumulated_findings.append(f_item)

        action = action_obj.get("action")

        # §R3.5: Fail-fast check on findings
        failing_findings = [f for f in accumulated_findings if not f.get("pass", True)]
        if failing_findings and action != "verdict":
            # Force verdict fail
            action = "verdict"
            action_obj["verdict"] = "fail"
            action_obj["items"] = failing_findings

        if action == "verdict":
            verdict_str = action_obj.get("verdict")
            verdict_items = action_obj.get("items", []) or failing_findings

            if verdict_str == "fail" or any(not it.get("pass", True) for it in verdict_items):
                # Verify defect text actionable (§R4.2)
                malformed = [
                    it
                    for it in verdict_items
                    if not it.get("pass", True)
                    and is_malformed_defect(str(it.get("id", "")), it.get("defect"))
                ]
                if malformed:
                    target_id = str(malformed[0].get("id", ""))
                    reask_prompt = (
                        f"The rubric item '{target_id}' failed but the defect description is insufficient. "
                        "State the specific defect: what is wrong and where (e.g. section, unit, or line)."
                    )
                    reask_messages = list(messages) + [
                        {"role": "assistant", "content": json.dumps(action_obj)},
                        {"role": "user", "content": reask_prompt},
                    ]
                    try:
                        with call_scope(max_tokens=read_turn_cap):
                            second = provider.complete_json(
                                reask_messages,
                                schema,
                                temperature=temperature,
                                on_reasoning=on_reasoning,
                                streaming=True,
                            )
                        second_items = second.get("items", []) if isinstance(second, dict) else []
                        if any(
                            not it.get("pass", True)
                            and is_malformed_defect(str(it.get("id", "")), it.get("defect"))
                            for it in second_items
                        ):
                            return ReviewVerdict(
                                node_id=node.id,
                                items=[],
                                verdict="unavailable",
                                skip_reason="malformed_defect",
                            )
                        verdict_items = second_items
                    except Exception:
                        return ReviewVerdict(
                            node_id=node.id,
                            items=[],
                            verdict="unavailable",
                            skip_reason="malformed_defect",
                        )

                _write_read_loop_audit(
                    run_dir,
                    node.id,
                    total_units,
                    len(covered),
                    turns_taken,
                    reread_count,
                    time.time() - t_start,
                )
                return ReviewVerdict(
                    node_id=node.id,
                    items=verdict_items,
                    verdict="fail",
                    truncated=False,
                )

            # Reviewer wants to pass: check coverage (§R3.3). A `pass`
            # verdict with unread units is rejected and re-asked; the loop
            # only exits `pass` when every unit is covered, or `unavailable`
            # when the turn budget runs out below. There is no escape hatch
            # that passes with units unread.
            missing = all_ordinals - covered
            if missing:
                coverage_prompts += 1
                newest_chunk_text = (
                    f"You have not read units {_format_ranges(missing)}. "
                    "Read them, or fail the item naming what you could not check."
                )
                continue

            # Coverage satisfied or max coverage prompts reached
            # Build passing items for all effective judgments
            passing_items = []
            for j in effective_judgment:
                passing_items.append({"id": j, "pass": True, "defect": "none"})

            _write_read_loop_audit(
                run_dir,
                node.id,
                total_units,
                len(covered),
                turns_taken,
                reread_count,
                time.time() - t_start,
            )
            return ReviewVerdict(
                node_id=node.id,
                items=passing_items,
                verdict="pass",
                truncated=False,
            )

        elif action == "read":
            requested_ords: list[int] = []
            if action_obj.get("unit") is not None:
                try:
                    requested_ords.append(int(action_obj["unit"]))
                except (ValueError, TypeError):
                    pass
            if action_obj.get("units") and isinstance(action_obj["units"], list):
                if len(action_obj["units"]) == 2:
                    try:
                        u_start, u_end = int(action_obj["units"][0]), int(action_obj["units"][1])
                        requested_ords.extend(range(u_start, u_end + 1))
                    except (ValueError, TypeError):
                        pass
                else:
                    for u in action_obj["units"]:
                        try:
                            requested_ords.append(int(u))
                        except (ValueError, TypeError):
                            pass

            if not requested_ords:
                # Default: pick next unread unit
                unread_list = sorted(all_ordinals - covered)
                if unread_list:
                    requested_ords = [unread_list[0]]
                else:
                    requested_ords = [1]

            # Assemble unit texts up to read_cap tokens
            chunk_parts = []
            cum_tokens = 0
            for u in requested_ords:
                if u in covered:
                    reread_count += 1
                if u in slice_by_ord:
                    u_slice = slice_by_ord[u]
                    u_tok = estimate_tokens(u_slice.text)
                    if cum_tokens + u_tok > read_cap and chunk_parts:
                        chunk_parts.append(
                            f"[Truncated: read request exceeded ~{read_cap} token limit; ask for remaining units next]"
                        )
                        break
                    chunk_parts.append(f"=== Unit {u} ===\n{u_slice.text}")
                    cum_tokens += u_tok
                    covered.add(u)
                else:
                    chunk_parts.append(f"=== Unit {u} ===\n(Unit {u} does not exist in artifact)")

            newest_chunk_text = "\n\n".join(chunk_parts)

        else:
            newest_chunk_text = "Invalid action. Respond with action 'read' or action 'verdict'."

    # Budget exhausted (§R3.4)
    _write_read_loop_audit(
        run_dir,
        node.id,
        total_units,
        len(covered),
        turns_taken,
        reread_count,
        time.time() - t_start,
    )
    return ReviewVerdict(
        node_id=node.id,
        items=[],
        verdict="unavailable",
        skip_reason="read_loop_turn_budget_exhausted",
    )


def _write_read_loop_audit(
    run_dir: str | Path | None,
    node_id: str,
    total_units: int,
    units_read: int,
    turns: int,
    rereads: int,
    elapsed_s: float,
) -> None:
    """PLAN-REVIEW-READ-LOOP.md §R3.6: write read loop coverage to audit."""
    if not run_dir:
        return
    try:
        p = Path(run_dir) / "audit" / f"{node_id}_read_loop.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "units_total": total_units,
            "units_read": units_read,
            "turns": turns,
            "rereads": rereads,
            "elapsed_s": round(elapsed_s, 2),
            "coverage_fraction": round(units_read / max(1, total_units), 3),
        }
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        pass
