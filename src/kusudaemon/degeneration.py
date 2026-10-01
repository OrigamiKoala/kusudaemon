"""Detect degenerate model output: script soup, think-tag storms, loops.

2026-09-26. NVIDIA NIM's ``nemotron-3.5-lightning-30b-a3b`` sometimes
emits garbage, in both harness runs and bare OpenCode (arm A). Seen in the
run traces and the OpenCode store:

- **Script soup from the first token.** ``Let story алatlama should beschäd
  supplies nil. Nameुल�หมาย için tragen`` at a 10k-token context. Many
  writing systems in one short span. (U+FFFD alone is not a signal: a
  writer that reads a binary file quotes plenty of it.)
- **Think-tag storms.** 12k characters of ``</think></think>...``.
- **Latin word salad.** Real words in no syntax at all (``Let of fromells
  ...``, 294-s2): one script, so ``script_soup`` misses it, and no repeats, so
  ``repetition_loop`` does too. Ordinary English is 30-50 % function words;
  the salad is nearly none. See ``_word_salad`` for the guards.
- **Loops.** ``0. 0. 0. ...``, ``n. \\nn. ...``, or one sentence repeated
  for 30k characters.

What each detection triggers is decided by the caller: a role call restarts
fresh instead of carrying the garbage forward, a writer episode is stopped,
and a node whose last session ended in garbage is dispatched fresh instead
of resumed.

Stdlib only: ``adapters/_agent_worker.py`` runs as a standalone script and
imports this module by path. ``KUSUDAEMON_DEGENERATION_GUARD=0`` turns every
guard off.
"""

from __future__ import annotations

import os
import re
import unicodedata

_WINDOW = 3000
# Back-to-back tags only: prose and code that talk about think tags (this
# repo's own dev sessions do) never stack three of them in a row.
_THINK_STORM_RE = re.compile(r"(?:</?think>\s*){3,}")
# Distinct non-Latin writing systems in one window. Japanese alone uses
# three Unicode scripts (kanji, hiragana, katakana), so East Asian scripts
# count as one family.
_SCRIPT_FAMILIES_MIN = 3
_SCRIPT_LETTERS_MIN = 2
_SCRIPT_MIN_CHARS = 200
# Fraction of distinct 20-char shingles (step 10) in the window. Ordinary
# prose and code sit at 0.9+; the loops in the traces sat at 0.01-0.18.
_REPEAT_MIN_CHARS = 1200
_REPEAT_UNIQUE_MAX = 0.3
_SHINGLE = 20
_SHINGLE_STEP = 10

# Word salad: long, newline-poor, letters-only prose with almost no function
# words. Measured on every 3000-char window of this repo's docs and sources
# (tests/test_longgen_41_fixes.py): healthy prose sits at 0.25+ function
# words, so the 0.12 line leaves a wide margin.
_SALAD_MIN_CHARS = 1500
_SALAD_MIN_WORDS = 250
_SALAD_LETTER_RATIO = 0.97
_SALAD_STOPWORD_MAX = 0.12
_SALAD_WORDS_PER_NEWLINE = 30
_SALAD_DIGIT_MAX = 0.01
_WORD_RE = re.compile(r"[A-Za-z']+")
_STOPWORDS = frozenset(
    "the of and to a in is that it for on with as are was be this by or from at an not have has had but "
    "they you we he she his her their its which will would can could should there been were if so than "
    "then them these those what when who how all any each more most other some such no nor only own same "
    "too very do does did into out up about over after before between through during under again further "
    "once here why where while because until against both few just now also i my your our me us him".split()
)

_EAST_ASIAN = {"CJK", "HIRAGANA", "KATAKANA", "KATAKANA-HIRAGANA", "HANGUL", "IDEOGRAPHIC", "HALFWIDTH", "FULLWIDTH", "BOPOMOFO"}
_IGNORED = {"LATIN", "MODIFIER", "COMBINING", "SUPERSCRIPT", "SUBSCRIPT", "MATHEMATICAL", "DOUBLE-STRUCK", "SCRIPT", "BLACK-LETTER", "CIRCLED", "PARENTHESIZED", "SQUARED", "NEGATIVE"}


def guard_enabled() -> bool:
    return os.getenv("KUSUDAEMON_DEGENERATION_GUARD", "1").strip().lower() not in ("0", "false", "off", "no")


def _script_family(ch: str) -> str | None:
    try:
        first = unicodedata.name(ch).split(" ", 1)[0]
    except ValueError:
        return None
    if first in _IGNORED:
        return None
    if first in _EAST_ASIAN:
        return "EAST_ASIAN"
    return first


