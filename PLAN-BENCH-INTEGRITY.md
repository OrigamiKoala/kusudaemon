# PLAN-BENCH-INTEGRITY.md — repairs the 2026-09-06 benchmark audit found still open

Audit scope: every arm C run in `bench_results/` (HarnessBench, commit
`214e78a3`) and `bench_results/longgen/` (LongGenBench, commit `da77ebe`), plus
the surviving run directories under `~/.kusudaemon/runs/bench_longgenbench_*`.

Companion documents: `docs/PLAN-WORKSPACE-MODE.md` (the 014/057 prompt and
tool-policy losses), `PLAN-CONCURRENCY-AND-SHARED-STATE.md` (parallelism and
benchmark process), `BENCHMARKING.md` (how to run a sweep). This file covers
only what those do not, and only what is still broken at HEAD.

Nothing here is capability work. Every item is the difference between a number
that means something and a number that does not.

---

## §0. What the audit changes about the existing results

**Half the HarnessBench arm C matrix is a provider outage recorded as
capability.** Eighteen of the thirty-six arm C runs halted before doing any
work, and each was written to `bench_results/` with a numeric score that
`summarize()` then averaged:

| Task | Seeds | `halt_reason` | Recorded score |
|---|---|---|---|
| 007-session-memory | 1,2,3 | `HTTP 404 from provider: 404 page not found` | 0.0 |
| 060-task-cancellation-cleanup | 2 | `harnessbench produced no result JSON (exit 1)` | 0.0 |
| 060-task-cancellation-cleanup | 3 | `role episode failed (timeout) after 3 attempts: Episode timed out after 300s` | 0.49 |
| 061-periodic-status-rollup | 1,2,3 | same 300s role-episode timeout | 0.0 |
| 103-policy-update-replan-diff | 1,2,3 | same | 0.1 |
| 104-async-ops-window-rollup | 1,2,3 | same | 0.1 |
| 105-partial-batch-resume-ledger | 1,2,3 | same | 0.045 |
| 106-release-approval-gate-plan | 1,2,3 | `role episode failed (error) after 3 attempts:` followed by an opencode billing log line (`…/billing`) | 0.11 |

`docs/PLAN-WORKSPACE-MODE.md` §K5 found the same six tasks and correctly called
them "the largest measured effect in the whole dataset, hiding in the tasks
excluded as harness bugs" — it computes the arm aggregate as 0.748 (A) against
0.368 (C) across all eleven tasks, essentially all of the gap being these halts.
This section adds three things §K5 does not have:

- **The chronology.** Every arm C cell attempted between 08:46 and 14:00 failed,
  and the 014/057 re-runs at 14:25–15:53 — after the model was changed —
  succeeded normally. This is a bounded outage window, not a property of those
  six tasks. Re-running them should be expected to work.
- **The identity.** These runs used `opencode/nemotron-3.5-lightning-free`;
  106's `halt_reason` carries the provider's own billing log line. Free-tier
  exhaustion, not a harness result.
- **A mechanism**, so this never has to be reconstructed by hand again (§4.2).

The tell is cheap to check and worth asserting in code (§4.2): a dead run
produces byte-identical output across seeds. 061 is 1860 bytes on all three
seeds, 104 is 2313 on all three, 106 is 4365 on all three.

**The two aggregate files are not the sweep.** `bench_results/summary.json`
reports one task (106) with two runs while sixty-six per-cell JSON files sit
next to it, because `run_harness_bench.py:587` *overwrites* `records.jsonl` and
`summary.json` with only the current invocation's records and the script has no
`--resume` at all. `run_longgen_bench.py` has `--resume` but the same blind
spot at line 560. Until §4.1 lands, the per-cell JSON files are the data and
the aggregates should be regarded as scratch.

**What survives the audit:** 014 and 057 arm C (14:25–15:53), and the three
LongGenBench arm C runs. Everything else in arm C needs re-running.

---

## §1. The LongGenBench single-writer question, answered

The observation was that arm C dispatched one subagent to write all 100 block
descriptions, and that this both risks overloading the model and fails to
exercise recursive decomposition. The second half is the real problem; the
first half is not what the data shows.

### §1.1 Why it happened

**Both gates that could have produced a decomposed tree are keyed on input
size, and LongGenBench is a small-input / large-output task.**

