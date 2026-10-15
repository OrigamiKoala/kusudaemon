# Kusudaemon User Guide

Kusudaemon is an advanced agent harness engineered specifically for complex, long-horizon tasks. When traditional AI agents are given large goals—such as rewriting a complex codebase, writing an entire book, or performing extensive research—they often suffer from context degradation, forget critical instructions, or prematurely declare a job finished.

Kusudaemon solves this by breaking down large goals into a tree of smaller, manageable subtasks. Each subtask is executed independently in a bounded environment and verified by code-based gates before being marked as complete.

The harness successfully ***doubles*** the mean completion rate on LongGenBench, as well as the rate of which the whole task is completed.

### Results

Long-horizon benchmark results on **LongGenBench** (September 2026 sweep, updated 2026-10-01: 120 runs across 20 tasks × 2 arms × 3 seeds, 5400 s per-run limit, using `nvidia/nemotron-3.5-lightning-30b-a3b`):

| Metric | Bare OpenCode (Arm A) | Kusudaemon (Arm C) | Delta |
|---|---|---|---|
| **Mean completion rate** | **42.7 %** | **96.2 %** | **+53.5 points** |
| **Whole task completed** (100 % units present) | **23 / 60 (38.3 %)** | **56 / 60 (93.3 %)** | **+55.0 points** |
| Median completion rate | 3.0 % | 100.0 % | +97.0 points |
| Tasks with 3 / 3 seeds finished | 3 of 20 | 17 of 20 | +14 tasks |
| Wall clock per *fully completed* document | 50.3 min | 55.6 min | Comparable cost |

**By task type:**
- **Week diary** (52 units): Arm A 53.6 % (7/15 whole) → **Arm C 100 % (15/15 whole)**
- **Building floor plan** (100 units): Arm A 65.1 % (9/15 whole) → **Arm C 96.9 % (14/15 whole)**
- **City blocks** (100 units): Arm A 30.5 % (4/15 whole) → **Arm C 100 % (15/15 whole)**
- **Weekly menus** (52 units, heavy constraints): Arm A 21.5 % (3/15 whole) → **Arm C 87.8 % (12/15 whole)**

Kusudaemon roughly doubles the completion rate and finishes whole documents more than twice as often as the bare agent. Bare agents display bimodal behavior (either finishing or collapsing early due to context drift or clobbered outputs), whereas Kusudaemon's outer-loop gates, unit stubs, stall nudges, and resume/salvage recovery ensure consistent task completion with comparable wall-clock cost per finished document.

For full methodology, breakdown by seed, and caveats, see [`docs/LONGGENBENCH-RESULTS-2026-09.md`](docs/LONGGENBENCH-RESULTS-2026-09.md) and [`BENCHMARKING.md`](BENCHMARKING.md).

### Core Architectural Invariants
Kusudaemon is built around a few strict design principles:

1. **Only Code Verifies "Done":** An agent cannot simply state that a subtask is complete. The harness evaluates machine-checkable gates (such as test suites, linters, or structural checks) before accepting any deliverable.
2. **Filesystem as the Single Source of Truth:** All run states, event logs, task trees, and artifacts live on disk. Model contexts are transient and can be destroyed or rebuilt at any time. If your system crashes or is interrupted, `kusudaemon resume` picks up exactly where it stopped with zero loss of progress.
3. **Strict Agent Isolation:** Subagents operating on a single subtask never see another subagent's raw scratchpad, reasoning, or unvetted output. They receive only frozen instructions, relevant context, and precise briefs.
4. **Human-in-the-Loop Quality Control (Pilot & Contract):** Before executing a massive, multi-step plan, Kusudaemon runs a "pilot" subtask. You review and edit the pilot output, and Kusudaemon infers your standards to freeze a quality `contract.md` that guides all remaining subtasks.

---

### The 4 Internal Roles
During a run, Kusudaemon coordinates four specialized roles:

