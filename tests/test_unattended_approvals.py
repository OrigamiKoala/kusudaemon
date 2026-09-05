"""The blank-answering approval resolver must stay confined to unattended runs.

Three phases stop and ask a human: intake questions (T1+), pilot artifact
sign-off (T3), and document-review triage (T2+). ``approvals.wait_for_resolution``
waits on them forever on purpose -- "the operator is the one control surface
that must never be rushed".

That is right for an interactive run and fatal for a scripted one, so
``approvals.Approver`` answers every pending approval blank in the entry points
that have no operator by construction. The whole value of the approval
mechanism disappears if that resolver ever leaks into a normal ``kusudaemon
run``: the operator would silently never be asked, and the run would proceed on
default assumptions the user never saw.

These tests pin that boundary. They are source-level on purpose -- the
behavioural half (that ``bench`` resolves and ``bench --attended`` does not) is
covered in ``test_bench_cli.py``; what cannot be covered behaviourally without
a live provider is the *absence* of auto-resolution from every other path, and
a refactor that hoisted the ``Approver`` up a level would not fail any
behavioural test.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from kusudaemon.pipeline import cli as cli_mod
from kusudaemon.pipeline import run as run_mod

# The only entry points allowed to answer approvals on the operator's behalf.
# Both are unattended by construction: `bench` is driven by a benchmark
# harness, `eval/runner.py` by the in-repo eval suite. Adding to this list
# means deciding that some other path has no human to ask -- do not do it to
# make a hang go away.
_ALLOWED_AUTO_RESOLVERS = {"cmd_bench"}


def _functions_using_approver(module) -> set[str]:
    names: set[str] = set()
    for name, obj in vars(module).items():
        if not (inspect.isfunction(obj) and getattr(obj, "__module__", "") == module.__name__):
            continue
        try:
            source = inspect.getsource(obj)
        except (OSError, TypeError):  # pragma: no cover - source always available here
            continue
        if "Approver(" in source:
            names.add(name)
    return names


class UnattendedApprovalBoundaryTest(unittest.TestCase):
    def test_only_the_bench_entry_point_auto_resolves_approvals(self) -> None:
        self.assertEqual(
            _functions_using_approver(cli_mod),
            _ALLOWED_AUTO_RESOLVERS,
            "A CLI command outside the allow-list constructs an Approver. An "
            "interactive run must leave approvals for the operator to answer.",
        )

    def test_bench_actually_is_the_one_that_has_it(self) -> None:
        """Guards the test above against passing because nothing uses it."""
        self.assertIn("Approver(", inspect.getsource(cli_mod.cmd_bench))

    def test_the_interactive_run_module_never_auto_resolves(self) -> None:
        """`kusudaemon run` and `kusudaemon resume` both land in pipeline/run.py
        (resume is documented as 'run with an existing run-id'), so a human
        driving either must still be asked."""
        self.assertNotIn(
            "Approver",
            inspect.getsource(run_mod),
            "pipeline/run.py drives interactive runs; it must never answer "
            "approvals on the operator's behalf.",
        )

    def test_auto_resolution_is_opt_out_only_on_bench(self) -> None:
        """--attended exists on `bench` and nowhere else: it is the escape hatch
        for a benchmark someone wants to supervise, not a switch that other
        commands need."""
        parser = cli_mod.build_pipeline_parser()

        bench = parser.parse_args([
            "bench", "--workspace", str(_REPO_ROOT), "--goal", "g", "--attended",
        ])
        self.assertTrue(bench.attended)

        default = parser.parse_args([
            "bench", "--workspace", str(_REPO_ROOT), "--goal", "g",
        ])
        self.assertFalse(
            default.attended,
            "A benchmark run has no operator; unattended must be the default.",
        )

        with self.assertRaises(SystemExit):
            parser.parse_args(["run", "--goal", "g", "--attended"])


if __name__ == "__main__":
    unittest.main()
