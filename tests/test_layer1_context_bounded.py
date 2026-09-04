"""Layer 1 mechanism benchmark: context boundedness (TEST-PLAN.md §1.2).

Verifies that model context sizes (specifically the orchestrator's) do not grow
with corpus size or run length.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.eval import measure
from kusudaemon.eval.tasks import make_t2_large_corpus_task
from kusudaemon.v1.manifest import append_manifest_line
from kusudaemon.v1.gates import GateResult
from kusudaemon.v1.orchestrator import _compact_state, decide_next_action, DISPATCH_SCHEMA
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree
from _support import FakeProvider


class ContextBoundednessTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def _build_tree_of_size(self, n_nodes: int, ready_count: int = 5) -> TaskTree:
        nodes = {}
        for i in range(n_nodes):
            nid = f"node-{i:04d}"
            # first ready_count nodes are pending with no deps; rest depend on prior
            deps = [] if i < ready_count else [f"node-{(i-1):04d}"]
            nodes[nid] = TaskNode(
                id=nid,
                brief=f"Process section {i} content and derive deliverables.",
                artifact=f"out/{nid}.md",
                gates=["nonempty"],
                inputs=[f"spine/unit-{i:04d}.md"],
                budget=NodeBudget(tokens=10_000, calls=5),
                depends_on=deps,
                status="pending",
            )
        return TaskTree(nodes=nodes)

    def test_orchestrator_context_bounded_with_corpus_size(self) -> None:
        """Instantiate at 60 / 600 / 6000 units and assert max tokens ratio <= 1.15."""
        sizes = [60, 600, 6000]
        max_tokens: dict[str, dict[int, int]] = {"orchestrator": {}}

        manifest_path = self.tmp_path / "manifest.jsonl"
        for i in range(5):
            append_manifest_line(
                manifest_path,
                node_id=f"seed-{i}",
                artifact_path=f"out/seed-{i}.md",
                artifact_text="artifact body",
                gate_results=[GateResult(gate="exists", passed=True)],
                promotion="done",
            )

        for size in sizes:
            tree = self._build_tree_of_size(size, ready_count=5)
            provider = FakeProvider([
                {"action": "dispatch", "node_id": "node-0000", "reason": "first ready node"}
            ])

            decide_next_action(
                tree,
                str(manifest_path),
                provider,
                round_index=1,
                max_parallel=1,
            )

            orchestrator_calls = [
                call for call in provider.calls
                if measure.role_of_schema(call[1]) == "orchestrator"
            ]
            self.assertTrue(orchestrator_calls, f"Expected orchestrator call for size {size}")
            tokens = max(measure.call_input_tokens(c) for c in orchestrator_calls)
            max_tokens["orchestrator"][size] = tokens

        ratio = max_tokens["orchestrator"][6000] / max_tokens["orchestrator"][60]
        self.assertLessEqual(
            max_tokens["orchestrator"][6000],
            max_tokens["orchestrator"][60] * 1.15,
            f"Orchestrator context grew with corpus size: {max_tokens['orchestrator']} (ratio={ratio:.3f} > 1.15)",
        )

    def test_orchestrator_context_flat_across_50_rounds_of_redispatch(self) -> None:
        """Hold corpus fixed, force 50 rounds of redispatch, assert prompt is flat."""
        tree = self._build_tree_of_size(20, ready_count=5)
        manifest_path = self.tmp_path / "manifest_50.jsonl"

        prompt_tokens_by_round: list[int] = []

        for round_idx in range(1, 51):
            append_manifest_line(
                manifest_path,
                node_id=f"node-{round_idx:04d}",
                artifact_path=f"out/node-{round_idx:04d}.md",
                artifact_text="sample artifact text",
                gate_results=[GateResult(gate="nonempty", passed=True)],
                promotion=f"Artifact completed at round {round_idx} with verified quality gates.",
            )

            # Measure prompt via _compact_state
            ready = tree.ready_nodes()
            prompt = _compact_state(tree, ready[:5], str(manifest_path), round_idx)
            tokens = measure.estimate_tokens(prompt)
            prompt_tokens_by_round.append(tokens)

        # The manifest tail is clamped to 5 items, so prompt size stabilizes immediately
        # and stays flat from round 5 to round 50
        round_5_tokens = prompt_tokens_by_round[4]
        round_50_tokens = prompt_tokens_by_round[49]

        self.assertLessEqual(
            round_50_tokens,
            round_5_tokens * 1.05,
            f"Prompt grew over rounds: round 5={round_5_tokens}, round 50={round_50_tokens}",
        )


if __name__ == "__main__":
    unittest.main()