| Role | Responsibility | Context Access |
|---|---|---|
| **Orchestrator** | Decides which subtask to dispatch next based on dependencies and ready states. | Sees tree status and event log tail. Stateless per round. |
| **Planner** | Breaks a larger goal into a tree of subtasks. Runs only at T2 and above (see the phase table below). A Writer that measurably overruns its budget may additionally propose a mid-run split, which code validates before grafting. | Sees structural unit labels and token budgets. Never sees raw source content. |
| **Writer** | Executes a single subtask tool loop (driven by `gptme`, Claude Code, or Codex). | Sees its brief, required inputs, and the quality contract. Writes one specific artifact file. |
| **Reviewer** | Audits completed artifacts against the quality contract and rubric. | Sees the completed artifact, rubric, and contract. Never sees the Writer's scratchpad or reasoning. |

---

### The Sequential Execution Pipeline
Kusudaemon does **not** run a fixed list of phases. The first phase, `classify`,
measures the goal and the work object and assigns the run a **tier**; the tier
then determines which phases run. A one-file edit does not pay for a planner, and
a book does not survive without one.

| Phase | What it does |
|---|---|
| **Classify** | Measures the goal and work object, assigns the tier, and decides which of the phases below will run. Always runs. |
| **Intake** | Questions you about your goal to establish global rubrics, constraints, and target outputs. Any unresolved points become explicit assumptions. |
| **Explore** | Chunks the workspace or source material into structural units and dispatches lightweight, read-only subagent probes over them. |
| **Plan** | Generates a structured tree of leaf tasks, ensuring each subtask is bounded and achievable within a small token budget. |
| **Pilot** | Executes a single representative subtask, allowing you to edit the output directly and establish the frozen `contract.md`. |
| **Research** | Runs scheduled research probes against external sources. |
| **Execute** | Dispatches Writer episodes for every subtask in the tree, running code-evaluated gates on every submitted artifact. |
| **Review** / **Verify** | Semantic review and cross-subtask consistency checks. `verify` is T0's combined review-and-finalize step; `review` is the T1–T3 form. |
| **Assemble** | Combines verified artifacts, runs final verification checks, and compiles the final result. |

Which phases a tier runs (`v6/tiering.py`):

| Tier | Phases |
|---|---|
| **T0** | classify → execute → verify |
| **T1** | classify → intake → explore → execute → review |
| **T2** | classify → intake → explore → **plan** → execute → review → assemble |
| **T3** | classify → intake → explore → **plan** → **pilot** → research → execute → review → assemble |

**The practical consequence is worth knowing before you run anything:** the
decomposition tree — the thing Kusudaemon exists to build — is produced by the
`plan` phase, so **only T2 and T3 runs decompose**. A T0 or T1 run is a single
Writer episode with gates around it. If a goal you expected to be broken up ran
as one subagent, check the run's tier first (`kusudaemon status <run-id>`, or
`tier.json` in the run directory); you can force it up with
`kusudaemon tier <run-id> T2` or `--tier` at launch.

---

## 2. Setup & Configuration

### Requirements

| Component | Purpose |
|---|---|
| **Python ≥3.10** | System runtime for Kusudaemon. |
| **uv** *(Recommended)* | Fast Python package installer and tool manager. |
| **Provider API Key** | Any OpenAI-compatible endpoint (OpenAI, OpenCode, Anthropic via proxy, etc.). |
| **Docker** *(Optional)* | Required only if you want Writer nodes to search the web using a local SearXNG instance. |

---

### Step 1: Installation

The recommended installation method uses `uv` for an isolated setup:

```bash
uv tool install "kusudaemon[gptme]"
```

Alternatively, you can install via standard `pip`:

```bash
pip install "kusudaemon[gptme]"
```

To update Kusudaemon in the future:
```bash
uv tool upgrade kusudaemon  # or: pip install --upgrade "kusudaemon[gptme]"
```

### Choosing an Agent Backend

Subagent episodes (from structural explorers to leaf writers) can run under five agent backends, selected with `--backend` (or `KUSUDAEMON_BACKEND`):

