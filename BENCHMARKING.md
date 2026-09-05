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
| **HarnessBench** (§3) | none | `git clone`, ~40 MB | none | model calls only |
| **LongGenBench** (§4.1) | none | in-repo, ~20 MB | none | model calls only |
| **WritingBench** (§4.2) | none | in-repo, ~50 MB | none | model calls + judge calls |
| **HelloBench** (§4.3) | none | Hugging Face (ungated) | none | model calls + judge calls |
| **Terminal-Bench** (§5) | Docker, many GB | Harbor registry | none for local Docker | model calls + ~17 h wall clock |
| **GAIA** (§6) | none | Hugging Face, **gated** | HF account + terms | model calls |
| **SWE-bench Verified** (§6) | Docker | Hugging Face (ungated) | none | model calls |

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
  --budget-tokens 2000 --max-rounds 2 \
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

It prints the task list, the arms, the seeds, and a worst-case token estimate
(`runs × --budget-tokens`). Read that number before you proceed.

### 3.5 First real task

Start with one task, one arm, one seed, and a tight budget:

```bash
python3 scripts/run_harness_bench.py \
  --bench-dir ~/harness-bench \
  --tasks 001-file \
  --arms A --seeds 1 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --budget-tokens 20000 \
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

The eight task classes, with counts:

| Class | Tasks |
|---|---|
| Software Engineering & Codebase Maintenance | 22 |
| Workspace, Tool Use & Multimodal Operations | 15 |
| Data, BI & Finance Analytics | 14 |
| Knowledge, Evidence & Retrieval | 13 |
| Vertical Professional Workflows | 12 |
| Office & Business Communication | 12 |
| **Long-running Autonomy & State Adaptation** | **11** |
| SRE, DevOps & Release Ops | 7 |

Start with the long-horizon class. It is the smallest, and it is the one your
thesis is about:

```bash
python3 scripts/run_harness_bench.py \
  --bench-dir ~/harness-bench \
  --classes "Long-running Autonomy & State Adaptation" \
  --arms A C --seeds 1 2 3 \
  --backend opencode --model opencode/nemotron-3.5-lightning-free \
  --budget-tokens 120000 \
  --results-dir bench_results
```

66 runs. Arm A runs first — it is the cheapest arm and it establishes the
baseline (`TESTING.md` §4). Add `--drop-solved-by-a` to skip, in arm C, any
task that arm A already solved on every seed; that is the single biggest cost
saving available and it costs you nothing you would have learned.

`--list-tasks` prints every task id with its class if you want to hand-pick
instead.

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

**Eight tasks are multi-round.** `007-session-memory`, `011-code-debug` (5
rounds), `057-interruption-resume`, `058-multiday-project-state`,
`059-event-update-replan`, `060-task-cancellation-cleanup`,
`103-policy-update-replan-diff`, `105-partial-batch-resume-ledger` — six of
which are in the long-horizon class you start with. HarnessBench invokes the
adapter once per round with the same workspace and session id, and expects a
shared-terminal agent to carry its conversation across rounds. kusudaemon
starts a fresh run per invocation.

The wrapper handles this by prepending the earlier rounds' instructions to the
current one as stated context, identically for both arms, and giving each
round its own run id. `007-session-memory` is the clearest case: it asks the
agent to hold a passphrase in conversation memory across rounds *and* its
round-1 hook fails the task if the passphrase is written into any workspace
file — so the prompt is the only legitimate channel, which is exactly what
the wrapper provides. This is an approximation of session continuity, not the
real thing; say so if you report those tasks.

**HarnessBench stops issuing rounds when an adapter exits non-zero.** That
would forfeit rounds 2..N and grade a workspace that never received its later
instructions. The wrapper therefore exits 0 and records the true status in its
sidecar record, so the oracle always sees the real final state. Set
`KUSU_BENCH_STRICT_EXIT=1` to propagate the exit code instead.

**One task needs the public internet.** `006-access-bilibili`. Everything else
is offline or served by a task hook. 28 tasks have `hooks.py` that prepare
runtime state — local servers, git repositories, seeded secrets — which is why
you run through HarnessBench's CLI rather than invoking `kusudaemon bench`
against the task directory yourself.

**Timeouts.** `timeout_sec` in `harness.yaml` (2400 s by default from
`--harness-timeout-sec`) overrides the per-task value in `task.yaml`. A
`subprocess.TimeoutExpired` inside the adapter propagates and aborts the run,
so keep kusudaemon's own bounds — `--budget-tokens`, `--max-rounds` — tighter
than the wall-clock cap and let the budget halt fire first. A budget halt is a
recorded outcome; a killed subprocess is a lost data point.

**Wall clock is a first-class metric.** kusudaemon runs subagents strictly in
series, so a wide decomposition can exhaust a wall-clock cap while spending
fewer tokens than a single-session agent that ran out of context. Both
`wall_clock_s` (kusudaemon's, summed across rounds) and `harness_wall_clock_s`
(the whole HarnessBench invocation) are in every record. Look at both.

---

## 4. Long-form generation — the half nothing else covers

Shape B in `TESTING.md` §2: source material and a rubric, no container
plumbing. These exercise the document-generation path that no agent benchmark
touches, and they are the cheapest external signal available.

### 4.1 LongGenBench

Long-form generation under explicit structural constraints — "write 52 weekly
diary entries, each satisfying these rules" — which is close to your pipeline's
native shape.

```bash
git clone https://github.com/mozhu621/LongGenBench.git
```

Data ships in the repo: `Dataset/Dataset_short.json` and
`Dataset/Dataset_long.json`. No download, no account.

Two metrics, and they are not equally free:

- **Completion rate** is regex-based. `Evalution/eval.py`'s `parse_blocks` and
  `calculate_completion_rate` count how many of the requested `#*#`-delimited
  blocks the model actually produced. Objective, free, and directly comparable
  to your own assembly checks.
