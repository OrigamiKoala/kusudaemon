# PLAN-WORKSPACE-MODE.md

Fixing the workspace-mode regressions found in the 2026-09-06 HarnessBench
arm A / arm C comparison (`bench_results/`, kusudaemon `214e78a3`, arm A
`fbf29a14`).

Companion to `PLAN-REVIEW-LATENCY.md`. Same conventions: every change ships
behind the §III.5 rule — **default-off, measure, then flip** — unless it is
a provable no-op or a pure waste removal.

---

## §0. Read this before changing anything: the current numbers cannot arbitrate these fixes

Scores over the four uncontaminated tasks (007, 060, 061, 103–106 excluded
per the operator; 057 seed3 re-ran at 15:53 and is included at its new value):

| task | arm A | arm C | nominal delta |
|---|---|---|---|
| 014-task-decomposition | 0.873 | 0.695 | −0.178 |
| 057-interruption-resume | 0.897 | 0.821 | −0.076 |
| 058-multiday-project-state | 0.594 | 0.828 | **+0.234** |
| 059-event-update-replan | 0.455 | 0.909 | **+0.454** |

Both apparent wins dissolve on inspection, and this governs the whole plan:

**058 is one arm-A infrastructure failure.** Per-seed, arm A is
`[0.156, 0.781, 0.844]` and arm C is `[0.859, 0.781, 0.844]`. Seeds 2 and 3
are *identical*. Seed 1's `halt_reason` reads
`round 2: bare process exited with code 1 … {"message":"Streaming response
failed: [504] Upstream idle timeout exceeded"}` and the same again for round 3.
Two of three rounds never ran. The +0.234 is a provider outage, not a harness
capability.

**059 is one JSON key.** `tasks/059-event-update-replan/oracle_grade.py`
reads `revised.get("blocks")` and keys each entry by `b.get("id")`. Arm C
emitted `{"blocks": [{"id": "setup", …}]}`; arm A emitted
`{"plan": [{"block": "setup", …}]}`. Arm A's plan is *substantively fine* —
its rehearsal ends 10:45, inside the 11:00 constraint — but `blocks` resolves
empty, so `required_blocks`, `room_constraint`, `rehearsal_time` and
`accessibility_check` all fail together. Four checks, 0.55 of the weight, one
container key.

**Consequences that constrain every change below:**

1. There is **no measured harness mechanism** that these fixes could
   degrade. Before assuming otherwise, note what was verified directly: in
   059 arm C seed 1, `out/original_plan.json` was written at 07:36:05 and
   the gate failed at 07:36:24; `out/revised_plan.json` at 07:40:26 and
   `diff.md` at 07:40:40, with the gate failing at 07:41:19. **Every scored
   artifact predates the gate failure in both rounds.** The redispatch wrote
   only `out/single.md` into the run directory. The gate-and-redispatch loop
   contributes nothing to the wins.
2. The one thing that *does* separate the arms on 059 is **output schema
   choice**, which is downstream of prompt wording. So §K3 (prompt edit) is
   the single genuinely risky change here — not because it removes a
   mechanism, but because it re-rolls a coin that currently lands well.
3. n=3 with per-check coin flips cannot resolve a ±0.08 effect. §M1 is not
   optional cleanup; it is a precondition for believing any A/B below.

---

## §K0. Every benchmark task was misclassified T2, so nothing the harness does ever ran

**This subsumes §K1–§K3 in importance.** Measured across all 24 arm C runs on
the four uncontaminated tasks:

| | value, every run |
|---|---|
| tree nodes | **1** |
| `node.judgment` | **`()`** |
| `audit/single.json` items | **0** |
| `audit/document_review.json` items | **0** |
| work object | **0–236 tokens**, 0–3 files |

Zero decomposition, zero judgment, zero review items, zero cross-leaf checks.
Arm C was `opencode` + a prompt wrapper + a restricted tool policy + a wasted
probe. Every mechanism this harness exists for was inert in every run.

**Why T2.** `v6/tiering.py:375`:

```python
if estimate.files_touched == "unknown" and _TIER_RANK[tier] < _TIER_RANK["T2"]:
    return "T2"
```

`estimate` is the model-produced scope estimate, and it returned
`files_touched: "unknown"` on every run — so a 145-token, 3-file work object
was forced to T2 regardless of what `Signals` had already measured by code.
`work_tokens` (145/188/236) and `work_files` (3) sit right there in the same
`tier.json`, unconsulted.

That single line is what turns a T1 task into a T2 one, which is what pulls
in the plan phase, which is what applies `_PROSE` (§K2), which is what denies
bash and adds `headers:std`. 059 seed1 round 1 is the natural control: it
classified **T1**, skipped the plan phase, kept `tools=()` and full
`DEFAULT_TOOL_ALLOWLIST`, carried no `headers:std` — and scored fine.

