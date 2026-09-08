"""Unit tests for output spine synthesis and related decomposition features.
PLAN-TOKEN-ACCOUNTING.md §J1, §J2, §K1, §K2, §K3.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from kusudaemon.tokens import (
    count_file_tokens,
    count_tokens,
    expected_units,
    expected_units_info,
    extract_unit_delimiter,
)
from kusudaemon.v1.tree import NodeBudget, TaskNode, TaskTree
from kusudaemon.v2.survey import (
    SpineUnit,
    _extract_constraints_for_range,
    synthesize_output_spine,
)


class OutputSpineTest(unittest.TestCase):
    def test_synthesize_output_spine_basic(self) -> None:
        goal = (
            "Generate a 20-block text document, with at least 800 words per block.\n"
            "Block 1: Introduction and context.\n"
            "Block 5: Deep analysis of market dynamics.\n"
            "Blocks 10-15: Technical requirements and architecture.\n"
            "Block 20: Summary and concluding remarks.\n"
        )
        chunks, units = synthesize_output_spine(goal)
        self.assertTrue(len(units) >= 2)
        self.assertEqual(units[0].id, "unit-01")
        self.assertEqual(units[0].units_expected, 5)

        # Check that constraints are in chunks
        chunk_0_text = chunks[0].text
        self.assertIn("Introduction", chunk_0_text)
        self.assertIn("Deep analysis", chunk_0_text)

        # Check last chunk
        self.assertEqual(units[-1].units_expected, 5)
        self.assertIn("Summary", chunks[-1].text)

    def test_synthesize_output_spine_single_leaf_returns_none(self) -> None:
        # If expected units < 8, returns ([], [])
        goal = "Write 4 blocks of text describing the weather."
        chunks, units = synthesize_output_spine(goal)
        self.assertEqual(chunks, [])
        self.assertEqual(units, [])

    def test_extract_constraints_for_range(self) -> None:
        goal = (
            "Block 1: Start here\n"
            "Block 2-4: Middle steps\n"
            "Block 5: End here\n"
        )
        c1 = _extract_constraints_for_range(goal, 1, 1, "block")
        self.assertIn("Block 1: Start here", c1)

        c2 = _extract_constraints_for_range(goal, 2, 4, "block")
        self.assertIn("Block 2-4: Middle steps", c2)

        c5 = _extract_constraints_for_range(goal, 5, 5, "block")
        self.assertIn("Block 5: End here", c5)


class TokenAccountingManifestTest(unittest.TestCase):
    def test_declared_inputs_manifest_formatting(self) -> None:
        from kusudaemon.pipeline.prompts import _format_declared_inputs

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            f1 = run_dir / "doc1.txt"
            f1.write_text("hello world " * 50, encoding="utf-8")
            sub = run_dir / "notes"
            sub.mkdir()
            (sub / "note1.txt").write_text("test note " * 20, encoding="utf-8")
            (sub / "note2.txt").write_text("another note " * 30, encoding="utf-8")

            node = TaskNode(
                id="leaf-1",
                brief="Work on doc1",
                artifact="out/leaf-1.md",
                inputs=["doc1.txt", "notes"],
                gates=["nonempty"],
                budget=NodeBudget(tokens=1000),
            )
            manifest = _format_declared_inputs(node, run_dir)
            self.assertIn("Declared inputs:", manifest)
            self.assertIn("doc1.txt", manifest)
            self.assertIn("notes/", manifest)
            self.assertIn("2 files", manifest)
            self.assertIn("Leaf budget: ~1,000 tokens.", manifest)


class MaxParallelInertEventTest(unittest.TestCase):
    def test_max_parallel_inert_event_emission(self) -> None:
        import asyncio
        _REPO_ROOT = Path(__file__).resolve().parents[1]
        import sys
        if str(_REPO_ROOT / "tests" / "fixtures") not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

        from fake_adapter import FakeStreamAgentAdapter
        from fake_provider import FakeProvider
        from kusudaemon.environment.local import LocalEnvironment
        from kusudaemon.types import EpisodeBudget
        from kusudaemon.v0.events import EventLog
        from kusudaemon.v0.run_dir import create_run_dir
        from kusudaemon.v1.run_dir import tree_path
        from kusudaemon.v1.round_loop import run_round_loop

        fake_cli = Path(__file__).resolve().parent / "fixtures" / "fake_stream_agent.py"

        with tempfile.TemporaryDirectory() as root_str:
            root = Path(root_str)
            run_dir = create_run_dir(root, "run1")
            prompt_dir = root / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)

            tree_file = tree_path(run_dir)
            tree_file.write_text(
                json.dumps([
                    {"id": "a", "brief": "first", "artifact": "out/a.md", "gates": ["nonempty"]},
                    {"id": "b", "brief": "second", "artifact": "out/b.md", "gates": ["nonempty"], "depends_on": ["a"]},
                ]),
                encoding="utf-8",
            )

            def adapter_factory(node):
                return FakeStreamAgentAdapter(
                    script_path=str(fake_cli),
                    pidfile=str(root / f"{node.id}.pid"),
                    prompt_dir=str(prompt_dir),
                    workspace_path=str(run_dir),
                )

            provider = FakeProvider([])

            asyncio.run(
                run_round_loop(
                    run_dir,
                    tree_file,
                    writer_adapter_factory=adapter_factory,
                    env=LocalEnvironment(tmp_dir=str(prompt_dir)),
                    provider=provider,
                    prompt_for_node=lambda node: f"do {node.id}",
                    writer_budget=EpisodeBudget(max_duration_seconds=30),
                    max_parallel=4,
                )
            )

            events = EventLog(run_dir / "events.jsonl").read_all()
            inert_events = [e for e in events if e.get("type") == "max_parallel_inert"]
            self.assertEqual(len(inert_events), 1)
            self.assertEqual(inert_events[0]["max_parallel"], 4)
            self.assertEqual(inert_events[0]["max_ready_width"], 1)


if __name__ == "__main__":
    unittest.main()
