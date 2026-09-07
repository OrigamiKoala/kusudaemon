#!/usr/bin/env bash
# HarnessBench <-> kusudaemon bridge (BENCHMARKING.md §0.2 "Shape A").
#
# HarnessBench's `generic_cli` adapter invokes this with cwd set to the task
# workspace. It is registered in harness-bench's config/harness.yaml as e.g.
#
#   "kusudaemon-armC": {
#     "adapter": "generic_cli",
#     "command": "/abs/path/to/kusudaemon/scripts/hb_adapter.sh",
#     "args": ["C", "{workspace}", "{prompt_file}", "{session_id}", "{task_id}"]
#   }
#
# Usage: hb_adapter.sh <arm> <workspace> <prompt_file> <session_id> <task_id>
#
# Everything else is configured through KUSU_BENCH_* environment variables so
# one wrapper serves every arm and every backend. See BENCHMARKING.md §3.
set -uo pipefail

ARM="${1:?arm (A|B|C) required}"
WORKSPACE="${2:?workspace required}"
PROMPT_FILE="${3:?prompt file required}"
SESSION_ID="${4:-unknown-session}"
TASK_ID="${5:-${HARNESSBENCH_TASK_ID:-unknown-task}}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BACKEND="${KUSU_BENCH_BACKEND:-opencode}"
MODEL="${KUSU_BENCH_MODEL:-}"
SEED="${KUSU_BENCH_SEED:-1}"
# No token or round ceiling by default. HarnessBench does not require one, and
# kusudaemon treats an unset budget as genuinely unbounded (RunOptions
# max_total_tokens=None skips the ceiling check entirely). Set
# KUSU_BENCH_BUDGET_TOKENS / KUSU_BENCH_MAX_ROUNDS to reimpose one; when unset,
# the flags are omitted and the CLI's own defaults apply (max-rounds 100).
BUDGET_TOKENS="${KUSU_BENCH_BUDGET_TOKENS:-}"
MAX_ROUNDS="${KUSU_BENCH_MAX_ROUNDS:-}"
TIER="${KUSU_BENCH_TIER:-auto}"
BENCH_NAME="${KUSU_BENCH_NAME:-harness-bench}"
SANDBOX="${HARNESSBENCH_SANDBOX:-$WORKSPACE/..}"

# kusudaemon resolves provider.json / .env relative to the *invoking* cwd, and
# our cwd here is the task workspace. Pin both to the repo explicitly.
export KUSUDAEMON_PROVIDER_CONFIG="${KUSUDAEMON_PROVIDER_CONFIG:-$REPO_ROOT/provider.json}"
export KUSUDAEMON_ENV_FILE="${KUSUDAEMON_ENV_FILE:-$REPO_ROOT/.env}"
export KUSUDAEMON_NO_NOTIFY=1
export KUSUDAEMON_OUTPUT_DIR="${KUSUDAEMON_OUTPUT_DIR:-$SANDBOX/kusudaemon-out}"
export KUSUDAEMON_ROLE_TIMEOUT="${KUSUDAEMON_ROLE_TIMEOUT:-300}"
export KUSUDAEMON_REVIEWER_TIMEOUT="${KUSUDAEMON_REVIEWER_TIMEOUT:-120}"

# Optional: route role (orchestrator/planner/reviewer) traffic through
# HarnessBench's usage proxy so its token accounting and process/security
# rubric see the run. Requires an upstream base_url -- set KUSU_BENCH_UPSTREAM.
# Backends whose CLI owns its own auth (opencode) cannot be proxied; see
# BENCHMARKING.md §5.
if [ "${KUSU_BENCH_PROXY:-0}" = "1" ] \
   && [ -n "${HARNESSBENCH_LLM_PROXY_URL:-}" ] \
   && [ -n "${HARNESSBENCH_LLM_PROXY_ROUTES:-}" ] \
   && [ -n "${KUSU_BENCH_UPSTREAM:-}" ]; then
  python3 - "$HARNESSBENCH_LLM_PROXY_ROUTES" "$KUSU_BENCH_UPSTREAM" <<'PY'
import json, sys, pathlib
routes_file, upstream = pathlib.Path(sys.argv[1]), sys.argv[2].rstrip("/")
try:
    existing = json.loads(routes_file.read_text(encoding="utf-8"))
except Exception:
    existing = {}
existing["/kusudaemon"] = {
    "framework": "kusudaemon",
    "provider": "primary",
    "upstream": upstream,
}
routes_file.parent.mkdir(parents=True, exist_ok=True)
routes_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
PY
  export KUSUDAEMON_PROVIDER_BASE_URL="${HARNESSBENCH_LLM_PROXY_URL%/}/kusudaemon"
  export KUSUDAEMON_ROLE_TRANSPORT="${KUSUDAEMON_ROLE_TRANSPORT:-http}"
fi

RUNS_ROOT="$SANDBOX/kusudaemon-runs"
mkdir -p "$RUNS_ROOT"

