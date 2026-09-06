# PLAN-EFFICIENCY-AND-HORIZON.md

Implementation plan: cost reduction without capability loss, plus the
feature work that actually extends long-horizon reach. Produced 2026-08-15
by tracing the live tree (`src/kusudaemon/`) against CLAUDE.md Parts I–X.

**Numbering.** Defects continue the `§D` series at **§D14** (last shipped:
§D13). Workstreams take fresh letters **§L** (efficiency), **§M**
(long-horizon features), **§N** (measurement). Nothing here renumbers
Part I, §A, §B, §C, or §E–§K — docstring citations stay resolvable.

**Rules carried from Part V, binding on every item below.**

1. Failing-first test, always. A fix without a demonstration is
   indistinguishable from a fix that does nothing.
2. No behavior change without a fallback — new paths opt in, consumers
   degrade to today's behavior when off.
3. Core package and test suite stay dependency-free (heavy imports inside
   function bodies).
4. A new default is a separate decision from a new mechanism. Ship
   default-off, measure, then flip.
5. Full suite green at the end of each workstream (954 tests as of
   2026-08-15).

**Verification status.** Items marked **[verified]** were reproduced by
executing the checked-out code in this session, not inferred from reading.
Items marked **[read]** are code-reading findings with the mechanism named
precisely enough to write the failing test directly.

---

# Part 0 — What the audit actually found

Three things, in descending order of how much they matter.

**1. The tier system's cost claim is partly fictional for large inputs.**
§A4's whole promise is "cost scales with the task" (invariant 8). Two
independent defects break that promise at the top end: T2 never got the
flat-plan cap it is documented as having (§D17), and the tier table's `or`
lets an arbitrarily large corpus into T2 in the first place (§D18). The
combined effect is that a 4.4M-token textbook is planned by a full
depth-4 recursion under a tier whose stated cap is "2–8 leaves, no
recursion" — T2 is currently T3-minus-pilot, and nobody measured it
because the eval harness's `t2-corpus` task is small.

**2. Roughly a third of remaining call cost is structurally avoidable
without touching output quality.** The biggest single items are: a
per-node reviewer verdict that is recomputed on every resume even when
neither artifact nor rubric changed (§L6), planner recursion that spends a
model call on slices a script can tile exactly (§L4), and document-review
windows that re-send their 20-node overlap and the entire spine label list
on every window (§L7). None of these change what the model is asked to
judge — they change how often it is asked.

**3. Wall-clock, not tokens, is the binding constraint on the phases that
matter most.** Structural exploration (§A6, up to 8 probes) and targeted
research probes are both dispatched with a bare `for … await` loop, fully
serialized, despite §A6 explicitly specifying "dispatchable in parallel"
(§L8). And `max_parallel` defaults to 1, so a 400-leaf tree with no
dependency edges runs 400 agent episodes end to end (§L9). Neither costs a
token to fix.

Below that sit twelve real defects, four of them reproduced by execution.

---

# Part I — Defects (§D14–§D27)

Severity: **P0** = the harness cannot do its stated job; **P1** = wrong
under a reachable input; **P2** = cost, correctness-of-record, or
ergonomics.

## §D14 Every streaming reasoning chunk is emitted twice (P1) — **[verified]**

`v1/provider.py:complete_json` fires `on_reasoning` twice for the same
text on any `streaming=True` call. `_consume_sse_event` (line 505) already
calls `on_reasoning(reasoning)` per delta as the stream is consumed; then
`complete_json` lines 270–273 read `message["reasoning_content"]` — which
`_consume_sse_lines` built by *concatenating those same chunks* — and
fires the callback again with the whole blob.

Reproduced against the checked-out code with a canned two-chunk SSE body:

```
on_reasoning calls: ['think A ', 'think B ', 'think A think B ']
total chars: 32   (14 expected)
```

Every driver phase call passes `streaming=True` (`_phase_classify`,
`_phase_intake`, `_phase_plan`, `_phase_review`, `_phase_research`,
document review), and `_reasoning_sink` appends one JSONL line per
callback. So **every `scratch/phase-*/trace.jsonl` is ~2× its true size**,
the dashboard's Chat tab and main feed render each thought twice (once
streamed, once as a duplicate blob at the end), and §F1's `?since=`
cursor paginates through the duplication. This is also the most likely
real cause of any "the model repeated itself" impression in phase traces —
it did not; the harness recorded it twice.

**Fix.** The post-hoc emission is the non-streaming path's mechanism only.
Guard it: fire the tail `on_reasoning` **only when `streaming is False`**
(the non-streaming transport has no per-chunk seam, so it is the only
place the blob is new information). One-line change, no fallback needed —
the streamed callbacks already carry every byte.

**Failing-first test** (`test_v1_units.py`): drive `complete_json` with a
`stream_transport` yielding two reasoning deltas and assert
`on_reasoning` receives exactly `['think A ', 'think B ']` — asserts 2
calls / 14 chars, fails at 3 calls / 32 chars today.

## §D15 Streaming is not streaming — the body is fully materialized first (P1) — **[read]**

`v1/provider.py:_http_stream_transport` line 423:

```python
lines = [line.decode("utf-8", errors="replace").rstrip("\n") for line in response]
return _consume_sse_lines(lines, on_reasoning)
```

The list comprehension drains the entire HTTP response before
`_consume_sse_lines` is called, so no `on_reasoning` callback fires until
the call has fully completed. B3-1's stated purpose — "phase trace files
grow live instead of only after the whole call completes" (Part VIII) — is
defeated by its own transport. On a long plan or document-review call the
operator watches a silent `in_progress` for the entire call, which is
precisely the §D0c/§B2-3 failure shape the liveness work exists to
eliminate.

**Fix.** Make `_consume_sse_lines` accept an *iterable* of lines rather
than a `list`, and pass the response object's line iterator directly. The
function is already written as a single forward pass over `lines` with no
indexing or `len()`, so this is a signature widening, not a rewrite. Keep
the list-accepting call sites working (a `list` is an iterable), so every
existing unit test that feeds canned lines is unchanged.

Two details that must survive: (a) the degenerate non-SSE fallback (`if
not content_parts and not reasoning_parts and not saw_sse: json.loads(...)`)
needs `raw_parts` accumulated as it goes — it already is; (b) the response
must stay inside the `with urllib.request.urlopen(...)` block while being
consumed — move the `return` to build the result inside the `with`.

**Failing-first test:** a fake stream transport whose iterator records the
wall-clock ordering of "line yielded" vs "on_reasoning fired"; assert the
first callback fires before the last line is yielded. Fails today (all
callbacks fire after).

## §D16 `Callable` is used but never imported in three modules (P2) — **[verified]**

`v1/reviewer.py`, `v4/probe_planner.py`, and `pipeline/prompts.py` all
annotate parameters `Callable[...]` without importing it. `from __future__
import annotations` (PEP 563) makes annotations lazy strings, so nothing
raises at import — but the names are genuinely unresolvable:

