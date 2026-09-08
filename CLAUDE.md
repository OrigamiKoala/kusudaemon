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
kusudaemon bench --workspace ./ --goal "..." --backend opencode --arm C --json # benchmark task (BENCHMARKING.md §0.2)
```

External benchmarks (HarnessBench all 8 classes, LongGenBench, WritingBench,
HelloBench, Terminal-Bench via Harbor, GAIA, SWE-bench Verified) have a
step-by-step setup and run guide in `BENCHMARKING.md`, which is now the
all-in-one benchmark document: experimental design (§0), setup and commands
(§2-§6), and the hermetic-suite gate that precedes any sweep (§9). The earlier
`TESTING.md` and `TEST-PLAN.md` are archived under `docs/`.
Run LongGenBench: `python3 scripts/run_longgen_bench.py --limit 8 --exclude-tasks 300-block --arms A C --seeds 1 2 3`

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
- **OpenCode Model Identifier Normalization:** OpenCode CLI syntax for third-party providers (e.g. NVIDIA NIM) requires `<provider>/<vendor>/<model>` (e.g. `nvidia/nvidia/nemotron-3.5-lightning-30b-a3b`). `OpenCodeAdapter`, `provider_config.py`, and `cmd_bench` automatically normalize shorthand aliases like `nvidia/nemotron-...` by prepending the missing vendor segment.
- **Benchmark Stdin Isolation:** `cmd_bench` and `scripts/run_longgen_bench.py` execute subprocesses with `stdin=subprocess.DEVNULL` to prevent non-interactive CLIs (such as `opencode run`) from hanging on an inherited open stdin pipe.

**Workspace mode & tiering follow-ups (PLAN-WORKSPACE-MODE.md, STATUS §10):**
- `ScopeEstimate.work_kind` (`document`/`procedure`/`code-edit`/`unknown`) is model-supplied evidence only: explicit `procedure` floors the tier at T1 with a `tier_work_kind_floor` event, nothing else moves, and the model can never lower a floor.
- `tier.json` carries `needs_research` (T3 with probing enabled); `_phase_research` short-circuits to a logged `phase_skipped` no-op when false or when the probe planner returns zero probes. Structural exploration already skips with `phase_skipped` when the plan phase will take the single-node path.
- Workspace-kind runs force `DEFAULT_TOOL_ALLOWLIST` in `build_writer_adapter` (prose/reference template tool opinions no longer deny bash on repo tasks); `code-dominant` shape exists in `_SHAPES` with full shell+patch tools.
- Small-workspace guards key off measured input AND output signals (`_measured_small` + `PLAN_MIN_WORKSPACE_TOKENS = 2000` shared with `tiering._T1_WORK_TOKENS_CEILING`); K0/K1/K2-gate/K2-tools/K3 workspace prompt stay behind default-off flags (`KUSUDAEMON_TIER_TRUST_SIGNALS`, `KUSUDAEMON_PLAN_SINGLE_UNIT_WORKSPACE`, `KUSUDAEMON_DIRECT_TEMPLATE`, `KUSUDAEMON_DIRECT_TOOLS`, `KUSUDAEMON_WORKSPACE_ARTIFACT_PROMPT`).

**Subagent Chat Timestamps & Benchmark Harvesting:**
- `dashboard/rendering.py`: `parse_trace_lines` pre-scans and propagates `effective_ts` across all message, tool call, and thinking trace entries from preceding timestamps/heartbeats instead of dropping to `None`.
- `adapters/_agent_worker.py`: automatically stamps missing `ts` with `time.time()` on every emitted JSON event.
- `dashboard/state.py`: `_summarize_subagent` attaches `mtime` from `trace_path.stat().st_mtime`.
- `dashboard/static/app.js`: guards `timestamp !== undefined && timestamp !== null` so valid timestamps are retained and fallback is trace `mtime` rather than `Date.now()`.
- `scripts/run_longgen_bench.py`: `harvest_artifact` checks `~/.kusudaemon/runs/*/out/*.md` on timeout or missing `--output-dir` to recover and preserve completed generated artifacts.

**Dashboard Thinking Persistence & Toggle:**
- `dashboard/static/app.js`:
  - `state.thinkingOpen`: tracks user-explicit toggle state per entry key (`ontoggle`).
  - `MORPH_OPTS.onBeforeElUpdated`: synchronizes `open` attribute on `<details>` elements to prevent morphdom from collapsing toggled thinking cards on background render ticks.
  - `loadMainThinking`: retains historical thinking across all phases and subagents in `state.mainThinking.agents`, merging unified entries chronologically into the feed rather than wiping entries when active agent transitions.
  - `mainAgentId`: falls back to live/active worker subagents during `execute` phase to capture live stream.
  - `renderAgentChatEntry`: scopes DOM keys by `node_id` to ensure stable identity across agents.

**Chunked Writing & Stream-Aware Liveness Timeout:**
- `pipeline/prompts.py`: `_artifact_instruction` explicitly guides writers to write and save long or multi-part documents incrementally in batches (10–20 sections at a time) rather than buffering in a single monolithic edit call, ensuring intermediate progress is committed to disk.
- `environment/local.py` & `base.py`: Implemented stream-aware soft timeout extension up to 1,800s (`KUSUDAEMON_SOFT_TIMEOUT_EXTENSION=1800`, `KUSUDAEMON_ACTIVITY_WINDOW=60.0`). Replaces passive worker thread heartbeats with true liveness verification:
  - Measures process group CPU time advancement (`_get_pgid_cputime`).
  - Inspects active established TCP network sockets (`_has_established_socket`).
  - Allows active model generation and thinking streams to complete uninterrupted without prematurely cutting off in-flight tool calls.

**Benchmark Integrity & Harness Hardening (PLAN-BENCH-INTEGRITY.md):**
- **Process Group Isolation:** `adapters/_agent_worker.py` and `scripts/run_longgen_bench.py` spawn subprocesses with `start_new_session=True` and terminate/kill entire process groups via `os.killpg(pgid, ...)`. Prevents orphaned worker processes from surviving episode timeouts and exhausting local compute/ports.
- **Manifest Salvage on Partial Runs:** When a run does not reach the `done` state, `cmd_bench` and `driver.py` salvage completed node artifacts recorded in `manifest.jsonl`, populating `partial: True`, `nodes_passed`, and `nodes_total`, and copying partial outputs to `--output-dir` instead of scoring 0.0 or failing without output.
- **Tier Reporting Key Alignment:** `cmd_bench` correctly inspects `measured_tier`/`measured` and `tier`/`final` in `tier.json`, eliminating fallback to `T?` in summary records.
- **Wall-Clock Clamping & Budget Awareness:** `RunOptions.wall_clock_budget` (`--wall-clock-budget`) bounds driver execution. `driver._budget_seconds()` clamps per-phase budgets to remaining run-level time and raises `TimeoutError` when exhausted. Phase transient error retry limit raised to 4 with duration-scaled backoffs.
- **Unattended Classify Fallback:** For headless/unattended runs (`attended=False`), classifier schema or parse failures degrade to single-node scope instead of raising `RuntimeError`, persisting `tier_degraded: True` in `tier.json`.
- **Role Token Caps & Timeouts:** `v1/provider.py` enforces `max_tokens` (configurable via `KUSUDAEMON_ROLE_MAX_TOKENS`) across `complete` and `complete_json` to prevent runaway role generations. Default role timeout in `hb_adapter.sh` set to 600s.
- **Episode Timeout & Size Defect Splitting:** In `v1/round_loop.py`, writer timeouts emit `node_episode_timeout` and set `last_defect = "episode_timeout: episode wall clock exceeded"`. Recognized as a size defect (`is_size_defect` in `v6/direct.py`), prompting runtime leaf split (`v7/split.py` and `_should_offer_split` in `writer.py`).
- **Output-Size Decomposition Behind Flag:** Under `KUSUDAEMON_TIER_OUTPUT_SIGNALS=1`, numeric targets >= 8 units floor tier classification at T2 (`v6/tiering.py`), set `units_expected` on `NodeBudget` (`v1/tree.py`), enforce `units_min` gate verification (`v1/gates.py`), and offer runtime splits when output unit counts overrun.
- **Quarantine Accounting & Harness Resilience:** `eval/common.py` and `scripts/bench_common.py` implement `classify_halt` to isolate transport, quota, billing, and socket errors into `invalid` records, excluding them from capability scores (`mean_completion_pct`, token metrics). Scripts emit a loud warning banner if >20% of seeds are quarantined and flag `suspect_identical_seeds` when distinct seeds yield identical outputs. Both `run_longgen_bench.py` and `run_harness_bench.py` rebuild records from disk on startup and support `--flags` (and `--resume`).

**Concurrency, Shared State & Rate Limiting (PLAN-CONCURRENCY-AND-SHARED-STATE.md):**
- **Arm A Token Parsing (§A4):** `_parse_opencode_usage` in `pipeline/cli.py` extracts prompt, completion, and total tokens from OpenCode stream-json events (`type: "step-finish"`, `usage`, `part.tokens`) and text logs (`total tokens: <N>`, `Tokens: <N> input, <N> output`), with `extract_visible_output` preserving clean markdown artifacts.
- **Benchmark Flag Recording (§A5):** Benchmark runners (`scripts/run_harness_bench.py`, `scripts/run_longgen_bench.py`) explicitly log resolved flags and environment overrides under `"flags"` in every benchmark cell record.
- **Max Parallel Derivation & Hardware Admission (§B1, §B8):**
  - Driver derives `max_parallel` dynamically up to the ready-set width, bounded by system available memory via `utils/system_resources.py` (`derive_memory_concurrency`, `get_available_memory_bytes`, `can_admit_episode`), floored at 1.
  - Waves inspect available memory before admitting new episode executions to protect against system out-of-memory crashes.
- **Rate-Limit Control & AIMD Wave Sizing (§B7):**
  - Rate-limited and 429 episodes are classified as non-attempts with status `"throttled"`. Emits `node_throttled` event, preserves node attempt counter (`attempts`), and returns node status to `"pending"` without exhausting retry limits.
  - `RunAdmissionController` (`pipeline/admission.py`) manages a shared concurrency semaphore and tracks global `Retry-After` backoffs across all direct provider requests (`v1/provider.py`) and writer episodes.
  - AIMD wave sizing: increases wave size additively (+1) when waves succeed without throttling and decreases multiplicatively (halved, floored at 1) upon experiencing throttled episodes.
  - Wave start jitter (`random.uniform(0.1, 0.5) * i`) desynchronizes wave bursts to reduce burst-rate 429s.
- **Worktree Isolation & Sequential Patch Application (§B4):**
  - `WorktreeManager` (`v1/worktree.py`) isolates concurrent writer episodes in dedicated git worktrees or shadow workspaces.
  - Upon episode completion, changes are diffed (`git diff`) and applied sequentially to trunk (`git apply`).
  - Merge conflicts log a `merge_conflict` event, record conflict counts, mark `last_defect`, and re-queue the node for re-execution against the updated trunk.
- **Environment Mutating Command Lock:** `LocalEnvironment.exec` (`environment/local.py`) inspects commands with `is_env_mutating_command` (detecting `pip`, `npm`, `apt`, etc.) and acquires `_env_lock` during execution to prevent concurrent environment corruption across parallel workers.

**Token Accounting & Output-Size Decomposition (PLAN-TOKEN-ACCOUNTING.md):**
- **Tokenizer Calibration & Exact Tokens (§A2–§A4, §C):** `tokens.py` module resolves offline HF tokenizers (e.g. Nemotron) from local cache, falls back to `tiktoken` (`o200k_base`), and then `chars/4`. Implements `(path, mtime_ns, size, model)` caching for file counts. Re-derived small-workspace and token constants: `PLAN_MIN_WORKSPACE_TOKENS = 3320`, `_T1_WORK_TOKENS_CEILING = 3320`.
- **OpenCode Token Stream Accounting (§C):** `_parse_opencode_usage` extracts and records cache tokens (`cache_read`, `cache_write`) from OpenCode usage objects, accurately tracking cached prompts.
- **Part Files Architecture (§O5a, §O9):** `v0/run_dir.py` adds `node_parts_dir` (`out/<node_id>/`) and `node_artifact_text` to automatically concatenate part files in sorted order. Prevents whole-file overwrites and destructive rewrite prompt instructions; prompts guide writers to write parts or append without repeating previous units.
- **Output-Size Spine Synthesis (§J1, §J2):** Behind `KUSUDAEMON_OUTPUT_SPINE=1`, `_phase_survey` in `driver.py` detects declared output counts (>= 8 units via `tokens.expected_units`), routes unit-specific constraints to respective slices, and writes synthetic spine files under `spine/`. Planner forces leaf tiling directly to match synthesized output units.
- **Gate Realignment & Non-destructive Retries (§B, §I, §L):** `headers:std` relaxed to warning gate (`warn_gates`). `units_min` gate parameter syntax `units_min:N@delim` validates expected unit counts against explicit unit delimiters. Prompt retry framing injects append instructions naming resume points when artifact already contains partial units; inline artifact caps truncate safely with last unit anchor rather than destroying content.
- **Concurrency & Observability (§K):** `--max-parallel` CLI plumbing across `run_longgen_bench.py`, `run_harness_bench.py`, and `hb_adapter.sh`. Driver logs `max_parallel_derived` and records `max_parallel` in bench records. Round loop tracks `max_ready_width` and emits `max_parallel_inert` when configured concurrency cannot be utilized due to serial tree dependencies.
- **Disclosure & Prompt Manifests (§D, §E, §F):** `prompts.py` renders a declared inputs manifest table with file sizes, token counts, and percentage ratios against the leaf budget. `workspace_read` tool decorates directory listings with estimated token counts (`(~{tok:,} tokens)`). `KUSUDAEMON_CONTEXT_DISCLOSURE=1` flag exposes context usage notices.

**Sweep Repair Wave 0 (PLAN-SWEEP-REPAIR.md §B, §C, §F2 — hermetic, no provider calls):**
- **Record Repairs (§C1, §C2):** `run_longgen_bench.py` filters the rebuilt record list to the invocation's selected `dataset_index` set (stale rows from other sweeps no longer crash `write_predictions` with `KeyError` or contaminate the summary); `write_predictions` defensively skips foreign indices. `run_harness_bench.py` filters to selected `task_id`s the same way. `eval/common.py::classify_halt` adds an `unknown` category for halts with no detail (`"escalated in execute: no detail"`), quarantined from capability means alongside transport/budget but counted separately in `excluded.by_reason`.
- **Artifact Identity (§B2–§B5, option (a)):** all artifact readers resolve via `v0/run_dir.py::node_artifact_text` — `v1/round_loop.py::_read_artifact` (gates, reviewer, shrink check), `v0/runner.py` snapshot/continuation/existing-artifact paths, `scripts/run_longgen_bench.py::harvest_artifact` (assembly/main.md → per-node resolved text → bare glob), and `pipeline/cli.py` manifest salvage (per-node resolved text materialized under `assembly/salvage_<node>.md`, never back into shadowed `out/<node>.md`). Pre-writer snapshots of part layouts are directory copies (`out/.versions/<node>/attempt_<ts>/`); `v3/run_dir.py` adds `list_attempt_snapshots` (attempt_* only, no mkdir side effect), `read_attempt_snapshot_text`, and `restore_attempt_snapshot` (restores at its own granularity so §L4 restores are not shadowed into no-ops).
- **Provider Error Context (§F2):** `v1/provider.py::_http_transport`/`_http_stream_transport` wrap `HTTPError`/`URLError`/`TimeoutError`/`OSError` with `[phase role node elapsed timeout]` context (elapsed measured around `urlopen`; `ProviderHTTPError.status` preserved so 429/5xx retry ladders are unaffected), so the next execute-phase halt names what today's `"The read operation timed out"` does not.
- **Doc Amendments (§I):** `PLAN-TOKEN-ACCOUNTING.md` §N demotes §O7 to forensics, records the §O+§L-alone isolation run as unrecoverable, and discharges the 1.66x-estimator ordering constraint (§A2–§A4 landed). `README.md` §5 already lists both accounting docs — no edit needed.
- **Tests:** `tests/test_artifact_readers.py` (12 tests, hermetic) covers the §B6 parts fixture, §B4(a) snapshot/restore round-trip, §C1 foreign-row filtering, §C2 unknown quarantine, and §F2 phase/role/elapsed context. Full suite: 1259 tests green, zero pytest imports, reachability floor intact.
- **Explicitly deferred to live runs (require API calls, not done here):** Wave 1 one-seed `KUSUDAEMON_OUTPUT_SPINE=1` measurement (§G.6), the Wave 2 branch on its outcome (§E vs §F), and the Wave 3 sweep + `BENCHMARKING.md` §9 invalidation of the 2026-09-08 arm-C `100-floor` rows.

