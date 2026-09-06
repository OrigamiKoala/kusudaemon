# PLAN-REVIEW-LATENCY-STATUS.md

Implementation audit of `PLAN-REVIEW-LATENCY.md`, verified 2026-09-06
against commit `214e78a` ("fix reviewer bug") plus the uncommitted working
tree. Every claim below was checked by reading the current source, not the
commit message.

**Headline.** 11 of 14 planned items are implemented, several of them well.
But the single highest-leverage item — T0-1, the transport fix that the
whole plan rests on — is **implemented in code and then disabled by
configuration**. `provider.json` now carries an explicit
`"roles": {"transport": "backend"}`, which routes every role call back
through the ~400 s/call CLI episode path. All the parallelism, delta
caching, and pre-filtering that landed is multiplying a constant that is
still roughly two orders of magnitude too large.

The §C items raised after the plan was written (templates never reaching
T0/T1, the four-shape vocabulary, the missing contract→rubric producer, the
silent empty-judgment skip) are **entirely unimplemented**. On the tier
HarnessBench actually runs at, there is still no model review at all.

---

## §1 Status table

| Item | Status | Evidence |
| --- | --- | --- |
| T0-1 provider config + pin `http` | **Regressed** | `provider.json` sets `roles.transport = "backend"` |
| T0-2 `role_transport_degraded` event | Done | `roles/factory.py` |
| T0-3 hoist adapter out of retry loop | Done | `backend_provider.py` L125–128 vs L137 |
| T0-4 parallel fan-out | Done | `reviewer.py` L508 `ThreadPoolExecutor` |
| T0-5 raise `max_parallel` | Done (scoped) | `driver.py` L1882 |
| T0-6 reviewer episode budget | Done | `KUSUDAEMON_REVIEWER_TIMEOUT` |
| T1-1 deterministic pre-filter | Done | `GATE_COVERED_JUDGMENTS` + graduated gates |
| T1-2 two-stage triage | **Built, not wired** | `_call_triage` has no caller |
| T1-3 delta review on retry | Done | `ReviewVerdict.section_verdicts` |
| T1-4 de-duplicate review layers | Done | `CROSS_LEAF_JUDGMENTS` |
| T2-1 ground the reviewer | Done | `contract_text` + `declared_inputs` |
| T2-2 closed/open rubric split | Done (inert) | `judgment_classification`; no `open` items exist |
| T2-3 claims + `refs_resolve` gate | **Partial** | prompt asks for claims; nothing validates them |
| T2-4 adversarial sampling | Done | `review_sample_rate` default `0.05` |
| §5 baseline additions | Done | wall-clock + 2 new defect classes |
| C-1 templates reach T0/T1 | **Not started** | — |
| C-2 non-document shape | **Not started** | — |
| C-3 contract→rubric producer | **Not started** | — |
| C-4 `review_skipped_no_judgment` | **Not started** | — |

---

## §2 What landed, and how well

### §2.1 Transport (T0-2, T0-3)

`_resolve_role_transport` now takes `log` and `run_dir`, tracks a
`degradation_reason` (`no_api_key` / `no_base_url` / `resolve_failed`) and
appends a `role_transport_degraded` event. This is exactly T0-2 and it is
the right shape — the reason is discriminated, and the log is derived from
`run_dir` when not passed. A silent 100x degradation is no longer possible.

`BackendRoleProvider.complete_json` builds its adapter once at L125–128,
before `for attempt in range(retries + 1)` at L137. T0-3 done.

### §2.2 Fan-out and concurrency (T0-4, T0-5, T0-6)

`review_node` grew a `parallel: bool = True` parameter and dispatches
sections through a `ThreadPoolExecutor` with `as_completed`. The serial
`for section_text in sections` loop is gone.

`driver.py` L1882 now derives `min(16, max(8, (os.cpu_count() or 1) * 2))`.
Note this is still gated on the same two preconditions as before —
`options.max_parallel == 1`, tier in `("T2", "T3")`, and a dependency-free
tree. `RunOptions.max_parallel` still defaults to `1`, and T0/T1 runs are
untouched. That is defensible, but it means the bump does not apply to the
HarnessBench runs in §4.

`BackendRoleProvider` reads `KUSUDAEMON_REVIEWER_TIMEOUT` ahead of
`KUSUDAEMON_ROLE_TIMEOUT`, and `driver._role_provider` sets `timeout = 45.0`
for `reviewer` and `triage` roles against 300.0 elsewhere. T0-6 done, and
done more precisely than the plan asked for.

### §2.3 Grounding (T2-1) — the best of the changes

`round_loop.review_and_transition_node` now reads `contract.md` via
`contract_path(run_dir)` and assembles a `declared_inputs_str` from the
node's research findings (truncated to 2000 chars each). Both are passed
into `review_node` and reach `_call_reviewer`, which appends
`Contract:\n{contract_text}` to the user message.

