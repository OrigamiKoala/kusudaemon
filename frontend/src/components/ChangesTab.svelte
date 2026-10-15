<script>
  import { GitBranch, RefreshCw, GitCommitHorizontal, Pencil, ChevronDown, ChevronRight, Columns2, Rows2 } from "@lucide/svelte";
  import Diff from "./Diff.svelte";
  import { app, live, persist, viewOf } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { poll } from "../lib/poll.js";
  import { buildFeed, toolDiffFiles } from "../lib/trace.js";
  import { parseUnified } from "../lib/diff.js";
  import { fmtStamp } from "../lib/format.js";
  import { setTab } from "../lib/commands.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  let mode = $state("git"); // git | edits
  let status = $state(null);
  let log = $state([]);
  let open = $state({}); // path -> files[]|null
  let commitOpen = $state({});
  let loading = $state(false);

  async function load() {
    if (!run || !app.activeRun) return;
    loading = true;
    try {
      status = await get("/api/git/status", { run: app.activeRun });
      if (status.repo) log = (await get("/api/git/log", { run: app.activeRun, params: { limit: 15 } })).commits;
    } finally {
      loading = false;
    }
  }
  $effect(() => {
    if (mode !== "git" || !app.activeRun) return;
    status = null;
    open = {};
    return poll(load, run?.running ? 5000 : 30000);
  });

  async function toggle(f) {
    if (open[f.path] !== undefined) {
      const { [f.path]: _, ...rest } = open;
      open = rest;
      return;
    }
    open = { ...open, [f.path]: null };
    const parts = [];
    for (const staged of [false, true]) {
      const r = await get("/api/git/diff", { run: app.activeRun, params: { path: f.path, staged: staged ? 1 : "" } });
      parts.push(...(r.files || []));
    }
    if (!parts.length) {
      open = { ...open, [f.path]: [] };
      return;
    }
    const merged = { ...parts[0], hunks: parts.flatMap((p) => p.hunks), added: parts.reduce((a, p) => a + p.added, 0), deleted: parts.reduce((a, p) => a + p.deleted, 0) };
    open = { ...open, [f.path]: [merged] };
  }
  async function toggleCommit(c) {
    if (commitOpen[c.sha]) {
      const { [c.sha]: _, ...rest } = commitOpen;
      commitOpen = rest;
      return;
    }
    commitOpen = { ...commitOpen, [c.sha]: [] };
    const r = await get("/api/git/diff", { run: app.activeRun, params: { ref: c.sha } });
    commitOpen = { ...commitOpen, [c.sha]: r.files || [] };
  }

  // ---- tool edits of the selected node (or of every loaded agent)
  const agentKey = $derived(view.node ? (run?.agentsOf(view.node).map((a) => a.id) ?? []).join("|") : "");
  const agentIds = $derived(agentKey ? agentKey.split("|") : []);
  $effect(() => {
    if (mode !== "edits" || !run) return;
    const rel = agentKey.split("|").filter(Boolean).map((id) => run.watch(id));
    return () => rel.forEach((r) => r());
  });
  const edits = $derived.by(() => {
    if (mode !== "edits" || !run) return [];
    const byPath = new Map();
    for (const id of agentIds) {
      const f = run.feeds[id];
      if (!f) continue;
      for (const it of buildFeed(f.entries)) {
        if (it.kind !== "tool" || !it.done || it.error) continue;
        const files = toolDiffFiles(it.name, it.input) || (it.serverDiff ? parseUnified(it.serverDiff) : null);
        if (!files) continue;
        for (const file of files) {
          const rec = byPath.get(file.path) || { path: file.path, files: [], added: 0, deleted: 0 };
          rec.files.push(file);
          rec.added += file.added;
          rec.deleted += file.deleted;
          byPath.set(file.path, rec);
        }
      }
    }
    return [...byPath.values()];
  });
  let editOpen = $state({});

  const code = (f) => (f.untracked ? "?" : (f.index || " ") + (f.worktree || " ")).trim() || " ";
</script>

