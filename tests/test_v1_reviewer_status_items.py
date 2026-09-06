"""Unit tests for PLAN-REVIEW-LATENCY-STATUS.md items:
- Brief plumbing in digest and reviewer/triage prompt
- Review skip reasons (C-4) and telemetry
- Triage provider fast path (T1-2)
- Templates _PROSE and _DIRECT (C-1/C-2, §7.4, §9.2, §9.6)
- Factual claims gate (T2-3)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider
from kusudaemon.v0.events import EventLog
from kusudaemon.v1.gates import evaluate_gates
from kusudaemon.v1.reviewer import (
    GATE_COVERED_JUDGMENTS,
    ReviewVerdict,
    compute_verdict_digest,
    review_node,
)
from kusudaemon.v1.round_loop import audit_path, review_and_transition_node
from kusudaemon.v1.tree import TaskNode, TaskTree
from kusudaemon.v6.direct import build_direct_node, build_single_node_tree
from kusudaemon.v6.templates import _DIRECT, _PROSE, template_for


class ReviewerStatusItemsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def test_brief_digest_invalidation(self) -> None:
        text = "## Heading\n\nContent"
        rubric = {"on_topic": "stay on topic"}
        judgment = ["on_topic"]
        d1 = compute_verdict_digest(text, rubric, judgment, contract_text="contract 1", brief="brief A")
        d2 = compute_verdict_digest(text, rubric, judgment, contract_text="contract 1", brief="brief B")
        self.assertNotEqual(d1, d2, "Changing brief must invalidate digest")

    def test_skip_reason_empty_judgment(self) -> None:
        node = TaskNode(id="n1", brief="test", artifact="out/n1.md", gates=["nonempty"], judgment=[], rubric={})
        verdict = review_node(node, "artifact text", FakeProvider([]))
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.skip_reason, "empty_judgment")

    def test_skip_reason_all_filtered(self) -> None:
        node = TaskNode(
            id="n1",
            brief="test",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["coverage_gap"],  # CROSS_LEAF_JUDGMENTS item, stripped by T1-4
            rubric={"coverage_gap": "rule"},
        )
        verdict = review_node(node, "artifact text", FakeProvider([]))
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.skip_reason, "all_filtered")

    def test_skip_reason_gate_covered_prefilter(self) -> None:
        node = TaskNode(
            id="n1",
            brief="test",
            artifact="out/n1.md",
            gates=["headers:std"],
            judgment=["headers:std"],
            rubric={"headers:std": "check headers"},
        )
        text = "# Title\n\n## Section\n\nValid text here."
        verdict = review_node(node, text, FakeProvider([]))
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.skip_reason, "gate_covered_prefilter")

    def test_triage_provider_fast_path(self) -> None:
        node = TaskNode(
            id="n1",
            brief="test",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=["semantic_check"],
            rubric={"semantic_check": "needs deep review"},
        )
        triage_provider = FakeProvider([{"suspect": False, "reason": "clean"}])
        main_provider = FakeProvider([])  # Should not be called!

        verdict = review_node(
            node,
            "artifact text",
            main_provider,
            triage_provider=triage_provider,
        )
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(len(triage_provider.calls), 1)
        self.assertEqual(len(main_provider.calls), 0)

    def test_templates_prose_and_direct(self) -> None:
        self.assertIn("on_topic", _PROSE.judgment)
        self.assertIn("claims_supported", _PROSE.judgment)
        self.assertEqual(_PROSE.judgment_classification.get("on_topic"), "closed")
        self.assertEqual(_PROSE.judgment_classification.get("claims_supported"), "closed")

        self.assertIn("on_topic", _DIRECT.judgment)
        self.assertIn("claims_supported", _DIRECT.judgment)
        self.assertEqual(_DIRECT.shapes, ())
        self.assertEqual(_DIRECT.gates, ())
        self.assertEqual(_DIRECT.tools, ())

        # template_for never resolves _DIRECT
        self.assertNotEqual(template_for("direct").name, "direct")

        # build_direct_node respects apply_template
        node_bare = build_direct_node("bare goal", apply_template=False)
        self.assertEqual(node_bare.judgment, [])

        node_direct = build_direct_node("direct goal", apply_template=True)
        self.assertIn("on_topic", node_direct.judgment)
        self.assertIn("claims_supported", node_direct.judgment)

        # build_single_node_tree defaults to apply_template=True
        tree = build_single_node_tree("single goal")
        single_node = tree.nodes["single"]
        self.assertIn("on_topic", single_node.judgment)

    def test_claims_resolve_gate(self) -> None:
        claims_file = self.tmp_path / "n1_claims.jsonl"
        # 1. Clean claims
        claims_file.write_text(
            json.dumps({"assertion": "Earth orbits the Sun", "ref": "ch1"}) + "\n",
            encoding="utf-8",
        )
        text = "# Science\n\nEarth orbits the Sun [ref:ch1]."
        res = evaluate_gates([f"claims_resolve:{claims_file}"], text)
        self.assertTrue(res[0].passed)

        # 2. Uncited claim
        claims_file.write_text(
            json.dumps({"assertion": "Uncited fact", "ref": ""}) + "\n",
            encoding="utf-8",
        )
        res_uncited = evaluate_gates([f"claims_resolve:{claims_file}"], text)
        self.assertFalse(res_uncited[0].passed)
        self.assertIn("uncited claim", res_uncited[0].detail)

        # 3. Unresolved ref
        claims_file.write_text(
            json.dumps({"assertion": "Fact", "ref": "ch99"}) + "\n",
            encoding="utf-8",
        )
        res_unresolved = evaluate_gates([f"claims_resolve:{claims_file}"], text)
        self.assertFalse(res_unresolved[0].passed)
        self.assertIn("unresolved claim ref", res_unresolved[0].detail)

    async def test_round_loop_review_skip_event_and_audit(self) -> None:
        run_dir = self.tmp_path / "run"
        run_dir.mkdir()
        out_dir = run_dir / "out"
        out_dir.mkdir()

        node = TaskNode(
            id="n1",
            brief="brief for n1",
            artifact="out/n1.md",
            gates=["nonempty"],
            judgment=[],  # triggers empty_judgment
            rubric={},
        )
        (run_dir / node.artifact).write_text("Hello world", encoding="utf-8")
        tree = TaskTree(nodes={node.id: node})
        tree_path = run_dir / "tree.json"
        tree.save(tree_path)

        log = EventLog(run_dir / "events.jsonl")
        await review_and_transition_node(
            run_dir,
            node,
            tree,
            tree_path,
            provider=FakeProvider([]),
            max_attempts=2,
            log=log,
        )

        # Verify review_skipped_no_judgment was logged
        events = log.read_all()
        skip_events = [e for e in events if e.get("type") == "review_skipped_no_judgment"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0].get("reason"), "empty_judgment")

        # Verify audit file recorded skip_reason
        a_path = audit_path(run_dir, node.id)
        self.assertTrue(a_path.exists())
        audit_data = json.loads(a_path.read_text(encoding="utf-8"))
        self.assertEqual(audit_data.get("skip_reason"), "empty_judgment")


if __name__ == "__main__":
    unittest.main()