**Change.** A model estimate of `"unknown"` must not outrank a code-side
measurement. Guard the escalation:

```python
if (estimate.files_touched == "unknown"
        and signals.work_tokens < _T1_WORK_TOKENS_CEILING   # propose 2_000
        and signals.work_files <= _T1_WORK_FILES_CEILING):  # propose 8
    pass  # measured small: trust Signals, do not force T2
```

Log `tier_escalation_declined` with the measured counts so the decision is
auditable.

**Risk to 058/059: MEDIUM.** It moves them from T2 to T1, which drops the
plan phase, the explore probe, the assemble phase and `_PROSE` in one step —
several §K changes at once. That is why it ships first and alone: with §K0
in place, §K1/§K2/§K3 only ever apply to work that is genuinely T2+, which
is the population they were written for.

**Bearing on the benchmark itself.** HarnessBench's median fixture directory
is 2 KB; its largest non-image task is 12 KB. One leaf's default budget is
50,000 tokens (~200 KB). **No HarnessBench task can reach T2 on measured
size** — only via this `"unknown"` escalation. HarnessBench is a fair test of
tier classification and of the T0/T1 direct path; it cannot test
decomposition, reassembly, or cross-leaf review. The corpus-scale claims in
`README.md` live or die on the external suites in `BENCHMARKING.md`
(LongGenBench, HelloBench, WritingBench, SWE-bench Verified,
Terminal-Bench), and none of those has been run yet.

**Tests.** `tests/test_tiering.py`: a small measured work object with
`files_touched="unknown"` stays T0/T1; a large one still escalates to T2; the
decline is logged.

---

## §K1. `is_empty_workspace` is a false positive on every single-unit workspace

**Symptom.** Every arm C run on 014, 057, 058 and 059 logs
`single_node_tree_fallback` with detail `"empty workspace or empty spine"`.
The planner never runs. Arm C is bare opencode plus overhead on all four
tasks.

**Root cause.** `pipeline/driver.py:1517-1520`:

```python
is_empty_workspace = (
    work_obj.kind == "workspace"
    and (not units or (len(units) == 1 and (not units[0].members or units[0].start_chunk == -1)))
)
```

`v6/work_object.py:survey_workspace` sets `start_chunk=end_chunk=-1` on
**every** unit it returns — its own docstring calls −1 "a sentinel precisely
because it can never be a valid chunk index." The predicate reads that
sentinel as emptiness. Any workspace surveying to exactly one unit therefore
skips planning.

057's `spine.json` is the proof: one unit, 188 tokens, members
`["in/case_queue.json", "in/interruption_notice.json",
"in/operator_patch.json"]` — three real input files, classified as an empty
workspace.

(014 *is* genuinely empty — `subtasks/` and `out/` start bare, so
`survey_workspace` returns its synthetic `(workspace root)` unit with
`members=()`. The fallback is correct there.)

**Change.** Extract the predicate and drop the sentinel clause:

```python
# v6/work_object.py
def is_empty_workspace_spine(units: list[SpineUnit]) -> bool:
    """True only for survey_workspace's synthetic no-files unit. Never
    keys off start_chunk: -1 is that function's sentinel on EVERY unit."""
    return not units or (len(units) == 1 and not units[0].members)
```

`pipeline/driver.py::_phase_plan` calls it; `_phase_explore` calls it too
(see §K4a).

**Risk to 058/059: HIGH, and it is the only change that alters what those
tasks do.** Both currently reach the writer through the fallback; after this
they reach it through `v2/planner.py::build_tree` for the first time. At T2
`depth_cap=1` and the work objects are ~190 tokens over 3 files, so a
one-leaf tree is the likely outcome — but "likely" is not measured, and a
planner call on a 3-file workspace could also emit 2–3 leaves whose briefs
each carry only part of the goal. For 059 that would split the four
constraints across leaves that cannot see each other, which is exactly how
`accessibility_check` (a cross-block ordering constraint) gets lost.

**Mitigation.**
- Ship behind `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE` (default `0`).
- Add a floor: when the corrected predicate says "plannable" but
  `work_obj.est_tokens < PLAN_MIN_WORKSPACE_TOKENS` (propose 2_000), keep
  the single-node path and log `single_node_tree_small_workspace` with the
  measured token count. This keeps 057/058/059 on today's path by
  measurement rather than by accident, and lets the fix take effect where it
  was actually meant to — real repos and note folders.
- A/B on 058 and 059 at n≥8 before flipping the default.

**Tests.** `tests/test_driver_phases.py`: a workspace with one populated
unit is plannable; a workspace with the synthetic empty unit is not; the
small-workspace floor holds the single-node path. Assert on the *logged
event type*, not just the tree shape, so a regression names itself.