Critically, **the cache correctness bug is fixed**: `compute_verdict_digest`
now receives `contract_text` at both call sites — L236 (the cache read) and
L881 (`_write_audit`). An amended contract now invalidates a cached pass,
which it did not before.

### §2.4 Pre-filter and gate graduation (T1-1)

Three §C1 warn-gates graduated to hard gates, per the plan and per the
project's own "measure, then flip" rule:

- `_PROBLEM_SET`: `headers:std` warn → hard
- `_DERIVATION`: `headers:std`, `latex_balanced` warn → hard
- `_REFERENCE`: `headers:std`, `refs_resolve` warn → hard
- `_PROSE`: `headers:std` added as hard

`review_node` then short-circuits when gates pass and every remaining
judgment item is in `GATE_COVERED_JUDGMENTS` or in `node.gates`.

### §2.5 Delta review (T1-3)

`ReviewVerdict` gained `section_verdicts`; `_write_audit` persists them
under `"sections"`; `review_and_transition_node` reads them back as
`cached_sections` and passes them in; `review_node` builds a
`digest → entry` map and reuses matching sections. Complete and coherent.

### §2.6 Measurement (§5)

`reviewer_baseline.json` gained `mean_wall_clock_per_call: 10.0` and
per-class recall entries for `uncited_claim` and `contradicts_contract`;
`DEFECT_CLASSES` in `test_layer1_reviewer_precision.py` matches.
`tests/test_v1_reviewer_fanout.py` is new (157 lines).

---

## §3 Gaps

### §3.1 T0-1 is configured off — the blocking issue

`provider.json`:

```json
{ "roles": { "transport": "backend" }, ... }
```

The code default was correctly flipped (`desired_transport` now defaults to
`"http"`), and the junk `"n"` model id was removed from
`gptme.providers.nvidia.models`. Both good. But the config then explicitly
selects `backend`, which is the path the entire plan exists to get off.

**There is also a precedence regression in the new branch order:**

```python
desired_transport = cfg_transport.lower() if cfg_transport else "http"
if desired_transport == "backend":
    transport = "backend"
elif effective_backend == "gptme":
    transport = "http"
```

The `backend` short-circuit is now checked *before* the gptme branch.
Previously `gptme` was unconditionally `http` — it speaks the
OpenAI-compatible protocol directly and has no CLI role path worth taking.
Now a config-level `transport: "backend"` drags gptme along with it. This
is what breaks `test_role_adapter_matrix.test_resolve_role_transport`
(`('gptme', 'backend') != ('gptme', 'http')`).

Fix: make the gptme branch win, and treat `cfg_transport` as a preference
for non-gptme backends only.

### §3.2 T1-2 triage is dead code

`TRIAGE_SCHEMA`, `_TRIAGE_SYSTEM_PROMPT`, `_call_triage`, and
`review_node(triage_provider=...)` all exist and look correct. Nothing
passes `triage_provider`. `round_loop`'s `_do_review()` builds its kwargs
as:

```python
kwargs = dict(
    contract_text=..., declared_inputs=..., cached_sections=...,
    judgment_classification=...,
)
```

— no `triage_provider`. A repo-wide grep finds no other caller. So stage 1
never runs and every reviewed node still takes the full `VERDICT_SCHEMA`
path.

The prerequisite the plan flagged is now satisfied: `driver.py` L2502–2518
builds a `role_models` map from `roles.models` / `roles.role_models` /
bare `roles.<role>` keys and passes it to `get_model_for_role`. So a cheap
triage model is configurable; it is only the call-site plumbing that is
missing. `driver._role_provider` already special-cases a `"triage"` role in
its timeout selection, which suggests this was intended and left unfinished.

### §3.3 T2-2 is inert

The mechanism is complete — `NodeTemplate.judgment_classification`,
populated on all three judgment-bearing templates, threaded through
`round_loop` into `review_node`, which filters
`jc.get(j, "closed") != "open"`. But **every existing entry is `"closed"`**,
so the filter currently removes nothing. It will start mattering the moment
an `"open"` item is authored; until then it is scaffolding, not a
behavioural change.

### §3.4 T2-3 is half a loop

`pipeline/prompts.py` L171–174 now instructs the writer to record factual
claims in `<node>_claims.jsonl` with `assertion` and `ref` fields. Nothing
reads that file. There is no `claims_resolve` gate, no `uncited_claim`
handler in `v1/gates.py`, and `refs_resolve` was graduated to a hard gate
only on `_REFERENCE` — the one shape least likely to carry open-world
factual claims.

So `reviewer_baseline.json` now scores recall on an `uncited_claim` defect
class that no deterministic check enforces. The plan's argument was that an
uncited claim should be a **gate failure at zero model calls**; right now it
is a writer instruction with no consumer.

### §3.5 §C entirely unimplemented

Re-verified against current source:

- **C-1**: `v6/direct.py:build_direct_node` still hardcodes
  `gates=["nonempty", f"max_tokens:{token_budget}"]`, sets no `shape`, and
  never calls `apply_template_to_node`. T0/T1 nodes still carry empty
  `judgment` by construction.
