# BENCHMARKING.md — the complete guide to running the external benchmarks

This is the all-in-one benchmark document: the experimental design, the setup,
the commands, and what each number you get back actually means. It absorbs the
design material that used to live in `TESTING.md` and the still-open items from
`TEST-PLAN.md`; both are archived under `docs/` and are history, not
instructions.

Everything here is free beyond your own model API calls. Where a benchmark's
normal path routes through a paid or account-gated third party, the free
alternative is spelled out and the tradeoff is stated.

**Where the other live documents fit:**

| Document | Covers |
|---|---|
| `BENCHMARKING.md` (this file) | design, setup, commands, and reading results — everything about running a sweep |
| `PLAN-BENCH-INTEGRITY.md` | what the first sweeps found, which runs to quarantine, and the repairs still open |
| `docs/PLAN-WORKSPACE-MODE.md` | why arm C loses on 014/057: prose template, denied bash, artifact-contract prompt |
| `PLAN-CONCURRENCY-AND-SHARED-STATE.md` | parallelism, worktrees, rate-limit control — proposals, none decided |
| `CLAUDE.md` | commands and architecture overview |

---

## 0. The experimental design — read this before running anything

Sections 0.1–0.4 are what make these runs worth doing. Skipping them produces
numbers that look like results and are not.

### 0.0 Status, 2026-09-06

The hermetic layers are green (§9.1), so this document is live rather than
prospective. Two benchmarks have been run:

| Benchmark | Shape | Adapter | Arms × seeds run | Usable result? |
|---|---|---|---|---|
| HarnessBench | A | `scripts/hb_adapter.sh` + `scripts/run_harness_bench.py` | 12 tasks × {A,C} × 3 | **Partly.** 18 of 36 arm C runs are provider failures scored as capability; only 014 and 057 arm C are clean. |
| LongGenBench | B | `scripts/run_longgen_bench.py` + `scripts/eval_longgen_free.py` | 1 task × {A,C} × 3 | **No.** Arm C never decomposes on a Shape B task, so it measures arm A plus two role calls. |

`PLAN-BENCH-INTEGRITY.md` is the audit: which runs to quarantine, why, and the
repairs required before the next sweep. Read it before interpreting anything in
`bench_results/`. Do not treat the `summary.json` files there as the sweep —
both scripts currently aggregate only the cells of their last invocation
(`PLAN-BENCH-INTEGRITY.md` §4.1); the per-cell JSON files are the data.

### 0.1 The methodological rule: three arms, always

Kusudaemon's claim is not "a model can do this task." It is "this model does
this task *better under kusudaemon* than without it, at comparable spend."
A single number against a benchmark cannot support that claim. Every
benchmark run below must be run in three arms:

| Arm | Configuration | What it isolates |
|---|---|---|
| **A — bare** | the backend CLI alone (gptme / Claude Code / Codex / OpenCode), no kusudaemon | the model's own ceiling |
| **B — decomposition only** | kusudaemon with review agents disabled | what recursive decomposition buys |
| **C — full** | the complete pipeline | what verification adds on top |

***User note: for the sake of cost-saving, we will only benchmark arms A and C, skipping B. We will run each benchmark 3 times to shrink variance.***

Hold **token spend**, not task count, roughly equal across arms — kusudaemon
spends more calls per task by construction, so an unequalized comparison
flatters it for the wrong reason. Report cost-per-solved-task alongside pass
rate.

If arm C does not beat arm A at equal spend, that is your single most
valuable experimental result, and you want it in week one rather than after
another month of features. Design the harness so that finding is *easy* to
surface, not easy to explain away.

Run **N = 3** seeds minimum. Agent benchmarks are high-variance; a single run
difference of a few points is noise.

**Two rules the first sweeps forced into existence:**

- *Token parity is currently unverifiable.* Every arm A record carries
  `tokens_by_role: {}` — `pipeline/cli.py:991-996` fills it only when
  `_parse_opencode_usage` finds usage in the captured stream, and it does not.
  Until that is fixed (`PLAN-BENCH-INTEGRITY.md` §4.6), the ±10% precondition
  above cannot be checked, and therefore no arm delta published so far is
  defensible under this file's own rule. Fix it before the next sweep, and
  expect the honest answer to be that arm C costs several times arm A.
- *Quarantine, do not score.* A run whose `halt_reason` names a transport
  failure — an HTTP status, a provider 404/429/5xx, a role-episode timeout, an
  auth or billing message, a harness-side kill — is not a data point. It must
  be excluded from `mean_score`, `resolve_rate` and every token aggregate, and
  reported separately with a count. n = 3 cannot resolve a ±0.08 delta; a
  single outage scored as a zero swamps it entirely. A second, independent
  check that costs nothing: within a (task, arm) group, identical artifact byte
  lengths across all seeds means the runs did no work.

### 0.2 The integration problem: two shapes

Most agent benchmarks assume a **shared-terminal** interaction model: one
session, the agent issues shell commands, the environment persists, a grader
inspects final state. Kusudaemon's model is different — decompose, dispatch
one leaf per episode, gate each artifact, assemble. Two integration shapes,
and which you need depends on the benchmark:

**Shape A — kusudaemon as the agent under test.** The benchmark hands you a
task directory and an instruction; you run to completion and it grades final
state. This maps onto the **workspace work object** (`v6/work_object.py`) with
the task directory as the work root. One entry point:

```
kusudaemon bench --goal-file <instruction> --workspace <task-dir> \
                 --backend <name> --tier auto --budget-tokens <cap> --json
```

**This exists** as `pipeline/cli.py::cmd_bench`, wrapped for HarnessBench by
`scripts/hb_adapter.sh` (which HarnessBench's `generic_cli` adapter invokes per
round) and driven across the matrix by `scripts/run_harness_bench.py`. It emits
the record in §0.4's shape. Setup is §3.

One caveat that bit the first sweep: `hb_adapter.sh` exits 0 even on a halt, by
design — HarnessBench stops issuing rounds the moment an adapter exits non-zero,
which would forfeit rounds 2..N of the eight multi-round tasks. The true status
lives in the sidecar record, so read `halt_reason` there and never infer success
from the exit code. `KUSU_BENCH_STRICT_EXIT=1` opts out.

**Shape B — corpus tasks.** Long-form generation benchmarks hand you source
material and a rubric rather than a container. These map onto the **text work
object** and need no container plumbing at all — they are the cheapest external
signal available to you, and they are the only ones that exercise the half of
kusudaemon that generates documents rather than editing code.

