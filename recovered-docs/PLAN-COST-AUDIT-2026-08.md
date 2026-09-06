# Kusudaemon: model-call and token-efficiency audit + implementation plan

**Scope.** Full workflow trace of every backend LLM call the harness makes, measured against
run `rec178681445199a5ed` (T3, 825k-token chemistry textbook, 30 spine units, 25 leaves,
backend `opencode/deepseek-v4-flash-free`, `roles.transport = backend`).

**Goal.** Fewest model calls and fewest input tokens at today's quality level. Every proposal
below either (a) removes a call whose outcome was already determined by code, (b) removes
tokens that carried no information, or (c) removes a *failure mode* that spent tokens and
returned nothing. Nothing here removes a quality signal.

---

## 1. Measured baseline

### 1.1 Call inventory for the sample run

| Phase | Role calls | Writer/agent episodes | Notes |
|---|---|---|---|
| classify | 1 | — | `estimate_scope_full` (already merges intake round 1) |
| intake | 0 | — | reused `tier.json:intake_round1` — correctly free |
| explore / survey | 0 | 1 (pseudo) | `survey_mode=auto` → `survey_chunks_structural`, 0 calls |
| plan | 22 | — | `plan_level` recursion; 8 returned `children`, 14 `children`+`probes` |
| pilot | 0 | 3 | pilot writer episodes only |
| research | 0 | 0 | ran 10× as an instant no-op (`probe_plan.json.evaluated=true`) |
| execute | **100** | **60** | all 100 are Reviewer verdicts |
| review | 0 | — | `document_review=false` |
| assemble | 0 | — | deterministic |
| **total** | **123** | **61** | |

### 1.2 Token accounting (measured, and a floor not a total)

Usage was reported for only **61 of 123** role calls and **21 of 72** cost rows.

```
role calls (61 reporting):     594,858 prompt   881,853 completion
writer episodes (9 reporting): 765,044 prompt    41,000 completion
cost.jsonl total:            1,060,254 prompt   275,444 completion   $1.89
```

Two facts drive everything below:

1. **Role calls spend more on output than input** (882k vs 595k). The reviewer is emitting
   12,000–32,000 completion tokens for a schema whose correct answer is a few hundred.
2. **26% of role prompt tokens and 36% of role completion tokens produced nothing at all.**
   18 of 123 role calls returned no parseable JSON: 155,456 prompt + 321,165 completion
   tokens burned for zero information. **9 calls terminated at exactly `completion_tokens:
   32000`** — the model's output cap — mid-emission.

### 1.3 Amplification

- 60 writer episodes for 25 leaves (**2.4×**). Four nodes burned 6 episodes each.
- All 35 redispatches logged `reason: resumed_session`.
- Worst observed single writer prompt: **170,094 tokens** — against a node whose output gate
  is `max_tokens:24000`.
- Spine units of **105,126 / 93,881 / 65,487** tokens against `DEFAULT_TARGET_UNIT_TOKENS =
  16_000`.

---

## 2. What is already optimal (do not regress)

Listing these because several are non-obvious and a naive "cost pass" would undo them.

- **Orchestrator is already zero-call.** `dispatch_policy=document_order` plus
  `decide_next_action`'s single-ready-node and wave-consumes-ready-set short-circuits
  (`v1/orchestrator.py`). 32 dispatch decisions, 0 model calls. Keep.
- **Intake round 1 is folded into classify** (`estimate_scope_full`, `v6/tiering.py`) and
  cached in `tier.json`, so resume re-asks nothing. 1 call, not 2.
- **Survey defaults to structural** (`survey_chunks_structural`) — 0 calls where the model
  path would have spent up to `DEFAULT_MAX_SURVEY_CALLS = 60`.
- **Probe planning is folded into `plan_level`'s `probes` sink**, so `plan_probes`' separate
  windowed pass never ran.
- **Review verdicts are digest-cached** (`compute_verdict_digest` + `audit/<node>.json`) —
  one `node_review_cached` hit in this run.
