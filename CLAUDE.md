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
kusudaemon run --goal "..." --workspace ./ --output-dir ./out # headless run with custom output dir
kusudaemon resume <run-id>                                   # resume after interruption/crash
kusudaemon bench --workspace ./ --goal "..." --backend opencode --arm C --json # benchmark task (TESTING.md §2)
```

External benchmarks (HarnessBench all 8 classes, LongGenBench, WritingBench,
HelloBench, Terminal-Bench via Harbor, GAIA, SWE-bench Verified) have a
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

**Design invariants worth knowing before changing core flow:** nothing declares itself done except code-evaluated gates; decomposition is unconditional (never gated by model judgment about task size); every context (including the orchestrator's) is bounded and does not grow with corpus size or run length; agents are isolated from each other's raw scratch/reasoning/output. Greenfield/empty workspace runs synthesize a root unit in `survey_workspace` and fallback in `_phase_plan` to `build_single_node_tree(goal)` so the writer receives the full user brief. Final artifacts export to `<run-dir>/out/<run-id>.md` (or `--output-dir` / `KUSUDAEMON_OUTPUT_DIR`), never touching `~/Downloads` unless `KUSUDAEMON_EXPORT_DOWNLOADS=1`. Role calls on CLI backends (e.g. OpenCode) explicitly deny `task`, `glob`, `grep`, `websearch`, `webfetch`, and `*` alongside write/shell tools to prevent subagent recursion and web-scraping loops, with default 180s budget timeout (configurable via `KUSUDAEMON_ROLE_TIMEOUT`). Writers and web-exploration probes retain full access to `web_search`, `websearch`, and `webfetch`.

**Review Latency & Grounding (PLAN-REVIEW-LATENCY.md & PLAN-REVIEW-LATENCY-STATUS.md):**
- **Tier 0 (Transport):** HTTP-first role transport configured via `"roles": {"transport": "http"}` with loud degradation events (`role_transport_degraded`) recording reason (`no_api_key`, `no_base_url`, `resolve_failed`). Fixed transport precedence so backend-specific constraints (e.g. `gptme`) always prefer HTTP before backend CLI fallback. Adapter construction hoisted out of retry loop in `BackendRoleProvider.complete_json`. Reviewer fan-out parallelized across headings with cancel-on-regenerate. Default `review_sample_rate` set to `0.05`, `max_parallel` derived floor raised to 8–16, reviewer timeout tightened to 45s.
- **Tier 1 (Scope & Triage):** Hard gate graduation for unambiguous checks (`latex_balanced`, `headers:std`, `refs_resolve`); deterministic pre-filter skips reviewer model call entirely when all gates pass and all judgments are gate-covered. Fast two-stage triage schema (`VERDICT_TRIAGE_SCHEMA`) wired via `triage_provider` in `driver.py` and `round_loop`. Delta reviewing on retries via cached per-section verdict digests incorporating artifact text, rubric, contract text, and brief. Cross-leaf defects (`coverage_gap`, `duplicate_content`, `register_drift`, `terminology_drift`) stripped from per-node reviewer in favor of windowed document review.
- **Tier 2 (Grounding & Closed-World):** Ground truth passed into reviewer prompt (`contract_text`, `declared_inputs`, `brief`) and verdict digest. Rubric items classified `closed` vs `open` in `NodeTemplate` with open items excluded from model review. `_PROSE` and `_DIRECT` templates carry closed rubric items (`on_topic`, `claims_supported`), evaluating support from context rather than open-world truth. The `claims_resolve` / `uncited_claim` gate handler fails uncited assertions in `<node>_claims.jsonl`, but no builtin template attaches it yet — it fires only when a tree explicitly carries the gate (auto-wiring is sequenced after re-measurement per STATUS §10). C-4 audit logging emits `review_skipped_no_judgment` with discriminated reasons (`empty_judgment`, `all_filtered`, `gate_covered_prefilter`), persisted to `events.jsonl` and `audit/<node>.json`. `RunOptions.direct_review` gates T0/T1 review, and `RunOptions.disable_node_review` allows bypassing per-node review without disabling document-level phase review.

**Role Transport (`roles/factory.py`):**
- `KUSUDAEMON_ROLE_TRANSPORT=http`: direct REST API call via `OpenAICompatibleProvider` to `/chat/completions`. Fast (~1-3s), uses `response_format`/native JSON mode, requires `api_key` and `base_url`.
- `KUSUDAEMON_ROLE_TRANSPORT=backend`: one-shot tool-less CLI episode via `BackendRoleProvider` (`opencode run`, etc.). No API key needed in harness, but higher latency (subprocess startup) and risk of model tool-confusion or schema echoing. `json_io.py` automatically unwraps schema-echoed `properties`/`type`, repairs single-item review objects, strips unexpected keys on strict schemas, defaults missing `questions` and `objections` to empty lists for scope/intake schemas, and auto-fills missing booleans and verdicts. Default timeouts configured to 120s for reviewer/triage and 300s for other roles; the driver's explicit 45s reviewer/triage budget is threaded through `make_role_provider` into `BackendRoleProvider` on the backend path too. `document_review` automatically short-circuits with a clean result when `len(entries) <= 1` and `keep_depth_pass=False`, eliminating redundant multi-minute review attempts on single-node documents. Defaulted by `provider.json` and `scripts/run_harness_bench.py`.
- **OpenCode Stream Error & Quota Hang Protection:** `_agent_worker.py` monitors child stderr (with `--print-logs` injected) and `~/.local/share/opencode/log/opencode.log`. When OpenCode CLI hits non-interactive fatal stream errors (e.g. `Rate limit exceeded` / `AI_APICallError`), the worker immediately terminates the process within seconds instead of hanging until the 300s episode timeout, emitting a system error trace line and non-zero exit code. `BackendRoleProvider` backs off on rate-limit errors before retrying.
- **OpenCode Model Identifier Normalization:** OpenCode CLI syntax for third-party providers (e.g. NVIDIA NIM) requires `<provider>/<vendor>/<model>` (e.g. `nvidia/nvidia/nemotron-3.5-lightning-30b-a3b`). `OpenCodeAdapter` and `provider_config.py` automatically normalize shorthand aliases like `nvidia/nemotron-...` by prepending the missing vendor segment.

**Workspace mode & tiering follow-ups (PLAN-WORKSPACE-MODE.md, STATUS §10):**
- `ScopeEstimate.work_kind` (`document`/`procedure`/`code-edit`/`unknown`) is model-supplied evidence only: explicit `procedure` floors the tier at T1 with a `tier_work_kind_floor` event, nothing else moves, and the model can never lower a floor.
- `tier.json` carries `needs_research` (T3 with probing enabled); `_phase_research` short-circuits to a logged `phase_skipped` no-op when false or when the probe planner returns zero probes. Structural exploration already skips with `phase_skipped` when the plan phase will take the single-node path.
- Workspace-kind runs force `DEFAULT_TOOL_ALLOWLIST` in `build_writer_adapter` (prose/reference template tool opinions no longer deny bash on repo tasks); `code-dominant` shape exists in `_SHAPES` with full shell+patch tools.
- Small-workspace guards key off measured input AND output signals (`_measured_small` + `PLAN_MIN_WORKSPACE_TOKENS = 2000` shared with `tiering._T1_WORK_TOKENS_CEILING`); K0/K1/K2-gate/K2-tools/K3 workspace prompt stay behind default-off flags (`KUSUDAEMON_TIER_TRUST_SIGNALS`, `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE`, `KUSUDAEMON_DIRECT_TEMPLATE`, `KUSUDAEMON_DIRECT_TOOLS`, `KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT`).


