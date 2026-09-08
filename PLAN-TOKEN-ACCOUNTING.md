# PLAN-TOKEN-ACCOUNTING.md — replace the token heuristic, and tell agents how big things are

Companion documents: `PLAN-BENCH-INTEGRITY.md` (what makes a number mean
something; §1.4 is corrected by §I2 below), `PLAN-CONCURRENCY-AND-SHARED-STATE.md`
§A4 (arm A token capture) and §B1 (`max_parallel` derivation; §H6 explains why it
has never engaged), `BENCHMARKING.md` §0.4 (record schema).

Three workstreams, deliberately kept separable — one is a correctness fix, one
is an unproven capability bet, and the third was added after the 100-floor
arm-C run made it unavoidable:

- **§A–§C — accounting.** `estimate_tokens` is wrong by a median factor of
  1.66x with a 6.7x interquartile spread. Replace it, re-derive every threshold
  calibrated against it, re-baseline.
- **§D–§F — disclosure.** Put input sizes in the writer's prompt so a leaf can
  decide *whether* to read a file. Context-window disclosure stays behind a
  default-off flag until an A/B says it helps, because it argues with a design
  invariant.
- **§H–§O — output size.** Every size the harness measures is an *input* size:
  the spine is chunked from the corpus, `leaf_gate` compares input tokens to
  budget, and no planner leaf knows how many units it owes. A prompt asking for
  100 documents plans as one 432-token leaf. §H is the run that shows it; §I–§M
  are the fixes. This is in *this* file rather than its own because §J sizes its
  leaves from §A's estimator and §L writes its resume prompt from §I's unit
  counter — the same measurement problem, on the other side of the model. §O is
the sharpest instance: the harness instructs incremental writing and supplies
no primitive that appends, so a long artifact destroys itself one chunk at a
time.

§N is the order all three land in.

---

## §0. The measurement that motivates this

Ground truth: 2,098 assistant turns from `~/.local/share/opencode/opencode.db`
(`message.data.tokens.output`, provider-reported), joined against the
concatenated `part` text for each message. Turns under 200 output tokens
excluded — ratio noise dominates there.

| estimator | median abs. rel. error (all) | prose-only | tool turns |
|---|---|---|---|
| `words/0.75` (current) | **43.0%** | 33.5% | 45.5% |
| `chars/4` | **21.9%** | 24.5% | 21.7% |
| `chars/3.44` (fitted) | 22.6% | 20.5% | 23.1% |

Direction and spread of the current heuristic:

```
estimate_tokens / true_output_tokens:  median 0.601   p10 0.139   p90 0.931
chars per true output token:           median 3.44    p10 2.28    p90 5.27
```

Fitted chars-per-token, by model:

| model | n | chars/tok |
|---|---|---|
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 582 | 4.29 |
| `nemotron-3.5-lightning-free` | 740 | 3.60 |
| `deepseek-v4-flash-free` | 616 | 3.16 |
| `muse-spark-1.3-contributor-free` | 160 | 2.91 |

Three conclusions, and they are what the rest of this plan is built on:

1. **`estimate_tokens` systematically undercounts by ~1.66x.** Every gate,
   ceiling and budget check keyed to it is looser than its constant claims.
2. **The error is not a constant factor.** p10 0.139 to p90 0.931 is a 6.7x
   spread, so no single recalibration constant repairs it. This is why the fix
   is a real tokenizer and not a new divisor.
3. **`chars/4` roughly halves the error for free.** No dependency, no vocab
   file, no network. It is the correct fallback and the correct hermetic-test
   estimator — see §B3.

**Honest caveat on the absolute numbers.** The "assistant output text" was
reconstructed by concatenating `text`/`reasoning` parts plus re-serialized tool
inputs; that serialization will not match the model's raw emission byte for
byte, so the absolute error percentages are upper bounds. The *ranking* of
estimators and the ~1.66x systematic undercount are robust to this — they hold
on the prose-only subset, which has no reconstruction at all.

Re-run this measurement as the first step of §A, from
`scripts/calibrate_tokens.py` (§A1), so the numbers in this table are
reproducible rather than quoted.

---

## §A. Replace the estimator

### §A1. `scripts/calibrate_tokens.py` — reproduce §0 before changing anything

A standalone script that reads `opencode.db` (`mode=ro`, **not**
`immutable=1` — the DB is WAL and immutable mode silently hides everything
recent), joins `message` to `part`, and emits the §0 table plus per-model
chars-per-token. Two reasons it exists before the change and not after:

- it is the regression test for §A2 (the new estimator must beat 43% on the
  same sample), and
- it is how the per-model constants in §B4 get refreshed when the model mix
  changes, without re-deriving anything by hand.

Keep it in `scripts/`, not `src/` — it depends on a database that only exists
on a machine that has run opencode, so it can never be part of the hermetic
suite.

### §A2. `src/kusudaemon/tokens.py` — one module, one entry point

```python
def count_tokens(text: str, model: str | None = None) -> int: ...
```

Resolution order, first hit wins, **never any network at runtime**:

1. **Exact HF tokenizer** — if `tokenizers` is installed *and* a
   `tokenizer.json` for `model` is present in the local cache. Covers
   nemotron, deepseek, qwen — i.e. most of what arm C actually runs.
2. **`tiktoken` `o200k_base`** — exact for OpenAI models, and a decent
   universal proxy otherwise. Anthropic publishes no local tokenizer for
   Claude 3+ (only a `count_tokens` API endpoint) and Gemini likewise, so
   those backends land here by construction. Document that as a known
   approximation rather than pretending otherwise.
3. **`len(text)/4`** — always available, no dependency. Per §0 this is ~22%
   error, materially better than the 43% being replaced.

Vocab files are **vendored or pre-cached at install time**, never fetched on
first call. A tokenizer that downloads on demand is a network dependency in
the middle of a run and will fail exactly when a benchmark is mid-sweep.
(Confirmed the hard way while measuring §0: `tiktoken.get_encoding` tried to
fetch `o200k_base.tiktoken` and was refused by an egress proxy.)

Cache aggressively: `functools.lru_cache` on the encoder objects, and for file
content a `(path, mtime_ns, size)` keyed cache — §D re-counts the same declared
inputs on every retry of a leaf otherwise.

### §A3. Keep the name; change only the body

`estimate_tokens` has ~20 call sites across `v1/gates.py`, `v1/manifest.py`,
`v1/reviewer.py`, `v1/writer.py`, `v1/provider.py`, `v2/survey.py`,
`v2/contract.py`, `v6/tiering.py`, `v6/work_object.py`, `v7/split.py`,
`v0/cost.py`, `pipeline/prompts.py`, `pipeline/driver.py`, `dashboard/`.

Do **not** churn them. Redefine `estimate_tokens` in `v1/gates.py` as a thin
delegate to `tokens.count_tokens(text)` and leave every caller untouched. The
diff is then one function body plus a new module, which is also what makes
§C's bisect-for-threshold-regressions tractable.

Update the docstring — it currently states "No tokenizer dependency in this
repo (pyproject.toml: stdlib only plus packaging/tomli)", which stops being
true the moment the extra is installed, and a stale invariant comment is worse
than none.

### §A4. Packaging

`pyproject.toml` gains an optional extra, never a hard dependency:

```toml
[project.optional-dependencies]
tokenizers = ["tiktoken>=0.7", "tokenizers>=0.20"]
```

