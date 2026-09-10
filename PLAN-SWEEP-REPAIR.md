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
  *Closed 2026-09-09: a 45 s reviewer/triage socket timeout, unretried. See §F.*

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
- ~~Anything about the transport failures. §F is a hypothesis with a test, not a
  diagnosis.~~ **Superseded 2026-09-09:** §F is now diagnosed from the rerun's
  own `elapsed=45.1s timeout=45.0s` and fixed. What remains unverified is
  whether the fix is *sufficient* — review latency still grows with the
  document, so a wider timeout is the same trap further out.
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
finish** — see §F. The 2026-09-09 rerun with the flag reached 26 floors / 26%
in 1355 s across 6 writer calls before the §F timeout, so the path executes
online; it has still never been allowed to run to completion.

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

## §F. The halt — diagnosed and closed (2026-09-09)

All three 2026-09-08 runs, and the 2026-09-09 `KUSUDAEMON_OUTPUT_SPINE=1`
rerun, ended on `error in execute: The read operation timed out`. **§F2's own
instrumentation is what closed this**: the rerun's record reads

```
error in execute: provider request failed
[phase=unknown role=unknown node=- elapsed=45.1s timeout=45.0s]:
The read operation timed out
```

`elapsed≈timeout` at **45.0 s, not the 300 s this section assumed.** The
hypothesis above ("a role call dying inside the execute phase") was right; the
number was wrong, and the number is the whole story.

### F.1 Where 45 s came from

`pipeline/driver.py::_role_provider` set `timeout = 45.0` for `reviewer` and
`triage`, threaded through `roles/factory.make_role_provider` into
`OpenAICompatibleProvider.timeout` — which is `urllib`'s **socket read timeout
for one entire non-streaming completion** (`complete_json` defaults
`streaming=False`).

The value's origin is `docs/PLAN-REVIEW-LATENCY.md` T0-6, which specified it
for `KUSUDAEMON_ROLE_TIMEOUT`: a **CLI subprocess episode budget**, on the
reasoning that "a reviewer episode still running at 60 s has already failed at
something and should fail fast into the retry rather than hold the wave." Two
things broke in the port to the HTTP transport:

1. **The semantics changed.** An episode budget bounds a subprocess that should
   have finished; a socket read timeout bounds one HTTP response that is
   legitimately still streaming. `v1/reviewer.py::_call_review` puts the
   **entire artifact** in the user message, so review latency grows with the
   document — on a 100-floor task, crossing 45 s is a matter of when, not if.
   This is why the halt always arrived mid-run rather than at the start.
2. **The retry T0-6 assumed did not exist.** `_http_transport` raises
   `ProviderError` (not `ProviderHTTPError`) for `URLError`/`TimeoutError`/
   `OSError`. `_call`'s ladders caught only `ProviderHTTPError`, so a socket
   timeout took **zero** of the three configured HTTP retries;
   `complete_json` catches only `ProviderHTTPError` (400 → drop
   `response_format`); and `v1/reviewer.py:279`'s `except ProviderError`
   re-raises unless the message contains `maxLength`. Three layers, none of
   which held it, and it surfaced at the phase boundary as a dead run.

### F.2 What landed

- **`v1/provider.py::_call`** — a transport-error branch retrying on the same
  short exponential shape as 5xx (never the hours-long §D11 ladder: a socket
  timeout is a link problem, not a rate limit), with its own
  `transport_attempt` counter so a 429 ladder earlier in the same call cannot
  silently spend the transport budget. Fires `on_backoff` and honors
  `should_abort` on §E16's sliced sleep.
- **`roles/factory.make_role_provider`** — new `http_timeout` parameter,
  applied on the HTTP branch only. T0-6's 45 s survives unchanged as the
  *episode* budget on the backend branch, where its premise still holds.
- **`pipeline/driver.py`** — `_role_provider` passes
  `http_timeout=_role_http_timeout()` (default **180 s**, override
  `KUSUDAEMON_ROLE_HTTP_TIMEOUT`) for reviewer/triage. §F3's warning stands and
  is the reason ordering matters: widening the fuse *without* the retry above
  would only convert a fast death into a slow one.
