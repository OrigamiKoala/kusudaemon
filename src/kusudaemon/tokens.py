"""src/kusudaemon/tokens.py — token counting and expected unit extraction.
PLAN-TOKEN-ACCOUNTING.md §A2, §A3, §I1.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path
from typing import Any

# §A2: Cached tokenizers
_HF_TOKENIZERS: dict[str, Any] = {}
_TIKTOKEN_ENCODING: Any = None
_TIKTOKEN_CHECKED: bool = False

_FILE_CACHE: dict[tuple[str, int, int, str | None], int] = {}


def _get_hf_tokenizer(model: str | None) -> Any | None:
    if not model:
        return None
    if model in _HF_TOKENIZERS:
        return _HF_TOKENIZERS[model]

    try:
        from tokenizers import Tokenizer  # type: ignore[import-not-found]
    except ImportError:
        _HF_TOKENIZERS[model] = None
        return None

    # Search local HF cache for tokenizer.json without any network
    hf_home = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")))
    hub_cache = Path(os.environ.get("HUGGINGFACE_HUB_CACHE", hf_home / "hub"))

    # Convert model name (e.g., 'nvidia/nemotron-3.5-lightning-30b-a3b' -> 'models--nvidia--nemotron-3.5-lightning-30b-a3b')
    parts = model.split("/")
    repo_dir_name = "models--" + "--".join(parts)
    model_dir = hub_cache / repo_dir_name

    tokenizer_path = None
    if model_dir.is_dir():
        for tok_file in model_dir.glob("snapshots/*/tokenizer.json"):
            if tok_file.is_file():
                tokenizer_path = tok_file
                break

    if tokenizer_path is not None:
        try:
            tok = Tokenizer.from_file(str(tokenizer_path))
            _HF_TOKENIZERS[model] = tok
            return tok
        except Exception:
            pass

    _HF_TOKENIZERS[model] = None
    return None


def _get_tiktoken_encoding() -> Any | None:
    global _TIKTOKEN_ENCODING, _TIKTOKEN_CHECKED
    if _TIKTOKEN_CHECKED:
        return _TIKTOKEN_ENCODING

    _TIKTOKEN_CHECKED = True
    try:
        import tiktoken  # type: ignore[import-not-found]
        # o200k_base universal proxy without network download
        _TIKTOKEN_ENCODING = tiktoken.get_encoding("o200k_base")
    except Exception:
        _TIKTOKEN_ENCODING = None
    return _TIKTOKEN_ENCODING


def count_tokens(text: str, model: str | None = None) -> int:
    """PLAN-TOKEN-ACCOUNTING.md §A2: count tokens using exact HF tokenizer -> tiktoken -> len//4."""
    if not text:
        return 0

    # 1. Exact HF tokenizer
    hf_tok = _get_hf_tokenizer(model)
    if hf_tok is not None:
        try:
            return len(hf_tok.encode(text).ids)
        except Exception:
            pass

    # 2. tiktoken o200k_base universal proxy
    enc = _get_tiktoken_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass

    # 3. chars/4 fallback (stdlib only, ~22% error)
    return max(1, len(text) // 4)


def count_file_tokens(path: str | Path, model: str | None = None) -> int:
    """PLAN-TOKEN-ACCOUNTING.md §A2: (path, mtime_ns, size) cached token counter for files."""
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size, model)
        if key in _FILE_CACHE:
            return _FILE_CACHE[key]
        text = p.read_text(encoding="utf-8", errors="replace")
        count = count_tokens(text, model)
        _FILE_CACHE[key] = count
        return count
    except Exception:
        return 0


# §I1 / §B: Expected units extraction
_NUMERIC_WORDS: tuple[str, ...] = (
    r"\d+",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "thousand",
)

_NUMERIC_TARGET_NOUNS: tuple[str, ...] = (
    "files?", "entries", "units?", "nodes?", "functions?", "classes?",
    "modules?", "tests?", "endpoints?", "components?", "pages?", "routes?",
    "items?", "todos?", "steps?", "phases?", "floors?", "blocks?", "problems?",
    "chapters?", "sections?", "days?", "weeks?", "scenes?", "parts?",
)

_TARGET_COUNT_RE = re.compile(
    r"\b("
    + "|".join(_NUMERIC_WORDS)
    + r")\s+(?:per\s+\w+|"
    + "|".join(_NUMERIC_TARGET_NOUNS)
    + r")\b",
    re.IGNORECASE,
)

_NAMED_ORDINAL_RE = re.compile(
    r"\b(?:floor|block|entry|item|problem|section|chapter|day|week|scene|step|part)s?\s+(\d+)\b",
    re.IGNORECASE,
)

_WORD_TO_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}


def expected_units(goal: str) -> int | None:
    """PLAN-TOKEN-ACCOUNTING.md §I1: extract declared target count or max named ordinal from goal.
    Returns None if no target is declared, never 0."""
    if not goal:
        return None

    counts: list[int] = []

    # Path 1: declared target count
    for m in _TARGET_COUNT_RE.finditer(goal):
        raw = m.group(1).lower()
        if raw.isdigit():
            v = int(raw)
            if v > 0:
                counts.append(v)
        elif raw in _WORD_TO_NUM:
            v = _WORD_TO_NUM[raw]
            if v > 0:
                counts.append(v)

    # Path 2: max named ordinal
    for m in _NAMED_ORDINAL_RE.finditer(goal):
        raw = m.group(1)
        if raw.isdigit():
            v = int(raw)
            if v > 0:
                counts.append(v)

    if not counts:
        return None
    return max(counts)


def expected_units_info(goal: str) -> tuple[int | None, str | None]:
    """PLAN-TOKEN-ACCOUNTING.md §I1, §I4: returns (expected_units, source)
    where source is 'declared', 'inferred' (from named ordinal), or None."""
    if not goal:
        return None, None

    declared_counts: list[int] = []
    for m in _TARGET_COUNT_RE.finditer(goal):
        raw = m.group(1).lower()
        if raw.isdigit():
            v = int(raw)
            if v > 0:
                declared_counts.append(v)
        elif raw in _WORD_TO_NUM:
            v = _WORD_TO_NUM[raw]
            if v > 0:
                declared_counts.append(v)

    inferred_counts: list[int] = []
    for m in _NAMED_ORDINAL_RE.finditer(goal):
        raw = m.group(1)
        if raw.isdigit():
            v = int(raw)
            if v > 0:
                inferred_counts.append(v)

    max_declared = max(declared_counts) if declared_counts else None
    max_inferred = max(inferred_counts) if inferred_counts else None

    if max_declared is not None and (max_inferred is None or max_declared >= max_inferred):
        return max_declared, "declared"
    if max_inferred is not None:
        return max_inferred, "inferred"
    return None, None


_DELIMITER_RE = re.compile(
    r"""(?:use\s+['"`]([^'"`]+)['"`]\s+(?:to\s+separate|as\s+a\s+separator)|(?:separate[ds]?|delimited)\s+(?:with|by)\s+['"`]([^'"`]+)['"`]|separator\s*(?:is|:)?\s*['"`]([^'"`]+)['"`])""",
    re.IGNORECASE,
)


def extract_unit_delimiter(goal: str) -> str | None:
    """PLAN-TOKEN-ACCOUNTING.md §I3: extract explicit unit separator if named in the goal."""
    if not goal:
        return None
    m = _DELIMITER_RE.search(goal)
    if m:
        for g in m.groups():
            if g:
                return g
    return None
