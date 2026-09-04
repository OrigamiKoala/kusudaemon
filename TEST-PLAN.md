# TEST-PLAN.md — Phase 0 repairs and Layer 1 mechanism benchmarks

Status as of 2026-09-04. Measured on Linux/CPython 3.10; local dev is 3.13,
which matters for two of the findings below.

```
python3 -m unittest discover -s tests -p "test_*.py"
→ Ran 1069 tests in 84.6s — FAILED (failures=1, errors=5)
```

| # | Finding | Severity | Phase |
|---|---------|----------|-------|
| A | `tomli` missing takes down the whole driver import (88 cascading errors on 3.10) | High — packaging lie | 0.1 |
| B | 5 test files are pytest-only and never run under the documented command | High — silent coverage hole | 0.2 |
| C | ~1000 lines of unconditional stdout hid finding B in plain sight | Medium | 0.3 |
| D | Dashboard trace cache stitches stale entries when the filesystem reuses inodes | High — real bug, not a test artifact | 0.4 |
| E | Nothing prevents A/B from recurring | Medium | 0.5 |
| F | Eval reports escalation precision 1.0 with zero escalations ever fired | High — vacuous metric | 1.7 |

---

## Phase 0 — Repairs

### 0.1 The `tomli` import chain

**Root cause.** `adapters/codex.py:47` calls `importlib.import_module("tomli")`
at module import time. `pipeline/backends.py:34` imports `CodexAdapter`
unconditionally, `pipeline/driver.py:119` imports `backends`, and
`pipeline/__init__.py:21` imports `driver`. So one optional dependency,
belonging to a backend nobody selected, is load-bearing for importing the
harness at all. On 3.11+ `tomllib` is stdlib and the fault is invisible;
`pyproject.toml` declares `tomli; python_version < '3.11'` but nothing in the
test path installs the package, so the documented `requires-python = ">=3.10"`
is not true of the test suite.

**Fix — two parts, both needed.**

1. *Tactical.* Document and use `pip install -e ".[gptme]"` before testing, and
   add a `[project.optional-dependencies] dev` group carrying `tomli` so the
   test path can be installed without pulling gptme.
2. *Structural (the one that matters).* Make the TOML import lazy — move
   `importlib.import_module("tomli")` inside the function that actually parses
   Codex config. An unselected backend must not be able to break the driver
   import. The same eager-import fragility applies to every adapter
   `backends.py` pulls in at module scope; audit all of them in the same pass.

**Regression test.** `tests/test_import_isolation.py`:

```python
class OptionalDepIsolationTest(unittest.TestCase):
    def test_driver_imports_without_tomli(self) -> None:
        with mock.patch.dict(sys.modules, {"tomli": None}):
            for mod in [m for m in sys.modules if m.startswith("kusudaemon")]:
                del sys.modules[mod]
            from kusudaemon.pipeline.driver import RecursiveDriver  # noqa: F401
```

Parameterize it over every optional dependency, not just `tomli`.

**CI.** Add a matrix at 3.10 / 3.11 / 3.13. The bug exists precisely because
only 3.13 was ever exercised.

---

### 0.2 Port the five pytest files to unittest

`test_backend_role_provider.py`, `test_keyless_run.py`,
`test_role_adapter_matrix.py`, `test_role_provider_protocol.py`,
`test_token_efficiency_audit.py` — 685 lines, 22 test functions total.

CLAUDE.md promises "stdlib `unittest`, no pytest, no network, no agent binary,
no API key required." These five break that promise, and because
`unittest discover` fails them at *load* time they surface as five
indistinguishable `unittest.loader._FailedTest` errors rather than as missing
coverage.

**Mechanical translation table:**

| pytest | unittest |
|---|---|
| `def test_x(tmp_path: Path)` | method; `self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))` |
| `monkeypatch.setenv(k, v)` | `self.enterContext(mock.patch.dict(os.environ, {k: v}))` |
| `monkeypatch.delenv(k)` | same, with `clear=` or an explicit pop + `addCleanup` |
| `monkeypatch.setattr(o, "a", v)` | `self.enterContext(mock.patch.object(o, "a", v))` |
| `pytest.raises(E, match=r"...")` | `self.assertRaisesRegex(E, r"...")` |
| `pytest.raises(E) as info` + `info.value` | `with self.assertRaises(E) as ctx:` + `ctx.exception` |
| module-level fixtures | `setUp` / `setUpClass` |

