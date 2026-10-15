<script>
  import Out from "./Out.svelte";
  import { app, live } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { poll } from "../lib/poll.js";
  import { renderMarkdown } from "../lib/markdown.js";
  import { fmtTokens } from "../lib/format.js";

  const run = $derived(live.current);
  const m = $derived(run?.meta ?? {});
  const DOCS = ["spec", "contract", "spine", "manifest", "assembly"];
  let doc = $state("spec");
  let data = $state({});
  let part = $state("output");

  $effect(() => {
    const d = doc;
    if (!app.activeRun) return;
    return poll(async () => {
      const r = await get(`/api/${d}`, { run: app.activeRun });
      data = { ...data, [d]: r };
    }, run?.running ? 6000 : 60000);
  });

  const spine = $derived.by(() => {
    const t = data.spine?.text;
    if (!t) return null;
    try {
      const j = JSON.parse(t);
      return Array.isArray(j) ? j : null;
    } catch {
      return null;
    }
  });
  const maxTok = $derived(spine ? Math.max(1, ...spine.map((u) => u.tokens || 0)) : 1);
  const asm = $derived(data.assembly || {});
  const checks = $derived(asm.checks && typeof asm.checks === "object" ? Object.entries(asm.checks) : []);
</script>

<div class="bar" role="tablist">
  {#each DOCS as t}
    <button role="tab" aria-selected={doc === t} class:on={doc === t} onclick={() => (doc = t)}>{t}</button>
  {/each}
</div>

<div class="body">
  {#if doc === "spec"}
    <div class="prose">{@html renderMarkdown(data.spec?.text || "")}</div>
  {:else if doc === "contract"}
    {#if data.contract}
      <div class="ceil">
        <div class="track"><i style:width="{Math.min(100, (data.contract.tokens / data.contract.ceiling) * 100)}%" class:over={data.contract.tokens > data.contract.ceiling}></i></div>
        <span class="num dim">{data.contract.tokens}/{data.contract.ceiling}</span>
      </div>
      <div class="prose">{@html renderMarkdown(data.contract.text)}</div>
    {/if}
  {:else if doc === "spine"}
    {#if spine}
      <div class="tbl">
        {#each spine as u, i (u.id ?? i)}
          <div class="r">
            <span class="mono id">{u.id}</span>
            <span class="trunc grow">{u.label}</span>
            <span class="bar2"><i style:width="{((u.tokens || 0) / maxTok) * 100}%"></i></span>
            <span class="num dim w">{fmtTokens(u.tokens || 0)}</span>
          </div>
        {/each}
      </div>
    {:else if data.spine?.text}
      <Out text={data.spine.text} lang="json" cap={200} />
    {/if}
  {:else if doc === "manifest"}
    <div class="tbl">
      {#each data.manifest?.lines || [] as l, i (i)}
        <div class="r mf">
          <span class="mono id">{l.node}</span>
          <span class="mono dim trunc grow">{Object.entries(l).filter(([k]) => k !== "node").map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ")}</span>
        </div>
      {/each}
    </div>
  {:else}
    <div class="sub" role="tablist">
      {#each ["output", "index", "checks", "compile_log"] as p}
        <button role="tab" aria-selected={part === p} class:on={part === p} onclick={() => (part = p)}>{p}</button>
      {/each}
    </div>
    {#if part === "output" || part === "index"}
      <div class="prose">{@html renderMarkdown(asm[part] || "")}</div>
    {:else if part === "checks"}
      {#each checks as [k, v]}
        <div class="r"><span class="dot {v === true || v?.passed ? 'passed' : v === false || v?.passed === false ? 'failed' : 'pending'}"></span><span class="mono">{k}</span><span class="dim trunc grow">{typeof v === "object" ? JSON.stringify(v) : ""}</span></div>
      {/each}
    {:else}
      <Out text={asm.compile_log || ""} cap={300} />
    {/if}
  {/if}
</div>

<style>
  .bar,
  .sub {
    display: flex;
    gap: 2px;
    padding: var(--sp-2) var(--sp-3);
    border-bottom: 1px solid var(--line-soft);
  }
  .sub {
    padding: 0 0 var(--sp-3);
    border: 0;
  }
  .bar button,
  .sub button {
    padding: 2px 9px;
    border-radius: 11px;
    color: var(--fg-2);
    font-size: var(--fs-sm);
  }
  .bar button:hover,
  .sub button:hover {
    color: var(--fg);
  }
  .bar button.on,
  .sub button.on {
    background: var(--bg-3);
    color: var(--fg);
  }
  .body {
    padding: var(--sp-4);
  }
  .ceil {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: var(--sp-3);
  }
  .track {
    flex: 1;
    height: 3px;
    background: var(--bg-3);
    border-radius: 2px;
    overflow: hidden;
  }
  .track i {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .track i.over {
    background: var(--bad);
  }
  .r {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 24px;
    font-size: var(--fs-sm);
  }
  .id {
    width: 84px;
    flex: none;
    color: var(--fg-1);
  }
  .mf .id {
    width: 110px;
  }
  .bar2 {
    width: 70px;
    height: 3px;
    background: var(--bg-3);
    border-radius: 2px;
    overflow: hidden;
    flex: none;
  }
  .bar2 i {
    display: block;
    height: 100%;
    background: var(--fg-2);
  }
  .w {
    width: 42px;
    text-align: right;
  }
</style>
