# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Kusudaemon is a recursive-decomposition harness for long-horizon, corpus-scale tasks: it decomposes one goal into leaves small enough for a model to reliably finish, drives each leaf to a verified artifact via a pluggable agent backend (`gptme`, Claude Code, Codex, or OpenCode), and reassembles the results. It is domain-agnostic — a textbook, a folder of notes, a codebase, or a research corpus all go through the same pipeline without special-casing.

Package: `src/kusudaemon/`. Entry point: `kusudaemon` (`kusudaemon.cli:main`), a thin shim over `pipeline/cli.py`'s command group (`run` / `status` / `approve` / `amend` / `resume` / `serve` / `bench`). Bare `kusudaemon` launches the web dashboard (`serve`).

## Commands

Install (editable, with the default `gptme` backend):
```bash
pip install -e ".[gptme]"
```

Run the full test suite (stdlib `unittest`, no pytest, no network, no agent binary, no API key required):
```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

The suite enforces zero pytest imports and a checked-in reachability floor (>=1100 tests in `test_suite_reachable.py`). Phase 1 Layer 1 mechanism benchmarks run hermetically under `tests/test_layer1_*.py` (crash matrix, context boundedness, gate soundness, reviewer precision with `KUSUDAEMON_LIVE_REVIEW=1`, planner coverage, and provider fault injection).

Run a single test file (`tests/` has no `__init__.py`, so target it via `discover`, not a dotted module path):
```bash
python3 -m unittest discover -s tests -p "test_v1_units.py" -v
```

Run a single test class or method by importing it directly from the `tests` directory:
```bash
cd tests && python3 -m unittest test_v1_units.SomeTestClass.test_some_case -v
```

Every test file does `sys.path.insert(0, str(_REPO_ROOT / "src"))` at the top — this is load-bearing to avoid picking up a stale editable install; don't remove it when adding new test files.

Launch the dashboard / run a goal from the CLI:
```bash
kusudaemon serve                                             # dashboard on :8765
kusudaemon run --goal "..." --workspace ./                   # headless run
kusudaemon resume <run-id>                                   # resume after interruption/crash
kusudaemon bench --workspace ./ --goal "..." --backend opencode --arm C --json # benchmark task (TESTING.md §2)
```

External benchmarks (HarnessBench, long-form generation, Terminal-Bench) have a
step-by-step setup and run guide in `BENCHMARKING.md`; `TESTING.md` holds the
experimental design those runs implement.

Provider config lives in `provider.json` (copy from `provider.example.json`) and `.env` (copy from `.env.example`) in the invoking working directory — see README.md §2 for the schema (per-backend blocks; only `gptme` takes a multi-provider `providers` map).

## Architecture

**Pipeline.** A run moves through phases gated by a tier classifier (T0 direct / T1 single-node / T2 shallow plan / T3 full pipeline): `classify → intake → survey → explore → plan → pilot → research → execute → review → assemble`. Which phases actually run for a given tier is decided by `v6/tiering.py::phases_for`.

**Four roles**, each with a different context-visibility contract (see `roles/`, `v1/orchestrator.py`, `v2/planner.py`, `v1/writer.py`, `v1/reviewer.py`):
- **Orchestrator** — stateless per round, decides what to dispatch next from `tree.json` + event log tail only.
- **Planner** — recursively partitions the goal into a flat tree of leaves; never sees source content, only structural unit labels/token counts.
- **Writer** — the only role with a tool loop; executes one leaf via an agent backend, sees only its brief, declared inputs, and the frozen contract.
- **Reviewer** — audits a submitted artifact against the contract/rubric; never sees the writer's reasoning or scratch, and cannot write repairs itself (those are separate writer dispatches).

**Run directory is the source of truth** (harness-owned, not model-owned): `tree.json` (nodes/deps/gates/status), `manifest.jsonl`, `events.jsonl` (append-only, fsync'd — the resume log), `spec.md`/`contract.md` (frozen goal + quality contract), `spine.json`/`spine/` (surveyed structure), `scratch/<node>/` (writer traces, deletable once a node passes), `out/<node>.md` (artifacts), `audit/<node>.json` (gate + review results). Default location: `~/.kusudaemon/runs/<run-id>`. Model contexts are rebuilt from this directory on every round/resume — nothing is trusted to persist in-memory.

**Module layout mirrors a build ladder**, each package roughly layering on the last:
- `v0/` — resumable event log + run-dir primitives (`events.py`, `run_dir.py`, `runner.py`).
- `v1/` — the round loop: gates (`gates.py`), tree schema (`tree.py`), orchestrator/writer/reviewer, the OpenAI-compatible direct-call provider (`provider.py`).
- `v2/` — intake, survey (chunking + boundary voting → `spine.json`), planner, pilot/contract freezing, optional embeddings/retrieval.
- `v3/` — assembly, script-based cross-cutting checks, compile/repair, re-validation against amended contracts, cross-leaf document review.
- `v4/` — research probes (web/workspace/corpus) and probe scheduling.
- `v6/` — work-object abstraction (text vs. workspace vs. corpus), tier classification, direct T0/T1 execution paths.
- `v7/` — runtime node splitting when a leaf overruns its budget mid-execution.
- `pipeline/` — the phase-state-machine driver (`driver.py`), prompt assembly (`prompts.py`), backend construction (`backends.py`), approvals, liveness tracking, and the CLI command handlers.
- `adapters/` — per-backend writer execution: `gptme_adapter.py` (subprocess per episode, live thinking stream), `claude_code.py` / `codex.py` / `opencode.py` (CLI-driven backends translated to the same gptme trace vocabulary by `_agent_worker.py`), each with its own auth — the harness never forwards its provider credentials to these CLIs.
- `dashboard/` — local web UI (`server.py`, `state.py` disk-backed parsing, `static/app.js` single-page app) for observing and steering a live run.
- `eval/` — fixed benchmark tasks + metrics for measuring resume correctness, reviewer precision, and call budgets.
- `provider_config.py` — loads `provider.json`/`.env` with a strict per-backend schema and a defined precedence chain (CLI args → `KUSUDAEMON_*` env → `provider.json` → `OPENAI_*`).

**Design invariants worth knowing before changing core flow:** nothing declares itself done except code-evaluated gates; decomposition is unconditional (never gated by model judgment about task size); every context (including the orchestrator's) is bounded and does not grow with corpus size or run length; agents are isolated from each other's raw scratch/reasoning/output.