**This exists** as `scripts/run_longgen_bench.py` (matrix driver) and
`scripts/eval_longgen_free.py` (upstream's two metrics, ported off vLLM). Setup
is §4. Pass `--work-object text --source @<prompt>`: measuring an empty
workspace for a generation task forces the shell tool allowlist onto a prose
writer and collapses the spine to a single synthetic unit.

**Known blocker, unfixed.** A Shape B task is small-input / large-output, and
every gate that could produce a decomposed tree is keyed on *input* size, so
arm C runs a Shape B task as a single writer episode no matter how many units
the prompt demands. LongGenBench task 300 asks for 100 blocks and dispatched one
subagent. That is not a scoring problem — completion was 99/100 and 100/100
against arm A's 42/100/10 — it is a construct-validity problem: the arm does not
contain the mechanism the benchmark was chosen to test.
`PLAN-BENCH-INTEGRITY.md` §1 has the diagnosis and the fix.

**Two constraints to design around:**

- **Wall-clock caps.** Several benchmarks impose a per-task timeout (90 minutes
  is common). Kusudaemon runs subagents strictly in series, so a decomposition
  into many leaves can blow a wall-clock cap while spending fewer tokens than a
  single-session agent that ran out of context. Record wall clock as a
  first-class metric or you will misread your own results. Note also that the
  pipeline's own per-node budget can exceed the benchmark's cell cap
  (`PLAN-BENCH-INTEGRITY.md` §3.1) — check the two against each other before a
  sweep, not after.
- **Credential isolation.** The adapters deliberately never forward harness
  provider credentials to backend CLIs. Benchmark containers usually expect a
  single API key in the environment. Decide explicitly how each arm is
  authenticated and record it, otherwise arms A and C will silently run
  different models. §2.4 is the checklist.

### 0.3 What a defensible claim looks like

Not: *"kusudaemon scores X on benchmark Y."*

But: *"on the N tasks of benchmark Y's long-horizon category, with model M held
fixed and token spend equalized within 10%, the full pipeline resolved
p_C ± σ versus p_A ± σ for the bare backend, over 3 seeds; the gain
concentrated in tasks requiring more than K artifacts, and vanished below
that threshold."*

The second sentence is the finding. The threshold — where decomposition starts
paying for itself — is the most useful thing this whole exercise can tell you,
and it is also the number that tells you which tier boundaries in
`v6/tiering.py` are set correctly.

Two things currently stand between the sweeps and that sentence, and both are
mechanical rather than conceptual: token spend is not captured for arm A, so
"spend equalized within 10%" cannot be asserted; and the tier is not recorded at
all (§0.4), so "the gain concentrated in tasks requiring more than K artifacts"
cannot be grouped. Neither is expensive to fix, and neither can be worked around
by running more seeds.

### 0.4 Recording results

Reuse `eval/measure.py` rather than inventing a second accounting system, so
external numbers stay comparable with the internal eval. `calls_by_role`,
`tokens_by_role`, `mean_tokens_by_segment` and `escalation_events` all apply
unchanged to a real-provider run — that portability is stated in the module
docstring and is worth preserving.

Write one JSON record per (benchmark, task, arm, seed):

```json
{
  "benchmark": "harness-bench",
  "task_id": "...",
  "arm": "C",
  "seed": 1,
  "model": "...",
  "backend": "...",
  "score": 0.0,
  "resolved": false,
  "tier_measured": "T2",
  "tier_final": "T2",
  "escalations": [],
  "calls_by_role": {},
  "tokens_by_role": {},
  "wall_clock_s": 0,
  "halt_reason": null,
  "commit": "..."
}
```

Keep `commit` in every record. Benchmark numbers without the harness revision
that produced them are not reproducible, and you will change the prompts.

A budget or round halt is a **recorded outcome, not a crash**. Uncapped agent
runs are how benchmark budgets evaporate; a halt with a reason is a result you
can read, and the scripts treat it as one.

`pipeline/cli.py::cmd_bench` emits this shape and is the only thing that should.
Four corrections to what it currently produces:

- **`kusudaemon bench --json`'s `score` is not a benchmark grade.** It is 1.0
  if the pipeline reached `done`. Only the external driver's merged record
  carries the oracle score. Always quote that one.
- **`tier_measured` and `tier_final` are always `null` for arm C.**
  `cli.py:1170` reads `measured`/`final`/`effective`; `driver.py:1077` writes
  `measured_tier`/`tier`. The keys have never matched, so the tier calibration
  §0.3 calls the most useful output of this whole exercise has never been
  recorded. (`PLAN-BENCH-INTEGRITY.md` §2.3 — a one-line fix.)
- **A run that halts after producing a passing artifact records
  `artifact_path: null` and `score: 0.0`.** `artifact_path` is learned only from
  a `run_completed` event, so a transient error anywhere after the writer
  discards the work. It has already happened: a complete, gate-passing 100/100
  LongGenBench document was recorded as a zero. (`PLAN-BENCH-INTEGRITY.md` §2.1.)
- **Arm A and arm C records have different key sets.** Arm A carries
  `session_id` and `round`; arm C carries `attended`. A record with arm A's keys
  and `"arm": "C"` did not come from this build and must not be scored.

Add `valid` / `invalid_reason` to the schema when §0.1's quarantine rule is
implemented, so an excluded run is excluded in the data rather than by hand.

### 0.5 Why these benchmarks and not others

The operational detail for each is in §3–§6; this is the case for spending the
wall clock on it at all.

- **HarnessBench (§3)** is the only published benchmark built to measure *the
  harness* rather than the model: environments, budgets and evaluators are held
  fixed while the harness varies, which is exactly this experimental design.
  Its central finding is directly load-bearing — **stronger models show low
  variance across harnesses; weaker models are far more sensitive to the
  execution substrate.** Targeting cheap models is therefore the regime where a
  good harness should show the largest effect, and if it does not, that is
  itself the most informative result available.
- **Long-form generation (§4)** is the half nothing else covers. No agent
  benchmark evaluates corpus-scale document generation, which is the use case
  kusudaemon was designed around. All three options lean on LLM-as-judge, which
  has known reliability limits for long outputs: use them for *relative*
  comparison across arms with the judge held fixed, never for absolute quality
  claims, and supplement with machine-checkable structural metrics you already
  have — assembly compiles, every source unit covered, no cross-leaf
  duplication — because those are objective and the judge is not.
- **LHTB (§5)** is the one whose scoring shape fits the thesis: **dense subtask
  reward** rather than binary pass/fail. Binary pass/fail on a decomposition
  harness tells you almost nothing — a run that got 90% of the way there scores
  identically to one that never started. Partial credit localizes *which leaf*
  failed, which is precisely the diagnostic this architecture needs. It is also
  by far the most expensive item here; §5.2 costs it before you commit.
- **GAIA (§6.1)** exercises `v4/research.py` and the probe scheduler, which
  nothing else on this list touches. Good weekly regression once set up.
- **SWE-bench Verified (§6.2)** maps cleanly onto the workspace path and has
  plenty of off-the-shelf runners, but most instances are small enough to land
  in T0/T1, so it barely exercises decomposition. Use it as a correctness check
  on the workspace path rather than as evidence for the long-horizon thesis. If
  you want it to test decomposition, filter to the multi-file instances.
- **DeepSWE was evaluated and rejected.** Binary pass/fail with no partial
  credit, and its weakest evaluated models are far stronger than
  nemotron-30b-a3b, so both arms would score ~0 and the sweep would buy no
  signal. The counter-argument — that it is the one benchmark on the list with
  outside name recognition — is recorded in
  `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §A1 and is not settled.

### 0.6 Corrections: where the design notes met reality

The design above was written from the papers. Several of its operational
assumptions did not survive contact with the actual repositories. The design
sections are unaffected; the integration sections were.

| The original note said | Reality |
|---|---|
| "container setup" for Harness-Bench | **There are no containers.** HarnessBench copies each task's `fixtures/` into a plain filesystem sandbox and runs your harness there with `cwd` set to it. Nothing to build, nothing to pull. |
| "you need one entry point: `kusudaemon bench`" | Necessary but not sufficient. HarnessBench drives *adapters*, and it ships a `generic_cli` adapter — so the integration is a wrapper script it invokes, not the CLI directly. `scripts/hb_adapter.sh` is that wrapper. |
| Task categories "long-horizon autonomy", "workspace operations" | The real field is `class:` in each `task.yaml`, and the eight values are listed in §3.6 below. The closest match is `Long-running Autonomy & State Adaptation` (11 tasks). |
| "Terminal-Bench — the cheap smoke test" | Terminal-Bench moved under the Harbor framework, and **§5 now runs LHTB instead** — same Docker plumbing, dense subtask reward rather than a binary pytest verdict. Either way it is Docker-based, the documented path pushes you toward Modal (paid), and on Apple Silicon it runs amd64 images under emulation. It is the *most* expensive item here in both wall clock and tokens, not the cheapest. See §5. |
| "GAIA — cheap, fast, well understood" | The dataset is gated on Hugging Face: account plus accepting terms. That is a third-party dependency, not a cost. See §6. |
| "one JSON record per (benchmark, task, arm, seed)" | Correct, and `scripts/run_harness_bench.py` now emits exactly that — with the four caveats in §0.4. |

Two things in the repo were wrong and have been fixed as part of this:

- `scripts/run_harness_bench.py` looked for `tasks/<category>/<task>/instruction.md`.
  The real layout is `tasks/<NNN-name>/{task.yaml,prompt.txt,fixtures/,oracle_grade.py}`,
  so it discovered zero tasks. It also never ran HarnessBench's grader.
  It has been rewritten to drive HarnessBench's own CLI.
- `cmd_bench` mapped only `status == "halted"` onto `halt_reason`. A driver
  `status == "error"` — provider unreachable, auth refused, rate limited: the
  common failure in a long sweep — produced `resolved: false, halt_reason: null`,
  indistinguishable from a clean unresolved run. Now every non-`done` status is
  recorded with its phase. Regression test:
  `tests/test_bench_cli.py::test_arm_c_error_status_records_diagnosable_halt_reason`.

---

## 1. What each benchmark actually costs you

| Benchmark | Containers | Data | Third-party account | Your cost |
|---|---|---|---|---|
| **HarnessBench** (§3) | none | `git clone`, ~40 MB | none | model calls only (deterministic oracle) |
| **LongGenBench** (§4.1) | none | in-repo, ~20 MB | none | model calls only (regex) / + free judge endpoint |
| **WritingBench** (§4.2) | none | in-repo, ~50 MB | none | model calls + free judge endpoint |
| **HelloBench** (§4.3) | none | Hugging Face (ungated) | none | model calls + free judge endpoint |
| **LHTB** (§5) | Docker, many GB | LHTB repo + Harbor registry | none for local Docker | model calls only (dense oracle) — **the expensive one: 5–15M tokens per task-run** |
| **GAIA** (§6.1) | none | Hugging Face, **gated** | free HF account | model calls only (deterministic exact-match) |
| **SWE-bench Verified** (§6.2) | Docker | Hugging Face (ungated) | none | model calls only (local Docker) |

Do them in that order. §3 is where the signal is.

---

## 2. Prerequisites

### 2.1 Python

kusudaemon runs on 3.10+. **HarnessBench requires 3.11+** despite claiming
3.10 in its `pyproject.toml`: `src/harnessbench/adapters/zeroclaw.py` does a
top-level `import tomllib`, which is 3.11-only, and its adapter package
imports every adapter eagerly. On 3.10 you get:

```
ModuleNotFoundError: No module named 'tomllib'
```

Give HarnessBench its own virtualenv on 3.11+ (§3.1). If you have Python 3.13
installed, it satisfies both requirements (kusudaemon 3.10+, HarnessBench 3.11+)
without needing an older version (3.11/3.12). kusudaemon runs directly under
your active 3.13 interpreter; keep HarnessBench in an isolated venv.

### 2.2 kusudaemon

```bash
cd ~/kusudaemon
pip install -e ".[gptme]"        # or just -e . if you only use CLI backends
python3 -m unittest discover -s tests -p "test_*.py"
```

The suite must be green before you spend anything on an external benchmark —
that is the whole point of §9's opening warning. A crash-matrix or
context-boundedness failure shows up on a leaderboard as "the model is bad".

### 2.3 Provider config

`provider.json` and `.env` are read **from the invoking working directory**.
The benchmark wrapper runs with `cwd` set to the task workspace, so it pins
both explicitly via `KUSUDAEMON_PROVIDER_CONFIG` and `KUSUDAEMON_ENV_FILE`.
You do not need to do anything, but if you move the repo, those paths follow
it automatically since the wrapper derives them from its own location.

### 2.4 Decide credential parity before you run anything

§0.2 flags this and it is the single easiest way to invalidate a
whole sweep: arms A and C must run **the same model**, or the comparison
measures the model rather than the harness.

The adapters deliberately never forward the harness's provider credentials to
backend CLIs, so the two arms authenticate differently by construction:

| Arm | What runs | How it authenticates |
|---|---|---|
| A | the bare backend CLI (`opencode run`, `gptme`, `claude -p`, `codex exec`) | the CLI's own auth — its config file or its own env var |
| C | kusudaemon: role calls over HTTP + writer episodes through the backend CLI | role calls use `provider.json`/`.env`; writer episodes use the CLI's own auth |

So before the first real run, confirm by hand that both arms resolve to the
same model string, and write that down in your notes next to the results.
With the `opencode` backend and `provider.json` as it currently stands, that
means `opencode/nemotron-3.5-lightning-free` on both sides.

---

## 3. HarnessBench — start here

106 offline tasks, no containers, and the only published benchmark built to
vary the harness while holding the model fixed. Its scoring formula is
`Security (binary gate) × Completion (deterministic validators) × Process
(LLM rubric)`.

### 3.1 Clone it and give it a 3.11+ virtualenv

```bash
cd ~                                    # anywhere outside the kusudaemon repo
git clone https://github.com/Qihoo360/harness-bench.git
cd harness-bench

uv venv --python 3.13 .venv             # or bare `uv venv .venv` with 3.13 on PATH
uv pip install --python .venv/bin/python PyYAML
```

`/benchmarks/` inside this repo is gitignored, so
`~/kusudaemon/benchmarks/harness-bench` works equally well if you would rather
keep the checkout next to the code — pass that path as `--bench-dir` below.
Every example here uses `~/harness-bench`, which is also the driver's default
when the repo lives at `~/kusudaemon`.

PyYAML is not optional. `config/harness.example.yaml` looks like JSON but
contains `#` comments and trailing commas, so `json.loads` fails on it and
HarnessBench falls back to `yaml.safe_load`.

Verify:

```bash
PYTHONPATH=src .venv/bin/python -m harnessbench.cli tasks | head
```

You should get a JSON object of 106 task ids.

### 3.2 Point its output directories somewhere you control

`config/app.yaml` ships with the authors' own scratch paths:

```json
{
  "results_dir": "data_try6/results",
  "work_root": "data_try6/sandbox"
}
```

Both are relative to the checkout and both are gitignored, so they work as-is.
Change them if you want results outside the checkout — but **mind the disk**:
every run keeps a full sandbox with a copy of the task fixtures, the rendered
prompts, the usage-proxy log, and kusudaemon's run directory. A 11-task ×
2-arm × 3-seed sweep is 66 sandboxes. Prune with:

```bash
rm -rf data_try6/sandbox/kusudaemon-arm*
```

or pass `--delete-sandbox` to HarnessBench's own CLI when you do not need the
traces (the driver in §3.5 keeps them, because the per-round kusudaemon
records live there).

### 3.3 Register the kusudaemon arms

From the kusudaemon repo:

```bash
cd ~/kusudaemon
python3 scripts/run_harness_bench.py \
  --bench-dir ~/harness-bench \
  --write-harness-config --arms A C
```

This creates `~/harness-bench/config/harness.yaml` (copied from the example if
absent) and inserts two entries:

```json
"kusudaemon-armA": {
  "adapter": "generic_cli",
  "command": "/Users/you/kusudaemon/scripts/hb_adapter.sh",
  "session_prefix": "hb-kusu-a",
  "timeout_sec": 2400,
  "args": ["A", "{workspace}", "{prompt_file}", "{session_id}", "{task_id}"]
},
"kusudaemon-armC": { ... same, with "C" ... }
```

**`command` must be an absolute path.** The `generic_cli` adapter runs it with
`cwd` set to the task workspace and does not resolve relative paths against
the checkout, unlike the bespoke adapters. The script writes an absolute path
for you; if you hand-edit the file, keep it absolute.

`--arms A C` follows your note in §0.1 — arm B (decomposition
without review) is skipped for cost. Add `B` here and to the sweep if you
later want to separate what decomposition buys from what verification adds.

### 3.4 Smoke test — three checks, zero API spend

**a. HarnessBench works on its own.** Its `demo` adapter is a local fake:

```bash
cd ~/harness-bench
PYTHONPATH=src .venv/bin/python -m harnessbench.cli \
  run-task --task 001-file --harness demo-local --mode demo
```

Expect `"combined_score": 1.0` and one passing check.

**b. The kusudaemon wrapper is wired up.** Point the provider at a dead port
so nothing is billed, and confirm the plumbing reports a real reason:

```bash
cd ~/kusudaemon
KUSUDAEMON_PROVIDER_BASE_URL="http://127.0.0.1:9/v1" \
KUSUDAEMON_PROVIDER_API_KEY=dead \
KUSUDAEMON_PROVIDER_MODEL=dummy/model \
python3 scripts/run_harness_bench.py \
  --bench-dir ~/harness-bench \
  --tasks 001-file --arms C --seeds 1 \
  --backend gptme --model dummy/model \
  --results-dir /tmp/bench_smoke
```

Expect:

```
  [1/1] 001-file arm=C seed=1 ...
      score=0.000 tokens=0 0.012s -- error in classify: provider request failed: [Errno 111] Connection refused
```

`score=0.000` is correct — the oracle graded an empty workspace. What you are
checking is that the halt reason names the phase and the cause. If you see
`halt_reason: null` here, the fix from §0.6 is not in your tree.

**c. The matrix is what you think it is.** Always dry-run before a sweep:

```bash
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Long-running Autonomy & State Adaptation" \
  --arms A C --seeds 1 2 3 --dry-run
```

It prints the task list, the arms, the seeds, and what bounds each run. By
default there is no token ceiling, so it tells you that spend is uncapped and
names the bounds that do apply — the round limit and the per-task wall-clock
cap. If you pass `--budget-tokens`, it prints a worst-case estimate
(`runs × --budget-tokens`) instead. Read whichever line you get before you
proceed.

### 3.5 First real task

Start with one task, one arm, one seed:

```bash
python3 scripts/run_harness_bench.py \
  --bench-dir ~/harness-bench \
  --tasks 001-file \
  --arms A --seeds 1 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --results-dir bench_results
```

Then the same with `--arms C`. Compare the two `bench_results/*.json` records
by hand before running anything larger. Specifically check:

- `model` is identical in both records (§2.4).
- `harness_bench.checks` shows the oracle actually ran, not an early error.
- arm C's `tokens_by_role` is populated. If it is empty, kusudaemon's cost
  ledger did not record — usually a provider config problem, and every
  spend comparison downstream will be meaningless.

### 3.6 The sweep

The eight task classes, with counts (106 tasks total):

| Class | Tasks | Primary focus | Special tasks / notes |
|---|---|---|---|
| **Long-running Autonomy & State Adaptation** | 11 | Multi-round persistence, state recovery, replanning | 007 (passphrase memory), 057, 058 (3 rounds), 059, 060 |
| **Software Engineering & Codebase Maintenance** | 22 | Git, refactoring, bug fixing, test running | 011 (retry persistence, 5 rounds) |
| **Workspace, Tool Use & Multimodal Operations** | 15 | Shell ops, CLI tools, multimodal media manipulation | 001 (smoke), 006 (needs internet), 008 & 013 (vision) |
| **Data, BI & Finance Analytics** | 14 | Pandas, SQL/SQLite, financial reporting pipelines | 105 (multi-round resume ledger) |
| **Knowledge, Evidence & Retrieval** | 13 | Document synthesis, fact cross-checking, retrieval | Document QA and text reconciliation |
| **Vertical Professional Workflows** | 12 | Standard operating procedures, legal/clinical formatting | 103 (multi-round policy replan diff) |
| **Office & Business Communication** | 12 | Structured memos, business emails, slide deck outlines | Format and layout verification |
| **SRE, DevOps & Release Ops** | 7 | Dockerfiles, deployment scripts, nginx/log diagnostics | Config debugging and service orchestration |

#### 3.6.1 Running individual classes

To run any single class across arms A and C (3 seeds, skipping tasks arm A solves on all seeds):

```bash
# 1. Long-running Autonomy & State Adaptation (11 tasks, 66 runs max) -- start here
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Long-running Autonomy & State Adaptation" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/autonomy

# 2. Software Engineering & Codebase Maintenance (22 tasks, 132 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Software Engineering & Codebase Maintenance" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/swe

# 3. Workspace, Tool Use & Multimodal Operations (15 tasks, 90 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Workspace, Tool Use & Multimodal Operations" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/workspace

# 4. Data, BI & Finance Analytics (14 tasks, 84 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Data, BI & Finance Analytics" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/data_analytics

# 5. Knowledge, Evidence & Retrieval (13 tasks, 78 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Knowledge, Evidence & Retrieval" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/retrieval

# 6. Vertical Professional Workflows (12 tasks, 72 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Vertical Professional Workflows" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/vertical

# 7. Office & Business Communication (12 tasks, 72 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "Office & Business Communication" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/office

# 8. SRE, DevOps & Release Ops (7 tasks, 42 runs max)
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --classes "SRE, DevOps & Release Ops" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/sre
```

#### 3.6.2 Full suite sweeps and task filtering

Run all 106 tasks across the full benchmark (omit `--classes`):

```bash
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --drop-solved-by-a --results-dir bench_results/full_suite
```

Task slicing options:
- By task IDs: `--tasks 001-file 011-code-debug 105-partial-batch-resume-ledger`
- By numeric ranges: `--from-num 1 --to-num 30` (runs tasks 001 through 030)
- Inspect all discovered tasks: `python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench --list-tasks`

#### 3.6.3 Task-specific nuances across classes

- **Internet dependency:** Task `006-access-bilibili` requires public internet access. If running offline/hermetic, exclude it with `--tasks` or expect connection failure.
- **Multimodal tasks:** Tasks `008-image-recognize` and `013-image-edit` blend a multimodal quality score ($w = 0.9$) into the outcome. If your model backend lacks vision, it scores 0 on the image checks.
- **Multi-round continuity (8 tasks total across classes):**
  - Autonomy: `007-session-memory`, `057-interruption-resume`, `058-multiday-project-state` (3 rounds), `059-event-update-replan`, `060-task-cancellation-cleanup`.
  - SWE: `011-code-debug` (5 retry rounds).
  - Vertical: `103-policy-update-replan-diff`.
  - Data: `105-partial-batch-resume-ledger`.
  Arm A resumes via backend session flag (`--continue`). Arm C passes forward previous round run directories in `kusudaemon-runs/`.
- **Runtime hooks:** 28 tasks contain a `hooks.py` that spins up mock servers or seeds git repos. The driver executes them automatically through HarnessBench's CLI.

#### 3.6.4 Disk and sandbox management for full runs

A full 106-task sweep across 2 arms and 3 seeds produces up to 636 sandboxes (~15–20 MB each, totaling 10–15+ GB).
- Sandboxes are written to `<bench-dir>/data_try6/sandbox/`.
- Prune sandboxes after extracting `bench_results/*.json` and `records.jsonl`:
  ```bash
  rm -rf ~/harness-bench/data_try6/sandbox/kusudaemon-arm*
  ```

#### 3.6.5 Eliminating all non-model costs

- **Oracle outcome only:** The driver keeps `--skip-process-grade` enabled by default (`HARNESSBENCH_SKIP_PROCESS_GRADE=1`, `HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1`), eliminating paid LLM judge calls. Outcome evaluation uses local deterministic python/bash assertions.
- **`--drop-solved-by-a`:** Skips arm C for tasks where arm A scored 1.0 on all 3 seeds, saving 40-60% of model spend.
- **Unattended execution:** `cmd_bench` uses silent default assumptions on approvals, preventing blocked processes and wall-clock timeout loss.

### 3.7 Reading the results

Per run, `bench_results/harness-bench_<task>_arm<A|C>_seed<N>.json`, in the
the §0.4 schema plus a `harness_bench` block holding the grader's
verdict verbatim:

```json
{
  "benchmark": "harness-bench",
  "task_id": "001-file",
  "task_class": "Workspace, Tool Use & Multimodal Operations",
  "arm": "C", "seed": 1,
  "model": "...", "backend": "opencode",
  "score": 0.0, "resolved": false,
  "tier_measured": null, "tier_final": null,
  "escalations": [], "calls_by_role": {}, "tokens_by_role": {},
  "wall_clock_s": 0.012,
  "halt_reason": "error in classify: provider request failed: ...",
  "commit": "460e5540...",
  "rounds": 1,
  "harness_bench": {
    "outcome_score": 0.0,
    "process_effective": 1.0,
    "security_score": 1.0,
    "combined_score": 0.0,
    "checks": [{"id": "linecount", "label": "out/linecount.txt == 4",
                "pass": false, "weight": 1.0, "detail": "got ''"}],
    "notes": "process skipped by env; security=1; ...",
    "usage_summary": {...},
    "sandbox": "...",
    "elapsed_sec": 0.511
  }
}
```

`score` is HarnessBench's `combined_score`; `resolved` is `score >= 0.95`.
`commit` is in every record, per §0.4 — you will change the prompts,
and numbers without the revision that produced them are not reproducible.

And `bench_results/summary.json`, per arm:

```
arm A: mean=0.412 +/-0.180  resolved 4/33  tokens/run=48210.0  halts=2
arm C: mean=0.559 +/-0.146  resolved 8/33  tokens/run=51877.0  halts=5
```

**Read `mean_tokens_per_run` before you read anything else.** If the arms
differ by more than ~10%, the score delta is partly a spend delta and the
comparison does not support the claim in §0.3. `tokens_per_solved_task`
is the cost-per-solved-task figure that section asks for.

Every record is also appended to `bench_results/records.jsonl` for analysis.

### 3.8 What scoring you get for free, and what the rubric would add

`combined_score = outcome_effective × process_effective × security_score`.

The driver defaults to `HARNESSBENCH_SKIP_PROCESS_GRADE=1` and
`HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1`. With those set, `process_effective`
and `security_score` both default to `1.0` and the combined score collapses to
the oracle's `outcome_score` — a deterministic, programmatic check of what
landed in the workspace. That is free, objective, and reproducible.

**What you lose:** the security gate. §0.5 specifically expects
kusudaemon's "only code verifies done" stance to show up as an advantage
there, and with the rubric skipped it cannot. That is a real gap in the
evidence, not a rounding error.

To enable the rubric — this costs judge calls, and needs the usage proxy to
have captured a trace (see §3.9):

```bash
python3 scripts/run_harness_bench.py ... --with-process-grade
```

with `RUBRIC_BASE_URL`, `RUBRIC_API_KEY` and `RUBRIC_MODEL` exported. Hold the
judge model **fixed across arms**; a rubric judge that changes between arms
invalidates the comparison more thoroughly than an unequal token budget does.

Two tasks — `008-image-recognize` and `013-image-edit` — blend a multimodal
quality score into their outcome at `w = 0.9` regardless of the process rubric.
Every other task is `w = 0`, oracle only.

### 3.9 Constraints worth knowing before a long sweep

**The usage proxy cannot see the `opencode` backend.** HarnessBench starts a
local reverse proxy per run and captures token usage from responses that pass
through it. Redirecting traffic there requires overriding a `base_url` — and
kusudaemon's OpenCode adapter deliberately has none, because the OpenCode CLI
always talks to OpenCode Zen itself. Consequences with `--backend opencode`:

- `harness_bench.usage_summary.available` is `false`.
- The process and security rubric have no trace to grade, so they are skipped
  even without `--skip-process-grade`.
- Token accounting comes from kusudaemon's own cost ledger (`tokens_by_role`),
  which covers role calls but not writer episodes inside the CLI.

If you want proxy-captured accounting and the security gate, run with
`--backend gptme` and a provider whose `base_url` you control:

```bash
python3 scripts/run_harness_bench.py ... \
  --backend gptme --model deepseek-ai/deepseek-v4-pro-0813 \
  --proxy --upstream https://integrate.api.nvidia.com/v1
```

The wrapper then registers a `/kusudaemon` route on the proxy and sets
`KUSUDAEMON_PROVIDER_BASE_URL` to it. Note this proxies **role** traffic; how
much of the writer's traffic it captures depends on the backend.

**Eight tasks are multi-round**, and they need real session continuity — this
is the subtlest part of the integration and the easiest place to manufacture a
fake pass. The tasks: `007-session-memory`, `011-code-debug` (which lists
`prompt.txt` five times, a retry-persistence test), `057-interruption-resume`,
`058-multiday-project-state` (3 rounds), `059-event-update-replan`,
`060-task-cancellation-cleanup`, `103-policy-update-replan-diff`,
`105-partial-batch-resume-ledger` — six of them in the long-horizon class you
start with.

HarnessBench invokes the adapter once per round with the same workspace and the
same session id, and expects the agent to carry its own conversation across
those calls. Seven of the eight let state flow through the workspace: their
later prompts say things like "read the existing `out/state.json` from round
1". **`007-session-memory` is the exception and the reason this matters.**
Round 1 hands over a passphrase and says it will not be repeated; round 2 says
"this message does not contain it — recall it using multi-turn conversation
memory only"; and a round-1 hook fails the task if the passphrase was written
anywhere under the workspace. It is a pure test of whether your harness has a
session at all.

Each arm therefore uses its own real continuity mechanism:

- **Arm A** resumes the backend CLI's own session. `cmd_bench` takes
  `--session-id` and `--round`, and on rounds after the first it adds the
  backend's resume flag — `opencode run --continue`, `gptme --resume`,
  `claude -p --continue`, `codex exec resume --last`. Each task has its own
  workspace directory and the driver runs strictly one task at a time, so
  "continue the last session" resolves to the previous round of the same task.
- **Arm C** is pointed at its own prior run directories. kusudaemon rebuilds
  every context from the run directory, so that directory *is* its session
  memory: round 1's goal — passphrase included — lands in
  `kusudaemon-runs/<run-id>/run.spec.json`. The wrapper appends a short
  "Session memory" footer to later rounds naming those paths, and **inlines
  nothing**. The run root lives outside `$WORKSPACE`, so the leak-scan hook is
  unaffected. Whether kusudaemon then passes 007 depends on whether its
  pipeline actually goes and reads its own prior record — which is precisely
  the capability the task is measuring.

**Do not bridge rounds by pasting earlier prompts into the current goal.** An
earlier version of this wrapper did exactly that, and it turned 007 into "copy
this string from your prompt into a file": arm A scored a clean 1.000 on all
three seeds in ~35 s each with a weak model. If you ever see a perfect score on
a task that is supposed to be hard, check the rendered goal file in the sandbox
before believing it.

**Verify arm A's resume flag before your first multi-round sweep.** OpenCode
documents `--continue` as "continue the last session" and `--session` as
"session ID to continue", but does not document whether `--session` will
*create* an id that does not exist yet. The default here is `--continue`, which
does not need a pre-existing session. If you confirm your CLI version accepts a
caller-chosen id, `KUSUDAEMON_BENCH_SESSION_FLAG=session` switches to the
explicit form, which is more precise. Check with:

```bash
python3 scripts/run_harness_bench.py --bench-dir ~/harness-bench \
  --tasks 007-session-memory --arms A --seeds 1 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --results-dir /tmp/check007
```

A score near 0 means the bare CLI genuinely has no continuity across the two
invocations — a real finding, not a setup bug. A score of 1.0 means it resumed
(good) *or* something leaked the passphrase (check the sandbox's round-2 prompt
for it before you celebrate).

**HarnessBench stops issuing rounds when an adapter exits non-zero.** That
would forfeit rounds 2..N and grade a workspace that never received its later
instructions. The wrapper therefore exits 0 and records the true status in its
sidecar record, so the oracle always sees the real final state. Set
`KUSU_BENCH_STRICT_EXIT=1` to propagate the exit code instead.

**Approvals block forever without an operator — this is why `bench` runs
unattended by default.** Three phases stop and ask a human: intake questions
(T1 and above), pilot artifact sign-off (T3), and document-review triage (T2
and above). `wait_for_resolution` is deliberately untimed — "the operator is
the one control surface that must never be rushed" — so with no dashboard
attached the run does not fail, it *waits*, until HarnessBench's wall-clock cap
kills the process. You get no graded result and no halt reason, and you lose
the full `timeout_sec` per affected task.

Every hermetic test that drives the pipeline end to end already wraps it in
`approvals.Approver`, including the Layer 1 crash matrix; only the headless
production paths were missing one. `cmd_bench` now attaches the same resolver
unless you pass `--attended`, and **only `cmd_bench`** — a normal `kusudaemon
run` or `resume` still stops and asks you, which is the entire point of the
approval. That boundary is pinned by `tests/test_unattended_approvals.py`,
which fails if any CLI command outside an explicit allow-list constructs an
`Approver`, and if `pipeline/run.py` ever references one. Resolving blank is the documented silent-operator
path at all three sites: intake ends after round 1 and writes its default
assumptions into `spec.md`, pilot approves the artifact as-is, document review
proceeds with repairs. Nothing is answered *for* the model — the assumptions
are recorded in the run directory and are auditable afterwards.

Two consequences worth carrying into how you read the results. First, if intake
does ask questions, the assumptions it records become part of the frozen spec
and can steer the run; that is real harness behavior being measured, not a rig
artifact, but read `spec.md` before concluding anything about a task that went
sideways. Second, **the exposure depends on tier**: `phases_for` gives T0
`classify/execute/verify` with no intake at all, while T1 and above all include
it. If most HarnessBench tasks classify as T0 you will rarely see this — and
that is itself the finding §0.5 predicts for SWE-bench, namely
that small tasks barely exercise decomposition. Check `tier_measured` across
your records early; if it is mostly T0, the long-horizon class is not testing
what you built.

**One task needs the public internet.** `006-access-bilibili`. Everything else
is offline or served by a task hook. 28 tasks have `hooks.py` that prepare
runtime state — local servers, git repositories, seeded secrets — which is why
you run through HarnessBench's CLI rather than invoking `kusudaemon bench`
against the task directory yourself.

**Timeouts.** `timeout_sec` in `harness.yaml` (2400 s by default from
`--harness-timeout-sec`) overrides the per-task value in `task.yaml`. A
`subprocess.TimeoutExpired` inside the adapter propagates and aborts the run,
and that is the one bound whose expiry is *not* graceful.

There is no token ceiling by default (see §3.10), so for a long arm-C run the
things that can stop it are the round limit (100, kusudaemon's own default) and
this wall-clock cap. Prefer to be stopped by the round limit: it ends the run
cleanly with a recorded outcome, whereas a wall-clock expiry raises
`TimeoutExpired` inside the adapter and loses the data point. So raise
`--harness-timeout-sec` generously rather than trimming it, and if you want a
guaranteed graceful stop, set `--budget-tokens` explicitly — a budget halt is a
recorded outcome; a killed subprocess is not.

**Wall clock is a first-class metric.** kusudaemon runs subagents strictly in
series, so a wide decomposition can exhaust a wall-clock cap while spending
fewer tokens than a single-session agent that ran out of context. Both
`wall_clock_s` (kusudaemon's, summed across rounds) and `harness_wall_clock_s`
(the whole HarnessBench invocation) are in every record. Look at both.

### 3.10 Budgets: what bounds a run, and what doesn't

**There is no token ceiling by default, and that is deliberate.** HarnessBench
does not require one — its only per-task bound is wall clock — and kusudaemon
treats an unset ceiling as genuinely unbounded rather than as a missing value:
`RunOptions.max_total_tokens = None` makes `_check_cost_ceiling` short-circuit
before it reads the ledger at all (`driver.py:719`). Unbounded is a supported
state, not an oversight.

The reason to leave it off for benchmarking is that a token cap does not fail
safely here. When it fires, the run halts and the oracle grades a
half-finished workspace, so the task scores near zero for a reason that has
nothing to do with the harness — and it fires *first* on exactly the wide
decompositions this benchmark exists to measure. You would be capping the
signal.

What still bounds every arm-C run:

| Bound | Default | On expiry |
|---|---|---|
| Round limit (`--max-rounds`) | 100, kusudaemon's own default | clean stop, recorded outcome |
| Wall clock (`--harness-timeout-sec`) | 2400 s | `TimeoutExpired`, run aborted, data point lost |
| Token ceiling (`--budget-tokens`) | **unset** | clean halt, recorded outcome |

Arm A has none of these except wall clock — it is the bare CLI, and
`--budget-tokens` is not referenced in its code path at all.

Set `--budget-tokens` when you are protecting a spend limit and would rather
have a truncated run than an expensive one. Do not set it as a matter of
routine, and if you do set it, record it next to the results: a run that
halted on budget is not evidence about the harness.

Verify what a run actually used in
`<sandbox>/kusudaemon-runs/<run-id>/run.spec.json`:

```json
{"max_total_tokens": null, "max_rounds": 100, ...}
```

---

## 4. Long-form generation — the half nothing else covers

Shape B in §0.2: source material and a rubric, no container
plumbing. These exercise the document-generation path that no agent benchmark
touches, and they are the cheapest external signal available.

### 4.1 LongGenBench

Long-form generation under explicit structural constraints — "write 52 weekly
diary entries, each satisfying these rules" — which maps directly onto
kusudaemon's text work object (`v6/work_object.py`).

#### 4.1.1 Setup and data

```bash
git clone https://github.com/mozhu621/LongGenBench.git ~/LongGenBench
cd ~/LongGenBench
pip install -r requirements.txt
```

Data ships in the repository (0 cost, no account required):
- `Dataset/Dataset_short.json` (16K target tokens)
- `Dataset/Dataset_long.json` (32K target tokens)

Each item has `id`, `prompt`, `length`, and `constraints` (once, range, periodic).

#### 4.1.2 Running arms A and C

Prepare goals from the dataset into individual files or feed them programmatically.

**Arm A (bare model):**
Run the bare model via backend CLI or direct HTTP completion on the task prompt:

```bash
# Example using opencode backend CLI
opencode run --model opencode/nemotron-3.5-lightning-free \
  "$(cat prompt.txt)" > armA_out.txt
```

**Arm C (kusudaemon text pipeline):**
Drive through kusudaemon's text work object with tiering and review enabled:

```bash
kusudaemon run \
  --goal-file prompt.txt \
  --workspace ./work_dir \
  --work-object text \
  --tier auto \
  --budget-tokens 80000 \
  --backend opencode
```

Collect all generated outputs into `predictions.json` in LongGenBench's expected format:

```json
[
  {
    "id": "task_001",
    "prediction": "<full generated document text>",
    "prompt": "..."
  }
]
```

#### 4.1.3 Zero-cost evaluation

LongGenBench provides two metrics:

**1. Completion Rate (Deterministic regex — 100% free, zero API calls):**
`Evalution/eval.py`'s `parse_blocks` and `calculate_completion_rate` verify that all
requested `#*#`-delimited sections were generated:

```bash
python Evalution/eval.py \
  --pred_file predictions.json \
  --dataset Dataset/Dataset_short.json \
  --metric completion
```

This yields an objective, reproducible completion fraction directly comparable to
kusudaemon's assembly compiler checks.

**2. Instruction-following Accuracy (Free OpenAI-compatible endpoint, no GPU/vLLM):**
The upstream `evaluate_accuracy` relies on vLLM (requiring a high-end GPU). To run on
CPU/Mac without paying for GPU cloud instances, replace vLLM with an HTTP call
to the free OpenAI-compatible endpoint already defined in your `provider.json` (e.g.
OpenCode Zen free endpoint or local Ollama):

```python
# scripts/eval_longgen_free.py
import json, os, re, requests, sys
from pathlib import Path

BASE_URL = os.environ.get("KUSUDAEMON_PROVIDER_BASE_URL", "https://integrate.api.nvidia.com/v1")
API_KEY = os.environ.get("KUSUDAEMON_PROVIDER_API_KEY", "")
MODEL = os.environ.get("KUSUDAEMON_PROVIDER_MODEL", "opencode/nemotron-3.5-lightning-free")

def query_judge(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    resp = requests.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
```

Run this free evaluator across predictions and ground-truth constraint prompts, holding
the judge model strictly fixed across arms A and C.

---

### 4.2 WritingBench

1,239 writing queries across 6 major domains (literature, business, education, etc.)
evaluated against dynamic, per-query rubric criteria.

#### 4.2.1 Setup and data

```bash
git clone https://github.com/X-PLUG/WritingBench.git ~/WritingBench
cd ~/WritingBench
pip install openai tqdm
```

Data ships in-repo under `benchmark_query/`:
- `benchmark_query/benchmark_all.jsonl` (full 1,239 queries)
- Single-axis subsets for budget-controlled runs:
  - `requirement/format.jsonl` (strict layout and structural rules)
  - `requirement/length.jsonl` (target word counts and chapter constraints)
  - `requirement/style.jsonl` (tone, voice, and perspective)

#### 4.2.2 Running arms A and C

Generate responses for the chosen subset (e.g. `requirement/format.jsonl`):

- **Arm A:** Run via bare model CLI or `python generate_response.py --model opencode/nemotron-3.5-lightning-free --benchmark_file requirement/format.jsonl --output_file responses_armA.jsonl`.
- **Arm C:** Run through kusudaemon text pipeline (`kusudaemon run --goal-file <query> --work-object text`), recording the assembled document into `responses_armC.jsonl`.

Format of each line in `responses_arm<A|C>.jsonl`:

```json
{"id": "format_001", "query": "Write a quarterly report...", "response": "..."}
```

#### 4.2.3 Zero-cost evaluation

Upstream defaults to paid Claude API calls (`ClaudeAgent`) or downloading large fine-tuned
weights. Eliminate external costs by configuring `evaluator/llm.py` to route to your
free OpenAI-compatible endpoint:

```bash
export OPENAI_BASE_URL="$KUSUDAEMON_PROVIDER_BASE_URL"
export OPENAI_API_KEY="$KUSUDAEMON_PROVIDER_API_KEY"

python evaluate_benchmark.py \
  --evaluator llm \
  --model opencode/nemotron-3.5-lightning-free \
  --input_file responses_armC.jsonl \
  --output_file scores_armC.jsonl
```

Aggregate the rubric scores across criteria:

```bash
python calculate_scores.py --score_file scores_armC.jsonl
```

Compare mean scores and per-criterion compliance (style, format, length, content)
between arms A and C.

---

### 4.3 HelloBench

Long text generation across 5 task types: open-ended QA, summarization, chat,
text completion, and heuristic text generation.

#### 4.3.1 Setup and data

```bash
git clone https://github.com/Quehry/HelloBench.git ~/HelloBench
cd ~/HelloBench
pip install -r requirements.txt
```

The dataset is hosted ungated on Hugging Face at `HelloBench/HelloBench`. Download
via git lfs or python:

```python
from datasets import load_dataset
ds = load_dataset("HelloBench/HelloBench")
ds["test"].to_json("hellobench_test.jsonl")
```

#### 4.3.2 Running arms A and C

- **Arm A:** Generate responses with bare CLI or `python run.py --model opencode/nemotron-3.5-lightning-free --data_file hellobench_test.jsonl --output_file preds_armA.jsonl`.
- **Arm C:** Run each prompt through `kusudaemon run --goal-file ... --work-object text`, saving outputs to `preds_armC.jsonl`.

#### 4.3.3 Zero-cost evaluation

HelloBench uses LLM-as-judge (`HelloEval`) scored via `llm_judge.py`. Route judge calls
through your free OpenAI-compatible endpoint to eliminate closed-API fees:

```bash
python llm_judge.py \
  --input_file preds_armC.jsonl \
  --output_file judgments_armC.jsonl \
  --api_base "$KUSUDAEMON_PROVIDER_BASE_URL" \
  --api_key "$KUSUDAEMON_PROVIDER_API_KEY" \
  --model "$KUSUDAEMON_PROVIDER_MODEL"
```

Compute overall scores and regression analysis:

```bash
python regression.py --judge_file judgments_armC.jsonl
```

**Guard for long-form benchmarks:** LLM judges can be biased toward verbosity.
Always cross-reference judge scores with objective length compliance and check
whether arm C's structured assembly maintained quality without token bloat.

---

## 5. Long-Horizon Terminal-Bench (LHTB) — Docker, local, and the expensive one

LHTB is the terminal benchmark to run, not stock Terminal-Bench. Both ride Harbor
and both are Docker-based; the difference is the grading. Terminal-Bench 2.0 gives
you a binary pytest verdict per task, so a weak model scores zero and you learn
nothing about *how far* the harness got. LHTB decomposes each task into weighted
subtasks and reports `R = Σ(wₖ·rₖ) / Σwₖ` — dense partial credit, which is the only
shape that can show a decomposition advantage at this model tier.

46 tasks, 9 categories, 90-minute budget per task, reward checkpoints every 30
minutes (`LHTB_CHECKPOINT_INTERVAL_SEC`). **29 of the 46 have never been solved by
any model tested in the paper**, which is the single most important fact for task
selection — see §5.3.

### 5.1 Installation and Apple Silicon Docker setup

LHTB ships a patched Harbor; stock Harbor runs its tasks in single-shot mode and
will not reproduce the published numbers.

```bash
git clone https://github.com/zli12321/LHTB.git ~/LHTB
cd ~/LHTB
pip install -e harbor            # the patched Harbor, not `pip install harbor`
```

To patch an existing PyPI Harbor 0.20.x in place instead:

```bash
PKG=$(python -c "import harbor, os; print(os.path.dirname(harbor.__file__))")
cp "$PKG/trial/single_step.py" "$PKG/trial/single_step.py.bak"
cp harbor/patches/single_step.py.harbor-0.20.0 "$PKG/trial/single_step.py"
rm -f "$PKG/trial/__pycache__/single_step."*.pyc
```

Many LHTB images are amd64-only:

```bash
export DOCKER_DEFAULT_PLATFORM=linux/amd64
```

On Apple Silicon that means Rosetta/QEMU emulation for every container, which is
why §5.3 rejects the compile-bound tasks: the agent spends its 90-minute budget
waiting on builds rather than making model calls.

### 5.2 What a run costs

Do this arithmetic before starting a matrix. LHTB is time-boxed, not ability-boxed,
so **a fast weak model spends more per task than a strong one, not less** — the
paper's own Table 1 has one model burning 25.33M tokens over 435 episodes and still
losing to another at 8.91M over 197.

Measured on this stack (13 arm-C HarnessBench runs with non-empty `tokens_by_role`):
**~245 tokens/sec** on fixture workspaces, which scales to roughly **1000–2000
tok/s** on a real source repo where per-turn context is 5–10x larger. The paper's
9.8M tokens in 88.9 min = 1837 tok/s, so the two agree.

| | arm A | arm C, 90-min box | arm C, extended cap (4–8x wall) |
|---|---|---|---|
| per task-run | 8–15M | 4–8M (incomplete) | 25–60M |

A 5-task × 2-arm × 3-seed matrix is ~240M tokens and 90–200 h of laptop wall clock.
The binding constraint is wall clock and the free tier's rate limit, not dollars.
Run **one task, arm A, one seed with usage capture on** before committing to
anything, and replace these numbers with measured ones.

### 5.3 Which tasks to run

Two tests decide it.

**Test 1 — is there a cheap cut in the dependency graph?** Decomposition pays off
only where a leaf can be briefed in a paragraph. A move in a search problem needs
the full board state *plus every line already tried*, so the cut costs as much as
the whole context. The isolation invariant makes this worse, not better: in search,
the previous agent's reasoning about why a line failed is the valuable artifact, and
the design discards it.

**Test 2 — is the difficulty in the size of the job, or inside the leaf?**
Decomposition turns a job too big to finish into N finishable jobs. It does nothing
when each leaf is independently beyond the model. A task only shows a delta when arm
A fails *from context exhaustion or losing the thread* — the failure mode kusudaemon
actually fixes.

**Run these:**

| Task | Category | Why |
|---|---|---|
| `apex-law433-matter` | APEX (Easy) | 70 reactive stages, ~600 documents, deterministically graded. The closest thing in the benchmark to the failure mode kusudaemon exists for. No build, no GPU, no vision. |
| `langchain-version-migration` | Software & RE (Hard) | Offline breaking-API migration with hard-fail compatibility gates. Pure Python, mechanically wide, code-verifies-done. |
| `commit0-multilib-tdd` | Software & RE (Hard) | Three libraries reimplemented from stubs, graded on hidden tests. Three near-independent leaves plus test gates. |
| `climate-netcdf-extreme-event-audit` | Earth/Climate (Hard) | Optional fourth. Five metric families, Python/xarray, checklist-shaped, no compile. |

**Verify before spending on `apex-law433-matter`:** its stages are *reactive*,
revealed one at a time, and the pipeline plans a tree up front. If kusudaemon cannot
run stage-by-stage, this task measures an integration gap rather than the
architecture. That check is the Phase 0 smoke test.

**Rejected, with reasons:**

| Task | Why not |
|---|---|
| `su2-airfoil-regression` | CFD compile + solve under emulation. The agent idles on builds: laptop-hours and thermal load spent, few tokens, near-floor score. |
| `unison-paper-reproduction` | Same compile-bound problem plus long feedback loops a 30B model will not close. |
| `scientific-figure-data-reconstruction` | Multimodal. Confirm the model has vision first, or both arms fail at step one. |
| `duckdb-optimizer-closure` | Good shape, but a duckdb C++ build under emulation can eat the whole budget. Only if the image ships a prebuilt binary. |
| `robotics-slam-benchmark-repair` | Mean reward 0.03 across all models tested. Floor: no delta available. |
| `spot-scheduler-traces`, `nbody-accel-iterative` | Mean 0.96 / 0.93. Ceiling: arm A wins these too. |
| games and puzzles (`2048`, `chess-mate`, `super-mario`, `snake_maze_campaign`) | No cut (Test 1) — one continuous play session. |
| `sudoku-recovery`, `rush_hour_campaign`, `sokoban` | A cut exists (7 puzzles / 4 stages / a ramp), but the difficulty is inside the leaf (Test 2). Perfect partitioning still fails every leaf. |

### 5.4 Harbor agent integration for kusudaemon

Define a custom agent inheriting `BaseInstalledAgent`. Create
`src/kusudaemon/bench/harbor_agent.py`:

```python
import base64
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template

class KusudaemonAgent(BaseInstalledAgent):
    @property
    def name(self) -> str:
        return "kusudaemon"

    async def install(self, environment) -> None:
        await environment.exec("pip install -e /kusudaemon")

    @with_prompt_template
    async def run(self, instruction: str, environment, context) -> None:
        b64 = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
        cmd = (
            f"echo '{b64}' | base64 -d > /tmp/goal.txt && "
            f"kusudaemon bench --goal-file /tmp/goal.txt --workspace . "
            f"--backend opencode --arm C --json > /tmp/kusu_record.json"
        )
        await environment.exec(f"bash -c \"{cmd}\"")

    def populate_context_post_run(self, context) -> None:
        pass
```

**Unverified as of this writing** — check against your patched Harbor before the
first paid run: (a) how the LHTB fork registers a custom agent, and (b) how to
select a single named task, since the README documents only
`harbor run -c configs/examples/terminus2_openai.yaml` against the whole config.
Read `configs/examples/` and `tasks/<name>/task.toml` and write the answers here
before spending anything; per-task `timeouts` live in `task.toml`.

### 5.5 Before the first run

- Confirm arm A and arm C resolve to the same model string (§2.4). LHTB's own
  numbers come from Terminus-2 plus frontier models; yours will not be comparable to
  the published leaderboard and should not be presented as if they were.
- Workspace mode must be settled first. §K6 of `PLAN-WORKSPACE-MODE.md` is why:
  a prose-templated leaf renders as `"bash": "deny"` in opencode, and LHTB is
  entirely shell work. `backends.py:216` now forces `DEFAULT_TOOL_ALLOWLIST` for
  `kind="workspace"` runs, but the tier and prompt flags around it are still
  default-off — decide them explicitly and record the setting next to the results.
- Set `KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT=1`. §K3's "that file is the deliverable"
  prompt is false when the deliverable is environment state.
- Raise `--harness-timeout-sec` generously rather than trimming it (§3.9): a
  wall-clock kill loses the data point, a round-limit or budget stop records one.
- **If you intend to run arm C with parallel writer episodes, read
  `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §B7 and §B8 first.** Two defects there bite
  specifically on a long LHTB sweep: a rate-limited episode currently burns a node
  attempt (`round_loop.py:762`), so three consecutive 429s block a node that was
  never attempted; and the `max_parallel` derivation at `driver.py:2072` is
  `min(16, max(8, cpu_count*2))` — a *floor* of 8, which on an 8-core laptop picks
  16 concurrent opencode subprocesses alongside emulated Docker. Neither is a
  parallelism problem as such, but both will be misread as one.

---

## 6. GAIA and SWE-bench Verified

### 6.1 GAIA (General AI Assistants)

GAIA tests complex multi-step reasoning, multimodal file inspection (PDF, spreadsheets,
audio, code), and web search probes. It maps cleanly to kusudaemon's workspace work object
(`v6/work_object.py`) and probe scheduler (`v4/research.py`).

#### 6.1.1 Dataset acquisition (Free HF account)

GAIA is hosted on Hugging Face at `gaia-benchmark/GAIA`. Accepting terms with a free
Hugging Face account is required ($0 cost):

```bash
# Log in with your free HF token
huggingface-cli login

# Download validation set with public ground truth
huggingface-cli download gaia-benchmark/GAIA --repo-type dataset --local-dir ~/gaia-data
```

The validation set contains 165 questions across Levels 1, 2, and 3 with answers in
`metadata.jsonl`. Task files (.pdf, .xlsx, .py, etc.) reside in `attachments/`.

#### 6.1.2 Running GAIA with kusudaemon

For each task in `metadata.jsonl`:
1. Initialize a task workspace directory containing its attachments from `attachments/`.
2. Run arms A and C:

**Arm A (bare CLI):**
```bash
cd task_workspace
opencode run --model opencode/nemotron-3.5-lightning-free "$QUESTION" > answer_armA.txt
```

**Arm C (kusudaemon workspace work object):**
```bash
kusudaemon bench \
  --workspace task_workspace \
  --goal "$QUESTION" \
  --work-object workspace \
  --tier auto \
  --backend opencode \
  --json > kusu_out.json
```

kusudaemon uses local python scripts and file readers to inspect attachments without
paid external APIs.

#### 6.1.3 Zero-cost deterministic evaluation

GAIA uses exact-match normalized string scoring. No LLM judge calls are required ($0 cost).
Use the standard GAIA normalization scoring function:

```python
# scripts/eval_gaia.py
import json, re, sys
from pathlib import Path

def normalize(text: str) -> str:
    s = str(text).strip().lower()
    s = re.sub(r'[\s,]+', ' ', s)
    s = re.sub(r'[$%€£]', '', s)
    return s.strip()

def gaia_score(prediction: str, ground_truth: str) -> float:
    p, g = normalize(prediction), normalize(ground_truth)
    if p == g or g in p:
        return 1.0
    try:
        return 1.0 if abs(float(p) - float(g)) < 1e-3 else 0.0
    except ValueError:
        return 0.0

def evaluate(pred_file: Path, meta_file: Path):
    preds = {json.loads(line)["task_id"]: json.loads(line)["model_answer"]
             for line in pred_file.read_text().splitlines() if line.strip()}
    metas = [json.loads(line) for line in meta_file.read_text().splitlines() if line.strip()]
    scores = [gaia_score(preds.get(m["task_id"], ""), m["Final answer"]) for m in metas]
    print(f"Accuracy: {sum(scores)/len(scores):.3f} ({sum(scores)}/{len(scores)})")

if __name__ == "__main__":
    evaluate(Path(sys.argv[1]), Path(sys.argv[2]))
```

Run:
```bash
python scripts/eval_gaia.py predictions_armC.jsonl ~/gaia-data/val/metadata.jsonl
```

---

### 6.2 SWE-bench Verified

**On DeepSWE (arXiv 2607.07946): evaluated and rejected.** 113 original tasks over
91 repos, run under `mini-swe-agent`, scored binary pass/fail with no partial
credit. Its weakest evaluated models sit at 5–10% pass@1 and are all well above
nemotron-30b-a3b, so both arms land at ~0 and the sweep buys no signal at any n.
Cost is comparable to LHTB per trial (~4M tokens for a frontier agent, more for a
weak one that exhausts the step cap). If you want SWE-family recognition, the
multi-file Verified subset below is the cheaper way to get it — but §5 is where the
signal is.


500 human-validated bug fixing tasks drawn from 12 large real-world Python codebases.

#### 6.2.1 Setup and data

The dataset is ungated on Hugging Face (`princeton-nlp/SWE-bench_Verified`).

```bash
pip install swebench
```

Requires local Docker running.

#### 6.2.2 Cost control: Strategic subset filtering

Running all 500 instances uncapped costs millions of tokens. Furthermore, ~70% of
instances modify only 1 file and land in T0 direct execution, bypassing decomposition.

**Filter to multi-file instances:**
Extract tasks where the gold patch touches $\ge 2$ files (roughly 85 tasks).
This specifically tests recursive decomposition and cross-file verification.

#### 6.2.3 Running arms A and C

For each instance in the chosen subset:
1. Clone the repository at `base_commit`.
2. Provide the issue text as the goal.

**Arm A:**
Run bare model CLI in the repo directory, then capture patch:
```bash
git diff > patch_armA.diff
```

**Arm C:**
Run kusudaemon workspace pipeline:
```bash
kusudaemon bench \
  --workspace ./repo_clone \
  --goal-file issue.txt \
  --work-object workspace \
  --tier auto \
  --backend opencode \
  --json > kusu_rec.json

git diff > patch_armC.diff
```

Assemble into `predictions.jsonl`:

```json
{"instance_id": "django__django-11099", "model_name_or_path": "kusudaemon-armC", "model_patch": "diff --git a/..."}
```

#### 6.2.4 Zero-cost local evaluation

Run official SWE-bench evaluation using **local Docker** ($0 cloud runner fees):

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path predictions.jsonl \
  --run_id kusu_verified_eval \
  --max_workers 4 \
  --cache_level env
```

Results are written to `logs/run_evaluation/kusu_verified_eval/report.json`.
Inspect resolved count, pass rate (`resolved / total`), and token cost per solve.

---

## 7. Order of operations

1. In-repo hermetic suite and Layer 1 mechanism benchmarks (§9.1) — free, and the gate on everything below.
2. HarnessBench smoke test (§3.4) — free, no API key.
3. HarnessBench: Long-running Autonomy class (11 tasks, §3.6.1).
4. HarnessBench: Remaining 7 classes or full 106-task sweep (§3.6.1–3.6.2).
5. LongGenBench completion rate (§4.1) — objective, 0 API calls.
6. WritingBench format/length subset (§4.2) — free OpenAI judge endpoint.
7. HelloBench subset (§4.3) — free OpenAI judge endpoint.
8. GAIA validation set (§6.1) — exact-match deterministic scoring.
9. **LHTB Phase 0 (§5.3): `apex-law433-matter`, arm C, seed 1, unscored** —
   verifies the reactive-stage protocol and measures real tok/s and per-episode RSS.
10. LHTB Phase 1: the three-task shortlist (§5.3), both arms, one seed.
11. LHTB Phase 2: seeds 2–3 only on tasks that showed a delta worth resolving.
12. SWE-bench Verified multi-file subset (§6.2) — optional, for name recognition.

Guards that apply throughout:
- Always `--dry-run` first to verify matrix size and parameters.
- Always run arm A first and pass `--drop-solved-by-a` to eliminate redundant arm C spend.
- Cost the matrix in tokens before starting it (§5.2), not after. LHTB is 10–30x a
  LongGenBench run per task.
- Keep HarnessBench process grading skipped by default; outcome checks are free and objective.
- Route all LLM judges to your existing free OpenAI-compatible endpoint; hold the judge fixed.
- Run all Docker tasks locally; avoid Modal, Daytona, and cloud sandbox platforms.
- Treat budget and round limits as recorded outcomes, never as failures (§0.4).
- Cache aggressively across arms where the benchmark permits it; the episode
  cache (`v0/episode_cache.py`) already exists and is free throughput.
- Quarantine transport failures rather than scoring them (§0.1), and check for
  identical artifact byte lengths across seeds before believing any aggregate.

---

## 8. Known gaps in this integration

1. **No security-gate evidence by default.** §3.8. The free path scores oracle
   outcome only; the LLM security gate requires judge traces.
2. **Token spend is not equalized automatically.** Nothing caps either arm by
   default, and `--budget-tokens` reaches only arm C. Compare `mean_tokens_per_run`
   per arm to verify comparability before drawing conclusions.
3. **Parallel writer episodes are untested against a live provider.** The wave
   mechanism (`round_loop.py:489`) is implemented and guarded, and
   `driver.py:2068` auto-derives a wave size for dependency-free T2/T3 trees, but
   every number in `bench_results/` to date was produced at `max_parallel=1`. Raising
   it changes the free tier's throttle behaviour, the event-log interleaving that the
   Layer 1 crash matrix pins, and the machine's memory headroom all at once. Treat a
   parallel sweep as a different experiment, not a faster version of the same one.

4. **Multi-round continuity depends on backend resume behavior.** Arm A uses
   `--continue`. Arm C points to prior `kusudaemon-runs/` directories. Task 007
   is the reference test for whether continuity works.
5. **Writer-episode tokens invisible on CLI backends.** `tokens_by_role` covers
   role calls; tokens inside CLI writer episodes are unmetered when the CLI owns
   its auth.
6. **`kusudaemon bench --json`'s `score` is pipeline exit status.** Always quote
   the external benchmark driver's oracle score, not the internal exit status.

---

## 9. The hermetic layers underneath

External benchmark numbers are the least informative result per dollar until the
in-repo layers are green. A crash-matrix failure or an unbounded orchestrator
context shows up on a leaderboard as "the model is bad," and you will spend a
week debugging the wrong layer. This section is what remains live from the
Phase 0 / Phase 1 repair plan; the full historical account, including the
rationale behind each suite's design, is archived at `docs/TEST-PLAN.md`.

### 9.1 The gate: run this before any sweep

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

```
2026-09-04 → Ran 1069 tests in  84.6s — FAILED (failures=1, errors=5)
2026-09-06 → Ran 1193 tests in 112.9s — FAILED (failures=1, skipped=1)
```

Both Phase 0 (repairs) and Phase 1 (mechanism benchmarks) have landed. All six
Layer 1 suites exist and run hermetically — no network, no agent binary, no API
key:

| Suite | Pins the claim that |
|---|---|
| `tests/test_layer1_crash_matrix.py` | resume is real — the driver dies at each of the 24 (tier, phase) crash points and recovers with byte-identical artifacts |
| `tests/test_layer1_context_bounded.py` | every context is bounded: the orchestrator's max prompt at 6000 units is within 1.15× its value at 60 |
| `tests/test_layer1_gate_soundness.py` | gates have a zero false-accept rate on an adversarial corpus — the number that stands between the harness and a model declaring itself done |
| `tests/test_layer1_planner_coverage.py` | partitions cover every unit, never overlap, and a deliberately bad partition is rejected |
| `tests/test_layer1_provider_faults.py` | 429s, truncated JSON, mid-stream resets and hangs each end in a completed run or a recorded halt — never a silently dropped node |
| `tests/test_layer1_reviewer_precision.py` | review earns its tokens — **skipped by default**, see §9.3 |

The suite also enforces zero pytest imports and a checked-in reachability floor
(`test_suite_reachable.py`), so a file that stops contributing tests is loud
rather than quiet. That guard exists because five test files were once
pytest-only and silently ran zero tests for an unknown length of time.

Treat the run above as the gate: if it is not green apart from the known failure
in §9.2, stop and fix that before spending model calls on a sweep.

### 9.2 Still open in the hermetic layers

1. **`mcp_server_overrides` fails silently on Python 3.10.** This is the one
   remaining suite failure (`test_backends_claude_codex.CodexAdapterTest.
   test_mcp_server_overrides`). `adapters/codex.py:172-180` returns `[]` when
   neither `tomllib` nor `tomli` imports, so on the 3.10 leg every configured
   Codex MCP server is dropped without a word. Making the driver import survive
   a missing `tomli` was correct; trading a loud failure for a silent capability
   loss was not. Fix: warn once naming the missing dependency and the dropped
   servers, and install the `dev` extra (which already carries `tomli`) on the
   3.10 CI leg. Tracked as `PLAN-BENCH-INTEGRITY.md` §4.7.
2. **`split_accepted` is still an unwired escalation trigger.** The eval report
   now prints `unwired_triggers` explicitly — currently `["split_accepted"]` —
   so the gap is visible rather than hidden behind a vacuous `precision: 1.0`.
   Making it visible was the intent; closing it is coupled to
   `PLAN-BENCH-INTEGRITY.md` §1.4b, since the split gate cannot currently fire
   on an output-bound task at all.
3. **The crash matrix covers `max_parallel=1` only.** That is the configuration
   documented as producing a byte-identical event sequence, and that determinism
   is what makes the matrix tractable. Every number in `bench_results/` to date
   was also produced at `max_parallel=1`. If
   `PLAN-CONCURRENCY-AND-SHARED-STATE.md` §B1 raises the default, the matrix
   either covers a configuration you no longer run or must cover a much larger
   interleaving state space — decide which deliberately, and record the decision
   here rather than discovering it from a flaky sweep.

### 9.3 The one experiment never run: does review earn its tokens?

`tests/test_layer1_reviewer_precision.py` is written (491 lines) and has never
been run against a live model. It is gated on `KUSUDAEMON_LIVE_REVIEW=1` and
skips otherwise, which is why the suite above reports one skip.

This is the cheapest remaining experiment on the list and the only one that puts
a quality number on arm C's cost over arm A. Right now `keep_depth_pass=False`,
the fused review call, and the `review_sample_rate` default of `0.05` are all
cost decisions with no quality measurement attached to them — and arm C's spend
premium has no justification without one.

Method, for when you run it:

- 30 artifacts: 15 clean, 15 carrying exactly one planted defect of a known
  class — contract rule violated, content duplicated from a sibling leaf,
  coverage gap against its spine span, register drift from the pilot exemplar.
- Report precision, recall, per-class recall, and mean tokens per review call.
- **Assert only non-regression against a checked-in baseline JSON**, never an
  absolute score. That is how a model-dependent metric becomes a regression test
  instead of flapping every time the provider updates.
- Include one artifact over `DEFAULT_ARTIFACT_CAP_TOKENS` with its defect
  planted in the final 10%, since `review_node`'s own docstring says catching
  exactly that is why fan-out exists.

Hold the model fixed across the baseline and the comparison, and record it in
the baseline JSON — the same rule as the LLM judges in §4.

---

## Sources

- HarnessBench: <https://github.com/Qihoo360/harness-bench> · <https://arxiv.org/abs/2605.27922>
- Harbor / Terminal-Bench: <https://www.harborframework.com/docs/tutorials/running-terminal-bench> · <https://www.harborframework.com/docs/agents> · <https://www.tbench.ai/docs/run-terminal-bench-2-0>
- LongGenBench: <https://github.com/mozhu621/LongGenBench> · <https://arxiv.org/html/2409.02076v6>
- WritingBench: <https://github.com/X-PLUG/WritingBench> · <https://arxiv.org/html/2503.05244v2>
- HelloBench: <https://github.com/Quehry/HelloBench> · <https://arxiv.org/html/2409.16191v1>
- GAIA: <https://huggingface.co/datasets/gaia-benchmark/GAIA> · <https://arxiv.org/abs/2311.12983>
- SWE-bench Verified: <https://github.com/princeton-nlp/SWE-bench> · <https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified>
- Long-Horizon-Terminal-Bench: <https://arxiv.org/html/2607.08964> · <https://github.com/zli12321/LHTB> · <https://zli12321.github.io/LHTB/>
- DeepSWE (evaluated, rejected — §6.2): <https://arxiv.org/abs/2607.07946> · <https://deepswe.datacurve.ai/blog/deepswe>