- **Instruction-following accuracy** (once / range / periodic) uses a local
  judge model through **vLLM**, which needs a GPU. On a MacBook Air that is not
  viable. Swap it for an OpenAI-compatible call to a free endpoint: the judge
  prompts are plain yes/no questions built in `create_prompts`, so replacing
  the `LLM(...)`/`SamplingParams` calls in `evaluate_accuracy` with an HTTP
  call is a contained change. Hold that judge fixed across arms.

Generate through kusudaemon's text work object (`kusudaemon run --goal-file`),
write each arm's outputs into the shape `eval.py` expects, then score.

### 4.2 WritingBench

1000 queries across domains, in-repo at
`benchmark_query/benchmark_all.jsonl`, with per-query dynamic criteria.

```bash
git clone https://github.com/X-PLUG/WritingBench.git
```

Evaluation is LLM-as-judge: `evaluate_benchmark.py` drives a `ClaudeAgent` or
their fine-tuned `CriticAgent` through `evaluator/llm.py`. Point that at any
OpenAI-compatible endpoint you already pay nothing for. Its rubric machinery
is conceptually close to your contract and review roles, so a flat score
across arms is itself informative about whether your rubrics do anything.

The queries are a mix of English and Chinese; filter by `lang` if you only
want one. `requirement/{format,length,style}` hold single-axis subsets, which
are a cheaper first pass than the full 1000.

### 4.3 HelloBench

Long text generation across five task types. Data is on Hugging Face,
ungated. Same judge caveat. Lowest priority of the three — it overlaps
WritingBench without adding a distinct axis.

**For all three:** LLM-as-judge has known reliability limits on long outputs.
Use them for *relative* comparison across arms with the judge held fixed,
never for absolute quality claims, and supplement with the machine-checkable
structural metrics you already have — assembly compiles, every source unit
covered, no cross-leaf duplication. Those are objective; the judge is not.

---

## 5. Terminal-Bench via Harbor — Docker, and honestly expensive

Terminal-Bench now runs under the Harbor framework. `TESTING.md` §3.2 calls it
"the cheap smoke test"; in wall clock it is the most expensive thing in this
document. Its value is still real — it exercises the workspace path end to end
and catches integration regressions — but budget a day, not an afternoon.

```bash
uv tool install harbor
harbor run -d terminal-bench/terminal-bench-2 -a oracle
```

The `oracle` agent is a baseline that runs the reference solution — use it
first to confirm your Docker setup works before involving kusudaemon at all.

**Free, but only on the local Docker environment.** Harbor's own docs lead with
Modal (`-e modal`), which is a paid third-party sandbox. Local Docker is the
default and costs nothing. Daytona (`-e daytona`) is a third option and also
needs an account.

**Apple Silicon gotchas**, in the order you will hit them:

1. Images are amd64. Without `export DOCKER_DEFAULT_PLATFORM=linux/amd64` you
   get `exec format error`. With it, everything runs under emulation — slow.
2. Docker's default bridge networking isolates containers enough that agents
   fail to reach API endpoints despite the host having internet. Use host
   networking in the Harbor config.
3. Containerized environments have no pseudo-terminal, and CLI agents die with
   "Input is required but no TTY is available". Wrap the agent in `unbuffer`
   (from the `expect` package).
4. Budget 2–4 GB RAM per container and roughly 17 hours for a full sequential
   sweep. On a MacBook Air, run a hand-picked 5-task subset, not the suite.

**Integrating kusudaemon** means writing a Harbor agent class rather than
reusing `hb_adapter.sh` — Harbor has its own plugin interface:

