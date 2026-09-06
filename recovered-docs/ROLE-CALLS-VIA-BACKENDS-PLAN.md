# Implementation plan — route *every* model call through the agent backends

**Goal.** Today only Writer episodes and v4 research probes go through a CLI agent backend
(`claude` / `codex` / `opencode` / `gptme`). Every other model call — classify, intake, survey,
plan, probe-plan, pilot-contract, orchestrate, review, document-review, revalidate, repair-triage,
assembly — is a raw `POST /chat/completions` made by `v1/provider.py::OpenAICompatibleProvider`,
resolved against `provider.json`'s **`gptme` block**. That is the only reason a run on the
`opencode` backend still needs an `OPENCODE_API_KEY`: the CLI authenticates itself against
OpenCode Zen, but the harness's own reasoning calls bypass the CLI entirely and need their own
credential.

**Target state.** The transport for a role call is a function of the run's backend:

| run backend | role-call transport | credential needed |
|---|---|---|
| `gptme` | `OpenAICompatibleProvider` (unchanged) | the `gptme.providers.*` key, as today |
| `claude` | one-shot `claude --print` episode | none (Claude Code's own auth) |
| `codex` | one-shot `codex exec` episode | none (Codex's own auth) |
| `opencode` | one-shot `opencode run` episode | **none** (OpenCode Zen via the CLI) |

A run with `--backend opencode` and a completely empty `.env` must work end to end.

---

## 0. What the current code actually looks like

Verified call graph (line numbers from the current tree):

- **The seam already exists in practice.** Nothing outside `v1/provider.py` calls
  `provider.complete()`. Every consumer uses exactly one method:
  `complete_json(messages, schema, *, temperature, retries, on_reasoning, streaming)`.
  `eval/runner.py:53 _ScriptedProvider` is already a duck-typed stand-in that satisfies it with no
  network. That is the entire surface to reimplement.

- **Consumers** (all sync, all `complete_json`): `v1/orchestrator.py:111`, `v1/reviewer.py:187`,
  `v2/intake.py:209`, `v2/survey.py:229`, `v2/planner.py:173`, `v2/pilot.py:177`,
  `v3/document_review.py:408,461`, `v3/revalidate.py:161`, `v3/repair.py`, `v3/assembly_loop.py`,
  `v4/probe_planner.py:310`, `v6/tiering.py:198,311`.

- **Construction sites** (4): `pipeline/run.py:263`, `pipeline/cli.py:378`,
  `dashboard/state.py:987` (`_default_driver`), `dashboard/state.py:1871` (`_runtime_for`).
  `RecursiveDriver.__init__` takes `provider` as a required keyword and, at `driver.py:446-460`,
  monkey-patches two private attributes onto it: `_should_abort` and `_on_model_fallback`.
  `on_backoff` is passed at construction.

- **Why a key is required today.** `OpenAICompatibleProvider.__init__` calls
  `provider_config.resolve()` eagerly (`v1/provider.py:123`), which reads the `gptme` block and
  raises `ProviderConfigError` when `base_url`/`model` can't be found. Worse,
  `provider_config.py:848-873` contains an explicit hack: if the selected model is declared under
  the **`opencode` CLI block**, `resolve()` re-routes it to `https://opencode.ai/zen/v1` with
  `OPENAI_API_KEY`, with a comment stating the reason — *"classify/plan/review are one-shot
  OpenAI-compatible calls and never go through the CLI, so 'the CLI brings its own auth' cannot
  cover them."* **That comment is the exact premise this work removes, and that block is deleted
  at the end of Phase 3.**

- **Backend plumbing that can be reused as-is**: `provider_config.read_backend_config()` returns a
  `BackendSettings` per backend with correct precedence and per-backend `api_key_env` isolation;
  `adapters/capabilities.py` has `translate_tools_to_claude_disallowed`,
  `translate_tools_to_opencode_permissions`, `emit_capability_event`;
  `adapters/trace_output.py::extract_visible_output` already populates
  `EpisodeResult.metadata["assistant_visible_output"]` for **all three** CLI adapters. Extracting a
  final assistant message from a CLI episode is a solved problem in this codebase.

---

## 1. Design

### 1.1 Formalize the seam: `RoleProvider`

New module `src/kusudaemon/roles/__init__.py` + `roles/protocol.py`:

```python
@runtime_checkable
class RoleProvider(Protocol):
    model: str
    def complete_json(self, messages, schema, *, temperature=0.0, retries=2,
                      on_reasoning=None, streaming=False) -> dict[str, Any]: ...
```

Promote the three driver-poked hooks from private attributes to a small explicit surface on a
shared base (`RoleProviderBase`), so `driver.py:446-460`'s `getattr(self.provider, "_should_abort")`
dance becomes `provider.set_abort_hook(...)` / `set_event_hook(...)`. `OpenAICompatibleProvider`
subclasses it; its existing `_should_abort` / `_on_model_fallback` / `_on_backoff` stay as the
implementation behind those setters, so nothing about the HTTP path changes.

Retype every `provider: OpenAICompatibleProvider` annotation (driver, all v1–v6 phase modules,
`pipeline/cli.py`) to `provider: RoleProvider`. Pure annotation churn, no behavior.

### 1.2 `BackendRoleProvider`

New `src/kusudaemon/roles/backend_provider.py`:

```python
class BackendRoleProvider(RoleProviderBase):
    def __init__(self, *, backend, run_dir, env, model=None,
                 budget=EpisodeBudget(max_duration_seconds=600),
                 max_episode_retries=2, log=None): ...
```

`complete_json` reproduces the existing contract exactly:

1. **Flatten** `messages` into one prompt string. `system` turns become a leading block; the
   schema instruction reuses `v1/json_schema.describe_schema(schema)` verbatim — the same prose
   the HTTP path already falls back to when an endpoint rejects `response_format`. There is no
   `response_format` equivalent for a CLI backend, so this is *always* the prose path; the
   `_response_format_ok` / `_format_supported` latches simply don't exist here.
2. **Run** a one-shot episode through `build_role_adapter(...)` (§1.3) with
   `live_trajectory_path` pointing into the run dir.
3. **Extract** `result.metadata["assistant_visible_output"]`, falling back to a last-JSON-object
   scan of the translated trace when it's empty.
4. **Parse + validate** with the *existing* `_parse_json_object` / `json_schema.validate` helpers —
   lift both out of `v1/provider.py` into a shared `roles/json_io.py` so both providers use one
   implementation.
5. **Re-prompt on failure**, identical to `v1/provider.py:284-291`: append the assistant text and a
   `"That did not validate: {error}. Return corrected JSON only."` user turn, re-flatten, re-run.
   Stateless by default (see §1.6 for the session-reuse optimization).
6. **Episode-level failure** (`status in ("error", "timeout")`) retries up to `max_episode_retries`
   and then raises `ProviderError`, which the driver's `_run_phase` already handles by marking the
   phase `error`. The §D11 rate-limit ladder does **not** apply — 429 handling belongs to the CLI
   now — so `RATE_LIMIT_BACKOFFS` and `_on_model_fallback` are HTTP-path-only. Emit a
   `role_episode_failed` event so a retry loop is visible rather than silent.

### 1.3 `build_role_adapter` — tool-less, read-only episodes

New factory in `pipeline/backends.py`, sibling to `build_writer_adapter` / `build_research_adapter`.
Every role call is text-in/JSON-out (CLAUDE.md §3) — it must not read or write anything. That is a
**hard invariant**, and it is enforceable per backend today:

| backend | tools | workspace | notes |
|---|---|---|---|
| `claude` | `--disallowedTools` = `translate_tools_to_claude_disallowed(())` → the full `ALL_CLAUDE_TOOLS` set | scratch dir | already supported |
| `opencode` | `translate_tools_to_opencode_permissions(())` → every key `deny` | scratch dir | via `OPENCODE_PERMISSION` |
| `codex` | **unsupported** — pass `sandbox_mode="read-only"` and `emit_capability_event(..., "tool_allowlist", role="<phase>")` | scratch dir | must **not** inherit the adapter's default `--dangerously-bypass-approvals-and-sandbox` |

`workspace_path` is `<run_dir>/tmp/roles/<phase>/` — an empty scratch directory, **not** the user's
workspace. Belt and braces: even if a tool restriction leaks (codex), there is nothing there to
read, which keeps §2 invariant 3 (role isolation) structural rather than prose-only. `hidden_paths`
still gets the run-dir subtree for the same reason.

Model resolution reuses `read_backend_config(backend, run_dir=..., model=...)` — no new precedence
ladder.

### 1.4 The sync-over-async bridge (the highest-risk piece)

`complete_json` is **sync**; `adapter.run_episode` is a **coroutine**; the phases that call
`complete_json` are `async def` methods running under `asyncio.run(driver.run())`. So:

- `asyncio.run()` inside `complete_json` raises (`RuntimeError: cannot be called from a running
  event loop`) whenever a phase is the caller.
- The fix is a dedicated worker: a module-level `EpisodeLoop` holding one background thread with
  its own event loop; `complete_json` does
  `concurrent.futures.Future.result()` on `asyncio.run_coroutine_threadsafe(...)`.
  A `threading.Semaphore` mirrors the HTTP provider's `concurrency=4` throttle.

**But offloading the work does not unblock the caller.** A sync `complete_json` called from a
coroutine blocks the driver's event loop for the whole call — true today too (urllib blocks), but a
CLI episode is 30 s–5 min instead of 2–20 s. With `max_parallel > 1` that stalls concurrent Writer
dispatch and the liveness heartbeat. Two-part answer:

- **Phase 2 (required):** the thread bridge above, so the calls at least *work*.
- **Phase 4 (required before flipping the default):** wrap the phase-level sync helpers at their
  `await` boundary in the driver — `survey_chunks`, the reviewer fan-out, `run_document_review`,
  `run_revalidation_pass`, `plan_partition` — as `await asyncio.to_thread(...)`. These helpers are
  already pure-sync and already take `provider` as a parameter, so this is a ~10-line diff per call
  site in `driver.py` and leaves the loop free to service writers and heartbeats.

### 1.5 Observability parity

The HTTP path streams `reasoning_content` into `on_reasoning`, which the driver renders as explorer
"thinking" cards (`driver.py:1057-1070`). Backend episodes must not regress this:

- Write each role episode's translated trace to
  `<run_dir>/roles/<phase>/<call_id>_raw_trajectory.jsonl` via `live_trajectory_path` — the same
  format the dashboard's incremental trace parser already reads.
- Tail that file from the bridge thread and forward `thinking` events to `on_reasoning`, so
  `_append_explorer_reasoning` and the Chat tab work unchanged.
- Emit the v0 subagent vocabulary (`episode_started`, `session_captured`, `episode_completed`) with
  `role="<phase>"`, so `_summarize_subagent` shows role calls as first-class subagents in the Nav
  panel — a visible win: today an eight-minute planner call shows as a silent `in_progress`.
- Record `{backend, model, duration_ms, attempts, episode_status}` on every call so
  `eval/measure.py`'s `calls_by_role` gains real cost data instead of just counts.

### 1.6 Cost and latency (do not skip this analysis)

A role call goes from one HTTP request to a process spawn + full agent turn. The multipliers are
real: `survey_chunks` is one call per chunk window, `document_review` one per window, the reviewer
fans out per node, `probe_planner` is one per 60 candidate nodes. Mitigations, in order of value:

1. **Session reuse within a phase.** `claude --resume` and `opencode run --session` let the
   validate-reprompt retry and successive windows share one session instead of paying cold start +
   full prompt each time. **Blocked on the audit's C6** (`_watch_for_session_id` in `v0/runner.py`
   returns on the bootstrap `logdir` line before the real `session_id` arrives) — fix C6 first.
2. **Keep §E17's input-digest phase cache.** It sits above the provider and keeps working
   untouched; it is what makes a resume cheap. Verify it still hits after the transport swap.
3. **Separate role model from writer model.** A `role_model` field per backend block in
   `provider.json` lets planning/review run on a cheap model while writers run on a strong one.
4. **Concurrency.** The semaphore in §1.4 should default higher than 1 for windowed phases; measure
   before tuning.

### 1.7 Configuration

`provider.json` gains two optional keys, both backward compatible:

```jsonc
{
  "gptme":  { "default": "nvidia", "providers": { /* ... unchanged ... */ } },
  "claude": { "model": null, "role_model": null },
  "codex":  { "model": null, "role_model": null, "wire_api": "responses" },
  "opencode": {
    "model": "opencode/deepseek-v4-flash-free",
    "role_model": "opencode/deepseek-v4-flash-free",
    "models": ["opencode/deepseek-v4-flash-free", "opencode/qwen3-coder"]
  },
  "roles": { "backend": null, "transport": null }   // null = follow the run's backend
}
```

- `roles.backend` — optional override for the split case ("writers on claude, reasoning on
  opencode"). `null` (default) means role calls use the run's own backend.
- `roles.transport` — `"backend"` | `"http"`, plus env override `KUSUDAEMON_ROLE_TRANSPORT`. This
  is the rollout flag; it becomes the escape hatch afterwards.
- **The `gptme` block becomes optional.** `read_config_file` must stop treating a missing `gptme`
  block as fatal for non-gptme runs. `resolve()` is only ever called on the HTTP path, which is now
  reachable only when the transport is `http`.

`.env` becomes fully optional for CLI-backend runs. Update `.env.example`, `provider.example.json`,
`SAMPLE_SETTINGS` (`provider_config.py:144`), and the README's §Step 2, which currently states an
API key is a hard requirement.

---

## 2. Phases

Each phase is independently shippable and leaves the tree green.

### Phase 0 — verify on your machine (half a day, do not skip)

Nothing below is worth writing until these are pinned down. Record answers as fixtures under
`tests/fixtures/`.

1. **Does a tool-less one-shot actually return clean JSON on each CLI?**
   For each of `claude --print --output-format stream-json --disallowedTools <all>`,
   `codex exec --json --sandbox read-only -`, `opencode run --format json --auto` with all
   permissions denied: feed a prompt containing `describe_schema(...)` of a real schema (use
   `v1/orchestrator.DISPATCH_SCHEMA`) and check that the final assistant message parses as a bare
   JSON object. **If a CLI wraps output in prose or fences, that determines how aggressive step 4's
   extractor must be** — the existing `_parse_json_object` already strips ``` fences, which may be
   enough.
2. **C4 from BACKEND-PARITY-AUDIT.md is still open and blocks this work for opencode.**
   `opencode run` takes the message as a positional argument, but `OpenCodeAdapter._template`
   delivers it via `< {prompt_path}` on stdin. Run
   `echo "reply with OK" | opencode run --format json --auto`. If it hangs or returns empty, fix
   prompt delivery (audit Phase 1a: `prompt_delivery = "argv"` + a `{prompt_arg}` placeholder)
   **before** anything else — every role call on opencode depends on it.
3. **Latency.** Time a trivial tool-less turn on each backend. Multiply by your largest survey
   window count. If a T3 plan phase goes from 40 s to 20 min, §1.6's mitigations move from
   "optimization" to "Phase 2 scope".
4. `codex exec --sandbox read-only` — confirm the flag name and that it still reads stdin via `-`.

### Phase 1 — the seam (no behavior change, fully shippable)

- Add `roles/protocol.py` (`RoleProvider`, `RoleProviderBase` with `set_abort_hook` /
  `set_event_hook`).
- Move `_parse_json_object` and the JSON-instruction builder into `roles/json_io.py`; both
  providers import them.
- `OpenAICompatibleProvider` subclasses `RoleProviderBase`; `driver.py:446-460` uses the setters.
- Retype `provider:` annotations across driver + v1–v6 + `pipeline/cli.py`.
- Add `roles/factory.py::make_role_provider(options, run_dir, env, log)` and route **all four**
  construction sites through it. Initially it returns `OpenAICompatibleProvider` unconditionally —
  this phase is purely about having one place to change.
- **Make construction lazy.** `pipeline/cli.py:378` builds a provider unconditionally at the top of
  `cmd_amend`; with the `gptme` block optional that must not raise. The factory returns a thin lazy
  proxy that resolves on first `complete_json`.
- Tests: existing suite must pass untouched. Add `test_role_provider_protocol.py` asserting
  `_ScriptedProvider` satisfies `RoleProvider`.

### Phase 2 — `BackendRoleProvider`, behind the flag

- `roles/episode_loop.py` (the thread bridge, §1.4).
- `roles/backend_provider.py` (§1.2).
- `pipeline/backends.py::build_role_adapter` (§1.3).
- `make_role_provider` returns `BackendRoleProvider` when
  `KUSUDAEMON_ROLE_TRANSPORT=backend` **and** the backend is not `gptme`; otherwise the HTTP
  provider. Default stays `http` — nothing changes for existing users yet.
- Tests: a `FakeEnvironment` whose `exec` returns a canned translated trace makes the whole class
  unit-testable with zero subprocesses. Cover: happy path; fenced JSON; schema-invalid → reprompt →
  valid; two schema failures → `ProviderError`; episode `timeout` → retry → `ProviderError`;
  `set_abort_hook` returning `True` mid-retry stops immediately.
- Matrix test (extend `test_backend_toggle.py`): for each backend × each role, assert the
  constructed `command_template` contains **no** write/edit/bash tool, no harness provider
  credential, and a scratch `cd` target.

### Phase 3 — flip the transport, drop the credential coupling

- Default `roles.transport` to `"backend"` for `claude`/`codex`/`opencode`; `gptme` keeps `http`.
- **Delete `provider_config.py:848-873`** (the opencode-model → OpenCode Zen re-route). Its stated
  premise no longer holds.
- **Delete the cross-backend key fallbacks** flagged as C5 in the audit: `os.getenv("OPENAI_API_KEY")`
  in `codex.py` and `opencode.py`'s constructors. Each backend reads only its own `api_key_env`.
  Regression test: `OPENAI_API_KEY` set in the environment must not appear in any adapter's
  `command_template`.
- Make the `gptme` block optional in `read_config_file`; `resolve()` raises only on the HTTP path.
- **Acceptance test for the whole feature:** a run with `--backend opencode`, an empty `.env`, and a
  `provider.json` containing only an `opencode` block completes classify → plan → execute → review
  against a fake environment. This is the test that literally encodes your request.
- Docs: README §Step 2 and §Choosing an Agent Backend, `.env.example`, `provider.example.json`,
  `SAMPLE_SETTINGS`.

### Phase 4 — keep the event loop alive + observability

- `await asyncio.to_thread(...)` at the driver's phase-level call sites (§1.4).
- Role-episode trace files, `on_reasoning` forwarding, v0 subagent events (§1.5).
- Dashboard: role calls appear in the Nav subagent list; the run header's model selector becomes
  backend-scoped (`list_models_for_backend` already exists and is unused by the frontend for this).
- `eval/measure.py` gains `duration_ms` / `backend` per call.

### Phase 5 — performance

- Fix C6 (`_watch_for_session_id`), then session reuse within a phase (§1.6.1).
- `role_model` per backend block.
- Tune the bridge semaphore against Phase 0's timings.

---

## 3. Files touched

| File | Change |
|---|---|
| `roles/protocol.py`, `roles/json_io.py`, `roles/factory.py`, `roles/episode_loop.py`, `roles/backend_provider.py` | **new** |
| `v1/provider.py` | subclass `RoleProviderBase`; export the shared JSON helpers; HTTP-only ladder stays |
| `pipeline/backends.py` | `+ build_role_adapter` |
| `pipeline/driver.py` | provider type; hook setters; `asyncio.to_thread` at phase boundaries |
| `pipeline/run.py:263`, `pipeline/cli.py:378`, `dashboard/state.py:987,1871` | construct via `make_role_provider` |
| `provider_config.py` | optional `gptme` block; `roles` block; `role_model`; **delete lines 848-873** |
| `adapters/codex.py`, `adapters/opencode.py` | delete `OPENAI_API_KEY` fallbacks (C5) |
| `adapters/opencode.py` | prompt delivery fix if Phase 0.2 confirms C4 |
| `v0/runner.py` | C6 session capture (Phase 5) |
| v1–v6 phase modules | annotation only |
| `README.md`, `.env.example`, `provider.example.json` | credential story |
| `tests/` | new: `test_role_provider_protocol.py`, `test_backend_role_provider.py`, `test_role_adapter_matrix.py`, `test_keyless_run.py` |

---

## 4. Risks

1. **Structured-output reliability.** The HTTP path can use `response_format: json_schema` when the
   endpoint supports it; a CLI backend never can. Every role call falls back to prose-schema +
   validate + reprompt. On a weak model (`deepseek-v4-flash-free`) that raises the retry rate and
   therefore cost. Phase 0.1 measures it; if it's bad, `retries` for backend transport should
   default higher than the HTTP path's 2.
2. **Latency and event-loop starvation.** The biggest one; §1.4 Phase 4 is not optional.
3. **Codex cannot be locked down.** No tool allowlist. `--sandbox read-only` + an empty scratch cwd
   is the best available; the `capability_unavailable` event makes the gap auditable rather than
   silent, per the audit's load-bearing rule.
4. **Opencode prompt delivery (C4) is unverified and load-bearing.** If `opencode run` doesn't take
   stdin, *every* role call on your primary backend silently runs on an empty prompt.
5. **Eval determinism.** `_ScriptedProvider` keeps working (same protocol), so `eval/runner.py` is
   unaffected — verify `_T2_REVIEW_CALLS` and the `calls_by_role` mapping still hold, since call
   *counts* are the harness's own regression signal.

---

## 5. Decision points

Three choices change the shape of the work. My recommendation first in each.

1. **Does `gptme` also route through a CLI?** *Recommend no.* gptme *is* the OpenAI-compatible
   backend; a gptme role call would be the same HTTP request with a subprocess wrapped around it.
   Keeping `gptme → http` also means the default install's behavior is byte-identical after this
   change.
2. **One backend for everything, or a separate `roles.backend`?** *Recommend implementing the
   override but defaulting to `null`* (role calls follow the run's backend). It's ~15 lines and
   covers the real use case of cheap-reasoning/strong-writers.
3. **Rollout flag, or flip directly?** *Recommend the flag* (`KUSUDAEMON_ROLE_TRANSPORT`), kept
   permanently as an escape hatch. If Phase 0 shows latency is fine, Phases 2 and 3 can land in one
   PR.
