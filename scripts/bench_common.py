"""Re-export bench utilities for scripts (PLAN-BENCH-INTEGRITY.md §4, §5)."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kusudaemon.eval.common import (  # noqa: E402
    check_identical_seeds,
    classify_halt,
    dedupe_records,
    parse_flags,
    read_records_jsonl,
)

__all__ = [
    "check_identical_seeds",
    "classify_halt",
    "dedupe_records",
    "parse_flags",
    "read_records_jsonl",
]
