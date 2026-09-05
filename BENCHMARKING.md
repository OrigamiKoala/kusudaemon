# BENCHMARKING.md — setting up and running the external benchmarks

`TESTING.md` is the experimental design: which arms, why three of them, what
counts as a defensible claim. **This file is the execution manual**: what to
install, where the data comes from, which commands to run, and what each
number you get back actually means.

Read `TESTING.md` §1 (three arms) and §6 (what a defensible claim looks like)
first — they are what make these runs worth doing. Everything below assumes
you have already accepted that design and just want the thing running.

Everything here is free beyond your own model API calls. Where a benchmark's
normal path routes through a paid or account-gated third party, the free
alternative is spelled out and the tradeoff is stated.

---

## 0. Corrections to TESTING.md

`TESTING.md` was written from the papers. Several of its operational
assumptions do not survive contact with the actual repositories. The design
sections are unaffected; the integration sections are.

| TESTING.md says | Reality |
|---|---|
| §3.1 "container setup" for Harness-Bench | **There are no containers.** HarnessBench copies each task's `fixtures/` into a plain filesystem sandbox and runs your harness there with `cwd` set to it. Nothing to build, nothing to pull. |
| §2 "you need one entry point: `kusudaemon bench`" | Necessary but not sufficient. HarnessBench drives *adapters*, and it ships a `generic_cli` adapter — so the integration is a wrapper script it invokes, not the CLI directly. `scripts/hb_adapter.sh` is that wrapper. |
| Task categories "long-horizon autonomy", "workspace operations" | The real field is `class:` in each `task.yaml`, and the eight values are listed in §3.6 below. The closest match is `Long-running Autonomy & State Adaptation` (11 tasks). |
| §3.2 "Terminal-Bench — the cheap smoke test" | Terminal-Bench moved under the Harbor framework. It is Docker-based, its documented path pushes you toward Modal (paid), and on Apple Silicon it runs amd64 images under emulation. It is the *most* expensive item here in wall clock, not the cheapest. See §5. |
| §3.4 "GAIA — cheap, fast, well understood" | The dataset is gated on Hugging Face: account plus accepting terms. That is a third-party dependency, not a cost. See §6. |
| §5 "one JSON record per (benchmark, task, arm, seed)" | Correct, and `scripts/run_harness_bench.py` now emits exactly that. But note that `kusudaemon bench`'s own `score` field is **not** a benchmark grade — it is 1.0 if the pipeline reached `done`. Only the driver's merged record carries HarnessBench's oracle score. |

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
| **Terminal-Bench** (§5) | Docker, many GB | Harbor registry | none for local Docker | model calls only (local Docker) |
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
that is the whole point of `TESTING.md`'s opening warning. A crash-matrix or
context-boundedness failure shows up on a leaderboard as "the model is bad".

### 2.3 Provider config

`provider.json` and `.env` are read **from the invoking working directory**.
The benchmark wrapper runs with `cwd` set to the task workspace, so it pins
both explicitly via `KUSUDAEMON_PROVIDER_CONFIG` and `KUSUDAEMON_ENV_FILE`.
You do not need to do anything, but if you move the repo, those paths follow
it automatically since the wrapper derives them from its own location.

### 2.4 Decide credential parity before you run anything

`TESTING.md` §2 flags this and it is the single easiest way to invalidate a
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