- **C-2**: `v2/planner.py` L43 is unchanged —
  `["prose-dominant", "derivation-dominant", "problem-set-dominant",
  "reference-dominant"]` — and the registry still holds exactly five
  templates. `forced_leaf` still hardcodes `shape="prose-dominant"`.
- **C-3**: `templates.py` L291/L295 remain the only writers of
  `node.judgment` / `node.rubric` outside tree deserialization.
  `render_spec_rubric_to_contract` still writes only to disk.
- **C-4**: no `review_skipped_no_judgment` event exists anywhere.
  `review_node`'s `if not node.judgment: return ReviewVerdict(...)` is
  still silent, and now there is a **second** silent early return — the
  `if not effective_judgment` path added by T1-4/T2-2, which can now
  zero out a node's judgment list that templates *did* populate.

C-4 has therefore become more important, not less: there are now three
distinct ways for a node to pass review without a model call (empty
judgment, everything filtered out, gate-covered pre-filter), and
`audit/<node>.json` records all three identically to a real reviewed pass.

---

## §4 Test status

`python3 -m unittest discover -s tests -p "test_*.py"` — **1144 tests, 3
failures, 1 skipped** (111.9 s).

1. `test_role_adapter_matrix.test_resolve_role_transport` —
   `('gptme', 'backend') != ('gptme', 'http')`. Caused by §3.1.
2. `test_token_efficiency_audit.test_resolve_role_transport_with_keys` —
   `('opencode', 'backend') != ('opencode', 'http')`. Same cause.
3. `test_backends_claude_codex.test_mcp_server_overrides` — **unrelated**.
   `adapters/codex.py:mcp_server_overrides` returns `[]` when neither
   `tomllib` nor `tomli` imports. This interpreter is Python 3.10.12
   (no stdlib `tomllib`) with `tomli` not installed. Not a code defect;
   it passes on 3.11+.

**Test-isolation defect worth fixing regardless.** Failures 1 and 2 exist
because those tests call `_resolve_role_transport` with no config override,
so they read the operator's real `provider.json` from the invoking cwd.
CLAUDE.md advertises the suite as requiring "no network, no agent binary,
no API key required" — it should also require no local provider config.
Both tests should pass an explicit `config_path` to a fixture, or the
`KUSUDAEMON_ROLE_TRANSPORT` env var should be cleared and a temp config
injected. As written, a routine config change silently reddens the suite.

---

## §5 Uncommitted work in the tree

Not part of this plan, but it interacts with it. 13 files modified,
+557/−29.

- **`roles/json_io.py` (+234)** — `_unwrap_schema_echo` and
  `_repair_common_schema_omissions`, with named repair cases for
  `REVIEW_SCHEMA` / `DOC_REVIEW_SCHEMA`, `ESTIMATE_SCHEMA`,
  `INTAKE_SCHEMA`, `PARTITION_SCHEMA` and **`TRIAGE_SCHEMA`**. This is
  host-side JSON salvage — the §2.3 mitigation for the backend transport's
  lack of `response_format`. The presence of a `TRIAGE_SCHEMA` case
  confirms §3.2 was meant to be wired.
- **`adapters/_agent_worker.py` (+166)** — `_is_opencode_fatal_error`,
  fail-fast on fatal opencode output lines rather than burning the episode
  budget to timeout. Complements T0-6.
- **`provider_config.py` (+19)** — `normalize_opencode_model`, applied both
  when validating against `declared_models` and to the resolved model.
  Matches the new `nvidia/nvidia/nemotron-3.5-lightning-30b-a3b` entry in
  `provider.json`.

Read together, this uncommitted work is investment in making the **backend**
transport survivable. That is a reasonable hedge, but it should not be
mistaken for T0-1: salvaging JSON out of a CLI episode still costs a process
spawn and an agentic loop. It lowers the failure rate, not the latency.

---

## §6 Recommended order from here

1. **Unset `roles.transport`** in `provider.json` (or set `"http"`), and fix
   the branch-order regression in §3.1 so gptme is never dragged onto the
   CLI path. This is one config line plus a three-line reorder, and it is
   worth more than everything else on this list combined. Two test failures
   clear with it.
2. **Fix the test isolation** in §4 so the suite stops depending on the
   operator's `provider.json`.
3. **Wire `triage_provider`** through `round_loop`'s `_do_review()` kwargs
   (§3.2). Everything else for T1-2 already exists, including the model
   mapping and the timeout special-case.
4. **C-4** — emit `review_skipped_no_judgment` with a `reason`
   discriminating the three skip paths. Cheapest item here, and it is what
   makes every remaining question in this document answerable from
   `events.jsonl` instead of by source reading.
5. **C-1** — one call to `apply_template_to_node` in `build_direct_node`.
   Until this lands, T0/T1 runs have no model review regardless of anything
   above.