`enterContext` is 3.11+; on 3.10 use `self.addCleanup(patcher.stop)` after
`patcher.start()`. Since 0.1 adds a 3.10 CI leg, write it the 3.10 way.

**Do this at the same time:** `FakeProvider` is currently redefined in at least
five test files. Extract it, `_InMemoryWriterAdapter`, and the temp-run-dir
setup into `tests/_support.py`. `unittest discover` puts `tests/` on `sys.path`,
so a bare `import _support` works and the no-`__init__.py` convention (and the
load-bearing `sys.path.insert` header) is preserved.

**Then:** delete the pytest imports, drop `.pytest_cache/` from the working
tree, and leave CLAUDE.md's claim intact — it becomes true again rather than
being edited to match the drift.

---

### 0.3 Silence the driver's stdout in tests

`pipeline/run.py:203` and `:273` print unconditionally:

```
run dir: /…/runs/r1
pipeline: status=done phase=assemble tree={}
```

Across 1069 tests this is roughly a thousand lines of scrollback, which is
exactly why five loader errors went unnoticed for however long they have been
there. Replace both with a module logger; have `pipeline/cli.py` attach a
`StreamHandler` at INFO so interactive CLI output is unchanged, and gate test
verbosity on `KUSUDAEMON_TEST_VERBOSE`.

Diagnostic noise that drowns the signal is a test-infrastructure bug with the
same practical effect as a missing assertion. Treat it as one.

---

### 0.4 The dashboard trace-cache bug — real, not environmental

`test_dashboard_server.ThinkingCursorTest.test_rewritten_trace_never_stitches_onto_old_parse`
fails with `AssertionError: 7 != 4`: the old five-entry parse plus a stitched
tail, which is precisely the corruption the test was written to prevent.

**It reproduces because the filesystem reused the inode.** Confirmed directly:

```
write 100 bytes → inode 864
unlink, write 200 bytes → inode 864   (reused: True)
```

`dashboard/state.py:1700` guards with:

```python
if cached is None or cached.inode != inode or size < cached.offset:
```

Under inode reuse with a larger replacement file, `cached.inode != inode` is
False and `size < cached.offset` is False, so the stale cache is trusted and
the new file's bytes are appended from the old offset onto the previous
episode's entries — the garbage tail in the chat window that the
§2026-08-13 comment describes, still happening.

This is not a sandbox quirk to wave away. Inode reuse is legal POSIX behavior
and is routine on ext4 and tmpfs. macOS APFS does not reuse promptly, which is
the only reason this passes on your machine. **Anyone running kusudaemon on
Linux can see a corrupted thinking stream today**, and the dashboard is the
mechanism you rely on to catch problems mid-run — a viewer that silently shows
the wrong reasoning is worse than no viewer.

**Fix.** Stop using inode as identity. Keep `st_ino` and the size check as cheap
fast paths, then add a content check as the correctness backstop:

- store `head_digest = sha256(first min(offset, 4096) bytes)` at parse time;
- before trusting the cache, re-read those bytes and compare;
- on mismatch, discard and reparse from zero.

Cost is one ≤4 KB `pread` per node per poll tick, which is nothing next to the
JSON parsing already happening. Optionally add `st_ctime_ns` to the fast path,
but do not rely on it — nanosecond ctime is not portable and the digest makes
it redundant.

**Make the regression test platform-independent.** The existing test only fails
where the filesystem happens to reuse inodes. Force the case:

```python
real_stat = Path.stat
def _fixed_ino(self, *a, **kw):
    st = real_stat(self, *a, **kw)
    return os.stat_result(tuple(st)[:1] + (999,) + tuple(st)[2:])
```

Patch that in and the reused-inode path is exercised on every platform,
including yours.

---

### 0.5 A reachability guard so 0.1 and 0.2 cannot recur

`tests/test_suite_reachable.py`:

- walk `tests/*.py`, `importlib.import_module` each one, assert no exception;
- assert every file defines at least one `unittest.TestCase` subclass;
- assert the total collected count matches a checked-in floor, so a file that
  stops contributing tests is loud rather than quiet.

