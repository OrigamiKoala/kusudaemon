"""Shared JSON parsing, extraction, and schema instruction utilities.

Used by both OpenAICompatibleProvider (direct HTTP) and BackendRoleProvider
(CLI episodes) to enforce identical schema extraction and validate-reprompt
semantics.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..v1.json_schema import describe_schema, validate


def build_json_instruction(schema: dict[str, Any]) -> str:
    """Build prose schema instructions matching the standard fallback format."""
    return (
        "Respond with a single JSON object only — no prose, no "
        f"code fences — matching this schema:\n{describe_schema(schema)}\n"
        "Do not output the schema structure itself (do not include top-level 'properties' or 'type'). "
        "Output the JSON instance object directly."
    )


_HARNESS_METADATA_TYPES = {"usage", "logdir", "heartbeat", "thinking", "system", "session_captured"}


def _unwrap_schema_echo(cand: Any, schema: dict[str, Any] | None = None) -> Any:
    """Unwrap dictionary if model wrapped its response inside schema keys like 'properties', 'result', etc."""
    if not isinstance(cand, dict):
        return cand
    if schema is not None and not validate(cand, schema):
        return cand
    for key in ("properties", "result", "data", "output", "response"):
        sub = cand.get(key)
        if isinstance(sub, dict):
            if schema is not None:
                if not validate(sub, schema):
                    return sub
            else:
                return sub
    if isinstance(cand.get("properties"), dict):
        sub = cand["properties"]
        if schema:
            expected = set(schema.get("required", [])) | set(schema.get("properties", {}).keys())
            if expected and any(k in sub for k in expected):
                return sub
    return cand


def _repair_common_schema_omissions(cand: Any, schema: dict[str, Any] | None = None) -> Any:
    """Auto-repair obvious schema omissions like missing 'pass' boolean on defect items, single item review dicts, missing verdict, extra keys."""
    if schema is None or not isinstance(schema, dict):
        return cand

    req = set(schema.get("required", []))
    props = schema.get("properties", {})

    # Case: schema expects {"items": [...], "verdict": ...} (REVIEW_SCHEMA / DOC_REVIEW_SCHEMA)
    if {"items", "verdict"}.issubset(req) or ("items" in props and "verdict" in props):
        if isinstance(cand, list):
            cand = {"items": cand}
        elif isinstance(cand, dict) and "items" not in cand:
            if any(k in cand for k in ("id", "pass", "defect", "check", "class")):
                cand = {"items": [cand]}
            else:
                cand = {"items": [], "verdict": "pass"}

        if isinstance(cand, dict):
            if not isinstance(cand.get("items"), list):
                if isinstance(cand.get("items"), dict):
                    cand["items"] = [cand["items"]]
                else:
                    cand["items"] = []

            item_schema = props.get("items", {}).get("items", {})
            allowed_item_keys = set(item_schema.get("properties", {}).keys()) if item_schema.get("additionalProperties") is False else None

            cleaned_items: list[Any] = []
            for i, item in enumerate(cand["items"]):
                if not isinstance(item, dict):
                    if not allowed_item_keys and (not item_schema or item_schema.get("type") != "object"):
                        cleaned_items.append(item)
                    continue
                it = dict(item)
                if allowed_item_keys is not None:
                    it = {k: v for k, v in it.items() if k in allowed_item_keys}
                if "id" not in it:
                    it["id"] = f"item_{i + 1}"
                else:
                    it["id"] = str(it["id"])
                if "pass" not in it:
                    if "defect" in it or "error" in it or it.get("class") in ("patchable", "regenerate"):
                        it["pass"] = False
                    else:
                        it["pass"] = True
                else:
                    it["pass"] = bool(it["pass"])
                if "class" in it and it["class"] not in ("patchable", "regenerate"):
                    del it["class"]
                if "defect" in it:
                    it["defect"] = str(it["defect"])[:500]
                if "node_ids" in it and isinstance(it["node_ids"], list):
                    it["node_ids"] = [str(x) for x in it["node_ids"]]
                cleaned_items.append(it)
            cand["items"] = cleaned_items

            if cand.get("verdict") not in ("pass", "fail"):
                has_defect = any(it.get("pass") is False or bool(it.get("defect")) for it in cleaned_items)
                cand["verdict"] = "fail" if has_defect else "pass"

            if schema.get("additionalProperties") is False:
                cand = {k: v for k, v in cand.items() if k in props}
            return cand

    # Case: schema expects {"files_touched", "artifacts", "answerable_without_exploration"} (ESTIMATE_SCHEMA)
    if "files_touched" in props and "artifacts" in props:
        if isinstance(cand, dict):
            cand = dict(cand)
            ft = cand.get("files_touched")
            if isinstance(ft, int):
                if ft <= 1:
                    cand["files_touched"] = "1"
                elif ft <= 5:
                    cand["files_touched"] = "few"
                else:
                    cand["files_touched"] = "many"
            elif not isinstance(ft, str) or ft not in ("1", "few", "many", "unknown"):
                cand["files_touched"] = "unknown"

            art = cand.get("artifacts")
            if not isinstance(art, int):
                try:
                    cand["artifacts"] = max(1, min(50, int(art)))
                except (TypeError, ValueError):
                    cand["artifacts"] = 1
            else:
                cand["artifacts"] = max(1, min(50, art))

            if "answerable_without_exploration" not in cand:
                cand["answerable_without_exploration"] = False
            else:
                cand["answerable_without_exploration"] = bool(cand["answerable_without_exploration"])

            if "work_kind" in props:
                wk = cand.get("work_kind")
                if not isinstance(wk, str) or wk.strip().lower() not in ("document", "procedure", "code-edit", "unknown"):
                    cand["work_kind"] = "unknown"
                else:
                    cand["work_kind"] = wk.strip().lower()

            if "questions" in props:
                if not isinstance(cand.get("questions"), list):
                    cand["questions"] = []
                else:
                    cand["questions"] = [
                        q for q in cand["questions"]
                        if isinstance(q, dict) and "id" in q and "text" in q and "default_assumption" in q
                    ]

            if "objections" in props:
                if not isinstance(cand.get("objections"), list):
                    cand["objections"] = []
                else:
                    cand["objections"] = [
                        o for o in cand["objections"]
                        if isinstance(o, dict) and "claim" in o and "why" in o and "options" in o
                    ]

            if schema.get("additionalProperties") is False:
                cand = {k: v for k, v in cand.items() if k in props}
            return cand

    # Case: schema expects {"questions", "objections"} (INTAKE_SCHEMA)
    if "questions" in props and "objections" in props:
        if isinstance(cand, dict):
            cand = dict(cand)
            if not isinstance(cand.get("questions"), list):
                cand["questions"] = []
            else:
                cand["questions"] = [
                    q for q in cand["questions"]
                    if isinstance(q, dict) and "id" in q and "text" in q and "default_assumption" in q
                ]
            if not isinstance(cand.get("objections"), list):
                cand["objections"] = []
            else:
                cand["objections"] = [
                    o for o in cand["objections"]
                    if isinstance(o, dict) and "claim" in o and "why" in o and "options" in o
                ]
            if schema.get("additionalProperties") is False:
                cand = {k: v for k, v in cand.items() if k in props}
            return cand

    # Case: schema expects {"suspect", "reason"} (TRIAGE_SCHEMA)
    if "suspect" in props and "reason" in props:
        if isinstance(cand, dict):
            cand = dict(cand)
            if "suspect" not in cand:
                cand["suspect"] = bool(cand.get("reason"))
            else:
                cand["suspect"] = bool(cand["suspect"])
            if "reason" not in cand:
                cand["reason"] = "none"
            else:
                cand["reason"] = str(cand["reason"])[:500]
            if schema.get("additionalProperties") is False:
                cand = {k: v for k, v in cand.items() if k in props}
            return cand

    # Case: schema expects {"children": [...]} (PARTITION_SCHEMA)
    if "children" in props:
        if isinstance(cand, list):
            cand = {"children": cand}
        if isinstance(cand, dict) and isinstance(cand.get("children"), list):
            cand = dict(cand)
            child_schema = props["children"].get("items", {})
            allowed_child_keys = set(child_schema.get("properties", {}).keys()) if child_schema.get("additionalProperties") is False else None
            cleaned_children = []
            for i, ch in enumerate(cand["children"]):
                if not isinstance(ch, dict):
                    continue
                c = dict(ch)
                if allowed_child_keys is not None:
                    c = {k: v for k, v in c.items() if k in allowed_child_keys}
                if "id" not in c:
                    c["id"] = f"node_{i + 1}"
                if "brief" not in c:
                    c["brief"] = f"Task {i + 1}"
                if "unit_start" not in c:
                    c["unit_start"] = 0
                if "unit_end" not in c:
                    c["unit_end"] = 0
                if "estimated_calls" not in c:
                    c["estimated_calls"] = 1
                if "shape" not in c or c["shape"] not in ("prose-dominant", "derivation-dominant", "reference-dominant", "problem-set-dominant"):
                    c["shape"] = "prose-dominant"
                cleaned_children.append(c)
            cand["children"] = cleaned_children
            if schema.get("additionalProperties") is False:
                cand = {k: v for k, v in cand.items() if k in props}
            return cand

    if isinstance(cand, dict) and schema.get("additionalProperties") is False:
        cand = {k: v for k, v in cand.items() if k in props}

    return cand


def _parse_json_object(content: str) -> tuple[dict[str, Any] | None, str]:
    """Parse a single JSON object from string content, stripping code fences or trailing data if present."""
    if not content or not content.strip():
        return None, "empty response"
    text = content.strip()

    # Fast path 1: direct parse of full string
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, ""
        return None, "response was not a JSON object"
    except json.JSONDecodeError:
        pass

    # Fast path 2: markdown code fences ```json ... ``` or ``` ... ```
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    matches = fence_pattern.findall(text)
    for block in matches:
        block_text = block.strip()
        try:
            parsed = json.loads(block_text)
            if isinstance(parsed, dict):
                return parsed, ""
        except json.JSONDecodeError:
            pass

    # Fallback: scan for JSON objects using raw_decode (handles trailing prose/extra data)
    decoder = json.JSONDecoder()
    pos = 0
    last_dict: dict[str, Any] | None = None
    first_err: str = ""

    while pos < len(text):
        brace_idx = text.find("{", pos)
        if brace_idx == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(text, brace_idx)
            if isinstance(obj, dict):
                last_dict = obj
            pos = max(end_idx, brace_idx + 1)
        except json.JSONDecodeError as exc:
            if not first_err:
                first_err = f"invalid JSON: {exc}"
            pos = brace_idx + 1

    if last_dict is not None:
        return last_dict, ""

    # If no dict was found, produce standard decode error message
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return None, "response was not a JSON object"
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"

    return None, first_err or "response was not a JSON object"


def extract_last_json_object(
    text: str, schema: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """Extract the last valid JSON object from arbitrary text or log traces.

    Attempts standard parsing first; if that fails, scans for JSON objects using
    JSONDecoder, skipping harness metadata lines, and validating against schema if given.
    """
    if not text or not text.strip():
        return None, "empty response"

    # Fast path: standard parse if valid single JSON object
    parsed, err = _parse_json_object(text)
    if parsed is not None and parsed.get("type") not in _HARNESS_METADATA_TYPES:
        normalized = _repair_common_schema_omissions(_unwrap_schema_echo(parsed, schema), schema)
        if schema is None or not validate(normalized, schema):
            return normalized, ""

    # Scan for all JSON objects in text
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []

    # Also scan inside fenced code blocks
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
    for block in fence_pattern.findall(text):
        b_obj, _ = _parse_json_object(block)
        if b_obj is not None and b_obj.get("type") not in _HARNESS_METADATA_TYPES:
            candidates.append(b_obj)

    pos = 0
    last_err = err
    while pos < len(text):
        brace_idx = text.find("{", pos)
        if brace_idx == -1:
            break
        try:
            obj, end_idx = decoder.raw_decode(text, brace_idx)
            if isinstance(obj, dict) and obj.get("type") not in _HARNESS_METADATA_TYPES:
                candidates.append(obj)
            pos = max(end_idx, brace_idx + 1)
        except json.JSONDecodeError as exc:
            last_err = f"invalid JSON: {exc}"
            pos = brace_idx + 1

    if not candidates:
        return None, last_err or "could not extract JSON object from output"

    # Search in reverse (last valid object)
    first_fallback: dict[str, Any] | None = None
    for cand in reversed(candidates):
        normalized = _repair_common_schema_omissions(_unwrap_schema_echo(cand, schema), schema)
        if schema is not None:
            if not validate(normalized, schema):
                return normalized, ""
            if first_fallback is None:
                first_fallback = normalized
        else:
            return normalized, ""

    if first_fallback is not None:
        return first_fallback, ""

    return None, last_err or "could not extract valid JSON object matching schema"