6. **C-2** — a `procedure-dominant` shape with real judgment items. This is
   what makes review meaningful for HarnessBench rather than for textbooks.
7. **T2-3's missing half** — a gate that reads `<node>_claims.jsonl` and
   fails on an uncited assertion, so the `uncited_claim` baseline entry
   measures something.
8. **C-3** — the contract→rubric producer, the general form of C-2.

Re-run the `014` / `007` / `057` arm-C triple after step 1 alone. Per
`PLAN-REVIEW-LATENCY.md` §5, the success criterion is wall clock falling
toward arm A's 265.9 s at unchanged `calls_by_role` and zero
`HTTP 404 from provider` halts — not a score change.

---

## §7 Revising C-1/C-2 into a single gated change

Added 2026-09-06 after tracing what a template assignment actually does to
a T0/T1 node, and what a new `_SHAPES` entry does to T2/T3.

### §7.1 C-1 as originally written is a no-op

`v6/direct.py:build_direct_node` sets no `shape`, so
`template_for("", "generic")` falls through every `template.shapes` and
every `template.types` check and returns `_GENERIC`, which contributes
nothing. Calling `apply_template_to_node` on a direct node changes nothing.

The real decision is therefore not "apply templates to T0/T1" but **"which
shape does a short-horizon node get"** — and that assignment carries three
costs, only one of which is latency.

### §7.2 The three costs, in increasing severity

**(a) One extra reviewer call — cheap.** T0 goes from 0 model reviews to 1.
Post-T0-1 that is 2–5 s against a writer episode of tens of seconds.
T1-1's pre-filter absorbs some of it, but not much: the three real template
judgment items (`worked_examples_reachable`, `derivation_self_consistent`,
`every_term_defined_once`) are semantic and deliberately excluded from
`GATE_COVERED_JUDGMENTS`. Still one call. This is the acceptable part.

**(b) `headers:std` as a hard gate — a correctness regression.** Every
judgment-bearing template now carries `headers:std` in `gates` (graduated
in `214e78a`). `_gate_headers_std` fails with `"no markdown headings
found"` on an artifact with none. A short T0 answer — one line of prose, a
shell invocation, a small JSON payload — has no headings, so:

```
hard gate fails -> _transition_after_writer bumps attempts -> re-dispatch
DIRECT_MAX_ATTEMPTS = 2 -> two full writer episodes -> node "blocked"
```

A task that completes in one episode takes two and then fails. Not a
slowdown — a broken run.

