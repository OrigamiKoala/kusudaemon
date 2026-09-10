#!/usr/bin/env python3
"""Shared LongGenBench format helpers (BENCHMARKING.md §4.1).

Upstream (github.com/mozhu621/LongGenBench) splits a generation on ``#*#``
into ``output_blocks``, then keys each block by the integer following the
task's ``type`` word ("Week 7", "Floor 23", "Menu Week 4", "Block 51").
``Evalution/eval.py`` and ``Evalution/inference.py`` own those two steps;
everything here is a faithful port of them, minus the vLLM import that makes
the originals unrunnable without a GPU.

Keeping them in one module matters for experimental validity: arms A and C
must be turned into blocks by *identical* code, or a difference in parsing
shows up as a difference in completion rate.


NOTE ON THE NAME: two unrelated papers are called "LongGenBench". This
implements Wu et al., arXiv 2409.02076 (repo mozhu621/LongGenBench) -- the
block-structured diary/menu/skyscraper tasks scored by completion rate plus a
model-verified instruction-following accuracy. It is NOT Liu et al., arXiv
2410.04199 (repo Dominic789654/LongGenBench), which synthesises GSM8K/MMLU/CSQA
and scores deterministically against gold labels with no verifier model. The
judge here is upstream's, not ours. See BENCHMARKING.md 4.1 and 4.1.4.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Upstream separator and end-of-document sentinel. The datasets are
# inconsistent about the trailing marker ('*** finished ***' in the Week
# prompts, '*** finished' in the others), so match the common prefix.
BLOCK_SEP = "#*#"
FINISHED_RE = re.compile(r"\*\*\*\s*finished\b.*", re.IGNORECASE | re.DOTALL)
STARTED_RE = re.compile(r"^.*?\*\*\*\s*started\s*\*\*\*", re.DOTALL)

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Backend CLIs render to a TTY-shaped stream; escape codes in the
    captured stdout are decoration, not generated content."""
    return _ANSI_RE.sub("", text)


def parse_blocks(output_blocks: list[str], type_: str) -> dict[int, str]:
    """Port of ``Evalution/eval.py::parse_blocks`` (first match wins)."""
    type_to_block: dict[int, str] = {}
    pattern = rf"{re.escape(type_)} (\d+)"
    for block in output_blocks:
        match = re.search(pattern, block)
        if match:
            identifier = int(match.group(1))
            if identifier not in type_to_block or type_to_block[identifier] is None:
                type_to_block[identifier] = block
    return type_to_block


def calculate_completion_rate(type_to_block: dict[int, str], total_number: int) -> float:
    """Port of ``Evalution/eval.py::calculate_completion_rate`` — percentage
    of the expected 1..N identifiers that appear at all."""
    if total_number <= 0:
        return 0.0
    identifiers = set(type_to_block.keys())
    expected = set(range(1, total_number + 1))
    return (len(expected) - len(expected - identifiers)) / len(expected) * 100.0


def to_output_blocks(raw_text: str, item: dict[str, Any]) -> list[str]:
    """Turn one generation into upstream's ``output_blocks``.

    Deviation from ``Evalution/inference.py``, applied identically to every
    arm: upstream unconditionally prepends ``item['prefix']`` because its
    prompts end mid-document ("*** started ***\\n#*# Week 1 (...):") and a
    base model *continues* them. A chat/agent backend restates the heading
    instead, so an unconditional prepend would duplicate block 1 and an
    unconditional skip would lose it. Prepend only when block 1 is absent.
    """
    text = strip_ansi(raw_text or "").strip()

    # Some backends echo the instruction before answering; everything up to
    # and including the '*** started ***' marker is prompt, not generation.
    started = STARTED_RE.match(text)
    if started and started.end() < len(text):
        text = text[started.end():].lstrip()

    # The trailing sentinel is a stop marker, never part of a block.
    text = FINISHED_RE.sub("", text).strip()

    blocks = text.split(BLOCK_SEP)
    type_ = str(item.get("type") or "")
    if type_ and 1 not in parse_blocks(blocks, type_):
        prefix = str(item.get("prefix") or "")
        if prefix:
            text = prefix + "\n" + text
            blocks = text.split(BLOCK_SEP)
    return blocks


def build_prediction_record(item: dict[str, Any], raw_text: str) -> dict[str, Any]:
    """One entry of the JSON that the evaluator consumes — the same shape
    ``Evalution/inference.py::process_and_save_results`` writes."""
    blocks = to_output_blocks(raw_text, item)
    joined = BLOCK_SEP.join(blocks)
    return {
        "input": item.get("prompt", ""),
        "checks_once": item.get("checks_once", {}),
        "checks_range": item.get("checks_range", {}),
        "checks_periodic": item.get("checks_periodic", {}),
        "type": item.get("type", ""),
        "number": item.get("number", 0),
        "output_blocks": blocks,
        "word_count": len(joined.split()),
    }


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of task objects")
    return data


def task_id_for(index: int, item: dict[str, Any]) -> str:
    """The dataset has no id field; the index is the identity. Include the
    type so a stratified subset stays readable in filenames and logs."""
    type_slug = str(item.get("type", "task")).lower().replace(" ", "-")
    return f"{index:03d}-{type_slug}"
