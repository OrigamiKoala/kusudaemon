# PLAN-REVIEW-LATENCY.md

Review-stage latency and reviewer grounding. Written 2026-09-06 against
commit `d89b547`, after the `data_try6` HarnessBench arm-C runs in
`bench_results/`.

**Thesis.** The review stage is not slow because reviewing is expensive.
`v1/reviewer.py:review_node` is a pure text-in / JSON-out function, and it
is being executed as a full agentic CLI episode. The cost is transport, not
judgment. Fixing transport is a ~100x wall-clock win with zero change to
what the reviewer concludes; only after that is it worth touching what the
reviewer *sees*.

The web-access question is downstream of a grounding bug, not a policy
question. See §3.

---

## §1 Evidence

`bench_results/records.jsonl`, arm C (the harness arm), `data_try6`:

| run | wall (s) | `calls_by_role` | outcome |
| --- | --- | --- | --- |
| `014-task-decomposition` seed2 | 1675.2 | `{'role': 2, 'writer': 2}` | 0.879, escalated in assemble |
| `014-task-decomposition` seed1 | 2400.4 | `{}` | hard timeout at the 2400 s harness cap |
| `014-task-decomposition` seed3 | 0.15 | `{}` | `error in classify: HTTP 404 from provider` |
| `007-session-memory` seed1–3 | ~0.30 | `{}` | `HTTP 404 from provider` (both rounds) |
| `057-interruption-resume` seed1–2 | ~0.30 | `{}` | `HTTP 404 from provider` (both rounds) |

Two independent failures are visible here and they interact:

1. **Six of eight arm-C runs never reached a reviewer at all** — they died
   in `classify` on `HTTP 404 from provider`. The direct HTTP provider is
   misconfigured (§2.1). A run that 404s in `classify` is not measuring the
   review stage; it is measuring nothing.
2. **The one arm-C run that produced an artifact spent 1675 s on four total
   model calls** — `{'role': 2, 'writer': 2}`, or roughly 400 s per call.
   That is process-spawn and agentic-loop latency. An HTTP call to any of
   the configured models is 2–5 s. The 400 s figure is the entire finding.

Arm A (`calls_by_role: {'bare': 1}`) averages 265.9 s per run with a mean
score of 0.577. Arm C is currently slower than arm A by an order of
magnitude while completing fewer tasks. Nothing about review *semantics*
explains that gap.

---

## §2 Root cause: three multipliers on one verdict

### §2.1 Transport — the dominant term

`roles/factory.py:_resolve_role_transport` resolves the role transport as:

```python
if effective_backend == "gptme":
    transport = "http"
else:
    res = resolve()
    transport = "http" if (res.api_key and res.base_url) else "backend"
```

With `backend=opencode` (the bench configuration) this depends entirely on
whether `provider_config.resolve()` returns a populated key *and* base_url
in the sandbox environment. When it does not, every role call —
orchestrator, planner, reviewer, document review — becomes
`roles/backend_provider.py:BackendRoleProvider.complete_json`, which runs a
one-shot CLI episode through `pipeline/backends.py:build_role_adapter`.

The budget on that path:

```
EpisodeBudget(max_duration_seconds=180)   # KUSUDAEMON_ROLE_TIMEOUT default
max_episode_retries = 2                   # -> 3 episodes per attempt
retries = 2                               # -> 3 attempts per complete_json
                                          # = up to 9 subprocess spawns
                                          # = up to 27 min for ONE verdict
```

Worse, the adapter is constructed **inside** the retry loop:

```python
for attempt in range(retries + 1):
    ...
    adapter = build_role_adapter(backend=..., run_dir=..., phase="role", ...)
```

so each attempt pays a fresh `mkdir` of `tmp/roles/role{,/prompts}`, a fresh
`read_backend_config`, and a fresh CLI process. Nothing about the adapter
varies across attempts.