```
CONFIRMED kusudaemon.v1.reviewer._call_reviewer: name 'Callable' is not defined
CONFIRMED kusudaemon.v1.reviewer.review_node: name 'Callable' is not defined
CONFIRMED kusudaemon.v4.probe_planner._ask_one_window: name 'Callable' is not defined
CONFIRMED kusudaemon.v4.probe_planner.plan_probes: name 'Callable' is not defined
```

(`pipeline/prompts.py:build_node_prompt` has the same defect at its
`segment_tokens` parameter; it could not be introspected in isolation
here only because the module pulls in `pipeline.approvals`.)

Any `typing.get_type_hints()` call — a doc generator, a runtime validator,
a future dataclass/`attrs` migration, or `pydantic` — raises `NameError`
on these four functions. It is latent, but it is latent breakage sitting
in the three modules most likely to acquire tooling.

**Fix.** Add `Callable` to each module's `typing` import. Three lines.

**Failing-first test** (`test_v1_units.py`, extended): a suite-wide guard
that walks every function in `kusudaemon.*`, calls
`typing.get_type_hints`, and asserts no `NameError` — this catches the
class, not just today's three instances, and costs ~1s.

## §D17 T2 never got its flat-plan cap — it recurses to depth 4 like T3 (P0) — **[read]**

CLAUDE.md §A7 point 2 and §A4.3's caps column both state, as **shipped**,
that "T2 plans one flat level and stops — `build_tree(..., max_depth=1)`,
a caller-passed cap like `depth_cap`". The code does not do this.
`pipeline/driver.py:_phase_plan` (line 1242) calls:

```python
tree = await asyncio.to_thread(
    build_tree,
    load_spine(self.run_dir),
    self.provider,
    input_path_for=...,
    log=..., unit_summary_for=..., on_reasoning=..., probe_sink=..., streaming=True,
)
```

No `depth_cap`. `build_tree`'s signature defaults to `DEFAULT_DEPTH_CAP =
4`, so **T2 and T3 plan identically**. §A4.3's T2 row reads "flat plan,
2–8 leaves, **no recursion**"; what actually runs is up to 4 levels and up
to `DEFAULT_NODE_CAP = 400` leaves.

Cost: at T2 the planner spends 1 top-level call plus one call per child
that fails `leaf_gate`, recursively. On a corpus large enough that most
children overrun the 24k budget, that is 1 + 12 + ~100 calls where the
tier promised 1. It also silently voids T2's downstream cost model: 400
leaves means 400 writer episodes and up to 400 reviewer calls in a tier
the operator chose because they "expected this to take four minutes."

**Fix.** Thread the tier's cap through:

```python
tier = self._current_tier()
depth_cap = 1 if tier == "T2" else DEFAULT_DEPTH_CAP
tree = await asyncio.to_thread(build_tree, ..., depth_cap=depth_cap, ...)
```

`build_tree` already handles `depth >= depth_cap` by emitting a
`forced_leaf` with reason `"depth cap reached"`, so a T2 slice that would
have recursed becomes one honest oversized leaf — which is exactly the
input `v7/split.py`'s runtime-split gate (§A8) is designed to catch, and
which escalates T2→T3 via the `split_accepted` trigger that is already
wired. **The two mechanisms compose correctly**: T2 stays flat and cheap;
a genuinely too-big leaf demonstrates the overrun and buys its own
promotion to the recursive tier. That is invariant 2 working as designed,
and it is unreachable today because T2 pre-empts it by recursing.

Note `tier` is already read at line 1300 of the same method for the
contract-rendering branch — move that read to the top rather than adding a
second one.

**Failing-first test** (`test_v2_planner.py` + `test_driver_phases.py`):
a T2 run over a spine whose units force a two-level partition; assert
`plan_level` is called exactly once and every node in `tree.json` has a
dot-free id. Fails today with 1 + N calls and `parent.child` ids.

## §D18 The tier table lets an arbitrarily large corpus into T2 (P1) — **[verified]**

`v6/tiering.py:_classify_raw` line 367:

```python
if estimate.artifacts <= 8 or signals.work_tokens < _T2_WORK_TOKENS_CEILING:
    return "T2"
return "T3"
```

The `or` means T3 is reachable **only** when `artifacts > 8` **and**
`work_tokens >= 150_000`. Reproduced:

```
4.4M-token corpus, model says artifacts=5  -> T2
4.4M-token corpus, model says artifacts=9  -> T3
```

The code faithfully implements §A4.3's literal row text — so the spec is
where the defect originates — but the consequence is concrete: one
model-supplied integer (`artifacts`, unverifiable, unbounded by anything
in code) is sufficient to route a 4.4M-token textbook into a tier whose
own caps column says "2–8 leaves." Eight leaves over 4.4M tokens is 550k
tokens per leaf, ~23× `DEFAULT_TOKEN_BUDGET`. This is §E28's defect
(a size-blind tier decision) surviving one row lower in the same table.

**Fix.** Make the T2 row conjunctive and add the size guard the T0/T1 rows
already got in §E28:

```python
if estimate.artifacts <= 8 and signals.work_tokens < _T2_WORK_TOKENS_CEILING:
    return "T2"