- **§F2 completed.** `make_role_provider`'s HTTP branch was dropping `role=`
  (it forwarded it only to `BackendRoleProvider`) and nothing ever set `phase`,
  which is why the message that solved this still said
  `phase=unknown role=unknown`. Both are now forwarded, and `_run_phase`
  stamps `phase` on the shared provider at each phase boundary. The same
  fields drive `calls_by_role`/`tokens_by_role` in bench records — the run that
  produced the evidence above reported `calls_by_role: {"unknown": 2, ...}`
  for exactly this reason.
- **Tests:** `tests/test_provider_transport_retry.py`, 14 hermetic tests, no
  network — the regression itself, bounded retries, counter independence,
  halt-signal abort, backoff observability, non-429 4xx still surfacing
  immediately, and the factory/driver timeout plumbing.

### F.3 What this does *not* claim

It does not claim the next run finishes. It claims a slow reviewer call is no
longer fatal on the first occurrence. The durable latency fixes remain
`docs/PLAN-REVIEW-LATENCY.md` T1-1 (deterministic pre-filter — zero-call review
when all gates pass and every judgment is gate-covered) and T1-2 (two-stage
triage), plus optionally streaming role calls so the timeout bounds a chunk
rather than a whole response. Those are follow-ups, not blockers.

**Do not attribute the 45 s halt to prompt size.** Seed 1's artifact was 26,239
characters — roughly 6.5k tokens. A 6.5k-token review prompt does not take 45
seconds; endpoint latency did, which is what F.2's retry and wider timeout
address. The two reviewer-chunking defects recorded in §F.4 are real and were
found while chasing this, but they are **not** the cause of the halt, and
saying otherwise would repeat §0's mistake of reading a number as a
measurement of something it does not measure.

**Residual risk for §D:** five leaves is five reviewer calls where there was
one. That multiplies exposure to endpoint latency even though it divides writer
blast radius.

### F.4 Two reviewer-chunking defects found while diagnosing §F

Both live on `review_node`'s over-cap path, and both are **latent**: the path
only runs when `estimate_tokens(artifact) > artifact_cap_tokens` (50k). No
LongGenBench artifact produced so far reaches it — `100-floor` at ~6.5k tokens
and `300-block` at ~28.5k both take the single-call under-cap branch. These
are prerequisites for a *successful* long-document run, not repairs to a
failed one. A completed 300-block artifact would be the first thing to cross
the cap, and it would cross it into both defects at once.

1. **Fan-out never engaged on a delimiter-structured artifact.**
   `_sections_by_heading` matched ATX headings only (`^#{1,6}[ \t]+\S`).
   LongGenBench delimits units with `#*#`, which has no whitespace after its
   hashes, so the splitter found **zero** sections, `_group_sections` passed
   the empty list through, and `review_node` fell to the no-headings
   whole-artifact truncation branch. PLAN.md §A9's "fan-out replaces
   truncation" has therefore never applied to this benchmark. The delimiter
   was not missing — `tokens.extract_unit_delimiter` resolves it per run and
   it is baked into the node's `units_min:N@<delim>` gate; the reviewer simply
   never read it back. Fixed by `_unit_delimiter_from_gates` +
   `_sections_by_delimiter` + a `_split_sections` dispatcher (delimiter first,
   ATX headings as fallback, `[]` when neither), matching
   `_gate_units_min`'s own `(?m)^\s*<delim>` so splitter and gate agree about
   where a unit begins. `review_node` takes an optional `unit_delimiter`
   override for callers that know it independently.

2. **The artifact cap overshot its own ceiling by ~31%.**
   `cap_artifact_text` cut at `ceiling_tokens * 0.75` words, documented as
   "the inverse of `estimate_tokens`". That was true of the old whitespace
   heuristic and false once `estimate_tokens` began delegating to
   `tokens.count_tokens` (PLAN-TOKEN-ACCOUNTING.md §A2/§A3): 50k ceiling →
   37,500 words → **65,655** measured tokens. Fixed by binary-searching the
   largest word prefix that fits, measuring the assembled string (prefix plus
   truncation notice) rather than budgeting the notice separately —
   tokenization is not additive across a concatenation boundary and the
   separate-budget form overshot by one token. O(log n) tokenizer calls, only
   ever on an artifact already over cap.