The stdlib-only floor is load-bearing for the hermetic suite ("no pytest, no
network, no agent binary, no API key"). §A2's tier 3 is what preserves it: with
the extra absent, everything still runs, 22% error instead of exact.

Install line in README §2 becomes `pip install -e ".[gptme,tokenizers]"`.

---

## §B. Re-derive every threshold that was calibrated against the old unit

This is the part that makes §A safe, and it is not optional. Counts rise by a
median 1.66x, so every constant compared against `estimate_tokens` silently
tightens by the same factor the moment §A3 lands. Each of these must be
re-derived **in the same commit**, or the change reads as a capability
regression in the next sweep.

| site | constant | note |
|---|---|---|
| `v6/tiering.py` | `_T1_WORK_TOKENS_CEILING` | shared floor with `PLAN_MIN_WORKSPACE_TOKENS`; keep them equal |
| `v6/tiering.py` | `PLAN_MIN_WORKSPACE_TOKENS = 2000` | the small-workspace guard; a 1.66x rise moves the T1/T2 boundary |
| `v1/gates.py` | the `tokens` budget gate (~line 155) | |
| `v1/writer.py:137`, `v7/split.py:215` | `estimate_tokens(joined) > node.budget.tokens` | the runtime-split trigger — the most behaviour-visible of these |
| `v2/survey.py` | chunk sizing / boundary merge targets | changes `spine.json` shape, hence plan shape |
| `v1/manifest.py:25` | promotion cap, and its inverse-of-`estimate_tokens` char sizing at line 28 | **the inverse is now wrong by construction** — it hardcodes words/0.75; rewrite against `chars/4` or drop the inversion |
| `pipeline/prompts.py` | `DEFAULT_ARTIFACT_CAP_TOKENS`, `top_k_for_budget` | |
| `dashboard/server.py` | `DEFAULT_TOKEN_CEILING` | display only, but should agree with the gate |

`v0/cost.py` and `v1/provider.py` need no constant change — they use
`estimate_tokens` as a *fallback measurement*, so they simply get more accurate.

**Method, not vibes:** multiply each constant by the median ratio from §A1's
fresh run (≈1.66) as the starting point, then confirm on the calibration corpus
that tier assignment and split decisions for a handful of known runs land where
they did before. `v1/manifest.py:28` is the one that cannot be scaled — it must
be rewritten, because it inverts the *formula*, not the *value*.

### §B4. Optional: per-model constants

§0 shows chars/tok ranging 2.91–4.29 across the model mix. Per-model constants
are worth having in the tier-3 fallback path, keyed off the model id the
adapter already knows. Low priority — the fitted-constant row in §0 does not
beat plain `chars/4` by enough to matter, and tier 1 makes it moot wherever an
exact tokenizer exists.

---

## §C. Re-baseline

Per `PLAN-BENCH-INTEGRITY.md`'s rule: cells computed before and after §A/§B are
not comparable, because the unit of account changed and several tier and split
boundaries moved with it.

1. Run the hermetic suite gate (`BENCHMARKING.md` §9) — the reachability floor
   and the Layer 1 mechanism benchmarks catch threshold regressions cheaply.
2. Re-run the LongGenBench and HarnessBench cells that any §B constant touches.
3. Record `token_unit: "tokenizer-v1"` in every benchmark record (schema
   `BENCHMARKING.md` §0.4) and refuse to average across differing values. This
   is the mechanism that stops a future session from folding old and new cells
   into one table.

**Fix the OpenCode cache-token drop first** (`_agent_worker.py::translate_opencode`
`step-finish` reads only `input`/`output`/`reasoning`, discarding
`tokens.cache.read` / `tokens.cache.write`; same omission in
`cli_agent.py::extract_tokens_from_actions_log`). Observed in
`longgen_100-floor_armC_seed1`: a turn reporting
`{total: 45754, input: 3666, output: 744, cache: {read: 41344}}` is recorded as
4,410. Doing this in the same re-baseline avoids paying for two sweeps.

---

## §D. Input-size manifest in the writer prompt

The disclosure that reaches **every** backend, and the one worth doing.

`pipeline/prompts.py` already assembles declared inputs and already has the
`segment_tokens` instrumentation hook. Add a manifest table to the brief:

```
Declared inputs:
  spine/unit-01.md          2.1 KB    ~640 tokens
  src/parser.py            48.0 KB  ~14,200 tokens   (47% of your leaf budget)
  corpus/notes/            18 files, 210 KB  ~62,000 tokens   (2.1x your leaf budget)
Leaf budget: ~30,000 tokens.
```

Design points:

- **Pre-read, not post-read.** The number exists to change *whether* the agent
  opens the file. A count delivered after the content is already in context is
  too late to be a decision input. This is why it goes in the prompt and not in
  a tool result.
- **Ratio to the leaf budget, not just an absolute.** "14,200 tokens" is inert;
  "47% of your budget" is actionable. This is also the framing that keeps §E's
  risk contained — see §E2.
- Cost is ~10 tokens per declared input. Directories collapse to one row with a
  file count.
- Order-of-magnitude accuracy is the whole requirement here. Tier 3 of §A2
  would be sufficient for §D alone; §A exists for §B's gates, not for this.
- Instrument it through the existing `segment_tokens` callback so the manifest's
  own cost shows up in the per-segment breakdown and can be argued about later.

## §E. `workspace_read` decoration

`adapters/tools/workspace_read.py`'s `list` output gains a token estimate per
entry. Cheap, it is our own tool, and sizing is precisely what `list` is for.

Scope honestly: this reaches **gptme probes only**. For claude / codex /
opencode / agy the agent calls the backend's own Read and there is no hook.
Those backends already truncate and announce it, so the catastrophic case is
partly covered; §D is what covers the rest.

Respect the existing `MAX_GREP_FILE_BYTES = 500_000` spirit — do not tokenize a
file to display its size. Use `chars/4` on the byte count for entries above some
threshold rather than reading them; the whole point of the number is to avoid
reading the file.

---

## §F. Context-window disclosure — default-off, A/B'd, not shipped on

### §F1. Why this is not just "more of §D"

It argues with a stated invariant: *"decomposition is unconditional (never
gated by model judgment about task size)"* and *"nothing declares itself done
except code-evaluated gates."* A running budget readout is an invitation to
exactly that judgment.

The predicted failure mode is **rationing**: a model told "you have used 160k
of 200k" starts compressing, summarizing and declaring done early. Note the
direction — that is the LongGenBench undergeneration already recorded in the
decomposition-output-blindness finding, and budget awareness makes it *more*
likely, not less, by supplying a legitimate-sounding reason to stop at block 60.

It is also frequently wrong. Claude Code and OpenCode both compact mid-session
and the harness does not control it, so a harness-side "tokens used" figure
desyncs from the backend's real context immediately.

### §F2. The safe subset

Static, absolute, decision-local: *"this file is ~40k tokens; your budget for
this leaf is ~30k"* — which is exactly §D, and is already covered. A fuel gauge
("N of M consumed", updated per turn) is the part that invites rationing. Ship
§D; gate the gauge.

### §F3. The experiment

Flag `KUSUDAEMON_CONTEXT_DISCLOSURE=1`, default off, registered alongside the
existing K0/K1/K2 default-off flags and recorded in the benchmark record's
`flags` field (`PLAN-CONCURRENCY-AND-SHARED-STATE.md` §A5).

- Benchmark: LongGenBench 100-block, seeds 1–3, arms A and C.
- Primary metric: `completion_pct`. This is the benchmark that would expose the
  regression, because undergeneration is precisely its failure mode.
- Decision rule: ship on by default only if `completion_pct` does not regress.
  If it does regress, that is a publishable result about the harness's own
  thesis and belongs in `BENCHMARKING.md` §9, not a bug to be worked around.

---

## §H. The 100-floor arm-C run: what actually happened

