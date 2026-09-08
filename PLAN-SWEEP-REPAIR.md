# PLAN-SWEEP-REPAIR.md — make the next LongGenBench sweep produce a number

Companion documents: `PLAN-TOKEN-ACCOUNTING.md` owns the specifications this
document sequences (§I unit counting, §J output spine, §L retry continuation,
§O part files) — its §N order is amended by §G below, not replaced.
`PLAN-BENCH-INTEGRITY.md` §4.1 and §4.2 own record rebuilding and halt
quarantine; §C below reports two defects in the *shipped implementations* of
both. `BENCHMARKING.md` §0.4 owns the record schema.

This document owns one question: **what must land before the next LongGenBench
sweep, and what is each step actually verified to do.**

It specifies almost nothing new. Every item below is either a bug in an
already-landed implementation or an existing flag that was never switched on.
It exists because the 2026-09-08 sweep ran the whole §I/§L/§O stack and still
produced no measurement, and the reason is invisible from inside any single
owner's document: the fixes landed in one half of the codebase and the
measurement layer stayed in the other.

Five workstreams, in dependency order:

- **§C — record repairs.** The sweep crashed before writing predictions, and
  the summary it would have written was contaminated with another task's runs.
  Two small bugs in §4.1/§4.2's implementations. Independent of everything else.
- **§B — artifact identity.** Five readers disagree about what "the artifact"
  is. This is why §O's part files are currently a trap rather than a feature,
  and it must land before any run that could exercise them.
- **§D — decomposition.** `KUSUDAEMON_OUTPUT_SPINE=1` is verified to turn the
  one-node tree into five gated leaves. It is also, incidentally, the strongest
  blast-radius bound available, and it requires no new code.
- **§E — write granularity.** The only workstream whose central assumption is
  unverified, and the only one that depends on model compliance.
- **§F — the halt.** Nothing in §B–§E explains why all three runs stopped.

§G is the order. §H is acceptance. §I lists the corrections this document makes
to other documents.

---

## §0. The run this is built from

`--tasks 100 --arms C --seeds 1 2 3`, commit `48ca02e` (dirty tree),
`flags: {}`. Records at `bench_results/longgen/records.jsonl` rows 8–13.

| seed | reported | floors actually in the artifact | writer tokens | artifact chars | halt |
|---|---|---|---|---|---|
| 1 | 5% | 97, 98, 99, 100 (ends `*** finished ***`) | 471,499 | 5,244 | `escalated in execute: no detail` |
| 2 | 11% | 41–50 | 137,958 | 9,717 | `error in execute: The read operation timed out` |
| 3 | 2% | 100 | 5,000,004 | 1,623 | `error in execute: The read operation timed out` |

Contiguous, non-overlapping, and **no artifact contains floor 1**. That is the
§O whole-file-overwrite signature, unchanged by the §O work. The earlier 40%
run is the same artifact class (§H: floors 1–52, harvested after a SIGKILL,
`valid: false`, `invalid_reason: transport`).

**`completion_pct` on this task measures which chunk was last written, not how
much was generated.** 5% against 471k writer tokens is not a capability score
and 40% was not one either; the difference between them is the wall-clock at
which the transport died (5400s vs 4291/2083/2895s). Do not read the pair as a
regression, and do not read either as a baseline.

