# PLAN-CONCURRENCY-AND-SHARED-STATE.md

Everything proposed in the 2026-09-06 benchmark-planning session, with the case
against each and, where the objection has an answer, the fix. Nothing here is
decided. Read §0 before costing any of it.

`docs/PLAN-WORKSPACE-MODE.md` is the prerequisite work; this file is the layer above
it. `PLAN-BENCH-INTEGRITY.md` (the 2026-09-06 post-sweep audit) sits *below*
both: it is the set of repairs required before any of this can be measured at
all, and it supersedes the status of §A4 and §A5 below.

Two earlier proposals were dropped outright and are recorded here so they are not
re-proposed: a **global environment ledger** (shared verified facts injected into
every writer brief — violates the bounded-context invariant, has no cheap staleness
answer, and its "machine-checkable" admission gate is too narrow to repay its
complexity), and **static node resource classes** (`isolated` / `exclusive` —
misclassification fails silently and blames the victim node, and almost everything
that runs tests classifies `exclusive` anyway). §B4's environment lock is the
surviving idea from the second one, and it acquires dynamically rather than
classifying up front.

---

## §0. The objection that applies to all of §B

**These are scaffolding, not capability.** None of them raise the score on a task
whose individual leaf is beyond the model. The measured evidence to date says arm C
loses to bare opencode for *prompt and tool-policy* reasons — the prose template,
the denied bash, the artifact-contract prompt, the redispatch waste — not because it
ran out of throughput. Orchestration concurrency is not the bottleneck any current
number identifies.

Land `docs/PLAN-WORKSPACE-MODE.md`, re-measure, and only then decide whether anything in
§B is worth its complexity.

**Amendment, 2026-09-06 (post-sweep audit).** The clause "the measured evidence
to date" is now weaker than it reads. Eighteen of the thirty-six HarnessBench
arm C runs were provider failures written into `bench_results/` with numeric
scores; only 014 and 057 arm C survive the audit, and even those ran with all
five §A5 flags off. The conclusion above still holds — nothing in the surviving
data points at throughput — but it now rests on two tasks, not twelve. The
correct reading is "no current number identifies concurrency as the bottleneck,
and very few current numbers identify anything." `PLAN-BENCH-INTEGRITY.md` §0
lists what to quarantine and §6 orders the repairs; **that plan comes before
this one in full**, including before the §C order below.

---

## §A. Benchmark process changes

### §A1. Drop DeepSWE; spend the budget on LHTB
Binary pass/fail with no partial credit. Its weakest evaluated models (Gemini 3
Flash 5%, DeepSeek v4-pro 8%) are far above nemotron-30b-a3b. Both arms score ~0 and
the sweep buys no signal. LHTB's dense subtask reward can show partial credit.

**Against.** DeepSWE is the one benchmark on the list with outside recognition. If
the goal is a claim other people will credit, name recognition has value a
better-shaped reward does not.

### §A2. Task shortlist
`apex-law433-matter`, `langchain-version-migration`, `commit0-multilib-tdd`,
optionally `climate-netcdf-extreme-event-audit`. Selected by two tests: is there a
cheap cut in the dependency graph, and is the difficulty in the size of the job
rather than inside the leaf. See BENCHMARKING.md §5.3 for the full rationale and the
rejected tasks.

**Against.** `apex-law433-matter` is the least code-like task in the benchmark, so a
win there proves long-form document-and-state competence that LongGenBench,
WritingBench and HelloBench already measure — while the coding-agent claim stays
untested. Its stages are also *reactive*, and the pipeline plans a tree up front; if
that mismatch is real the task measures an integration gap, not the architecture.

### §A3. Phase 0 calibration run before committing to a matrix
One task, arm C, one seed, unscored: measure real tok/s on a real repo, measure
per-episode RSS for §B8, and check the reactive-stage protocol.

**Against.** ~20M tokens and a laptop-day for no scored data point, and if the flag
configuration changes afterwards the numbers describe a config you no longer run.

### §A4. Fix arm A token capture (`pipeline/cli.py:991`, `_parse_opencode_usage`)
Without it `tokens_by_role` is `{}` for every arm-A record and BENCHMARKING.md §0.1's
token-parity precondition has never been verifiable.

