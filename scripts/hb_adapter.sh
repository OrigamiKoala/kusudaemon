#!/usr/bin/env bash
# HarnessBench <-> kusudaemon bridge (TESTING.md §2 "Shape A").
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
BUDGET_TOKENS="${KUSU_BENCH_BUDGET_TOKENS:-120000}"
MAX_ROUNDS="${KUSU_BENCH_MAX_ROUNDS:-60}"
TIER="${KUSU_BENCH_TIER:-auto}"
BENCH_NAME="${KUSU_BENCH_NAME:-harness-bench}"
SANDBOX="${HARNESSBENCH_SANDBOX:-$WORKSPACE/..}"

# kusudaemon resolves provider.json / .env relative to the *invoking* cwd, and
# our cwd here is the task workspace. Pin both to the repo explicitly.
export KUSUDAEMON_PROVIDER_CONFIG="${KUSUDAEMON_PROVIDER_CONFIG:-$REPO_ROOT/provider.json}"
export KUSUDAEMON_ENV_FILE="${KUSUDAEMON_ENV_FILE:-$REPO_ROOT/.env}"

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
# per round with the same workspace and session_id, writing prompt-roundN.txt
# into the sandbox each time. A shared-terminal agent carries its conversation
# across those calls; kusudaemon starts a fresh run per invocation, so without
# help round 2 sees only the workspace and not what round 1 was asked to do.
# Prepend the earlier rounds as stated context, identically for every arm so
# the comparison stays fair, and give each round its own run id.
ROUND=1
case "$(basename "$PROMPT_FILE")" in
  prompt-round*.txt)
    ROUND="$(basename "$PROMPT_FILE" .txt)"
    ROUND="${ROUND#prompt-round}"
    ;;
esac

GOAL_FILE="$PROMPT_FILE"
if [ "$ROUND" -gt 1 ] 2>/dev/null; then
  GOAL_FILE="$SANDBOX/kusudaemon-goal-round${ROUND}.txt"
  : > "$GOAL_FILE"
  i=1
  while [ "$i" -lt "$ROUND" ]; do
    PRIOR="$SANDBOX/prompt-round${i}.txt"
    if [ -f "$PRIOR" ]; then
      {
        printf '## Earlier instruction %s of %s (already carried out in this workspace)

' "$i" "$((ROUND - 1))"
        cat "$PRIOR"
        printf '

'
      } >> "$GOAL_FILE"
    fi
    i=$((i + 1))
  done
  {
    printf '## Current instruction

'
    cat "$PROMPT_FILE"
  } >> "$GOAL_FILE"
fi

ARGS=(
  bench
  --workspace   "$WORKSPACE"
  --goal-file   "$GOAL_FILE"
  --arm         "$ARM"
  --seed        "$SEED"
  --backend     "$BACKEND"
  --benchmark   "$BENCH_NAME"
  --task-id     "$TASK_ID"
  --tier        "$TIER"
  --budget-tokens "$BUDGET_TOKENS"
  --max-rounds  "$MAX_ROUNDS"
  --runs-root   "$RUNS_ROOT"
  --run-id      "hb_${TASK_ID}_arm${ARM}_s${SEED}_r${ROUND}_${SESSION_ID}"
  --output      "$SANDBOX/kusudaemon-record-r${ROUND}.json"
)
[ -n "$MODEL" ] && ARGS+=(--model "$MODEL")

# Run from the repo root so relative imports and defaults behave, but keep the
# task workspace as the work root (passed explicitly above). PYTHONPATH beats a
# stale editable install -- same reason tests/ pin sys.path.
cd "$REPO_ROOT" || exit 1
PYTHONPATH="$REPO_ROOT/src" python3 -m kusudaemon.cli "${ARGS[@]}"
STATUS=$?

# Last round's record is also written to the canonical path for convenience;
# run_harness_bench.py merges the per-round records for accounting.
cp -f "$SANDBOX/kusudaemon-record-r${ROUND}.json" "$SANDBOX/kusudaemon-record.json" 2>/dev/null || true

# A halt is a recorded outcome, not a crash (TESTING.md §4), and HarnessBench
# stops issuing rounds the moment an adapter exits non-zero -- which would
# forfeit rounds 2..N of the eight multi-round tasks and grade a workspace that
# never got its later instructions. Exit 0 so the oracle always sees the real
# final state; the sidecar record carries the true status. Set
# KUSU_BENCH_STRICT_EXIT=1 to propagate instead.
if [ "${KUSU_BENCH_STRICT_EXIT:-0}" = "1" ]; then
  exit $STATUS
fi
exit 0