`--arms A C` follows your note in `TESTING.md` §1 — arm B (decomposition
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
`halt_reason: null` here, the fix from §0 is not in your tree.

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
`TESTING.md` §5 schema plus a `harness_bench` block holding the grader's
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
`commit` is in every record, per `TESTING.md` §5 — you will change the prompts,
and numbers without the revision that produced them are not reproducible.

And `bench_results/summary.json`, per arm:

```
arm A: mean=0.412 +/-0.180  resolved 4/33  tokens/run=48210.0  halts=2
arm C: mean=0.559 +/-0.146  resolved 8/33  tokens/run=51877.0  halts=5
```

**Read `mean_tokens_per_run` before you read anything else.** If the arms
differ by more than ~10%, the score delta is partly a spend delta and the
comparison does not support the claim in `TESTING.md` §6. `tokens_per_solved_task`
is the cost-per-solved-task figure that section asks for.

Every record is also appended to `bench_results/records.jsonl` for analysis.

### 3.8 What scoring you get for free, and what the rubric would add

`combined_score = outcome_effective × process_effective × security_score`.

The driver defaults to `HARNESSBENCH_SKIP_PROCESS_GRADE=1` and
`HARNESSBENCH_SKIP_ORACLE_QUALITY_LLM=1`. With those set, `process_effective`
and `security_score` both default to `1.0` and the combined score collapses to
the oracle's `outcome_score` — a deterministic, programmatic check of what
landed in the workspace. That is free, objective, and reproducible.

**What you lose:** the security gate. `TESTING.md` §3.1 specifically expects
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
that is itself the finding `TESTING.md` §3.5 predicts for SWE-bench, namely
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

Shape B in `TESTING.md` §2: source material and a rubric, no container
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

## 5. Terminal-Bench via Harbor — Docker, local & zero cost

Terminal-Bench evaluates multi-step CLI operations against a live Linux terminal,
verifying final environment state with objective pytest test suites.

Harbor framework docs push toward Modal (paid cloud containers). **Running on
local Docker eliminates all platform fees.**

### 5.1 Installation and Apple Silicon Docker setup

```bash
# Install Harbor CLI
uv tool install harbor || pip install harbor

# Ensure Docker uses amd64 emulation on Apple Silicon Macs
export DOCKER_DEFAULT_PLATFORM=linux/amd64
```

Docker Desktop configuration for macOS:
- Allocate 4–8 CPU cores and 8–16 GB RAM.
- Use default bridge or host networking.
- Wrap interactive agents in `unbuffer` (from `brew install expect`) if terminal TTY
  errors arise during Docker exec.

### 5.2 Verification: Oracle baseline

Confirm your local Docker environment works by running Harbor's reference oracle:

```bash
harbor run -d terminal-bench@2.0 -a oracle --env docker
```

Expect 100% pass on oracle tasks. If this fails, resolve Docker daemon permissions
or amd64 emulation settings before proceeding.

### 5.3 Harbor agent integration for kusudaemon

To evaluate kusudaemon under Harbor, define a custom agent inheriting `BaseInstalledAgent`.
Create `src/kusudaemon/bench/harbor_agent.py`:

```python
import base64
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template

class KusudaemonAgent(BaseInstalledAgent):
    @property
    def name(self) -> str:
        return "kusudaemon"

    async def install(self, environment) -> None:
        # Install harness into the task container
        await environment.exec("pip install -e /kusudaemon")

    @with_prompt_template
    async def run(self, instruction: str, environment, context) -> None:
        # Pass goal via base64 to prevent shell escaping corruption
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

### 5.4 Running the benchmark

Run a stratified 5-task smoke subset first to avoid long wall-clock times:

```bash
# Arm A (bare CLI agent):
harbor run -d terminal-bench@2.0 \
  --tasks 1 5 12 24 38 \
  --agent opencode \
  --model opencode/nemotron-3.5-lightning-free \
  --env docker

# Arm C (kusudaemon):
harbor run -d terminal-bench@2.0 \
  --tasks 1 5 12 24 38 \
  --agent kusudaemon.bench.harbor_agent:KusudaemonAgent \
  --env docker
```

Harbor grades final environment state with deterministic pytests inside the container.
Zero cost beyond your local CPU cycles and model tokens.

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

1. In-repo hermetic suite and Layer 1 mechanism benchmarks (`tests/test_layer1_*.py`) — free.
2. HarnessBench smoke test (§3.4) — free, no API key.
3. HarnessBench: Long-running Autonomy class (11 tasks, §3.6.1).
4. HarnessBench: Remaining 7 classes or full 106-task sweep (§3.6.1–3.6.2).
5. LongGenBench completion rate (§4.1) — objective, 0 API calls.
6. WritingBench format/length subset (§4.2) — free OpenAI judge endpoint.
7. HelloBench subset (§4.3) — free OpenAI judge endpoint.
8. GAIA validation set (§6.1) — exact-match deterministic scoring.
9. SWE-bench Verified multi-file subset (§6.2) — local Docker evaluation.
10. Terminal-Bench 5-task subset (§5) — local Docker evaluation.

Guards that apply throughout:
- Always `--dry-run` first to verify matrix size and parameters.
- Always run arm A first and pass `--drop-solved-by-a` to eliminate redundant arm C spend.
- Keep HarnessBench process grading skipped by default; outcome checks are free and objective.
- Route all LLM judges to your existing free OpenAI-compatible endpoint; hold the judge fixed.
- Run all Docker tasks locally; avoid Modal, Daytona, and cloud sandbox platforms.
- Treat budget and round limits as recorded outcomes, never as failures.

---

## 8. Known gaps in this integration

1. **No security-gate evidence by default.** §3.8. The free path scores oracle
   outcome only; the LLM security gate requires judge traces.
2. **Token spend is not equalized automatically.** Nothing caps either arm by
   default, and `--budget-tokens` reaches only arm C. Compare `mean_tokens_per_run`
   per arm to verify comparability before drawing conclusions.
3. **Multi-round continuity depends on backend resume behavior.** Arm A uses
   `--continue`. Arm C points to prior `kusudaemon-runs/` directories. Task 007
   is the reference test for whether continuity works.
4. **Writer-episode tokens invisible on CLI backends.** `tokens_by_role` covers
   role calls; tokens inside CLI writer episodes are unmetered when the CLI owns
   its auth.
5. **`kusudaemon bench --json`'s `score` is pipeline exit status.** Always quote
   the external benchmark driver's oracle score, not the internal exit status.

---

## Sources

- HarnessBench: <https://github.com/Qihoo360/harness-bench> · <https://arxiv.org/abs/2605.27922>
- Harbor / Terminal-Bench: <https://www.harborframework.com/docs/tutorials/running-terminal-bench> · <https://www.harborframework.com/docs/agents> · <https://www.tbench.ai/docs/run-terminal-bench-2-0>
- LongGenBench: <https://github.com/mozhu621/LongGenBench> · <https://arxiv.org/html/2409.02076v6>
- WritingBench: <https://github.com/X-PLUG/WritingBench> · <https://arxiv.org/html/2503.05244v2>
- HelloBench: <https://github.com/Quehry/HelloBench> · <https://arxiv.org/html/2409.16191v1>
- GAIA: <https://huggingface.co/datasets/gaia-benchmark/GAIA> · <https://arxiv.org/abs/2311.12983>
- SWE-bench Verified: <https://github.com/princeton-nlp/SWE-bench> · <https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified>
- Long-Horizon-Terminal-Bench: <https://arxiv.org/html/2607.08964>