| Backend | CLI required | Notes |
|---|---|---|
| `gptme` *(default)* | `gptme` (installed via `kusudaemon[gptme]`) | Full harness integration: live thinking streaming, session resume, interjections, skills/plugins/MCP (`gptme-capabilities.toml`). |
| `claude` | `claude` (Claude Code, authenticated on its own) | Tool-restricted (hidden run state denied), supports `--resume` after a crash. |
| `codex` | `codex` (authenticated on its own) | No session resume; sandbox bypassed unless you set one. |
| `opencode` | `opencode` (authenticated on its own or via provider) | Structured JSON output, session resume (`--session`), permission restrictions. |
| `antigravity` *(alias `agy`)* | `agy` (authenticated on its own) | No session resume. |

**Each CLI uses its own credentials — the harness never shares your provider key with them.** All subagents (explorers, research probes, writers) can utilize any of the supported backends. Mid-episode interjections are gptme-only.

The backend can be switched for an already-running run — the change takes effect at the next dispatch:

```bash
kusudaemon backend <run-id> codex   # or: gptme | claude | opencode | antigravity
kusudaemon backend <run-id> default # clear the override
```

or from the dashboard: the backend selector in the run header, the new-run modal's "subagent backend" field, or the `/backend` command.

---

### Step 2: Provider & API Key Configuration

Kusudaemon uses two files in your project workspace root directory to manage models and keys: `provider.json` and `.env`.

1. **Create `provider.json`** to define your provider endpoints, model routing, and role execution:
   ```json
   {
     "default": "opencode",
     "providers": {
       "opencode": {
         "base_url": "https://opencode.ai/zen/v1",
         "model": "opencode/deepseek-v4-flash-free",
         "api_key_env": "OPENCODE_API_KEY"
       }
     },
     "roles": {
       "transport": "auto",
       "backend": null,
       "model": null
     }
   }
   ```

   **Role Execution Routing:**
   - Harness role calls (`classify`, `survey`, `plan`, `pilot`, `review`, etc.) automatically route through the run's selected backend (`opencode`, `claude`, `codex`) or direct HTTP for `gptme`.
   - When using `opencode`, `claude`, or `codex`, runs can operate completely **keyless** without any external `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` defined.
   - You can configure custom role models per-backend via `role_model` in backend settings or `KUSUDAEMON_<BACKEND>_ROLE_MODEL`.

2. **Create `.env`** to store your environment variables and API keys securely:
   ```bash
   OPENCODE_API_KEY=sk-your-api-key-here
   ```

---

### Step 3: Optional Web Search Setup (SearXNG via Docker)