The 404s in §1 are the same bug seen from the other side: `provider.json`'s
`gptme.providers.nvidia.models` contains the entry `"n"` — a truncated model
id that will resolve and 404. When `resolve()` succeeds but the model id is
junk, the run halts in `classify`; when `resolve()` fails outright, the run
silently degrades to the 400 s/call CLI path. Both are one config defect.

### §2.2 Fan-out is serial

`v1/reviewer.py:review_node`, over-cap branch:

```python
for section_text in sections:
    ...
    payload = _call_reviewer(rubric_lines, section_text, provider, ...)
```

Up to `MAX_FANOUT_SECTIONS = 6` sequential calls against the same rubric,
short-circuiting only when a section returns a `regenerate`-class defect.
The sections are independent by construction — same rubric, disjoint text —
so the serialization buys nothing. On the CLI transport this is 6 × §2.1.

### §2.3 Schema repair re-runs whole episodes

There is no `response_format` on the CLI path, so
`roles/json_io.py:build_json_instruction` states the schema in prose and
`_flatten_messages` folds it into the system text. When the CLI wrapper
emits commentary around the object, `_parse_json_object` and
`extract_last_json_object` both fail and `complete_json` reprompts — which
means another full episode, not another turn.

`v1/reviewer.py:_call_reviewer` already carries a bespoke escape hatch for
one recurring instance of this (`DEFECT_MAXLENGTH` 400 →
`RELAXED_DEFECT_MAXLENGTH` 600 on a `maxLength` `ProviderError`). That
workaround existing in the reviewer specifically is direct evidence the
repair path fires in production rather than in theory.

### §2.4 On the web-scraping symptom

The current mitigation is already in place and is not the problem.
`build_role_adapter`'s opencode branch passes
`translate_tools_to_opencode_permissions(())`, which sets every entry in
`("read", "edit", "write", "bash", "web_search", "websearch", "webfetch",
"task", "glob", "grep", "question")` to `deny` and adds `"*": "deny"`.
The claude branch passes `translate_tools_to_claude_disallowed(())`, which
denies all of `ALL_CLAUDE_TOOLS` including `WebSearch`.

The reviewer model still *emits* the tool calls. A coding-CLI system prompt
trains it to resolve a string like `out/n03.md` as a repository path on
sight; the denial arrives, the model reasons about the denial, and retries.
The network hop is gone; the loop is not. Those turns are billed and timed
either way. Denying the tool addressed the observable (outbound requests)
without addressing the driver (§3).

---

## §3 Why the reviewer reaches for the web

`review_node` builds its entire user message as:

```python
f"Rubric:\n{rubric_lines}\n\nArtifact:\n{artifact_text}"
```

It never receives `contract.md`. It never receives the node's declared
inputs. It never receives the spine entry or any `research/` raw finding.

Note that `v1/reviewer.py:compute_verdict_digest` already accepts a
`contract_text` parameter, and the one caller —
`v1/round_loop.py:review_and_transition_node` — omits it:

```python
verdict_digest = compute_verdict_digest(artifact_text, node.rubric, node.judgment)
```

So the digest is computed over a contract the reviewer was never shown, and
a contract amendment does not invalidate a cached pass. Both halves of that
are bugs, and the second one is a correctness bug in the cache, not just a
latency one.

The consequence for hallucination: when a rubric item asks whether claims
are supported, the reviewer holds no ground truth and must either
rubber-stamp or invent. Restoring web access does not fix this — it
converts an unanswerable question into an unbounded search, which is
precisely the observed behaviour. The fix is a closed world, not a
denylist: the reviewer should never need to look anything up, because
everything it is permitted to judge is already in its context.

The measured value of the current check, from
`tests/fixtures/reviewer_baseline.json`:

```json
{"precision": 0.80, "recall": 0.80,
 "per_class_recall": {"contract_rule_violated": 0.75, "duplicate_content": 0.75,
                      "coverage_gap": 0.75, "register_drift": 0.75},
 "mean_tokens_per_call": 4500.0}
```

