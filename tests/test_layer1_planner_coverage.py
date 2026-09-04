"""PLAN.md §4.3 / TEST-PLAN.md §1.5 — Planner coverage.

Property-based and fully deterministic tests for build_tree and plan_level.
Verifies invariants over randomly generated spines (1-500 units, random token weights):
- the union of child spans covers every unit index 0..N-1;
- no two children overlap;
- every child's estimated load is within the leaf budget;
- child ids are unique;
- same spine + same seed -> identical partition.

Also verifies harness validation against deliberately bad partitions:
- gaps (assert harness fills gap and logs planner_partition_repaired);
- overlaps (assert harness truncates overlap and logs planner_partition_repaired);
- out-of-range indices (assert clamp to valid slice indices);
- duplicate ids (assert disambiguated to unique node ids);
- circular dependencies (assert cycle dropped with planner_dep_dropped).
"""

from __future__ import annotations

import random
import re
import tempfile
import unittest
from pathlib import Path

from kusudaemon.roles.protocol import RoleProvider
from kusudaemon.v0.events import EventLog
from kusudaemon.v2.planner import (
    DEFAULT_NODE_CAP,
    DEFAULT_TOKEN_BUDGET,
    DEFAULT_TOOL_CALL_CAP,
    build_tree,
)
from kusudaemon.v2.survey import SpineUnit