**Against.** Almost none on the merits — but name the likely outcome: arm C is
plausibly 3–10x arm A in tokens. "Wins by 0.1 reward at 6x the token cost" is a
materially weaker claim than the one currently unfalsifiable.

**Status 2026-09-06: still open, and still first.** Confirmed unchanged at HEAD
across every arm A record in `bench_results/` and `bench_results/longgen/`.
Tracked as `PLAN-BENCH-INTEGRITY.md` §4.6.

### §A5. Decide the five default-off flags explicitly and record them
`KUSUDAEMON_TIER_TRUST_SIGNALS`, `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE`,
`KUSUDAEMON_DIRECT_TEMPLATE`, `KUSUDAEMON_DIRECT_TOOLS`,
`KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT`.

**Against.** Freezing a configuration before knowing which setting is better means
the sweep measures one arbitrary point in a 32-point space; varying them is worse.

**Status 2026-09-06: this is now blocking, not deferrable.** All five default to
`"0"` and **no benchmark entry point sets any of them** — not `hb_adapter.sh`,
not `run_harness_bench.py`, not `run_longgen_bench.py`. So the 014 and 057 arm C
re-runs at 14:25–15:53, the only clean late arm C data in the sweep, measured
the *unfixed* path, and every diagnosis in `docs/PLAN-WORKSPACE-MODE.md` remains both
unrefuted and unconfirmed. The objection above stands, but the status quo is not
a neutral default: it is the one point in the 32-point space already known to be
wrong. Minimum resolution: a `--flags` pass-through on both scripts, the
resolved flag set recorded in every record, and the §K7 A/B ladder
(`docs/PLAN-WORKSPACE-MODE.md:761-767`) at one seed on 014 and 057.

---

## §B. Code changes

### §B1. Widen the `max_parallel` auto-derivation
`driver.py:2069` gates auto-derivation on `all(not node.depends_on ...)`. The wave
filler (`round_loop.py:658`) already fills from `tree.ready_nodes()`, which respects
dependencies by construction, so the whole-tree test is not a correctness
requirement — it is a conservative trigger testing the wrong property. One edge
anywhere in a 40-node tree forces fully serial execution even when 30 nodes are
mutually independent. Gate on ready-set width instead.

Secondary benefit: `orchestrator.py:107` skips the dispatch model call entirely when
`max_parallel >= len(ready)`, so raising it *removes* one provider round-trip per
multi-ready round.

**Remaining objection after §B7 and §B8.** The resume guarantee narrows:
`max_parallel=1` is documented as producing a byte-identical event sequence, and
that determinism is what makes the Layer 1 crash matrix tractable. At N>1 the matrix
either covers a configuration you do not run, or it must cover a much larger
interleaving state space. Decide deliberately which one, and say so in
`BENCHMARKING.md` §9.2, which already records this as an open question.

### §B2. *(withdrawn — global environment ledger; see the header note)*

### §B3. Discovery-driven tree proposals
`v7/split.py` already mutates the tree mid-run, but only downward and only
reactively to a budget overrun. No agent can propose a sibling node for work it
discovered. Proposal: agents emit proposals, code validates them (gate present,
artifact path present, non-conflicting write set) and accepts or rejects — the
existing `handle_split_proposal` pattern.

**Against.** It bypasses the spend approval `amend` exists to provide; a proposal
budget puts you back on a token ceiling, which §3.10 argues fails unsafely. A
proposes B, B proposes C, so any depth cap is arbitrary. And it destabilises
measurement: two seeds of one task build different trees for model-internal reasons,
widening variance where n=3 already cannot resolve ±0.08.

### §B4. Worktrees, made workable

The original form had three problems. Each has an answer.

**Problem 1 — the planner cannot declare `writes:` globs.** It never sees source
content by design. *Fix: stop predicting.* Run each wave member in its own worktree,
then compute the actual change set afterwards (`git status --porcelain`) and apply
the patches sequentially onto trunk. Conflicts are detected, never predicted, which
deletes the objection rather than mitigating it — optimistic concurrency control,
the same trade a database makes. On conflict the loser is redispatched *rebased onto
the winner's trunk* with the conflicting hunk as a declared input; that is the
existing redispatch vocabulary, the only new thing is the conflict rides along. Cap
it: a node that loses twice runs alone in the next wave against current trunk, which
guarantees termination.

