"""Hermetic tests for PLAN-SWEEP-REPAIR.md Wave 0 (no provider calls, no network).

Covers:
  §B6 — parts-on-disk fixture: gates, shrink/snapshot, harvest all see the
        concatenation (fails at four call sites on pre-fix code).
  §B4 — option (a): directory snapshots restore the parts layout instead of
        writing a shadowed single file.
  §C1 — write_predictions against a records list contaminated with an
        unselected task's rows writes predictions without KeyError.
  §C2 — "escalated in execute: no detail" classifies as unknown and is
        quarantined from mean_completion_pct under excluded.by_reason.unknown.
  §F2 — provider transport failures name phase, role and elapsed seconds.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


def _make_parts_run_dir(root: Path, node_id: str = "n1") -> Path:
    run_dir = root / "run1"
    out_node = run_dir / "out" / node_id
    out_node.mkdir(parents=True, exist_ok=True)
    (out_node / "part-01.md").write_text("# Floor 1\ncontent one\n", encoding="utf-8")
    (out_node / "part-02.md").write_text("# Floor 2\ncontent two\n", encoding="utf-8")
    # No out/<node>.md single file — the pre-fix readers see "".
    assert not (run_dir / "out" / f"{node_id}.md").exists()
    return run_dir


class ArtifactReadersTest(unittest.TestCase):
    def test_round_loop_read_artifact_sees_parts(self) -> None:
        from kusudaemon.v1.round_loop import _read_artifact

        with tempfile.TemporaryDirectory() as td:
            run_dir = _make_parts_run_dir(Path(td))
            text = _read_artifact(run_dir, "n1")
            self.assertIn("Floor 1", text)
            self.assertIn("Floor 2", text)

    def test_gate_evaluation_sees_parts(self) -> None:
        from kusudaemon.v0.run_dir import node_artifact_text
        from kusudaemon.v1.gates import all_passed, evaluate_gates
        from kusudaemon.v1.round_loop import _read_artifact

        with tempfile.TemporaryDirectory() as td:
            run_dir = _make_parts_run_dir(Path(td))
            text = _read_artifact(run_dir, "n1")
            self.assertEqual(text, node_artifact_text(run_dir, "n1"))
            results = evaluate_gates(["nonempty"], text)
            self.assertTrue(all_passed(results))

    def test_snapshot_captures_parts_dir(self) -> None:
        from kusudaemon.v0.runner import _snapshot_pre_writer

        with tempfile.TemporaryDirectory() as td:
            run_dir = _make_parts_run_dir(Path(td))
            snap = _snapshot_pre_writer(run_dir, "n1")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertTrue(snap.is_dir())
            self.assertTrue((snap / "part-01.md").is_file())
            self.assertTrue((snap / "part-02.md").is_file())

    def test_snapshot_single_file_layout_still_file(self) -> None:
        from kusudaemon.v0.runner import _snapshot_pre_writer

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run1"
            out = run_dir / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "n1.md").write_text("hello single\n", encoding="utf-8")
            snap = _snapshot_pre_writer(run_dir, "n1")
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertTrue(snap.is_file())

    def test_restore_directory_snapshot_replaces_parts(self) -> None:
        from kusudaemon.v0.runner import _snapshot_pre_writer
        from kusudaemon.v0.run_dir import node_artifact_text
        from kusudaemon.v3.run_dir import list_attempt_snapshots, restore_attempt_snapshot

        with tempfile.TemporaryDirectory() as td:
            run_dir = _make_parts_run_dir(Path(td))
            snap = _snapshot_pre_writer(run_dir, "n1")
            assert snap is not None
            # Simulate a clobber: parts dir now holds only the last chunk.
            shutil.rmtree(run_dir / "out" / "n1")
            (run_dir / "out" / "n1").mkdir(parents=True)
            (run_dir / "out" / "n1" / "part-02.md").write_text(
                "# Floor 2\ncontent two\n", encoding="utf-8"
            )
            shrunk = node_artifact_text(run_dir, "n1")
            self.assertNotIn("Floor 1", shrunk)
            # §B4(a): restore at parts granularity — Floor 1 comes back and
            # the resolved read reflects it (no shadowed single-file write).
            snaps = list_attempt_snapshots(run_dir, "n1")
            self.assertEqual(len(snaps), 1)
            restore_attempt_snapshot(run_dir, "n1", snaps[-1])
            restored = node_artifact_text(run_dir, "n1")
            self.assertIn("Floor 1", restored)
            self.assertIn("Floor 2", restored)

    def test_harvest_artifact_sees_parts(self) -> None:
        import run_longgen_bench as rlb

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = _make_parts_run_dir(root / "runs", "n1")
            # Minimal tree.json so harvest can enumerate node ids.
            (run_dir / "tree.json").write_text(
                json.dumps([{"id": "n1", "brief": "b", "artifact": "out/n1.md"}]),
                encoding="utf-8",
            )
            run_dir.rename(root / "runs" / "longgen_100-floor_armC_seed1")
            rdir = root / "runs" / "longgen_100-floor_armC_seed1"
            artifact_path = root / "raw" / "x.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            text = rlb.harvest_artifact(
                "C",
                artifact_path,
                {},
                runs_root=root / "runs",
                run_id="longgen_100-floor_armC_seed1",
            )
            self.assertIn("Floor 1", text)
            self.assertIn("Floor 2", text)

    def test_harvest_prefers_assembly_main(self) -> None:
        import run_longgen_bench as rlb

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rdir = root / "runs" / "longgen_100-floor_armC_seed1"
            out_node = rdir / "out" / "n1"
            out_node.mkdir(parents=True, exist_ok=True)
            (out_node / "part-01.md").write_text("parts text\n", encoding="utf-8")
            asm = rdir / "assembly"
            asm.mkdir(parents=True, exist_ok=True)
            (asm / "main.md").write_text("assembled text\n", encoding="utf-8")
            artifact_path = root / "raw" / "x.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            text = rlb.harvest_artifact(
                "C",
                artifact_path,
                {},
                runs_root=root / "runs",
                run_id="longgen_100-floor_armC_seed1",
            )
            self.assertIn("assembled text", text)


class RecordRepairTest(unittest.TestCase):
    def test_write_predictions_filters_unselected_task(self) -> None:
        import run_longgen_bench as rlb

        with tempfile.TemporaryDirectory() as td:
            results_dir = Path(td)
            item100 = {
                "prompt": "floors prompt",
                "type": "Floor",
                "number": 2,
                "prefix": "",
                "checks_once": {},
                "checks_range": {},
                "checks_periodic": {},
            }
            tasks = [(100, item100)]
            art = results_dir / "art100.md"
            art.write_text("#*# Floor 1\n#*# Floor 2\n", encoding="utf-8")
            records = [
                {
                    "task_id": "100-floor",
                    "dataset_index": 100,
                    "arm": "C",
                    "seed": 1,
                    "artifact_path": str(art),
                    "dry_run": False,
                },
                {
                    # Stale row from another sweep — must not KeyError.
                    "task_id": "300-block",
                    "dataset_index": 300,
                    "arm": "C",
                    "seed": 1,
                    "artifact_path": str(art),
                    "dry_run": False,
                },
            ]
            # §C1 acceptance: no KeyError, summary names one task.
            written = rlb.write_predictions(records, tasks, results_dir)
            self.assertEqual(len(written), 1)
            entries = json.loads(written[0].read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["task_id"], "100-floor")

    def test_unknown_halt_quarantined(self) -> None:
        from kusudaemon.eval.common import classify_halt

        import run_longgen_bench as rlb

        self.assertEqual(classify_halt("escalated in execute: no detail"), "unknown")
        self.assertEqual(classify_halt("error in execute: no detail"), "unknown")
        # Transport/budget/agent behaviour unchanged.
        self.assertEqual(classify_halt("socket read timeout"), "transport")
        self.assertEqual(classify_halt("wall_clock_budget exhausted"), "budget")
        self.assertEqual(classify_halt("gate failed badly"), "agent")

        records = [
            {
                "task_id": "100-floor",
                "arm": "C",
                "seed": 1,
                "completion_rate": 0.05,
                "valid": False,
                "invalid_reason": "unknown",
                "halt_reason": "escalated in execute: no detail",
                "tokens_by_role": {"writer": 10},
                "harness_wall_clock_s": 5.0,
                "timed_out": False,
                "artifact_chars": 100,
            },
            {
                "task_id": "100-floor",
                "arm": "C",
                "seed": 2,
                "completion_rate": 0.5,
                "valid": True,
                "halt_reason": None,
                "tokens_by_role": {"writer": 10},
                "harness_wall_clock_s": 5.0,
                "timed_out": False,
                "artifact_chars": 200,
            },
        ]
        summary = rlb.summarize(records)
        arm = summary["arms"]["C"]
        self.assertEqual(arm["valid_runs"], 1)
        self.assertEqual(arm["mean_completion_pct"], 0.5)
        self.assertEqual(arm["excluded"]["by_reason"].get("unknown"), 1)


class ProviderErrorContextTest(unittest.TestCase):
    def _provider(self, **kw):
        from kusudaemon.v1.provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            api_key="unused",
            base_url="http://127.0.0.1:9",
            model="m",
            role="reviewer",
            phase="execute",
            node_id="n1",
            **kw,
        )

    def test_http_transport_timeout_names_phase_role_elapsed(self) -> None:
        import urllib.error

        from kusudaemon.v1.provider import ProviderError

        p = self._provider()
        err = urllib.error.URLError(TimeoutError("The read operation timed out"))
        with patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ProviderError) as ctx:
                p._http_transport("http://x", {"model": "m"}, {})
        msg = str(ctx.exception)
        self.assertIn("execute", msg)
        self.assertIn("reviewer", msg)
        self.assertIn("elapsed=", msg)

    def test_http_transport_raw_timeout_names_phase_role_elapsed(self) -> None:
        from kusudaemon.v1.provider import ProviderError

        p = self._provider()
        with patch("urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaises(ProviderError) as ctx:
                p._http_transport("http://x", {"model": "m"}, {})
        msg = str(ctx.exception)
        self.assertIn("execute", msg)
        self.assertIn("reviewer", msg)
        self.assertIn("elapsed=", msg)

    def test_http_error_keeps_status_with_context(self) -> None:
        import io
        import urllib.error

        from kusudaemon.v1.provider import ProviderHTTPError

        p = self._provider()
        http_err = urllib.error.HTTPError(
            "http://x", 500, "boom", {"Retry-After": "1"}, io.BytesIO(b"boom")
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaises(ProviderHTTPError) as ctx:
                p._http_transport("http://x", {"model": "m"}, {})
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("execute", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