and `v6/templates.py` records that most leaves ship with an empty
`judgment`, so `review_node` auto-passes without a model call at all. The
current design pays a full CLI-episode budget for a check that fires on a
minority of nodes at 80/80.

---

## §4 Plan

Three tiers, strictly ordered. Tier 0 changes no semantics and must be
measured on its own before Tier 1 lands, or the effect of everything after
it becomes unattributable.

### Tier 0 — transport only

**T0-1. Repair the provider config, then pin role transport to `http`.**
Remove the `"n"` entry from `gptme.providers.nvidia.models` in
`provider.json` and verify each remaining model id resolves against
`https://integrate.api.nvidia.com/v1`. Then set an explicit
`"roles": {"transport": "http"}` block rather than relying on
`_resolve_role_transport`'s inference. HTTP gives no process spawn, no
agentic loop, native `response_format` schema enforcement (which retires
§2.3 entirely), and streaming. Expected: ~400 s → 2–5 s per verdict.

**T0-2. Make transport degradation loud.** A role call that falls back from
`http` to `backend` currently does so silently and costs ~100x. Emit a
`role_transport_degraded` event from `_resolve_role_transport` carrying the
reason (`no_api_key` / `no_base_url` / `resolve_failed`). A bench run must
not be able to become 20x slower without a line in `events.jsonl`.

**T0-3. Hoist adapter construction out of the retry loop** in
`BackendRoleProvider.complete_json`. Build once before
`for attempt in range(retries + 1)`. Independent of T0-1; the CLI path
remains the fallback and should not be gratuitously bad.

**T0-4. Parallelize reviewer fan-out.** Replace `review_node`'s serial
`for section_text in sections` with a bounded gather. The existing
`provider_semaphore` threaded through
`round_loop.review_and_transition_node` is the natural bound; the
`regenerate` short-circuit becomes "cancel the rest of the wave" rather
than "break". 6x → 1x wall clock on over-cap artifacts.

**T0-5. Raise `max_parallel`.** `RunOptions.max_parallel` defaults to 1 and
is bumped to `min(4, cpu_count)` only for dependency-free trees
(`driver.py` ~L1878). Post-T0-1 the reviewer is IO-bound HTTP, not CPU
work; 8–16 is appropriate. Keep the derived bump as a floor, not a ceiling.

**T0-6. Lower the reviewer's episode budget.** `KUSUDAEMON_ROLE_TIMEOUT`
defaults to 180 s. That is a timeout, not a target: a reviewer episode
still running at 60 s has already failed at something and should fail fast
into the retry rather than hold the wave. Set ~45 s for the reviewer phase
specifically.

### Tier 1 — shrink what review has to judge

**T1-1. Deterministic pre-filter.** The §C1 gates (`headers:std`,
`latex_balanced`, `refs_resolve`, `problems>=N`, `terms_defined`) are
already evaluated for free and shipped warn-only per `v1/gates.py`'s
docstring. Graduate the unambiguous ones — `latex_balanced` and
`headers:std` — from `warn_gates` to `gates`, per the project's own
"ship default-off, measure, then flip" rule (§III.5). Then skip the model
call entirely when all gates pass and every one of the node's `judgment`
items is gate-covered. Zero-call review for the common case.

**T1-2. Two-stage triage → deep review.** Stage 1: one cheap-model call
with a binary schema (`{"suspect": bool, "reason": str}`), ~500 output
tokens. Stage 2: the full `VERDICT_SCHEMA` call, only for flagged nodes.
Tune stage 1 for recall, not precision — over-flagging is the correct
failure mode. Budget goes from N expensive calls to N cheap + ~0.2N
expensive.

Prerequisite: `provider_config.get_model_for_role` accepts a `role_models`
mapping, but `driver._role_provider` never passes one —

```python
return get_model_for_role(role, default_model=self.options.model, run_dir=self.run_dir)
```

