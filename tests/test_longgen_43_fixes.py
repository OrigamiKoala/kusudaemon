"""Hermetic tests for the LongGenBench data-integrity fixes
(docs/LONGGENBENCH-RESULTS-2026-09.md section 4.3, items 1-9, plus the caffeinate wrapper).

No network, no model call, no OpenCode install: the OpenCode store is a throwaway
sqlite file and the log a text file in a temp dir.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import longgen_common as lc  # noqa: E402
import longgen_integrity as li  # noqa: E402
import run_longgen_bench as rlb  # noqa: E402
from kusudaemon.eval.common import classify_halt  # noqa: E402

WEEK = {
    "type": "Week",
    "number": 4,
    "prefix": "#*# Week 1 (Jan 1 - Jan 7):",
    "prompt": "Write a diary.\n*** started ***\n#*# Week 1 (Jan 1 - Jan 7):",
}


def unit(n: int, body: str = "Dear diary, today was fine.") -> str:
    return f"#*# Week {n} (x):\n{body}\n"


def cr(text: str, item: dict | None = None) -> float:
    item = item or WEEK
    value, _ = rlb.score_text(text, item)
    return value


def rec(task="000-week", arm="C", seed=1, rate=100.0, **kw) -> dict:
    base = {
        "benchmark": "longgenbench", "task_id": task, "dataset_index": 0, "arm": arm, "seed": seed,
        "completion_rate": rate, "blocks_found": 4 if rate >= 100 else 1, "blocks_expected": 4,
        "halt_reason": None, "tokens_by_role": {}, "wall_clock_s": 10.0, "harness_wall_clock_s": 10.0,
        "artifact_chars": 100,
    }
    base.update(kw)
    return base


class EmptyOutputScoringTest(unittest.TestCase):
    """4.3 item 2: an empty or absent output scores 0, not 1/N."""

    def test_empty_output_scores_zero(self) -> None:
        self.assertEqual(cr(""), 0.0)
        self.assertEqual(cr("   \n  "), 0.0)

    def test_unstructured_narration_scores_zero(self) -> None:
        self.assertEqual(cr("The.supported.\n"), 0.0)
        self.assertEqual(cr("Let me analyze this task carefully. I need to write 52 weeks."), 0.0)

    def test_document_that_restates_week_one_still_counts_it(self) -> None:
        text = unit(1) + unit(2)
        self.assertEqual(cr(text), 50.0)

    def test_continuation_of_the_prompt_credits_block_one(self) -> None:
        # The prompt ends mid-document ("#*# Week 1 (...):"); a model that
        # continues it writes the body of week 1 with no heading, then week 2.
        text = "Dear diary, the year began quietly.\n" + unit(2) + unit(3)
        self.assertEqual(cr(text), 75.0)

    def test_document_starting_at_a_later_unit_does_not_get_block_one_free(self) -> None:
        text = unit(2) + unit(3)
        self.assertEqual(cr(text), 50.0)  # weeks 2 and 3 only
        self.assertEqual(cr(unit(22)), 0.0)  # week 22 is outside this 4-week task

    def test_stray_started_marker_between_units_does_not_eat_week_one(self) -> None:
        # 247-menu-week armC seed2 wrote "*** started ***" after every week; the
        # blind cut-at-first-marker deleted week 1 and the prefix hid it.
        text = unit(1) + "\n*** started ***\n\n" + unit(2) + unit(3) + unit(4) + "*** finished ***"
        self.assertEqual(cr(text), 100.0)
        # ... and a genuine prompt echo is still dropped, not scored.
        echo = "Write a diary.\n*** started ***\n" + unit(2)
        self.assertEqual(cr(echo), 25.0)

    def test_finished_sentinel_still_cuts_at_the_last_one(self) -> None:
        text = unit(1) + unit(2) + "*** finished ***\n" + unit(3) + unit(4) + "*** finished ***"
        self.assertEqual(cr(text), 100.0)


class WriteBackTest(unittest.TestCase):
    """4.3 item 1: arm A score/resolved/termination are not the exit code."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _bench(self, **kw) -> Path:
        data = {"arm": "A", "score": 1.0, "resolved": True, "termination": "gates_satisfied",
                "halt_reason": None, "completion_rate": None}
        data.update(kw)
        path = self.tmp / "cell.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_arm_a_incomplete_document_is_not_resolved(self) -> None:
        path = self._bench()
        out = li.write_back_scored_record(
            path, arm="A", completion_rate=11.5, blocks_found=6, blocks_expected=52
        )
        self.assertEqual(out["score"], 0.115)
        self.assertFalse(out["resolved"])
        self.assertEqual(out["termination"], "stopped_early")
        self.assertEqual(out["score_basis"], "completion_rate")
        self.assertEqual(out["process_exit_code"], 0)  # what the old fields really were
        self.assertEqual(json.loads(path.read_text())["completion_rate"], 11.5)

    def test_arm_a_complete_document_is_resolved(self) -> None:
        out = li.write_back_scored_record(
            self._bench(), arm="A", completion_rate=100.0, blocks_found=52, blocks_expected=52
        )
        self.assertEqual(out["score"], 1.0)
        self.assertTrue(out["resolved"])
        self.assertEqual(out["termination"], "complete")

    def test_arm_a_provider_halt_gives_provider_error_termination(self) -> None:
        out = li.write_back_scored_record(
            self._bench(), arm="A", completion_rate=2.0, blocks_found=1, blocks_expected=52,
            halt_reason="provider unavailable during run: 1x HTTP 500 Internal server error",
        )
        self.assertEqual(out["termination"], "provider_error")
        self.assertEqual(li.arm_a_termination(0.0, None, timed_out=True), "harness_timeout")

    def test_arm_c_keeps_the_harness_fields(self) -> None:
        path = self._bench(arm="C", score=0.0, resolved=False, termination="attempts_exhausted")
        out = li.write_back_scored_record(
            path, arm="C", completion_rate=100.0, blocks_found=52, blocks_expected=52
        )
        self.assertFalse(out["resolved"])  # the harness's own verdict is not rewritten
        self.assertTrue(out["resolved_by_score"])
        self.assertEqual(out["termination"], "attempts_exhausted")

    def test_missing_record_returns_none(self) -> None:
        self.assertIsNone(
            li.write_back_scored_record(self.tmp / "nope.json", arm="A", completion_rate=1.0,
                                        blocks_found=1, blocks_expected=2)
        )

    def test_bench_cli_arm_a_with_delimiter_does_not_claim_a_score(self) -> None:
        from kusudaemon.pipeline.cli import build_pipeline_parser, cmd_bench

        ws = self.tmp / "ws"
        ws.mkdir()
        out = self.tmp / "a.json"

        class Proc:
            returncode = 0
            stdout = "Let me output the full document\n"
            stderr = ""

        parser = build_pipeline_parser()
        args = parser.parse_args([
            "bench", "--workspace", str(ws), "--goal", "g", "--backend", "gptme", "--arm", "A",
            "--artifact-delimiter", "#*#", "--output", str(out), "--json",
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_bench(args, subprocess_runner=lambda *a, **k: Proc())
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(data["score"])
        self.assertIsNone(data["resolved"])
        self.assertEqual(data["termination"], "unscored")
        self.assertEqual(data["process_exit_code"], 0)

    def test_run_one_score_only_writes_back_arm_a(self) -> None:
        results = self.tmp / "results"
        (results / "bench").mkdir(parents=True)
        (results / "raw").mkdir()
        stem = "000-week_armA_seed1"
        (results / "raw" / f"{stem}.txt").write_text(unit(1) + unit(2), encoding="utf-8")
        bench = results / "bench" / f"{stem}.json"
        bench.write_text(json.dumps({"arm": "A", "score": 1.0, "resolved": True,
                                     "termination": "gates_satisfied"}), encoding="utf-8")
        args = argparse.Namespace(
            backend="opencode", model=None, tier="auto", work_object="text", budget_tokens=None,
            max_rounds=None, runs_root=str(self.tmp / "runs"), max_parallel=1, dry_run=False,
            score_only=True, workspaces_root=str(self.tmp / "ws"), flags=None, verbose=False,
        )
        item = dict(WEEK, prompt="p *** started ***\n#*# Week 1 (x):")
        row = rlb.run_one(index=0, item=item, arm="A", seed=1, args=args, results_dir=results)
        self.assertEqual(row["completion_rate"], 50.0)
        data = json.loads(bench.read_text(encoding="utf-8"))
        self.assertEqual(data["score"], 0.5)
        self.assertFalse(data["resolved"])
        self.assertEqual(data["termination"], "stopped_early")
        self.assertEqual(row["code_dirty"], "unknown")  # a rescore has no code of its own


class PredictionPathTest(unittest.TestCase):
    """4.3 item 3: dead VM paths resolve through raw/, and failure is loud."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "raw").mkdir()
        self.item = dict(WEEK)

    def _record(self, **kw) -> dict:
        r = rec(arm="A", **kw)
        r["dataset_index"] = 0
        return r

    def test_dead_vm_path_resolves_to_raw(self) -> None:
        (self.tmp / "raw" / "000-week_armA_seed1.txt").write_text(unit(1) + unit(2), encoding="utf-8")
        r = self._record(artifact_path="/sessions/rcw-xyz/mnt/kusudaemon/bench_results/longgen/raw/000-week_armA_seed1.txt")
        self.assertEqual(li.resolve_artifact_path(r, self.tmp), self.tmp / "raw" / "000-week_armA_seed1.txt")
        written = rlb.write_predictions([r], [(0, self.item)], self.tmp)
        entries = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertGreater(entries[0]["word_count"], 5)

    def test_path_without_a_recorded_one_uses_the_stem(self) -> None:
        (self.tmp / "raw" / "000-week_armA_seed1.md").write_text(unit(1), encoding="utf-8")
        self.assertEqual(
            li.resolve_artifact_path({"task_id": "000-week", "arm": "A", "seed": 1}, self.tmp),
            self.tmp / "raw" / "000-week_armA_seed1.md",
        )

    def test_unreadable_artifact_raises_instead_of_writing_an_empty_prediction(self) -> None:
        r = self._record(artifact_path="/sessions/gone/raw/000-week_armA_seed1.txt")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(rlb.PredictionArtifactError) as ctx:
                rlb.write_predictions([r], [(0, self.item)], self.tmp)
        self.assertIn("000-week_armA_seed1", str(ctx.exception))
        self.assertIn("[ERROR]", err.getvalue())

    def test_non_strict_logs_and_writes_empty(self) -> None:
        r = self._record(artifact_path="/sessions/gone/raw/000-week_armA_seed1.txt")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            written = rlb.write_predictions([r], [(0, self.item)], self.tmp, strict=False)
        self.assertIn("[ERROR]", err.getvalue())
        self.assertEqual(json.loads(written[0].read_text(encoding="utf-8"))[0]["word_count"], 0)

    def test_a_cell_that_left_nothing_is_not_an_error(self) -> None:
        r = self._record(artifact_path=None, artifact_chars=0)
        with contextlib.redirect_stderr(io.StringIO()):
            rlb.write_predictions([r], [(0, self.item)], self.tmp)

    def test_persist_artifact_warns_on_oserror(self) -> None:
        blocker = self.tmp / "file"
        blocker.write_text("x")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rlb._persist_artifact(blocker / "sub" / "a.txt", "text")
        self.assertIn("[WARNING]", err.getvalue())


class ReconcileTest(unittest.TestCase):
    """4.3 item 4: bench, records and raw must agree for the matrix."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("bench", "raw"):
            (self.tmp / d).mkdir()
        self.cells = li.expected_cells(["000-week"], ("A", "C"), (1,))

    def _cell(self, stem: str, bench=True, raw=True) -> None:
        if bench:
            (self.tmp / "bench" / f"{stem}.json").write_text("{}")
        if raw:
            (self.tmp / "raw" / f"{stem}.txt").write_text("x")

    def test_agreement_is_ok(self) -> None:
        self._cell("000-week_armA_seed1")
        (self.tmp / "bench" / "000-week_armC_seed1.json").write_text("{}")
        (self.tmp / "raw" / "000-week_armC_seed1.md").write_text("x")
        rows = [rec(arm="A", artifact_path=str(self.tmp / "raw" / "000-week_armA_seed1.txt")),
                rec(arm="C", artifact_path=str(self.tmp / "raw" / "000-week_armC_seed1.md"))]
        report = li.reconcile_cells(self.tmp, self.cells, rows)
        self.assertTrue(report["ok"], report)

    def test_each_kind_of_disagreement_is_named(self) -> None:
        self._cell("000-week_armA_seed1", bench=False)           # bench missing
        (self.tmp / "bench" / "000-week_armC_seed1.json").write_text("{}")  # raw + record missing
        report = li.reconcile_cells(self.tmp, self.cells, [rec(arm="A", artifact_path="/dead/x.txt")])
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_bench"], ["000-week_armA_seed1"])
        self.assertEqual(report["missing_records"], ["000-week_armC_seed1"])
        self.assertEqual(report["missing_raw"], ["000-week_armC_seed1"])
        self.assertEqual(report["unresolvable_paths"], [])  # resolves through raw/ by stem
        self.assertIn("MISMATCH", li.format_reconcile(report))

    def test_a_dead_path_with_no_raw_file_is_unresolvable(self) -> None:
        self._cell("000-week_armA_seed1")
        (self.tmp / "raw" / "000-week_armA_seed1.txt").unlink()
        report = li.reconcile_cells(
            self.tmp, [("000-week", "A", 1)], [rec(arm="A", artifact_path="/dead/000-week_armA_seed1.txt")]
        )
        self.assertEqual(report["unresolvable_paths"], ["000-week_armA_seed1"])

    def test_documented_absent_artifact_is_accepted(self) -> None:
        (self.tmp / "bench" / "000-week_armC_seed1.json").write_text("{}")
        report = li.reconcile_cells(
            self.tmp, [("000-week", "C", 1)],
            [rec(arm="C", artifact_path=None, artifact_chars=0, artifact_absent=True)],
        )
        self.assertTrue(report["ok"], report)

    def test_cli_flag_reports_and_exits_nonzero(self) -> None:
        # run_longgen_bench --reconcile on an empty results dir for a dataset of one task
        data = self.tmp / "ds.json"
        data.write_text(json.dumps([dict(WEEK, prompt="p")]), encoding="utf-8")
        argv = ["run_longgen_bench.py", "--dataset", str(data), "--tasks", "0", "--results-dir",
                str(self.tmp), "--reconcile", "--arms", "A", "--seeds", "1"]
        buf = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
            code = rlb.main()
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", buf.getvalue())


class SummaryTest(unittest.TestCase):
    """4.3 items 5 and 6: a summary over a complete matrix, with and without
    quarantine, first attempt versus final."""

    def setUp(self) -> None:
        self.cells = li.expected_cells(["000-week", "005-week"], ("A", "C"), (1,))

    def _full(self) -> list[dict]:
        return [
            rec("000-week", "A", 1, 100.0), rec("005-week", "A", 1, 4.0),
            rec("000-week", "C", 1, 100.0), rec("005-week", "C", 1, 100.0),
        ]

    def test_refuses_an_incomplete_matrix(self) -> None:
        rows = self._full()[:3]
        with self.assertRaises(li.IncompleteMatrixError) as ctx:
            li.summarize_matrix(rows, self.cells)
        self.assertEqual(ctx.exception.missing, ["005-week_armC_seed1"])

    def test_partial_must_be_asked_for_and_is_flagged(self) -> None:
        s = li.summarize_matrix(self._full()[:3], self.cells, allow_partial=True)
        self.assertTrue(s["matrix"]["partial"])
        self.assertEqual(s["matrix"]["missing_cells"], ["005-week_armC_seed1"])

    def test_complete_matrix_headline(self) -> None:
        s = li.summarize_matrix(self._full(), self.cells)
        self.assertFalse(s["matrix"]["partial"])
        self.assertEqual(s["headline_all"]["A"]["mean_completion_pct"], 52.0)
        self.assertEqual(s["headline_all"]["C"]["whole_task_rate_pct"], 100.0)

    def test_unknown_halt_with_incomplete_document_is_not_quarantined(self) -> None:
        rows = self._full()
        rows[3] = rec("005-week", "C", 1, 54.0, halt_reason="escalated in execute: no detail")
        s = li.summarize_matrix(rows, self.cells)
        self.assertEqual(s["headline_excluding_quarantined"]["C"]["excluded_runs"], 0)
        self.assertEqual(s["headline_excluding_quarantined"]["C"]["mean_completion_pct"], 77.0)
        self.assertEqual(s["quarantined"], [])

    def test_transport_halt_is_quarantined_but_reported_both_ways(self) -> None:
        rows = self._full()
        rows[1] = rec("005-week", "A", 1, 4.0, halt_reason="provider unavailable during run: 2x HTTP 500")
        s = li.summarize_matrix(rows, self.cells)
        self.assertEqual(s["headline_all"]["A"]["runs"], 2)          # nothing silently dropped
        self.assertEqual(s["headline_all"]["A"]["mean_completion_pct"], 52.0)
        ex = s["headline_excluding_quarantined"]["A"]
        self.assertEqual((ex["runs"], ex["excluded_runs"], ex["mean_completion_pct"]), (1, 1, 100.0))
        self.assertEqual([q["cell"] for q in s["quarantined"]], ["005-week_armA_seed1"])

    def test_quarantine_never_drops_a_complete_document(self) -> None:
        ok, reason = li.record_validity(rec(halt_reason="timeout after 5400s", completion_rate=100.0))
        self.assertTrue(ok)
        ok, reason = li.record_validity(rec(halt_reason="timeout after 5400s", rate=40.0))
        self.assertEqual((ok, reason), (False, "budget"))

    def test_attempts_first_versus_final(self) -> None:
        rows = [
            rec("000-week", "A", 1, 100.0), rec("005-week", "A", 1, 4.0),
            rec("000-week", "C", 1, 100.0),
            rec("005-week", "C", 1, 36.0, harness_wall_clock_s=50.0),
            rec("005-week", "C", 1, 36.0, harness_wall_clock_s=0.0),   # a rescore of attempt 1
            rec("005-week", "C", 1, 100.0, harness_wall_clock_s=60.0),  # a re-run
        ]
        li.assign_attempts(rows)
        self.assertEqual([r["attempt"] for r in rows], [1, 1, 1, 1, 1, 2])
        s = li.summarize_matrix(rows, self.cells)
        self.assertEqual(s["first_attempt"]["C"]["mean_completion_pct"], 68.0)
        self.assertEqual(s["final"]["C"]["mean_completion_pct"], 100.0)
        self.assertEqual(s["cells_rerun"]["cells"], ["005-week_armC_seed1"])
        self.assertEqual(len(rows), 6)  # every attempt is kept

    def test_existing_attempt_tags_are_respected(self) -> None:
        rows = [rec(attempt=2, harness_wall_clock_s=0.0), rec(harness_wall_clock_s=0.0)]
        li.assign_attempts(rows)
        self.assertEqual([r["attempt"] for r in rows], [2, 2])

    def test_next_attempt_numbering_in_the_runner(self) -> None:
        prior = [rec(attempt=1), rec(attempt=2)]
        self.assertEqual(rlb.next_attempt(prior, "000-week", "C", 1, real_run=True), 3)
        self.assertEqual(rlb.next_attempt(prior, "000-week", "C", 1, real_run=False), 2)
        self.assertEqual(rlb.next_attempt([], "000-week", "C", 1, real_run=True), 1)

    def test_classify_halt_completion_pct_is_wired(self) -> None:
        self.assertEqual(classify_halt("escalated in execute: no detail", 54.0), "agent")
        s = rlb.summarize([rec(arm="C", halt_reason="escalated in execute: no detail", rate=54.0)])
        self.assertEqual(s["arms"]["C"]["excluded"]["total"], 0)


class ProvenanceTest(unittest.TestCase):
    """4.3 item 7: a dirty flag and a diff hash, not just HEAD."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        run = lambda *a: subprocess.run(["git", *a], cwd=self.tmp, check=True, capture_output=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("x = 1\n")
        run("add", ".")
        run("commit", "-qm", "init")

    def test_clean_then_dirty_then_untracked(self) -> None:
        clean = li.code_provenance(self.tmp)
        self.assertFalse(clean["code_dirty"])
        self.assertIsNone(clean["code_diff_sha256"])
        self.assertEqual(clean["code_version"], clean["code_commit"][:7])

        (self.tmp / "src" / "a.py").write_text("x = 2\n")
        dirty = li.code_provenance(self.tmp)
        self.assertTrue(dirty["code_dirty"])
        self.assertTrue(dirty["code_version"].endswith("-dirty"))
        self.assertEqual(dirty["code_commit"], clean["code_commit"])
        self.assertEqual(len(dirty["code_diff_sha256"]), 64)

        (self.tmp / "src" / "a.py").write_text("x = 3\n")
        other = li.code_provenance(self.tmp)
        self.assertNotEqual(other["code_diff_sha256"], dirty["code_diff_sha256"])

        (self.tmp / "src" / "a.py").write_text("x = 1\n")
        (self.tmp / "src" / "new.py").write_text("y = 1\n")
        untracked = li.code_provenance(self.tmp)
        self.assertTrue(untracked["code_dirty"])  # an untracked source file is code too

    def test_outside_git_degrades_to_none(self) -> None:
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        p = li.code_provenance(plain)
        self.assertIsNone(p["code_dirty"])
        self.assertIsNone(p["code_diff_sha256"])

    def test_historical_rows_are_marked_unknown(self) -> None:
        self.assertEqual(li.UNKNOWN_PROVENANCE["code_dirty"], "unknown")


def _make_store(tmp: Path) -> Path:
    db = tmp / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("create table message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
    con.execute("create table part (id text primary key, message_id text, session_id text, time_created integer, data text)")
    rows = [
        ("m1", "ses_a", 1000, 2000, {"role": "user"}),
        ("m2", "ses_a", 1000, 2000, {"role": "assistant", "tokens": {"total": 150, "input": 100, "output": 30, "reasoning": 5, "cache": {"read": 15, "write": 0}}}),
        ("m3", "ses_a", 3000, 4000, {"role": "assistant", "tokens": {"total": 50, "input": 10, "output": 10, "reasoning": 0, "cache": {"read": 30, "write": 0}}}),
        ("m4", "ses_b", 5000, 6000, {"role": "assistant", "tokens": {"total": 7, "input": 5, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}}}),
        ("m5", "ses_c", 5000, 6000, {"role": "assistant", "tokens": {"total": 999, "input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}}),
    ]
    for mid, sid, t0, t1, data in rows:
        con.execute("insert into message values (?,?,?,?,?)", (mid, sid, t0, t1, json.dumps(data)))
    con.commit()
    con.close()
    return db


class OpenCodeStoreTest(unittest.TestCase):
    """4.3 items 8 and 9: tokens and provider errors from OpenCode's own store."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = _make_store(self.tmp)

    def test_tokens_sum_over_the_cells_sessions_only(self) -> None:
        tk = li.opencode_session_tokens(["ses_a", "ses_b"], self.db)
        self.assertEqual(tk["total"], 207)
        self.assertEqual(tk["input"], 115)
        self.assertEqual(tk["output"], 42)
        self.assertEqual(tk["reasoning"], 5)
        self.assertEqual(tk["cache_read"], 45)
        self.assertEqual(tk["messages"], 3)   # the user message and ses_c are not counted
        self.assertEqual(tk["sessions"], 2)

    def test_no_sessions_is_zero_and_missing_db_raises(self) -> None:
        self.assertEqual(li.opencode_session_tokens([], self.db)["total"], 0)
        with self.assertRaises(FileNotFoundError):
            li.opencode_session_tokens(["ses_a"], self.tmp / "absent.db")

    def test_the_database_is_never_written(self) -> None:
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        li.opencode_session_tokens(["ses_a"], self.db)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("insert into message values ('x','s',1,1,'{}')")
        ro.close()

    def _log(self, lines: list[str]) -> Path:
        path = self.tmp / "opencode.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_provider_errors_are_counted_and_classified(self) -> None:
        log = self._log([
            'timestamp=2026-09-13T20:18:02.606Z level=ERROR run=a message="stream error" session.id=ses_a error.error="AI_APICallError: Too Many Requests"',
            'timestamp=2026-09-13T20:19:02.606Z level=ERROR run=a message="stream error" session.id=ses_a error.error.message="Internal server error" error.error.code=500',
            'timestamp=2026-09-13T20:20:02.606Z level=ERROR run=a message="stream error" session.id=ses_a error.error="ProviderHeaderTimeoutError: Provider response headers timed out after 300000ms"',
            'timestamp=2026-09-13T20:21:02.606Z level=ERROR run=a message="stream error" session.id=ses_other error.error="AI_APICallError: Too Many Requests"',
            'timestamp=2026-09-13T20:22:02.606Z level=INFO run=a message="stream error" session.id=ses_a',
        ])
        e = li.opencode_provider_errors(["ses_a"], log)
        self.assertEqual((e["http_429"], e["http_5xx"], e["header_timeout"], e["total"]), (1, 1, 1, 3))
        self.assertEqual(e["last_error_ts"], "2026-09-13T20:20:02.606Z")
        self.assertEqual(li.opencode_provider_errors(["ses_none"], log)["total"], 0)

    def test_halt_reason_makes_quarantine_apply(self) -> None:
        for errors in ({"http_429": 3, "total": 3}, {"http_5xx": 1, "total": 1}, {"header_timeout": 2, "total": 2}):
            reason = li.provider_error_halt_reason(errors)
            self.assertEqual(classify_halt(reason, 5.0), "transport", reason)
        self.assertIsNone(li.provider_error_halt_reason({"total": 0}))
        ok, why = li.record_validity(rec(arm="A", rate=5.0, halt_reason=li.provider_error_halt_reason({"http_5xx": 1, "total": 1})))
        self.assertEqual((ok, why), (False, "transport"))

    def test_run_one_would_not_crash_without_a_store(self) -> None:
        # The runner's arm-A branch wraps both lookups; a missing store warns.
        with self.assertRaises(FileNotFoundError):
            li.opencode_provider_errors(["ses_a"], self.tmp / "no.log")


class CaffeinateTest(unittest.TestCase):
    """4.1 item 7: wrap runs in `caffeinate -ims`, degrade where it is absent."""

    CMD = ["python3", "-m", "kusudaemon.cli", "bench"]

    def test_wraps_when_caffeinate_exists(self) -> None:
        with patch.object(rlb.shutil, "which", return_value="/usr/bin/caffeinate"), \
                patch.dict(rlb.os.environ, {}, clear=False):
            rlb.os.environ.pop("KUSUDAEMON_NO_CAFFEINATE", None)
            self.assertEqual(rlb.with_caffeinate(self.CMD), ["/usr/bin/caffeinate", "-ims", *self.CMD])

    def test_unwrapped_when_absent_or_disabled(self) -> None:
        with patch.object(rlb.shutil, "which", return_value=None):
            self.assertEqual(rlb.with_caffeinate(self.CMD), self.CMD)
        with patch.object(rlb.shutil, "which", return_value="/usr/bin/caffeinate"):
            self.assertEqual(rlb.with_caffeinate(self.CMD, enabled=False), self.CMD)
            with patch.dict(rlb.os.environ, {"KUSUDAEMON_NO_CAFFEINATE": "1"}):
                self.assertEqual(rlb.with_caffeinate(self.CMD), self.CMD)

    def test_run_one_launches_the_wrapped_command(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        args = argparse.Namespace(
            backend="opencode", model=None, tier="auto", work_object="text", budget_tokens=None,
            max_rounds=None, runs_root=str(tmp / "runs"), max_parallel=1, dry_run=False,
            score_only=False, workspaces_root=str(tmp / "ws"), flags=None, verbose=False,
            timeout_sec=60, continue_runs=True, no_caffeinate=False,
        )
        launched = []

        class FakeProc:
            returncode = 0
            pid = 1

            def communicate(self, timeout=None):
                return "", ""

        def fake_popen(cmd, **kw):
            launched.append(cmd)
            return FakeProc()

        with patch.object(rlb.shutil, "which", return_value="/usr/bin/caffeinate"), \
                patch.object(rlb.subprocess, "Popen", side_effect=fake_popen), \
                contextlib.redirect_stdout(io.StringIO()):
            rlb.os.environ.pop("KUSUDAEMON_NO_CAFFEINATE", None)
            rlb.run_one(index=0, item=dict(WEEK), arm="C", seed=1, args=args, results_dir=tmp / "results")
        self.assertEqual(launched[0][:2], ["/usr/bin/caffeinate", "-ims"])
        self.assertIn("bench", launched[0])


class RecomputeTest(unittest.TestCase):
    """The recovery logic in scripts/longgen_recompute.py."""

    def setUp(self) -> None:
        import longgen_recompute as rc

        self.rc = rc
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_lost_document_only_counts_files_inside_the_session_window(self) -> None:
        import os

        db = self.tmp / "opencode.db"
        con = sqlite3.connect(db)
        con.execute("create table message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        con.execute("create table part (id text primary key, message_id text, session_id text, time_created integer, data text)")
        inside, outside = self.tmp / "inside.txt", self.tmp / "outside.txt"
        document = "".join(unit(n) for n in (1, 2, 3, 4))
        inside.write_text(document, encoding="utf-8")
        outside.write_text(document, encoding="utf-8")
        t_ms = int(inside.stat().st_mtime * 1000)
        os.utime(outside, (1_500_000_000, 1_500_000_000))  # a later cell's /tmp file, years away
        con.execute("insert into message values ('m1','ses_x',?,?,?)", (t_ms - 5000, t_ms + 5000, json.dumps({"role": "assistant"})))
        for i, p in enumerate((inside, outside)):
            part = {"type": "tool", "tool": "bash", "state": {"input": {"command": f"cat > {p} <<EOF"}, "output": "Total blocks: 100\n"}}
            con.execute("insert into part values (?, 'm1', 'ses_x', ?, ?)", (f"p{i}", t_ms, json.dumps(part)))
        con.commit()
        con.close()
        found = self.rc.find_lost_document(["ses_x"], WEEK, 0.0, db)
        self.assertEqual(found["recovered_path"], str(inside))
        self.assertEqual(found["recovered_completion_rate"], 100.0)
        self.assertIn("Total blocks", found["lost_document_evidence"])
        # nothing better than what was exported -> nothing recovered
        self.assertNotIn("recovered_path", self.rc.find_lost_document(["ses_x"], WEEK, 100.0, db))

    def test_attempt_order_uses_commit_then_end_time(self) -> None:
        order = {"aaa1111": 1, "bbb2222": 2}
        rows = [
            {"attempt": 1, "wall_clock_s": 100.0, "commit": "bbb2222", "completion_rate": 100.0},
            {"attempt": 1, "wall_clock_s": 50.0, "commit": None, "completion_rate": 10.0, "harness_wall_clock_s": 1.0},
        ]
        rows[1]["attempt"] = 2
        # attempt 1 is the later commit, attempt 2 the commit-less (oldest) run
        atts = self.rc.build_attempts(("000-week", "C", 1), rows, self.tmp / "archive", dict(WEEK), order)
        self.assertEqual([a["recorded_cr"] for a in atts], [100.0, 10.0][::-1])
        self.assertEqual(self.rc.commit_rank(None, order), -1.0)

    def test_stats_helpers(self) -> None:
        self.assertAlmostEqual(self.rc._binom_two_sided(1, 17), 36 / 131072)
        self.assertEqual(self.rc._binom_two_sided(0, 0), 1.0)
        self.assertEqual(self.rc.era_of("2b6a2ab05b9a"), 6)
        self.assertEqual(self.rc.era_of(None), 4)


if __name__ == "__main__":
    unittest.main()