return "T3"
```

Amend §A4.3's T2 row text in CLAUDE.md in the same commit — the table is
the spec, and leaving it saying "or" while the code says "and" recreates
the §D10 class of defect (a docstring describing an intent the code
inverts).

Interaction with §D17: these two must land together. Fixing §D18 alone
pushes big corpora to T3 (correct); fixing §D17 alone caps T2's planner
but leaves big corpora in it. Together, small work gets a genuinely flat
cheap plan and large work gets the recursive planner it needs.

**Failing-first test** (`test_v6_tiering.py`): the two cases above,
asserting `T3` for the 4.4M/artifacts=5 case. Fails today with `T2`.

## §D19 The in-place retry loop bypasses the single-writer tree lock (P2) — **[read]**

`v1/round_loop.py` line 458, inside the retry `while`:

```python
node.status = "dispatched"
tree.save(tree_path)          # <-- direct save
await dispatch(node)
```

Every other writer of the shared tree goes through `_save_tree_locked`
(§C2's "single-writer discipline"; §E20i fixed exactly this at the
wave-dispatch site). The retry path was missed. Today the retry loop is
serialized inside `for chunk: for node:` so no race is *reachable* — but
the discipline exists precisely so a future gather cannot silently break
it, and one unguarded `tree.save` of a shared in-memory `TaskTree` would
serialize a stale view over another task's just-committed status.

**Fix.** `await _save_tree_locked(tree, tree_path, tree_lock)`. One line.

**Failing-first test:** a static guard in `test_v1_round_loop_parallel.py`
asserting `round_loop.py`'s source contains no bare `tree.save(` outside
`_save_tree_locked` — a source assertion is the honest test here, since the
race is not reachable at `max_parallel=1` and the invariant is structural.

## §D20 A cyclic fallback map spins the rate-limit ladder forever (P2) — **[read]**

`v1/provider.py:_call`, the §G4 model-fallback branch (lines 329–348),
resets `attempt = 0` and `continue`s after switching models. If
`provider.json`'s `gptme.fallbacks` maps A→B and B→A (a natural thing for
an operator to write, meaning "either of these two"), every second 429
switches models and resets the ladder, and the loop never reaches
`attempt >= len(RATE_LIMIT_BACKOFFS)`. The run hangs, burning a 1-minute
sleep per iteration indefinitely, with `rate_limit_waiting` events
accumulating and no terminal error.

**Fix.** Track models already tried in this `_call` (`tried: set[str]`,
seeded with the initial model); take the fallback branch only when the
candidate is not in `tried`; add it on switch. The ladder then advances
normally once the fallback chain is exhausted, and a cycle terminates
after visiting each model once. Also cap total model switches at, say, 4 —
belt and braces, and it bounds the worst case explicitly rather than
relying on the map being acyclic.

**Failing-first test** (`test_provider_config.py` / `test_v1_units.py`): a
transport that always 429s with a cyclic fallback map and a fake `sleep`
counter; assert `ProviderHTTPError` is raised within a bounded number of
sleeps. Hangs (or exceeds the bound) today.

## §D21 An unrecognized `survey_mode` silently takes the expensive path (P2) — **[read]**

`pipeline/driver.py:_phase_survey`:

```python
mode = self.options.survey_mode
if mode == "auto":
    mode = "structural"
if mode in ("deterministic", "structural"):
    votes = survey_chunks_structural(chunks)
else:
    ... survey_chunks(...)   # up to MAX_SURVEY_CALLS = 60 model calls
```

Anything not in `{auto, deterministic, structural}` falls into the model
path. `"embedding"` is exactly such a value — it is the spelling CLAUDE.md
Part II still documents as the default, it is what `v2/embeddings.py`'s
module docstring is written around, and any `run.spec.json` written before
`survey_mode` moved to `"auto"` carries it. A resume of such a run now
spends up to 60 model calls where it spent 0.

This is aggravated by a second finding: **`v2/embeddings.py:embeddings_available()`
now returns `False` unconditionally** ("Always False — local models and
sentence-transformers are not used") and `embed_texts` raises. So the
"embedding" mode this codepath is named for cannot function at all; the
only two real modes are structural (0 calls) and model (up to 60).

**Fix.** Invert the branch — the model path becomes opt-in by exact name,
everything else degrades to structural with a loud event:

```python
if mode == "model":
    ... survey_chunks(...)
else:
    if mode not in ("auto", "deterministic", "structural"):
        self._log({... "type": "survey_mode_unrecognized",
                   "detail": f"{mode!r} is not a survey mode; using structural (0 calls)"})
    votes = survey_chunks_structural(chunks)
```

Same shape as §E5's `deterministic`-as-alias handling: an unknown value is
a config mistake, and the safe reading of a config mistake is the free
path, not the 60-call one.

**Related cleanup, same commit.** `v2/retrieval.py` fuses BM25 with dense
cosine and documents "BM25 alone when no embeddings — degradation, not
failure." With `embeddings_available()` hard-`False`, the dense half is
permanently dead: `build_chunk_index` never writes vectors,
`_default_dense_scorer` always returns `None`, and `retrieve_spans` is
BM25-only. That is *fine* — but `RunOptions.inline_spans`'s docstring
still advertises "BM25 + dense," and the dashboard's §E6 fix still gates a
UI option on `embeddings_available()`. Update both docstrings to state the
current truth. Do **not** delete the dense path: it is a working
implementation behind one honest availability flag, and deleting it would
cost more to restore than it costs to keep.

**Failing-first test** (`test_v2_survey.py`): drive `_phase_survey` with
`survey_mode="embedding"` and a provider that raises on any call; assert
the phase completes and a `survey_mode_unrecognized` event is logged.
Raises today.

## §D22 `_log_blocked_tree` reads the whole event log on every execute pass (P2) — **[read]**

`pipeline/driver.py:_log_blocked_tree` dedupes by scanning **every** line
of `events.jsonl` for the last `node_blocked`:

```python
for ev in self.log.read_all():
    if ev.get("type") == "node_blocked":
        last = ev
```

`events.jsonl` is the append-only resume log for the entire run. On a long
T3 run it is tens of MB, and this fires on every `_phase_execute` that
ends parked — i.e. on every resume of a parked run, which is exactly the
state an operator resumes repeatedly while intervening.

**Fix.** `EventLog` already has a `scan` seam (used by v0's
`_completion_consumed`). Add a bounded reverse read — `read_tail(n=200)`
— and dedupe against that. A `node_blocked` older than the last 200 events
is not the one we would be deduping against anyway.

**Failing-first test:** a 50k-line event log; assert `_log_blocked_tree`
reads under a bounded byte count (instrument via a counting file wrapper).

## §D23 `_promotions_of` re-reads the whole manifest per node prompt (P2) — **[read]**

`pipeline/prompts.py:_promotions_of` calls `read_all_manifest_entries`
(whole file, every line, all nodes) to resolve the promotions of a node's
`depends_on` list. Called once per writer prompt, i.e. once per dispatch
per attempt. On a 400-node tree that is O(N²) manifest reads.

Today it is masked because `depends_on` is empty on every planner leaf, so
the function returns early at `if not node.depends_on: return ""`. **§M2
below populates `depends_on` for real**, which un-masks it — so this must
be fixed as part of that workstream, not after.

**Fix.** The same stat-stamp cache pattern `_load_contract_cached` and
`_load_spec_cached` already use in this module: cache
`{node_id: promotion}` keyed on `(manifest path, size, mtime_ns)`, bounded
FIFO at 64 entries, under a lock. The manifest is append-only, so the
stamp is a sound invalidation key.

## §D24 `v7/capabilities.py` is dead code (P2) — **[verified]**

`v7/capabilities.py` (5.5 KB — `Skill`, `MCPServer`, `discover_skills`,
skill/MCP discovery from `~/.gemini/config` and `.agents`) has **no
importers anywhere in the tree**. `discover_skills`'s only caller is
another function in the same file. Nothing in `pipeline/`, `adapters/`, or
`dashboard/` references it. (`adapters/capabilities.py` is a different,
genuinely-wired module — backend tool translation.)

Meanwhile CLAUDE.md Part VII §K and Part X item 1 both state §K is "NOT
SHIPPED (only documented)." That is now half-wrong in the way that costs
the most: there *is* code, it *doesn't* run, and a reader of Part X will
either rewrite it or assume it works.

**Fix.** Decide, and record the decision. Two acceptable outcomes:

- **Wire it** (preferred — see §M5): it is the honest majority of §K3's
  discovery half, and the remaining work is the `gptme-capabilities.toml`
  emission (§K1) plus `extra_tools` on the allowlist (§K2).
- **Delete it** and note in Part X that §K starts from zero.

What is not acceptable is leaving it: an unreferenced module that a
docstring describes as implementing a spec section marked unshipped is
exactly the §D0 case-C failure — it looks like progress and is not.

## §D25 Web-search tools are forced onto every writer, defeating per-shape narrowing (P2) — **[read]**

`pipeline/backends.py:build_writer_adapter` lines 163–167:

```python
base_tools = tuple(node.tools) if node and node.tools else DEFAULT_TOOL_ALLOWLIST
web_search_tools = allowed_tools_for("web_search")
all_writer_tools = base_tools + tuple(t for t in web_search_tools if t not in base_tools)
```

The docstring is candid: web search is added "unconditionally, so even a
node scoped down via `node.tools` keeps [it]." But A6-3's whole stated
benefit was that narrowing a prose leaf to `("read", "save")` "removes the
largest single tool-doc block in gptme's prompt" — and `v6/templates.py`
does correctly ship `tools=("read", "save")` for the prose template. That
narrowing is then partly undone here, on **every leaf of every run**,
including the 90% of prose leaves that will never search the web.

Tool schemas sit in the stable prefix (§8 ordering), so this is not a
per-turn cost — but it is a per-episode cost on a fresh subprocess, and
every leaf pays it.

**Fix.** Grant web search when the node has a reason to want it:

```python
wants_web = bool(node and ("web" in node.tools or _node_has_web_probe(node, run_dir)))
```

where `_node_has_web_probe` checks for a `kind="web"` finding among
`node.inputs` (the probe machinery already writes those paths onto the
node). Keep an escape hatch: `RunOptions.always_grant_web_search`,
default **True** for one release so this is a pure no-op on ship, then
flip to False after measuring — rule 4.

**Failing-first test** (`test_pipeline_backends.py`): a prose-template
node with no web probe; assert the searxng tool path is absent from the
adapter's allowlist when the new flag is False, present when True.

## §D26 The planner recurses on model-estimated call counts (P2, architectural) — **[read]**

`v2/planner.py:leaf_gate` fails a candidate on either of two conditions:

```python
if candidate.tokens > token_budget:            # measured by code
if candidate.estimated_calls > tool_call_cap:  # asserted by the model
```

and `build_tree.recurse` spends a real `plan_level` model call on any
candidate that fails either. So a model that returns `estimated_calls: 20`
for a slice whose tokens fit comfortably inside the 24k budget forces an
extra planner call — and forces it recursively, since the deeper call's
children carry the same unverifiable field.

§A2 invariant 2, as amended, is explicit that this is the wrong shape:
"A model's opinion that something 'feels too big' is never sufficient, and
never necessary." `v7/split.py:evaluate_split` honors that at runtime
(precondition 1 is a *measured* overrun). The planner does not.

**Fix.** Demote `estimated_calls` to advisory:

- `leaf_gate` recurses on **measured token overrun only**.
- `estimated_calls` is retained on the `Candidate`, still stored on the
  node's `NodeBudget.calls`, still recorded — it is useful as a budget and
  as a signal; it just stops being a decision.
- A candidate that fits the budget but claims high call count becomes a
  leaf with a `warn`-severity note in the planner event, exactly the "ship
  at warn severity first, graduate after measuring" pattern §C1
  established.

Add a `leaf_gate(..., trust_estimated_calls: bool = True)` parameter so
the change ships default-off (rule 2/4), flip after one measured run.

**Failing-first test** (`test_v2_planner.py`): a candidate with
`tokens=1000, estimated_calls=50, tool_call_cap=15`; assert
`leaf_gate(...)` returns `True` when `trust_estimated_calls=False`, and
that `build_tree` makes exactly one `plan_level` call for a spine of such
candidates.

## §D27 `_parse_retry_after`'s docstring contradicts the 429 branch (P3) — **[read]**

The docstring says "§11.10.3 caps whatever comes back at 60s anyway." That
is true on the 5xx branch (`min(exc.retry_after, 60.0)`) and false on the
429 branch, which caps at `RATE_LIMIT_BACKOFFS[-1]` = 18000s (5 hours). A
reader debugging a 5-hour wait will look here and be told it cannot
happen. Rewrite the docstring to name both caps. Zero code change — this
is a §D10-class correction and belongs in the same commit as §D20.

---

# Part II — §L: efficiency workstreams

Each item states what it removes, what it must not change, and its ship
gate. Ordered by (savings × confidence) ÷ risk.

## §L1 Kill the duplicate reasoning emission

Subsumes **§D14**. Halves every phase trace file, halves the bytes the
dashboard's `?since=` cursor pages through, and removes the duplicated
thinking the operator currently reads as model repetition.

**Ship gate:** phase trace byte count for a fixed scripted `t2-corpus` run
drops by ≥45% with an identical concatenation of `text` fields.

## §L2 True incremental streaming

Subsumes **§D15**. No token savings — this is a liveness fix that makes
§F1/§F4's "thinking indicator that can't lie" true for phase calls, and it
is a precondition for §M3's live cost meter (you cannot meter a call whose
progress you only learn about at the end).

## §L3 Give T2 the flat plan it is documented to have

Subsumes **§D17** and **§D18**, which must land together (see §D18).

**Expected savings, T2 over a spine that would otherwise recurse:** planner
calls go from `1 + N_failing + …` to exactly **1**. On the shipped
`t2-corpus` eval task the measured fresh budget is 5 calls (estimate +
plan + 3 windowed review) — that number stays 5 for a small corpus and
stops growing with corpus size, which is the actual claim §A4 makes and
which nothing currently tests. See §N1.

**Ship gate:** a new eval task `t2-large-corpus` (a spine of 60 units,
each over budget) asserts **exactly one** `plan_level` call at T2, and
asserts the same corpus classifies **T3** when `artifacts <= 8`.

## §L4 Code-tile a slice the planner cannot improve

The planner's judgment buys a *semantically meaningful* grouping. When a
slice cannot be grouped — because every unit in it is already at or over
the leaf budget — the model has no choice to make, and one leaf per unit
is the only legal partition. `build_tree` already forces a leaf at
`len(slice_units) <= 1`; it does not recognize the general case.

**Mechanism** (pure code, no new model surface): before calling
`plan_level`, test

```python
if all(u.tokens >= token_budget for u in slice_units):
    # every unit must be its own leaf; any grouping violates the budget
    for unit in slice_units: forced_leaf([unit], f"{path}.{unit.id}", "unit exceeds budget alone")
    return
```

and symmetrically, if `sum(u.tokens for u in slice_units) <= token_budget`
the whole slice is already one legal leaf — which `leaf_gate` at the
parent should have caught, but which is worth asserting at the top of
`recurse` as a cheap guard against a partition repair having produced it.

This is invariant 5 applied to the planner: "anything a script can
compute, a script computes."

**Expected savings:** on a textbook whose units are chapter-sized (the
motivating case — `DEFAULT_MIN_UNIT_TOKENS = 800`, `DEFAULT_TARGET_UNIT_TOKENS
= 16_000`, budget 24k), the depth-2 and depth-3 calls collapse to zero.
Combined with §L3 this is the largest single planner saving.

**Must not change:** any slice with genuine grouping freedom still goes to
the model. Guard with `RunOptions.code_tile_planner: bool = False` for one
release.

**Ship gate:** a spine of 20 units each at 30k tokens produces 20 leaves
with **zero** `plan_level` calls beyond the top level, and the resulting
`tree.json` tiles the spine exactly (`check_split_parents_derived`'s
sibling check, plus `_repair_partition` reporting no repairs).

## §L5 Skip the orchestrator when the wave consumes the ready set

§E18 already short-circuits `decide_next_action` at `len(ready) == 1`. The
same reasoning extends one step: when `max_parallel >= len(ready)`, the
wave fill takes **every** ready node this round regardless of which one
the orchestrator names, and order within a wave is not observable
(episodes run concurrently, and `depends_on` readiness is recomputed from
the tree, not from dispatch order). The call's answer cannot change the
outcome.

**Mechanism:** pass `max_parallel` into `decide_next_action_with_policy`;
short-circuit with reason `"code-decided: wave consumes the entire ready
set (max_parallel=N >= ready=M)"`.

Only bites when `dispatch_policy="model"` — which is not today's default
(`"deterministic"`) — so this is insurance against the model policy, and
it becomes load-bearing the moment §M2 gives `depends_on` real edges and
an operator turns the model policy on to exploit them.

**Ship gate:** `test_v1_orchestrator_policy.py` — policy `"model"`,
`max_parallel=4`, 3 ready nodes, provider that raises on any call: the
round dispatches all 3 with zero calls.

## §L6 Cache the per-node reviewer verdict

Today `review_and_transition_node` calls `review_node` unconditionally.
Three reachable paths recompute an identical verdict:

1. **Resume.** `run_round_loop`'s resume scan re-reviews every node caught
   in `awaiting_review` — correct, but if the process died *after* the
   verdict was written to `audit/<node>.json` and before the tree save,
   the verdict is on disk and gets recomputed anyway.
2. **Re-dispatch of an unchanged artifact.** An operator redispatch (§E23)
   whose episode produces a byte-identical artifact re-pays the review.
3. **Repair loops.** `v3/repair.py` re-reviews after each repair; a repair
   that touched a different node leaves this one's verdict valid.

§E17 established exactly this pattern for `document_review`
(`audit/document_review.json`, digest of precisely the pass's inputs, skip
on clean match). Apply it one level down.

**Mechanism.** Extend `audit/<node>.json` with a `verdict_digest` field:
`sha256` over `(artifact_text, sorted rubric items, contract text)` — the
complete set of inputs `review_node` reads. On entry, if the stored digest
matches and the stored verdict was `pass`, skip the call and log
`node_review_cached`. **Only cache passes**, never failures: a failing
verdict must be re-earned, because the retry that follows it is supposed
to change the artifact, and caching a failure would let a genuinely-fixed
artifact stay failed.

**Must not change:** invariant 1 — the harness still writes `passed`, and
still only after both gates and a verdict agree. A cached verdict is a
verdict the harness itself recorded, over inputs it has proven identical;
this is bookkeeping, not delegation.

**Expected savings:** eliminates the single largest resume cost at T2/T3.
The eval harness currently *records the waste as expected behavior* for
document review (§E17 fixed that); the per-leaf equivalent is unmeasured.

**Ship gate:** `test_v1_round_loop.py` — a node reviewed to `pass`, then
`run_round_loop` re-entered against the same run dir: zero provider calls,
`node_review_cached` logged, node still `passed`. And the negative: touch
one byte of the artifact, assert the call happens.

## §L7 Stop re-sending document-review overlap and the full spine label list

Two independent wastes in `v3/document_review.py`:

**(a) Window overlap is re-judged.** `DEFAULT_REVIEW_WINDOW = 120`,
`DEFAULT_REVIEW_STRIDE = 100` — a 20-node overlap, deliberately, so
boundary defects aren't split. But the overlap is re-*sent* and
re-*judged* in full, producing duplicate items for the same node pair
(union-merged, no dedup). For N=400 that's 4 windows carrying 480
node-rows for 400 nodes: **20% redundant context, plus duplicate items in
the triage.**

*Fix:* keep the overlap in the *context* (it is load-bearing for boundary
gap detection) but scope the *ask*: the system prompt gains "report items
whose primary node falls in rows K..M" and the code passes that range.
Items outside it are dropped with a `document_review_out_of_scope` log.
Cheap, preserves the boundary check, kills the duplicates.

**(b) The full spine label list ships in every window.** `_merged_render`
renders `extra["spine_labels"]` — every label in the spine — into every
window's prompt, while the node rows are correctly sliced. For a 400-unit
spine that's the whole label list × 4 windows. Window N cannot report a
coverage gap in window M's range (it has no rows there), so the labels
outside its own range are pure cost.

*Fix:* slice `spine_labels` to the window's own unit range plus one label
of margin on each side (the margin preserves the boundary-gap check).

**Ship gate:** measured prompt tokens for a 400-node document review drop
≥25% with identical triage output on a fixed scripted run.

## §L8 Dispatch probes in parallel

`driver._run_structural_exploration`:

```python
for unit in selected:
    ...
    await run_research_query(...)   # fully serialized
```

and `v4/research_loop.py` line 81–83 has the same shape for targeted
probes. §A6 explicitly specifies structural exploration as "dispatchable
in parallel." Up to 8 complete agent episodes (T3 cap) run end to end.

**Fix.** Reuse `run_round_loop`'s exact chunking idiom — the pattern is
already proven in this codebase and its concurrency guards
(`EventLog`'s lock, the tree lock) are already in place:

```python
def chunks(xs, n): return [xs[i:i+n] for i in range(0, len(xs), n)]
for chunk in chunks(selected, self.options.max_parallel_probes):
    await asyncio.gather(*(dispatch_probe(u) for u in chunk))
```

New `RunOptions.max_parallel_probes: int = 1` — **default 1 keeps today's
byte-identical sequence** (rule 2), raise after measuring. Probes write to
distinct `research_finding_path(run_dir, node, unit.id)` files, so there
is no shared-write hazard; assert distinct paths in the wave the same way
`run_round_loop` asserts distinct artifacts.

**Zero token cost.** Pure wall-clock: 8 probes at ~90s each go from ~12
minutes to ~90 seconds at `max_parallel_probes=8`.

**Ship gate:** 6 probes with a fake adapter that sleeps 100ms; assert total
elapsed < 250ms at `max_parallel_probes=6` and > 550ms at 1.

## §L9 Make `max_parallel > 1` the measured default for dependency-free trees

`RunOptions.max_parallel = 1`. Every planner leaf carries
`depends_on=[]` by construction (§4.5 freezes the contract precisely so
leaves are independent), so a 400-leaf T3 tree runs 400 agent episodes
strictly sequentially. §C2 built the whole wave mechanism for this and it
ships off.

This is rule 4 in its purest form — the mechanism shipped, it was never
measured, so the default was never flipped. **Do the measurement.**

**Proposal:** default `max_parallel = min(4, cpu_count)` **only when every
node in the tree has `depends_on == []`** (a code-checkable property of
the loaded tree, not a guess), and keep 1 otherwise until §M2 lands real
edges. Log the derived value as `max_parallel_derived` so it is never a
silent change.

**Risk to manage explicitly:** four concurrent gptme subprocesses against
one free-tier endpoint is a 429 generator. Gate the derived default on
`provider_concurrency` being set, and wire `provider_concurrency` to the
same value — the semaphore §C2 built and documented as "inert today"
becomes load-bearing here, which is the point.

**Ship gate:** the existing `test_v1_round_loop_parallel.py` suite passes
unchanged at the derived default, plus a new assertion that a tree with
any non-empty `depends_on` derives 1.

## §L10 Prove the stable prefix is actually stable

Part VIII lists this as outstanding: "a per-segment token recording
before/after A6-2 asserting the stable prefix is byte-identical across two
different nodes in one run (the actual test for 'prefix caching can
work')." A6-2 reordered `build_node_prompt` for exactly this and nothing
verifies it.

**Mechanism.** `build_node_prompt` already exposes the `segment_tokens`
callback. Add a sibling `segments()` accessor returning the ordered
`(label, text)` list, and assert in test: for two nodes of the same shape
in one run, the concatenation of segments up to and including
`hidden_paths` is **byte-identical**.

This is cheap and it protects a real invariant — any future contributor
who adds a per-node segment above `brief` silently destroys prefix caching
for the entire harness-authored portion of every writer prompt, and today
nothing would notice.

## §L11 Bound `_inputs_exceed_budget`'s re-read

`v1/writer.py:_inputs_exceed_budget` reads every input file from disk to
estimate tokens, before **every** dispatch including retries. The spine
units are immutable once materialized; their token counts are already in
`spine.json`. Read the counts, not the files, when every input resolves to
a known spine unit; fall back to reading for research findings (small) and
unknown paths.

---

# Part III — §M: long-horizon features

Ranked by how much horizon they actually buy per unit of complexity.
Every one obeys the invariants: nothing declares itself done, decomposition
is gated by code, contexts stay bounded, the filesystem is the state.

## §M1 A real cost ledger, and a run-level budget that halts (highest value)

**The gap.** The harness meters nothing. There is no answer to "what did
this run cost," "which node is eating the budget," or "stop before you
spend more than X." §E19 fixed the *loss* of gptme's per-message
usage/cost metadata (`yield from` preserving `_StreamWithMetadata`) — and
then nothing consumes it. The direct provider's responses carry `usage`
in `raw` and nothing reads it. For a harness whose entire premise is
long-horizon work against metered endpoints, this is the largest missing
faculty.

**Why it is a horizon feature, not an accounting feature.** A run that
cannot see its own spend cannot make the trade §A2 invariant 8 describes.
Every adaptive policy below (§M4's model ladder, §M6's review sampling)
needs a cost signal to be adaptive *about*. Metering is the substrate.

**Design.**

- New `cost.jsonl` in the run dir, append-only and fsync'd like
  `events.jsonl` — one line per provider call and per agent episode:
  `{ts, role, phase, node, model, prompt_tokens, completion_tokens,
    reasoning_tokens, cost_usd, cached}`. Harness-derived; nothing a model
  writes.
- `v1/provider.py` records from the response's `usage` block (and from the
  SSE terminal chunk's `usage` when streaming — most OpenAI-compatible
  endpoints emit it with `stream_options: {include_usage: true}`, which we
  should start sending). Adapters record from the metadata §E19 preserved.
- `RunOptions.max_cost_usd` / `max_total_tokens`. On exceedance the driver
  sets `halt.flag` through the **existing** halt path — no new stop
  mechanism, so §E15's three checkpoints and the "never mid-turn" rule
  come for free.
- Dashboard: one header chip `$1.24 · 412k tok`, and a per-node column in
  the tree tab. §9's density rules apply — a number, not a chart (§12
  non-goals is explicit: "no charts").

**Ship gate:** a scripted run's `cost.jsonl` totals match the sum of the
fake provider's declared usage exactly; a run with `max_cost_usd` set below
the scripted total halts at a phase boundary with `run_halted{reason:
"cost ceiling"}` and resumes correctly when raised.

**Depends on:** §L2 (a call whose progress arrives only at completion
cannot be metered live).

## §M2 Populate `depends_on` for real

**The gap.** §A7 says "`depends_on` is populated at T2/T3 whenever the
estimate marks a node as consuming another's artifact (true for code: 'add
the endpoint' follows 'add the model')." `v2/planner.py:add_leaf` hardcodes
`depends_on=[]`, and `PARTITION_SCHEMA` has no field for it. §12's
non-goals even notes "`depends_on` is empty on every planner leaf today."

For prose over a spine this is correct and should stay — chapters are
independent by construction, and §4.5 freezes the contract precisely to
keep them so. **For workspace work it is the difference between a plan that
respects "model before endpoint" and one that races** — §C2's own
motivation, still unrealized.

**Design.**

- `PARTITION_SCHEMA.children[].depends_on: {type: array, items: string,
  maxItems: 3}` — ids of *siblings in this same call*, so the model can
  only name ids it has seen (the same discipline A5-3 applies to probes).
- Code validates: every named id must be a sibling in the same partition;
  unknown ids are dropped and logged (`planner_dep_dropped`). Then **run a
  cycle check** and drop back-edges — a DAG is a code-checkable property
  and a cyclic `depends_on` would deadlock `ready_nodes()` forever, which
  is precisely the kind of model claim invariant 2 says to verify.
- Gate on work-object kind: `depends_on` is requested only when
  `work.kind == "workspace"`. A corpus partition asking for dependency
  edges invites the model to serialize independent chapters.

**Then, and only then, §L9's parallel default is safe in general** rather
than gated on "every node has no deps."

**Ship gate:** a workspace goal ("add the model, then the endpoint, then
the test") produces a tree whose topological order matches; a scripted
cyclic response is broken and logged; `TaskTree.ready_nodes()` never
returns a node whose deps are unpassed. Plus §D23's manifest cache, since
this un-masks it.

## §M3 Content-addressed episode memoization

**The gap.** A repair, a redispatch, an amendment revalidation, and a
resume can each re-run a writer episode whose *complete* input set — the
prompt, the contract, the inputs' bytes, the model — is identical to one
that already produced a gate-passing, review-passing artifact. The harness
has no way to know that, so it re-pays a full agent episode.

**Design.** A `cache/` directory in the runs root (not the run dir —
sharing across runs is most of the value):

- Key: `sha256(prompt_text ‖ model ‖ sorted(input_path, input_sha) ‖
  contract_sha ‖ tool_allowlist)`.
- Value: the artifact bytes, the promotion, and the gate results.
- On hit: write the artifact, log `episode_cache_hit`, and **still
  re-evaluate gates and review from scratch** — never trust the cached
  verdict. The cache short-circuits *production*, never *judgment*.
  Invariant 1 is untouched.
- Opt-in (`RunOptions.episode_cache`, default False), with an
  `--no-episode-cache` escape and a documented `kusudaemon cache clear`.

**Why this is the right long-horizon primitive.** The failure mode of
multi-day runs is not that work is wrong; it is that interruption forces
re-derivation. §10's resume already handles process death. This handles
*semantic* re-derivation: the same leaf, re-asked after an unrelated
amendment.

**Risk to state plainly:** a cache key that under-specifies inputs serves
a stale artifact. Mitigations: hash file *contents*, not paths or mtimes;
include the contract hash (so any amendment invalidates everything);
include the tool allowlist (so §D25's change invalidates); and re-run gates
+ review on every hit so a bad serve fails loudly rather than silently
passing.

**Ship gate:** the same node dispatched twice across two run dirs with an
identical contract produces one episode and one cache hit; changing one
byte of one input produces two episodes; a hit still writes
`audit/<node>.json` with a freshly-computed verdict.

## §M4 Per-role model routing, wired (close §G2's read-path gap)

**The gap.** Part X item 2 is explicit and correct: `model_override.json`
is written by three surfaces (`/model`, `POST /api/model/override`,
`kusudaemon pipeline model`) and **`get_model_for_role` has no callers**.
The write path exists; the read path does not. The backend override, by
contrast, is fully wired and re-read at every dispatch.

This is small and it unlocks real economics: the Reviewer, the
Orchestrator, and the Planner emit **tiny, schema-constrained JSON**. They
do not need the model the Writer needs. Routing review to a cheap model
and writing to a strong one is the single largest cost lever the harness
has, and the config table for it already exists.

**Design.** Mirror `_current_backend()`'s proven shape exactly:

- `_model_for_role(role)` re-reads `model_override.json` per dispatch,
  falling back to `provider.json`'s `roles` table, then `options.model`.
- `_default_writer_factory` resolves the `writer` role.
- `run_round_loop` gains `reviewer_provider` (§G2's outstanding item),
  defaulting to `provider` so every existing caller is byte-identical.
- An invalid override logs `model_override_invalid` and falls back — the
  same defensive shape `backend_override_invalid` already has.

Then **§G5**: header chip `⚙ writer: sonnet-5 · reviewer: haiku`, per-role
selects in the new-run modal. Small, and it makes the routing visible
instead of a file nobody knows to look at.

**Ship gate:** `test_backend_toggle.py`'s sibling for models — override
written, next dispatch uses it, invalid override falls back and logs,
reviewer provider is distinct from writer provider in the round loop.

## §M5 Wire §K: skills, plugins, MCP

Given **§D24** (the discovery module already exists, unwired), the
remaining work is smaller than Part X implies. §K1's design is sound and
should be built as written — the key insight, that gptme has no
`GPTME_CONFIG` env var and the config must be injected **in our own
worker** (`_gptme_worker.py`) via `ProjectConfig.from_dict` + `merge` +
`set_config` before `init_tools()`, is correct and non-obvious.

**Sequence:**

1. `RunOptions.capabilities` → `<run_dir>/gptme-capabilities.toml`
   (`[mcp]`/`[plugins]`/`[lessons]` — exactly the keys `MCPConfig.from_dict`
   accepts). README already documents this file; make it real.
2. `_gptme_worker.py` loads it before `init_tools()`.
3. `GptmeAdapter.__init__(extra_tools=...)` appended to the allowlist;
   `build_writer_adapter` composes node tools + enabled MCP patterns.
   Compose this with **§D25** — the same call site, same release.
4. Worker prints `{"type":"capabilities", "skills":[…], "tools":[…],
   "mcp":[…]}` as its second line so the operator can *see* a skill fired.
   `_agent_worker.py` passes unknown record types through raw, so the
   dashboard's incremental parser needs one new case, not a vocabulary
   change.
5. Attach capabilities per shape in `v6/templates.py` — the natural place,
   and it keeps per-node scoping intact.

**Cost when unused: literally zero.** `create_mcp_tools` short-circuits
unless `config.mcp.enabled` **and** `servers` is non-empty.

**Ship gate:** a run with an empty capabilities config produces a
byte-identical gptme command line to today; a run with one stub MCP server
has that server's tools in the allowlist and prints the capabilities line.

## §M6 Reviewer calibration — measure precision before trusting it

**The gap.** §C5 lists "reviewer catch rate" among the seven unbuilt
measurements. The reviewer is the only thing standing between a plausible
artifact and a `passed` status, and its precision has never been measured.
Worse, `review_node` returns `pass` **with zero calls** when
`node.judgment` is empty — and §C1 exists precisely because the default
planner shipped empty judgment on every leaf. §C1 fixed the population;
nothing verifies the resulting verdicts mean anything.

**Design (cheap, sampled, honest).**

- `RunOptions.review_sample_rate: float = 0.0`. At rate *r*, a passing
  node is independently re-reviewed once at a *different* temperature (or
  a different role-routed model, once §M4 lands). Disagreement is recorded
  to `audit/<node>.json` as `sampled_disagreement: true` and logged — it
  does **not** flip the verdict.
- `eval/measure.py` gains `review_disagreement_rate`. A rate near zero on
  a corpus with planted defects means the reviewer is rubber-stamping; a
  rate near 0.5 means it is noise. Both are things you want to know before
  a 400-node run.
- Plant defects deliberately: extend `eval/tasks.py` with a
  `t2-planted-defects` task whose canned artifacts contain known,
  rubric-violating errors at known positions — including one past the 8k
  fan-out boundary (§B6's own ship gate, now as a standing regression).

**Ship gate:** `review_disagreement_rate` is computable and reported; the
planted-defect task's catch rate is asserted above a floor.

## §M7 The resumption brief — bounded state for a returning operator

**The gap.** §10's resume is process-level and correct. What does not
exist is *human* resume: an operator returning to a 3-day run reads
`events.jsonl` (tens of MB), a feed capped at the last N entries, and a
tree of 400 rows. The §D0c/§B2 work made "is it alive" answerable; "what
happened while I was gone" is not.

**Design — and note this is a *script*, not a model call** (invariant 5):

- `assembly/resumption.md`, regenerated at every phase boundary from
  `tree.json` + `manifest.jsonl` + `cost.jsonl` (§M1) + `events.jsonl`'s
  tail: nodes passed since the last brief, nodes blocked and why (their
  `last_defect`, verbatim), escalations with triggers, approvals pending,
  spend to date and projected, and the single next action.
- Bounded by construction: counts and the ready/blocked sets, never the
  whole tree — the same discipline `_compact_state` applies to the
  orchestrator's context.
- Surfaced as an inspector tab and as `kusudaemon status --brief`.

**Zero model calls.** This is the operator-facing twin of §3's
"orchestrator is stateless per round, rebuilt from disk" — the operator's
context should be rebuildable from disk too, at constant size.

## §M8 Delta runs — re-run only what the source change touched

**The gap.** Change one chapter of a textbook, or one module of a repo,
and the harness re-runs everything. `v3/prefilter.py` already solves the
*analogous* problem for contract amendments (a lexical pre-filter that
safely skips unaffected nodes) — the machinery and, crucially, the
safety argument both exist.

**Design.** Reuse it, keyed on source rather than contract:

- On resume with a changed `source.txt` / workspace, re-chunk and diff the
  spine. Units whose content hash is unchanged keep their nodes' `passed`
  status; changed units mark their nodes `stale` (an existing
  `NodeStatus`).
- A `stale` node's downstream `depends_on` closure is also marked stale —
  the same transitive reasoning `revalidate.py` already does.
- `source.txt` is protected on resume today (§11.9, and §D12's poisoned
  run notes it explicitly). So this needs an explicit
  `kusudaemon resync <run-id> --source <path>` command rather than
  silently re-reading — the protection exists for good reason and should
  not be weakened, only given a deliberate door.

**Highest leverage on the actual use case.** A textbook or a codebase is
edited during a long run far more often than it is authored once.

**Ship gate:** a 10-unit corpus run to completion, one unit edited,
`resync` marks exactly that unit's node and its dependents stale and
re-runs only those.

---

# Part IV — §N: measurement (closing §C5's and Part VIII's residue)

Efficiency claims that nothing asserts regress silently. That is the
lesson Part VIII already recorded — "a `t2-corpus` eval assertion on the
survey call count specifically (the number that regressed silently)" — and
§D17 is the same lesson repeating.

**§N1 — Call-count assertions per tier, per phase.** `eval/runner.py`
already asserts fresh-run budgets (T0=1, T1=1, T2=5, T3=2). Extend to
per-*phase* counts, and add the large-corpus tasks §L3 and §L4 need:
`t2-large-corpus`, `t3-recursive`. **These are the tests that would have
caught §D17 and §D18 the day they were introduced.**

**§N2 — The survey-call regression assertion.** Part VIII's named
outstanding item. Assert `t2-corpus` spends **0** survey calls at the
default `survey_mode`, and that an unrecognized mode also spends 0
(§D21's guard).

**§N3 — Prefix stability.** §L10's byte-identical-prefix assertion.

**§N4 — The remaining §C5 measurements**, now that §M1 and §M6 supply the
instruments: reviewer catch rate (§M6), orchestrator context bound (assert
`_compact_state` length is flat from a 20-node to a 400-node tree — a
direct test of invariant 3), mean input tokens by prompt segment (the
`segment_tokens` callback already exists), resume-after-kill-9, approval
rate by shape.

**§N5 — `kusudaemon eval` CLI.** The runner is a library only. A one-command
`kusudaemon eval --task t2-corpus` is what makes any of the above get run.

---

# Part V — Sequencing

Four waves. Each ends with the full suite green and the eval budgets
re-measured.

**Wave 1 — correctness and the tier fix (highest urgency).**
§D14, §D15, §D16, §D17, §D18, §D19, §D20, §D21, §D27, plus §N1's
`t2-large-corpus` task so the tier fix is asserted rather than asserted-to.
*This wave changes a classification boundary — re-measure every eval
budget after it, and expect `t3-*` counts to move.*

**Wave 2 — free savings, no new surface.**
§L1, §L2 (both subsumed above but re-verified here), §L4, §L5, §L6, §L7,
§L8, §L10, §L11, §D22, §D23, §D25. Every item is either a cache, a code
short-circuit, or a parallel dispatch; none change what a model is asked.
Flip §L9's default at the end of this wave, on measurement, not on
principle.

**Wave 3 — the instruments.**
§M1 (cost ledger), §N2–§N5. §M1 is the substrate for Wave 4's adaptive
policies; building the policies first would mean tuning them blind.

**Wave 4 — horizon.**
§M4 (model routing — small, unlocks the economics), §M2 (`depends_on`),
§M3 (episode cache), §M6 (reviewer calibration), §M7 (resumption brief),
§M5 (§K capabilities), §M8 (delta runs). §M4 first because it is the
smallest and every later item is cheaper to run once review is routed to a
cheap model.

**Documentation debt to clear alongside.** CLAUDE.md needs five
corrections found during this audit, and each belongs in the commit that
makes it true rather than a doc-only pass:

| Claim | Location | Reality |
|---|---|---|
| "T2 plans one flat level and stops — shipped" | §A7 pt 2 | Not implemented (§D17) |
| T2 row "`artifacts <= 8` **or** `work_tokens < 150k`" | §A4.3 | Admits unbounded corpora (§D18) |
| "`survey_mode` defaults to `embedding`" | Part II v2 | Defaults to `auto`→structural; embeddings hard-disabled (§D21) |
| "per-node `tools` emitted by the planner — unbuilt" | Part X item 5 | Shipped via `v6/templates.py`; partly undone by §D25 |
| "§K NOT SHIPPED (only documented)" | Part VII §K, Part X item 1 | `v7/capabilities.py` exists and is dead (§D24) |

---

# Appendix — expected effect

Directional, for a T3 run over a large corpus. Percentages are of the
*harness's own* call and token cost; writer episodes dominate absolute
spend and are deliberately not reduced here — reducing them is what would
cost quality.

| Change | Effect |
|---|---|
| §L3 (T2 flat plan + tier fix) | Planner calls at T2: `1 + N` → `1`. Removes the largest uncapped call source remaining after §A2's survey fence. |
| §L4 (code-tiled slices) | Planner recursion calls → 0 on chapter-sized spines. |
| §L6 (review cache) | Per-leaf review on resume: N → 0 when nothing changed. |
| §L7 (doc-review windows) | Document-review prompt tokens −25%. |
| §L1 (duplicate reasoning) | Trace bytes −50%; dashboard duplication gone. |
| §L8 + §L9 (parallelism) | Wall-clock on explore ≈ ÷6–8; on execute ≈ ÷4. Zero token change. |
| §M4 (role routing) | Reviewer/orchestrator/planner spend re-priced to a cheap model — the largest single lever, and the config table already exists. |
| §M3 (episode cache) | Re-derivation after an unrelated amendment → 0 episodes. |

The one thing worth restating: **none of the above reduces what any model
is asked to judge.** Every saving is a call the harness did not need to
make, a token it did not need to re-send, or a wait it did not need to
serialize. The quality surface — gates, verdicts, the contract, the
review fan-out — is untouched, and §M6 adds the first real measurement of
whether that surface works at all.