**Problem 2 — semantic conflicts pass textually.** A clean 3-way apply can still
break the build, and gates running only in the worktree never see it. *Fix: gate
twice.* Cheap gates in the worktree (fast fail), then the post-merge gate set on
trunk after the apply; a node is `passed` only post-merge. That converts "clean
apply, broken build" into an attributable failure. Commit once per applied node so
attribution is exact and `git bisect` works when a late gate fails. Cost: gates run
twice, which is noise for `nonempty`/`max_tokens`/`headers` and real for a test
suite — so make the post-merge set configurable, usually build plus the tests
touching that node's files.

**Problem 3 — worktrees do not isolate the environment.** Installed packages, build
outputs, databases and bound ports are shared no matter how many worktrees exist.
*Fix: split the axes.* Files go to the worktree; the environment gets a single
run-level lock that any environment-mutating command must hold, so those steps
serialize while file edits stay parallel. Acquire it **dynamically at the tool
layer** — you already own the shell wrapper, so a pattern match on the command line
(`pip install`, `npm i`, `apt`, `docker`, a port bind) takes the lock automatically.
This is the important difference from the withdrawn static classification: a
mismatch degrades to "held the lock unnecessarily" (slower) rather than "two nodes
corrupted each other" (silently wrong).

Non-git fixtures: `git init` a scratch repo in the sandbox, or use hardlink copies
(`cp -al`) per node and rsync the changed files back.

**Residual, accepted.** For LHTB the true isolation boundary is the container, and
nesting containers inside the task container is not worth it. Parallelize the
read/analyze/edit leaves and let build-and-run leaves serialize through the
environment lock. A task that is mostly build-and-run gets no parallelism — that is
the correct outcome, not a defect.

### §B5. *(folded into §B4 — declared write globs are replaced by observed diffs)*

### §B6. *(withdrawn — static node resource classes; see the header note)*

### §B7. Rate-limit control for parallel waves

**What exists.** `v1/provider.py` already has a six-rung 429 ladder that honors
`Retry-After`, falls back to another model on the second rung, and shares a
`threading.Semaphore(concurrency)` throttle across callers. That covers **HTTP role
calls only**. Writer episodes go through the CLI, where `_agent_worker.py:863` only
pattern-matches "rate limit" in the log to kill the process quickly.

**The concrete defect.** After that kill, `round_loop.py:762` / `:801` increment
`node.attempts` and set `blocked` at `max_attempts`. So three consecutive 429s
permanently block a node that was never actually attempted — and parallelism makes
consecutive 429s much more likely. Fix this before raising `max_parallel` at all.

Fixes, in order of value:

1. **Classify throttling as a non-attempt.** A distinct episode outcome that returns
   the node to `pending` without incrementing `attempts`, recorded as a
   `node_throttled` event. Everything below is pointless without it.

   *Build this together with `PLAN-BENCH-INTEGRITY.md` §2.4.* An episode
   **timeout** is currently mis-reported the same way — `round_loop.py:767`
   emits `node_gate_failed` with `unmet: []` and
   `last_defect = "episode did not complete"`, so a node that ran out of wall
   clock is indistinguishable from one that failed a gate, burns an attempt, and
   cannot trigger the split that exists for exactly that case. Throttle and
   timeout are the same refactor of the episode-outcome enum; doing them
   separately means touching `round_loop.py`'s attempt accounting twice.
2. **One admission controller per run**, shared by role calls and writer episodes.
   Today the HTTP throttle and the wave size are independent, so at
   `max_parallel=16` sixteen CLI processes plus role traffic hit one endpoint with
   no shared view of it. The wave filler should acquire from the same semaphore the
   provider uses.
3. **AIMD on wave size.** Start at 2; after a wave completes with zero throttles,
   +1; on any 429, halve and set a cooldown of `max(Retry-After seen, backoff
   rung)`. This is TCP congestion control, the wave boundary is already the natural
   adjustment point, and it self-tunes to whatever the free tier is doing that hour
   — which no static number can.
4. **Stagger wave starts.** N episodes launching together is a thundering herd whose
   first requests land in the same few milliseconds. Jitter each start by
   `random.uniform(0, 2s)`; this alone removes most correlated 429s.
5. **Honor `Retry-After` run-globally.** When any caller sees one, publish a
   "closed until T" to a run-level gate every in-flight episode checks before its
   next request. Otherwise fifteen episodes keep hammering through one episode's
   backoff.