1. **The tier gate.** `v6/tiering.py::_classify_raw` reads
   `estimate.artifacts` — the model's count of output *files* — and
   `estimate.files_touched`. One document means `artifacts=1`,
   `files_touched="1"`, so the run classifies **T1**, and
   `_PHASES_BY_TIER["T1"]` (`tiering.py:544`) has no `plan` phase. The run
   directories confirm it exactly: `explore_spine` is skipped with
   `reason: "tier has no plan phase"`, `build_single_node_tree` produces the
   single node `single`, and `calls_by_role` is one writer.

   The signals that would have caught it *were measured*. Every LongGenBench
   `tier.json` carries `output_targets: 2` and `breadth_markers: 8`.
   `_classify_raw` never reads them. The only consumer of `output_targets` is
   `_measured_small` (`tiering.py:397`), which exists to **decline** an
   escalation, is guarded by the default-off `KUSUDAEMON_TIER_TRUST_SIGNALS`,
   and can therefore only ever move a tier *down*. There is no path in the
   codebase from "the goal declares 100 entries" to a higher tier.

2. **The split gate.** `v7/split.py::_measured_overrun` is the reactive escape
   hatch — a writer may propose a split by writing `scratch/<node>/split.json`.
   It fires only when `estimate_tokens(node.inputs) > node.budget.tokens`, or
   when the previous attempt failed with a size-class defect. LongGen's
   `source.txt` is 2,489 bytes (~600 tokens) against a 50,000-token node
   budget, and the timeout path sets `last_defect = "episode did not complete"`
   (`round_loop.py:767`), which `v6/direct.py::is_size_defect` does not match.
   `v1/writer.py:166` gates the writer's split-hint path on the same input-side
   test, so the writer was never even told a split was available.

3. **The split handler is not even installed below T2.**
   `driver.py:2067` is `enable_split = tier in ("T2", "T3")`, and
   `driver.py:2106` passes `split_handler=handle_split_proposal if enable_split
   else None`. So a T1 run has no split path at all, independent of gates 1 and
   2. This is deliberate — the comment above it says the split machinery
   expects a multi-node `tree.json`, never T1's code-built single node — but it
   means the tier decision in gate 1 is *load-bearing for the entire
   decomposition mechanism*, with no reactive fallback behind it. Fixing gate 2
   without fixing gate 1 accomplishes nothing.

### §1.2 What the data says about "overloaded"

Not much, and what it does say cuts both ways:

| Run | Blocks | Writer prompt tokens | Outcome |
|---|---|---|---|
| arm C seed 1 | 99/100 (missing Block 90) | 180,921 | hit the 1800s episode box, redispatched `resumed_session`, killed by the 3600s cell cap |
| arm C seed 2 | 100/100 | 46,621 | writer `status: done` in 460s, gates pass |
| arm A seed 1 | 42/100 (blocks 1–42 emitted twice) | — | 784s |
| arm A seed 2 | 100/100 | — | 917s |
| arm A seed 3 | 10/100 | — | 1829s |

Arm C is not losing here, and the model demonstrably can produce all 100 in one
episode. Seed 1 is a variance event that then collided with the wall-clock
mechanics in §3, not evidence of a context ceiling.

### §1.3 The actual defect: construct validity

On the one benchmark selected to exercise recursive decomposition, arm C never
decomposes. What LongGenBench currently measures for arm C is *arm A plus a
four-minute classify call and an intake call*. Whatever number it produces can
neither support nor refute the thesis, and that is true whether the number is
good or bad.

This also answers the "some models can do all 100 in one go, so maybe
decomposition is unnecessary" objection: that is a hypothesis, and the right
response is to make it measurable rather than to settle it by omission.
`BENCHMARKING.md` §0.3 already names the threshold — *where decomposition starts
paying for itself* — as the most useful thing the whole benchmark exercise can
produce. Right now the harness cannot reach either side of that threshold on a
Shape B task.

### §1.4 The change

**§1.4a — Give the tier table an output-size signal.** In `_classify_raw`, an
`artifacts == 1` estimate should not by itself reach T0/T1 when the measured
output signals contradict it. Add, as a code rule ahead of the T1 row:

```python
# a single output FILE is not a single unit of WORK
if signals.output_targets > 0 and _declared_target_count(goal) >= _PLAN_MIN_OUTPUT_UNITS:
    return tier_max(_classify_raw_inner(signals, estimate), "T2")
```

`_declared_target_count` is a new helper over the existing `_NUMERIC_TARGET_RE`
match — it already captures the numeral, so extracting `100` from "consists of
100 entries" is a regex group, not a new parser. `_PLAN_MIN_OUTPUT_UNITS`
starts at 8, matching `v7/split.SPLIT_MAX_CHILDREN`.

