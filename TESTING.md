# TESTING.md — running kusudaemon against public benchmarks

This document covers **Layer 3**: external, published benchmarks. For the
in-repo hermetic suite and the Layer 1 mechanism benchmarks, see
`CLAUDE.md` (commands) and `TEST-PLAN.md` (what to build next).

Do not start here. Public benchmark numbers are the least informative result
per dollar until Layer 1 is green — a crash matrix failure or an unbounded
orchestrator context will show up on a leaderboard as "the model is bad,"
and you will spend a week debugging the wrong layer.

---

## 1. The methodological rule: three arms, always

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

---

## 2. The integration problem

Most agent benchmarks assume a **shared-terminal** interaction model: one
session, the agent issues shell commands, the environment persists, a grader
inspects final state. Kusudaemon's model is different — decompose, dispatch
one leaf per episode, gate each artifact, assemble. Two integration shapes,
and which you need depends on the benchmark:

**Shape A — kusudaemon as the agent under test.** The benchmark hands you a
container and an instruction; you run to completion and it grades final state.
This maps onto the **workspace work object** (`v6/work_object.py`) with the
task directory as the work root. You need one entry point:

```
kusudaemon bench --goal-file <instruction> --workspace <task-dir> \
                 --backend <name> --tier auto --budget-tokens <cap> --json
```

It should be a thin wrapper over `pipeline/cli.py`'s `run`, exiting non-zero
on halt and emitting a machine-readable summary (final tier, calls by role,
tokens, wall clock, halt reason). This is the only integration code most of
section 3 needs. Build it once.

**Shape B — corpus tasks.** Long-form generation benchmarks hand you source
material and a rubric rather than a container. These map onto the **text work
object** and need no container plumbing at all — they are the cheapest external
signal available to you, and they are the only ones that exercise the half of
kusudaemon that generates documents rather than editing code.

Two constraints to design around before you write the adapter:

- **Wall-clock caps.** Several benchmarks impose a per-task timeout (90 minutes
  is common). Kusudaemon runs subagents strictly in series, so a decomposition
  into many leaves can blow a wall-clock cap while spending fewer tokens than a
  single-session agent that ran out of context. Record wall clock as a
  first-class metric or you will misread your own results.
- **Credential isolation.** The adapters deliberately never forward harness
  provider credentials to backend CLIs. Benchmark containers usually expect a
  single API key in the environment. Decide explicitly how each arm is
  authenticated and record it, otherwise arms A and C will silently run
  different models.

---

## 3. The benchmarks, in the order worth doing them

### 3.1 Harness-Bench — start here

The only published benchmark built to measure *the harness* rather than the
model. 106 sandboxed offline tasks across eight categories including
**long-horizon autonomy**, **workspace operations**, and
**evidence-grounded knowledge work**. Environments, budgets and evaluators are
held fixed while the harness varies, which is exactly your experimental design.

Scoring is `Security (binary gate) × Completion (deterministic validators) ×
Process (LLM rubric)`. The security gate matters for you specifically: it
checks permission and constraint violations, and kusudaemon's whole
"only code verifies done" stance should show up there as an advantage.

The paper's central finding is directly load-bearing for your thesis:
**stronger models show low variance across harnesses; weaker models are far
more sensitive to the execution substrate.** Since you are targeting cheap
models on OpenCode Zen, this is the regime where a good harness should show
the largest effect — and if it does not, you have learned something important.

```bash
git clone https://github.com/Qihoo360/harness-bench
# follow its README for the evaluation protocol and container setup
```

Start with the `long-horizon autonomy` and `workspace operations` categories
only. Offline and sandboxed means no per-task API cost beyond your own model
calls.

**Record:** completion rate, security violations, process score, tokens, turns
— per arm, per category.

### 3.2 Terminal-Bench — the cheap smoke test

Standard, widely reported, Docker-based, and small enough to run often. Its
value is not the headline number, it is that it exercises Shape A end to end
and catches integration regressions in your `bench` entry point before you
spend money on anything larger. Run it after every significant driver change.

### 3.3 Long-Horizon Terminal-Bench — the one that fits your thesis, when you can afford it

46 tasks across nine categories (interactive games, reverse engineering,
scientific computing, multimodal analysis, earth science, research
reproduction, systems and security, logic puzzles), graded with **dense
subtask reward** rather than binary pass/fail: each task decomposes into
semantically meaningful subtasks scoring in [0,1], the task reward is their
weighted average, and a task resolves at R ≥ 0.95 (or 1.0).