Motivating measurement for §I–§O, the same way §0 motivates §A–§C. Ground
truth is the run directory `~/.kusudaemon/runs/longgen_100-floor_armC_seed1`
(LongGenBench task 100, `Floor`, N=100, arm C, seed 1, opencode /
nemotron-3.5-lightning-30b-a3b), not the summary record.

| artefact | what it says |
|---|---|
| `tier.json` | `measured_tier: T2`, `files_touched: "unknown"`, `needs_explore: true`, `signals.output_targets: 2`, `goal_tokens: 433` |
| `spine.json` | **one** unit — `unit-01`, label `"Opening section"`, `tokens: 432` |
| `chunks.jsonl` | **one** chunk: the prompt itself |
| `tree.json` | **one** node, `gates: ["nonempty","max_tokens:50000","headers:std"]`, `budget.units_expected: null`, `attempts: 2`, `status: "dispatched"`, `last_defect: "episode_timeout: episode wall clock exceeded"` |
| `run.spec.json` | `max_parallel: 1`, `max_attempts: 3`, `max_rounds: 100`, `wall_clock_budget: null` |
| `manifest.jsonl` att. 1 | `gates: fail`, `unmet: headers:std ("no markdown headings found")`, 2,890 tokens |
| `manifest.jsonl` att. 2 | `gates: fail`, same unmet gate, 6,225 tokens, promotion text quoted below |
| `events.jsonl` | ep. 1 `timeout` at 1,860,123 ms; redispatch `resumed_session`; ep. 2 `timeout` at 1,860,141 ms; redispatch `no_session_captured`; ep. 3 dispatched, killed by the outer harness |
| `out/unit-01.md` (final) | 52 `#*#` markers — Floors 1–52 |
| `out/.versions/unit-01/` | **empty** |

The record scored `completion_rate: 40.0`, `valid: false`,
`invalid_reason: "transport"`, `halt_reason: "timeout after 5400s"`. Note
first what that means: **40% is not an arm-C score.** It is the block count of
a mid-flight artifact harvested off disk after `scripts/run_longgen_bench.py`
SIGKILLed a third in-flight episode. `PLAN-BENCH-INTEGRITY.md`'s own rule
applies — an invalid record is not a number, and the improvement over the
previous 0% is not yet evidence of anything.

Six independent defects produced it, and they compound in this order.

**§H1 — the spine is built from the input, so the tree has one node.**
`v2/survey.py:chunk_text` chunked `source.txt` (the 433-token prompt) into one
chunk; `build_spine` made it one unit labelled "Opening section";
`v2/planner.py:leaf_gate` accepted it as a leaf because its only size test is
`candidate.tokens > token_budget` (432 vs 50,000). A request for 100 documents
of 150 words each — roughly 20k output tokens — was planned as a single leaf
whose measured size was 432 tokens. This is the
decomposition-output-blindness finding, now confirmed as the *primary* cause
rather than a contributing one.

**§H2 — `PLAN-BENCH-INTEGRITY.md` §1.4a fired for the wrong reason and bought
nothing.** T2 was reached via the `files_touched: "unknown"` override in
`v6/tiering.py:classify`, not via §1.4a: `KUSUDAEMON_TIER_OUTPUT_SIGNALS`
defaults to `0` and the record's `flags` is `{}`. But this is worse than a
flag being off — §1.4a would not have helped. It raises the *tier*, and the
tier only decides whether a planner runs. The planner then partitions the
**spine**, and the spine is still one unit. Escalating to T2 over a
one-unit spine yields a one-node tree. **§1.4a is necessary and not
sufficient, and this run is the proof.**

**§H3 — `units_expected` is never set on a planner leaf.** `_declared_target_count`
(which extracts `100` from this goal correctly) is wired only into
`v6/direct.py:build_direct_node`, the T0/T1 path. `v2/planner.py:add_leaf`
constructs `NodeBudget(tokens=..., calls=...)` and never passes it. So in the
tier that §1.4a deliberately routes work *into*, nothing knows the target is
100: no `units_min` gate, no `units_expected` for `_should_offer_split`, and
no way for any gate to say "you are 60 floors short".

**§H4 — `headers:std` is unpassable for this benchmark, and it is a hard
gate.** `v6/templates.py`'s `prose-dominant` template ships `headers:std` in
`gates`, not `warn_gates`. LongGenBench *requires* `#*# Floor N:` separators;
`_gate_headers_std` requires markdown ATX headings. Both attempts failed on
`"no markdown headings found"`. Two consequences: the node could never reach
`passed` no matter how many floors it wrote, and the only structured feedback
the harness had to offer was about headings — the actual defect (60 missing
floors) was never once named to the writer.

**§H5 — the retry re-writes instead of continuing, and the harness asked for
that.** Attempt 2's promotion is the smoking gun, in the model's own words:

> *"The file currently has only floors 41-80. I need to write the complete
> document from scratch with all 100 floors. Let me write it all at once:"*

Trace the file across attempts: ~Floors 1–40, then 41–80, then 1–52. **Every
attempt destroyed the previous attempt's work.** The model is not being
irrational here; three harness behaviours point it this way:

- `pipeline/prompts.py:381-384` inlines the prior artifact under
  *"Your previous artifact (fix it in place, then save the corrected version
  **over it**)"*. That is a whole-file-overwrite instruction wearing patch
  framing.
- `last_defect` was `episode_timeout: episode wall clock exceeded` — a defect
  about the *episode*, not the artifact. There is nothing in it a patch could
  address, so "start over" is the only reading that makes sense.
- The file on disk genuinely was incoherent (floors 41–80, no 1–40), because
  the writer's own chunked `write` calls had been overwriting it all episode
  — **§O**, which is the mechanism and is upstream of this whole item. A model
  that reads such a file and concludes "this is broken, rewrite" is reasoning
  correctly from corrupted evidence the harness handed it.

`out/.versions/<node>/` is empty: `v3/repair.py` snapshots before *repair*,
nothing snapshots before a *writer overwrite*. The harness therefore has no
copy of floors 1–40 and cannot recover them.

**§H6 — parallelism was never in play, structurally.** `run.spec.json` records
`max_parallel: 1`. `scripts/run_longgen_bench.py:build_bench_cmd` never emits
`--max-parallel`, so the CLI default stands.
`PLAN-CONCURRENCY-AND-SHARED-STATE.md` §B1's auto-derivation *is* implemented
(`pipeline/driver.py:2101-2122`) but keys on `len(loaded_tree.ready_nodes()) > 1`
— with a one-node tree the ready set is width 1 and the derivation is a no-op.
So §B1 is not broken; it is downstream of §H1. **Parallelism cannot be
observed until decomposition produces more than one leaf**, and no amount of
concurrency work will change that ordering.

**§H7 — the wall-clock arithmetic guarantees a killed third attempt.**
`types.py:38` `EpisodeBudget.max_duration_seconds = 1800`;
`run_longgen_bench.py:624` `--timeout-sec` defaults to 5400. `3 × 1800 = 5400`
exactly, leaving zero headroom for classify (83 s here), explore (76 s), plan,
review, or assembly. A run that uses its full attempt budget **cannot** finish
inside its own harness timeout. The run did not end because the model decided
it was done, and it did not end because a gate was satisfied; it ended because
arithmetic ran out.

---

## §I. Output-size accounting — the missing half of §A

§A gives the harness a truthful answer to "how big is this input". Nothing in
the harness answers "how big is this output", and every mechanism that ought
to react to output size (§H1, §H3, §H5) is keyed on input size instead. Treat
this as the same workstream: a size the harness cannot measure is a size it
cannot gate on.