class _DeterministicPartitionProvider:
    """Deterministic partitioner for property-based spine tests."""

    def __init__(self, chunk_size: int = 8) -> None:
        self.chunk_size = chunk_size
        self.calls: list[list[dict]] = []

    def complete_json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        on_reasoning: object = None,
        streaming: bool = False,
    ) -> dict:
        self.calls.append(messages)
        content = messages[1]["content"]
        match = re.search(r"Slice has (\d+) units", content)
        n = int(match.group(1)) if match else 1

        children = []
        step = max(1, min(self.chunk_size, (n + 7) // 8))
        idx = 0
        child_num = 1
        while idx < n:
            end = min(n - 1, idx + step - 1)
            children.append(
                {
                    "id": f"c{child_num:02d}",
                    "brief": f"Handle slice units {idx} through {end}.",
                    "unit_start": idx,
                    "unit_end": end,
                    "estimated_calls": 3,
                    "shape": "prose-dominant",
                }
            )
            idx = end + 1
            child_num += 1
        return {"children": children}


class _ScriptedDictProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete_json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        on_reasoning: object = None,
        streaming: bool = False,
    ) -> dict:
        return self.payload


class Layer1PlannerCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def _make_random_spine(self, n_units: int, rng: random.Random) -> list[SpineUnit]:
        units = []
        for i in range(n_units):
            tokens = rng.randint(50, 4000)
            units.append(SpineUnit(f"u{i:04d}", f"Spine Unit {i}", i, i, tokens))
        return units

    def test_property_based_coverage_across_spine_sizes(self) -> None:
        """Property-based verification over random spines (1..500 units)."""
        spine_sizes = [1, 2, 5, 12, 45, 100, 250, 500]

        for size in spine_sizes:
            with self.subTest(spine_size=size):
                rng = random.Random(42 + size)
                spine = self._make_random_spine(size, rng)

                provider = _DeterministicPartitionProvider(chunk_size=10)
                tree = build_tree(
                    spine,
                    provider,  # type: ignore[arg-type]
                    token_budget=DEFAULT_TOKEN_BUDGET,
                    tool_call_cap=DEFAULT_TOOL_CALL_CAP,
                )

                # 1. Child ids are strictly unique
                node_ids = list(tree.nodes.keys())
                self.assertEqual(len(node_ids), len(set(node_ids)), f"Duplicate ids in tree: {node_ids}")

                # 2. Extract unit inputs covered by each node
                covered_indices: set[int] = set()
                for node_id, node in tree.nodes.items():
                    # Each input is a unit id like 'u0003'
                    node_indices = [int(u[1:]) for u in node.inputs]
                    # No overlap between children
                    intersection = covered_indices.intersection(node_indices)
                    self.assertEqual(
                        len(intersection),
                        0,
                        f"Node {node_id} overlaps units {intersection} with earlier nodes",
                    )
                    covered_indices.update(node_indices)

                    # 3. Every child's estimated load is within leaf budget
                    self.assertLessEqual(node.budget.calls, DEFAULT_TOOL_CALL_CAP)
                    self.assertLessEqual(node.budget.tokens, DEFAULT_TOKEN_BUDGET)

                # 4. The union covers every unit index 0..size-1
                expected_indices = set(range(size))
                self.assertEqual(
                    covered_indices,
                    expected_indices,
                    f"Spine size {size}: uncovered units {expected_indices - covered_indices}",
                )

    def test_partition_determinism(self) -> None:
        """Same spine + same seed -> identical partition."""
        rng1 = random.Random(1337)
        spine1 = self._make_random_spine(80, rng1)
        rng2 = random.Random(1337)
        spine2 = self._make_random_spine(80, rng2)

        tree1 = build_tree(spine1, _DeterministicPartitionProvider())  # type: ignore[arg-type]
        tree2 = build_tree(spine2, _DeterministicPartitionProvider())  # type: ignore[arg-type]

        self.assertEqual(list(tree1.nodes.keys()), list(tree2.nodes.keys()))
        for nid in tree1.nodes:
            self.assertEqual(tree1.nodes[nid].inputs, tree2.nodes[nid].inputs)
            self.assertEqual(tree1.nodes[nid].brief, tree2.nodes[nid].brief)

    def test_deliberately_bad_partition_gap_repaired(self) -> None:
        """Feed a partition with gaps; assert harness repairs it and logs event."""
        log = EventLog(self.tmp_path / "gap_events.jsonl")
        units = [SpineUnit(f"u{i}", f"Unit {i}", i, i, 100) for i in range(10)]
        bad_payload = {
            "children": [
                {"id": "c1", "brief": "part 1", "unit_start": 0, "unit_end": 2, "estimated_calls": 2, "shape": "prose-dominant"},
                {"id": "c2", "brief": "part 2", "unit_start": 6, "unit_end": 9, "estimated_calls": 2, "shape": "prose-dominant"},
            ]
        }
        tree = build_tree(units, _ScriptedDictProvider(bad_payload), log=log)  # type: ignore[arg-type]

        events = log.read_all()
        repair_events = [e for e in events if e.get("type") == "planner_partition_repaired"]
        self.assertTrue(len(repair_events) >= 1, "Expected planner_partition_repaired event for gap")
        self.assertIn("gap [3..5] filled", repair_events[0]["detail"])

        # Covered units must still be all 0..9
        covered = {int(u[1:]) for n in tree.nodes.values() for u in n.inputs}
        self.assertEqual(covered, set(range(10)))

    def test_deliberately_bad_partition_overlap_repaired(self) -> None:
        """Feed an overlapping partition; assert harness truncates overlap and logs event."""
        log = EventLog(self.tmp_path / "overlap_events.jsonl")
        units = [SpineUnit(f"u{i}", f"Unit {i}", i, i, 100) for i in range(10)]
        bad_payload = {
            "children": [
                {"id": "c1", "brief": "part 1", "unit_start": 0, "unit_end": 5, "estimated_calls": 2, "shape": "prose-dominant"},
                {"id": "c2", "brief": "part 2", "unit_start": 3, "unit_end": 9, "estimated_calls": 2, "shape": "prose-dominant"},
            ]
        }
        tree = build_tree(units, _ScriptedDictProvider(bad_payload), log=log)  # type: ignore[arg-type]

        events = log.read_all()
        repair_events = [e for e in events if e.get("type") == "planner_partition_repaired"]
        self.assertTrue(len(repair_events) >= 1, "Expected planner_partition_repaired event for overlap")
        self.assertIn("overlap c2 [3..9] truncated to [6..9]", repair_events[0]["detail"])

        # No two nodes share any unit
        seen: set[str] = set()
        for n in tree.nodes.values():
            for inp in n.inputs:
                self.assertNotIn(inp, seen, f"Unit {inp} appeared in multiple leaves")
                seen.add(inp)

    def test_deliberately_bad_partition_out_of_range_and_duplicate_ids(self) -> None:
        """Feed out-of-range indices and duplicate IDs; assert harness sanitizes them."""
        units = [SpineUnit(f"u{i}", f"Unit {i}", i, i, 100) for i in range(5)]
        bad_payload = {
            "children": [
                {"id": "same_id", "brief": "part 1", "unit_start": -10, "unit_end": 2, "estimated_calls": 2, "shape": "prose-dominant"},
                {"id": "same_id", "brief": "part 2", "unit_start": 3, "unit_end": 999, "estimated_calls": 2, "shape": "prose-dominant"},
            ]
        }
        tree = build_tree(units, _ScriptedDictProvider(bad_payload))  # type: ignore[arg-type]

        # Node ids must be unique
        node_ids = list(tree.nodes.keys())
        self.assertEqual(len(node_ids), len(set(node_ids)))
        self.assertIn("same_id", node_ids)
        self.assertIn("same_id-2", node_ids)

        # Indices clamped: all inputs correspond to valid unit ids in 0..4
        all_inputs = [u for n in tree.nodes.values() for u in n.inputs]
        self.assertEqual(set(all_inputs), {f"u{i}" for i in range(5)})

    def test_deliberately_bad_partition_cycles_dropped(self) -> None:
        """Feed cyclic dependencies; assert cycle dropped with planner_dep_dropped event."""
        log = EventLog(self.tmp_path / "cycle_events.jsonl")
        units = [SpineUnit(f"u{i}", f"Unit {i}", i, i, 100) for i in range(4)]
        bad_payload = {
            "children": [
                {
                    "id": "c1",
                    "brief": "part 1",
                    "unit_start": 0,
                    "unit_end": 1,
                    "estimated_calls": 2,
                    "shape": "prose-dominant",
                    "depends_on": ["c2"],
                },
                {
                    "id": "c2",
                    "brief": "part 2",
                    "unit_start": 2,
                    "unit_end": 3,
                    "estimated_calls": 2,
                    "shape": "prose-dominant",
                    "depends_on": ["c1"],
                },
            ]
        }
        tree = build_tree(units, _ScriptedDictProvider(bad_payload), log=log)  # type: ignore[arg-type]

        events = log.read_all()
        dropped_events = [e for e in events if e.get("type") == "planner_dep_dropped"]
        self.assertTrue(len(dropped_events) >= 1, "Expected planner_dep_dropped event for cycle")
        self.assertTrue(any("cycle detected" in str(e.get("reason")) for e in dropped_events))


if __name__ == "__main__":
    unittest.main()
