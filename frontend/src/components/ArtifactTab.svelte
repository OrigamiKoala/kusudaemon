<script>
  import { FileText, Code, GitCompareArrows } from "@lucide/svelte";
  import Diff from "./Diff.svelte";
  import Out from "./Out.svelte";
  import { app, live, viewOf } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { renderMarkdown } from "../lib/markdown.js";
  import { serverLinesToFile } from "../lib/diff.js";
  import { fmtTokens } from "../lib/format.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  const id = $derived(view.node);
  const d = $derived(run?.detail && run.detail.id === id ? run.detail : null);

  let mode = $state("render"); // render | raw | diff
  let tag = $state("");
  let diffFile = $state(null);
  let old = $state(null);

  const versions = $derived(d?.versions ?? []);
  $effect(() => {
    id;
    tag = "";
    diffFile = null;
    old = null;
  });
  $effect(() => {
    if (mode !== "diff" || !id || !app.activeRun) return;
    const t = tag || versions[versions.length - 1];
    if (!t) {
      diffFile = null;
      return;
    }
    get(`/api/node/${encodeURIComponent(id)}/diff/${encodeURIComponent(t)}`, { run: app.activeRun })
      .then((r) => (diffFile = serverLinesToFile(id, r.lines)))
      .catch(() => (diffFile = null));
  });
  $effect(() => {
    if (!tag || mode === "diff" || !id) return (old = null);
    get(`/api/node/${encodeURIComponent(id)}/version/${encodeURIComponent(tag)}`, { run: app.activeRun })
      .then((r) => (old = r.text))
      .catch(() => (old = null));
  });
  const text = $derived(tag && old != null ? old : (d?.artifact ?? ""));
  const html = $derived(mode === "render" ? renderMarkdown(text) : "");
</script>

{#if !id}
  <div class="none dim"><FileText size={16} /></div>
{:else if !d}
  <div class="none dim">…</div>
{:else}
  <div class="bar">
    <button class="icon-btn" aria-label="rendered" aria-pressed={mode === "render"} onclick={() => (mode = "render")}><FileText size={14} /></button>
    <button class="icon-btn" aria-label="raw" aria-pressed={mode === "raw"} onclick={() => (mode = "raw")}><Code size={14} /></button>
    <button class="icon-btn" aria-label="diff" aria-pressed={mode === "diff"} disabled={!versions.length} onclick={() => (mode = "diff")}><GitCompareArrows size={14} /></button>
    <span class="grow"></span>
    {#if versions.length}
      <select aria-label="version" bind:value={tag}>
        <option value="">current</option>
        {#each [...versions].reverse() as v}<option value={v}>{v}</option>{/each}
      </select>
    {/if}
    <span class="num dim">{fmtTokens(d.artifact_tokens)}</span>
  </div>
  <div class="body">
    {#if mode === "diff"}
      {#if diffFile}<Diff files={[diffFile]} />{:else}<div class="none dim"><GitCompareArrows size={16} /></div>{/if}
    {:else if !text}
      <div class="none dim"><FileText size={16} /></div>
    {:else if mode === "raw"}
      <Out {text} cap={400} numbered />
    {:else}
      <div class="prose">{@html html}</div>
    {/if}
  </div>
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: var(--sp-2) var(--sp-3);
    border-bottom: 1px solid var(--line-soft);
    position: sticky;
    top: 0;
    background: var(--bg-1);
    z-index: 2;
  }
  select {
    height: 22px;
    font-size: var(--fs-xs);
    max-width: 160px;
  }
  .body {
    padding: var(--sp-4);
  }
  .none {
    display: grid;
    place-items: center;
    height: 100px;
  }
</style>