- **Reviewer never sees writer reasoning**; gates run before review; a node with no
  `judgment` skips review entirely (6 of 25 nodes here).
- **`build_node_prompt` orders segments stable-first for prefix caching.** Correct — but see
  finding L: on a CLI backend no prefix caching happens at all, so the work is currently
  unrealized.
- **Assemble/compile are deterministic.** No model call.

---

## 3. Findings, ranked by measured savings

### A — Reviewer output is unbounded, and the retry path echoes the failure back

**Severity: highest. 26% of role input tokens, 36% of role output tokens.**

Three defects compound:

1. `v1/reviewer.py:VERDICT_SCHEMA` — `items` is an unbounded array. Every other schema in the
   codebase caps its arrays (`FULL_SCOPE_SCHEMA` uses `maxItems: 4/8`,
   `probe_planner.py` caps per window). The reviewer does not, so a reviewer that finds a
   long tail of nits emits until it runs out of output budget.
2. There is **no output-token ceiling on role episodes.** `types.py:EpisodeBudget` carries
   only `max_duration_seconds`. Nine calls stopped at exactly 32,000 completion tokens.
3. `roles/backend_provider.py:complete_json` reprompt path:

   ```python
   base_messages = [*base_messages,
       {"role": "assistant", "content": content or episode_result.actions_log},
       {"role": "user", "content": f"That did not validate: {last_error}. ..."}]
   ```

   The *entire* failed output (or, when empty, the entire `actions_log`) is appended,
   untruncated, and the loop runs `retries + 1 = 3` times. Directly observable:
   three consecutive role calls at prompts of **40,866 / 40,862 / 40,849** tokens — the same
   review, growing by its own garbage.

**Fix.**

- Add `"maxItems": 12` to `VERDICT_SCHEMA.properties.items` and to
  `v3/document_review.py:DOC_REVIEW_SCHEMA`. 12 located defects is far past the point where
  a node should just be regenerated; nothing real is lost.
- Add `max_output_tokens: int | None = None` to `EpisodeBudget`, thread it through
  `CommandAgentAdapter.run_episode` to each backend's CLI flag, and default role episodes to
  `2048`. A role call that cannot answer in 2k tokens is failing, and failing at 2k costs
  1/16 of failing at 32k.
- Do not echo the failure. Replace the reprompt with a fresh, terse message appended to the
  *original* messages: `"Return only a JSON object matching the schema. No prose, no
  reasoning, no code fences. At most 12 items."` Cap any echoed fragment at 200 tokens if
  you keep one at all. The failed text carries no information the schema restatement does
  not.
- Classify "stopped at the output cap" as its own failure and stop retrying it more than
  once — an unbounded generator does not become bounded on attempt 3.

**Projected:** role completion 882k → ~80k; role prompt 595k → ~380k. Eliminates all 18 dead
calls.

**Quality risk:** low. The 18 dead calls contributed nothing; the surviving verdicts are
short. Regression test: extend `tests/test_v1_reviewer_fanout.py` with a fake provider that
returns 40 items and assert schema rejection, and `tests/test_backend_role_provider.py` with
an assertion that the reprompt message set does not grow by more than N tokens.

---

### L — Route role calls over HTTP, not a CLI session, whenever a provider entry exists

**Severity: highest (structural; subsumes much of A).**

`roles/factory.py:_resolve_role_transport` defaults every non-gptme backend to
`transport = "backend"`. So all 123 role calls spawned a fresh `opencode run` process.
Consequences:

- The CLI's own system prompt and tool schema are loaded on every call even though
  `build_role_adapter` passes `tool_allowlist=()`. That is a fixed per-call tax × 123.
- **No `response_format` / JSON-schema enforcement.** `BackendRoleProvider` flattens the
  schema into prose (`json_io.build_json_instruction`) and parses the output with
  `extract_last_json_object`. That prose instruction is exactly what the 18 failures ignored.
  `v1/provider.py:OpenAICompatibleProvider` has the real latch.
- **No prompt-prefix caching.** Every reviewer call resends a byte-identical system prompt
  in a brand-new session. 123 calls × an identical preamble, never cached.