---

## §K2. The code-built fallback node inherits a prose-document template

**Symptom.** The fallback node ships
`gates=["nonempty", "max_tokens:50000", "headers:std"]` and
`tools=["read", "save"]`, so (a) `headers:std` fails whenever the writer
correctly does workspace work, costing an extra episode, and (b) **bash is
denied**: `adapters/capabilities.py::translate_tools_to_opencode_permissions`
maps `("read","save")` to allow read/glob/grep/edit/write and emits
`"bash": "deny"`. Arm A has bash. The 014 seed3 trace shows two `invalid`
tool uses and zero bash calls.

**Root cause.** `v6/direct.py::build_direct_node` never sets `shape`, and
`v1/tree.py:98` defaults `TaskNode.shape = "prose-dominant"`. The driver
then calls `merge_template_into_tree` at `pipeline/driver.py:1587`, which
resolves `template_for("prose-dominant")` → `_PROSE`
(`v6/templates.py:170-177`: `gates=("headers:std",)`, `tools=("read","save")`).

A default field value silently opts a code-built node into a document
template it was never planned as. `v6/templates.py` already has the right
template for this node — `_DIRECT` (line 202), `gates=()`, `tools=()`,
built for exactly "T0/T1 short-horizon nodes" — but its `shapes=()` means
`template_for` can never select it. 059 seed1 round 1 ran at T1, skipped the
plan phase entirely, and correctly came out with `gates=('nonempty',
'max_tokens:50000')` and `tools=()`. Only the T2 fallback is affected.

**Change.**
1. `build_direct_node(...)` sets `shape="direct"`.
2. `_DIRECT` gets `shapes=("direct",)` so `template_for` selects it.
3. `_DIRECT.judgment` stays `("on_topic", "claims_supported")` — those are
   cheap and `review_sample_rate=0.05` keeps them mostly unsampled — but
   verify against the reviewer-precision benchmark before merge, and drop to
   `judgment=()` if it adds reviewer calls on the fallback path.

`apply_template_to_node` unions gates, so the node keeps its own `nonempty`
and `max_tokens` and simply never acquires `headers:std`. Its tools branch is
`if not node.tools and template.tools: node.tools = list(template.tools)` —
with `_DIRECT.tools=()` the node stays tool-less and
`pipeline/backends.py:183` falls through to
`DEFAULT_TOOL_ALLOWLIST = ("shell", "read", "save", "patch")`. Bash returns.

**Risk to 058/059: LOW for the gate half, MEDIUM for the tools half.**
- Removing `headers:std` removes the redispatch. Verified above: the
  redispatch never touched a scored file in 059 either round. Pure savings.
- Restoring bash is a real capability change. It should help — 057's
  pre-rerun seed3 got C-104 = 9 (3×3) and C-105 = 10 (5×2), i.e. it never
  applied patches P-1/P-2 to the arithmetic while its own `patch_audit`
  claimed it had; that is the error a five-line script does not make. But
  more capability is more variance, and a model with bash may rewrite files
  it would otherwise leave alone. 058's `preserve_setup`-style checks reward
  leaving things alone.
- Mitigation: land the gate half and the tools half as **two commits behind
  two flags** (`KUSUDAEMON_DIRECT_TEMPLATE`,
  `KUSUDAEMON_DIRECT_TOOLS`) so the A/B attributes each effect separately.
  The gate half can flip on the strength of the mtime evidence; the tools
  half needs its own n≥8 run.

**Tests.** `tests/test_templates.py`: `build_direct_node` resolves to
`_DIRECT`, not `_PROSE`; the node carries no `headers:std`; `node.tools`
stays empty; a planner-built `prose-dominant` leaf still resolves to
`_PROSE` (guard against over-broad matching).

---

## §K3. The writer prompt tells the model the scored files do not count

**Symptom.** 014 arm C seed3 wrote four subtask files, left every one at
`## STATUS: pending`, spent the tail of its episode on "let me verify the
output directory and write the final deliverable there", and finished with
"All artifacts are created and verified." The `execution` check scored 0/4
at weight 0.40 — effectively the whole 014 gap.

**Root cause.** `pipeline/prompts.py::_artifact_instruction` (line 157) is
added unconditionally at line 255, with no branch on `work_obj.kind`:

> Write your artifact to `<run_dir>/out/single.md` using your file tools …
> **That file is the deliverable; nothing else you write or say is.**

reinforced by `v1/writer.py::_ARTIFACT_INSTRUCTION`:

> Do not close with a status update … the artifact file itself, saved with
> your file tools, is what gets read next.