**Why not model-chosen reads instead.** Roles have no tool loop —
`RoleProvider` is one method, `complete_json` — and §A9 rejects the
neighbouring shape ("unbounded reviewer recursion"). The decisive objection is
soundness rather than mechanism: for a role whose output *is* the gate,
"I did not look there" and "no defect there" become indistinguishable, and a
`pass` silently degrades to "pass on the part I sampled". Deterministic
splitting gives coverage accounting for free — every byte lands in exactly one
group, which is why the preamble before the first unit is kept as its own
section rather than dropped. T1-2's two-stage triage is the sanctioned way to
get model-chosen *selection* without a tool loop: one cheap call picks from
structure plus gate results, one bounded call per pick.

**Tests:** `tests/test_reviewer_chunking.py` (21 hermetic tests). The
integration cases lower `artifact_cap_tokens` rather than building 50k-token
fixtures. Fixing (2) also resolved
`test_v1_reviewer_fanout.PathologicalMegaSectionTest`, which had been failing
at HEAD — it asserted the ceiling the cap was overshooting.
`test_v1_units.ReviewerInputCapTest.test_cap_artifact_text_marks_rather_than_silently_cuts`
was rewritten: it asserted the `0.75` ratio itself (`"x x x x x x x"`), which
was the defect, and now asserts the contract its own name states — mark the
cut, stay inside the ceiling.


### F.5 An unevaluatable rubric item must not fail

`_PROSE` and `_DIRECT` (`v6/templates.py`) attach `claims_supported` as a
**closed** judgment: *"Every factual claim traces to a declared input, to the
contract, or is explicitly marked as an assumption."* On a greenfield
generative leaf that item has no determinate answer. The 2026-09-09
`longgen_100-floor_armC_seed1` run is the demonstration: `contract.md` renders
`## Global rubric` / `(none)`, the leaf's only declared input is
`Spine unit: {...}` — the harness restating the assignment, which itself says
*"Document each floor independently with detailed descriptions of the intended
facilities, architectural features, and unique design elements"* — and the
writer's only route to satisfying the item is to annotate every sentence as an
assumption, which then fails `on_topic`.

The verdicts prove it is a coin flip rather than a finding: **unit-01 passed
`claims_supported` at 23:41:54 and unit-02 failed it at 23:44:02**, same
template, same reviewer model, same class of invented prose. Note also that
`review_sample_rate: 0.05` is not "review 5% of nodes" — reading
`round_loop.py:470` it is a *second-opinion sampler on passing verdicts*
(temperature 0.7), so unit-01's pass had a 95% chance of never being checked.

`claims_supported` is not a gate, so it never enters `all_passed` — but a failed
verdict fires `node_review_failed` → redispatch, and `max_attempts: 3` means
three bad flips lose the leaf and 25 of 100 floors. That is a false-zero
generator of exactly the class PLAN-BENCH-INTEGRITY.md exists to remove.

