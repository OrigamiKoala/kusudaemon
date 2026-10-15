// One registry drives the palette (Cmd-K) and the composer's /commands.
import { app, closeRun, deleteRun, live, openRun, persist, selectNode, toast, toggleTheme, viewOf, TABS } from "./store.svelte.js";
import { post } from "./api.js";
import { fuzzy, missingParams, parseArgs } from "./cmdparse.js";

export { fuzzy, missingParams, parseArgs };

const q = (s) => `"${String(s).replace(/"/g, '\\"')}"`;

export const ctx = () => {
  const run = live.current;
  const view = viewOf(app.activeRun);
  return { run, view, runId: app.activeRun, nodes: run?.tree ?? [], backends: app.caps?.backends ?? [], models: [] };
};

function liveAgent(c) {
  const sel = c.view.node && c.run.agentsOf(c.view.node).find((a) => a.live);
  return (sel && sel.id) || c.run.subagents.find((a) => a.live)?.id || null;
}

export function setTab(tab) {
  const v = viewOf(app.activeRun);
  v.tab = tab;
  app.inspectorOpen = true;
  persist();
}

async function job(command, args, c) {
  const r = await post("/api/cli", { command, args, run_id: c.runId || "" });
  setTab("tools");
  app.focusJob = r.id;
  toast(`${command} started`);
}

/**
 * params: [{ key, label, type: text|select|node|number, options?, optional? }]
 * cli(args, c) -> equivalent shell command (shown, copyable) or null.
 */
