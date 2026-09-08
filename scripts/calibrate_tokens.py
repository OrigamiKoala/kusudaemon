#!/usr/bin/env python3
"""scripts/calibrate_tokens.py — reproduce PLAN-TOKEN-ACCOUNTING.md §0 stats from opencode.db."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    db_path = Path(os.environ.get("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))
    if not db_path.exists():
        print(f"opencode.db not found at {db_path}", file=sys.stderr)
        return 1

    uri = f"file:{db_path.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()

    cur.execute("""
        SELECT m.id, m.data, p.data
        FROM message m
        JOIN part p ON p.message_id = m.id
        ORDER BY m.id, p.id
    """)

    # Group parts by message
    messages: dict[str, dict] = {}
    msg_parts: dict[str, list[dict]] = {}

    for m_id, m_raw, p_raw in cur.fetchall():
        if m_id not in messages:
            try:
                m_data = json.loads(m_raw)
            except Exception:
                continue
            if m_data.get("role") != "assistant":
                continue
            tokens_data = m_data.get("tokens") or {}
            output_tokens = tokens_data.get("output") or 0
            if output_tokens < 200:
                continue
            messages[m_id] = m_data
            msg_parts[m_id] = []

        if m_id in msg_parts:
            try:
                p_data = json.loads(p_raw)
                msg_parts[m_id].append(p_data)
            except Exception:
                pass

    con.close()

    if not messages:
        print("No qualifying assistant messages (>= 200 output tokens) found.")
        return 0

    rows = []
    # For each message, reconstruct text and calculate metrics
    for m_id, m_data in messages.items():
        parts = msg_parts.get(m_id, [])
        output_tokens = (m_data.get("tokens") or {}).get("output", 0)
        model_id = m_data.get("modelID") or m_data.get("model") or "unknown"

        text_pieces = []
        has_tool = False
        for p in parts:
            ptype = p.get("type")
            if ptype in ("text", "reasoning"):
                t = p.get("text", "")
                if t:
                    text_pieces.append(t)
            elif ptype == "tool":
                has_tool = True
                state = p.get("state") or {}
                inp = state.get("input")
                if inp is not None:
                    if isinstance(inp, str):
                        text_pieces.append(inp)
                    else:
                        try:
                            text_pieces.append(json.dumps(inp))
                        except Exception:
                            pass

        recon_text = "\n".join(text_pieces)
        char_len = len(recon_text)
        word_count = len(recon_text.split())

        if output_tokens > 0 and char_len > 0:
            rows.append({
                "model": model_id,
                "output_tokens": output_tokens,
                "chars": char_len,
                "words": word_count,
                "is_prose": not has_tool,
                "text": recon_text,
            })

    print(f"Analyzed {len(rows)} assistant turns with output_tokens >= 200.\n")

    def median(vals: list[float]) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 != 0 else (s[mid - 1] + s[mid]) / 2.0

    def percentile(vals: list[float], p: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        k = (len(s) - 1) * (p / 100.0)
        f = int(k)
        c = f + 1
        if c < len(s):
            return s[f] + (k - f) * (s[c] - s[f])
        return s[f]

    # Error computations
    # 1. words / 0.75
    # 2. chars / 4
    # 3. chars / 3.44
    estimators = [
        ("words/0.75 (current)", lambda r: r["words"] / 0.75),
        ("chars/4", lambda r: r["chars"] / 4.0),
        ("chars/3.44 (fitted)", lambda r: r["chars"] / 3.44),
    ]

    print("| estimator | median abs. rel. error (all) | prose-only | tool turns |")
    print("|---|---|---|---|")
    for name, est_fn in estimators:
        err_all = [abs(est_fn(r) - r["output_tokens"]) / r["output_tokens"] for r in rows]
        err_prose = [abs(est_fn(r) - r["output_tokens"]) / r["output_tokens"] for r in rows if r["is_prose"]]
        err_tool = [abs(est_fn(r) - r["output_tokens"]) / r["output_tokens"] for r in rows if not r["is_prose"]]

        m_all = median(err_all) * 100
        m_prose = median(err_prose) * 100 if err_prose else 0.0
        m_tool = median(err_tool) * 100 if err_tool else 0.0
        print(f"| {name} | {m_all:.1f}% | {m_prose:.1f}% | {m_tool:.1f}% |")

    # Ratio & chars/token spread
    ratios = [((r["words"] / 0.75) / r["output_tokens"]) for r in rows]
    cpts = [r["chars"] / r["output_tokens"] for r in rows]

    print("\nDirection and spread of current heuristic:")
    print(f"estimate_tokens / true_output_tokens:  median {median(ratios):.3f}   p10 {percentile(ratios, 10):.3f}   p90 {percentile(ratios, 90):.3f}")
    print(f"chars per true output token:           median {median(cpts):.2f}    p10 {percentile(cpts, 10):.2f}    p90 {percentile(cpts, 90):.2f}")

    # By model breakdown
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)

    print("\nFitted chars-per-token, by model:")
    print("| model | n | chars/tok |")
    print("|---|---|---|")
    for m, mrows in sorted(by_model.items(), key=lambda x: len(x[1]), reverse=True):
        if len(mrows) >= 10:
            cpt = median([r["chars"] / r["output_tokens"] for r in mrows])
            print(f"| {m} | {len(mrows)} | {cpt:.2f} |")

    return 0


if __name__ == "__main__":
    sys.exit(main())