In corpus mode that is correct and load-bearing (PLAN.md §D0). In workspace
mode, where the brief names real workspace paths as the deliverables, it is
false and actively harmful — it tells the model the graded files do not
count, and the gate then agrees with the prompt rather than with the task.

**Change.** Branch on `work_obj.kind == "workspace"`. Keep `out/<node>.md` —
assembly, review and the manifest all depend on it — but reframe it as the
handoff it actually is, and name the workspace as the deliverable surface:

```
Your deliverables are the files your brief names, written in place under
<workspace_root>. Write them with your file tools; they are what gets read
next.

When the work itself is complete, also write a short summary of what you
produced to `<run_dir>/out/<node>.md` — the harness reads that summary, not
your workspace, when it assembles the run. The summary never substitutes
for the deliverables.
```

Suppress `_ARTIFACT_INSTRUCTION`'s "nothing else you write or say is" clause
on the workspace branch. Corpus and text mode keep today's text byte-for-byte.

**Risk to 058/059: HIGH — this is the one change that can plausibly undo a
win.** 059's +0.454 rests entirely on the writer choosing `{"blocks":
[{"id": …}]}` over `{"plan": [{"block": …}]}`, and nothing in the task
prompt names either. That choice is a function of the surrounding prompt
text. Any edit to the writer prompt is a re-roll.

**Mitigation.**
- Make the change **strictly additive**. Do not delete or reorder the
  `goal_and_rubric` and `brief` segments — arm C restates the full goal
  twice (once from `spec.md`, once as `Your brief:`) and that repetition of
  059's four constraints is the most plausible reason arm C attends to all
  of them. Preserving it is the point.
- Flag: `KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT` (default `0`).
- A/B **059 specifically at n≥8** before flipping, and record the emitted
  container key per run as a first-class metric, not just the score. If the
  `blocks` rate drops, the fix is a prompt problem, not a plan problem, and
  is cheap to iterate on.
- Do not combine this flag with §K1 in the same A/B cell. §K1 changes which
  path builds the node; §K3 changes what that node's prompt says. Confounded
  together they are uninterpretable at n=8.

**Tests.** `tests/test_prompts.py`: workspace-kind node's prompt contains
the workspace framing and omits "nothing else you write or say is";
text-kind node's prompt is unchanged (assert against a checked-in golden
string, so an accidental corpus-mode edit fails loudly).

---

## §K4. The explore probe burns ~27k tokens and returns a trace dump

Two independent defects; both must land together if §K1 ever flips on,
because §K1 makes probe output start mattering.

### §K4a. The probe runs even when the planner will not

`_phase_explore` (`pipeline/driver.py:1334`) runs
`_run_structural_exploration` at T2/T3 whenever `needs_explore`. Its entire
purpose is, in its own docstring, "a strictly better partition input than
the label alone" — it feeds the planner. When `_phase_plan` then takes the
fallback branch, nothing consumes it. In 014 seed3 that was 25,439 prompt
tokens for a probe whose output was read by nobody: $0.030 of a $0.164 run,
18% of spend, and ~35s of the 426s wall clock.

**Change.** Hoist the §K1 predicate above the explore phase. Skip structural
exploration when the plan phase is already known to be taking the
single-node path, logging `phase_skipped` with reason
`"plan phase will not partition"`.

**Risk to 058/059: NONE.** The probe's finding today reaches only
`build_tree`, which does not run on those tasks. Verified: `probe_plan.json`
is `{"evaluated": true, "probes": []}` in every inspected run.

### §K4b. Probes are told to write a file they have no tool to write

`v4/research.py::research_prompt` instructs:

> Write your answer to `{raw_path}` as a JSON object: `{"finding": …}`.
> This is the only thing anyone downstream will ever see.

But `v4/mcp_research.py:56-60` grants:

```python
"workspace": ("read", str(WORKSPACE_READ_TOOL_PATH)),
"corpus": ("read",),
```

Neither includes a write tool. So `_read_raw_finding` returns `None` every
time and `run_research_query` falls back to
`result.metadata["assistant_visible_output"] or result.actions_log` — the
raw trace. That is exactly what
`scratch/explore/research/unit-01.md` contains: `{"type": "logdir", …}`,
`{"type": "message", "role": "tool", "tool_name": "read", …}`. **Every
workspace and corpus probe in the repo has been returning a tool-call
transcript instead of an answer**, capped to 300 tokens and handed to the
planner as if it were a finding.

A second bug sits under it.
`pipeline/backends.py::_hidden_paths_and_exceptions_for_probe` (line 80)
computes the write carve-out as
`raw_resolved.relative_to(workspace_resolved)` and swallows `ValueError`.
In workspace mode the run dir is a *sibling* of the workspace, so that
raises and `exceptions` comes back `()` — the carve-out silently does not
exist. The design intent (carve the raw finding path out of the hidden
subtree) is right; both halves are broken.

**Change.**
1. Add `"save"` to the `workspace` and `corpus` allowlists.
2. Make the exception path absolute when the raw path is not relative to the
   workspace, instead of dropping it. Log
   `probe_finding_path_unreachable` rather than swallowing.
3. Belt and braces in `run_research_query`: when the raw finding is missing
   and the fallback text parses as JSONL trace lines, write an empty finding
   and log `probe_finding_degraded` instead of promoting a transcript. A
   probe that returned nothing should say so.

**Risk to 058/059: NONE today** (nothing reads probe findings on the
fallback path), **but it is a prerequisite for §K1.** Landing §K1 while
probes still return transcripts would feed the planner garbage on exactly
the tasks §K1 newly routes through it. Merge order is K4b → K1, never the
reverse.

**Tests.** `tests/test_research.py`: a probe whose adapter writes the raw
file yields that finding; one that writes nothing yields `""` and logs
`probe_finding_degraded`, never the actions log; the workspace allowlist
translates to opencode permissions with `write: allow`; the carve-out
survives a run dir outside the workspace.

---

## §K5. One advisory model call is a hard single point of failure in front of every run

**This is the largest measured effect in the whole dataset, and it was hiding
in the tasks excluded as "harness bugs."** Across all 11 tasks run:

| task | arm A | arm C | arm C halt |
|---|---|---|---|
| 007-session-memory | **1.000** | **0.000** | `error in classify: HTTP 404 from provider` (3/3 seeds) |
| 060-task-cancellation-cleanup | 1.000 | 0.436 | seed2: no result JSON |
| 061-periodic-status-rollup | 0.939 | **0.000** | `error in classify: role episode failed (timeout) after 3 attempts` |
| 103-policy-update-replan-diff | 0.576 | 0.100 | classify timeout |
| 104-async-ops-window-rollup | 0.741 | 0.100 | classify timeout |
| 105-partial-batch-resume-ledger | 0.555 | 0.045 | classify timeout |
| 106-release-approval-gate-plan | 0.600 | 0.110 | classify: billing error |
| **all 11 tasks** | **0.748** | **0.368** | |

Six of eleven tasks scored near zero for arm C, on every seed, because a
single model call failed *before any work started*.

**Root cause.** `pipeline/driver.py:966` — the estimate call is an unguarded
`await`:

```python
estimate, question_set = await asyncio.to_thread(
    estimate_scope_full, goal, work, self.provider, ...
)
measured = classify(signals, estimate)
```

Any exception — HTTP 404, three consecutive 300s timeouts, a provider
billing error — propagates out of `_phase_classify` and halts the run. The
method's own docstring calls this "the one **advisory** model call that
decides how much of the rest of the pipeline this run actually needs." An
advisory call must not be able to destroy a run.

Opencode has no equivalent: it starts working immediately. This is a pure
architectural tax that only kusudaemon pays, and it is currently costing more
score than every other defect in this document combined.

**Change.** The fallback already exists and is already trusted elsewhere:
lines 933–940 construct a synthetic `ScopeEstimate` by hand for the override
path and feed it to `classify(signals, estimate)`. Reuse it.

```python
try:
    estimate, question_set = await asyncio.to_thread(estimate_scope_full, ...)
