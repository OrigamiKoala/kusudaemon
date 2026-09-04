"""Tests for Phase 1.4: Reviewer precision/recall benchmarks.

Corpus: 30 artifacts (15 clean, 15 carrying exactly one planted defect across
contract rule violated, duplicate content from sibling leaf, coverage gap,
and register drift from pilot exemplar).

Includes one artifact over DEFAULT_ARTIFACT_CAP_TOKENS with its defect planted
in the final 10% to verify fan-out.

Hermetic by default; gated on KUSUDAEMON_LIVE_REVIEW=1 for live model evaluation.
Asserts non-regression against tests/fixtures/reviewer_baseline.json.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider
from kusudaemon.v1.gates import estimate_tokens
from kusudaemon.v1.provider import OpenAICompatibleProvider
from kusudaemon.v1.reviewer import (
    DEFAULT_ARTIFACT_CAP_TOKENS,
    ReviewVerdict,
    review_node,
)
from kusudaemon.v1.tree import TaskNode

_BASELINE_PATH = _REPO_ROOT / "tests" / "fixtures" / "reviewer_baseline.json"

DEFECT_CLASSES = (
    "contract_rule_violated",
    "duplicate_content",
    "coverage_gap",
    "register_drift",
)


@dataclass
class ReviewArtifactCase:
    name: str
    artifact_text: str
    is_clean: bool
    defect_class: str | None
    node: TaskNode
    defect_location_ratio: float | None = None


def _make_node(name: str, rubric: dict[str, str]) -> TaskNode:
    return TaskNode(
        id=name,
        brief=f"Implement {name}",
        artifact=f"out/{name}.md",
        gates=["nonempty"],
        judgment=list(rubric.keys()),
        rubric=rubric,
    )


def build_review_corpus() -> list[ReviewArtifactCase]:
    """Constructs the 30-artifact benchmark corpus (15 clean, 15 defective)."""
    cases: list[ReviewArtifactCase] = []

    # ------------------------------------------------------------------
    # 15 Clean Artifacts
    # ------------------------------------------------------------------
    clean_specs = [
        ("clean_b_tree", "B-Tree Implementation", "Complete discussion of B-tree node splitting, merging, and search algorithms."),
        ("clean_raft", "Raft Consensus", "Rigorous analysis of leader election, log replication, and safety properties."),
        ("clean_lsm", "LSM Trees", "Detailed architectural breakdown of memtable flushing, SSTable compaction, and bloom filters."),
        ("clean_tcp", "TCP Congestion Control", "Mathematical modeling of AIMD, slow start, congestion avoidance, and fast recovery."),
        ("clean_allocator", "Memory Allocator", "Deep dive into slab allocation, buddy systems, and internal fragmentation mitigation."),
        ("clean_compiler", "Compiler SSA Form", "Explanation of dominance frontiers, phi node placement, and dead code elimination."),
        ("clean_jit", "JIT Tiering", "Walkthrough of baseline compilation, profiling hooks, and speculative deoptimization."),
        ("clean_scheduler", "CFS Scheduler", "Analysis of virtual runtime, red-black tree task queues, and latency target bounds."),
        ("clean_crypto", "Zero Knowledge Proofs", "Detailed formulation of arithmetic circuits, R1CS, and Schwartz-Zippel lemma."),
        ("clean_gc", "Generational GC", "Clear separation of nursery scavenging, tenuring thresholds, and write barriers."),
        ("clean_parser", "LR(1) Parsing", "Formal description of item sets, canonical collections, and lookahead propagation."),
        ("clean_vfs", "Virtual File System", "Dentry caching, inode lifecycle, and mount point traversal algorithms."),
        ("clean_cache", "Cache Coherence", "MESI and MOESI protocol state transition diagrams and bus snooping mechanisms."),
        ("clean_async", "Event Loop Mechanics", "Epoll reactor patterns, ready list queues, and non-blocking socket state machines."),
        ("clean_vector", "SIMD Vectorization", "Auto-vectorization patterns, loop peeling, and memory alignment constraints."),
    ]

    for name, title, topic in clean_specs:
        rubric = {
            "completeness": f"Must thoroughly cover {topic}",
            "tone": "Formal academic and technical prose",
            "contract": "Must define all terms and include concrete algorithms",
        }
        text = (
            f"# {title}\n\n"
            f"## Overview\n{topic}\n\n"
            f"## Architecture and Invariants\n"
            f"The architecture follows formal verification standards. All invariants are strictly maintained.\n\n"
            f"## Implementation Details\n"
            f"1. Initialize data structures with bounds checking.\n"
            f"2. Execute deterministic state transitions.\n"
            f"3. Verify postconditions before commit.\n"
        )
        cases.append(ReviewArtifactCase(
            name=name,
            artifact_text=text,
            is_clean=True,
            defect_class=None,
            node=_make_node(name, rubric),
        ))

    # ------------------------------------------------------------------
    # 15 Defective Artifacts
    # ------------------------------------------------------------------
    # Class 1: contract_rule_violated (4 cases)
    c1_specs = [
        ("defect_c1_second_person", "Rule: Never use second person pronouns ('you', 'your')",
         "When you configure the driver, your settings might fail if you forget the flag."),
        ("defect_c1_no_todos", "Rule: No TODO comments or unwritten sections",
         "## Section 4\nTODO: write the proof for lemma 3 later before shipping."),
        ("defect_c1_forbidden_import", "Rule: Do not use third-party libraries; standard library only",
         "import requests\nresponse = requests.get('https://api.internal/data')"),
        ("defect_c1_required_footer", "Rule: Artifact must conclude with formal audit seal",
         "The analysis concludes without the required cryptographic verification seal."),
    ]
    for name, rule, violation in c1_specs:
        rubric = {"contract_rules": f"Mandatory requirement: {rule}"}
        text = f"# Technical Document {name}\n\nStandard overview.\n\n## Implementation\n{violation}\n"
        cases.append(ReviewArtifactCase(
            name=name,
            artifact_text=text,
            is_clean=False,
            defect_class="contract_rule_violated",
            node=_make_node(name, rubric),
            defect_location_ratio=0.7,
        ))

    # Class 2: duplicate_content (4 cases)
    c2_specs = [
        ("defect_c2_verbatim_paragraph", "Verbatim copied paragraph from Chapter 2",
         "The quick brown fox jumped over the lazy dog repeatedly. " * 10),
        ("defect_c2_cloned_algorithm", "Identical cloned code block from sibling leaf",
         "```python\ndef solve_problem_x():\n    return 42\n```\n" * 3),
        ("defect_c2_redundant_derivation", "Unnecessary duplicate mathematical derivation",
         "We derive the equation: E=mc^2. Once again, by identical steps, E=mc^2."),
        ("defect_c2_copied_table", "Identical table pasted twice in same artifact",
         "| Col A | Col B |\n|---|---|\n| 1 | 2 |\n\n| Col A | Col B |\n|---|---|\n| 1 | 2 |\n"),
    ]
    for name, defect_desc, dup_text in c2_specs:
        rubric = {"novelty": "No duplicate content or repeated sections from sibling leaves"}
        text = f"# Module {name}\n\nUnique introduction.\n\n## Duplicate Section\n{dup_text}\n"
        cases.append(ReviewArtifactCase(
            name=name,
            artifact_text=text,
            is_clean=False,
            defect_class="duplicate_content",
            node=_make_node(name, rubric),
            defect_location_ratio=0.6,
        ))

    # Class 3: coverage_gap (4 cases)
    c3_specs = [
        ("defect_c3_missing_error_handling", "Must cover edge cases and network disconnect errors",
         "Covers happy path only, omitting disconnect and timeout recovery entirely."),
        ("defect_c3_missing_span_units", "Must cover units 15 through 30 of the spine",
         "Covers units 15 through 20; completely drops units 21 through 30."),
        ("defect_c3_missing_benchmarks", "Must include latency and throughput benchmarks",
         "Provides architecture summary but provides zero benchmark measurements."),
        ("defect_c3_missing_threat_model", "Must provide STRIDE security threat model",
         "Explains authentication flows but omits the mandatory STRIDE threat model."),
    ]
    for name, req, desc in c3_specs:
        rubric = {"coverage": f"Required scope: {req}"}
        text = f"# Scope Review {name}\n\nPartial implementation.\n\n## Notes\n{desc}\n"
        cases.append(ReviewArtifactCase(
            name=name,
            artifact_text=text,
            is_clean=False,
            defect_class="coverage_gap",
            node=_make_node(name, rubric),
            defect_location_ratio=0.5,
        ))

    # Class 4: register_drift (2 standard + 1 huge over-cap artifact = 3 cases)
    c4_specs = [
        ("defect_c4_casual_slang", "Exemplar: academic rigor. Defect: conversational slang",
         "Hey ya'll! So basically this algorithm is super lit and does crazy stuff yo!"),
        ("defect_c4_marketing_pitch", "Exemplar: neutral engineering. Defect: promotional marketing hype",
         "Revolutionary, industry-leading, world-class synergy unleashing next-gen paradigms!"),
    ]
    for name, exemplar, drift in c4_specs:
        rubric = {"register": f"Maintain technical exemplar register: {exemplar}"}
        text = f"# Engineering Report {name}\n\n## Discussion\n{drift}\n"
        cases.append(ReviewArtifactCase(
            name=name,
            artifact_text=text,
            is_clean=False,
            defect_class="register_drift",
            node=_make_node(name, rubric),
            defect_location_ratio=0.8,
        ))

    # Special Case: Over DEFAULT_ARTIFACT_CAP_TOKENS with defect in final 10%
    # DEFAULT_ARTIFACT_CAP_TOKENS = 50_000 tokens -> ~37,500 words
    # Build 6 sections, 6,500 words each = 39,000 words (> 50,000 tokens)
    # Plant defect in Section 6 (words 36,000 to 39,000, i.e. final ~8%)
    overcap_sections = []
    for i in range(1, 6):
        section_body = "The subsystem executes deterministic protocol steps. " * 1200
        overcap_sections.append(f"# Section {i}\n\n{section_body}\n\n")

    # Section 6 has the register drift defect in its tail
    clean_s6_body = "The subsystem executes deterministic protocol steps. " * 1150
    defect_tail = "\n\nOMG folks, honestly I just gave up here, whatever lol, who cares about RFC specs!"
    overcap_sections.append(f"# Section 6\n\n{clean_s6_body}{defect_tail}\n")
    huge_text = "".join(overcap_sections)

    overcap_rubric = {
        "register": "Maintain consistent formal technical register across all sections",
    }
    cases.append(ReviewArtifactCase(
        name="defect_c4_huge_overcap_tail_drift",
        artifact_text=huge_text,
        is_clean=False,
        defect_class="register_drift",
        node=_make_node("defect_c4_huge_overcap_tail_drift", overcap_rubric),
        defect_location_ratio=0.95,
    ))

    return cases


def evaluate_reviewer_metrics(
    verdicts: dict[str, ReviewVerdict],
    corpus: list[ReviewArtifactCase],
    token_counts: list[int] | None = None,
) -> dict[str, Any]:
    """Computes precision, recall, per-class recall, and mean tokens per call."""
    tp = 0
    fp = 0
    fn = 0
    tn = 0
    class_tp: dict[str, int] = {c: 0 for c in DEFECT_CLASSES}
    class_fn: dict[str, int] = {c: 0 for c in DEFECT_CLASSES}

    case_map = {c.name: c for c in corpus}
    for name, verdict in verdicts.items():
        case = case_map[name]
        has_defect_detected = (verdict.verdict == "fail") or any(
            not item.get("pass", True) for item in verdict.items
        )

        if case.is_clean:
            if has_defect_detected:
                fp += 1
            else:
                tn += 1
        else:
            if has_defect_detected:
                tp += 1
                if case.defect_class:
                    class_tp[case.defect_class] += 1
            else:
                fn += 1
                if case.defect_class:
                    class_fn[case.defect_class] += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0

    per_class_recall: dict[str, float] = {}
    for c in DEFECT_CLASSES:
        tot = class_tp[c] + class_fn[c]
        per_class_recall[c] = class_tp[c] / tot if tot > 0 else 0.0

    mean_tokens = (
        float(sum(token_counts)) / max(1, len(token_counts))
        if token_counts
        else 0.0
    )

    return {
        "precision": precision,
        "recall": recall,
        "per_class_recall": per_class_recall,
        "mean_tokens_per_call": mean_tokens,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


class ReviewerPrecisionTest(unittest.TestCase):
    """Phase 1.4: Reviewer precision and recall benchmark suite."""

    def test_corpus_structure_and_invariants(self) -> None:
        corpus = build_review_corpus()
        self.assertEqual(len(corpus), 30, "Corpus must contain exactly 30 artifacts")

        clean_cases = [c for c in corpus if c.is_clean]
        defect_cases = [c for c in corpus if not c.is_clean]
        self.assertEqual(len(clean_cases), 15, "Corpus must have 15 clean artifacts")
        self.assertEqual(len(defect_cases), 15, "Corpus must have 15 defective artifacts")

        for cls in DEFECT_CLASSES:
            cls_cases = [c for c in defect_cases if c.defect_class == cls]
            self.assertGreaterEqual(
                len(cls_cases),
                3,
                f"Defect class '{cls}' must have at least 3 representations",
            )

        # Over-cap artifact checks
        overcap = next(c for c in corpus if c.name == "defect_c4_huge_overcap_tail_drift")
        tokens = estimate_tokens(overcap.artifact_text)
        self.assertGreater(
            tokens,
            DEFAULT_ARTIFACT_CAP_TOKENS,
            f"Over-cap artifact must exceed {DEFAULT_ARTIFACT_CAP_TOKENS} tokens (got {tokens})",
        )
        self.assertGreaterEqual(
            overcap.defect_location_ratio or 0.0,
            0.90,
            "Defect must be planted in the final 10% of the over-cap artifact",
        )

    def test_baseline_fixture_validity(self) -> None:
        self.assertTrue(_BASELINE_PATH.exists(), f"Baseline file {_BASELINE_PATH} must exist")
        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        self.assertIn("precision", baseline)
        self.assertIn("recall", baseline)
        self.assertIn("per_class_recall", baseline)
        self.assertIn("mean_tokens_per_call", baseline)
        for cls in DEFECT_CLASSES:
            self.assertIn(cls, baseline["per_class_recall"])

    def test_metrics_calculation_hermetic(self) -> None:
        corpus = build_review_corpus()
        # Simulated verdicts: all clean pass, all defective fail
        verdicts: dict[str, ReviewVerdict] = {}
        for c in corpus:
            if c.is_clean:
                verdicts[c.name] = ReviewVerdict(node_id=c.name, items=[], verdict="pass")
            else:
                verdicts[c.name] = ReviewVerdict(
                    node_id=c.name,
                    items=[{"id": "d1", "pass": False, "defect": "found defect"}],
                    verdict="fail",
                )

        metrics = evaluate_reviewer_metrics(verdicts, corpus, token_counts=[1000] * 30)
        self.assertEqual(metrics["precision"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        for cls in DEFECT_CLASSES:
            self.assertEqual(metrics["per_class_recall"][cls], 1.0)
        self.assertEqual(metrics["mean_tokens_per_call"], 1000.0)

    def test_live_reviewer_precision(self) -> None:
        """Runs the 30-artifact benchmark against a live provider when enabled."""
        if os.environ.get("KUSUDAEMON_LIVE_REVIEW") != "1":
            raise unittest.SkipTest("KUSUDAEMON_LIVE_REVIEW != 1 (live review suite disabled)")

        corpus = build_review_corpus()
        provider = OpenAICompatibleProvider()
        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))

        verdicts: dict[str, ReviewVerdict] = {}
        token_samples: list[int] = []

        for case in corpus:
            verdict = review_node(case.node, case.artifact_text, provider)
            verdicts[case.name] = verdict
            tokens = estimate_tokens(case.artifact_text)
            token_samples.append(tokens)

        metrics = evaluate_reviewer_metrics(verdicts, corpus, token_samples)

        # Print report
        print("\n=== Reviewer Precision & Recall Benchmark Report ===")
        print(f"Total Artifacts Tested: {len(corpus)}")
        print(f"Precision: {metrics['precision']:.3f} (baseline: {baseline['precision']:.3f})")
        print(f"Recall:    {metrics['recall']:.3f} (baseline: {baseline['recall']:.3f})")
        print("Per-Class Recall:")
        for cls, rec in metrics["per_class_recall"].items():
            base_rec = baseline["per_class_recall"].get(cls, 0.0)
            print(f"  - {cls}: {rec:.3f} (baseline: {base_rec:.3f})")
        print(f"Mean Tokens Per Call: {metrics['mean_tokens_per_call']:.1f}")

        # Assert non-regression against checked-in baseline
        self.assertGreaterEqual(
            metrics["precision"],
            baseline["precision"],
            f"Reviewer precision {metrics['precision']:.3f} regressed below baseline {baseline['precision']:.3f}",
        )
        self.assertGreaterEqual(
            metrics["recall"],
            baseline["recall"],
            f"Reviewer recall {metrics['recall']:.3f} regressed below baseline {baseline['recall']:.3f}",
        )
        for cls, base_rec in baseline["per_class_recall"].items():
            actual = metrics["per_class_recall"].get(cls, 0.0)
            self.assertGreaterEqual(
                actual,
                base_rec,
                f"Reviewer recall for '{cls}' ({actual:.3f}) regressed below baseline {base_rec:.3f}",
            )


if __name__ == "__main__":
    unittest.main()