**Dense reward is the reason to care.** Binary pass/fail on a decomposition
harness tells you almost nothing — a run that got 90% of the way there scores
identically to one that never started. Partial credit localizes *which leaf*
failed, which is precisely the diagnostic your architecture needs and precisely
what your current eval fixtures cannot give you.

Two caveats:

- It expects the **Terminus-2** shared-terminal harness inside Harbor
  containers. Adapting kusudaemon means Shape A plus honoring the 90-minute
  cap; budget real integration time.
- **It is expensive.** Reported averages: ~239 episodes, ~9.8M tokens,
  ~88.9 minutes, ~$10.79 per task. A full 46-task sweep is roughly **$500 and
  ~68 hours of wall clock per arm** — three arms puts you near $1,500. That is
  incompatible with a zero-spend constraint. Run a stratified 10-task subset
  first, and only expand if the subset shows a real effect.

For calibration on difficulty: reported results put Grok 4.5 at 28.3% pass at
R ≥ 0.95, with the cross-model mean at 6.4%. Do not expect a cheap model plus
a good harness to post a high absolute number here. The interesting quantity
is the *mean normalized reward* delta between arms, not pass@1.

### 3.4 GAIA — research probes

Multi-step tool use with unambiguous short answers. Cheap, fast, well
understood. Its specific value to you is that it exercises `v4/research.py`
and the probe scheduler, which nothing else on this list touches. Good weekly
regression.

### 3.5 SWE-bench Verified — the workspace work object

Well-tooled, plenty of off-the-shelf runners, and it maps cleanly onto your
workspace path. Its weakness for your purposes is that most instances are
small enough to land in T0/T1, so it barely exercises decomposition. Use it as
a correctness check on the workspace path rather than as evidence for the
long-horizon thesis. If you want it to test decomposition, filter to the
multi-file instances.

### 3.6 Long-form generation — the half nothing else covers

No agent benchmark evaluates corpus-scale document generation, which is the
use case kusudaemon was designed around (textbooks, study guides, exam sets).
Three options, all Shape B and all cheap:

- **LongGenBench** — long-form generation under explicit structural constraints;
  closest to "write N chapters that each satisfy a spec," which is your
  pipeline's native shape.
- **WritingBench** — broad generative writing with query-dependent rubric
  evaluation. The rubric machinery is conceptually close to your contract and
  review roles, so it doubles as a sanity check on whether your rubrics are
  doing anything.
- **HelloBench** — long text generation capability across task types.

Be careful with the grading: all three lean on LLM-as-judge, which has known
reliability limits for long outputs. Use them for *relative* comparison across
your three arms with the judge held fixed, never for absolute quality claims.
Where you can, supplement with machine-checkable structural metrics you already
have — assembly compiles, every source unit covered, no cross-leaf duplication —
because those are objective and the judge is not.

---

## 4. Cost control

Given the zero-spend constraint, order matters more than ambition:

1. Everything hermetic (in-repo suite, Layer 1) — free.
2. Harness-Bench subsets — offline environments, cost is only your model calls.
3. Long-form generation benchmarks — no containers, short runs, cheap judges.
4. Terminal-Bench — moderate.
5. GAIA / SWE-bench Verified subsets — moderate.
6. Long-Horizon Terminal-Bench 10-task subset — expensive; last.

Practical guards:

- Set `--budget-tokens` per task and treat a budget halt as a recorded outcome,
  not a crash. Uncapped agent runs are how benchmark budgets evaporate.
- Cache aggressively across arms where the benchmark permits it; the episode
  cache (`v0/episode_cache.py`) already exists.
- Run arm A first for every task set. It is the cheapest arm and it establishes
  the baseline you are trying to beat — if the bare model already solves a
  subset, drop those tasks from the expensive arms.

---

## 5. Recording results

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

---

## 6. What a defensible claim looks like

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

---

## Sources

- Harness-Bench: <https://arxiv.org/html/2605.27922v1> · <https://github.com/Qihoo360/harness-bench>
- Long-Horizon-Terminal-Bench: <https://arxiv.org/html/2607.08964>
- LongGenBench: <https://arxiv.org/html/2409.02076v6>
- WritingBench: <https://arxiv.org/html/2503.05244v2>
- HelloBench: <https://arxiv.org/html/2409.16191v1>
- Awesome-Long-Horizon-Agents: <https://github.com/RUC-NLPIR/Awesome-Long-Horizon-Agents>
