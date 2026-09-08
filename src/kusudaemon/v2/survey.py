"""Survey — structure discovery (PLAN.md §4.2). Three stages, uniform
output regardless of input structure:

1. **Mechanical chunking** (``chunk_text``, no model) — split on whatever
   boundaries exist: headings, page breaks, blank-line runs. A textbook's
   TOC and a folder of unstructured lecture notes both go through the same
   function; the difference shows up only in how much work stage 2 has to
   do.
2. **Windowed survey** (``survey_chunks``, model, tiny outputs) — walk
   chunks in overlapping windows. Each call sees only that window (never
   the whole corpus, §8) and emits only candidate boundaries — never a
   summary, never content.
3. **Spine assembly** (``assemble_spine``, harness, no model) — merge
   boundary votes across overlapping windows, apply a minimum-size floor,
   produce ``spine.json``.
4. **Materialization** (``materialize_units``, no model, PLAN-zeromem.md
   §7) — write each unit's verbatim chunk range to ``spine/<unit-id>.md``,
   so a leaf's ``inputs`` entries (``unit_input_path``) resolve to a real
   file a Writer's own tools can open, instead of an opaque id nothing on
   disk answers to.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

from ..v0.run_dir import write_text_atomic
from ..v1.gates import estimate_tokens
from ..roles.protocol import RoleProvider
from .run_dir import spine_path, spine_unit_path

DEFAULT_MIN_CHUNK_TOKENS = 80
# A2-2 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): size the window to the
# payload, not to a mental model of "12 big chunks". Each chunk contributes
# only a ~25-word preview (~40 tokens), so a window of 12 was absurdly
# small: 75% of every survey call was fixed overhead. 64/56 keeps an 8-chunk
# overlap (so boundary votes aren't split) and cuts the call count 7× while
# giving each call *more* context.
DEFAULT_WINDOW_SIZE = 64
DEFAULT_WINDOW_STRIDE = 56
# A2-3: hard fence on model-mode survey calls — a pathological corpus
# degrades to deterministic chunking for the remainder instead of issuing
# thousands of requests (the only unbounded call loop in the harness).
DEFAULT_MAX_SURVEY_CALLS = 60
DEFAULT_MIN_UNIT_TOKENS = 1_320
DEFAULT_CONFIDENCE_FLOOR = 0.5

DEFAULT_BOUNDARY_PERCENTILE = 0.75  # keep the top quartile of dissimilarity
DEFAULT_SMOOTHING_WINDOW = 2
HEADING_BOOST = 0.25

_HEADING_RE = re.compile(
    r"(?m)^(?:#{1,6}[ \t]+\S.*"
    r"|(?:chapter|section|part)\s+\d+\b.*"
    r"|\d+(?:\.\d+)*[.)][ \t]+\S.*)$",
    re.IGNORECASE,
)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_PAGE_BREAK_RE = re.compile(r"\f")


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int


def chunk_text(text: str, *, min_chunk_tokens: int = DEFAULT_MIN_CHUNK_TOKENS) -> list[Chunk]:
    """Deterministic, model-free split on headings / page breaks / blank-line
    runs, then folds any resulting fragment under ``min_chunk_tokens`` into
    its neighbor so stage 2 never sees a near-empty window entry."""
    if not text.strip():
        return []
    positions = {0, len(text)}
    for pattern in (_HEADING_RE, _PAGE_BREAK_RE, _BLANK_RUN_RE):
        for match in pattern.finditer(text):
            positions.add(match.start() if pattern is _HEADING_RE else match.end())
    ordered = sorted(positions)
    segments = [
        text[start:end] for start, end in zip(ordered, ordered[1:]) if text[start:end].strip()
    ]
    if not segments:
        segments = [text]
    merged = _merge_small_segments(segments, min_chunk_tokens)
    return [Chunk(index=i, text=segment, tokens=estimate_tokens(segment)) for i, segment in enumerate(merged)]


def _merge_small_segments(segments: list[str], min_tokens: int) -> list[str]:
    # §11.10.9: a running token count instead of re-tokenizing the growing
    # tail on every fold — the old loop was O(n²) in corpus size (it
    # re-estimated a string it was also concatenating in place).
    merged: list[str] = []
    running: list[int] = []
    for segment in segments:
        if merged and running[-1] < min_tokens:
            merged[-1] += segment
            running[-1] += estimate_tokens(segment)
        else:
            merged.append(segment)
            running.append(estimate_tokens(segment))
    if len(merged) > 1 and running[-1] < min_tokens:
        merged[-2] += merged[-1]
        merged.pop()
        running[-2] += running[-1]
        running.pop()
    return merged


DEFAULT_TARGET_UNIT_TOKENS = 26_500
DEFAULT_TARGET_MAX_CHUNKS = 100


def prefold_chunks(
    chunks: list[Chunk],
    *,
    max_tokens: int = DEFAULT_MIN_UNIT_TOKENS,
    target_max_chunks: int | None = None,
) -> list[Chunk]:
    """A2-4 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): merge adjacent chunks up
    to ~``max_tokens`` before surveying. ``assemble_spine`` folds
    sub-``DEFAULT_MIN_UNIT_TOKENS`` units away afterward anyway, so surveying
    the merged list loses no resolution while cutting the chunk count ~5–8×
    on a long corpus.

    When ``target_max_chunks`` is provided (or on multi-million-token inputs),
    adaptively scales the fold threshold so total chunk count is bounded.

    Folding is greedy left-to-right, and a chunk that alone exceeds
    ``max_tokens`` is kept as its own entry (never split) — it was already a
    large structural block ``chunk_text`` chose not to split."""
    if len(chunks) < 2:
        return chunks

    effective_max = max_tokens
    if target_max_chunks is not None and target_max_chunks > 0:
        total_tokens = sum(c.tokens for c in chunks)
        effective_max = max(max_tokens, total_tokens // target_max_chunks)

    merged: list[Chunk] = []
    for chunk in chunks:
        if merged and merged[-1].tokens < effective_max:
            prev = merged[-1]
            merged[-1] = Chunk(
                index=prev.index,
                text=prev.text + chunk.text,
                tokens=prev.tokens + chunk.tokens,
            )
        else:
            merged.append(chunk)
    return merged


@dataclass
class BoundaryVote:
    boundary_after: int
    label: str
    confidence: float


SURVEY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["boundaries"],
    "additionalProperties": False,
    "properties": {
        "boundaries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["boundary_after", "label", "confidence"],
                "additionalProperties": False,
                "properties": {
                    "boundary_after": {"type": "integer"},
                    "label": {"type": "string", "maxLength": 120},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}

_SURVEY_SYSTEM_PROMPT = (
    "You are surveying a corpus for structural boundaries. You never "
    "summarize and never quote content back — you see a window of "
    "consecutive chunks, each shown only as its first few words, and each "
    "labeled by its index within this window. Emit only candidate "
    "boundaries: the local chunk index after which a new structural unit "
    "begins, a short label for what follows, and a confidence from 0 to 1. "
    "A window with no real boundary should return an empty list. Respond "
    "with a single JSON object only."
)


def _render_window(window: list[Chunk]) -> str:
    lines = []
    for local_index, chunk in enumerate(window):
        # A2-2: 25 words instead of 15 — still tiny, and boundary detection
        # is mostly a lexical/structural judgment that benefits from a bit
        # more text.
        preview = " ".join(chunk.text.split()[:25])
        lines.append(f"{local_index}: {preview}")
    return "\n".join(lines)


def survey_chunks(
    chunks: list[Chunk],
    provider: RoleProvider,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    stride: int = DEFAULT_WINDOW_STRIDE,
    max_calls: int = DEFAULT_MAX_SURVEY_CALLS,
    on_reasoning: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    streaming: bool = False,
) -> list[BoundaryVote]:
    """Walk ``chunks`` in overlapping windows of ``window_size`` (advancing
    by ``stride``), collecting one boundary-vote call per window. Each call's
    ``boundary_after`` is local to its window; this converts it back to a
    global chunk index before returning.

    A2-3 (IMPLEMENTATION-PLAN-COST-AND-LIVE.md): ``max_calls`` fences the
    loop. A pathological corpus (tens of thousands of chunks) degrades to
    deterministic chunking for the remainder instead of issuing thousands of
    requests — the fence is code-side, per §A6's "cost fences are code-side".
    """
    votes: list[BoundaryVote] = []
    if len(chunks) < 2:
        return votes

    total_calls = 0
    s = 0
    while s < len(chunks) and total_calls < max_calls:
        w = chunks[s : s + window_size]
        if len(w) < 2:
            break
        total_calls += 1
        if s + window_size >= len(chunks):
            break
        s += stride

    start = 0
    calls = 0
    while start < len(chunks):
        window = chunks[start : start + window_size]
        if len(window) < 2:
            break
        if calls >= max_calls:
            break
        calls += 1
        if on_progress is not None:
            try:
                on_progress(calls, total_calls)
            except Exception:
                pass
        messages = [
            {"role": "system", "content": _SURVEY_SYSTEM_PROMPT},
            {"role": "user", "content": _render_window(window)},
        ]
        payload = provider.complete_json(
            messages, SURVEY_SCHEMA, on_reasoning=on_reasoning, streaming=streaming
        )
        for boundary in payload.get("boundaries", []):
            global_index = start + int(boundary["boundary_after"])
            if 0 <= global_index < len(chunks) - 1:
                votes.append(
                    BoundaryVote(
                        boundary_after=global_index,
                        label=str(boundary["label"])[:120],
                        confidence=float(boundary["confidence"]),
                    )
                )
        if start + window_size >= len(chunks):
            break
        start += stride
    return votes


def _label_for_chunk(chunk: Chunk) -> str:
    """Structural label, never a paraphrase (§3.2: the author's own words,
    with provenance): the first heading line in the chunk, stripped of
    leading ``#`` / numbering, else the first 8 words. Capped at 120 chars
    to match ``SURVEY_SCHEMA``'s ``maxLength``."""
    match = _HEADING_RE.match(chunk.text)
    if match is not None:
        label = _strip_heading_markers(match.group(0))
        if not label:
            label = match.group(0).lstrip("#").strip()
    else:
        label = " ".join(chunk.text.split()[:8])
    return label[:120]


