import { ApiError, del, get, getToken, onAuthRequired, post, setToken, url } from "./api.js";

/* ------------------------------------------------------------ persisted ui */
const UI_KEY = "kd.ui";
function loadUi() {
  try {
    return JSON.parse(localStorage.getItem(UI_KEY) || "{}") || {};
  } catch {
    return {};
  }
}
function saveUi(data) {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify(data));
  } catch {
    /* storage unavailable */
  }
}
const saved = loadUi();

export const TABS = ["node", "changes", "artifact", "files", "docs", "usage", "events", "tools"];

/** Global application state (the run being watched lives in `live`). */
export const app = $state({
  booted: false,
  authRequired: false,
  caps: null,
  runs: [],
  runsLoaded: false,
  models: { by_backend: {}, default_by_backend: {} },
  openRuns: Array.isArray(saved.openRuns) ? saved.openRuns : [],
  activeRun: saved.activeRun || null,
  views: saved.views && typeof saved.views === "object" ? saved.views : {},
  theme: saved.theme || "",
  railOpen: saved.railOpen ?? true,
  inspectorOpen: saved.inspectorOpen ?? true,
  inspectorW: saved.inspectorW || 420,
  railW: saved.railW || 252,
  scoreH: saved.scoreH || 280,
  diffSplit: !!saved.diffSplit,
  palette: false,
  newRun: false,
  toasts: [],
  sse: "connecting",
  narrow: false,
  focusJob: null,
});

export function persist() {
  saveUi({
    openRuns: app.openRuns,
    activeRun: app.activeRun,
    views: app.views,
    theme: app.theme,
    railOpen: app.railOpen,
    inspectorOpen: app.inspectorOpen,
    inspectorW: app.inspectorW,
    railW: app.railW,
    scoreH: app.scoreH,
    diffSplit: app.diffSplit,
  });
}

export function applyTheme() {
  const el = document.documentElement;
  if (app.theme === "light" || app.theme === "dark") el.dataset.theme = app.theme;
  else delete el.dataset.theme;
  try {
    if (app.theme) localStorage.setItem("kd.theme", app.theme);
    else localStorage.removeItem("kd.theme");
  } catch {
    /* ignore */
  }
}