so the per-role model mapping in `provider.json` is currently dead. Wire it
through before T1-2; a cheap triage model has nowhere to be configured
otherwise.

**T1-3. Review deltas, not documents, on retry.** On attempt 2+ a node's
artifact typically differs from attempt 1 in one section.
`compute_verdict_digest` already gives the mechanism; store per-section
digests during fan-out and re-review only the sections whose hash changed,
merging cached verdicts for the rest.

**T1-4. De-duplicate the two review layers.** Per-node `review_node` and
`v3/document_review.py`'s passes 1–3 currently both ask about coverage
gaps, duplicate content, and terminology drift — those are three of the
four planted-defect classes in `test_layer1_reviewer_precision.py`. Strip
them from the per-node rubric: the windowed document pass is structurally
better at cross-leaf defects (`v1/reviewer.py`'s own docstring says the
per-node reviewer is "structurally incapable" of seeing them) and costs
≤16 calls flat for N=400 rather than N.

### Tier 2 — grounding, closed-world

**T2-1. Give the reviewer its ground truth.** Pass `contract_text` (already
a parameter of `compute_verdict_digest`, just never supplied) plus the
node's declared inputs — `depends_on` promotions from `manifest.jsonl`, the
spine entry, any `research/` raw findings attributed to the node — into the
review prompt. Fix the digest call site at the same time so a contract
amendment invalidates cached passes (§3). "Does this contradict the
contract" becomes answerable; "is this true about the world" stays out of
scope.

**T2-2. Classify each rubric item `closed` or `open`.** Add the field to
`v6/templates.py:NodeTemplate`'s judgment/rubric defaults. A `closed` item
is checkable from artifact + contract + declared inputs; an `open` item
needs external truth. Send only `closed` items to the reviewer. This is
what actually removes the scraping impulse — not a denylist, but deleting
the question that motivated it.

**T2-3. Route open-world claims to the research path.** `v4/research.py`
probes already have genuine web access and write `raw_finding` files. Have
the writer emit a `claims.jsonl` beside its artifact (assertion + the
`[ref:N]` it rests on), then promote `refs_resolve` from warn to a hard
gate. An uncited factual claim becomes a **deterministic gate failure at
zero model calls**. The reviewer's remaining factual job is only "does the
cited source actually say this," with the source in context. This is
strictly stronger anti-hallucination than a web-enabled reviewer, strictly
cheaper, and unit-testable.

**T2-4. Turn on adversarial sampling.** `RunOptions.review_sample_rate`
exists and defaults to 0.0; when set it re-runs `review_node` at
`temperature=0.7` and logs `reviewer_sampled_disagreement`
(`round_loop.py` ~L353). Set ~0.05 against the strong model, logged and
non-blocking. That is a hallucination canary at 5% of the cost of universal
deep review.

---

## §5 Measurement

`tests/test_layer1_reviewer_precision.py` plus
`tests/fixtures/reviewer_baseline.json` is the right instrument and is
already gated on `KUSUDAEMON_LIVE_REVIEW=1`. Two additions make this plan
falsifiable:

1. **Add `mean_wall_clock_per_call` to the baseline** alongside
   `mean_tokens_per_call`, and assert it as a non-regression floor. Without
   it, every claim in §4 is unverifiable.
2. **Add planted-defect classes `uncited_claim` and `contradicts_contract`**
   to `DEFECT_CLASSES`. These are what Tier 2 exists to catch, and nothing
   in the corpus currently tests for either.

Re-run the `014` / `007` / `057` arm-C triple after Tier 0 alone. The
success criterion for Tier 0 is not a score change — it is arm-C wall clock
falling to within ~10% of arm A's 265.9 s while `calls_by_role` shows the
same call counts, and zero `HTTP 404 from provider` halts. Per
`bench_results/summary.json`'s own caveat, a token-spend delta above ~10%
across arms invalidates the comparison; check `mean_tokens_per_run` before
reading any of it as a harness effect.