Ship behind `KUSUDAEMON_TIER_OUTPUT_SIGNALS` (default `0`), for the same reason
every other §K flag is default-off: it must be A/B-able against the current
behavior on the same task. This is the flag that makes §1.3's threshold
measurable.

These three are ordered by leverage: gate 1 is the only one whose fix changes
behavior on its own, so §1.4a below is the item that matters and §1.4b/c are
the reactive backstop for the cases the up-front estimate misses.

**§1.4b — Let the split gate see output overrun.** `_measured_overrun` should
accept a second trigger: the node's brief declares N units and the artifact
produced so far covers fewer than N. The gate machinery to count them already
exists — the leaf carries `gates` and the benchmark's own `parse_blocks` is the
same shape as a `problems_min`-style structural gate. Concretely, add an
optional `units_expected` field to `NodeBudget` (or to the node's gate list as
`units_min:N`), set it from the same `_declared_target_count`, and treat
"episode ended with units_found < units_expected" as a size defect so
`is_size_defect` matches and the existing split path runs unchanged.

**§1.4c — Tell the writer.** `v1/writer.py:166` should offer `split.json` when
*either* the input test or §1.4b's output test holds. One-line condition
change; the rest of `handle_split_proposal` is untouched.

**Acceptance.** On LongGenBench task 300 with the flag on: the run classifies
T2 or above, `_phase_plan` runs, `tree.json` has between 2 and 8 nodes, and
`records.jsonl` shows `calls_by_role.writer >= 2`. With the flag off, the run
is byte-for-byte the current behavior. Then run both and record the completion
rate and token cost of each — that comparison *is* the deliverable, not the
higher number.

---

## §2. False zeros — the highest-value repairs

These convert completed work into recorded failures. They matter more than
anything in §1 because they corrupt results the sweep already has.

### §2.1 A transient error after a passing artifact zeroes the run

LongGenBench arm C seed 2, in order, from `events.jsonl`:

```
episode_completed  node=single  status=done  duration_ms=460639
   → manifest.jsonl: gates "pass", unmet_gates [], out/single.md = 100/100 blocks
phase_auto_resuming  phase=execute  attempt=1  error="The read operation timed out"
phase_failed         phase=execute             error="The read operation timed out"
```

Recorded outcome: `score 0.0`, `resolved false`, `artifact_path null`. A
complete, gate-passing 100/100 document was thrown away by a post-writer socket
timeout.

**Cause.** `pipeline/cli.py:1191-1205` learns `artifact_path` *only* from a
`run_completed` event, and `cli.py:1161` scores binary on
`report.status == "done"`. Neither consults `manifest.jsonl`, which already
records every passed node's artifact path and gate result at the moment it
passed.

**Fix.** When the driver returns a non-`done` report, fall back to the
manifest: read `manifest.jsonl`, take the passed nodes, and populate
`artifact_path` (and, for Shape B, the exported document) from it. Add a
`partial: true` field and a `nodes_passed` / `nodes_total` pair to the record
so the caller can tell a salvaged run from a clean one. Do **not** silently
promote `score` to 1.0 — a Shape B harness computes its own metric from the
artifact, and a Shape A oracle grades the workspace; both need the artifact,
neither needs a fabricated pass.

**Regression test.** `tests/test_bench_cli.py`: drive `cmd_bench` with a
driver stub that writes a passing manifest and then returns
`RunReport(status="error", ...)`; assert `artifact_path` is populated and
`partial` is `True`.

### §2.2 Backend children outlive the run and contaminate the next one

`adapters/_agent_worker.py:974` calls `create_subprocess_exec` without
`start_new_session=True`, and every kill path (`:910`, `:937`, `:952`, `:1008`)
targets `proc` alone. When `run_longgen_bench.py` kills `kusudaemon bench` on
its `--timeout-sec` expiry, the worker and the `opencode` CLI beneath it
survive.

Measured: arm C seed 1 was killed at 21:20 and harvested at 21:18 with 91,874
characters; `out/single.md` was **95,174 bytes at 21:55** — still growing 35
minutes after the kill, and 15 minutes into the seed 3 run. The cost is visible
in the next cells' numbers: classify wall clock across the three seeds went
194s → 230s → 320s as the orphan competed for the same rate limit.

**Still live at the time of writing.** That same `out/single.md` was 95,174
bytes at 21:55 and 96,306 bytes at 22:16 — an hour after the run was killed,
the orphan is still generating, still spending the free tier, and still
competing with the seed 3 run that is in flight. Anyone reproducing this should
check for stray `opencode` processes before trusting a wall-clock number.