export const COMMANDS = [
  { id: "new", title: "new run", alias: ["new"], group: "run", run: () => (app.newRun = true) },
  {
    id: "resume", title: "restart driver", alias: ["resume"], group: "run", needsRun: true,
    cli: (a, c) => `kusudaemon resume ${c.runId}`, run: (a, c) => c.run.resumeDriver(),
  },
  { id: "pause", title: "pause", alias: ["pause", "halt"], group: "run", needsRun: true, cli: (a, c) => `kusudaemon pause ${c.runId}`, run: (a, c) => c.run.halt(true) },
  { id: "unpause", title: "continue", alias: ["continue", "unpause"], group: "run", needsRun: true, run: (a, c) => c.run.halt(false) },
  { id: "kill", title: "kill", alias: ["kill"], group: "run", needsRun: true, danger: true, cli: (a, c) => `kusudaemon kill ${c.runId}`, run: (a, c) => c.run.kill() },
  { id: "escalate", title: "escalate tier", alias: ["escalate", "esc"], group: "run", needsRun: true, cli: (a, c) => `kusudaemon escalate ${c.runId}`, run: (a, c) => c.run.escalate() },
  {
    id: "tier", title: "set tier", alias: ["tier"], group: "run", needsRun: true,
    params: [{ key: "tier", label: "tier", type: "select", options: ["T0", "T1", "T2", "T3"] }],
    cli: (a, c) => `kusudaemon tier ${c.runId} ${a.tier}`, run: (a, c) => c.run.setTier(a.tier),
  },
  {
    id: "model", title: "set model", alias: ["model"], group: "run", needsRun: true,
    params: [{ key: "model", label: "model", type: "model", optional: true }],
    cli: (a, c) => `kusudaemon model ${c.runId} ${a.model || "default"}`, run: (a, c) => c.run.setModel(a.model === "default" ? "" : a.model),
  },
  {
    id: "backend", title: "set backend", alias: ["backend"], group: "run", needsRun: true,
    params: [{ key: "backend", label: "backend", type: "select", options: (c) => ["default", ...c.backends] }],
    cli: (a, c) => `kusudaemon backend ${c.runId} ${a.backend}`, run: (a, c) => c.run.setBackend(a.backend === "default" ? "" : a.backend),
  },
  {
    id: "amend", title: "amend contract", alias: ["amend"], group: "run", needsRun: true,
    params: [{ key: "text", label: "rule", type: "text" }],
    cli: (a, c) => `kusudaemon amend ${c.runId} --text ${q(a.text)}`, run: (a, c) => c.run.amend(a.text),
  },
  {
    id: "reopen", title: "reopen node", alias: ["reopen"], group: "node", needsRun: true,
    params: [{ key: "node", label: "node", type: "node" }, { key: "defect", label: "defect", type: "text", optional: true }],
    cli: (a, c) => `kusudaemon reopen ${c.runId} ${a.node} --defect ${q(a.defect || "")}`, run: (a, c) => c.run.reopen(a.node, a.defect || ""),
  },
  {
    id: "redispatch", title: "redispatch node", alias: ["redispatch"], group: "node", needsRun: true,
    params: [{ key: "node", label: "node", type: "node" }, { key: "reason", label: "reason", type: "text", optional: true }],
    run: (a, c) => c.run.redispatch(a.node, a.reason || ""),
  },
  {
    id: "bypass", title: "bypass node process", alias: ["bypass", "cancel"], group: "node", needsRun: true,
    params: [{ key: "node", label: "node", type: "node" }, { key: "process", label: "process", type: "text", optional: true }],
    cli: (a, c) => `kusudaemon bypass ${c.runId} ${a.node}${a.process ? " --process " + a.process : ""}`, run: (a, c) => c.run.bypass(a.node, a.process || ""),
  },
  {
    id: "interject", title: "message agent", alias: ["say", "interject"], group: "node", needsRun: true,
    params: [{ key: "text", label: "message", type: "text" }],
    run: (a, c) => {
      const id = liveAgent(c);
      if (!id) return toast("no live agent", "error");
      return c.run.interject(id, a.text);
    },
  },
  {
    id: "approve", title: "approve oldest pending", alias: ["approve"], group: "run", needsRun: true,
    cli: (a, c) => `kusudaemon approve ${c.runId}`,
    run: (a, c) => {
      const p = c.run.pending[0];
      if (!p) return toast("nothing pending");
      const primary = (p.options || []).find((o) => o.style === "primary") || (p.options || [])[0];
      return c.run.resolveApproval(p, primary ? { action: primary.value } : { action: "answer", user_input: "" });
    },
  },
  { id: "delete", title: "delete run", alias: ["delete"], group: "run", needsRun: true, danger: true, run: (a, c) => deleteRun(c.runId) },
  { id: "close", title: "close tab", alias: ["close"], group: "view", needsRun: true, run: (a, c) => closeRun(c.runId) },

  // jobs: CLI verbs without a dedicated dashboard route
  { id: "status", title: "status brief", alias: ["status"], group: "cli", needsRun: true, cli: (a, c) => `kusudaemon status ${c.runId} --brief`, run: (a, c) => job("status", { brief: true }, c) },
  {
    id: "resync", title: "resync source", alias: ["resync"], group: "cli", needsRun: true,
    params: [{ key: "source", label: "source path", type: "text" }],
    cli: (a, c) => `kusudaemon resync ${c.runId} --source ${a.source}`, run: (a, c) => job("resync", { source: a.source }, c),
  },
  { id: "cache-clear", title: "clear episode cache", alias: ["cache"], group: "cli", danger: true, cli: () => "kusudaemon cache clear", run: (a, c) => job("cache-clear", {}, c) },
  {
    id: "eval", title: "run eval", alias: ["eval"], group: "cli",
    params: [{ key: "task", label: "task", type: "text", optional: true }, { key: "runs", label: "runs", type: "number", optional: true }],
    cli: (a) => `kusudaemon eval${a.task ? " --task " + a.task : ""}${a.runs ? " --runs " + a.runs : ""}`, run: (a, c) => job("eval", a, c),
  },

  // view
  ...TABS.map((t) => ({ id: `tab-${t}`, title: t, alias: [t], group: "view", needsRun: true, run: () => setTab(t) })),
  { id: "view-both", title: "score + feed", alias: ["both"], group: "view", needsRun: true, run: (a, c) => ((c.view.center = "both"), persist()) },
  { id: "view-score", title: "score only", alias: ["score"], group: "view", needsRun: true, run: (a, c) => ((c.view.center = "score"), persist()) },
  { id: "view-feed", title: "feed only", alias: ["feed"], group: "view", needsRun: true, run: (a, c) => ((c.view.center = "feed"), persist()) },
  { id: "rail", title: "toggle rail", alias: ["rail"], group: "view", run: () => ((app.railOpen = !app.railOpen), persist()) },
  { id: "inspector", title: "toggle inspector", alias: ["inspector"], group: "view", run: () => ((app.inspectorOpen = !app.inspectorOpen), persist()) },
  { id: "theme", title: "toggle theme", alias: ["theme"], group: "view", run: () => toggleTheme() },
];

export const byAlias = (word) => COMMANDS.find((c) => c.alias.includes(word.toLowerCase()));

export function optionsOf(param, c) {
  if (param.type === "node") return c.nodes.map((n) => n.id);
  if (param.type === "model") return ["default", ...(app.models.by_backend?.[c.run?.meta?.backend_override || c.run?.meta?.backend] || [])];
  if (typeof param.options === "function") return param.options(c);
  return param.options || [];
}

export async function execute(cmd, args) {
  const c = ctx();
  if (cmd.needsRun && !c.run?.meta?.attached) {
    toast("open a run first", "error");
    return false;
  }
  try {
    await cmd.run(args || {}, c);
    return true;
  } catch (err) {
    toast(`${cmd.title}: ${err.message}`, "error", 6000);
    return false;
  }
}

export { openRun, selectNode };