# Eight of the 106 tasks are multi-round: HarnessBench calls the adapter once
# per round with the same workspace and the same session_id, and expects the
# agent to carry its own conversation across those calls. Task
# 007-session-memory exists purely to test that -- round 1 hands over a
# passphrase, round 2 states that it is NOT in the message and must be recalled
# from session memory, and a hook fails the task if it was written to the
# workspace.
#
# Do NOT bridge rounds by pasting earlier prompts into the current goal: for
# 007 that is the answer key, and it silently turns the task into "copy this
# string to a file". Each arm instead uses its own real continuity mechanism --
# arm A resumes the backend CLI's session (--round tells cmd_bench to), and
# arm C is pointed at the previous rounds' run directories, which are where
# kusudaemon's memory actually lives.
ROUND=1
case "$(basename "$PROMPT_FILE")" in
  prompt-round*.txt|prompt_round*.txt)
    ROUND="$(basename "$PROMPT_FILE" .txt)"
    ROUND="${ROUND#prompt-round}"
    ROUND="${ROUND#prompt_round}"
    ;;
  *day*.txt)
    ROUND="$(basename "$PROMPT_FILE" .txt)"
    ROUND="${ROUND#*day}"
    ;;
esac
case "$ROUND" in ''|*[!0-9]*) ROUND=1 ;; esac

GOAL_FILE="$PROMPT_FILE"
if [ "$ROUND" -gt 1 ] && [ "$ARM" != "A" ]; then
  # kusudaemon rebuilds every context from the run directory -- that directory
  # IS its session memory. Name the earlier rounds' run dirs so the pipeline can
  # go and read them; never inline their content, or we are back to prompt
  # stuffing. The run root sits outside $WORKSPACE, so the round-1 leak scan
  # (which only inspects the workspace) is unaffected.
  PRIOR_RUNS=""
  for d in "$RUNS_ROOT"/hb_"${TASK_ID}"_arm"${ARM}"_s"${SEED}"_r*; do
    [ -d "$d" ] && PRIOR_RUNS="${PRIOR_RUNS}  - ${d}\n"
  done
  if [ -n "$PRIOR_RUNS" ]; then
    GOAL_FILE="$SANDBOX/kusudaemon-goal-round${ROUND}.txt"
    {
      cat "$PROMPT_FILE"
      printf '\n\n---\n\n## Session memory\n\n'
      printf 'This is round %s of a multi-round task. Your own record of the\n' "$ROUND"
      printf 'earlier rounds -- the frozen goal, the event log and the artifacts\n'
      printf 'produced -- is on disk at:\n\n'
      printf "$PRIOR_RUNS"
      printf '\nRead from there when the current instruction refers to something\n'
      printf 'established earlier. These paths are your memory, not task inputs:\n'
      printf 'do not copy them, or anything read from them, into the workspace\n'
      printf 'unless the current instruction asks for that content specifically.\n'
    } > "$GOAL_FILE"
  fi
fi

ARGS=(
  bench
  --workspace   "$WORKSPACE"
  --goal-file   "$GOAL_FILE"
  --arm         "$ARM"
  --seed        "$SEED"
  --session-id  "$SESSION_ID"
  --round       "$ROUND"
  --backend     "$BACKEND"
  --benchmark   "$BENCH_NAME"
  --task-id     "$TASK_ID"
  --tier        "$TIER"
  --runs-root   "$RUNS_ROOT"
  --run-id      "hb_${TASK_ID}_arm${ARM}_s${SEED}_r${ROUND}_${SESSION_ID}"
  --output      "$SANDBOX/kusudaemon-record-r${ROUND}.json"
)
[ -n "$MODEL" ] && ARGS+=(--model "$MODEL")
[ -n "$BUDGET_TOKENS" ] && ARGS+=(--budget-tokens "$BUDGET_TOKENS")
[ -n "$MAX_ROUNDS" ] && ARGS+=(--max-rounds "$MAX_ROUNDS")

# Run from the repo root so relative imports and defaults behave, but keep the
# task workspace as the work root (passed explicitly above). PYTHONPATH beats a
# stale editable install -- same reason tests/ pin sys.path.
cd "$REPO_ROOT" || exit 1
PYTHONPATH="$REPO_ROOT/src" python3 -m kusudaemon.cli "${ARGS[@]}"
STATUS=$?

# Last round's record is also written to the canonical path for convenience;
# run_harness_bench.py merges the per-round records for accounting.
cp -f "$SANDBOX/kusudaemon-record-r${ROUND}.json" "$SANDBOX/kusudaemon-record.json" 2>/dev/null || true

# A halt is a recorded outcome, not a crash (BENCHMARKING.md §0.4), and HarnessBench
# stops issuing rounds the moment an adapter exits non-zero -- which would
# forfeit rounds 2..N of the eight multi-round tasks and grade a workspace that
# never got its later instructions. Exit 0 so the oracle always sees the real
# final state; the sidecar record carries the true status. Set
# KUSU_BENCH_STRICT_EXIT=1 to propagate instead.
if [ "${KUSU_BENCH_STRICT_EXIT:-0}" = "1" ]; then
  exit $STATUS
fi
exit 0