**Fix, three parts:**

1. `start_new_session=True` on the `create_subprocess_exec` call, so the worker
   and its children form one process group.
2. Every kill path escalates to `os.killpg(os.getpgid(proc.pid), SIGTERM)` then
   `SIGKILL` after the existing 1.5s grace.
3. `run_longgen_bench.py` / `run_harness_bench.py` launch `kusudaemon bench`
   with `start_new_session=True` and, on `TimeoutExpired`, `killpg` the group
   before reading anything back. `subprocess.run(timeout=)` kills only the
   direct child; that is why the orphan survived.

**Regression test.** Spawn a worker whose command is a shell that backgrounds a
`sleep`, kill it through the adapter's timeout path, assert the grandchild is
gone within 5s.

### §2.3 `tier_measured` / `tier_final` are always `None`

`pipeline/cli.py:1170` reads `t_data.get("measured")` and
`t_data.get("final") or t_data.get("effective")`. `pipeline/driver.py:1077`
and `:1113` write `measured_tier` and `tier`. The keys have never matched.

Every arm C record in `bench_results/` carries `tier_measured: null`, including
runs whose `tier.json` says `measured_tier: "T1"`, and
`run_harness_bench.py:313` propagates the null into the sweep record.
`BENCHMARKING.md` §0.4 advertises these fields as part of the record schema, and
`BENCHMARKING.md` §0.3 names `v6/tiering.py`'s boundaries as the thing the whole
exercise is meant to calibrate — with no tier recorded, that calibration has
never been possible.

**Fix.** Read `measured_tier` and `tier`, keeping the old keys as fallbacks for
records written by older builds. One line each. Add an assertion to
`tests/test_bench_cli.py` that a run whose `tier.json` says `measured_tier: T1`
produces `tier_measured == "T1"`.

### §2.4 An episode timeout is reported as a gate failure

`v1/round_loop.py:767-780` emits `node_gate_failed` with `unmet: []` and sets
`last_defect = "episode did not complete"` when the episode itself timed out.
Three consequences, all observed on LongGen seed 1:

- The event log says a gate failed when none did — and the same node's
  `manifest.jsonl` line simultaneously says `gates: "pass"`.
- `is_size_defect("episode did not complete")` is False, so a node that timed
  out *because it was too big* cannot trigger the split that exists for exactly
  that case.
- The node burns an attempt and is redispatched (§3.1).

**Fix.** Give the timeout its own outcome and event — `node_episode_timeout`,
with `unmet` omitted — and set `last_defect` to a size-class string so
`is_size_defect` matches, since an episode that ran out of wall clock with a
partial artifact is the canonical size defect. This is the same shape as
`PLAN-CONCURRENCY-AND-SHARED-STATE.md` §B7.1's `node_throttled` outcome; build
both against one refactor of the outcome enum.

---

## §3. Wall-clock and budget mechanics

### §3.1 The pipeline's own budget can exceed the benchmark's cell cap

`driver.py:147-172` maps a node's token budget to an episode duration: 50,000
tokens → 1800s, floored at 300s and capped at 7200s. `RunOptions.max_attempts`
defaults to 3. So one node can legitimately consume 5400s of wall clock, before
classify, intake, review or assemble.

`run_longgen_bench.py --timeout-sec` defaults to **3600**. Seed 1 hit
1800 + 1800 = 3600 exactly and was killed mid-second-attempt. Any node that
exhausts its first episode box on that benchmark is *guaranteed* to blow the
cell cap.

Seed 3 reproduced it while this document was being written: dispatched at
ts 1788730821, a second `session_captured` at ts 1788732622 — 1801s later —
so it is on its second 1800s attempt and will be killed at 3600s exactly as
seed 1 was. Two of three arm C seeds on this task are lost to a mechanical
interaction between two independently reasonable defaults.