def _strip_heading_markers(line: str) -> str:
    stripped = line.strip()
    stripped = stripped.lstrip("#").strip()
    stripped = re.sub(r"^(?:chapter|section|part)\s+\d+\b[.:]?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"^\d+(?:\.\d+)*[.)]\s*", "", stripped)
    return stripped.strip()


def survey_chunks_structural(
    chunks: list[Chunk],
    *,
    target_unit_tokens: int = DEFAULT_TARGET_UNIT_TOKENS,
    min_unit_tokens: int = DEFAULT_MIN_UNIT_TOKENS,
) -> list[BoundaryVote]:
    """Zero-token, model-free, dependency-free structural survey.

    Emits candidate boundaries from structural signals in the text:
    1. Author-declared headings (markdown #, chapter/section/part headers)
    2. Explicit page breaks (\\f)
    3. Target unit token budgeting to avoid oversized units on uniform text.

    Returns a list of BoundaryVotes compatible with assemble_spine without
    requiring any model calls or embedding packages.
    """
    if len(chunks) < 2:
        return []

    votes: list[BoundaryVote] = []
    running_tokens = 0

    for i in range(len(chunks) - 1):
        next_chunk = chunks[i + 1]
        running_tokens += chunks[i].tokens

        is_heading = _HEADING_RE.match(next_chunk.text) is not None
        has_page_break = "\f" in chunks[i].text or "\f" in next_chunk.text
        is_oversized = running_tokens >= target_unit_tokens

        if (is_heading or has_page_break or is_oversized) and running_tokens >= min_unit_tokens:
            confidence = 1.0 if is_heading else (0.85 if has_page_break else 0.7)
            votes.append(
                BoundaryVote(
                    boundary_after=i,
                    label=_label_for_chunk(next_chunk),
                    confidence=confidence,
                )
            )
            running_tokens = 0

    return votes


