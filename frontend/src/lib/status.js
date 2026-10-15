// Mapping of backend status strings to the dot vocabulary in app.css.
export function runDot(run) {
  if (!run) return "pending";
  if (run.pending_approvals > 0) return "waiting";
  const s = run.status;
  if (s === "failed" || s === "error") return "failed";
  if (s === "halted" || s === "escalated") return "blocked";
  if (s === "in_progress" || (run.hosted && s !== "done")) return "running";
  if (s === "done") return "passed";
  if (s === "awaiting_approval") return "waiting";
  return "pending";
}

export function phaseDot(status) {
  switch (status) {
    case "in_progress": return "running";
    case "done": return "passed";
    case "failed":
    case "error": return "failed";
    case "awaiting_approval": return "waiting";
    case "halted":
    case "escalated":
    case "stalled": return "blocked";
    case "skipped": return "split";
    default: return "pending";
  }
}

export const PHASES = ["classify", "intake", "survey", "explore", "plan", "pilot", "research", "execute", "review", "assemble", "verify"];

export const NODE_ORDER = ["failed", "blocked", "dispatched", "awaiting_review", "pending", "stale", "passed", "split"];
