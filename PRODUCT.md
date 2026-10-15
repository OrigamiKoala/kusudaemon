# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Svelte 5 + Vite (delegated: user chose "framework + build step" over vanilla; Svelte picked for small output and fine-grained updates under a live event stream). Source in `frontend/`; the production build is committed to `src/kusudaemon/dashboard/static/` so `pip install -e .` needs no Node. Served by the existing Python `ThreadingHTTPServer` (`dashboard/server.py`) over JSON routes and an SSE stream (`/api/stream`).

## Users

One operator (the repository owner, a researcher/engineer) who launches long-horizon, corpus-scale runs and watches or steers them for minutes to hours, often from a second monitor next to an editor. Several runs may be live at once.

## Product Purpose

Kusudaemon decomposes one goal into leaves, drives each leaf to a verified artifact through a pluggable agent backend (gptme, Claude Code, Codex, OpenCode, Antigravity), and reassembles the results. The dashboard is the control and observation surface: every verb the CLI has, plus live visibility into what each agent is thinking, which tools it ran, what they returned, and what changed on disk.

## Positioning

A harness UI whose unit of work is a tree of nodes with gates, reviews, contracts and approvals, not a single chat. Neighbouring harnesses (Claude Code, OpenCode, Codex, Antigravity) show one agent loop; this shows a whole decomposed run and lets the operator intervene at any node.

## Operating Context

- Phases: classify, intake, survey, explore, plan, pilot, research, execute, review, assemble. Tiers T0 to T3 (tiers only rise).
- Node statuses and gates are decided by code; the operator approves pilots, intake answers, amendments, reopen/redispatch, escalation.
- Run dir (`~/.kusudaemon/runs/<id>`) is the source of truth: `tree.json`, `events.jsonl`, `scratch/<node>/trace.jsonl`, `out/`, `audit/`, `assembly/`.
- Terminology to preserve: run, node, leaf, unit, gate, contract, spine, pilot, tier, episode, writer, reviewer, orchestrator, planner, assembly, halt, bypass, reopen, redispatch, amend.

## Capabilities and Constraints

- Full CLI parity is required: run (all RunOptions), resume, status, approve, amend, escalate, pause/halt, kill, reopen, bypass, tier, model, backend, resync, delete run, interject, plus read access to spec, contract, spine, manifest, assembly, node artifact, versions, diffs, trace and thinking.
- Must display model thinking, tool calls and their results, and git diffs (workspace and per node).
- Must include: command palette and shortcuts, token/context meters, multi-run workspace, file tree and artifact preview.
- New read-only server routes for git status/diff are approved; existing routes must not change shape.
- Control routes honour `control_enabled` (read-only mode) and an optional auth token (cookie / Bearer).
- Data volume is high and growing (traces of thousands of entries); the UI must stay responsive while streaming.
- Undecided: none that block work.

## Brand Commitments

Name: Kusudaemon. Lowercase wordmark. No mascot. Instruction from the user: elegant, sleek, minimalistic; may be dense; every piece of text on screen must be strictly necessary (no explanatory prose, taglines, placeholders that explain, or help copy).

## Evidence on Hand

Real run data lives under `~/.kusudaemon/runs/`; the old UI is `src/kusudaemon/dashboard/static/` (kept in git history). No testimonials, benchmarks or marketing claims should appear in the UI.

## Product Principles

1. State comes from the run directory; the UI never invents or hides state.
2. Everything the CLI can do is one keystroke or click away; nothing destructive is one click.
3. Density without noise: show data, not description.
4. Live and history read the same: a streaming entry and a finished one render identically.
5. Words only when they are labels, values or errors.

## Accessibility & Inclusion

Keyboard-complete (palette, list navigation, focus visible), WCAG AA contrast, respects `prefers-reduced-motion` and `prefers-color-scheme`.