<div class="tab">
  <div class="seg">
    <button class="icon-btn" aria-label="workspace" aria-pressed={mode === "git"} onclick={() => (mode = "git")}><GitBranch size={14} /></button>
    <button class="icon-btn" aria-label="tool edits" aria-pressed={mode === "edits"} onclick={() => (mode = "edits")}><Pencil size={14} /></button>
    <span class="grow"></span>
    <button class="icon-btn" aria-label={app.diffSplit ? "unified" : "split"} aria-pressed={app.diffSplit} onclick={() => ((app.diffSplit = !app.diffSplit), persist())}>
      {#if app.diffSplit}<Columns2 size={14} />{:else}<Rows2 size={14} />{/if}
    </button>
    {#if mode === "git"}<button class="icon-btn" aria-label="refresh" onclick={load}><RefreshCw size={13} class={loading ? "spin" : ""} /></button>{/if}
  </div>

  {#if mode === "git"}
    {#if status && !status.repo}
      <div class="none dim"><GitBranch size={16} /></div>
    {:else if status}
      <div class="meta">
        <span class="chip mono"><GitBranch size={11} />{status.branch}</span>
        <span class="mono dim">{status.head}</span>
        {#if status.ahead}<span class="num dim">↑{status.ahead}</span>{/if}
        {#if status.behind}<span class="num dim">↓{status.behind}</span>{/if}
        <span class="grow"></span>
        <span class="num dim">{status.files.length}</span>
      </div>
      <div class="files">
        {#each status.files as f (f.path)}
          <div class="f">
            <button class="fr" aria-expanded={open[f.path] !== undefined} onclick={() => toggle(f)}>
              {#if open[f.path] !== undefined}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
              <span class="code mono {f.untracked ? 'u' : f.index ? 's' : 'm'}">{code(f)}</span>
              <span class="mono trunc p">{f.path}</span>
              <span class="add num">{f.added ? "+" + f.added : ""}</span><span class="del num">{f.deleted ? "−" + f.deleted : ""}</span>
            </button>
            {#if open[f.path]}<div class="d"><Diff files={open[f.path]} compact split={app.diffSplit} /></div>{/if}
          </div>
        {/each}
      </div>
      {#if log.length}
        <div class="sub"><GitCommitHorizontal size={13} /></div>
        {#each log as c (c.sha)}
          <div class="f">
            <button class="fr" onclick={() => toggleCommit(c)}>
              {#if commitOpen[c.sha]}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
              <span class="mono dim">{c.short}</span>
              <span class="trunc p">{c.subject}</span>
              <span class="num dim">{fmtStamp(c.ts)}</span>
            </button>
            {#if commitOpen[c.sha]?.length}<div class="d"><Diff files={commitOpen[c.sha]} compact collapseAll split={app.diffSplit} /></div>{/if}
          </div>
        {/each}
      {/if}
    {/if}
  {:else}
    {#if !view.node}
      <div class="none dim"><Pencil size={16} /></div>
    {:else if !edits.length}
      <div class="none dim"><Pencil size={16} /></div>
    {:else}
      {#each edits as e (e.path)}
        <div class="f">
          <button class="fr" onclick={() => (editOpen[e.path] = !editOpen[e.path])}>
            {#if editOpen[e.path]}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
            <span class="mono trunc p">{e.path}</span>
            <span class="num dim">×{e.files.length}</span>
            <span class="add num">+{e.added}</span><span class="del num">−{e.deleted}</span>
          </button>
          {#if editOpen[e.path]}<div class="d"><Diff files={e.files} compact split={app.diffSplit} /></div>{/if}
        </div>
      {/each}
    {/if}
  {/if}
</div>

<style>
  .tab {
    padding: var(--sp-2) 0 var(--sp-4);
  }
  .seg {
    display: flex;
    gap: 2px;
    padding: 0 var(--sp-3) var(--sp-2);
  }
  .meta {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px var(--sp-4) var(--sp-3);
    font-size: var(--fs-sm);
  }
  .none {
    display: grid;
    place-items: center;
    height: 90px;
  }
  .fr {
    display: flex;
    align-items: center;
    gap: 7px;
    width: 100%;
    height: 26px;
    padding: 0 var(--sp-4) 0 var(--sp-3);
    text-align: left;
    font-size: var(--fs-sm);
    color: var(--fg-1);
  }
  .fr:hover {
    background: var(--bg-2);
  }
  .p {
    flex: 1;
  }
  .code {
    width: 18px;
    text-align: center;
    font-size: var(--fs-xs);
  }
  .code.m { color: var(--warn); }
  .code.s { color: var(--ok); }
  .code.u { color: var(--wait); }
  .add { color: var(--add-fg); font-size: var(--fs-xs); }
  .del { color: var(--del-fg); font-size: var(--fs-xs); }
  .d {
    padding: 4px var(--sp-3) var(--sp-2);
  }
  .sub {
    padding: var(--sp-3) var(--sp-4) 4px;
    color: var(--fg-2);
    border-top: 1px solid var(--line-soft);
    margin-top: var(--sp-3);
  }
</style>