export function toggleTheme() {
  const dark = (app.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")) === "dark";
  app.theme = dark ? "light" : "dark";
  applyTheme();
  persist();
}

let toastId = 0;
export function toast(message, kind = "info", ms = 3600) {
  const id = ++toastId;
  app.toasts = [...app.toasts.slice(-3), { id, message: String(message), kind }];
  if (ms) setTimeout(() => dismissToast(id), ms);
}
export function dismissToast(id) {
  app.toasts = app.toasts.filter((t) => t.id !== id);
}

/** Per-run view state (selection, tabs, layout), persisted. */
export function viewOf(runId) {
  if (!runId) return { node: null, pin: null, tab: "node", center: "both", collapsed: [] };
  if (!app.views[runId]) app.views[runId] = { node: null, pin: null, tab: "node", center: "both", collapsed: [] };
  return app.views[runId];
}

/* ------------------------------------------------------- structural sharing */
const sigCache = new WeakMap();
function sig(o) {
  let s = sigCache.get(o);
  if (s === undefined) {
    s = JSON.stringify(o);
    sigCache.set(o, s);
  }
  return s;
}
/** Keep previous object identities for unchanged rows so each-blocks stay put. */
export function share(prev, next, key = "id") {
  if (!Array.isArray(next)) return next;
  if (!Array.isArray(prev)) return next;
  const by = new Map(prev.map((o) => [o && o[key], o]));
  let same = prev.length === next.length;
  const out = next.map((o, i) => {
    const p = by.get(o && o[key]);
    if (p !== undefined && sig(p) === JSON.stringify(o)) {
      if (prev[i] !== p) same = false;
      return p;
    }
    same = false;
    return o;
  });
  return same ? prev : out;
}

const META_KEYS = [
  "attached", "run_id", "goal", "backend", "phase", "phase_status", "phase_detail", "stalled", "stalled_reason", "tier",
  "measured_tier", "tier_override", "escalation_history", "phases", "tree_counts", "hosted_count", "halted", "hosted",
  "has_spec", "has_contract", "has_assembly", "assembly_path", "model_override", "backend_override",
  "total_tokens", "events_count", "control_enabled", "max_concurrent_runs", "default_model", "default_provider",
];

/* ------------------------------------------------------------------ RunLive */
export class RunLive {
  id;
  meta = $state.raw({});
  tree = $state.raw([]);
  subagents = $state.raw([]);
  approvals = $state.raw([]);
  jobs = $state.raw([]);
  events = $state.raw([]);
  feeds = $state.raw({});
  detail = $state.raw(null);
  elapsedBase = $state.raw({ elapsed: 0, at: 0 });
  now = $state(Date.now() / 1000);
  connected = $state(false);

  #es = null;
  #poll = null;
  #retry = null;
  #clock = null;
  #stopped = false;
  #busy = new Set();
  #watch = new Map();
  #eventsBusy = false;
  #detailFor = null;
  #detailSig = "";
  #lastSnapshotAt = 0;

  constructor(id) {
    this.id = id;
  }

  start() {
    this.#clock = setInterval(() => (this.now = Date.now() / 1000), 1000);
    this.#connect();
  }

  stop() {
    this.#stopped = true;
    clearInterval(this.#clock);
    clearInterval(this.#poll);
    clearTimeout(this.#retry);
    if (this.#es) this.#es.close();
  }

  /* -- transport ---------------------------------------------------------- */
  #connect() {
    if (this.#stopped) return;
    if (!this.id) return this.#startPolling();
    try {
      const es = new EventSource(url("/api/stream", this.id));
      this.#es = es;
      es.addEventListener("snapshot", (ev) => {
        this.connected = true;
        app.sse = "live";
        clearInterval(this.#poll);
        this.#poll = null;
        try {
          this.#onSnapshot(JSON.parse(ev.data));
        } catch (err) {
          console.error("snapshot", err);
        }
      });
      es.onerror = () => {
        es.close();
        this.connected = false;
        app.sse = "poll";
        this.#startPolling();
        clearTimeout(this.#retry);
        this.#retry = setTimeout(() => this.#connect(), 5000);
      };
    } catch {
      this.#startPolling();
    }
  }

  #startPolling() {
    if (this.#poll) return;
    const tick = async () => {
      try {
        this.#onSnapshot(await get("/api/snapshot", { run: this.id || undefined }));
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) app.sse = "poll";
      }
    };
    tick();
    this.#poll = setInterval(tick, this.id ? 2000 : 3000);
  }

  async refresh() {
    try {
      this.#onSnapshot(await get("/api/snapshot", { run: this.id || undefined }));
    } catch {
      /* the stream will catch up */
    }
  }

  /* -- snapshot ----------------------------------------------------------- */
  #onSnapshot(snap) {
    this.#lastSnapshotAt = Date.now();
    app.runsLoaded = true;
    app.runs = share(app.runs, snap.runs || []);
    if (snap.models_by_backend) {
      app.models = { by_backend: snap.models_by_backend, default_by_backend: snap.default_model_by_backend || {}, all: snap.models || [], default: snap.default_model || "" };
    }
    if (!this.id) return;
    const meta = {};
    for (const k of META_KEYS) meta[k] = snap[k];
    if (JSON.stringify(meta) !== JSON.stringify(this.meta)) this.meta = meta;
    this.elapsedBase = { elapsed: snap.elapsed || 0, at: Date.now() / 1000 };
    this.tree = share(this.tree, snap.tree || []);
    this.subagents = share(this.subagents, snap.subagents || []);
    this.approvals = share(this.approvals, snap.approvals || [], "approval_id");
    this.jobs = share(this.jobs, snap.jobs || [], "job_id");
    this.#syncEvents(snap.events_count || 0);
    this.#pollFeeds();
    this.#syncDetail();
  }

  async #syncEvents(count) {
    if (this.#eventsBusy || count === this.events.length) return;
    if (count < this.events.length) this.events = [];
    this.#eventsBusy = true;
    try {
      const r = await get("/api/events", { run: this.id, params: { after: this.events.length } });
      if (r && r.events && r.events.length) this.events = this.events.concat(r.events);
    } catch {
      /* retried next tick */
    } finally {
      this.#eventsBusy = false;
    }
  }

  /* -- derived helpers ---------------------------------------------------- */
  get elapsed() {
    const b = this.elapsedBase;
    if (!b.at) return 0;
    const running = this.running;
    return running ? b.elapsed + (this.now - b.at) : b.elapsed;
  }
  get running() {
    const m = this.meta;
    return !!(m.hosted || m.phase_status === "in_progress") && !m.stalled && !m.halted;
  }
  get pending() {
    return this.approvals.filter((a) => a.status === "pending");
  }
  agent(id) {
    return this.subagents.find((s) => s.id === id) || null;
  }
  isLive(id) {
    return !!this.agent(id)?.live;
  }
  /** Agent ids that belong to a node (its own, plus ~repair~/~research~ derivatives). */
  agentsOf(nodeId) {
    return this.subagents.filter((s) => s.id === nodeId || s.id.startsWith(nodeId + "~"));
  }
  node(id) {
    return this.tree.find((n) => n.id === id) || null;
  }

  /* -- feeds -------------------------------------------------------------- */
  /** Register interest in an agent's trace; returns a release function. */
  watch(id) {
    this.#watch.set(id, (this.#watch.get(id) || 0) + 1);
    this.fetchFeed(id);
    return () => {
      const n = (this.#watch.get(id) || 1) - 1;
      if (n <= 0) this.#watch.delete(id);
      else this.#watch.set(id, n);
    };
  }

  #pollFeeds() {
    for (const id of this.#watch.keys()) {
      const f = this.feeds[id];
      const agent = this.agent(id);
      const stale = !f || !f.loaded || (agent && agent.live) || (agent && f.mtime !== agent.mtime);
      if (stale) this.fetchFeed(id);
    }
  }

  async fetchFeed(id) {
    if (this.#busy.has(id)) return;
    this.#busy.add(id);
    try {
      const f = this.feeds[id];
      const since = f && f.loaded ? f.next : 0;
      const r = await get(`/api/node/${encodeURIComponent(id)}/thinking`, { run: this.id, params: { since } });
      const incoming = r.entries || [];
      let entries;
      if (!f || !f.loaded || r.reset) entries = incoming;
      else if (!incoming.length) entries = f.entries;
      else {
        const first = incoming[0].ts;
        entries = f.entries.filter((e) => e.ts < first).concat(incoming);
      }
      const agent = this.agent(id);
      const changed = !f || entries !== f.entries || r.total !== f.total;
      if (changed || !f.loaded) {
        this.feeds = { ...this.feeds, [id]: { entries, next: r.next, total: r.total, truncated: r.truncated, loaded: true, mtime: agent?.mtime ?? null } };
      } else if (agent && f.mtime !== agent.mtime) {
        this.feeds = { ...this.feeds, [id]: { ...f, mtime: agent.mtime } };
      }
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 404) console.warn("feed", id, err);
      if (!this.feeds[id]) this.feeds = { ...this.feeds, [id]: { entries: [], next: 0, total: 0, loaded: true, mtime: null } };
    } finally {
      this.#busy.delete(id);
    }
  }

  /* -- node detail -------------------------------------------------------- */
  selectDetail(id) {
    this.#detailFor = id;
    this.#detailSig = "";
    if (!id) this.detail = null;
    else this.#syncDetail(true);
  }

  async #syncDetail(force = false) {
    const id = this.#detailFor;
    if (!id) return;
    const row = this.node(id);
    const agent = this.agent(id);
    const s = JSON.stringify([row && [row.status, row.attempts, row.artifact_tokens, row.gate_results], agent && [agent.status, agent.live, agent.mtime]]);
    if (!force && s === this.#detailSig && this.detail?.id === id) return;
    this.#detailSig = s;
    try {
      const d = await get(`/api/node/${encodeURIComponent(id)}`, { run: this.id });
      if (this.#detailFor === id) this.detail = d;
    } catch (err) {
      if (this.#detailFor === id) this.detail = err instanceof ApiError && err.status === 404 ? { id, missing: true } : this.detail;
    }
  }

  /* -- actions ------------------------------------------------------------ */
  async #act(label, fn, okMessage) {
    try {
      const r = await fn();
      if (okMessage) toast(okMessage);
      this.refresh();
      return r ?? true;
    } catch (err) {
      toast(`${label}: ${err.message}`, "error", 6000);
      return false;
    }
  }

  halt(value = true) {
    return this.#act("pause", () => post("/api/halt", { value }, { run: this.id }), value ? "paused" : "resumed");
  }
  async resumeDriver() {
    try {
      await post("/api/runs", { run_id: this.id });
      toast("resume requested");
      this.refresh();
    } catch (err) {
      if (String(err.message).includes("already running")) {
        await this.halt(false);
      } else toast(`resume: ${err.message}`, "error", 6000);
    }
  }
  kill() {
    return this.#act("kill", () => post("/api/runs/kill", { run_id: this.id }), "killed");
  }
  escalate() {
    return this.#act("escalate", () => post("/api/escalate", {}, { run: this.id }), "tier raised");
  }
  setTier(tier) {
    return this.#act("tier", () => post("/api/command", { command: `/tier ${tier}` }, { run: this.id }), `tier ${tier}`);
  }
  setModel(model) {
    return this.#act("model", () => post("/api/model/override", { model: model || null }, { run: this.id }), model ? `model ${model}` : "model default");
  }
  setBackend(backend) {
    return this.#act("backend", () => post("/api/backend", { backend: backend || null }, { run: this.id }), backend ? `backend ${backend}` : "backend default");
  }
  amend(text, reason = "web amendment") {
    return this.#act("amend", () => post("/api/amend", { text, reason }, { run: this.id }), "amendment queued");
  }
  reopen(nodeId, defect) {
    return this.#act("reopen", () => post("/api/reopen", { node_id: nodeId, defect, is_manual: true }, { run: this.id }), "reopen queued");
  }
  redispatch(nodeId, reason = "", mode = "auto") {
    return this.#act("redispatch", () => post(`/api/node/${encodeURIComponent(nodeId)}/redispatch`, { reason, mode }, { run: this.id }), "redispatch queued");
  }
  bypass(nodeId, processName = "") {
    return this.#act("bypass", () => post(`/api/node/${encodeURIComponent(nodeId)}/bypass`, { process: processName }, { run: this.id }), "bypassed");
  }
  interject(nodeId, text) {
    return this.#act("send", () => post(`/api/node/${encodeURIComponent(nodeId)}/interject`, { text, content: text }, { run: this.id }), "sent");
  }
  resolveApproval(a, payload, okMessage = "resolved") {
    return this.#act("approve", async () => {
      await post(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, payload, { run: this.id });
      if (a.kind === "redispatch" && payload.action === "apply" && !this.meta.hosted) await this.resumeDriver();
    }, okMessage);
  }
  pilotSave(a, nodeId, text) {
    return this.#act("pilot", () => post(`/api/approvals/${encodeURIComponent(a.approval_id)}/pilot-save`, { node_id: nodeId, text }, { run: this.id }), "pilot saved");
  }
  cancelJob(jobId) {
    return this.#act("cancel", () => post(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {}, { run: this.id }), "cancelled");
  }
}

/* ------------------------------------------------------------- singletons */
export const live = $state({ current: null });

function mount(runId) {
  if (live.current && live.current.id === runId) return;
  if (live.current) live.current.stop();
  const next = new RunLive(runId);
  live.current = next;
  next.start();
}

export function openRun(runId) {
  if (!runId) return;
  if (!app.openRuns.includes(runId)) app.openRuns = [...app.openRuns, runId];
  app.activeRun = runId;
  mount(runId);
  live.current?.selectDetail(viewOf(runId).node);
  persist();
}

export function closeRun(runId) {
  app.openRuns = app.openRuns.filter((r) => r !== runId);
  if (app.activeRun === runId) {
    const i = app.openRuns.length ? app.openRuns[app.openRuns.length - 1] : null;
    app.activeRun = i;
    mount(i);
    if (i) live.current?.selectDetail(viewOf(i).node);
  }
  persist();
}

export function cycleRun(delta) {
  const list = app.openRuns;
  if (list.length < 2) return;
  const i = list.indexOf(app.activeRun);
  openRun(list[(i + delta + list.length) % list.length]);
}

export function selectNode(nodeId) {
  const v = viewOf(app.activeRun);
  v.node = nodeId;
  if (live.current) live.current.selectDetail(nodeId);
  persist();
}

export async function startRun(body) {
  const r = await post("/api/runs", body);
  if (r && r.run_id) {
    openRun(r.run_id);
    return r.run_id;
  }
  return null;
}

export async function deleteRun(runId) {
  await del(`/api/runs/${encodeURIComponent(runId)}`);
  app.openRuns = app.openRuns.filter((r) => r !== runId);
  delete app.views[runId];
  if (app.activeRun === runId) {
    app.activeRun = app.openRuns[app.openRuns.length - 1] || null;
    mount(app.activeRun);
  }
  persist();
  toast("run deleted");
}

/* ------------------------------------------------------------------- boot */
export async function boot() {
  onAuthRequired(() => (app.authRequired = true));
  applyTheme();
  const narrow = matchMedia("(max-width: 980px)");
  const sync = () => (app.narrow = narrow.matches);
  sync();
  if (app.narrow && !saved.narrowSeen) {
    app.railOpen = false;
    app.inspectorOpen = false;
  }
  narrow.addEventListener("change", sync);
  try {
    app.caps = await get("/api/capabilities");
  } catch {
    /* auth overlay handles 401 */
  }
  const valid = new Set();
  mount(app.activeRun && app.openRuns.includes(app.activeRun) ? app.activeRun : null);
  // validate persisted tabs once the first run list arrives
  const unwatch = setInterval(() => {
    if (!app.runsLoaded) return;
    clearInterval(unwatch);
    for (const r of app.runs) valid.add(r.id);
    app.openRuns = app.openRuns.filter((r) => valid.has(r));
    if (app.activeRun && !valid.has(app.activeRun)) app.activeRun = app.openRuns[0] || null;
    if (app.activeRun) mount(app.activeRun);
    if (live.current && app.activeRun) live.current.selectDetail(viewOf(app.activeRun).node);
  }, 150);
  app.booted = true;
}

export async function submitToken(token) {
  setToken(token);
  try {
    await post("/api/attach", { run_id: app.activeRun || app.runs[0]?.id || "" });
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      toast("token rejected", "error");
      return false;
    }
  }
  app.authRequired = false;
  location.reload();
  return true;
}

export const hasToken = () => !!getToken();
