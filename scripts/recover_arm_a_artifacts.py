#!/usr/bin/env python3
"""Rebuild arm-A artifacts from the backend's session store (no model spend).

PLAN-SWEEP-REPAIR.md §Q1. ``bench --arm A`` wrote the bare CLI's stdout to
``raw/<task>_armA_seed<N>.txt`` and scored that. ``opencode run`` prints
assistant text only, so a coding agent that answers a generation prompt by
writing a script and reading the file back hands the document over through a
tool result and leaves narration on stdout. Every such cell scored 1-2 % on a
document that was complete on disk.

This walks the finished cells, recovers the most complete document each session
actually produced (``adapters.opencode_session``), and overwrites the raw
artifact only when the recovered text carries strictly more units than what is
already there. Provenance goes to ``raw/<stem>.provenance.json`` so a cell
harvested from a tool result is never quietly compared against one harvested
from a model's own response.

Then rescore for free:

    python3 scripts/recover_arm_a_artifacts.py --apply
    python3 scripts/run_longgen_bench.py --score-only --arms A --tasks 301 ...

Paths default to this machine's opencode data dir. Running from a VM that
mounts the real home elsewhere needs --db / --log / --workspaces-root, because
``Path.home()`` there is not the user's home (BENCHMARKING.md §4.1.5 gotcha).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from kusudaemon.adapters.opencode_session import (  # noqa: E402
    best_artifact,
    default_db_path,
    default_log_path,
    session_directory,
    session_ids_from_log,
)

DEFAULT_DELIMITER = "#*#"

# A unit of a LongGenBench answer is a 150-word-minimum prose block, so ~900+
# characters. A candidate far below that carries unit *headers* without their
# bodies -- a `grep "#*# Block 18"` excerpt, a head -3 -- and accepting one
# would credit the arm with blocks it never wrote: 301-block armA seed3's best
# candidate is 10 headers in 482 chars, which would score as 10 % against a
# recorded 1 %. Both numbers are wrong; only a re-run fixes that cell. The
# floor is deliberately well under one real unit so a genuine partial document
# still passes.
DEFAULT_MIN_CHARS_PER_UNIT = 400


def count_units(text: str, delimiter: str) -> int:
    return len(re.findall(re.escape(delimiter), text or ""))


def cells_from_raw(raw_dir: Path, tasks: list[str] | None) -> list[tuple[str, int, Path]]:
    out: list[tuple[str, int, Path]] = []
    for path in sorted(raw_dir.glob("*_armA_seed*.txt")):
        m = re.match(r"(.+)_armA_seed(\d+)\.txt$", path.name)
        if not m:
            continue
        task_id, seed = m.group(1), int(m.group(2))
        if tasks and not any(task_id.startswith(t) for t in tasks):
            continue
        out.append((task_id, seed, path))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results-dir", default=str(_REPO_ROOT / "bench_results" / "longgen"))
    p.add_argument("--workspaces-root", default=None,
                   help="Parent of the per-cell agent workspaces (default "
                        "~/.kusudaemon/bench_workspaces/longgen).")
    p.add_argument("--tasks", nargs="*", default=None,
                   help="Task id prefixes to limit to, e.g. 301 000.")
    p.add_argument("--delimiter", default=DEFAULT_DELIMITER,
                   help=f"Unit delimiter used to rank candidates (default {DEFAULT_DELIMITER!r}).")
    p.add_argument("--db", default=None, help="Path to opencode.db.")
    p.add_argument("--log", default=None, help="Path to opencode.log.")
    p.add_argument("--min-chars-per-unit", type=float, default=DEFAULT_MIN_CHARS_PER_UNIT,
                   help="Reject a candidate whose mean chars-per-unit is below this: "
                        f"it is an excerpt, not a document (default {DEFAULT_MIN_CHARS_PER_UNIT:g}).")
    p.add_argument("--apply", action="store_true",
                   help="Write the recovered artifacts. Without it, report only.")
    args = p.parse_args()

    results_dir = Path(args.results_dir).expanduser()
    raw_dir = results_dir / "raw"
    if not raw_dir.is_dir():
        print(f"no raw dir at {raw_dir}", file=sys.stderr)
        return 2
    ws_root = (
        Path(args.workspaces_root).expanduser()
        if args.workspaces_root
        else Path.home() / ".kusudaemon" / "bench_workspaces" / "longgen"
    )
    db_path = Path(args.db).expanduser() if args.db else default_db_path()
    log_path = Path(args.log).expanduser() if args.log else default_log_path()

    cells = cells_from_raw(raw_dir, args.tasks)
    if not cells:
        print("no arm-A cells found")
        return 0
    print(f"db:  {db_path}")
    print(f"log: {log_path}")
    print(f"ws:  {ws_root}")
    print(f"{len(cells)} arm-A cell(s)\n")

    improved = unchanged = unmapped = rejected = 0
    for task_id, seed, raw_path in cells:
        stem = f"{task_id}_armA_seed{seed}"
        current = raw_path.read_text(errors="replace") if raw_path.is_file() else ""
        have = count_units(current, args.delimiter)
        workspace = ws_root / stem
        sessions = session_ids_from_log(workspace, log_path=log_path)
        if not sessions:
            print(f"{stem:26s} units={have:4d}  -- no session mapped (log rotated?)")
            unmapped += 1
            continue
        # Compare by directory name, not absolute path: see the note in
        # session_ids_from_log about reading another machine's log.
        escaped = [
            d for d in (session_directory(s, db_path=db_path) for s in sessions)
            if d and Path(d).name != workspace.name
        ]
        cand = best_artifact(sessions, delimiter=args.delimiter, db_path=db_path)
        if cand is None:
            print(f"{stem:26s} units={have:4d}  -- session has no delimited text")
            unchanged += 1
            continue
        density = len(cand.text) / cand.markers if cand.markers else 0.0
        excerpt = density < args.min_chars_per_unit
        if excerpt:
            verdict = "EXCERPT-REJECTED"
        elif cand.markers > have:
            verdict = "IMPROVED"
        else:
            verdict = "keep"
        print(
            f"{stem:26s} units={have:4d} -> {cand.markers:4d}  "
            f"chars={len(current):7d} -> {len(cand.text):7d}  "
            f"[{cand.source}] {verdict}"
            + (f"  density={density:.0f}c/u" if excerpt else "")
            + ("  ESCAPED-WORKSPACE" if escaped else "")
        )
        if cand.detail:
            print(f"{'':26s}   via: {cand.detail!r}")
        if excerpt:
            rejected += 1
            continue
        if cand.markers <= have:
            unchanged += 1
            continue
        improved += 1
        if args.apply:
            raw_path.write_text(cand.text, encoding="utf-8")
            (raw_dir / f"{stem}.provenance.json").write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "arm": "A",
                        "seed": seed,
                        "artifact_source": cand.source,
                        "session_id": cand.session_id,
                        "sessions_seen": sessions,
                        "tool_detail": cand.detail,
                        "units_recovered": cand.markers,
                        "chars_per_unit": round(density, 1),
                        "units_before": have,
                        "chars_recovered": len(cand.text),
                        "chars_before": len(current),
                        "workspace": str(workspace),
                        "workspace_escaped_to": escaped or None,
                        "recovered_by": "scripts/recover_arm_a_artifacts.py",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    print(
        f"\n{improved} improved, {unchanged} unchanged, {rejected} excerpt-rejected, "
        f"{unmapped} unmapped"
        + ("" if args.apply else "  (dry run -- pass --apply to write)")
    )
    if rejected:
        print(
            "An excerpt-rejected cell produced a document that was never captured "
            "anywhere readable. Report it as unrecovered, not as its recorded score."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