except Exception as exc:
    estimate = ScopeEstimate(
        files_touched=_files_touched_from_signals(signals),
        artifacts=1,
        answerable_without_exploration=(signals.work_tokens < _T1_WORK_TOKENS_CEILING),
    )
    question_set = QuestionSet()
    self._log({"type": "scope_estimate_degraded", "reason": str(exc)[:200], ...})
measured = classify(signals, estimate)
```

`Signals` is pure code — `measure_signals(goal, work)` on line 930 has
already run and cannot fail. Every input `classify` needs is in hand.

Audit every other unguarded role call on the critical path for the same
shape. A role call that gates *how much* work to do should degrade to a
code-side default; only a call that produces the work itself may halt.

**Risk to 058/059: NONE.** Those runs never hit the failure; the guard is
inert on a successful call. This is the one change in this document with a
strictly one-directional effect.

**Tests.** `tests/test_driver_phases.py`: a provider raising on
`estimate_scope_full` still writes `tier.json`, still logs
`scope_estimate_degraded`, and the run proceeds; a large work object still
classifies ≥T2 through the degraded path.

---

## §K6. There is no code shape, so a code leaf gets a prose template — read this before Terminal-Bench

`v2/planner.py:43`:

```python
_SHAPES = ["prose-dominant", "derivation-dominant", "problem-set-dominant", "reference-dominant"]
```

Four document shapes. Nothing for code, a repo, or shell work. Every
planner-built leaf on a codebase must resolve to one of these, and two of the
four — `_PROSE` and `_REFERENCE`, the likeliest picks for anything ambiguous
— carry `tools=("read", "save")`, which
`translate_tools_to_opencode_permissions` renders as `"bash": "deny"`.

**Terminal-Bench is entirely shell work. SWE-bench Verified is mostly shell
and edit.** Running either before this is fixed measures a tool-policy
defect, not the harness. Expect a near-floor score and no way to tell why
from the summary.

**Change.** Add a `code-dominant` (or `workspace-dominant`) shape to
`_SHAPES` and a matching `NodeTemplate` with
`tools=("read", "save", "patch", "shell")`, no `headers:std`, and judgment
items suited to code rather than prose. Until it exists, force
`DEFAULT_TOOL_ALLOWLIST` for any run whose work object is `kind="workspace"`.

**Risk to 058/059: LOW** — they never reach the planner today, and after §K0
they classify T1 and never will.

---

## §H. HarnessBench-side fixes (`/Users/carlliu/harness-bench`)

These change the ruler. They must land **before** any kusudaemon A/B, and
every number in this document must be regenerated for both arms afterwards.

### §H1. 014's oracle never loads its own ground truth

`tasks/014-task-decomposition/oracle_grade.py` does
`task_dir = w.parent.parent` and reads `ground_truth.json` from there. It
misses: every run in `bench_results/` reports `topics_covered: 0/0`, though
the file defines five `expected_subtask_topics`. With `gt = {}`,
`min_subtasks` falls to its default 3 and `decomposition_score` degenerates
to `num_subtasks / 5` — so 3 subtasks scores 0.6 and auto-fails the 0.7 bar,
4 scores 0.8, and the topic-coverage half of the metric never runs at all.
That is 014 arm C seed2's `decomposition` failure.

**Fix.** Resolve from `__file__`, the way 057's oracle already does
(`_TASK_DIR = Path(__file__).resolve().parent`).

**Effect.** Changes absolute 014 scores for **both** arms. Expect them to
move down as topic coverage starts being measured.

### §H2. Three oracles score JSON shape, not correctness

- **057 `state_scores`**: `results.get(k, {}).get("risk_score")` requires
  `per_item_results` to be a dict keyed by id. A list of
  `{"id": …, "risk_score": …}` fails with every score correct. Arm A
  produced a dict in 1 of 3 scored seeds, arm C in 0 of 3.
- **057 `skip_audit`**: requires a `status` field drawn from
  `{skipped_preexisting, skip_preexisting, reused_preexisting, reused}`,
  a vocabulary the prompt never states. Observed values across runs:
  `preexisting`, `reused`, `skipped_preexisting`, and one run that put the
  same information in a `notes` string.
- **059 `required_blocks`** and its three dependents: require the container
  key `blocks` and per-entry key `id`. This single divergence is 0.55 of
  059's weight and the entire arm C "win."

**Fix.** Normalize before checking — accept a list of objects carrying `id`
alongside the dict form; accept `blocks`/`plan`/`schedule` as the container
and `id`/`block`/`name` as the entry key; accept the documented skip
vocabulary in `status`, `step`, or `notes`. Where a specific shape is
genuinely part of the task, **state it in the prompt** and keep the strict
check. Either is defensible; the current combination — strict check, silent
requirement — measures luck.

**Effect.** Reduces variance for both arms and will compress 059's margin
toward zero. That is the correct outcome: the margin was never real.

### §H3. Arm A reports zero tokens, so token parity cannot be checked

`TESTING.md §1` requires spend within ~10% across arms for a comparison to
mean anything, and `summary.json` restates the caveat. Every arm A record
carries `tokens_by_role: {}` and
`usage_summary.reason = "adapter metadata has no usage root"`.

Cause: both arms run through `scripts/hb_adapter.sh` →
`harnessbench` `generic_cli`, whose metadata is only `{"returncode": …}`.
`runner.py::_collect_usage_summary` looks for `state_dir` / `openclaw_home`
/ `nanobot_home` / `zeroclaw_home` / `picoclaw_workspace` / `hermes_home` —
no opencode key. Arm C's numbers come from kusudaemon's own
`kusudaemon-record.json`, which arm A has no equivalent of:
`pipeline/cli.py:867-950` runs `opencode run --auto <goal>` with
`capture_output=True` and hardcodes `"tokens_by_role": {}`.

**Fix.** In the arm A branch, inject `--print-logs` and parse usage from the
stream with the same code path `adapters/_agent_worker.py` already uses for
arm C (it reads `usage.input_tokens` / `output_tokens` per message, lines
253-270). Write the totals into the arm A record under `tokens_by_role`
`{"bare": …}`. Then `scripts/run_harness_bench.py:355` picks them up and
`summary.json`'s parity check becomes evaluable.

**Effect.** Arm C is currently 6× (014) to 12× (057) arm A's wall clock and
spends 60k–320k tokens per run. It is likely that the parity precondition
has never been met and that every delta in this document is
confounded by spend. Measuring it may be the most consequential item here.

---

## §M1. Measurement hygiene (blocking for every A/B above)

1. **Quarantine provider failures.** 058 arm A seed1 scored 0.156 because
   two of three rounds died on a 504. A run whose `halt_reason` names a
   transport or upstream error is a void run: retry it, and if it fails
   again record it as `errored`, excluded from the mean and reported
   separately. Scoring an outage as capability is how §0's phantom +0.234
   happened.
2. **n ≥ 8 seeds** on any task used to gate a flag flip. At n=3 with
   per-check coin flips, a ±0.08 delta is indistinguishable from noise —
   057's entire remaining gap is two coin flips.
3. **Report a schema-variance metric** beside the score: for each task with
   a shape-sensitive oracle, log the container/entry keys the run actually
   emitted. This turns §H2-class failures into something visible in the
   summary rather than something that has to be excavated from a sandbox.
4. **Re-baseline both arms** after §H lands, before reading any kusudaemon
   change. Numbers from before §H are not comparable to numbers after it.

---

## §R. Long-horizon regression review (corrections to this plan)

Every fix above was written against small-task evidence. Re-read against the
long-horizon path they are meant to protect, **three of them are wrong as
drafted**. Two are errors in the guards added to keep 058/059 stable — and
those guards were written against *input* size, which is not what makes a
task long-horizon.

### §R1. The §K0/§K1 size floor would break generative long-horizon work — including LongGenBench

`v6/tiering.py::measure_signals` measures the **input**: `work_tokens` and
`work_files` come straight off the `WorkObject`. The only output-side signal
is `_OUTPUT_MARKERS = ("chapter", "section", "per file", "for each",
"suite")` — five bare words, no numeric-target detection.

A generative long-horizon goal has a **tiny input and an enormous output**:
"write a 40-chapter reference from this outline" is one 1,500-token file.
The §K0 guard (`work_tokens < 2_000 and work_files <= 8` → decline the T2
escalation) and the §K1 floor (`est_tokens < 2_000` → keep the single-node
path) both fire on it. Result: T1, one node, no decomposition — which is
precisely the failure `v6/direct.py`'s §E28 note already records ("a corpus
that big ... its single node ran blind ... degenerated into repetition").

**LongGenBench is exactly this shape.** As drafted, this plan would sabotage
the next benchmark on the list.

**Correction.** The floor must be a conjunction over input *and* output
evidence, and may only decline an escalation when **every** signal agrees the
work is small:

```python
def _measured_small(signals: Signals, estimate: ScopeEstimate) -> bool:
    return (
        signals.work_tokens < _T1_WORK_TOKENS_CEILING
        and signals.work_files <= _T1_WORK_FILES_CEILING
        and signals.breadth_markers == 0
        and signals.output_markers == 0
        and not _numeric_output_target(goal)   # new: "40 chapters", "50,000 words", "one per X"
        and estimate.artifacts <= 1
    )