def _looks_binary(window: str) -> bool:
    """Decoded binary (a quoted PDF stream) reads as script soup too, but it
    carries control characters, which model garbage does not."""
    controls = sum(1 for ch in window if ord(ch) < 0x20 and ch not in "\n\r\t")
    return controls >= max(3, len(window) // 50)


def _script_soup(window: str) -> bool:
    if len(window) < _SCRIPT_MIN_CHARS or _looks_binary(window):
        return False
    counts: dict[str, int] = {}
    for ch in window:
        if ord(ch) < 0x80 or not ch.isalpha():
            continue
        family = _script_family(ch)
        if family is not None:
            counts[family] = counts.get(family, 0) + 1
    return sum(1 for n in counts.values() if n >= _SCRIPT_LETTERS_MIN) >= _SCRIPT_FAMILIES_MIN


def _word_salad_window(window: str) -> bool:
    if len(window) < _SALAD_MIN_CHARS:
        return False
    plain = sum(1 for ch in window if ch.isalpha() or ch in " \n'.,;!?-")
    if plain < _SALAD_LETTER_RATIO * len(window):
        return False
    if sum(ch.isdigit() for ch in window) > _SALAD_DIGIT_MAX * len(window):
        return False
    words = _WORD_RE.findall(window)
    if len(words) < _SALAD_MIN_WORDS:
        return False
    if window.count("\n") * _SALAD_WORDS_PER_NEWLINE > len(words):
        return False
    stop = sum(1 for w in words if w.lower() in _STOPWORDS)
    return stop / len(words) < _SALAD_STOPWORD_MAX


def _word_salad(text: str, window: int = _WINDOW) -> bool:
    """Real-looking words with no function words (LONGGENBENCH-RESULTS-2026-09
    §4.1 item 5, 294-s2).

    Function words alone cannot tell salad from telegraphic text: a menu
    written as ``fresh strawberries kiwi blueberries`` has 2 % of them, like
    the salad. So this is deliberately narrow: **two** consecutive windows
    must both be long, letters-only (no digits, colons or symbols),
    newline-poor and nearly free of function words. Code, tables, numbered
    lists, dates and short quoted drafts never qualify. Heuristic, tuned on
    this repo's text rather than on a captured salad sample."""
    if len(text) < 2 * _SALAD_MIN_CHARS:
        return False
    tail = text[-2 * window:]
    half = len(tail) // 2
    return _word_salad_window(tail[:half]) and _word_salad_window(tail[half:])


def _unique_shingle_ratio(window: str) -> float:
    shingles = [window[i:i + _SHINGLE] for i in range(0, len(window) - _SHINGLE, _SHINGLE_STEP)]
    if not shingles:
        return 1.0
    return len(set(shingles)) / len(shingles)


def degeneration_reason(text: str, *, window: int = _WINDOW) -> str | None:
    """Why the tail of ``text`` looks degenerate, or None when it looks fine.

    Only the last ``window`` characters are examined, so a long, healthy
    response that collapses at the end is caught and one that recovered is not.
    """
    if not text:
        return None
    tail = text[-window:]
    if _THINK_STORM_RE.search(tail):
        return "think_tag_leak"
    if _script_soup(tail):
        return "script_soup"
    if len(tail) >= _REPEAT_MIN_CHARS and _unique_shingle_ratio(tail) < _REPEAT_UNIQUE_MAX:
        return "repetition_loop"
    if _word_salad(text, window):
        return "word_salad"
    return None


class DegenerationMonitor:
    """Incremental ``degeneration_reason`` over a stream of text chunks.

    Keeps only the last ``window`` characters and re-checks every
    ``check_every`` new characters, so the cost per chunk stays bounded however
    long the stream runs. ``feed`` returns the reason once, on the chunk
    where it is first detected, and None before and after.
    """

    def __init__(self, *, window: int = _WINDOW, check_every: int = 400) -> None:
        self.window = window
        self.check_every = check_every
        self._buf = ""
        self._since_check = 0
        self.reason: str | None = None

    def feed(self, chunk: str) -> str | None:
        if self.reason is not None or not chunk:
            return None
        self._buf = (self._buf + chunk)[-2 * self.window:]
        self._since_check += len(chunk)
        if self._since_check < self.check_every:
            return None
        self._since_check = 0
        self.reason = degeneration_reason(self._buf, window=self.window)
        return self.reason