6. **Quarantine, do not score.** A run whose `halt_reason` names throttling is
   excluded per the existing rule. With parallelism the throttle rate rises, so this
   matters more, not less.

### §B8. Hardware admission control

**The concrete defect.** `driver.py:2072`:

```python
derived_parallel = min(16, max(8, (os.cpu_count() or 1) * 2))
```

`max(8, ...)` is a **floor**, so the derivation can never choose fewer than 8
concurrent writer episodes, and on an 8-core machine it picks 16. Each one is a node
process plus an opencode CLI plus a model stream, on the same laptop running amd64
Docker under emulation.

Fixes:

1. **Floor at 1, not 8** — `min(hard_cap, max(1, ...))`. One-line change, and it is
   the difference between "parallelism is opt-in" and "parallelism is mandatory once
   the tree is dependency-free".
2. **Derive from memory, not cores.** Writer episodes are network-bound; they mostly
   wait. The binding resource is RSS, not CPU. `concurrency = clamp(1,
   available_bytes * safety / measured_episode_rss, hard_cap)`. Measure
   `measured_episode_rss` once in the §A3 calibration run; default conservatively
   (~600 MB/episode) until it is measured.
3. **Admit at wave-fill time, not only at derivation.** Before appending the k-th
   node to a wave, re-read available memory and stop filling below a floor. Waves
   shrink under pressure instead of the machine swapping. `vm_stat` on macOS,
   `SC_AVPHYS_PAGES` on Linux — no new dependency, `pyproject.toml` stays stdlib.
4. **Subtract Docker.** Docker Desktop's VM allocation is a fixed reservation, so
   total RAM is not available RAM. Give the benchmark containers explicit CPU and
   memory limits and subtract them from the harness budget, or the two sides fight
   for the same cores and both get slower.
5. **Separate the two pools.** Role calls are HTTP with near-zero local footprint;
   writer episodes are subprocesses. Only the latter needs a hardware bound.
   `max_parallel` currently conflates them.

   Prerequisite: writer episodes currently **survive the run that spawned them**.
   `_agent_worker.py:974` omits `start_new_session=True` and every kill path
   targets the direct child only, so a killed run leaves an orphaned CLI writing
   to `out/` and consuming the same rate limit — observed for 35 minutes past a
   kill, inflating the next two seeds' classify wall clock from 194s to 320s. At
   `max_parallel=1` that is a measurement bug; at N>1 it is an unbounded process
   leak. Fix `PLAN-BENCH-INTEGRITY.md` §2.2 before raising anything here.
6. **Thermal, optional.** `pmset -g therm` reports `CPU_Speed_Limit` on macOS; halve
   the wave and cool down when it drops below ~90. Probably subsumed by (2)–(4),
   since network-bound episodes generate little heat — the heat is emulated Docker,
   which argues for (4) rather than for polling.

---

## §C. Order

**Amended 2026-09-06.** `PLAN-BENCH-INTEGRITY.md` §6 items 1–6 come before all
of this. They are small, none of them needs a model call to verify, and without
them a re-measurement produces the same unreadable `bench_results/` the audit
just went through by hand.

0. `PLAN-BENCH-INTEGRITY.md` §6 steps 1–6 — process-group kill, manifest
   salvage, the tier key mismatch, aggregate rebuild and quarantine, role
   `max_tokens` and the classify fallback, the timeout outcome and budget clamp.
1. §A4 (arm A token capture) — cheap, and everything else is unmeasurable without it.
   (Also `PLAN-BENCH-INTEGRITY.md` §4.6.)
2. §A5 — decide the five flags and wire a `--flags` pass-through, then
   `docs/PLAN-WORKSPACE-MODE.md` in full, then re-measure HarnessBench including the
   seven quarantined arm C tasks. §A5 moved ahead of the re-measure because
   re-running with the flags still off repeats the sweep that produced no
   answer.
3. §A3, then the §A2 shortlist at one seed.
4. §B7.1 and §B8.1 before `max_parallel` is ever raised — they are small and they
   prevent the two failure modes that would otherwise be blamed on parallelism.
   §B7.1 shares its refactor with `PLAN-BENCH-INTEGRITY.md` §2.4;
   §B8's process-pool work is blocked on §2.2.
5. Nothing else in §B until step 2's numbers say which part of the system is
   limiting.