Kusudaemon subagents can search the web through a local, self-hosted [SearXNG](https://docs.searxng.org/) instance.

1. **Launch SearXNG in Docker:**
   ```bash
   mkdir -p searxng
   docker run -d --name searxng -p 8080:8080 \
     -v "$(pwd)/searxng:/etc/searxng" \
     searxng/searxng
   ```
   *(Note: The first run automatically populates default configuration files into your local `./searxng` directory.)*

2. **Enable JSON Output:**
   SearXNG disables JSON API formatting by default. Edit `./searxng/settings.yml` and ensure `json` is included under `search.formats`:
   ```yaml
   search:
     formats:
       - html
       - json
   ```

3. **Restart the Container:**
   ```bash
   docker restart searxng
   ```

4. **Point Kusudaemon at SearXNG:**
   Add the following to your `.env` file (if using non-default host/port):
   ```bash
   KUSUDAEMON_SEARXNG_URL=http://localhost:8080
   ```

5. **Verify the Setup:**
   ```bash
   curl "http://localhost:8080/search?q=test&format=json" | head -c 200
   ```
   If a valid JSON object is returned, web search is ready for your agent nodes.

---

### Step 4: Connecting Agent Skills, Plugins, and MCP Servers

*(`gptme` backend only — Claude Code, Codex, and OpenCode use their own configuration.)*

Kusudaemon allows subagent nodes to extend their toolset using **Agent Skills**, **Plugins**, and **MCP (Model Context Protocol) Servers**. These are configured by placing a `gptme-capabilities.toml` file in your **project workspace root directory**:

#### A. Agent Skills (`SKILL.md`)
Skills are auto-discovered from standard directories such as `~/.claude/skills`, `./skills`, or `./.gptme/skills`. You can also specify custom directories in `gptme-capabilities.toml`:
```toml
[lessons]
dirs = ["/path/to/custom/skills"]
```

#### B. MCP (Model Context Protocol) Servers
To attach external MCP tool servers (e.g. database query tools, GitHub integrations, or custom API wrappers), define them under `[mcp]` in `gptme-capabilities.toml`:
```toml
[mcp]
enabled = true
auto_start = true

[[mcp.servers]]
name = "github-tools"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
env = { GITHUB_PERSONAL_ACCESS_TOKEN = "your-github-token" }
```

#### C. Custom Plugins
Custom Python plugins and tool extensions can be configured under `[plugins]` in `gptme-capabilities.toml`:
```toml
[plugins]
enabled = ["custom_tool_plugin"]
paths = ["/path/to/plugins"]
```

When capabilities are enabled, Kusudaemon automatically dynamically loads the tool definitions and includes their summaries in the Writer subagents' toolchains.

---

## 3. Using the Dashboard

### Step 1: Launching & Resuming Runs

To launch the Kusudaemon web dashboard, run:

```bash
kusudaemon serve
```
*(Running `kusudaemon` without arguments is shorthand for `kusudaemon serve`.)*

By default, the dashboard starts on `http://localhost:8765`. You can launch a specific run directly or run headless via the CLI:
```bash
kusudaemon run --goal "Refactor the auth package" --workspace ./
```

If a run is interrupted or halted, you can pick up execution from disk at any time:
```bash
kusudaemon resume <run-id>
```

---

### Step 2: Layout

The dashboard is a score-based operator console: a run is read left to right in time, and every part (node) is a line on that score.

```
┌─ RAIL ─────────┬─ CENTER ──────────────────────────────────┬─ INSPECTOR ────────────┐
│ runs           │ open-run tabs                             │ node  changes artifact │
│ node tree      │ goal · phases · tier · model · cost · ctl │ files docs cost events │
│                │ waiting: blocked nodes, jobs, approvals   │ tools                  │
│                │ SCORE  (node lines, episode bars, marks)  │                        │
│                │ FEED   (thinking, tool calls, diffs)      │                        │
│                │ composer (message agent, /commands)       │                        │
└────────────────┴───────────────────────────────────────────┴────────────────────────┘
```

* **Rail:** every run with live status, token count and a filter; below it the node tree (dotted ids group into folders, status chips filter, gate pips and attempt counts per node).
* **Run bar:** goal, phase strip, tier (with escalate), backend and model overrides, tokens, elapsed, pause/continue, restart driver, kill.
* **Waiting tray:** blocked nodes, running background jobs, and pending approvals. Approvals are answered in place (intake questions, pilot editor with live diff, amend triage, redispatch/reopen confirmations).
* **Score:** one line per node over time. Bars are agent episodes (accent = running, green = done, red = error, hatched = timeout), ticks are events (gate failures, nudges, throttling, review outcomes), the top lane is the pipeline phases. Zoom with the buttons or `Ctrl`/`⌘` + wheel; the accent line is *now*. Click a line to open that node.
* **Feed:** the selected agent's reasoning, assistant messages, and tool calls with their results. Edit and write calls expand into diffs. A pinned todo list, context-size sparkline and token totals sit in the header; the split icon pins a second agent beside the first. With no node selected it shows the run feed (events and phase reasoning).
* **Inspector:**
  - **node** — brief, gates, review rubric, audit, inputs, budget; reopen, redispatch, bypass, message.
  - **changes** — git status and diffs of the run's workspace (working tree, staged, commits), and every edit the selected node's tool calls made. Unified or split.
  - **artifact** — rendered or raw, with version history and version diffs.
  - **files** — browse and search the run directory or the workspace.
  - **docs** — `spec`, `contract` (with token ceiling), `spine`, `manifest`, `assembly`.
  - **usage** — token totals, a cumulative token curve, and breakdowns by role, phase, model and node. Prices are not shown: the ledger's `cost_usd` is an estimate from a built-in table, not a billed amount.
  - **events** — the full `events.jsonl` with type filter and search, plus resolved approvals.
  - **tools** — the CLI verbs that have no dedicated control (`status`, `resync`, `cache clear`, `eval`, `bench`), run as jobs with live output.

---

### Step 3: Steering the Agent & Handling Approvals

When Kusudaemon needs a human, the waiting tray opens:

* **The Pilot Editor:** edit the pilot artifact; the diff against the frozen original is shown live and becomes contract rules on **save**.
* **Batched Intake Form:** every ambiguity in one form, with the default assumption as each field's placeholder.
* **Amend Triage:** subtasks are sorted into `clean`, `patchable` and `regenerate` before you confirm; the counts expand to node lists.
* **Node Interventions:** reopen (repair), redispatch (reset to pending), bypass (cancel a running process) from the node tab, or type to message a live agent in the composer.

---

### Step 4: Command Palette & Slash Commands

`⌘K` / `Ctrl+K` opens the palette: commands, nodes and runs in one fuzzy list. Commands that need arguments ask for them step by step, dangerous ones ask to confirm, and any command with a CLI equivalent shows it, ready to copy. The composer accepts the same commands as `/name args`.

| Command | CLI equivalent |
|---|---|
| `new` | `kusudaemon run ...` |
| `resume` | `kusudaemon resume <run>` |
| `pause` / `continue` | `kusudaemon pause <run>` / clears `halt.flag` |
| `kill` | `kusudaemon kill <run>` |
| `escalate` | `kusudaemon escalate <run>` |
| `tier T0..T3` | `kusudaemon tier <run> T2` |
| `model <name\|default>` | `kusudaemon model <run> <name>` |
| `backend <name\|default>` | `kusudaemon backend <run> <name>` |
| `amend <rule>` | `kusudaemon amend <run> --text ...` |
| `reopen <node> [defect]` | `kusudaemon reopen <run> <node>` |
| `bypass <node> [process]` | `kusudaemon bypass <run> <node>` |
| `redispatch <node>`, `say <text>`, `approve`, `delete` | dashboard-side |
| `status`, `resync <path>`, `cache`, `eval` | `kusudaemon status\|resync\|cache clear\|eval` (as jobs) |
| `node`, `changes`, `artifact`, `files`, `docs`, `usage`, `events`, `tools`, `both`, `score`, `feed`, `rail`, `inspector`, `theme` | view only |

---

### Step 5: Keyboard Navigation Reference

| Shortcut | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Command palette. |
| `/` | Focus the composer. |
| `n` | New run. |
| `[` / `]` | Previous / next open run. |
| `j` / `k` | Next / previous node. |
| `↑` `↓` `←` `→` in the tree | Move, collapse, expand. |
| `1`–`8` | Inspector tabs. |
| `s` | Cycle score + feed / score / feed. |
| `p` | Pause or continue the run. |
| `⌘\` / `⌘.` | Toggle rail / inspector. |
| `Esc` | Close the palette or the new-run sheet. |

---

## 4. Symbol & Status Reference

### Step 1: Node Status Marks

Status is carried by shape as well as colour:

| Mark | Status | Meaning |
|---|---|---|
| dotted ring | **pending** | Waiting for dependencies. |
| solid accent, pulsing | **dispatched** | Writer episode running. |
| half-filled accent | **awaiting review** | Gates and reviewer are auditing. |
| solid green | **passed** | Cleared every gate and review. |
| red diamond | **failed** | Failed a gate; retries remain. |
| amber square | **blocked** | Out of attempts; needs the operator. |
| dashed ring | **stale / split** | Invalidated by an amendment, or split into children. |
| solid blue | **waiting** | A pending approval. |

The phase strip uses the same marks: an accent bar is running, green is done, red failed, blue waiting, amber halted, dashed skipped.

---

### Step 2: Task Shapes

Nodes are automatically classified by task **shape**; the node tab shows it as a chip:

| Chip | Shape | Description |
|---|---|---|
| `prose-dominant` | **Prose** | Text heavy (documentation, essays, user guides). |
| `derivation-dominant` | **Derivation** | Mathematical, formal logic, or algorithmic step-by-step derivations. |
| `problem-set-dominant` | **Problem set** | Code tasks, bug fixes, or unit-test driven subtasks. |
| `reference-dominant` | **Reference** | API reference sheets, glossaries, or index listings. |

---

### Step 3: Tier & Escalation Indicators

The run bar displays the execution Tier assigned by the classifier based on task scale:

| Badge | Tier | Scope & Execution Behavior |
|---|---|---|
| `T0` | **Direct** | Single-episode task; no task tree generated. 1 episode, ≤3 model calls. |
| `T1` | **Single Node** | Single node task with basic review. No `plan` phase, so **no decomposition** — and no mid-run split either, since the split handler is only installed at T2 and above. |
| `T2` | **Shallow Plan** | Multi-node flat plan (2–8 leaves); no pilot. Mid-run splits enabled. |
| `T3` | **Full Pipeline** | Full recursive decomposition pipeline with pilot, contract freeze, & deep review. |
| `T3↑` | **Floored** | A tier override raised the tier. Mid-run escalations are listed in the node tab when no node is selected. |

The classifier reads the *number of output files* a goal implies, not the amount
of writing it implies, so a goal that produces one large document ("write 100
sections into `book.md`") currently classifies **T1** and runs as a single
episode. If that is not what you want, set the tier explicitly with `--tier T2`.
Making the classifier read declared output size is tracked in
`PLAN-BENCH-INTEGRITY.md` §1.

---

### Step 4: Machine Gate Pips

In the node tree each node shows a row of small squares, one per code-evaluated gate: filled green passed, outlined red failed. The node tab lists every gate with its detail.

---

## 5. Project Documentation Map

This README is the user guide. The design and evaluation documents are separate,
and each owns one question:

| Document | Owns |
|---|---|
| `CLAUDE.md` | Commands and architecture overview for working on the codebase. |
| `BENCHMARKING.md` | Everything about benchmarking: the experimental design (§0), setup and commands (§2–§6), and the hermetic-suite gate that precedes any sweep (§9). |
| `docs/LONGGENBENCH-RESULTS-2026-09.md` | 120-run empirical results, statistics, caveats, and forensic bug audit for the LongGenBench sweep. |
| `PLAN-BENCH-INTEGRITY.md` | What the first sweeps found, which runs must be quarantined, and the repairs still open. |
| `docs/PLAN-WORKSPACE-MODE.md` | Why arm C underperforms a bare backend on workspace tasks, and the flagged fixes for it. |
| `PLAN-CONCURRENCY-AND-SHARED-STATE.md` | Parallelism, worktrees and rate-limit control — proposals with the case against each. Nothing there is decided. |
| `PLAN-TOKEN-ACCOUNTING.md` | Token accounting and output-size accounting: the estimator replacement (§A–§C), prompt disclosure (§D–§F), and output-size decomposition, retry continuation and part files (§H–§O). |
| `PLAN-SWEEP-REPAIR.md` | What must land before the next LongGenBench sweep, why the 2026-09-08 sweep produced no measurement, and what each step is verified to do. |
| `docs/PLAN-REVIEW-LATENCY.md`, `docs/PLAN-REVIEW-LATENCY-STATUS.md` | Review-path latency work and its current status. |
| `PLAN-REVIEW-READ-LOOP.md` | Reviewer transport and the review loop: streaming role calls with wall-clock deadlines (§R1), review-failure containment (§R2), the goal-directed read loop with coverage ledger (§R3), actionable verdicts (§R4), triage (§R5), role-provider plumbing (§R6), and the 2026-09-13/14 arm-C wave defects (§O1–§O10). |
| `docs/TESTING.md`, `docs/TEST-PLAN.md` | **Archived.** Superseded by `BENCHMARKING.md`; kept for the historical record of the Phase 0/1 repairs and the original benchmark design notes. |

When two documents appear to disagree, the one in this table's "Owns" column
wins for that question.