# Alias for backwards compatibility
survey_chunks_deterministic = survey_chunks_structural


@dataclass
class SpineUnit:
    id: str
    label: str
    start_chunk: int
    end_chunk: int
    tokens: int
    # PLAN.md §A3/§B1: workspace mode's unit content -- real file paths,
    # relative to a WorkObject's `root` (v6/work_object.py:survey_workspace).
    # Corpus mode keeps start_chunk/end_chunk as-is and leaves this empty;
    # exactly one of {members, start_chunk/end_chunk} is meaningful per unit.
    # Additive with a default so every existing spine.json (which has no
    # "members" key) loads unchanged -- see load_spine below.
    members: tuple[str, ...] = ()
    units_expected: int | None = None
    unit_delimiter: str | None = None


def assemble_spine(
    chunks: list[Chunk],
    votes: list[BoundaryVote],
    *,
    min_unit_tokens: int = DEFAULT_MIN_UNIT_TOKENS,
    max_unit_tokens: int = 2 * DEFAULT_TARGET_UNIT_TOKENS,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> list[SpineUnit]:
    """Harness-side merge (§4.2 stage 3, no model call): overlapping windows
    can vote on the same boundary more than once, so this keeps the
    highest-confidence vote per boundary index, drops anything below
    ``confidence_floor``, then folds any resulting unit under
    ``min_unit_tokens`` into a neighbor."""
    if not chunks:
        return []
    accepted: dict[int, BoundaryVote] = {}
    for vote in votes:
        if vote.confidence < confidence_floor:
            continue
        if not (0 <= vote.boundary_after < len(chunks) - 1):
            continue
        current = accepted.get(vote.boundary_after)
        if current is None or vote.confidence > current.confidence:
            accepted[vote.boundary_after] = vote

    boundary_indices = sorted(accepted)
    bounds = [-1, *boundary_indices, len(chunks) - 1]
    raw_units: list[list[Any]] = []
    for i in range(len(bounds) - 1):
        start_chunk = bounds[i] + 1
        end_chunk = bounds[i + 1]
        if bounds[i] in accepted:
            label = accepted[bounds[i]].label
        elif bounds[i] == -1 and chunks and _HEADING_RE.match(chunks[0].text):
            label = _label_for_chunk(chunks[0])
        else:
            label = "Opening section"
        tokens = sum(chunk.tokens for chunk in chunks[start_chunk : end_chunk + 1])
        raw_units.append([start_chunk, end_chunk, label, tokens])

    split = _split_oversized_units(raw_units, chunks, max_unit_tokens)
    merged = _apply_min_size_floor(split, min_unit_tokens)
    return [
        SpineUnit(id=f"unit-{i + 1:02d}", label=label, start_chunk=start, end_chunk=end, tokens=tokens)
        for i, (start, end, label, tokens) in enumerate(merged)
    ]


def _split_oversized_units(
    raw_units: list[list[Any]], chunks: list[Chunk], max_tokens: int
) -> list[list[Any]]:
    """Split units larger than max_tokens at chunk boundaries so spine units stay within budget."""
    if not chunks or max_tokens <= 0:
        return raw_units
    split_units: list[list[Any]] = []
    for start, end, label, tokens in raw_units:
        if tokens <= max_tokens or end <= start:
            split_units.append([start, end, label, tokens])
            continue
        cur_start = start
        cur_tokens = 0
        part_idx = 1
        for c_idx in range(start, end + 1):
            c_tok = chunks[c_idx].tokens
            if cur_tokens + c_tok > max_tokens and c_idx > cur_start:
                part_label = f"{label} (part {part_idx})"
                split_units.append([cur_start, c_idx - 1, part_label, cur_tokens])
                cur_start = c_idx
                cur_tokens = c_tok
                part_idx += 1
            else:
                cur_tokens += c_tok
        if cur_start <= end:
            part_label = f"{label} (part {part_idx})" if part_idx > 1 else label
            split_units.append([cur_start, end, part_label, cur_tokens])
    return split_units


def _apply_min_size_floor(raw_units: list[list[Any]], min_unit_tokens: int) -> list[list[Any]]:
    merged = [list(unit) for unit in raw_units]
    i = 0
    while i < len(merged) - 1:
        if merged[i][3] < min_unit_tokens:
            # Fold into the following unit; keep that unit's label (it's the
            # larger, more representative piece) and extend its start.
            merged[i + 1][0] = merged[i][0]
            merged[i + 1][3] += merged[i][3]
            merged.pop(i)
            continue
        i += 1
    if len(merged) > 1 and merged[-1][3] < min_unit_tokens:
        merged[-2][1] = merged[-1][1]
        merged[-2][3] += merged[-1][3]
        merged.pop()
    return merged


def save_spine(run_dir: str | Path, units: list[SpineUnit]) -> None:
    payload = [asdict(unit) for unit in units]
    write_text_atomic(spine_path(run_dir), json.dumps(payload, indent=2) + "\n")


def load_spine(run_dir: str | Path) -> list[SpineUnit]:
    path = spine_path(run_dir)
    if not path.exists():
        return []
    raw_text = path.read_text(encoding="utf-8").strip()
    raw = json.loads(raw_text) if raw_text else []
    # Records are constructed from the known SpineUnit field names only:
    # an unknown key (a spine.json written by a newer version) is dropped
    # instead of raising, and a field added later with a dataclass default
    # tolerates a legacy file that predates it — the tolerance the original
    # comment promised and SpineUnit(**item) never delivered (§11.11). A
    # record missing a *required* field still raises, exactly as the
    # dataclass demands. See materialize_units/unit_input_path
    # (PLAN-zeromem.md §7): a resumed pre-§7 run has a spine.json but no
    # materialized units, and unit_input_path already falls back to the
    # bare id in that case.
    known = {field.name for field in fields(SpineUnit)}
    units = []
    for item in raw:
        kwargs = {key: value for key, value in item.items() if key in known}
        # json round-trips a tuple as a list; SpineUnit.members is typed
        # tuple[str, ...] (frozen-adjacent convention this module follows
        # elsewhere -- SpineUnit itself isn't frozen, but every other tuple
        # field in this package stays a tuple after a load/save cycle).
        if "members" in kwargs:
            kwargs["members"] = tuple(kwargs["members"])
        units.append(SpineUnit(**kwargs))
    return units


def materialize_units(
    run_dir: str | Path, chunks: list[Chunk], units: list[SpineUnit]
) -> None:
    """Write ``spine/<unit-id>.md`` per unit — the verbatim concatenation of
    its chunk range (PLAN-zeromem.md §7) — so a leaf's ``inputs`` entries
    resolve to a real file instead of an opaque id nothing on disk answers
    to. Idempotent: a unit whose materialized file already holds the exact
    same text is left untouched rather than rewritten."""
    for unit in units:
        text = "".join(chunk.text for chunk in chunks[unit.start_chunk : unit.end_chunk + 1])
        path = spine_unit_path(run_dir, unit.id)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        path.write_text(text, encoding="utf-8")


def unit_input_path(run_dir: str | Path, unit: SpineUnit) -> str:
    """Resolve a unit to its materialized file, relative to ``run_dir``, when
    ``materialize_units`` has actually written one for it; otherwise fall
    back to the bare unit id, today's behavior — the resolver a legacy or
    unmaterialized run degrades to (PLAN-zeromem.md §7)."""
    path = spine_unit_path(run_dir, unit.id)
    if path.exists() and path.stat().st_size > 0:
        return str(path.relative_to(Path(run_dir)))
    return unit.id


def _extract_constraints_for_range(goal: str, start: int, end: int, noun: str) -> list[str]:
    """Extract code-routed constraints matching a unit's index range (PLAN-TOKEN-ACCOUNTING.md §J2)."""
    raw_lines = [line.strip() for line in re.split(r"(?<=[.!?])\s+|\n+", goal) if line.strip()]
    matched: list[str] = []
    for line in raw_lines:
        is_hit = False
        # 1. Range: e.g. "floors 63 to 67", "floors 63-67"
        for m in re.finditer(
            rf"\b(?:{noun}|floor|block|unit|item|entry|chapter|day|week|step|part)s?\s+(\d+)\s*(?:-|–|to)\s*(\d+)\b",
            line,
            re.IGNORECASE,
        ):
            r_s, r_e = int(m.group(1)), int(m.group(2))
            if max(start, r_s) <= min(end, r_e):
                is_hit = True
                break
        if is_hit:
            matched.append(line)
            continue

        # 2. Single ordinals: e.g. "floor 51"
        for m in re.finditer(
            rf"\b(?:{noun}|floor|block|unit|item|entry|chapter|day|week|step|part)\s+(\d+)\b",
            line,
            re.IGNORECASE,
        ):
            val = int(m.group(1))
            if start <= val <= end:
                is_hit = True
                break
        if is_hit:
            matched.append(line)
            continue

        # 3. Periodic rules: "every 20 floors starting from 40"
        m_per = re.search(
            rf"\bevery\s+(\d+)\s*(?:{noun}|floor|block|unit|item|entry|chapter|day|week|step|part)s?\s*(?:starting\s+from|from|after)?\s*(\d+)?",
            line,
            re.IGNORECASE,
        )
        if m_per:
            step = int(m_per.group(1))
            start_val = int(m_per.group(2)) if m_per.group(2) else step
            first_in_range = start_val
            if first_in_range < start:
                rem = (start - first_in_range) % step
                first_in_range = start if rem == 0 else start + (step - rem)
            if first_in_range <= end:
                is_hit = True
        if is_hit:
            matched.append(line)

    return matched


def synthesize_output_spine(
    goal: str,
    *,
    token_budget: int = 50_000,
    target_leaf_tokens: int = 4_000,
) -> tuple[list[Chunk], list[SpineUnit]]:
    """Synthesize an output spine when goal declares N >= _PLAN_MIN_OUTPUT_UNITS (PLAN-TOKEN-ACCOUNTING.md §J2)."""
    import math
    from ..tokens import count_tokens, expected_units, extract_unit_delimiter

    n = expected_units(goal)
    if not n or n < 8:
        return [], []

    delim = extract_unit_delimiter(goal)

    nouns = ("floor", "block", "chapter", "entry", "item", "problem", "section", "day", "week", "scene", "step", "part")
    noun = "unit"
    for cand in nouns:
        if re.search(rf"\b{cand}s?\b", goal, re.IGNORECASE):
            noun = cand
            break

    m_words = re.search(r"(?:at least|around|about|approximately|minimum of)?\s*(\d+)\s*words\s*(?:per|for each|each|in each)\b", goal, re.IGNORECASE)
    if not m_words:
        m_words = re.search(r"(?:each|every)\s+\w+\s+(?:should be|must be|is|needs to be)?\s*(?:at least|around|about|approximately)?\s*(\d+)\s*words\b", goal, re.IGNORECASE)
    if m_words:
        words = int(m_words.group(1))
        tokens_per_unit = count_tokens("word " * words)
    else:
        tokens_per_unit = 200

    units_per_leaf = max(1, target_leaf_tokens // max(tokens_per_unit, 50))
    units_per_leaf = max(5, min(25, units_per_leaf))
    if n <= units_per_leaf:
        units_per_leaf = max(1, math.ceil(n / 2))

    num_leaves = math.ceil(n / units_per_leaf)
    chunks: list[Chunk] = []
    units: list[SpineUnit] = []

    for i in range(num_leaves):
        start_idx = i * units_per_leaf + 1
        end_idx = min(n, (i + 1) * units_per_leaf)
        count_in_leaf = end_idx - start_idx + 1

        label = f"{noun.capitalize()}s {start_idx} to {end_idx}"
        unit_id = f"unit-{i + 1:02d}"

        constraints = _extract_constraints_for_range(goal, start_idx, end_idx, noun)
        constraint_bullets = "\n".join(f"- {c}" for c in constraints) if constraints else "- None specifically assigned to this range."
        delim_instruction = f"Use '{delim}' to separate the documentation for each {noun}." if delim else f"Format each {noun} clearly."

        context_text = (
            f"# Assigned Range: {noun.capitalize()}s {start_idx} to {end_idx} (Total: {count_in_leaf} {noun}s)\n\n"
            f"{delim_instruction}\n\n"
            f"## Specific Constraints for {label}:\n"
            f"{constraint_bullets}\n\n"
            f"## Complete Task Specification & Global Rules (Reference):\n"
            f"{goal.strip()}\n"
        )

        tokens = count_tokens(context_text)
        chunks.append(Chunk(index=i, text=context_text, tokens=tokens))
        units.append(
            SpineUnit(
                id=unit_id,
                label=label,
                start_chunk=i,
                end_chunk=i,
                tokens=count_in_leaf * tokens_per_unit,
                units_expected=count_in_leaf,
                unit_delimiter=delim,
            )
        )

    return chunks, units
