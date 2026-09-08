"""Tests for zero-token structural survey and adaptive prefolding.

Verifies:
1. Heading and page-break detection without models or embeddings.
2. Target token unit spacing on uniform text without headings.
3. Adaptive prefolding on multi-million token corpora (e.g. 4.4M tokens).
4. Survey mode routing in RecursiveDriver (auto, structural, deterministic, and fallback).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "fixtures"))

from fake_provider import FakeProvider  # noqa: E402
from kusudaemon.pipeline.driver import RecursiveDriver, RunOptions  # noqa: E402
from kusudaemon.v2.survey import (  # noqa: E402
    Chunk,
    assemble_spine,
    chunk_text,
    load_spine,
    prefold_chunks,
    survey_chunks_structural,
)


class SurveyStructuralTest(unittest.TestCase):
    def test_fewer_than_two_chunks_returns_empty(self) -> None:
        self.assertEqual(survey_chunks_structural([]), [])
        self.assertEqual(survey_chunks_structural([Chunk(0, "single", 10)]), [])

    def test_detects_markdown_headings(self) -> None:
        chunks = [
            Chunk(0, "Intro text...", 500),
            Chunk(1, "## Chapter 1: Foundations\nContent of chapter 1...", 600),
            Chunk(2, "More content of chapter 1...", 500),
            Chunk(3, "## Chapter 2: Methods\nContent of chapter 2...", 600),
            Chunk(4, "More content of chapter 2...", 500),
        ]
        votes = survey_chunks_structural(chunks, min_unit_tokens=400)
        self.assertEqual(len(votes), 2)
        self.assertEqual(votes[0].boundary_after, 0)
        self.assertEqual(votes[0].label, "Foundations")
        self.assertEqual(votes[1].boundary_after, 2)
        self.assertEqual(votes[1].label, "Methods")

    def test_detects_page_breaks(self) -> None:
        chunks = [
            Chunk(0, "Page 1 content\f", 1000),
            Chunk(1, "Page 2 content starts here", 1000),
        ]
        votes = survey_chunks_structural(chunks, min_unit_tokens=400)
        self.assertEqual(len(votes), 1)
        self.assertEqual(votes[0].boundary_after, 0)

    def test_splits_oversized_uniform_corpus(self) -> None:
        # Uniform text without headings splits when target_unit_tokens is exceeded
        chunks = [Chunk(i, f"Body paragraph {i} without heading " * 100, 1000) for i in range(20)]
        votes = survey_chunks_structural(chunks, target_unit_tokens=5000, min_unit_tokens=1000)
        self.assertGreater(len(votes), 0)
        spine = assemble_spine(chunks, votes, min_unit_tokens=1000)
        self.assertGreater(len(spine), 1)
        # Verify units cover all chunks
        self.assertEqual(spine[0].start_chunk, 0)
        self.assertEqual(spine[-1].end_chunk, len(chunks) - 1)

    def test_adaptive_prefold_bounds_chunk_count_on_4m_tokens(self) -> None:
        # Simulate ~4.4M token corpus with 5,500 small 800-token chunks
        simulated_chunks = [Chunk(i, "word " * 600, 800) for i in range(5500)]
        self.assertEqual(sum(c.tokens for c in simulated_chunks), 4_400_000)

        # Standard prefold without target_max_chunks keeps ~5,500 chunks
        default_folded = prefold_chunks(simulated_chunks, max_tokens=800)
        self.assertEqual(len(default_folded), 5500)

        # Adaptive prefold with target_max_chunks=100 bounds chunks to ~100
        adaptive_folded = prefold_chunks(simulated_chunks, max_tokens=800, target_max_chunks=100)
        self.assertLessEqual(len(adaptive_folded), 105)
        self.assertGreaterEqual(len(adaptive_folded), 95)
        self.assertEqual(sum(c.tokens for c in adaptive_folded), 4_400_000)


class DriverSurveyModeTest(unittest.TestCase):
    def test_auto_survey_mode_uses_zero_model_calls_on_large_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            # 10 sections = large corpus (> 50k chars)
            source = "".join(f"## Section {i}\n" + ("word " * 2000) + "\n\n" for i in range(10))
            provider = FakeProvider([])  # No canned responses: will fail if complete_json is called!

            driver = RecursiveDriver(
                run_dir,
                provider=provider,
                options=RunOptions(goal="Summarize this 10-chapter document", source_text=source, survey_mode="auto"),
                writer_adapter_factory=lambda node: (_ for _ in ()).throw(AssertionError("unexpected writer dispatch")),
                research_adapter_factory=lambda node, q: (_ for _ in ()).throw(AssertionError("unexpected research dispatch")),
            )
            driver._write_source_and_spec()
            asyncio.run(driver._phase_survey())

            # Verify zero model calls were made to provider during survey
            self.assertEqual(len(provider.calls), 0)

            # Verify spine was created and loaded successfully
            units = load_spine(driver.run_dir)
            self.assertGreaterEqual(len(units), 5)
            self.assertTrue(all(u.label.startswith("Section") for u in units))

    def test_deterministic_mode_runs_zero_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as root_str:
            run_dir = Path(root_str) / "run"
            source = "## Part 1\n" + ("word " * 1500) + "\n\n## Part 2\n" + ("word " * 1500)
            provider = FakeProvider([])

            driver = RecursiveDriver(
                run_dir,
                provider=provider,
                options=RunOptions(goal="Summarize", source_text=source, survey_mode="deterministic"),
                writer_adapter_factory=lambda node: (_ for _ in ()).throw(AssertionError("unexpected writer dispatch")),
                research_adapter_factory=lambda node, q: (_ for _ in ()).throw(AssertionError("unexpected research dispatch")),
            )
            driver._write_source_and_spec()
            asyncio.run(driver._phase_survey())

            self.assertEqual(len(provider.calls), 0)
            units = load_spine(driver.run_dir)
            self.assertEqual(len(units), 2)


if __name__ == "__main__":
    unittest.main()