```python
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template

class KusudaemonAgent(BaseInstalledAgent):
    async def install(self, environment): ...      # pip install the harness
    @with_prompt_template
    async def run(self, instruction, environment, context): ...
    def populate_context_post_run(self, context): ...
```

then `harbor run -d terminal-bench/terminal-bench-2 --agent your.module:KusudaemonAgent`.
Base64-encode the instruction when passing it into the container; shell
escaping through the Jinja2 install template is the usual source of silent
breakage.

That is real integration work and it is not yet in this repo. Do §3 and §4
first and see whether you still need it.

---

## 6. GAIA and SWE-bench Verified

Both are in `TESTING.md` §3.4–3.5, both are moderate cost, and both have a
caveat that matters given your constraints.

**GAIA** exercises `v4/research.py` and the probe scheduler, which nothing else
on this list touches — that is its specific value. But the dataset is **gated
on Hugging Face**: you need an account and must accept the terms to download
it. There is no free ungated mirror that is also the real evaluation set. If
"no third parties" is a hard line, GAIA is out, and you lose research-probe
coverage; if an HF account is acceptable, it is otherwise cheap and fast.

**SWE-bench Verified** is ungated and well-tooled, with off-the-shelf runners.
Its execution environments are Docker images, so it inherits every Apple
Silicon caveat in §5. Its weakness for your purposes is stated correctly in
`TESTING.md` §3.5: most instances land in T0/T1 and barely exercise
decomposition. Filter to multi-file instances if you want it to say anything
about the long-horizon thesis; otherwise treat it as a correctness check on
the workspace work object and nothing more.

**Long-Horizon Terminal-Bench** stays where `TESTING.md` §3.3 put it: last,
and probably never under a zero-spend constraint. ~$10.79 per task means a
10-task stratified subset is ~$108 per arm.

---

## 7. Order of operations

1. In-repo suite and Layer 1 mechanism benchmarks — free, no API key.
2. HarnessBench smoke test (§3.4) — free, no API key.
3. HarnessBench, long-horizon class, arms A and C, 3 seeds (§3.6).
4. HarnessBench, one more class, if §3 showed an effect worth localizing.
5. LongGenBench completion rate (§4.1) — objective, no judge needed.
6. WritingBench subset (§4.2).
7. Terminal-Bench 5-task subset (§5), only if you need Shape A coverage.
8. SWE-bench Verified multi-file subset (§6).

Guards that apply throughout:

- Always `--dry-run` first and read the worst-case token estimate.
- Always run arm A first; drop tasks it already solves from arm C with
  `--drop-solved-by-a`.
- Treat a budget halt as a recorded outcome, never a crash.
- Keep `--budget-tokens` set. Uncapped agent runs are how budgets evaporate.
- The episode cache (`v0/episode_cache.py`) exists; use it across arms where
  the benchmark permits.

## 8. Known gaps in this integration

Stated plainly so they do not get discovered mid-writeup:

1. **No security-gate evidence by default.** §3.8. The free path scores oracle
   outcome only, and the security gate is the thing `TESTING.md` §3.1 predicts
   kusudaemon should win on.
2. **Token spend is not equalized automatically.** `--budget-tokens` caps arm
   C; the bare CLI in arm A has no equivalent ceiling. The summary reports
   `mean_tokens_per_run` per arm so you can check parity, but nothing enforces
   it. `TESTING.md` §1 requires it, so check before claiming anything.
3. **Multi-round continuity is approximated**, not real. §3.9.
4. **Writer-episode tokens are invisible with CLI backends.** `tokens_by_role`
   covers role calls; what the backend CLI spends inside an episode is not in
   kusudaemon's ledger and not in the proxy either when the backend owns its
   auth. Arm-vs-arm spend comparisons on the `opencode` backend are therefore
   partial.
5. **`kusudaemon bench --json`'s `score` is not a grade.** It is pipeline exit
   status. Never quote it as a benchmark result; quote the driver's record.
6. **Terminal-Bench has no agent class yet.** §5.

---

## Sources

- HarnessBench: <https://github.com/Qihoo360/harness-bench> · <https://arxiv.org/abs/2605.27922>
- Harbor / Terminal-Bench: <https://www.harborframework.com/docs/tutorials/running-terminal-bench> · <https://www.harborframework.com/docs/agents> · <https://www.tbench.ai/docs/run-terminal-bench-2-0>
- LongGenBench: <https://github.com/mozhu621/LongGenBench> · <https://arxiv.org/html/2409.02076v6>
- WritingBench: <https://github.com/X-PLUG/WritingBench> · <https://arxiv.org/html/2503.05244v2>
- HelloBench: <https://github.com/Quehry/HelloBench> · <https://arxiv.org/html/2409.16191v1>
- Long-Horizon-Terminal-Bench: <https://arxiv.org/html/2607.08964>