- No `max_tokens`, no `temperature` control (the `temperature=0.0` argument is silently
  dropped by the backend transport).

**Fix.** Invert the default: prefer `transport="http"` when `provider.json` (or the env)
resolves a base URL and key for the role model; fall back to `"backend"` only for keyless
setups where the CLI's own auth is the only credential. Same model, same weights — this is a
transport change, not a quality change. Keep `KUSUDAEMON_ROLE_TRANSPORT` as the escape hatch.

**Projected:** on top of A, another 25–40% off role input tokens from prefix caching plus
elimination of the CLI preamble, and most of A's parse failures disappear because
`response_format` enforces the schema server-side.

**Quality risk:** low, but this is the change most worth A/B-ing with `eval/runner.py` before
flipping the default, because it changes which sampler path the role sees.

---

### C — Retry pays for the same content three times

**Severity: high. Drives the 2.4× writer amplification.**

On a review failure, three mechanisms all deliver the prior attempt, and they stack:

1. `v0/runner.py:run_node` — `node_review_failed` correctly invalidates the completion
   replay, but the `session_captured` event still exists, and `opencode` has
   `supports_session_resume = True`. So the retry **resumes the old session**, which replays
   the entire prior transcript — including the 105k-token unit file the writer read.
   All 35 redispatches took this path.
