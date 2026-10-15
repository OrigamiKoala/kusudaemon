// Derives the Score (timeline) model from events.jsonl + the tree.

const BAD = /fail|error|crash|blocked|reject|timeout|degenerate|shrank|conflict|stalled|killed/;
const WARN = /unavailable|backoff|throttl|transport|skipped|degraded|nudged|warning|bypass|fell_back|regress|inert|disabled/;
const OK = /passed|approved|done|salvaged|restored|completed|repaired|exported|cached|synthesized|derived/;
const WAIT = /requested|awaiting|approval|reopened/;

export function eventTone(type) {
  const t = String(type || "");
  if (BAD.test(t)) return "bad";
  if (WARN.test(t)) return "warn";
  if (WAIT.test(t)) return "wait";
  if (OK.test(t)) return "ok";
  return "";
}

const START = new Set(["node_dispatched", "node_redispatched", "node_reopened"]);
// point events that matter on a node's staff line
const MARK_SKIP = new Set([
  "session_captured", "capability_unavailable", "node_dispatch_decided", "heartbeat", "orchestrator_decision",
  "orchestrator_call_skipped", "node_dispatched", "node_redispatched", "node_reopened", "episode_completed",
  "max_parallel_derived", "max_parallel_inert", "usage", "reasoning", "message", "thinking", "logdir",
]);

export function buildScore(events, treeRows, { now, running }) {
  const evs = events || [];
  let t0 = Infinity;
  let tEnd = 0;
  for (const e of evs) {
    if (!e.ts) continue;
    if (e.ts < t0) t0 = e.ts;
    if (e.ts > tEnd) tEnd = e.ts;
  }
  if (!isFinite(t0)) return { t0: 0, t1: 1, lanes: [], phases: [], empty: true };
  const t1 = Math.max(running ? now : tEnd, tEnd, t0 + 1);

  const lanes = new Map();
  const lane = (id) => {
    let l = lanes.get(id);
    if (!l) {
      l = { id, episodes: [], marks: [], open: null };
      lanes.set(id, l);
    }
    return l;
  };
  // tree order first so rows are stable even before their first event
  for (const n of treeRows || []) lane(n.id);

  const phases = [];
  const phaseOpen = new Map();

  for (const e of evs) {
    const ts = e.ts || t0;
    const type = e.type;
    if (type === "phase_started") {
      const span = { phase: e.phase, start: ts, end: null, status: "running" };
      phases.push(span);
      phaseOpen.set(e.phase, span);
      continue;
    }
    if (type === "phase_done" || type === "phase_failed" || type === "phase_skipped") {
      let span = phaseOpen.get(e.phase);
      if (!span) {
        span = { phase: e.phase, start: ts, end: ts, status: "done" };
        phases.push(span);
      }
      span.end = ts;
      span.status = type === "phase_failed" ? "failed" : type === "phase_skipped" ? "skipped" : e.status === "failed" ? "failed" : "done";
      phaseOpen.delete(e.phase);
      continue;
    }
    const id = e.node_id;
    if (!id || id === "-") continue;
    const l = lane(id);
    if (START.has(type)) {
      if (l.open) {
        l.open.end = ts;
        l.open.status = "interrupted";
        l.episodes.push(l.open);
      }
      l.open = { start: ts, end: null, status: "running", attempt: l.episodes.length + 1, via: type };
      continue;
    }
    if (type === "episode_completed") {
      const dur = (e.duration_ms || 0) / 1000;
      const ep = l.open || { start: Math.max(t0, ts - dur), attempt: l.episodes.length + 1, via: "" };
      ep.end = ts;
      ep.status = String(e.status || "done");
      ep.error = e.error || "";
      ep.duration = dur || ts - ep.start;
      l.episodes.push(ep);
      l.open = null;
      continue;
    }
    if (!MARK_SKIP.has(type)) l.marks.push({ ts, type, tone: eventTone(type), detail: e.detail || e.reason || (e.unmet ? e.unmet.join(", ") : "") || e.message || "" });
  }
  for (const [, span] of phaseOpen) span.end = running ? t1 : span.start;

  // an episode with no end is live only while the run is; otherwise it was cut
  for (const l of lanes.values()) {
    if (l.open) {
      l.open.end = t1;
      l.open.status = running ? "running" : "cut";
      l.episodes.push(l.open);
      l.open = null;
    }
  }
  const out = [];
  const treeIds = new Set((treeRows || []).map((n) => n.id));
  for (const l of lanes.values()) {
    if (!l.episodes.length && !l.marks.length && !treeIds.has(l.id)) continue;
    out.push(l);
  }
  return { t0, t1, lanes: out, phases, empty: false };
}

/**
 * Tree rows -> nested structure. Real `parent` links win; otherwise dotted ids
 * (`c03.thermo.leaf`) group under virtual folders, as textbook-style runs name them.
 */
export function buildTree(rows) {
  const nodes = new Map();
  for (const r of rows) nodes.set(r.id, { id: r.id, row: r, children: [], virtual: false, parent: null });
  const ensureFolder = (id) => {
    let n = nodes.get(id);
    if (!n) {
      n = { id, row: null, children: [], virtual: true, parent: null };
      nodes.set(id, n);
      const cut = id.lastIndexOf(".");
      if (cut > 0) attach(n, id.slice(0, cut));
    }
    return n;
  };
  const attach = (n, parentId) => {
    const p = ensureFolder(parentId);
    if (p === n) return;
    n.parent = p.id;
    p.children.push(n);
  };
  for (const r of rows) {
    const n = nodes.get(r.id);
    if (r.parent && nodes.has(r.parent)) attach(n, r.parent);
    else {
      const cut = r.id.lastIndexOf(".");
      if (cut > 0) attach(n, r.id.slice(0, cut));
    }
  }
  // keep the plan's own order: a folder sits where its first child sits
  const rank = new Map(rows.map((r, i) => [r.id, i]));
  const ord = (n) => {
    if (n.ord === undefined) n.ord = n.row ? (rank.get(n.id) ?? 0) : Math.min(...n.children.map(ord));
    return n.ord;
  };
  for (const n of nodes.values()) if (n.children.length) n.children.sort((a, b) => ord(a) - ord(b));
  const roots = [...nodes.values()].filter((n) => !n.parent).sort((a, b) => ord(a) - ord(b));
  const tally = (n) => {
    const c = {};
    const add = (x) => {
      if (x.row && !x.children.length) c[x.row.status] = (c[x.row.status] || 0) + 1;
      x.children.forEach(add);
    };
    add(n);
    return c;
  };
  for (const n of nodes.values()) if (n.children.length) n.counts = tally(n);
  return roots;
}

/** DFS flatten honouring a collapsed-id set. */
export function flattenTree(roots, collapsed) {
  const out = [];
  const walk = (n, depth) => {
    out.push({ ...n, depth, open: n.children.length > 0 && !collapsed.has(n.id) });
    if (n.children.length && !collapsed.has(n.id)) n.children.forEach((c) => walk(c, depth + 1));
  };
  roots.forEach((r) => walk(r, 0));
  return out;
}