**(c) Tool narrowing — a second correctness regression.**
`apply_template_to_node` sets `node.tools` only when the node carries none,
and T0/T1 direct nodes carry none by construction.
`gptme_adapter.DEFAULT_TOOL_ALLOWLIST` is
`("shell", "read", "save", "patch")`; `_PROSE.tools` is `("read", "save")`.
Assigning `prose-dominant` to a T0 node therefore strips `shell` and
`patch` from the writer. For agentic short work — what T0 exists for —
that is fatal. `apply_template_to_node`'s own docstring already names this
invariant ("T0/T1 direct nodes ... keep the adapter's
`DEFAULT_TOOL_ALLOWLIST` fallback"); the original C-1 would have undone it.

### §7.3 One worry that proved unfounded

An earlier draft of this section argued that a template with `gates=()`
would be a model-reachable gate bypass. **It is not.**
`v2/planner.py:add_leaf` sets

```python
gates = [*default_gates, f"max_tokens:{token_budget}"]   # default_gates = ("nonempty",)
```

*before* calling `apply_template_to_node`, and the merge is pure-additive.
Every planner-built leaf therefore carries `nonempty` + `max_tokens:N`
regardless of its template, and a template can only add to that floor.
`gates=()` means "no gates beyond the floor," never "no gates."

### §7.4 The revised change

C-1 and C-2 become one change, and the short-horizon template is **not**
model-selectable:

- **Add `_DIRECT = NodeTemplate(name="direct", shapes=(), types=(), ...)`**
  — no `shapes` entry, so `template_for` can never resolve it and the
  planner can never select it. Reached only via
  `apply_template_to_node(node, template=_DIRECT)`, which the existing
  keyword-only `template` parameter already supports.
  - `gates=()` — no `headers:std`. Fixes §7.2(b). The `nonempty` +
    `max_tokens` floor still applies (§7.3).
  - `tools=()` — explicit no-opinion, so the `DEFAULT_TOOL_ALLOWLIST`
    fallback survives. Fixes §7.2(c).
  - `judgment=(...)` + `rubric=(...)` + `judgment_classification=(...)`
    with the semantic items. This is the only thing C-1 actually wanted.
- **Call it from `build_direct_node`**, passing `template=_DIRECT`
  explicitly rather than relying on shape resolution.
- **Gate it.** T0 is by definition the "this is trivial, do not spend"
  tier. Add a `RunOptions` flag defaulting **on for T1, off for T0**.
  `phases_for("T0")` is `classify/execute/verify` and `verify` is already
  T0's dedicated review step, so the hook exists.

Net effect on short-horizon runs: +1 reviewer call on T1, none on T0 by
default, no gate changes, no tool changes.

### §7.5 Medium- and long-horizon impact

**The §7.4 change cannot reach T2/T3 at all.** `_DIRECT` has an empty
`shapes` tuple, so `template_for` never returns it; `build_direct_node` is
called only from `v6/direct.py`'s T0/T1 paths. T2 and T3 are untouched by
construction. This is the whole reason for preferring an explicit
`template=` argument over a fifth `_SHAPES` entry.

**The separate question — adding `procedure-dominant` to `_SHAPES` for
T2/T3 ops leaves — has a wider blast radius than templates, and it lands
on exactly the medium/long-horizon tiers.** `node.shape` has five
consumers beyond the template registry:

| Consumer | Effect of a fifth shape |
| --- | --- |
| `v2/pilot.py:select_pilot_nodes` | Returns **one pilot node per distinct shape**. A fifth shape = a fifth pilot episode + a fifth operator approval + more contract-rule elicitation, on every run that uses it. |
| `v3/document_review.py` (depth pass) | Reuses `select_pilot_nodes` — the "≤4 shape-median nodes" becomes ≤5. One more artifact-opening review call per document review. |
| `v4/probe_planner.py` | `_NEEDS_PROBE_SHAPE_RE` matches `problem-set\|derivation\|reference\|code\|api\|specification`. `procedure-dominant` does **not** match, so those leaves fall through to the `_BRIEF_LOOKUP_RE` brief-content fallback. Probably right for ops work — but naming the shape `specification-dominant` instead would silently start scheduling research probes. |
| `v2/contract.py` | Groups `ContractRule`s by shape — one more group, tolerant. |
| `eval/`, `dashboard/` | `approval_rate_by_shape`, node rendering — tolerant. |

So the recurring medium-horizon cost of adding the shape is roughly
**+1 pilot episode, +1 approval, +1 depth-review call per run**, plus
whatever the leaves themselves now review. That is real but bounded, and it
buys the first shape whose judgment items describe non-document work.

**Recommended split, given the stated priority on long horizon:**

1. **Land §7.4 first.** It is unreachable from T2/T3, so it carries zero
   medium- or long-horizon risk, and it is what makes T0/T1 reviewable at
   all.
2. **Treat `procedure-dominant` in `_SHAPES` as a separate, measured
   change.** Land it behind the same `RunOptions` flag, run the `014` /
   `007` / `057` arm-C triple with and without, and read the pilot-episode
   count out of `calls_by_role` before keeping it.
3. **C-3 (the contract→rubric producer) remains the better long-horizon
   answer** and should outrank step 2. A new shape gives judgment items to
   leaves the planner happens to label correctly; C-3 gives them to every
   leaf the contract scopes a rule to, whatever its shape. For T3 in
   particular that is the difference between reviewing some leaves and
   reviewing the ones the contract actually constrains.

### §7.6 An already-shipped medium-horizon change worth watching

`214e78a` graduated `headers:std` to a **hard** gate on `_PROSE`, which is
the modal shape for T2/T3 leaves. Every prose leaf in a medium- or
long-horizon run must now carry at least one markdown heading with no
skipped levels, or it fails a blocking gate and burns an attempt.

For chapter-scale document work that is correct and intended. It is worth
confirming it does not fire on short T2 leaves — a 200-word bridging
section that the planner emitted without a heading would now block where it
previously passed. Check `audit/<node>.json` for `headers:std` failures on
the next T2 run before assuming this is free.

---

## §8 Classifier-driven phase and review bypass

Decided 2026-09-06. **This section is half rejection, half queued work.**
§8.3 records a mechanism we are deliberately not building; §8.4 records the
substitute, which *is* to be implemented — at lower priority than §6's
steps 1–4. Read the status marker on each item; do not read the section
heading as "rejected" and skip it.

### §8.1 The request

Make the harness flexible: let the classifier recognize that a given task
does not need a particular phase, or needs only an abbreviated review, and
skip accordingly. Stated constraint: **not at the cost of long-horizon
work.**

### §8.2 This already exists, with one line drawn deliberately

`v6/tiering.py`'s module docstring states the existing contract:

> One bounded, advisory model call (`estimate_scope`), mapped into a tier
> by a pure code table (`classify`). **The model estimates; the harness
> decides**; every cap is code, never a model's opinion about whether
> something "feels too big."

The adaptivity is already there:

- `estimate_scope` — one capped `complete_json` call that never sees file
  contents (a `top_dirs` digest plus a content-free path listing).
- `_classify_raw` / `classify` — a pure table, first match wins.
- `phases_for` — a dict lookup, "computed by code from a tier, never
  chosen by a model and never fixed at seven" (invariant 8).
- Conditional phases already skip: `intake` and `explore` remain in the
  phase tuple but short-circuit to **logged** no-ops when `needs_intake` /
  `needs_explore` are false, decided once at classify time and read back
  out of `tier.json` (`driver.py` L990/998 → L1103/1384). The docstring
  notes this mirrors the existing `_phase_done` idiom rather than
  "inventing a second, parallel skip mechanism."

So the proposal is not "add adaptivity." It is "move the decision from the
code table to the model."

### §8.3 REJECTED — giving the classifier bypass authority

Three reasons, all of which bite hardest at long horizon.

**(a) The classifier is most confident exactly where it is least
reliable.** At classify time, "genuinely simple" and "not yet understood"
are indistinguishable — a goal looks small precisely when its scope has not
been grasped. The current table already encodes the counter-rule:

```python
if estimate.files_touched == "unknown" and _TIER_RANK[tier] < _TIER_RANK["T2"]:
    return "T2"
```

*"an estimator that cannot tell is exactly the case that needs
exploration."* A bypass-authorized classifier deletes that guardrail at the
one point it is load-bearing.

**(b) The error does not recover.** Invariant 9 makes escalation one-way,
and `escalate()` enforces monotonicity for *every* trigger — it raises on
an unknown trigger rather than silently no-op'ing, so a typo cannot
masquerade as a harmless escalation. That asymmetry exists because
down-tiering is unsafe. A skipped phase has no inverse: raising a run's
tier mid-flight does not retroactively run `plan` for leaves already
executed.

**(c) Damage compounds with horizon.** A bad skip at T0 costs one episode.
A bad skip at T3 propagates through every leaf and surfaces at assemble.
The risk concentrates exactly where the stated priority is.

Given the constraint in §8.1 — *"if this degrades long-horizon work, we
will not implement it"* — this is a rejection on the requester's own terms,
not an outside objection.

### §8.4 ACCEPTED — the substitute (queued, post-§6-step-4)

The governing rule, which every item below satisfies:

> **A model may supply evidence, and may collapse other model calls. Only
> code may lower a floor.**

**§8.4a — Widen `ScopeEstimate`, never `classify`'s authority.** *(queued)*
Add fields the model fills in — a `work_kind` (`document` / `procedure` /
`code-edit`), additional structural signals — and consume them in
`_classify_raw`. The model supplies evidence; the table maps evidence to a
tier. Fully extensible, no new risk surface, and it is the honest version
of "the classifier should realize."

**§8.4b — Extend the `needs_*` no-op pattern to the remaining phases.**
*(queued)* This is the real answer to "a task that doesn't need a phase."
The pattern is already proven by `needs_intake` / `needs_explore`: a
disk-backed, **code-checkable** condition, decided once at classify time,
logged when it fires, phase left in the tuple. The concrete gap worth
closing: `research` runs unconditionally at T3 even when
`v4/probe_planner.py` would return zero probes. That is genuine rigidity,
and it is fixable by this mechanism without any model authority.

**§8.4c — Wire T1-2's triage.** *(queued — already §6 step 3)* This *is*
the requested "sped-up review," and it is already designed correctly: the
model returns `{"suspect": false}` and the expensive `VERDICT_SCHEMA` call
is skipped, but the gates underneath still run. The model collapses model
work and cannot touch gate work. It is the reference implementation of the
§8.4 rule and it only needs a call-site kwarg (§3.2).

**§8.4d — Keep model-driven adjustment monotone.** *(policy, no code)* A
model may request *more* review, never less — the shape `escalate()` and
`review_sample_rate` already have. Any future model-facing knob should be
built this way.

**§8.4e — Genuine override stays with the operator.** *(already built)*
`pipeline/bypass.py` provides file-based, per-node, per-process bypass
(`is_node_bypassed(run_dir, node_id, "review")`), written to disk and
logged to `events.jsonl`, plus `RunOptions.disable_review`. A human taking
auditable responsibility is categorically different from a model deciding
silently at classify time. "I know this task doesn't need review" belongs
here, and it already works.

### §8.5 Priority

§8.4a and §8.4b are real work and should be built — but **after** §6 steps
1–4, and after the arm-C re-measurement. Neither is on the critical path
for review latency: the transport fix (§3.1) dominates both by two orders
of magnitude, and §8.4c is already counted as §6 step 3.

---

## §9 Give `_PROSE` judgment items: on-topic and supported claims

Requested 2026-09-06: prose leaves should be judged for **factual
information that stays on-topic**. Accepted, with one reframing that
matters and two prerequisites that are not optional.

### §9.1 This is two requests, and they have different safety profiles

**"Stays on-topic" is closed-world.** Whether an artifact addresses the
brief it was dispatched for is answerable from context the harness already
owns — the node's brief and the frozen contract. Nothing needs looking up.
Safe to add as a judgment item today.

**"Factual information" is open-world in its naive form, and that is the
exact failure this whole document exists to fix.** A rubric item reading
"claims are factually correct" gives the reviewer a question it cannot
answer from context, so it either rubber-stamps or invents — and, on a
CLI-backed transport, reaches for the web to try. §3 of this document and
the original scraping incident are both this bug.

The reframing: **"supported by context" is closed; "true about the world"
is open.** Same subject, entirely different cost and safety profile. A
prose leaf's factual bar should be *"every factual claim traces to a
declared input, the contract, or is explicitly marked as an assumption"* —
checkable against material now in the reviewer's context post-T2-1 — never
*"every factual claim is true."*

### §9.2 The template change

```python
_PROSE = NodeTemplate(
    name="prose",
    shapes=("prose-dominant",),
    gates=("headers:std",),
    warn_gates=(),
    tools=("read", "save"),
    judgment=("on_topic", "claims_supported"),
    judgment_classification={
        "on_topic": "closed",
        "claims_supported": "closed",
    },
    rubric={
        "on_topic": (
            "Every section addresses this leaf's stated brief; material "
            "that belongs to another leaf's brief, or to no brief, is a "
            "defect. Judge against the brief as written, not against what "
            "the topic could plausibly cover."
        ),
        "claims_supported": (
            "Every factual claim traces to a declared input, to the "
            "contract, or is explicitly marked as an assumption. A claim "
            "with no such support is a defect. Do NOT judge whether a "
            "claim is true in the world -- only whether this artifact "
            "shows where it came from."
        ),
    },
)
```

The last sentence of `claims_supported` is load-bearing, not decoration. It
is what keeps the item closed-world, and it should survive any future
rewording of the rubric text.

Neither id collides with `GATE_COVERED_JUDGMENTS` (so the T1-1 pre-filter
will not silently skip them) or with `CROSS_LEAF_JUDGMENTS` (so T1-4 will
not strip them). That second point is worth stating explicitly because
`on_topic` sits adjacent to `register_drift`, which *is* cross-leaf: the
distinction is that `on_topic` asks whether **this leaf** matches **its
own** brief, while `register_drift` asks whether leaves agree with **each
other**. The first is per-node and belongs here; the second is
`v3/document_review.py`'s job and must stay there.

### §9.3 Prerequisite: the reviewer cannot currently see the brief

`_call_reviewer` assembles its user message from `rubric_lines`,
`contract_text`, `declared_inputs`, and `artifact_text`. **`node.brief`
is not among them.** As written today, `on_topic` would ask the reviewer to
judge topicality against a brief it has never been shown — a rubric item
that is structurally unanswerable, which is exactly the class of defect
§3 documented.

Required change, alongside §9.2:

- Add a `brief: str = ""` parameter to `_call_reviewer` and `review_node`,
  rendered as its own `Brief:\n{brief}` block ahead of `Artifact:`.
- Pass `node.brief` from `round_loop.review_and_transition_node`, beside
  the existing `contract_text` / `declared_inputs` wiring.
- Include it in `compute_verdict_digest`'s payload so an amended brief
  invalidates a cached pass, matching what T2-1 did for the contract.

`_call_triage` needs the same parameter for the stage-1 call to be
meaningful.

### §9.4 Cost: this is the change that actually turns review on

`_PROSE` is the **modal** shape for T2/T3 leaves, and `v2/planner.py`'s
`forced_leaf` hardcodes `shape="prose-dominant"` for every depth-cap and
single-unit leaf. So unlike the three existing judgment-bearing templates —
which fire only on textbook-shaped leaves the planner rarely selects — this
change moves the typical T2/T3 run from roughly **zero** model reviews to
**one per leaf**.

That is the intended effect. It is also a step change in review spend, and
it makes two items that were previously optional into hard prerequisites:

1. **§6 step 1 (the transport fix) is mandatory before this lands.** At
   ~400 s per role call on the current `roles.transport = "backend"`
   config, one reviewer call per prose leaf on a 40-leaf T3 run is over
   four hours of pure review. On HTTP it is a couple of minutes.
2. **§6 step 3 (wiring T1-2 triage) stops being optional.** Without it
   every prose leaf takes a full `VERDICT_SCHEMA` call. With it the run is
   N cheap triage calls plus roughly 0.2N deep calls. Given that `_PROSE`
   is modal, this is where the triage stage earns its existence.

`review_sample_rate` at its new 0.05 default also now samples a much larger
population, which is a benefit — the calibration signal gets real data for
the first time — at 5% of the added cost.

### §9.5 What this does not do

`claims_supported` only has teeth once something produces the support it
checks for. Today `pipeline/prompts.py` instructs the writer to record
claims in `<node>_claims.jsonl`, and **nothing reads that file** (§3.4).
Until T2-3's missing half lands — a gate that fails an artifact whose
claims file does not cover its assertions — `claims_supported` rests
entirely on model judgment over `declared_inputs`, with no deterministic
floor beneath it.

That is still strictly better than today's silence, and it is a reasonable
thing to ship first and tighten after. But it means the honest ordering is:
§9 gives the reviewer the right *question*; T2-3 gives the harness a
code-checkable *answer* for the subset of that question a gate can decide.
Both are wanted; §9 does not replace T2-3.

### §9.6 Applies to `_DIRECT` too

§7.4's `_DIRECT` template (T0/T1, not model-selectable) should carry the
same two judgment items and the same classifications. The request —
factual, on-topic output — is not tier-specific, and T0/T1 are precisely
the tiers with no review at all today. `_DIRECT` keeps `gates=()` and
`tools=()` per §7.4; only the judgment/rubric block is shared.

### §9.7 Order

1. §9.3's brief plumbing (prerequisite, small, no behaviour change alone).
2. §6 step 1 — transport. Non-negotiable before §9.2.
3. §6 step 3 — triage wiring.
4. §9.2's `_PROSE` change, and the same block on `_DIRECT` per §9.6.
5. Re-measure `tests/test_layer1_reviewer_precision.py` — `on_topic` and
   `claims_supported` want planted-defect classes of their own, alongside
   the `uncited_claim` / `contradicts_contract` entries already added to
   `reviewer_baseline.json` in §5.

---

## §10 Follow-up fixes (2026-09-06, working tree on top of `214e78a`)

Re-audited every STATUS gap plus every PLAN-WORKSPACE-MODE.md §K/§R item
against the working tree. Most of §§1–9's "missing" rows had already landed
uncommitted (transport pin, triage wiring, C-4 event, `_DIRECT`, brief
plumbing, `_PROSE` judgments, K0/K1/K3/K4a/K5, `_CODE` shape). The items
below are what was still genuinely open, and what was done about each.
Covered by `tests/test_plan_followups.py` (24 tests); full suite
1189 tests green.

**Fixed (bugs / mechanical leftovers):**

1. **T0-6 timeout never reached the backend path.** `make_role_provider`
   accepted `timeout` but dropped it for `BackendRoleProvider`, so
   `driver._role_provider`'s 45 s reviewer/triage budget only applied to
   HTTP. `BackendRoleProvider` now takes `timeout: float | None = None`
   (explicit wins over env; `None` preserves the 120 s / 300 s defaults),
   and `make_role_provider` threads it through (`roles/factory.py`,
   `roles/backend_provider.py`).
2. **K4b log missing on the common path.** `_hidden_paths_and_exceptions_for_probe`
   now emits `probe_finding_path_unreachable` on the sibling-run-dir
   `ValueError` branch too (absolute fallback + reason), not just `OSError`
   (`pipeline/backends.py`).
3. **K1 floor was a magic literal.** `PLAN_MIN_WORKSPACE_TOKENS = 2_000`
   extracted to `v6/work_object.py`, shared with `tiering._T1_WORK_TOKENS_CEILING`
   by value, used at both driver sites (`_plan_will_partition`, the
   `single_node_tree_small_workspace` event).
4. **K6 workspace fallback was missing.** `build_writer_adapter` now forces
   `DEFAULT_TOOL_ALLOWLIST` when `is_workspace` is true (prose/reference
   template opinions no longer deny bash on repo tasks); the dashboard's
   `_runtime_for` factory passes the flag too (`pipeline/backends.py`,
   `dashboard/state.py`).
5. **§8.4b `needs_research`.** `tier.json` carries `needs_research`
   (T3 with an explicit or auto probe plan), `_phase_research`
   short-circuits to a logged `phase_skipped` no-op when it is false, and
   the empty-plan path logs `zero probes` instead of returning silently
   (`pipeline/driver.py`).
6. **§8.4a `work_kind`.** `ScopeEstimate.work_kind`
   (`document`/`procedure`/`code-edit`/`unknown`, default `unknown`) plumbed
   through both schemas (optional, not required), both system prompts, both
   parsers, the backend-transport JSON salvage, and `tier.json` via
   `asdict`. Sole table consumption is monotone: explicit `procedure`
   floors at T1 with a `tier_work_kind_floor` event; every other value —
   and any `unknown` — leaves the table result untouched, so the model can
   never lower a floor (`v6/tiering.py`, `roles/json_io.py`).

**Verified coherent, no change:** K2's `_DIRECT`/`_direct_template` duality
is the §7.4 default plus the §K2/§R4 flag-gated steps 4/5 (gate half and
tools half separately flippable); `merge_template_into_tree` runs only on
the planner branch, so the fallback path never re-acquires `headers:std`.

**Deliberately still open (need measurement or design, per §§6–8
sequencing — single-variable cells, measure then flip):**
`procedure-dominant` in `_SHAPES` (§6.6/§7.5 step 2; `_CODE` from §K6
already covers non-document work, and a sixth shape costs +1 pilot
episode per §7.5), the C-3 contract→rubric producer (§6.8, ranked last),
and `claims_resolve` auto-wiring onto builtin templates (§6.7 — the gate
handler itself is correct and stays available as explicit opt-in).