2. `pipeline/prompts.py:_prior_attempt_artifact` — inlines the failed artifact into the retry
   prompt, capped at `DEFAULT_ARTIFACT_CAP_TOKENS = **50_000**` (borrowed from the
   *reviewer's* input cap, which is the wrong ceiling for this use).
3. The full node prompt (goal, contract, brief, inputs) is re-sent alongside.

The artifact is on disk and the writer has `read`/`save` tools. Handing it 50k tokens of its
own prior output, inside a session that already contains that output, is triple-paying.

**Fix.**

- In `prompts.py:segments`, skip the `_prior_attempt_artifact` inline entirely when the
  dispatch will resume a session (pass a `resuming: bool` through from `v0/runner.py`, which
  already knows). The transcript has it.
- When not resuming, cap the inline at the node's own `budget.tokens` (24k here), not 50k —
  introduce a distinct `RETRY_INLINE_CAP_TOKENS` rather than reusing the reviewer constant.
- For `class: patchable` defects, send the located defect and the artifact *path*, not the
  artifact body. `_PATCH_RETRY_INSTRUCTION` already says "make the MINIMAL change" — a patch
  instruction plus a path is the cheap, coherent version of that.

**Projected:** retry episodes drop from ~160k to ~30k prompt tokens, and retries that
currently drown in noise start converging — writer episodes 60 → ~35.

---

### D — `inline_spans` defaults disagree, so writers read 105k-token files

**Severity: high.**

- `pipeline/driver.py:226` — `RunOptions.inline_spans: bool = True`
- `dashboard/state.py:1074` — `inline_spans=bool(body.get("inline_spans", False))`
- `pipeline/prompts.py:221` — parameter default `False`

This run came through the dashboard and got `inline_spans: false`. So `node.inputs` was
handed to the writer as a bare path (`spine/unit-09.md`, 105,126 tokens) and the agent read
the whole thing — against a 24k-token output gate. `v2/retrieval.py:retrieve_spans` and the
3.8MB `chunks.jsonl` index were both built and then unused.

**Fix.** One default, `True`, in all three places. Additionally: derive the span budget from
the node's own budget instead of a flat `DEFAULT_TOP_K = 8` — `top_k` such that
`sum(span.tokens) ≈ 2 × node.budget.tokens` is the honest relationship (you need more input
than output, but not 7× more).

**Quality risk:** real and worth measuring. Retrieved spans can miss material a full read
would catch. Mitigation: `eval/measure.py` already reports `mean_tokens_by_segment`; run the
same corpus both ways and compare gate/review pass rates before flipping the dashboard
default. This is the one item I would gate on eval data rather than ship on reasoning.

---

### E — Spine units overshoot their own target by 6.5×

**Severity: high, and it is the root cause of D's damage.**

`pipeline/driver.py:_phase_survey` calls `prefold_chunks(chunks, target_max_chunks=100)`.
Over an 825k-token corpus that makes atomic chunks of ~8,250 tokens. `assemble_spine` then
tries to hit `DEFAULT_TARGET_UNIT_TOKENS = 16_000` but can only cut at chunk boundaries, so
it cannot land near target. Result: units of 105,126 / 93,881 / 65,487 tokens. Every leaf
fed one of those is structurally over budget before a model is ever called.

**Fix (deterministic, zero model calls).** Either:

- derive `target_max_chunks` from corpus size so an atomic chunk stays ≤ ~2,000 tokens
  (`target_max_chunks = ceil(corpus_tokens / 2000)`, with a hard ceiling for pathological
  corpora); or
- add a post-`assemble_spine` splitter that bisects any unit > `2 × target_unit_tokens` at
  chunk boundaries.

The first is better — it fixes the input to the boundary logic instead of patching its
output. Note the interaction: more atomic chunks makes the *model* survey path more
expensive, but that path is off by default (`survey_mode=auto` → structural), so there is no
call-count cost here.

---

### G — The cost ledger is half blind, so the budget fences cannot fire

**Severity: prerequisite for everything else.**

- 51 of 72 `cost.jsonl` rows have `prompt_tokens: 0`.
- 62 of 123 role trajectories have no `usage` record.
- `driver.py:718–740` enforces `max_cost_usd` / `max_total_tokens` by reading that ledger.
  With ~50% of rows at zero, a run cannot be capped, and no claim in this document can be
  verified after a change.

**Fix.** In `v0/cost.py:CostLedger.record` (and the two call sites in
`roles/backend_provider.py` and the writer path), when the adapter reports no usage, fall
back to `v1/gates.py:estimate_tokens` over the prompt and the captured output, and stamp the
row `"estimated": true`. Deterministic, free, and it makes the budget fence real.

**Do this first.** It is the measurement instrument for items A–E.

---

### H — Plan phase: 22 calls where ~13 would do

**Severity: medium.**

22 `plan_level` calls for a 30-unit spine that yielded 25 leaves. Two avoidable classes:

1. **The `probes` sub-schema is sent on every call but only read on one.** `PARTITION_SCHEMA`
   (`v2/planner.py:45`) is a static module-level dict that *always* contains the `probes`
   array, while `recurse` passes `probe_sink=probe_sink if depth == 0 else None`
   (`v2/planner.py:546`). So 21 of 22 calls shipped the `probes` schema in their prompt and
   **13 of them spent output tokens emitting probes that were discarded on return**. The
   comment there is right that deeper suggestions could never resolve — but the schema is
   still asking for them. Build the schema per call: `_partition_schema(with_probes: bool)`,
   probes only at depth 0.
2. **No deterministic leaf short-circuit on the non-`code_tile_planner` path.**
   `v2/planner.py:522–532` has exactly the right checks — `all(u.tokens >= token_budget)` →
   forced leaves, `sum(u.tokens) <= token_budget` → single leaf — but both sit behind
   `if code_tile_planner:`, which was `false` for this run. `leaf_gate` is pure code; a slice
   that already satisfies it does not need a model to be told it is a leaf. Hoist those two
   checks out of the `code_tile_planner` branch.

**Projected:** plan calls 22 → 12–14, and the surviving deep calls shed the `probes`
sub-schema from both their input and output budgets.

---

### K — The episode cache exists and is switched off

`run.spec.json` has `episode_cache: false`. `v0/episode_cache.py` implements
content-addressed memoization keyed on (prompt, model, input SHAs, contract SHA, tool
allowlist), and a hit still re-evaluates gates and review from scratch — so it cannot mask a
quality regression. This run had 6 `phase_failed execute` → restart cycles; identical
episodes re-ran.

**Fix.** Default `episode_cache: true`. Low risk given the gate/review re-evaluation, and it
directly attacks the crash-restart waste that produced the 35 session resumes.

---

### F — Warn-gate noise reaching retry prompts

`audit/c09.json` and `audit/c12.data-881.json` record
`terms_defined: passed=false, "276 candidate terms unverified"` with `verdict: pass` — these
are `warn_gates`, correctly non-blocking. Confirm (with a test) that warn-gate detail never
lands in `node.last_defect`, since `prompts.py` reads `last_defect` back into the retry
prompt and 276 unverified terms would be a large, actionable-looking, meaningless block.

---

### J — Re-entry churn (diagnose, don't optimize)

`research` ran 10× as an instant no-op because `execute` failed 6× and `run()` recomputes the
phase list each iteration. The research no-op costs ~0 model calls — leave the loop alone.
The thing worth fixing is *why* execute failed 6 times, because each restart is what produced
the session-resume path in finding C. Two `job_failed` events show a redispatch job firing at
an already-`passed` node (`"node 'c01.kinetic-motion' is 'passed' — not redispatchable"`),
which suggests the dashboard's redispatch queue and the round loop disagree about node state.
Worth a separate look; not a token issue directly.

Also cosmetic: 121 `session_captured` events for 61 episodes — the watcher logs the logdir
line and the logdir+session_id line as two events. Harmless, but it inflates every event-log
scan.

---

## 4. Implementation plan

### Phase 0 — Instrumentation (do first; nothing else is verifiable without it)

| # | Change | Files | Test |
|---|---|---|---|
| 0.1 | Estimated-token fallback in the cost ledger, `estimated: true` flag | `v0/cost.py`, `roles/backend_provider.py`, writer record path | new case in `tests/test_token_and_subagent_labels.py` |
| 0.2 | Assert the budget fence fires on estimated rows | `pipeline/driver.py:718` | `tests/test_driver_phases.py` |
| 0.3 | Add per-role call-count + in/out token rollup to `eval/measure.py` | `eval/measure.py` | `tests/test_eval_harness.py` |

**Exit criterion:** re-run the same corpus; `cost.jsonl` has zero `prompt_tokens: 0` rows.

### Phase 1 — Role-call economics (largest win, lowest quality risk)

| # | Change | Files |
|---|---|---|
| 1.1 | `maxItems: 12` on `VERDICT_SCHEMA.items` and `DOC_REVIEW_SCHEMA.items` | `v1/reviewer.py`, `v3/document_review.py` |
| 1.2 | `EpisodeBudget.max_output_tokens`, threaded to each adapter's CLI flag; role default 2048 | `types.py`, `adapters/cli_agent.py`, `adapters/{opencode,claude_code,codex,antigravity}.py` |
| 1.3 | Stop echoing failed output in the reprompt; terse schema restatement instead; cap any echo at 200 tokens | `roles/backend_provider.py:complete_json` |
| 1.4 | Treat output-cap termination as a distinct failure; at most one retry | `roles/backend_provider.py` |
| 1.5 | Prefer `transport="http"` when a provider/key resolves for the role model | `roles/factory.py:_resolve_role_transport` |
| 1.6 | Lower `DEFAULT_ARTIFACT_CAP_TOKENS` 50k → 12k so fan-out engages and each call stays small | `v1/reviewer.py` |

**Exit criterion:** role completion tokens down ≥80%; zero role calls terminating at the
output cap; parse-failure rate <2% (from 14.6%).

### Phase 2 — Writer input diet

| # | Change | Files |
|---|---|---|
| 2.1 | `target_max_chunks` derived from corpus size (≤2k tokens/chunk) | `pipeline/driver.py:_phase_survey`, `v2/survey.py` |
| 2.2 | Deterministic post-`assemble_spine` splitter for units > 2× target | `v2/survey.py` |
| 2.3 | `inline_spans=True` as the single default in all three places | `pipeline/driver.py`, `dashboard/state.py:1074`, `pipeline/prompts.py` |
| 2.4 | `top_k` derived from `node.budget.tokens` instead of flat 8 | `v2/retrieval.py`, `pipeline/prompts.py` |
| 2.5 | Skip `_prior_attempt_artifact` inline when resuming a session | `pipeline/prompts.py`, `v0/runner.py` (pass `resuming`) |
| 2.6 | `RETRY_INLINE_CAP_TOKENS = node.budget.tokens`, separate from the reviewer cap | `pipeline/prompts.py` |
| 2.7 | Patchable defects: send defect + artifact path, not artifact body | `pipeline/prompts.py` |

**Exit criterion:** no writer episode prompt exceeds 3× its node's `budget.tokens`; writer
episodes per leaf ≤1.5× on the same corpus at equal gate/review pass rates.

**Gate 2.3 on eval data** — see finding D's quality note.

### Phase 3 — Trimming and hygiene

| # | Change | Files |
|---|---|---|
| 3.1 | `leaf_gate` check before recursing into `plan_level` | `v2/planner.py` |
| 3.2 | Request `probes` only on the top-level plan call | `v2/planner.py`, `pipeline/driver.py:_phase_plan` |
| 3.3 | `episode_cache: true` by default | `pipeline/driver.py:RunOptions`, `dashboard/state.py` |
| 3.4 | Test: warn-gate detail never enters `last_defect` | `tests/test_v1_gates_c1.py` |
| 3.5 | Collapse the double `session_captured` event | `v0/runner.py` |
| 3.6 | Investigate the 6 `execute` failures and the `job_failed` redispatch/state disagreement | `pipeline/driver.py`, `dashboard/state.py` |

---

## 5. Projected result on the same run

Stated as a projection from the measured baseline, not a promise — Phase 0 exists to check it.

| Metric | Measured now | After Phase 1 | After Phase 2 | After Phase 3 |
|---|---|---|---|---|
| Role calls | 123 | ~105 | ~105 | **~90** |
| Role prompt tokens | 594,858 | ~330,000 | ~330,000 | **~280,000** |
| Role completion tokens | 881,853 | ~80,000 | ~80,000 | **~75,000** |
| Writer episodes | 60 | 60 | ~38 | **~35** |
| Writer prompt tokens (worst episode) | 170,094 | 170,094 | ~35,000 | ~35,000 |
| Dead calls (no parseable output) | 18 (14.6%) | ~2 | ~2 | **~2** |

Roughly **2–3× less total spend at equal quality**, with the reviewer output blowup (A+L) and
the writer input diet (D+E) accounting for most of it.

---

## 6. The one structural question worth deciding before Phase 2

The harness caps a leaf's *output* at 24,000 tokens (`gates: max_tokens:24000`) but places no
ceiling on its *input*. Findings D and E are both consequences of that asymmetry: a 105k-token
unit was allowed to become one leaf's input because nothing in `leaf_gate` or the gate list
says it cannot be.

`v2/planner.py:leaf_gate` does check `candidate.tokens > token_budget` — but `token_budget`
is **one number serving three roles at once**: `DEFAULT_TOKEN_BUDGET` (50k, 24k for this run)
is simultaneously the leaf gate's *input* ceiling, the node's `NodeBudget.tokens`, and the
literal output gate `max_tokens:{token_budget}` (`v2/planner.py:466,479`). And the check has a
documented escape hatch that swallows the violation: `recurse`'s
`len(slice_units) <= 1 → forced_leaf(..., "single unit, cannot split further")`
(`v2/planner.py:516–518`) fires *before* any budget check, so unit-09 at 105,126 tokens became
a leaf with a 24,000-token input ceiling and nothing logged an objection. That is why finding E
is upstream of finding D: the spine, not the planner, is the only place that violation can be
prevented.

The clean fix is to make input budget a
first-class, code-enforced quantity: a node is only a legal leaf if its retrieved-or-read
input fits `k × budget.tokens`, and the spine splitter is what guarantees that is satisfiable.
That is a slightly larger change than the individual items in Phase 2, and it would let 2.1,
2.2 and 2.4 fall out of one invariant instead of three separate patches. Worth deciding which
shape you want before implementing that phase.