**Fix.** Pass the cell cap into the run (`--wall-clock-budget`, or reuse
`--budget-tokens`'s plumbing) and have `_budget_seconds` clamp against the
remaining run budget rather than only against `_MAX_EPISODE_SECONDS`. A node
that cannot finish inside the remaining budget should halt with a recorded
reason, which is a result, rather than be killed from outside, which is not.
Also raise the LongGenBench default `--timeout-sec` to 5400 so the current
defaults are at least self-consistent, and document the relationship in
`BENCHMARKING.md`.

### §3.2 `classify` is the pipeline's most expensive and most fragile call

Measured across the three LongGenBench arm C runs:

| Run | prompt tokens | completion tokens | wall clock |
|---|---|---|---|
| seed 1 | 1,264 | 7,297 | 194s |
| seed 2 | 1,264 | 9,460 | 230s |
| seed 3 | 1,264 | 12,091 | 320s |

For one small JSON object. `grep max_tokens` finds no cap on any role call in
`v1/provider.py` or `roles/`, so the model preambles freely and
`extract_last_json_object` cleans up afterwards. On seed 2 that call was **half
the run's wall clock**.

Under HarnessBench this is not merely expensive, it is the failure. `hb_adapter.sh:47`
sets `KUSUDAEMON_ROLE_TIMEOUT=300`; a call that takes 194–320s when healthy is
a coin flip against a 300s box, and 3 attempts × 300s is precisely the 900s
wall clock on every failed 061/104 run.

**Fix.** Set `max_tokens` on schema-constrained role calls, sized from the
schema (a few hundred for `ESTIMATE_SCHEMA`, generous for writer-adjacent
roles), overridable by `KUSUDAEMON_ROLE_MAX_TOKENS`. Then raise
`KUSUDAEMON_ROLE_TIMEOUT` in `hb_adapter.sh` to 600 so a slow-but-healthy call
is not scored as a failure. Record classify tokens in the sweep summary so the
regression is visible next time.

### §3.3 The classify halt is a deliberate trade, decided for the wrong caller

`driver.py:1018-1053`: below T2 a failed scope estimate logs
`scope_estimate_degraded` and **continues** with a default estimate. At or
above T2 it retries once, and on failure constructs a complete T2 fallback
payload, writes `tier.json`, logs `scope_estimate_unavailable` — and then
raises `RuntimeError`.

This is not an oversight. It is `docs/PLAN-WORKSPACE-MODE.md` §K5 as corrected
by §R3, implemented exactly as specified, and §R3's reasoning is sound on its
own terms: falling back also discards `question_set`, so a six-hour corpus run
would silently skip ambiguity resolution and discover the misunderstanding at
assembly. "A long run deserves a loud early failure; it is cheap to restart at
minute one and expensive at hour six," and `tier.json` is written first so
`resume` does not re-pay the classify call.

**The trade assumes an operator who resumes.** A benchmark run has none. It is
killed, scored zero, and the sweep moves to the next cell — so on the unattended
path §R3's "loud early failure" is indistinguishable from a silent one, and the
`tier.json` written for the resume that never happens is pure ceremony. Three
LongGenBench runs died 0.8s in on a provider 404 this way, and 007's three arm C
runs died the same way on HarnessBench.

**Fix.** Gate the raise on attendance rather than on tier alone. `cmd_bench`
already computes exactly the right signal — `attended = bool(getattr(argv,
"attended", False))`, `cli.py:1127` — and already passes an `Approver` on the
unattended path for precisely this class of reason ("a benchmark run is
unattended by construction"). Thread it into `RunOptions` and, when the run is
unattended, take the §R3 sub-T2 branch at every tier: degrade, log
`scope_estimate_degraded`, record `tier_degraded: true` on the run so the
result is legible as "classified without an estimate", and continue. Attended
runs keep §R3's behavior unchanged.

This is a scoping change to a landed decision, not a reversal of it. Both
authors of that decision were right about their own case.

### §3.4 Phase retry budget is mis-sized against the socket timeout

`driver.py:432-434`: `_PHASE_TRANSIENT_MAX_ATTEMPTS = 2`,
`_PHASE_TRANSIENT_BASE_DELAY = 1.0`. On LongGen seed 2 both attempts were spent
inside 182s, because each one waited out a ~90s socket read timeout before the
1s backoff even applied. A provider hiccup that lasts three minutes defeats it.

**Fix.** Raise to 4 attempts and make the backoff start after the *observed*
call duration rather than at a flat 1s. With §2.1 in place this stops being
load-bearing, so treat it as the cheap second layer, not the primary fix.

---

## §4. Harness and accounting repairs

### §4.1 Both scripts drop completed cells from their aggregates

- `run_harness_bench.py:587-591` writes `records.jsonl` with `write_text`
  (overwrite) from the in-process list only, and the script has no `--resume`.
  Re-running any subset silently discards every earlier cell.
- `run_longgen_bench.py:560-561` has `--resume`, but `summarize()` and
  `write_predictions()` receive only the records this process produced.
  Observed: `predictions_armC_seed2.json` was never written (the predictions
  directory is stamped 21:22; seed 2 finished at 21:34), and `summary.json`
  (21:18) predates the last two records entirely.

**Fix.** Both scripts should rebuild their record list from disk before
aggregating — glob the per-cell JSON files (HarnessBench) or read back
`records.jsonl` and de-duplicate on `(task_id, arm, seed)` keeping the newest
(LongGenBench) — then summarize over the union. Append to `records.jsonl`,
never overwrite. Add `--resume` to `run_harness_bench.py`.

### §4.2 Quarantine transport failures instead of scoring them

The rule already exists in prose ("quarantine runs whose halt_reason names a
transport error rather than scoring them") and is enforced nowhere.

**Fix.** Add `classify_halt(halt_reason) -> "ok" | "transport" | "budget" |
"agent"` to a shared module both scripts import. A record gains
`"valid": false, "invalid_reason": "transport"` when the halt names an HTTP
status, a provider 404/429/5xx, a role-episode timeout, an auth or billing
message, or a harness-side timeout. `summarize()` excludes invalid records from
`mean_score`, `resolve_rate` and the token aggregates, and reports them in a
separate `excluded` block with counts by reason. A summary whose `excluded`
count exceeds ~20% of an arm should print a loud banner — that arm is not a
result.

Add the identical-output check as a second, independent signal: within a
(task, arm) group, if every seed's artifact has the same byte length, emit
`suspect_identical_seeds` in the summary. That alone would have flagged 061,
104 and 106 at the moment they were written.

### §4.3 `harvest_artifact` can capture another run's in-flight output

`run_longgen_bench.py:186-194` globs
`<runs_root>/*{task_id}*arm{arm}*s{seed}*/out/*.md` and takes the first
non-empty match. Combined with §2.2, a cell killed on timeout is harvested from
a file an orphaned writer is still appending to — which is exactly what
happened to arm C seed 1 (91,874 chars harvested at 21:18 from a file that was
95,174 bytes at 21:55).

**Fix.** Restrict the glob to the run id this cell actually launched (the
script can pass `--run-id` the way `hb_adapter.sh` already does, instead of
letting `cmd_bench` synthesize one), and only harvest after confirming the
process group is dead (§2.2 part 3).

### §4.4 The timeout path races the record write

`run_longgen_bench.run_one` reads `record_path` immediately after
`TimeoutExpired`. Arm C seed 1's line in `records.jsonl` has `commit: null`,
`calls_by_role: {}` and `halt_reason: "timeout after 3600s"`, while the record
file on disk carries a commit and a full role breakdown.

**Fix.** After killing the process group, wait up to ~5s for `record_path` to
appear or change mtime before reading it.

Separately: `bench_results/longgen/bench/300-block_armC_seed1.json` has the
**arm A** field shape (`session_id` and `round` present, `attended` absent,
`tier_measured` populated) which `cmd_bench`'s arm C branch at
`cli.py:1207-1229` cannot produce. It did not come from this build. Delete it
and re-run the cell rather than scoring it.

### §4.5 `mean_completion_rate` mixes units

`longgen_common.calculate_completion_rate` returns a **percentage** (0–100).
`run_longgen_bench.summarize` labels the mean `mean_completion_rate` and then
computes `tokens_per_completion_point = total_tokens / rate` against it. The
current `summary.json` reads `mean_completion_rate: 1.0` for a set whose only
record scored 99.

**Fix.** Rename to `mean_completion_pct`, or divide by 100 at the boundary.
Pick one and make `eval_longgen_free.py` agree.

### §4.6 Arm A token capture (unchanged from `PLAN-CONCURRENCY` §A4)

Every arm A record still has `tokens_by_role: {}`; `cli.py:991-996` fills it
only when `_parse_opencode_usage` finds usage in the captured stream.
`BENCHMARKING.md` §0.1's ±10% token-parity precondition has never been checkable, so
no arm delta published so far is defensible under the document's own rule.

Still the first item in `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §C, and still
open.

### §4.7 `mcp_server_overrides` fails silently on 3.10

`adapters/codex.py:172-180` returns `[]` when neither `tomllib` nor `tomli`
imports. `docs/TEST-PLAN.md` §0.1's structural fix correctly made the driver import
survive a missing `tomli`, but on the 3.10 leg it also made every configured
Codex MCP server disappear without a word. This is the suite's one remaining
failure — `test_backends_claude_codex.CodexAdapterTest.test_mcp_server_overrides`,
in an otherwise green 1189-test run.

**Fix.** Log a one-time warning naming the missing dependency and the dropped
servers, and install the `dev` extra (which already carries `tomli`) on the
3.10 CI leg.

---

## §5. The flag decision cannot be deferred any longer

`KUSUDAEMON_TIER_TRUST_SIGNALS`, `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE`,
`KUSUDAEMON_DIRECT_TEMPLATE`, `KUSUDAEMON_DIRECT_TOOLS` and
`KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT` all default to `"0"`, and **no benchmark
entry point sets any of them** — not `hb_adapter.sh`, not
`run_harness_bench.py`, not `run_longgen_bench.py`.

So the 014 and 057 arm C re-runs on 2026-09-06 at 14:25–15:53, the only clean
late arm C data in the sweep, measured the *unfixed* path. Every diagnosis in
`docs/PLAN-WORKSPACE-MODE.md` remains unrefuted and unconfirmed, and 014 arm C still
loses to arm A (0.879 / 0.779 / 0.429 against 0.910 / 0.829 / 0.879).

This is `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §A5. The objection recorded
there — that freezing a configuration measures one arbitrary point in a
32-point space — is real, but the status quo measures the one point known to be
wrong. Minimum viable resolution: add a `--flags` pass-through to both
benchmark scripts, record the resolved flag set in every record, and run the
§K7 A/B ladder in `docs/PLAN-WORKSPACE-MODE.md:761-767` at one seed on 014 and 057
before any further sweep.

---

## §7. Manual judge pass — instruction-following accuracy, all 6 seeds (2026-09-06)

`eval_judge_seed2_seed3.log` only ever scored 2 of 6 files (armC seed2/seed3,
against `nvidia/nemotron-3.5-lightning-30b-a3b`) and reported `accuracy: 0.000`
across every category for both. That number is wrong on its face — several of
the checked blocks explicitly open with "This block is the main university
campus" for a check asking "does this include the university?" — so either
the endpoint call was silently failing/echoing and `Judge.ask` was scoring a
non-answer as "no", or the model itself is unreliable for this yes/no format.
Either way `eval_armC_seed2_seed3.json` should not be trusted and the harness
needs a smoke check on judge responses (e.g. assert the raw completion is
literally "yes" or "no" before counting it) before this judge model is used
again.

In place of the broken endpoint, this pass emulates the judge by hand: for
each of the 6 prediction files, `create_prompts()`'s exact selection logic
(`scripts/eval_longgen_free.py:58-80`, `scripts/longgen_common.py:39-49`) was
re-run to find which `checks_once` / `checks_range` / `checks_periodic`
entries have a matching output block, then each matched (event description,
block text) pair was read and answered "yes"/"no" as the judge prompt asks.
Full per-check verdicts are in `bench_results/longgen/eval_manual_judge_all6.json`.

| file | completion% | acc_once | acc_range | acc_period | acc_avg | evaluated (o/r/p) |
|---|---:|---:|---:|---:|---:|---|
| armA_seed1 | 42.0 | 0.500 | 0.000 | 0.000 | 0.167 | 2/0/0 |
| armA_seed2 | 100.0 | 0.000 | 0.000 | 0.000 | 0.000 | 5/4/5 |
| armA_seed3 | 10.0 | 0.000 | 0.000 | 0.000 | 0.000 | 0/0/0 |
| armC_seed1 | 99.0 | 1.000 | 1.000 | 0.600 | 0.867 | 5/4/5 |
| armC_seed2 | 100.0 | 1.000 | 0.750 | 0.200 | 0.650 | 5/4/5 |
| armC_seed3 | 100.0 | 0.200 | 0.000 | 0.200 | 0.133 | 5/4/5 |

Arm means: **A** completion 50.67% / instruction-accuracy **0.056**; **C**
completion 99.67% / instruction-accuracy **0.550**. Two findings that change
what the completion-rate table in §0/summary.json is allowed to claim:

1. **armA_seed2 games the completion metric.** It emits all 100 numbered
   `Block N` headers (100% completion) but the body text is the same rotating
   paragraph of generic urban-development boilerplate ("sustainability
   goals... community outreach... public art installations...") reused under
   every heading regardless of what that block is supposed to be. All 14
   evaluable checks answer "no" — zero of them actually describe a university,
   library, sports complex, museum, shopping district, or bus station. This is
   the exact failure mode `eval_longgen_free.py`'s own docstring warns about
   ("a model that emits 3 of 52 blocks can post a high accuracy") but inverted:
   here full completion coexists with zero content fidelity. Completion rate
   alone is not a valid proxy for arm A quality on this seed; any report that
   cites armA_seed2's 100% completion without also citing 0% instruction
   accuracy is misleading.
2. **armC_seed3 drops the per-block category constraint as the document goes
   on, distinct from seed1/2's strong result.** The dataset fixes which block
   number must be which category (block 95 = university, 80 = library, 36 =
   sports complex, 51 = museum) — that assignment is not the model's to
   invent — and seed3 stops honoring it for 4 of 5 "once" checks, instead
   filling those slots with generic transit/civic filler: block 95 becomes
   "a periodic bus station stop along the city's north-south transit
   corridor," block 80 becomes "a rooftop terrace and event space," block 36
   becomes "a bus stop shelter," block 51 becomes another periodic bus-station
   stop. Only `once/34` (library) is honored correctly. The clearest single
   piece of evidence is block 51, which the dataset requires to satisfy *two*
   checks on the same block — it's both the "once" museum and the first node
   of the "periodic" bus-station series, and seed1/seed2 both write one
   integrated paragraph doing both jobs (seed2: "the city's main museum...
   with the added feature of an integrated bus station entrance"). Seed3
   writes block 51 as pure transit-stop content and drops the museum
   requirement outright, so it passes `periodic/51` but fails `once/51` on
   the identical block. The same drift shows in the "range" checks (92-95,
   all should be shopping district) — seed3 gives a parking garage, a
   municipal clerk's office, a wellness fair venue, and (block 95 again) a bus
   station, none mentioning a shopping district — and in the rest of the
   periodic series, where 53/55/57/59 turn into a sports stadium, a driving
   range, a tennis club, and a boxing arena instead of bus stations. (An
   earlier pass on this section attributed the defect to a `(row, col)`
   coordinate-annotation swap seen in the model's own parenthetical asides,
   e.g. `Block 95 (9, 4)` here vs `Block 95 (4, 9)` in seed1/2 — that
   coordinate is decorative flavor text, not causal: block 95's own seed3
   coordinates don't even land on the periodic axis, yet it still got
   bus-station content, so the swap doesn't explain the miscategorization.)
   This drags armC_seed3's instruction-accuracy (0.133) far below armC_seed1
   (0.867) and armC_seed2 (0.650) despite identical 99-100% completion, and
   the cause is content drift in that run, not a difference in arm C's
   mechanism.

armA_seed1 and armA_seed3's accuracy numbers (0.167 and 0.000) are near-
meaningless on their own — 2 and 0 evaluable checks out of 14 respectively,
because completion is only 42% and 10% — and should always be read next to
the evaluated-count column, per the upstream semantics `create_prompts()`
already documents.

Net: arm A's instruction-following accuracy (0.056 mean) is not merely lower
than arm C's (0.550 mean) — one of its three seeds is actively gaming the
completion metric with repeated boilerplate, which the completion-rate-only
view in §0/summary.json cannot see. Any writeup of this sweep must report
both metrics side by side, not completion rate alone, and armC_seed3's defect
should be root-caused (likely in whichever pass builds `output_blocks`/the
per-block content generation for that seed) before treating 99.67% as three
comparable data points.

---

## §6. Order

| # | Item | Why first |
|---|---|---|
| 1 | §2.2 process-group kill | Everything measured after a timeout is contaminated until this lands. |
| 2 | §2.1 manifest salvage | Converts an existing false zero into a real result. |
| 3 | §2.3 tier key mismatch, §4.5 unit mix | One-line fixes to fields the docs already promise. |
| 4 | §4.1 + §4.2 aggregate rebuild and quarantine | Makes the existing sixty-six records readable without re-running anything. |
| 5 | §3.2 role `max_tokens` + timeout, §3.3 unattended classify fallback | Removes the single point of failure that cost 18 runs. |
| 6 | §2.4 timeout outcome, §3.1 budget clamp | Wall-clock correctness; §2.4 is also the prerequisite for §1.4b. |
| 7 | §5 flag decision, then re-run 007/060/061/103/104/105/106 arm C | The re-run is only worth its wall clock once 1–6 hold. |
| 8 | §1.4 output-size decomposition, behind its flag | The actual thesis experiment. Do it last, on a harness that can record the answer. |
| 9 | §4.6 arm A tokens, §4.7 tomli warning | Independent; land whenever. |

Items 1–6 are all small and none of them require a model call to verify.