```

Add `_NUMERIC_TARGET_RE` (a cardinal or written number adjacent to a
unit-of-output noun) to `v6/tiering.py` and fold its count into `Signals` as
`output_targets`. This is a code-only signal, consistent with §A4.1's "free,
deterministic, no model call," and it is worth having independently of this
plan: today a goal saying "produce 200 sections" is invisible to tiering
unless the word "section" happens to appear.

**Net effect on long-horizon: positive.** Today `files_touched: "unknown"`
force-escalates everything to T2 including trivial work; with this
correction, escalation is declined only on unanimous evidence of smallness,
and generative goals escalate on their own signals rather than on a model's
shrug.

### §R2. §K3's prompt rewrite would break multi-leaf assembly

`v3/assemble.py` reads **every node's `out/<node>.md`** — its own comment:
"Assembly reads every node's artifact, so every node must have one" — and
concatenates them into `assembly/main.md`, which is what gets exported.

§K3 as drafted tells the writer:

> the harness reads that summary, not your workspace ... The summary never
> substitutes for the deliverables.

On the single-node workspace fallback that is true and is the fix. **On a
planner-built leaf in a T2/T3 tree it is false and destructive**: the leaf's
artifact is not a summary of the deliverable, it *is* the deliverable, and
telling a writer otherwise makes assembly concatenate summaries instead of
content. That would gut exactly the long-horizon path this harness exists
for.

**Correction.** Scope §K3 to the code-built single-node path only —
`node.id in (SINGLE_NODE_ID, DIRECT_NODE_ID)` — never to planner-built
leaves. Planner-built leaves keep today's `_artifact_instruction` verbatim in
every work-object kind. Add a test that asserts a planner-built leaf's prompt
is byte-identical before and after this change.

### §R3. §K5's silent degradation is the wrong trade on a long run

Falling back to a synthetic `ScopeEstimate` also discards `question_set` —
the intake questions and objections. On a 90-second task, proceeding on
assumptions is obviously right. On a six-hour corpus run, silently skipping
ambiguity resolution and discovering the misunderstanding at assembly is
worse than failing at minute one.

**Correction.** Make the fallback size-dependent, using signals that have
already been measured by the time the call fails:

- **Below the T2 line** — degrade, log `scope_estimate_degraded`, continue.
- **At or above it** — retry with backoff, and if it still fails, halt with a
  distinct `scope_estimate_unavailable` reason. A long run deserves a loud
  early failure; it is cheap to restart at minute one and expensive at hour
  six.

Either way `tier.json` is written before halting, so `resume` picks up
without re-paying the classify call.

### §R4. §K2 should demote `headers:std`, not delete it

`build_single_node_tree` is reached by two paths: the small-work fallback
(§K1) and `driver.py:1549`'s "planner returned empty tree" fallback, which
can fire on a corpus of any size. On a 50,000-token single-node document,
markdown heading hygiene is a real check worth keeping.

**Correction.** §K2 moves `headers:std` from `gates` to `warn_gates` rather
than dropping it. That satisfies `v1/gates.py`'s own §C1 warn-only policy
(which the `_PROSE` template violated by graduating it), removes the
redispatch, and keeps the signal in `audit/<node>.json` where the reviewer
and the dashboard can see it.

### §R5. Changes that are neutral or actively help long-horizon

| change | effect on long-horizon |
|---|---|
| §K1 core (drop the `start_chunk == -1` clause) | **strongly positive** — this is the bug that has been silently disabling decomposition on every single-unit workspace |
| §K4b (probes get a write tool) | **positive** — probe findings feed the planner, and matter more the larger the corpus; today every one is a trace dump |
| §K4a (skip probes when the planner will not run) | inert — the planner runs on long-horizon work, so probes still run |
| §K6 (code shape) | **positive** — purely additive; unblocks repo-scale work |
| §K2 tools (restore `DEFAULT_TOOL_ALLOWLIST`) | positive — only touches the single-node path; planner leaves are unaffected |
| §H1–§H3, §M1 | no runtime effect (benchmark-side) |

### §R6. Standing rule

No guard added to protect a small-task benchmark result may key on input
size alone. Long-horizon is a property of the **work to be produced**, not of
the bytes handed in. Any new floor states which output-side signal it
consulted, and ships with a test whose fixture is a small input with a large
declared output.

---

## §S. Sequencing

| step | items | flag state | gate to proceed |
|---|---|---|---|
| 1 | §H1, §H2, §H3, §M1 | n/a | full re-baseline, both arms, n≥8 |
| 2 | **§K5 classify fallback** | on (strictly one-directional) | 007/061/103-106 stop halting; re-run all 11 tasks |
| 3 | §K0 tier guard **as corrected by §R1** | `KUSUDAEMON_TIER_TRUST_SIGNALS=1` | all four tasks classify T1; a small-input/large-output fixture still reaches T2 |
| 3b | §K6 code shape | on | Terminal-Bench/SWE-bench precondition |
| 3 | §K4b, §K4a | on (bug fixes, nothing reads the output today) | suite green; probe findings are prose, not JSONL |
| 4 | §K2 gate half | `KUSUDAEMON_DIRECT_TEMPLATE=1` | redispatch count → 0; 058/059 unchanged |
| 5 | §K2 tools half | `KUSUDAEMON_DIRECT_TOOLS=1` | 057 `state_scores`/arithmetic improves; 058 no regression |
| 6 | §K3 **scoped per §R2** | `KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT=1` | 014 `execution` → 3/3; 059 container-key rate holds |
| 7 | §K1 **with the §R1 floor** | `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE=1` + token floor | 058/059 unchanged; real-repo run improves |

Steps 2–7 are single-variable cells. Never combine them in one benchmark
run; at these effect sizes a combined cell cannot be attributed.

## §T. Test conventions

Stdlib `unittest`, no pytest, no network, no agent binary, no API key
(`CLAUDE.md`). Every new file keeps the load-bearing
`sys.path.insert(0, str(_REPO_ROOT / "src"))` prologue. New tests raise the
`SUITE_TEST_COUNT_FLOOR` in `tests/test_suite_reachable.py` (currently 1100)
in the same commit that adds them.

Run:

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```
