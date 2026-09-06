from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.eval.measure import tokens_by_role
from kusudaemon.pipeline.driver import RunOptions
from kusudaemon.pipeline.prompts import build_node_prompt, segments
from kusudaemon.roles.backend_provider import BackendRoleProvider
from kusudaemon.roles.factory import _resolve_role_transport
from kusudaemon.types import EpisodeBudget
from kusudaemon.v0.cost import CostLedger, CostRecord
from kusudaemon.v1.reviewer import VERDICT_SCHEMA
from kusudaemon.v1.tree import TaskNode
from kusudaemon.v2.retrieval import top_k_for_budget
from kusudaemon.v2.survey import Chunk, SpineUnit, _split_oversized_units, assemble_spine
from kusudaemon.v3.document_review import DOC_REVIEW_SCHEMA


class TokenEfficiencyAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp_path = Path(td.name)

    def test_cost_record_token_estimation_fallback(self) -> None:
        ledger_path = self.tmp_path / "cost.jsonl"
        ledger = CostLedger(ledger_path)

        # When explicit tokens are provided
        rec1 = ledger.record(
            role="writer",
            phase="execute",
            prompt_tokens=100,
            completion_tokens=50,
        )
        self.assertFalse(rec1.estimated)
        self.assertEqual(rec1.prompt_tokens, 100)
        self.assertEqual(rec1.completion_tokens, 50)

        # When tokens are 0 but text is provided -> estimated tokens
        rec2 = ledger.record(
            role="role",
            phase="role",
            prompt_text="Hello world, this is a test prompt for cost estimation.",
            completion_text="Here is the output text.",
        )
        self.assertTrue(rec2.estimated)
        self.assertGreater(rec2.prompt_tokens, 0)
        self.assertGreater(rec2.completion_tokens, 0)

        all_records = ledger.read_all()
        self.assertEqual(len(all_records), 2)
        self.assertFalse(all_records[0]["estimated"])
        self.assertTrue(all_records[1]["estimated"])

    def test_schema_max_items_constraints(self) -> None:
        self.assertEqual(VERDICT_SCHEMA["properties"]["items"]["maxItems"], 12)
        self.assertEqual(DOC_REVIEW_SCHEMA["properties"]["items"]["maxItems"], 12)

    def test_episode_budget_max_output_tokens(self) -> None:
        budget = EpisodeBudget(max_duration_seconds=300, max_output_tokens=2048)
        self.assertEqual(budget.max_output_tokens, 2048)

        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            EpisodeBudget(max_output_tokens=0)

    def test_split_oversized_spine_units(self) -> None:
        # 5 chunks of 8,000 tokens each = 40,000 tokens
        chunks = [
            Chunk(index=i, text=f"Chunk {i}", tokens=8000)
            for i in range(5)
        ]
        raw_units = [[0, 4, "Mega Chapter", 40000]]
        # Split with max_tokens = 16000
        split = _split_oversized_units(raw_units, chunks, max_tokens=16000)
        self.assertEqual(len(split), 3)
        # First unit: chunks 0, 1 = 16k tokens
        self.assertEqual(split[0][0], 0)
        self.assertEqual(split[0][1], 1)
        self.assertEqual(split[0][3], 16000)
        # Second unit: chunks 2, 3 = 16k tokens
        self.assertEqual(split[1][0], 2)
        self.assertEqual(split[1][1], 3)
        self.assertEqual(split[1][3], 16000)
        # Third unit: chunk 4 = 8k tokens
        self.assertEqual(split[2][0], 4)
        self.assertEqual(split[2][1], 4)
        self.assertEqual(split[2][3], 8000)

    def test_top_k_for_budget(self) -> None:
        # Low budget (2,000 tokens) -> minimum floor of 4
        self.assertEqual(top_k_for_budget(2000, avg_chunk_tokens=800), 5)
        # High budget (50,000 tokens) -> maximum ceiling of 32
        self.assertEqual(top_k_for_budget(50000, avg_chunk_tokens=800), 32)
        # Zero or negative budget -> DEFAULT_TOP_K (8)
        self.assertEqual(top_k_for_budget(0), 8)

    def test_prompt_retry_resuming_skips_inlining(self) -> None:
        run_dir = self.tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        out_dir = run_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        node = TaskNode(
            id="n1",
            brief="write n1",
            artifact="out/n1.md",
            gates=["nonempty"],
            type="generic",
            last_defect="gate failure: line repetition",
        )
        (run_dir / node.artifact).write_text("Previous long artifact content here...", encoding="utf-8")

        # Not resuming -> inlines prior artifact
        prompt_fresh = build_node_prompt(node, run_dir, resuming=False)
        self.assertIn("Your previous artifact", prompt_fresh)
        self.assertIn("Previous long artifact content here", prompt_fresh)

        # Resuming -> does NOT inline prior artifact
        prompt_resuming = build_node_prompt(node, run_dir, resuming=True)
        self.assertNotIn("Your previous artifact", prompt_resuming)
        self.assertNotIn("Previous long artifact content here", prompt_resuming)

    def test_tokens_by_role_rollup(self) -> None:
        calls = [
            ([{"role": "user", "content": "hello world"}], VERDICT_SCHEMA),
            ([{"role": "user", "content": "another prompt"}], VERDICT_SCHEMA),
        ]
        summary = tokens_by_role(calls)
        self.assertIn("reviewer", summary)
        self.assertEqual(summary["reviewer"]["calls"], 2)
        self.assertGreater(summary["reviewer"]["input_tokens"], 0)

    def test_run_options_defaults(self) -> None:
        options = RunOptions()
        self.assertTrue(options.episode_cache)
        self.assertTrue(options.inline_spans)

    def test_resolve_role_transport_with_keys(self) -> None:
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "OPENAI_API_KEY",
                "NVIDIA_API_KEY",
                "KUSUDAEMON_PROVIDER_API_KEY",
                "KUSUDAEMON_ROLE_TRANSPORT",
                "KUSUDAEMON_ROLE_BACKEND",
            )
        }
        patcher = mock.patch.dict(os.environ, clean_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        empty_cfg = self.tmp_path / "empty_provider.json"
        empty_cfg.write_text("{}", encoding="utf-8")

        # Without api key -> opencode defaults to backend
        self.assertEqual(_resolve_role_transport("opencode", config_path=empty_cfg), ("opencode", "backend"))

        # With API key configured -> opencode resolves to http
        with mock.patch.dict(os.environ, {"KUSUDAEMON_PROVIDER_API_KEY": "sk-test-key-12345"}):
            self.assertEqual(_resolve_role_transport("opencode", config_path=empty_cfg), ("opencode", "http"))


if __name__ == "__main__":
    unittest.main()