**Rule:** a grounding judgment is not judged when there is nothing to trace to
— the contract declares no rules **and** no declared input is upstream content.
It is recorded as a **vacuous pass**, the same shape `v1/gates._gate_headers_std`
already uses (`"vacuous pass: non-markdown delimiter present"` appears in the
same audit file). Never a silent omission (which would read as "never in the
rubric") and never a failure.

Implementation in `v1/reviewer.py`: `contract_declares_rules` (any line that is
not a section header, `(none)`, or the intake-skipped note),
`has_traceable_source` (a `Handoff from `/`Research finding` line — a
`Spine unit:` line is the assignment, not a source), `ungroundable_judgments`
over `GROUNDING_JUDGMENTS`, and `effective_judgment_for` extracted so the T1-4 /
T2-2 filters are derived in one place. `review_node` is now a thin wrapper:
`_review_node_judged` never asks the model about an ungroundable item, and the
wrapper adds the vacuous-pass records back to `items`. A vacuous pass can never
turn a fail into a pass, because it only ever adds items the inner call was not
asked to judge. Where a source or a contract rule *does* exist, the item is
judged exactly as before and can still fail.

### F.6 Typographic punctuation defeats the writer's exact-match edit tool

From `scratch/unit-02/trace.jsonl`, in the writer's own words:

> I see - the file has `skyscraper’s` with a right single quotation mark
> (Unicode), and when I try to match with a regular apostrophe it doesn't work

and, two thoughts later:

> let me just use the write tool to write the entire file

**That is the §O whole-file clobber, and this is the mechanism that drives the
model into it.** An `edit` whose `oldString` the model cannot reproduce
byte-for-byte fails repeatedly, and rewriting becomes the only apparent way
forward. The model typed the curly apostrophe itself on the first pass; it
cannot reliably type it again. The edit tool belongs to the backend CLI
(OpenCode) and cannot be patched here, so the characters it trips over are
removed instead.

- `v0/run_dir.py`: `normalize_typographic_punctuation` (pure) maps smart
  quotes, en/em dashes, ellipsis and exotic spaces to ASCII —
  **punctuation only**, so accented and non-Latin text is untouched;
  `normalize_artifact_punctuation` rewrites the artifact in place, handling the
  single file *and* `out/<node>/*.md` part files separately so the parts layout
  §B depends on is preserved.
- `v0/runner.py`: called on the redispatch branch — the one moment the harness
  owns the file and the writer does not — before `_continuation_prompt`, logging
  `artifact_punctuation_normalized` when it changes anything, and never raising.
- Prompts: `pipeline/prompts.py::_artifact_instruction` now asks for ASCII
  punctuation on the *first* pass (the preventative half), and the continuation
  framing adds "if an edit fails to match, re-read the exact bytes and retry a
  smaller edit — do not fall back to rewriting the whole file."

**Tests:** `tests/test_grounding_and_punctuation.py` (23 hermetic tests),
including the exact character from the live trace and a parts-layout case that
would fail a concatenate-then-write implementation.

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
- **§F:** a halt in the execute phase names the phase, role and elapsed seconds
  — and a single socket read timeout no longer *is* a halt: it costs a bounded
  backoff and the run continues. A record whose `halt_reason` is a transport
  error now means the endpoint failed four times in a row, not once.

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

---

## §J. Wave 1 readout and repairs (2026-09-09)

Run: `~/.kusudaemon/runs/longgen_100-floor_armC_seed1`, arm C, seed 1,
`flags: {"KUSUDAEMON_OUTPUT_SPINE": "1"}`, `nvidia/nemotron-3.5-lightning-30b-a3b`.
Started 09-08 22:00, halted 22:23, resumed 09-09 16:34, completed 17:10.

### §J1. The three §G questions, answered

**1. Did it finish?** Yes — `run_completed`, `assembly/main.md` exported,
`returncode: 0`. With two asterisks:

- The 09-08 segment died at `phase_failed` in `execute` round 4 with
  `elapsed=45.1s timeout=45.0s ... phase=unknown role=unknown` — the §F halt
  exactly as diagnosed, *before* the §F fix existed. The 09-09 resume ran the
  fixed code and saw no transport error. §F is closed by this run, but note
  the halt was survived by an operator `resume`, not by the retry ladder.
- `unit-02` reached `passed` only via an operator bypass
  (`node_bypass_requested`, 16:54:05) after two review failures. The run is
  therefore **not** an autonomous completion. See §J3.

**2. How many leaves?** **Four**, not the five §D predicted. `spine.json` shows
`units_expected: 25` on each of `unit-01..04` with `unit_delimiter: "#*#"`;
gates `['nonempty', 'max_tokens:50000', 'units_min:25@#*#']`, `headers:std` in
`warn_gates`. §D's `21/21/21/21/16` was computed against a different token
budget. The *shape* claim §D makes — forced tiling, `units_expected` reaching a
planner leaf, non-vacuous gates — is confirmed. Update §D's arithmetic; do not
treat 4-vs-5 as a defect.

**3. Did any leaf's artifact shrink?** No. `out/.versions/unit-02/` holds two
attempt snapshots at 26,296 and 26,883 bytes against a 31,777-byte final — the
artifact grew monotonically. All four leaves are intact and non-overlapping:
unit-01 floors 1–25, unit-02 26–50, unit-03 51–75, unit-04 76–100, 25 `#*#`
units each. **§D's blast-radius bound held**: `unit-03` performed a whole-file
overwrite mid-episode and it cost nothing outside its own leaf.

Contrast §0's table, where no artifact contained floor 1 and each held one
contiguous chunk. That signature is gone.

### §J2. The artifact was complete and the score was wrong

`assembly/main.md` contains **100 of 100 floors**, 100 `#*#` delimiters,
124,016 chars. The bench record says `blocks_found: 75`,
`completion_rate: 75.0`.

**Cause.** `scripts/longgen_common.py`'s `FINISHED_RE` is
`r"\*\*\*\s*finished\b.*"` under `re.DOTALL`, and `to_output_blocks` applied
it as `FINISHED_RE.sub("", text)` — deleting everything from the **first**
sentinel to EOF. Every leaf is handed the whole goal, including *"When the
design of all 100 floors is complete, use '\*\*\* finished' to indicate the end
of the document"*. `unit-03` obeyed it and ended its slice with `*** finished`
(raw line 157); `unit-04` ended with `*** finished ***` (line 211). The
first-match cut discarded floors 76–100.

This is the same class of defect as §C1/§C2 — the harness generated the work
and the measurement threw it away — and it is **more** dangerous than either,
because it produces a plausible number rather than a crash.

**Verified fix:** re-scoring the untouched artifact with the last-sentinel cut
yields `found=100/100, rate=100.0, missing=[]`. Re-scoring all thirteen raw
artifacts changes **only** this one; every other cell is byte-identical.

**Wave 1's real completion is 100%, not 75%.**

### §J3. What actually cost this run its time and tokens

2,662,453 writer tokens across 11 writer calls for a ~31k-token artifact — an
~85x amplification. Per-episode wall clock: `unit-04` 259 s clean;
`unit-03` **705 s**; `unit-02` three episodes plus a bypass. The difference is
not model verbosity. Three mechanisms, all harness-side:

**(a) The exact-match edit tool versus typographic punctuation (§F.6).**
`unit-02`'s reviewer demanded `claims_supported` citations on eight floors. The
writer's `edit` calls failed repeatedly against `skyscraper’s` (U+2019) and it
walked its own reasoning down to *"Let me try a shorter match."* — four
verbatim repeats — before concluding *"I'll use the write tool to write the
entire file."* §F.6's `normalize_typographic_punctuation` was authored at
17:00, twenty-six minutes **after** the driver process started at 16:34. Python
had already imported the old modules. **§F.5 and §F.6 did not execute in any
part of this run** and remain unverified online.

**(b) `bash` denied to the writer.** `_PROSE` sets `tools=("read", "save")`,
which `translate_tools_to_opencode_permissions` renders as `bash: deny`. When
`unit-03` wanted to validate a JSON file it reasoned *"let me use bash to
inspect the exact bytes"*, the call came back as OpenCode's synthetic `invalid`
tool, and it then spent several turns counting braces by eye before rewriting
the file. Denying inspection did not make the run safer; it routed the writer
to the destructive tool.

**(c) The first-attempt prompt said nothing about not rewriting.** §E1 already
records that the carry-forward clause became optional. Worse: every remaining
"do not rewrite / append / minimal change" sentence lives on a **retry-only**
path (`_PATCH_RETRY_INSTRUCTION`, `prompts.py:447 if node.last_defect:`,
`runner.py`'s continuation framing). `unit-03` was on `attempts: 0`. The
attempt that writes 34 KB was the one told nothing. Measured compliance with
the optional phrasing stays 0/4.

### §J4. Prompt-induced repetition

The writer's reasoning is repetitive because the prompt contradicts itself, not
because the model rambles. Segment 1 renders the entire run goal
(*"Ensure that the document consists of 100 entries"*); segment 5 says
*"Produce the artifact for Floors 51 to 75"*; nothing reconciles them. The
writer's first thinking block is a direct read-out:

> *"So it seems like I need to produce a full 100-floor document, but the
> assigned range is 51-75."*

~640 completion tokens on turn 1 resolving an ambiguity the harness created,
and the same confusion resurfaces later (*"Floor 74 incorrectly designated as
photography studio (the requirement is Floor 99, outside this range)"*) — a
global constraint applied inside a local slice.

The goal is also rendered **three times** in one context: `spec.md`'s `## Goal`
via `_goal_and_rubric_block`, a byte-identical copy inside `spine/unit-03.md`
under `## Complete Task Specification & Global Rules (Reference)`, and a
third filtered restatement as `## Specific Constraints for Floors 51 to 75`
(whose `- -` double bullets and leaked `2)`/`3)` numbering show the slicer
doing string surgery). Segment 2 additionally announces *"Global contract —
every artifact you produce must satisfy it:"* over a contract whose body is
`(none)`.

Prompt size is not the problem: turn 1 is 10,374 prompt tokens of which 8,704
are OpenCode's own cached system prompt. The harness's share is ~1.0–1.1 k. The
defect is duplication and contradiction inside that 1 k.

### §J5. Landed in this pass

Hermetic, no provider calls. Suite: **1341 tests, OK**, zero pytest imports,
reachability floor intact.

| # | Change | File |
|---|---|---|
| J5-1 | `to_output_blocks` cuts at the **last** sentinel, not the first; new `FINISHED_ANCHOR_RE` (the DOTALL `FINISHED_RE` can only ever match once). Documented as a deviation applied identically to every arm. | `scripts/longgen_common.py` |
| J5-2 | `_leaf_scope_block` — a decomposed leaf is told it owns one slice and that document-level totals and end-markers are the assembler's job. Rendered immediately after the goal it qualifies; empty for single-node trees. | `pipeline/prompts.py` |
| J5-3 | `strip_interior_terminator` — assembly drops a terminator line from the tail of every slice but the last. Conservative: final line only, decoration + terminator word only. | `v3/assemble.py` |
| J5-4 | `_artifact_instruction` states the incremental/edit-first rule on the **first** attempt: build incrementally, targeted edits, "do not re-emit the whole file", retry a smaller edit on a failed match, prefer part files. `"freely edit"` removed. | `pipeline/prompts.py` |
| J5-5 | `READONLY_BASH_PATTERNS` — a denied `bash` becomes an inspection-only pattern map (`cat`/`head`/`wc`/`xxd`/`sed -n`/`json.tool`/…) with `"*": "deny"`. Opt-in per call site; **writer only** — probes stay hard-denied. Off via `KUSUDAEMON_WRITER_READONLY_BASH=0`. | `adapters/capabilities.py`, `pipeline/backends.py` |
| J5-6 | Reviewer told the artifact was written by a different AI agent, in system + triage prompts and again at the artifact label (`ARTIFACT_LABEL`). | `v1/reviewer.py` |
| J5-7 | Dashboard: `_merge_by_timestamp` dedupes file-trace against opencode-store entries by `(role, tool_name, text)`, **multiplicity-aware** so a genuine 4x repeat still shows 4x. | `dashboard/state.py` |
| J5-8 | Dashboard: main feed follows phase agents only (`isPhaseAgent`); `headerPillAgentId` keeps the writer fallback for the cosmetic pill. | `dashboard/static/app.js` |
| J5-9 | Dashboard: reasoning expanded by default; a user collapse persists via `state.thinkingOpen` + `onBeforeElUpdated`. | `dashboard/static/app.js` |
| J5-10 | Dashboard: in-flight guard on the per-agent `?since=` poll. | `dashboard/static/app.js` |

Tests: `tests/test_wave1_repairs.py` (22 hermetic tests), plus two rewritten in
`tests/test_trace_history.py`. **`test_artifact_instruction_allows_edits_preserves_finished_work`
asserted the §E1 defect as a contract** — that the first-pass instruction says
"freely edit" and carries no rewrite prohibition — and has been replaced with
its inverse.

### §J6. Known-unverified after this pass

1. **§F.5 (vacuous grounding passes) and §F.6 (punctuation normalisation) have
   never run online.** They were authored mid-run. Both have hermetic tests;
   neither has a live episode.
2. **Whether a writer obeys J5-2 or J5-4.** Same compliance assumption §E2
   flags. Prior base rate against the optional phrasing: 0/4.
3. **Whether read-only `bash` prevents the rewrite loop** or merely relocates
   it. OpenCode matches the patterns as globs; `cat x && rm y` matches `cat *`.
   J5-5 is a usability fix, not a sandbox — see the comment at
   `READONLY_BASH_PATTERNS`.
4. **`calls_by_role: {"unknown": 3}`** — §F2's phase/role stamping still misses
   the reviewer and triage calls. Three role calls in this run are
   unattributed, so `tokens_by_role` cannot separate review cost from writer
   cost.
5. **Two of four explore probes lost their findings.** `out/explore~research~unit-0[1-4].md`
   are all 0 bytes; only `unit-01` and `unit-04` left a `research/*.raw.json`.
   The phase cost 603 s. `needs_research` was already false; consider whether
   the probes should have run at all.
6. **`max_parallel: 1` recorded against `max_parallel_derived: 3`.**
   `run_longgen_bench.py` only passes `--max-parallel` when `>1`, so §K's
   measurement needs it set explicitly.

### §J7. Wave 2 — the branch, resolved

§G's rule reads: *died on transport → §F is the whole job; finished, leaves
clobbered → §E1/§E2 then re-run one seed; finished, leaves intact → skip §E,
go to Wave 3.*

The literal reading is **"finished, leaves intact → skip §E, go to Wave 3."**
Taking it literally would be a mistake, for three reasons the branch was
written before anyone could see:

- The finish was not autonomous (`unit-02` bypassed by an operator).
- The measurement was wrong in the *optimistic-looking* direction — 75% for a
  100% artifact. A three-seed sweep run through the old evaluator would have
  produced six plausible, wrong numbers.
- The two fixes most likely to change leaf behaviour (§F.5, §F.6) were never
  loaded by the process being measured.

So: **Wave 1.5 before Wave 3.** One seed, same cell
(`--tasks 100 --arms C --seeds 1 --flags KUSUDAEMON_OUTPUT_SPINE=1`), against
the code in §J5, with the driver started *after* the last source edit. It costs
one seed and answers what Wave 1 could not:

1. Does it complete with **no operator bypass**? (§J1 asterisk 2)
2. Does `completion_rate` now read 100 for a 100-floor artifact? (J5-1)
3. Do writers stop whole-file rewriting — is there a `write` over a non-empty
   artifact in any trace? (J5-4)
4. Does any leaf still emit an interior `*** finished`? (J5-2, with J5-3 as the
   backstop — check the leaf's own `out/unit-NN.md`, not the assembly)
5. Do `artifact_punctuation_normalized` events appear, and does the
   "shorter match" loop disappear? (§F.6, first online exposure)
6. Does writer token count fall from 2.66 M? A clean run should be nearer
   4 x 259 s than 705 s + three episodes.

**Then Wave 3** as §G7/§G8 specify: both arms, seeds 1–3, flags recorded,
`--max-parallel` set explicitly, and the 2026-09-08 arm-C `100-floor` rows
marked invalid in `BENCHMARKING.md` §9. Add the 2026-09-09 seed-1 row to that
invalidation list too — not because the artifact is bad (it is the best one
this harness has produced) but because it was scored by the pre-J5-1 evaluator
and was operator-assisted.

### §J8. Deferred, with reasons

- **§E2's pre-created part files.** Still unverified, still low-confidence, and
  §J1 shows §D's bound already holds without it. J5-4 makes the instruction
  mandatory, which is §E2's cheaper half. Re-evaluate after Wave 1.5 question 3.
- **Reviewer latency (§F "not fixed").** `_call_review` still ships the whole
  document every round. Untouched here; T1-1/T1-2 remain the durable fix.
- **The `(none)` contract announcement and the triply-rendered goal (§J4).**
  Suppressing the contract segment when its body is `(none)`, and trimming
  `## Complete Task Specification & Global Rules (Reference)` out of synthesized
  spine units, are both straightforward. Held back from this pass so Wave 1.5
  measures J5-2/J5-4 against one changed variable rather than four.