The actual failure was never "a test broke" — it was "five files stopped
existing as far as the runner was concerned, and nothing said so." Only a
meta-check fixes that class of problem.

**Phase 0 acceptance:** `unittest discover` reports `OK` with a test count
≥ 1069 + the 22 ported tests, on 3.10 and 3.13, with stdout under 50 lines.

---

## Phase 1 — Layer 1 mechanism benchmarks

Hermetic, free, deterministic. Every one of these turns a design invariant
that currently lives in a docstring into a number that fails a build.

### 1.1 Crash matrix — `tests/test_layer1_crash_matrix.py`

Today the eval runs the driver twice cleanly and calls that resume. Real resume
means dying at arbitrary points. `_PHASES_BY_TIER` gives 3 + 5 + 7 + 9 = **24
(tier, phase) crash points**.

Method: run the driver in a subprocess with a test-only hook honoring
`KUSUDAEMON_CRASH_AT=<phase>` that calls `os._exit(137)` immediately after that
phase's event is appended; resume in-process; assert:

- every value of `measure.terminal_events_per_node(run_dir)` is ≤ 1;
- `out/*.md` are byte-identical (sha256) to an uninterrupted run of the task;
- `EventLog.read_all()` parses fully, with at most one torn trailing line;
- resume dispatches zero writer episodes for already-passed nodes.

Add a torn-write case: truncate `events.jsonl` to a random offset inside its
last line and assert recovery. `v0/events.py:64` documents a torn-line rule;
nothing currently tests it.

Use `subTest(tier=…, phase=…)` so one failure names its crash point.

### 1.2 Context boundedness — `tests/test_layer1_context_bounded.py`

CLAUDE.md: *"every context (including the orchestrator's) is bounded and does
not grow with corpus size or run length."* This is the claim that makes the
whole design worth building, and nothing checks it.

Generalize `t2-large-corpus` (currently fixed at 60 units) into a factory and
instantiate at **60 / 600 / 6000** units. For each, record per role
`max(measure.call_input_tokens(c) for c in provider.calls if role matches)`.

Assert a **ratio**, not an absolute:

```python
self.assertLessEqual(max_tokens["orchestrator"][6000],
                     max_tokens["orchestrator"][60] * 1.15)
```

Absolute token counts churn with every prompt edit; the slope is the invariant.
Second axis for run length: hold the corpus fixed, force 50 rounds of
redispatch, assert the orchestrator's max prompt is flat across rounds.

Highest value per hour in Phase 1. Build this one first.

### 1.3 Gate false-accept corpus — `tests/test_layer1_gate_soundness.py`

`evaluate_gates(gates: list[str], artifact_text: str)` is pure, so this needs no
driver, no provider, no temp dirs.

For each of the ten gate kinds (`exists`, `nonempty`, `len`, `max_tokens`,
`contains`, `headers_std`, `problems_min`, `terms_defined`, `latex_balanced`,
`refs_resolve`), build five must-pass and five must-fail artifacts. The
must-fail set is where the value is — make them adversarial, not obvious:

- truncated mid-sentence but within the length band;
- correct token count, unrelated content;
- Unicode lookalikes defeating `contains`;
- `\begin{a}\begin{b}\end{a}\end{b}` — balanced by count, wrongly nested;
- a reference whose anchor text appears in prose but resolves to no heading.

Report false-accept and false-reject rates as numbers; **assert FA == 0** on the
adversarial set. Gates are the only thing standing between the harness and a
model declaring itself done, so their false-accept rate is arguably the single
most important number in the codebase.

### 1.4 Reviewer precision/recall — `tests/test_layer1_reviewer_precision.py`

30 artifacts: 15 clean, 15 carrying exactly one planted defect of a known class
(contract rule violated, content duplicated from a sibling leaf, coverage gap
against its spine span, register drift from the pilot exemplar).

This one needs a real model, so gate it on `KUSUDAEMON_LIVE_REVIEW=1` and skip
otherwise — it stays out of the hermetic suite.

Report precision, recall, per-class recall, and mean tokens per review call.
**Assert only non-regression against a checked-in baseline JSON**, never an
absolute score. That is how a model-dependent metric becomes a regression test
without flapping every time the provider updates.