Arm A on this task is equally uninterpretable in the other direction:
`raw/100-floor_armA_seed1.txt` is 1,777 bytes of CLI transcript ("The script
has generated all 100 floor descriptions… Let me present the complete
document") — the model wrote a generator script and summarised it, and harvest
captured the summary. The same arm produced 135 KB on `300-block`.

## §A. What is verified, and what is not

Being explicit up front, because §D's priority and §E's demotion both lean on
this and the two conclusions point in opposite directions.

**Verified by execution, 2026-09-08, offline, zero provider calls:**

- `expected_units_info(<100-floor prompt>)` → `(100, 'declared')`;
  `extract_unit_delimiter` → `'#*#'`.
- `synthesize_output_spine(goal, token_budget=50_000)` → **5 units**,
  `units_expected` 21/21/21/21/16, ~3,927 projected tokens each. It sizes on
  projected *output*, so §J does not reproduce §H's input-blindness one layer up.
- `build_tree(units, <provider raising on any call>)` → **5 leaves**, gates
  `['nonempty', 'max_tokens:50000', 'units_min:N@#*#']`, `headers:std` in
  `warn_gates`. The provider never fired, which proves `planner.py:573`'s
  forced-tiling branch runs ahead of both `plan_level` and the
  `code_tile_planner` "slice fits budget" collapse.
- `_gate_units_min` against the three real artifacts: 4<21, 10<21, 1<16 — all
  fail. Seed 1's `nodes_passed: 1` (a four-floor document passing gates) becomes
  a failure.
- The `KeyError: 300` reproduced exactly; the §C1 filter reduces 12 records to
  the 6 cells of this matrix.
- `tokens.count_tokens('hello ' * 100)` → 150, i.e. a real tokenizer rather
  than `words/0.75`. §A2–§A4 have landed.

**Verified from code, not executed:** the reader table in §B1; `classify_halt`'s
four call sites; that `versions_dir` creates its directory on every call, so
`round_loop.py:850`'s `if v_dir.is_dir()` guard is vacuous.

**NOT verified, and load-bearing where noted:**

- **That a writer obeys a mandatory part-file instruction.** Observed base rate
  is 0/3 against the current optional phrasing. §E depends on this entirely.
- That five leaves complete. Five leaves is also five chances to hit §F.
- Anything about the transport failures. §F is a hypothesis with a test, not a
  diagnosis.
- §O11's open item stands: that `edit` works reliably at artifact scale is
  still unconfirmed.

---

## §B. Artifact identity: five readers, three answers

### §B1. The defect

`v0/run_dir.py:node_artifact_text` resolves part files under `out/<node>/` and
falls back to `out/<node>.md`. It is the correct accessor and it already
handles both layouts. Roughly half the codebase does not use it.

| reader | what it reads | consequence if a writer uses parts |
|---|---|---|
| `v1/round_loop.py:817` `_read_artifact` | `out/<node>.md` only | feeds **gate evaluation (line 207)** and **the reviewer (line 265)** — both evaluate `""` |
| `v1/round_loop.py:845` shrink check | same | `current_bytes == 0` → classified `unscoped` every time |
| `v1/round_loop.py:887` §L4 restore | writes `out/<node>.md` | a non-empty parts dir shadows it — the restore is a silent no-op |
| `v0/runner.py:61` `_snapshot_pre_writer` | copies `out/<node>.md` | snapshots nothing; §L4/§L5/§O7 all have no prior state |
| `pipeline/cli.py:1341` | `out_dir.glob("*.md")` | never sees the artifact |
| `scripts/run_longgen_bench.py:226,233` | `out_dir.glob("*.md")` | never sees the artifact |

Already correct: `v3/checks.py:49,91`, `pipeline/corruption.py:80`,
`v3/assemble.py:94`, `pipeline/prompts.py:536`.

**A writer that complied with the instruction §O5a shipped would fail every
gate on empty text and score 0.** The parts path as it stands is not an unused
feature; it is a trap, and it is armed for whichever writer reads the prompt
carefully first.

This is not "choose a contract". `node_artifact_text` already resolves both
layouts, so nothing needs choosing — the readers are simply wrong, and the
mechanisms built on them (snapshot, shrink detection, recovery, harvest) are
each correct for a different definition of the artifact than the one the writer
is being handed.

### §B2. `_read_artifact` — parts-aware

```python
# v1/round_loop.py:817
def _read_artifact(run_dir: Path, node_id: str) -> str:
    from ..v0.run_dir import node_artifact_text
    try:
        return node_artifact_text(run_dir, node_id)
    except (FileNotFoundError, OSError):
        return ""
```

`node_artifact_text` accepts a node or a bare id (`getattr(node_or_id, "id",
node_or_id)`), so all three call sites pass unchanged. This one edit fixes gate
evaluation, the reviewer, and the shrink check together.

### §B3. Snapshot the resolved artifact, not the single file

`v0/runner.py:_snapshot_pre_writer` currently guards on
`art_p.exists() and st_size` and then `shutil.copy2`. Both must go through the
accessor, or every snapshot is empty the moment parts exist:

```python
from ..v0.run_dir import node_artifact_text, write_text_atomic
try:
    text = node_artifact_text(run_dir, node_id)
except (FileNotFoundError, OSError):
    return None
if not text.strip():
    return None
write_text_atomic(v_dir / f"attempt_{int(time.time() * 1000)}.md", text)
```

### §B4. §L4's restore granularity is the same bug one level down

Restoring a *concatenation* into `out/<node>.md` is wrong whenever a parts dir
exists, because the parts dir wins on the next read. Two honest options:

- **(a) Snapshot and restore the parts directory**, not a flattened string:
  `out/.versions/<node>/attempt_<ts>/` as a directory copy. This is the only
  option under which §L4 can restore *the part that was lost* rather than
  overwriting five good parts with a stale concatenation.
- **(b) Skip §L4 entirely while a parts dir is non-empty**, and log
  `artifact_shrank` without acting.

(a) is correct; (b) is one line and is honest about doing nothing. Do not ship
the current behaviour, which claims to restore and does not.

Note the general shape, because it is the same error as §O's original: **a
recovery mechanism whose granularity is coarser than the destructive operation
cannot recover anything.** §O7/§L4/§L5 all operate per *episode*; the loss is
per *tool call*. §D is what actually changes that ratio.

### §B5. Harvest

`scripts/run_longgen_bench.py:harvest_artifact` and `pipeline/cli.py:1341` both
glob `out/*.md`. Both need the parts dir. In the bench script, `bench_common`
already puts `src` on `sys.path`, so the accessor is importable directly; prefer
`assembly/main.md` first (as `cli.py` already does), then per-node resolution,
then the bare glob as the last fallback.

### §B6. Test

Hermetic, no backend. Extend `tests/test_output_spine.py` (or a new
`tests/test_artifact_readers.py`): build a run dir with `out/n1/part-01.md` and
`out/n1/part-02.md` and no `out/n1.md`, then assert that gate evaluation, the
shrink check, the snapshot, and `harvest_artifact` all see the concatenation.
That test fails on today's code at four call sites, which is the point.

**Confidence: high that these are bugs. Unverified that fixing them changes any
number today** — no writer in the 2026-09-08 sweep used parts, so all six
readers happened to agree. This is a fix that stops the next sweep from
producing a mystery, not one that improves this one.

---

## §C. Record repairs

### §C1. `write_predictions` crashes on a resumed matrix

`run_longgen_bench.py:734` rebuilds `all_records` from the append-only
`records.jsonl` per §4.1, which still holds the `300-block` rows from the Sep-6
sweep. `write_predictions` then builds `by_index` from *this invocation's*
`tasks` only and dies at line 570 on `KeyError: 300`.

The same contamination reaches `summarize(all_records)` on the line below
without crashing: it folds six `300-block` runs into the `100-floor` arm stats
and `mean_completion_pct`. That half is worse, because it is silent.

```python
# run_longgen_bench.py:734
selected = {i for i, _ in tasks}
all_records = [
    r for r in dedupe_records(read_records_jsonl(records_path) + records)
    if r.get("dataset_index") in selected
]
```

This preserves §4.1's intent exactly — §4.1 is about not discarding earlier
cells *of this matrix*, and rows for tasks the invocation did not select were
never part of it. Verified: 12 records → the 6 cells of this matrix.

Check `run_harness_bench.py` for the same shape before closing this out.

**Test:** `tests/test_bench_cli.py` — a `records.jsonl` holding two tasks, a run
selecting one, asserting predictions are written and the summary names one task.

### §C2. A halt with no detail is being scored

Seed 1 halted with `escalated in execute: no detail`. `classify_halt` finds no
budget pattern, no status code and no transport pattern, so it returns `agent`,
`is_valid` stays true, and 5% lands in `mean_completion_pct` as a capability
measurement. A halt whose detail is literally "no detail" is not evidence of
model capability.

Add `unknown` to `HaltCategory` (`eval/common.py:9`) for escalations with no
detail, and add it to the exclusion tuples at `run_longgen_bench.py:400` and
`run_harness_bench.py:389`. Four call sites total; all are in the two scripts.

Do **not** file it as `transport` — it is not one, and mislabelling it corrupts
the quarantine reason counts that §4.2 exists to produce.

**Confidence: high.** Both are contained, hermetically testable, and block
nothing else.

---

## §D. Decomposition, which is also the blast-radius bound

`KUSUDAEMON_OUTPUT_SPINE=1` is implemented (`driver.py:1386`), off by default,
and was off for this sweep (`flags: {}`). Switching it on is verified to
produce, on this exact prompt:

```
synthesize_output_spine -> 5 units, units_expected 21/21/21/21/16, ~3,927 tok each
build_tree              -> 5 leaves
  unit-01  gates=['nonempty','max_tokens:50000','units_min:21@#*#']  warn=['headers:std']
  ...
  unit-05  gates=[...,'units_min:16@#*#']
```

Three defects close at once: the one-node tree (§H defect 1), `units_expected`
never reaching a planner leaf (§H defect 3 — now populated), and the vacuous
gate set that let a four-floor artifact pass.

**And it bounds the loss without anyone's cooperation.** With five leaves the
writer owns 21 floors (~3.9k output tokens) in its own file. A whole-file
overwrite costs at most 21 floors and cannot touch the other four leaves. That
holds with zero part-file compliance and zero prompt changes, which is why §D
outranks §E rather than depending on it.

Run it as `--flags KUSUDAEMON_OUTPUT_SPINE=1` so §A5 records the flag on the
cell; the sweep above recorded `flags: {}` and that is how we know it was off.

**Confidence: high on the tree shape (executed). Unknown on whether five leaves
finish** — see §F.

---

## §E. Write granularity — the unverified workstream

### §E1. What shipped, and why it did nothing

`_artifact_instruction` dropped §L1's carry-forward clause ("before any
whole-file overwrite, read the current file and carry its existing content
forward") and replaced it with an *optional* offer: writers "may" write part
files "or write the single artifact directly." All three took the second branch.

Net effect: the harness has less protection than before the change. An optional
safety property is not a safety property.

### §E2. Make the grid, don't ask for a convention

Making the instruction mandatory is the obvious move and it is worth doing, but
it rests entirely on compliance, and the measured compliance rate with the
current phrasing is 0/3. A stronger version costs little:

**With §D on, the harness already knows the unit ranges** — `unit-01` is
"Floors 1 to 21". So pre-create the part files it expects
(`out/<node>/units-001-021.md`, empty) and name them in the brief. That turns
"invent and maintain a naming convention across five episodes" into "fill in
these files", which is a materially easier instruction to follow and makes
coverage and overlap computable from filenames alone.

Keep §O9's constraint intact: parts are rewritable, not immutable. The
invariant is only that no single write carries more than one part's content.

### §E3. What this does not need

§O7's `artifact_shrank` at write granularity **preserves zero tokens**. It is
forensics — worth having, because recovering §0's diagnosis required grepping
floor numbers out of three files by hand, but it is not a fix and should not
gate anything. With §D and §E2 in place the same signal falls out of the
filenames for free.

**Confidence: low on §E2's compliance assumption. Do not sequence anything
behind it.**

---

## §F. The halt nobody has diagnosed

All three runs ended on `error in execute: The read operation timed out` or an
escalation with no detail. **Nothing in §B–§E touches this.** §0–§E explain what
the runs *measured*; none of them explain why the runs *stopped*.

The phrasing is a `urllib` socket read timeout. Writers execute as an OpenCode
subprocess, not through `urllib`, so this is a **role** call dying inside the
execute phase — reviewer or triage — against `v1/provider.py:124`'s
`KUSUDAEMON_HTTP_TIMEOUT`, default 300s. That is a hypothesis, not a finding.

**Cheapest test, in order:**

1. **Keep the run directory.** §O11 already records that the previous ground
   truth (`~/.kusudaemon/runs/longgen_100-floor_armC_seed1`) was deleted and its
   analysis can no longer be re-derived. Do not repeat that. One seed, run dir
   preserved, before any interpretation.
2. Widen the error to name the phase, role and elapsed time at the raise site in
   `v1/provider.py`, so the next occurrence is self-diagnosing rather than
   requiring a preserved run dir at all.
3. Only then consider whether 300s is the wrong number. Raising a timeout before
   knowing which call is hitting it is how a 300s hang becomes a 900s hang.

**Confidence: none. This is the largest open risk in the plan** — five leaves is
five more chances to hit whatever this is, and §D's improvement is invisible if
every run still dies mid-flight.

---

## §G. Order

**Wave 0 — mechanical, hermetic, no provider calls, nothing depends on the
outcome. Land together.**

1. §C1 record filter. Unblocks the sweep from crashing at the end.
2. §C2 `unknown` halt category and both exclusion tuples.
3. §B2, §B3, §B5 reader unification, with §B6's test in the same commit.
4. §B4 — pick (a) or (b) explicitly. Shipping the current behaviour is not an
   option; it claims a restore it does not perform.
5. §F2 — make the provider raise site name phase, role and elapsed.

Full suite must stay green, including the ≥1100 reachability floor in
`test_suite_reachable.py`.

**Wave 1 — one seed. This is the measurement that decides the rest.**

6. `--tasks 100 --arms C --seeds 1 --flags KUSUDAEMON_OUTPUT_SPINE=1`,
   **run directory preserved**.

Read three things off it, in this order: did it finish (§F); how many leaves
(expect 5); did any leaf's artifact shrink. Do not run three seeds — one seed
answers all three questions and the second and third only cost money until §F is
understood.

**Wave 2 — branch on Wave 1.**

- Died on transport again → **§F is the whole job.** Nothing else is measurable
  until it is fixed. Do not proceed to §E.
- Finished, leaves clobbered internally → §E1/§E2, then re-run one seed.
- Finished, leaves intact → skip §E entirely; go to Wave 3.

**Wave 3 — the sweep.**

7. Both arms, seeds 1–3, flag recorded. Mark the existing arm-C
   `100-floor` rows invalid in `BENCHMARKING.md` §9 (§M3 already requires this;
   the 2026-09-08 rows join the list).
8. Only now is §K (parallelism) measurable — §H6's rule applies: with a
   one-node tree there was nothing to run concurrently, and with five leaves
   there is.

**The ordering constraint that matters:** §B before Wave 1, not after. If a
writer picks the parts branch during Wave 1 with today's readers, the run scores
0 and looks like a decomposition failure.

## §H. Acceptance

- **§C1:** a sweep selecting `--tasks 100` against a `records.jsonl` containing
  `300-block` rows writes predictions and a summary naming one task. No
  `KeyError`.
- **§C2:** a record with `halt_reason: "escalated in execute: no detail"` is
  excluded from `mean_completion_pct` and appears under
  `excluded.by_reason.unknown`.
- **§B:** the §B6 fixture — parts on disk, no single file — passes gates,
  snapshots non-empty text, and harvests the concatenation. Fails at four call
  sites today.
- **§D:** the Wave 1 record shows `nodes_total: 5` and
  `flags: {"KUSUDAEMON_OUTPUT_SPINE": "1"}`.
- **§E (if reached):** every part file is under ~1.5x its declared unit share,
  and the union of parts covers 1–100 with no gap.
- **§F:** a halt in the execute phase names the phase, role and elapsed seconds.

None of these is `completion_pct`. **`completion_pct` is not an acceptance
criterion for any step in this plan** — it is the thing that becomes meaningful
once they all pass.

## §I. Corrections to other documents

1. **`PLAN-TOKEN-ACCOUNTING.md` §N Wave 0 item 6** ranks §O7 `artifact_shrank`
   as "the cheapest instrumentation in this file… land it before any fix, so the
   fixes have a signal to move." Demote it. It preserves no tokens, its
   attempt-granular form cannot see the loss it was built for (§B4), and once §D
   and §E2 are in the signal is derivable from filenames. Keep it as forensics;
   remove it as a prerequisite.
2. **§N Wave 2's isolation run** — "re-run one seed of `100-floor` before §J…
   that isolates their contribution from §J's" — has now been attempted
   (2026-09-08) and cannot yield what it was designed to yield. The run died on
   transport and its gates were reading a file the writer might not have been
   writing (§B). The §O-and-§L-alone measurement is not recoverable; do not
   spend three more seeds pursuing it.
3. **§N's "one hard ordering constraint"** — do not land §J against the 1.66x
   estimator — **is discharged.** §A2–§A4 landed (verified: `count_tokens`
   returns 150 for 100 words, not 133), and `synthesize_output_spine` sizes on
   projected output rather than input text.
4. **`README.md` §5** omits `PLAN-TOKEN-ACCOUNTING.md` from the ownership table
   even though it owns §H–§O. Add it, and add this document.