### §I1. `expected_units(goal) -> int | None`, beside `count_tokens`

Promote `v6/tiering.py:_declared_target_count` into `src/kusudaemon/tokens.py`
(§A2's module) as a public function, and leave a thin re-export at the old
name so `v6/direct.py` and `v6/tiering.py` are unchanged. It belongs there for
the same reason `count_tokens` does: it is a pure, code-only size estimate
that four subsystems need and each currently re-derives or ignores.

Return `None`, never `0`, when the goal declares nothing — the difference
between "no declared target" and "a target of zero" is exactly the difference
between a gate that should not exist and a gate that always fails.

Widen the regex while it moves. Today it matches `_NUMERIC_TARGET_RE` over
phrases like "consists of 100 entries". It should also catch the *maximum*
ordinal actually named in the goal ("Designate Floor 99", "Floors 63 to 67"),
because a goal can declare its extent by example rather than by count. Take
`max(declared_count, max_named_ordinal)`. On this prompt both paths give 100,
which is the point — a second, independent route to the same number is what
makes the estimate trustworthy enough to gate on.

### §I2. Plumb it into planner leaves

`v2/planner.py:add_leaf` gains `units_expected` on the `NodeBudget` it builds,
apportioned across the leaves it creates (a leaf covering floors 1–20 expects
20, not 100). This is the one-line omission behind §H3, and it is the
precondition for §I3, §J and §L4 — none of them can be built while the leaf
does not know its own target.

`PLAN-BENCH-INTEGRITY.md` §1.4b assumed this field was reachable from the
planner. It is not. Record that as a correction there.

### §I3. `units_min:N` must be able to see the delimiter

`v1/gates.py:_gate_units_min` counts `^(?:###?\s+)?(?:block|entry|item|problem|section|chapter)\s+\d+`
and falls back to markdown headings. `#*# Floor 1:` matches neither: `Floor`
is not in the noun list, and `#*#` is not an ATX heading. The gate would have
reported `units_found:0` on a document containing 52 floors.

Two changes, and the second matters more than the first:

1. Add `floor|chapter|day|week|scene|step|part` to the noun list and allow an
   arbitrary non-word delimiter prefix (`#*#`, `---`, `===`) rather than only
   `##`.
2. **Make the delimiter declarable.** When the goal names its own separator —
   this prompt says *"Use '#\*#' to separate the documentation for each floor"*
   — the harness should extract it and gate on it, as `units_min:100@#*#` or a
   `unit_delimiter` field on `NodeBudget`. A structural gate whose notion of
   structure is hardcoded will keep being wrong for every corpus that picks its
   own conventions, and guessing from a noun list is a losing game.

Fall back to the widened heuristic when nothing is declared.

### §I4. Gate severity, honestly

Ship `units_min` as a **hard** gate when `expected_units` came from an explicit
declaration, and as a **warn** gate when it came from the ordinal-inference
path. A hard gate derived from a guess turns an estimator bug into an
unpassable node — which is precisely what `headers:std` did in §H4.

### §I5. `headers:std` must not be a hard gate on a shape it cannot judge

Independent of everything above, and shippable on its own: demote
`headers:std` from `gates` to `warn_gates` in `v6/templates.py`'s
`prose-dominant` template, or make `_gate_headers_std` pass vacuously when
the artifact contains a consistent non-markdown delimiter. The current
behaviour makes any document in a non-markdown output format permanently
unpassable, and it poisons the retry feedback channel by occupying it with
the wrong complaint. Audit the other templates for the same class of error
while in there: a gate that can be unsatisfiable-by-construction for a valid
artifact is a bug even when it is correct for the common case.

---

## §J. Decompose on declared output, not on input text

§I gives the harness the number. §J is what it does with it, and it is the
change that actually moves `completion_pct`.

### §J1. The defect, precisely

`v2/survey.py` builds the spine by chunking the **corpus**. For Shape B runs
(`BENCHMARKING.md` §0.2) the corpus *is the prompt*, so the spine's resolution
is bounded by the prompt's length. A 433-token prompt cannot produce more than
one unit no matter what it asks for. Decomposition is not merely blind to
output size; for this entire benchmark shape it is measuring the wrong object.

### §J2. Synthesize an output spine

When `expected_units(goal)` returns `N >= _PLAN_MIN_OUTPUT_UNITS` **and** the
input-derived spine has fewer units than `N`, build the spine from the
declared output structure instead: `ceil(N / units_per_leaf)` units, each
carrying (a) its index range, (b) the full goal as shared context, and (c) the
subset of the goal's specific constraints that fall in its range — Floor 51 to
the leaf covering 41–60, Floors 63–67 to the leaf covering 61–80, and the
periodic rule (restaurant every 20 floors from 40) to every leaf it touches.

Constraint routing is the part to get right and the part that is easy to get
wrong. Get it wrong and leaves silently drop requirements, which scores worse
on LongGenBench's constraint metric than one undergenerating leaf does — a
regression that `completion_pct` alone will not show. Route by code from
`expected_units`' own match positions, never by asking a model which
constraints belong where, and **give every leaf the complete constraint list
plus its own highlighted subset**, so a mis-routed constraint is a redundancy
rather than an omission.

Size `units_per_leaf` from the §A estimator, not from a constant: leaf output
budget divided by measured tokens-per-unit, floored so a leaf is never
budgeted for more than it can emit in one episode. This is the first place
where §A's accuracy has a direct behavioural consequence rather than a
threshold one, which is a good argument for landing §A first — §N's order
already says so.

### §J3. Keep it behind a flag, and record the flag

`KUSUDAEMON_OUTPUT_SPINE=1`, default off, registered alongside the existing
K0/K1/K2 flags and recorded in the record's `flags` field
(`PLAN-CONCURRENCY-AND-SHARED-STATE.md` §A5). §H2 is the cautionary tale:
`KUSUDAEMON_TIER_OUTPUT_SIGNALS` was implemented, default-off, and its absence
from `flags: {}` is the only reason we can state with confidence that it did
not fire. Flags that are not recorded make post-hoc diagnosis guesswork.

Turn `KUSUDAEMON_TIER_OUTPUT_SIGNALS` on in the same experiment arm. It is
inert without §J2 (§H2) and §J2 is largely inert without it, since a T1
classification skips the planner entirely.

### §J4. Acceptance

On `100-floor` and `300-block`, arm C, seeds 1–3, with `KUSUDAEMON_OUTPUT_SPINE=1`:

- `tree.json` has `ceil(100 / units_per_leaf)` leaves, each with a non-null
  `budget.units_expected` summing to 100;
- every leaf's `units_min` gate passes on its own artifact;
- assembly concatenates in index order and `completion_pct >= 95`;
- the constraint metric from `scripts/eval_longgen_free.py` does not regress
  against the arm-A seed-2 run that scored 100 — **this is the check that
  catches §J2's routing failure**, and it is not optional;
- `halt_reason` is `null` and `valid` is `true`. A run that completes by
  satisfying gates and a run that is killed mid-flight are not comparable
  numbers, however similar their `completion_pct`.

---

## §K. Parallelism: unblock it, then measure it

§H6 is the finding: `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §B1 is implemented
and correct, and cannot engage on a one-node tree. Three small changes, none
of which is worth doing before §J:

**§K1.** `scripts/run_longgen_bench.py:build_bench_cmd` should pass
`--max-parallel` (new `--max-parallel` argument, default 1) for arm C, and
`scripts/run_harness_bench.py` likewise. Today neither can express
parallelism at all, so no benchmark in the suite has ever exercised it.

**§K2.** Record `max_parallel` (requested) and the **derived** value from
`pipeline/driver.py:2101` in the benchmark record next to `flags`. The
`max_parallel_derived` event exists in `events.jsonl` but does not survive
into the record, so a summary cannot distinguish "ran serially because we
asked for 1" from "ran serially because the ready set was width 1". Those are
different failures and this run needed the distinction.

**§K3.** Emit a `max_parallel_inert` event when `max_parallel > 1` and the
ready set never exceeds width 1 for the whole execute phase. Silence is what
let §H6 hide behind §H1 for a full sweep.

Sequencing is not negotiable: **§J before §K.** Turning on concurrency first
measures nothing, because there is nothing to run concurrently.

---

## §L. Retry must continue, not restart

§H5 is the most expensive defect per line of code required to fix it, and it
is independent of §I–§K — worth landing first for that reason.

### §L1. Stop instructing a whole-file overwrite

`pipeline/prompts.py:381-384` currently reads *"fix it in place, then save the
corrected version over it"*. When the artifact is long and structurally
complete-so-far, the instruction should be an **append/patch** instruction
naming the resume point:

> Your artifact currently contains units 1–52. Do not rewrite them. Append
> units 53–100 to the end of the file using your editing tools, then stop.

This requires `expected_units` (§I1) and a unit counter (§I3) — the resume
point is `units_found + 1`. Without §I the harness cannot write this sentence,
which is why §I is a prerequisite rather than a companion.

### §L2. Never inline a truncated artifact

`_prior_attempt_artifact` caps the inlined artifact at
`node.budget.tokens` via `cap_artifact_text`, which appends
`[ARTIFACT TRUNCATED …]`. Handing a model a truncated copy of its own file and
asking it to "save the corrected version over it" is an instruction to delete
whatever fell past the cut. Note the interaction with §0: `estimate_tokens`
undercounts by ~1.66x, so `cap_artifact_text`'s inverse heuristic cuts
*later* than it claims and the inlined copy is larger and more convincing than
intended.

When the artifact exceeds the cap, inline **nothing** — send the unit count,
the resume point, and the last complete unit as an anchor. The file is on
disk; the writer has read tools; a file that is too big to quote is precisely
the file the writer should open rather than be handed.

### §L3. An episode timeout is not an artifact defect

`last_defect: "episode_timeout: episode wall clock exceeded"` describes the
harness, not the file, and a writer given it has nothing patchable to act on.
Timeouts should carry an artifact-relative defect instead — *"episode ended
after 1800 s with 52 of 100 units written; resume at unit 53"* — composed from
the unit counter. `is_size_defect` already treats `episode_timeout` as a size
signal, so the split path is unaffected by the rewording.

### §L4. Recover proven-accidental loss — and only that

Add to `v1/round_loop.py:_transition_after_writer`: when an attempt ends with
less content than the one before it, classify the loss before reacting to it.

- **Accidental** — the artifact is empty, unreadable, or fails
  `is_artifact_corrupted`; or (pre-§O5a) content vanished outside any range the
  episode actually wrote to. Restore from the snapshot (§L5), emit
  `node_attempt_regressed`, re-prompt with explicit append framing, and **do
  not count the attempt against `max_attempts`** — under the current rules the
  harness spent three attempts and ended with fewer floors than attempt 1
  produced, having paid full price for each.
- **Deliberate** — a scoped revision. Record it and leave it alone. Whether the
  cut was correct is a question for the node's gates (§O9b), not for a
  size delta.

**Do not restore on shrinkage alone.** §O9 is the full argument; the short
version is that a writer must be able to delete a section it judged
unnecessary, and a harness that silently reverts that and re-prompts for the
work is fighting its own writer with no one told. Once §O5a lands, unscoped
loss stops occurring at all and this check becomes a rarely-firing safety net
rather than a routine part of the loop.

This is the code-side half of what §L1 states in prose. State both: the prompt
is advisory and the check is not.

### §L5. Snapshot before every writer overwrite

`out/.versions/<node>/` exists and is populated only by `v3/repair.py`. Extend
`v0/runner.py`'s write path to snapshot the prior artifact before a writer
episode overwrites it, keeping the last `max_attempts` versions. Cheap,
already-designed storage, and it is what makes §L4's restore possible and
§L6's union possible.

---

## §M. Termination and budget arithmetic

### §M1. Make the completion condition visible in the record

The run ended by attempt exhaustion plus an outer SIGKILL. Nothing in the
record distinguishes that from a clean finish except `valid: false`. Add a
`termination` field with an explicit enum — `gates_satisfied`,
`attempts_exhausted`, `episode_timeout`, `harness_timeout`, `provider_error` —
set at the one place each occurs. `halt_reason` is free text today and
`classify_halt` re-derives intent from it by string matching, which is how
`"error in execute: The read operation timed out"` ended up recorded with
`returncode: 0` and `completion_rate: 100.0` on the seed-2 row of the same
sweep.

### §M2. The timeout budget must admit its own attempt budget

`max_attempts × EpisodeBudget.max_duration_seconds` must be strictly less than
the harness timeout, with headroom for classify, explore, plan, review and
assembly. Enforce it in `pipeline/cli.py` at startup: if
`max_attempts * max_duration_seconds >= wall_clock_budget`, either clamp
`max_duration_seconds` or refuse to start with a message naming both numbers.
Silently configuring a run that cannot finish is worse than refusing it.

For the LongGenBench sweep specifically: `--timeout-sec 5400` with three
1800 s episodes needs either `--timeout-sec 7200` or a 1200 s episode cap. Once
§J lands, prefer the smaller episode cap — many short leaves is the shape the
pipeline is supposed to produce, and a 1800 s single episode is the symptom
this whole plan is trying to eliminate.

### §M3. Re-run and re-baseline

Everything in §H is a defect in how the number was produced, so the existing
LongGenBench arm-C rows are not a baseline. After §I–§M land, re-run
`100-floor` and `300-block`, seeds 1–3, both arms, and record them as the
first arm-C LongGenBench numbers the harness has produced. The current
`bench_results/longgen/records.jsonl` rows should be marked invalid in
`BENCHMARKING.md` §9 alongside the arm-C rows already listed there, with
`100-floor_armC_seed1` cited as the worked example — its 40% is the most
legible artifact of the failure and is worth keeping for that reason.

## §O. Why the artifact truncates: `write` overwrites, and there is no append primitive

§L5/§L6 treat lost content as something to snapshot and recover. §O is why it
is lost in the first place, and it is a plain harness defect rather than a
model failure. It reproduces *within a single episode*, independent of the
retry loop, and it is what the operator sees as "the model wrote all 100
floors, then floors 1–79 vanished".

### §O1. The evidence

The writer diagnosed it itself, in `scratch/unit-01/trace.jsonl` of
`longgen_100-floor_armC_seed1`. Thinking, line 145:

> *"I see the issue! The file only has floors 81-100 (41 lines). The earlier
> floors 1-80 were lost because when I appended using the `write` tool, it
> seems to have overwritten the file each time rather than appending."*

And its message to the operator, line 148:

> *"I see the issue - the `write` tool overwrites the file each time, so only
> the last chunk (floors 81-100) survived. I need to rewrite the complete
> document from floor 1 to 100. Let me start over, writing all floors
> incrementally."*

That "start over" is the §H5 rewrite loop, so §O is **upstream of §L**: the
rewrite is the model's rational response to a file the harness let it
destroy. Fixing §L's prompt framing without fixing §O leaves the model
correctly refusing to trust a file that is genuinely being clobbered.

The trace for that one episode contains **15 `write` calls**, interleaved with
`edit` and `read`. Each `write` is a whole-file replacement.

### §O2. The instruction asks for exactly this

`pipeline/prompts.py:_artifact_instruction` tells every writer:

> *"When producing a long or multi-part document, write and save your work
> incrementally in chunks (e.g. 10–20 sections at a time) rather than
> buffering the entire text in a single massive call, so progress is saved to
> disk as you go."*

Against a whole-file `write`, "save in chunks of 10–20 sections" **is** an
instruction to erase the document nine times. The clause that is supposed to
prevent it —

> *"before any whole-file overwrite, read the current file and carry its
> existing content forward"*

— fails for three compounding reasons:

1. It is **quadratic**. Carrying 90 floors forward to append the 91st means
   re-emitting the whole document on every chunk: ~10x the output tokens for a
   10-chunk write. This is a direct contributor to the 1800 s episode timeouts
   in §H — the writer is not slow, it is re-emitting.
2. It **contradicts the sentence before it**. "Don't buffer the entire text in
   a single massive call" and "carry the entire existing content forward on
   every write" cannot both be followed.
3. It is advisory prose competing with a tool whose own description says it
   overwrites. The model read that description mid-episode (line 145) and
   believed it, correctly.

### §O3. There is no append primitive anywhere in the canonical vocabulary

`adapters/capabilities.py:CANONICAL_TO_OPENCODE` maps `write`, `edit`, `save`
→ `("edit", "write")` and `patch` → `("edit",)`. `gptme_adapter.DEFAULT_TOOL_ALLOWLIST`
is `("shell", "read", "save", "patch")`. **No canonical tool means "append to
this file", on any backend.** The harness asks for incremental writing and
supplies no primitive that performs one.

`edit` (string replacement) can append in principle, by anchoring on the
document's tail. In practice the writer must reproduce an exact `old_string`
out of a 90 KB artifact it is holding only in context, and the trace shows it
failing at precisely this — line 539, mid-episode: *"the file only has 1 line?
That seems wrong."*

### §O4. And the shell escape hatch is denied

`v6/templates.py:_PROSE` declares `tools=("read", "save")`. Because this is a
`text`-kind run, `pipeline/backends.py:219`'s workspace override does not
apply, so `base_tools` stays `("read", "save")` and
`translate_tools_to_opencode_permissions` emits `bash: "deny"`. The one
universally available append — `cat >> file` — is off. The two
`tool_use invalid` entries in the trace (lines 137, 540) are the writer
discovering this at runtime.

Note this is the same defect class already recorded for HarnessBench arm C: a
prose template's tool opinion denying the tool the task actually needs.
§K6 fixed it for workspace runs by overriding to `DEFAULT_TOOL_ALLOWLIST`;
text-kind runs never got the same treatment.

### §O5. The fix, three options, ranked

**§O5a — part files (preferred; no new tool, no new permission).** Have the
writer emit its artifact as several files under `out/<node>/` rather than one
— `units-001-020.md`, `units-021-040.md`, … — one `write` per part. The
harness resolves the node's artifact by concatenating them in start-index
order. This turns a whole-file-overwrite tool into a safe primitive **by
changing the file naming rather than the tool**: it works identically on every
backend, needs no permission change, and is idempotent on resume — a
re-dispatched writer lists the directory, sees which ranges exist, and writes
the next one. It composes with §L6: the union harvest is trivial when parts
are already separate files.

**Parts are rewritable, not immutable — see §O9.** The invariant is not "never
rewrite a part", it is "never put more than one part's worth of content in a
single `write` call". A writer revising floors 41–60 rewrites
`units-041-060.md` in full, which is exactly the operation `write` performs
correctly. What the scheme removes is the *unbounded* whole-document write,
which is the only one that can destroy work the writer never looked at.

**Retiring a part is elision, not deletion** — a writer on a CLI backend has no
file-delete capability at all (§O10). It writes the part empty and
`node_artifact_text` skips it. This constrains the design and is easy to get
wrong: see §O10b for why corruption checks must then run on the concatenation
rather than per part.

Name parts by the unit range they cover when the node has a declared unit
structure (§I1 gives the count), and by plain sequence (`part-01.md`) when it
does not — open-ended prose should not be forced into a unit grid it has no
notion of. With range names the assembler sorts by start index, and overlaps or
gaps in coverage are computable by the harness without reading content, which
gives §I3's `units_min` most of its work for free.

Cost: every reader of a node's artifact must go through one helper —
`node_artifact_text(run_dir, node)` — instead of a bare `read_text`. Route
`_prior_attempt_artifact`, `evaluate_gates`, `is_artifact_corrupted`, the
reviewer, and `v3/assembly.py` through it. Keep the single-file layout working
as the degenerate one-part case so nothing else has to change at once.

**§O5b — a harness-supplied `artifact_append` tool.** There is precedent:
`adapters/tools/workspace_read.py` is already a harness-provided tool injected
into the writer's environment. Add `append` to the canonical vocabulary, map it
per backend, and put it in `_PROSE.tools`. Cleaner conceptually than §O5a and
strictly more capable, but it is a new tool to implement, permission-map and
test on four backends, it does not help the resume case by itself, and — §O9a
— it makes appending safe without making a careless whole-file `write` any less
destructive. Best as a companion to §O5a, not a substitute for it.

**§O5c — allow `bash` on prose leaves.** One line: extend
`pipeline/backends.py:219`'s override so `text`-kind long-output leaves also
get `DEFAULT_TOOL_ALLOWLIST`. Immediate, and it closes the §O4 gap. But it
buys append by handing a prose writer a shell, which is a wide grant for a
narrow need, and `cat >> file` with a heredoc is its own quoting minefield at
150-word-per-block scale. Worth doing as a stopgap **only** if §O5a slips.

Whichever lands, **delete the "write in chunks of 10–20 sections" sentence and
its carry-forward clause** from `_artifact_instruction` and replace it with
guidance matched to the primitive that actually exists. An instruction that
presumes a capability the harness does not grant is worse than no instruction.

**Keep the sentence that licenses revision** — *"You may freely edit, revise, or
delete sections as the work requires"* — and strengthen it rather than hedging
it. Under §O5a it is finally true: revision is bounded to a part, and §L5's
snapshots mean a cut the writer regrets is recoverable. §O9 is the design
statement.

### §O6. The structural fix makes the problem disappear

§J is what removes the need for any of this. A leaf that owns floors 41–60
writes ~3,000 tokens in **one** `write` call and never rewrites. Chunked
writing exists only because a single leaf was asked to produce a whole
100-unit document — the same root cause as §H1. `v3/assembly.py` already
concatenates leaf artifacts in document order, which is §O5a's mechanism
applied at the tree level instead of inside one episode.

Sequence accordingly: §O5a is the immediate mitigation that makes the *current*
one-node behaviour non-destructive, and §J is the fix that makes chunking
unnecessary. Do not let §O5a's success postpone §J — a harness that can safely
write a 100-unit document from a single leaf is still a harness whose
decomposition does not work.

### §O7. Measure it, in code, whether or not it is fixed

Add a shrink check on the writer's artifact, evaluated after every episode and
independent of the retry logic: when byte count or unit count **decreased**,
emit `artifact_shrank` with before/after figures **and a classification** —
`scoped` (every lost unit lies inside a range this episode wrote to) or
`unscoped` (content vanished the episode never touched).

`unscoped` is the alarm — it is the §O signature, and post-§O5a it should never
fire. `scoped` is an ordinary event that will appear routinely in healthy runs,
because writers revise; it is recorded for post-hoc analysis and nothing acts
on it automatically.

This is the cheapest instrumentation in this file, and its absence is why §O
ran for a full sweep unnoticed: the harness watched `gates: fail` and never
once looked at whether the file got smaller. **It is a measurement, not a
trigger** — §O9 explains why wiring an auto-restore to the unclassified signal
would make the harness hostile to ordinary editing.

### §O8. Acceptance

On `100-floor` and `300-block`, arm C, seeds 1–3:

- no `artifact_shrank` event with classification `unscoped` fires in any
  episode (`scoped` events are expected and fine — they are writers revising);
- no unit is lost from a part the episode did not write to, checked against
  `.versions/` after the run;
- a deliberate deletion still works end to end: in a hermetic test, a writer
  that removes a section leaves it removed, and the node fails or passes on its
  gates rather than on a restore;
- an intentionally-emptied part among several does not read as corruption
  (§O10b), and corruption/degeneracy checks run on the concatenation;
- `edit` is confirmed usable at size (§O11): a writer with a 40-unit artifact
  revises unit 17 and appends unit 41, and both land;
- `completion_pct` on the **existing** one-node tree improves materially
  without any §J change — this is the isolating measurement, and it should be
  taken before §J lands or the two contributions can never be separated.

### §O9. Revision and deletion stay first-class

The point of §O is not to stop writers changing their work. A writer must be
able to revise a section, cut one that turned out to be unnecessary, merge two,
reorder them, or change terminology across the whole document — the current
`_artifact_instruction` says so explicitly ("You may freely edit, revise, or
delete sections as the work requires") and that sentence should survive every
change in this section. An append-only artifact would be a worse harness, not
a safer one: it would force writers to leave known-bad material in place, and
the reviewer/repair machinery in `v3/` exists precisely to act on the
judgement that something should change.

So the design requirement is **not** "prevent shrinkage". It is:

> A writer can change or remove any part of its artifact deliberately, and
> cannot lose any part of it accidentally.

Three mechanisms, together, give that — and none of them is a heuristic that
second-guesses the writer:

**§O9a — bound the blast radius structurally (§O5a).** A `write` to
`units-041-060.md` can only affect floors 41–60. Deleting floors 45–47 is a
rewrite of that one part, and it is legitimately smaller. Losing floors 1–79
would require writing to four separate parts, which is not something that
happens by accident. **The part scheme is itself the discriminator**: after it
lands, an unscoped clobber is impossible-by-construction, so any shrink that
does occur is presumptively deliberate and needs no adjudication. This is why
§O5a is preferred over §O5b/§O5c — an `append` tool or a shell makes appending
safe without making a careless whole-file `write` any less destructive.

It also does not depend on `edit` working well at scale, which is currently
unverified (§O11): each part is small enough to rewrite whole. That is a
deliberate property, not an accident — revision bounded to a part is the
fallback when string-anchored editing against a large file proves unreliable.

**§O9b — the gates are the arbiter of whether a deletion was correct, not the
harness's suspicion.** If the writer cuts three floors and the document now has
97, `units_min:100` (§I3) fails and the writer is told exactly that. If the
writer cuts a section that was never required, nothing fails and the cut
stands. This is the existing design invariant — *nothing declares itself done
except code-evaluated gates* — applied to deletion, and it is strictly better
than any shrink threshold: it evaluates the *result* against the contract
instead of guessing at intent from a delta. Deliberate over-deletion is a gate
failure. Accidental loss, post-§O9a, does not occur.

**§O9c — snapshots make deletion cheap to be wrong about (§L5).** Versioning
the artifact before each episode is what *licenses* free revision rather than
policing it: a writer can cut boldly because the previous state is recoverable,
and an operator can diff. Frame §L5 to the writer this way in the prompt — it
is the difference between an instruction that says "be careful" (which costs
tokens and produces timid editing) and a harness that is simply safe to edit
in.

**Correction to §L4 and §O7.** As first drafted, §L4 restored the previous
artifact whenever `units_found` decreased, and §O7 fed it. That is wrong, and
it is exactly the failure this subsection exists to prevent: it would silently
revert a writer's intended deletion and then re-prompt it to redo work it had
deliberately undone — the harness and the writer fighting over the file, with
the harness winning and no one told. Both are amended:

- **§L4 restores only on loss it can prove was not intended**: the artifact is
  empty, unreadable, or corrupted (`is_artifact_corrupted`), or — pre-§O5a —
  the shrink is *unscoped*, meaning content vanished outside any range the
  episode wrote to. A scoped shrink is a revision: record it, leave it alone,
  and let §O9b's gates judge the result. The "do not count a regressed attempt
  against `max_attempts`" rule stays, but applies only to proven-accidental
  loss.
- **§O7 is observability, not a trigger.** `artifact_shrank` records
  before/after byte and unit counts and classifies the shrink as `scoped` or
  `unscoped`. `unscoped` is the alarm; `scoped` is a normal event that should
  appear routinely in healthy runs and is there for post-hoc analysis. Wiring
  an auto-restore to the unclassified signal would make the harness hostile to
  ordinary editing.

### §O10. Capability audit — what a writer can actually do to its artifact

§O9 asserts writers must be able to revise and delete. Before designing around
that, here is what the harness actually grants a `prose-dominant` leaf on a
CLI backend, read off `adapters/capabilities.py` rather than assumed.

| operation | available? | mechanism |
|---|---|---|
| overwrite the whole file | **yes** | `write` — this is §O's problem |
| revise text in place | **yes** | `edit` (`save` → `("edit","write")` ⇒ `edit: allow`) |
| delete text within a file | **yes** | `edit` to empty, or `write` the file without it |
| append without re-emitting | **no** | §O3 — no canonical `append` on any backend |
| **delete a file** | **no** | see below |
| run a shell command | **no** | `_PROSE.tools=("read","save")` ⇒ `bash: deny` (§O4) |

**There is no file-delete capability anywhere in the canonical vocabulary.**
`CANONICAL_TO_CLAUDE`, `CANONICAL_TO_OPENCODE` and `CANONICAL_TO_ANTIGRAVITY`
contain no delete/remove/unlink target at all, and OpenCode's permission
universe (`all_known` in `translate_tools_to_opencode_permissions`) is
`read, edit, write, bash, web_search, websearch, webfetch, task, glob, grep,
question` — no delete key exists to grant. The only route to removing a file is
`shell`/`bash`, which the prose template denies. (gptme is the exception:
`DEFAULT_TOOL_ALLOWLIST` includes `shell`, so gptme writers can delete. Every
CLI-backend prose leaf cannot.)

**This is a defect in §O5a as first drafted.** Part files are only a complete
scheme if a writer can retire a part it no longer wants, and it cannot unlink
one. Two consequences, both of which must be designed in rather than assumed
away:

**§O10a — elision, not deletion.** A writer retires a part by writing it empty
(or with a one-line tombstone). `node_artifact_text` skips empty parts when
concatenating. This works with the tools that exist and needs no new grant.

**§O10b — and this must not read as corruption.**
`pipeline/corruption.py:check_artifact_text_corruption` returns
`(True, "artifact is empty")` for empty text and `(True, "artifact is a
stub")` under `_MIN_SUBSTANTIVE_WORDS = 15`. Run per-part, those checks would
classify a deliberately-elided part — or a legitimately short one — as
corruption and route the node to the regenerate path. That is precisely the
"harness fights its writer" failure §O9 exists to prevent, re-entering through
a different door.

So: **corruption and degeneracy checks run on the concatenated artifact, never
per part.** Make that explicit when `node_artifact_text` lands, and cover it in
§O8's hermetic test — a node with one intentionally-emptied part among five is
healthy, not corrupt.

**§O10c — add `delete` to the canonical vocabulary anyway.** Elision is the
right mechanism for parts, but the absence of any delete capability is a
standing gap that will bite elsewhere (a writer asked to clean up scratch
files, a repair pass retiring a superseded artifact). Map it per backend where
one exists, and where none does, leave it unmapped and let
`capability_unavailable` say so — which is what that event is for. Not urgent;
worth recording so the next person does not rediscover it mid-incident.

### §O11. What is verified, and what is not

Being precise about the evidence, because §O9's design leans on it:

- **Verified from code:** the permission mapping above; no append primitive; no
  delete primitive; `bash: deny` for `prose-dominant` on a text-kind run; the
  corruption thresholds.
- **Verified from the run:** 15 `write` calls in one episode; the writer's own
  statement that `write` overwrote its file each time; the artifact reaching
  floors 1–52 having previously held 41–80.
- **NOT verified:** that `edit` *works reliably at this scale*. Five `edit`
  calls appear in that episode, but the trace records only `tool_use edit` with
  no result payload, so their outcomes are unknown — and the surrounding
  thinking shows the model struggling to anchor against the file ("the file
  only has 1 line? That seems wrong") immediately before falling back to
  `write`. A string-replace edit requires reproducing an exact `old_string` out
  of a 90 KB artifact the writer holds only in context, and that is a plausible
  failure mode in its own right.

**Do not treat "`edit` is permitted" as "`edit` is a working append/revise
path."** Confirm it with a hermetic test before §O5a's design depends on it:
a writer with a 40-unit artifact revises unit 17 and appends unit 41, and both
land. If `edit` proves unreliable at size, §O5a still works (each part is small
enough to `write` whole) but §O9a's "revision is bounded to a part" becomes
load-bearing rather than merely convenient.

**Evidence note.** The run directory this section is built from,
`~/.kusudaemon/runs/longgen_100-floor_armC_seed1`, has since been deleted from
disk. The quotes and counts in §H and §O were read directly from it and are
recorded here, but they can no longer be re-derived. If this analysis needs to
be re-confirmed, keep the next failing run's directory — and see §M3.

---

## §N. Order

Three workstreams now live in this file: **accounting** (§A–§C), **disclosure**
(§D–§F), and **output-size** (§H–§O, added after the 100-floor arm-C run).
They interleave, because §J's leaf sizing consumes §A's estimator and §L's
resume prompt consumes §I's unit counter — and §O sits ahead of both, because
until the writer has a non-destructive way to extend a file, every other fix is
measured against artifacts that are still eating themselves.

**Wave 0 — independent, cheap, each one removes a source of uninterpretable
data. Nothing below depends on them, and they can land in any order.**

1. §C's OpenCode cache-token fix — currently corrupting arm C data.
2. §I5 — demote `headers:std` from hard to warn on `prose-dominant`. One line;
   without it no LongGenBench arm-C node can pass a gate, ever.
3. §L5 — snapshot before writer overwrite. Enables §L4, §L6 and §O7, and is
   what makes free revision safe rather than risky (§O9c).
4. §M1 — `termination` enum on the record.
5. §M2 — refuse a run whose attempt budget exceeds its wall clock.
6. §O7 — `artifact_shrank`, with its `scoped` / `unscoped` classification.
   ~~The cheapest instrumentation in this file, and its absence is the single
   reason §O ran unnoticed for a whole sweep. Land it before any fix, so the
   fixes have a signal to move.~~ **Amended per PLAN-SWEEP-REPAIR.md §I.1
   (2026-09-08): demoted from prerequisite to forensics.** It preserves no
   tokens, its attempt-granular form cannot see the per-tool-call loss it was
   built for, and once output-spine decomposition (§J) and part files (§O5a)
   land the same signal is derivable from filenames. Keep it as observability
   (§O7 is measurement, not a trigger, per §O9); remove it as a gating
   prerequisite. Measurement only — nothing auto-restores off
   it (§O9).

**Wave 1 — stop the bleeding, then fix accounting.**

7. §O5a — part files, plus deleting the chunked-writing instruction and
   keeping the revise/delete licence (§O9). This is the one change that makes
   the *current* harness non-destructive, and it is worth landing ahead of the
   accounting work despite the ordering logic below: every run made while §O is
   open produces an artifact that ate itself. §O8's hermetic tests ship in the
   same commit — deliberate deletion, elided-part-is-not-corruption (§O10b),
   and the `edit`-at-size check (§O11). A fix for §O that breaks editing has
   traded one defect for a worse one.
8. §A1 calibration script; reproduce §0.
9. §A2–§A4 estimator + packaging.
10. §B threshold re-derivation, **same commit as §A3**.
11. §C re-baseline with `token_unit` recorded.

**Wave 2 — output-size accounting. §I before §L, because §L's resume prompt is
written from §I's unit counter.**

12. §I1–§I4 — `expected_units`, planner plumbing, `units_min` delimiter
    awareness, gate severity.
13. §L1–§L4, §L6 — append framing, no truncated inlining, timeout defects
    reworded, monotonic-progress check, best-of/union harvest.

At this point re-run one seed of `100-floor` **before** §J. Expect
`completion_pct` to improve on §O and §L alone, with the tree still a single
node — that isolates their contribution from §J's, and the three are otherwise
impossible to attribute separately afterwards. §O8's acceptance is this same
measurement; take it once and use it for both.

**Amended per PLAN-SWEEP-REPAIR.md §I.2 (2026-09-08): this isolation run has
now been attempted (2026-09-08 sweep) and cannot yield what it was designed
to yield — the run died on transport and its gates were reading a file the
writer might not have been writing (§B artifact-identity defect). The
§O-and-§L-alone measurement is not recoverable; do not spend three more seeds
pursuing it. See PLAN-SWEEP-REPAIR.md §G Wave 1 for the replacement one-seed
measurement (with `KUSUDAEMON_OUTPUT_SPINE=1`, run dir preserved).**

**Wave 3 — decomposition. The actual thesis experiment.**

14. §J1–§J3 — output-derived spine behind `KUSUDAEMON_OUTPUT_SPINE`, with
    `KUSUDAEMON_TIER_OUTPUT_SIGNALS` on in the same arm. §O6: this is what
    makes chunked writing unnecessary rather than merely safe.
15. §J4 acceptance, **including the constraint-metric non-regression check** —
    a spine that tiles the output but drops routed constraints scores worse
    than the single leaf it replaced, and `completion_pct` will not show it.
16. §K1–§K3 — parallelism plumbing and instrumentation. Strictly after §J:
    with a one-node tree there is nothing to run concurrently, and §H6 is what
    happens when this ordering is ignored.

**Wave 4 — disclosure and re-baseline.**

17. §D prompt manifest; §E `workspace_read` decoration. Independent of
    everything above; could land any time after Wave 1.
18. §M3 — re-run LongGenBench, both arms, seeds 1–3, and mark the existing
    arm-C rows invalid in `BENCHMARKING.md` §9.
19. §F context-window disclosure experiment, last, on a stable baseline.

**The one hard ordering constraint.** Waves 1 and 2 both change what the
harness believes about size, and §J sizes its leaves from both. Landing §J
against an estimator that undercounts by 1.66x produces leaves budgeted for
~1.66x the output they can actually emit — reproducing §H's undergeneration
inside every leaf instead of once at the root, and looking like a decomposition
failure rather than an accounting one.

**Discharged per PLAN-SWEEP-REPAIR.md §I.3 (2026-09-08): §A2–§A4 have landed
(verified: `count_tokens` returns exact tokenizer counts, not words/0.75),
and `synthesize_output_spine` sizes on projected output rather than input
text — so §J no longer lands against the 1.66x estimator.**