Include one artifact over `DEFAULT_ARTIFACT_CAP_TOKENS` with its defect planted
in the final 10%, since `review_node`'s own docstring says catching exactly
that is why fan-out exists.

This is the test that tells you whether review earns its tokens. Right now
`keep_depth_pass=False` and the fused A5-4 call are cost decisions with no
quality number attached to them.

### 1.5 Planner coverage — `tests/test_layer1_planner_coverage.py`

Property-based and fully deterministic. Over randomly generated spines
(1–500 units, random token weights):

- the union of child `[unit_start, unit_end]` spans covers every unit index;
- no two children overlap;
- every child's estimated load is within the leaf budget;
- child ids are unique;
- same spine + same seed → identical partition.

Then the half that actually matters: feed a scripted provider a **deliberately
bad** partition — gaps, overlaps, out-of-range indices, duplicate ids — and
assert the harness rejects it. A planner is only as trustworthy as the
validation behind it, and validation is what nothing currently exercises.

### 1.6 Provider fault injection — `tests/test_layer1_provider_faults.py`

`_FaultyProvider` wrapping `_ScriptedProvider` with a fault script: HTTP 429
with `Retry-After`, connection reset mid-stream, JSON truncated at a random
offset, schema-valid JSON with an extra unexpected key, empty response, and a
call that hangs past the episode budget.

Per fault, assert the run either completes or halts with a recorded event —
never silently drops a node, never leaves a partial artifact that a gate then
accepts. `terminal_events_per_node` stays ≤ 1 throughout.

Give truncated JSON its own subtest at three artifact sizes, since you have
already observed that DeepSeek V4 Flash's JSON validity degrades specifically
with artifact size. That observation deserves to be a test rather than a memory.

### 1.7 Close the zero-escalation hole — extend `eval/tasks.py`

The current report:

```
escalation precision: {runs: 6, precision: 1.0, escalated_runs: 0, triggers: {}}
```

`measure.py`'s own docstring says *"zero escalation across varied tasks means it
is too conservative."* Precision 1.0 over a set where the mechanism never fired
is not a passing grade — it is an untested code path reported as a success.
Worse, `grep -rn 'escalate('` shows only `size_defect_retry`,
`majority_regenerate`, and `operator` have call sites; `tiering.py:429` admits
`split_accepted` has none at all.

Add two tasks:

- **`t2-misclassified`** — a goal phrased small ("tidy up the helper module")
  against a work object measuring well past the T2 ceiling, so `_classify_raw`
  lands T2 on artifact count but the size defect must fire at runtime. Assert
  exactly one `run_tier_escalated` event with trigger `size_defect_retry`, and
  `tier_final > tier_measured`.
- **`t3-regenerate`** — enough review failures on one node to trip
  `majority_regenerate`. Assert escalation to T3.

And make the report print unwired triggers explicitly, so `split_accepted`
stays visible as a gap instead of disappearing into a clean-looking 1.0.

---

## Sequencing

| Step | Item | Effort | Unblocks |
|---|---|---|---|
| 1 | 0.3 output noise | 30 min | everything (you can read failures) |
| 2 | 0.1 tomli + lazy imports + CI matrix | 2 h | 3.10 support being true |
| 3 | 0.2 port 5 files + `tests/_support.py` | 4–6 h | 1.x reuse the shared fixtures |
| 4 | 0.4 dashboard digest fix + forced-inode test | 2 h | trustworthy mid-run observation |
| 5 | 0.5 reachability guard | 1 h | prevents recurrence |
| 6 | 1.2 context boundedness | 3 h | the core invariant |
| 7 | 1.1 crash matrix | 5 h | the resume claim |
| 8 | 1.3 gate soundness | 3 h | the "only code verifies done" claim |
| 9 | 1.5 planner coverage | 2 h | decomposition correctness |
| 10 | 1.7 escalation tasks | 2 h | makes the eval metric mean something |
| 11 | 1.6 fault injection | 4 h | robustness under real providers |
| 12 | 1.4 reviewer precision | 4 h + API spend | whether review pays for itself |

Steps 1–5 are repairs and should land as one PR. Steps 6–10 are free and
hermetic. Steps 11–12 are where real-provider cost starts.

Public-benchmark work belongs after step 10 — see `TESTING.md`. Leaderboard
numbers are the least informative thing per dollar until the mechanism
benchmarks are green.
