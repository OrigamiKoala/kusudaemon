#!/usr/bin/env python3
"""Score LongGenBench predictions without vLLM or a GPU (BENCHMARKING.md §4.1).

Upstream's `Evalution/eval.py` cannot run on this machine: it does
`from vllm import LLM` at module import and then instantiates
`meta-llama/Llama-3.3-70B-Instruct` across N GPUs. This is a faithful port of
its two metrics with that dependency removed.

  1. Completion rate  -- deterministic regex over `#*#`-delimited blocks.
                         Zero API calls, zero cost, exactly upstream's
                         `parse_blocks` + `calculate_completion_rate`.
  2. Instruction-following accuracy -- upstream's `create_prompts` verbatim
                         (same three few-shot examples, same yes/no
                         instruction), sent to any OpenAI-compatible
                         /chat/completions endpoint instead of a local vLLM.

Metric 1 is the headline number for an arms comparison and needs no
credentials at all:

    python3 scripts/eval_longgen_free.py \\
        bench_results/longgen/predictions/predictions_armA_seed1.json \\
        bench_results/longgen/predictions/predictions_armC_seed1.json

Metric 2 additionally needs a judge endpoint, and the judge model MUST be held
fixed across arms or the comparison is meaningless:

    export LONGGEN_JUDGE_BASE_URL=https://integrate.api.nvidia.com/v1
    export LONGGEN_JUDGE_API_KEY=$NVIDIA_API_KEY
    export LONGGEN_JUDGE_MODEL=moonshotai/kimi-k3
    python3 scripts/eval_longgen_free.py --judge <prediction files...>


NOTE ON THE NAME: two unrelated papers are called "LongGenBench". This
implements Wu et al., arXiv 2409.02076 (repo mozhu621/LongGenBench) -- the
block-structured diary/menu/skyscraper tasks scored by completion rate plus a
model-verified instruction-following accuracy. It is NOT Liu et al., arXiv
2410.04199 (repo Dominic789654/LongGenBench), which synthesises GSM8K/MMLU/CSQA
and scores deterministically against gold labels with no verifier model. The
judge here is upstream's, not ours. See BENCHMARKING.md 4.1 and 4.1.4.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from longgen_common import calculate_completion_rate, parse_blocks  # noqa: E402

# Verbatim from Evalution/eval.py::create_prompts -- changing these would
# change the metric, so they are copied rather than paraphrased.
_EXAMPLES = [
    "Example 1: Context: The district's new residential area will feature two 10-story apartment buildings, a shopping mall, and a park. The construction is set to begin at 9 AM, and the park will include a playground and jogging tracks. The plan also includes space for a small medical clinic, which will open from 10 AM to 6 PM. ### Instruction: Does this context include a medical clinic? Please answer with 'yes' or 'no' only. Answer: yes",
    "Example 2: Context: The menu for today's lunch at the office includes grilled turkey, mashed potatoes with gravy, roasted vegetables, and pumpkin pie for dessert. The meal will be served from 12 PM to 2 PM, and there will be a vegetarian option available. The meal is planned to accommodate 50 people, and the turkey will be served with cranberry sauce. ### Instruction: Does this context include mashed potatoes? Please answer with 'yes' or 'no' only. Answer: yes",
    "Example 3: Context: On April 15th, the weather was sunny with a high of 75°F. In the morning, I volunteered for a community cleanup from 9 AM to 12 PM. We collected trash and planted 20 new trees along the riverbank. After lunch, I helped organize the donation of clothes and food for a local shelter, where we served sandwiches and drinks. The day ended at 4 PM. ### Instruction: Does this context include long-distance running? Please answer with 'yes' or 'no' only. Answer: no",
]


def create_prompts(checks: dict[str, str], type_to_block: dict[int, str]) -> tuple[list[str], list[int]]:
    """Port of Evalution/eval.py::create_prompts.

    Note the upstream semantics, which are load-bearing for interpreting the
    number: a check whose block is *missing entirely* produces no prompt and
    is simply not counted. Accuracy is therefore conditional on the block
    existing, which is why it must always be read next to completion rate --
    a model that emits 3 of 52 blocks can post a high accuracy.
    """
    prompts: list[str] = []
    identifiers: list[int] = []
    for identifier, event_desc in checks.items():
        identifier = int(identifier)
        if identifier in type_to_block:
            prompt = (
                "\n".join(_EXAMPLES)
                + " \n ### Refer to the examples above for how to answer. \nContext: "
                + type_to_block[identifier]
                + f"\n\n### Instruction: Now, for the following context, does it include the {event_desc}? Please answer with 'yes' or 'no' only. Answer: "
            )
            prompts.append(prompt)
            identifiers.append(identifier)
    return prompts, identifiers


class Judge:
    """Minimal OpenAI-compatible /chat/completions client (stdlib only)."""

    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: int = 120,
                 retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries

    def ask(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 8,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_err: Exception | None = None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=payload, headers=headers
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return str(body["choices"][0]["message"]["content"]).strip().lower()
            except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
                last_err = exc
                if attempt < self.retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"judge call failed after {self.retries} attempts: {last_err}")


def score_file(path: Path, judge: Judge | None, *, max_checks: int | None) -> dict[str, Any]:
    entries = json.loads(path.read_text(encoding="utf-8"))

    completion_total = 0.0
    per_task = []
    prompts_by_kind: dict[str, list[str]] = {"once": [], "range": [], "periodic": []}

    for entry in entries:
        type_ = str(entry.get("type", ""))
        number = int(entry.get("number", 0))
        blocks = entry.get("output_blocks") or []
        found = parse_blocks(blocks, type_)
        rate = calculate_completion_rate(found, number)
        completion_total += rate
        per_task.append({
            "task_id": entry.get("task_id"),
            "type": type_,
            "blocks_found": len(found),
            "blocks_expected": number,
            "completion_rate": round(rate, 3),
            "word_count": entry.get("word_count", 0),
        })
        for kind in ("once", "range", "periodic"):
            p, _ids = create_prompts(entry.get(f"checks_{kind}", {}) or {}, found)
            prompts_by_kind[kind].extend(p)

    result: dict[str, Any] = {
        "file": str(path),
        "tasks": len(entries),
        "completion_rate": round(completion_total / len(entries), 3) if entries else 0.0,
        "mean_word_count": (
            round(sum(t["word_count"] for t in per_task) / len(per_task), 1) if per_task else 0.0
        ),
        "per_task": per_task,
    }

    if judge is None:
        result["accuracy"] = None
        result["checks_evaluable"] = {k: len(v) for k, v in prompts_by_kind.items()}
        return result

    accuracies: dict[str, float] = {}
    for kind, prompts in prompts_by_kind.items():
        subset = prompts[:max_checks] if max_checks else prompts
        if not subset:
            accuracies[kind] = 0.0
            continue
        yes = 0
        for i, prompt in enumerate(subset, 1):
            answer = judge.ask(prompt)
            if "yes" in answer:
                yes += 1
            if i % 25 == 0:
                print(f"    {kind}: {i}/{len(subset)}", file=sys.stderr, flush=True)
        accuracies[kind] = round(yes / len(subset), 4)
    result["accuracy"] = {
        "once": accuracies["once"],
        "range": accuracies["range"],
        "periodic": accuracies["periodic"],
        "average": round(sum(accuracies.values()) / 3, 4),
    }
    result["checks_evaluated"] = {
        k: len(v[:max_checks] if max_checks else v) for k, v in prompts_by_kind.items()
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("predictions", nargs="+", help="Prediction JSON files from run_longgen_bench.py.")
    p.add_argument("--judge", action="store_true",
                   help="Also compute instruction-following accuracy (needs an endpoint).")
    p.add_argument("--judge-base-url", default=os.environ.get("LONGGEN_JUDGE_BASE_URL", ""))
    p.add_argument("--judge-api-key", default=os.environ.get("LONGGEN_JUDGE_API_KEY", ""))
    p.add_argument("--judge-model", default=os.environ.get("LONGGEN_JUDGE_MODEL", ""))
    p.add_argument("--max-checks", type=int, default=None,
                   help="Cap judged checks per category per file (cost control). "
                        "Applied identically to every file or the comparison breaks.")
    p.add_argument("--output", default=None, help="Write the full JSON report here.")
    p.add_argument("--quiet", action="store_true", help="Suppress the per-task table.")
    return p


def main() -> int:
    args = build_parser().parse_args()

    judge = None
    if args.judge:
        if not args.judge_base_url or not args.judge_model:
            raise SystemExit(
                "--judge needs --judge-base-url and --judge-model "
                "(or LONGGEN_JUDGE_BASE_URL / LONGGEN_JUDGE_MODEL)."
            )
        judge = Judge(args.judge_base_url, args.judge_api_key, args.judge_model)
        print(f"judge: {args.judge_model} @ {args.judge_base_url}", file=sys.stderr)

    reports = []
    for raw in args.predictions:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise SystemExit(f"prediction file not found: {path}")
        print(f"scoring {path.name} ...", file=sys.stderr, flush=True)
        reports.append(score_file(path, judge, max_checks=args.max_checks))

    print("\n=== LongGenBench ===")
    header = f"{'file':<44} {'tasks':>5} {'completion%':>12} {'words':>8}"
    if judge is not None:
        header += f" {'acc_once':>9} {'acc_range':>10} {'acc_period':>11} {'acc_avg':>8}"
    print(header)
    print("-" * len(header))
    for rep in reports:
        line = (f"{Path(rep['file']).name:<44} {rep['tasks']:>5} "
                f"{rep['completion_rate']:>12.2f} {rep['mean_word_count']:>8.0f}")
        if judge is not None and rep.get("accuracy"):
            acc = rep["accuracy"]
            line += (f" {acc['once']:>9.3f} {acc['range']:>10.3f} "
                     f"{acc['periodic']:>11.3f} {acc['average']:>8.3f}")
        print(line)

    if not args.quiet:
        for rep in reports:
            print(f"\n-- {Path(rep['file']).name}")
            for t in rep["per_task"]:
                print(f"   {t['task_id'] or '?':<22} {t['type']:<11} "
                      f"{t['blocks_found']:>4}/{t['blocks_expected']:<4} "
                      f"{t['completion_rate']:>7.2f}%  {t['word_count']:>6} words")

    if args.output:
        Path(args.output).write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"\nwrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
