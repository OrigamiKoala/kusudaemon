<script>
  import { Folder, File, ChevronRight, Search, ArrowUp, FolderGit2, FolderClosed, X } from "@lucide/svelte";
  import Out from "./Out.svelte";
  import { app } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { highlight, langForPath, renderMarkdown } from "../lib/markdown.js";
  import { fmtBytes, fmtStamp } from "../lib/format.js";

  let root = $state("run");
  let path = $state("");
  let listing = $state(null);
  let file = $state(null);
  let q = $state("");
  let hits = $state(null);
  let rendered = $state(true);
  let failed = $state(false);

  const p = (extra) => ({ root, ...extra });

  async function list(next = path) {
    failed = false;
    try {
      listing = await get("/api/files", { run: app.activeRun, params: p({ path: next }) });
      path = listing.path;
    } catch {
      listing = null;
      failed = true;
    }
  }
  async function open(rel) {
    hits = null;
    try {
      file = await get("/api/file", { run: app.activeRun, params: p({ path: rel }) });
    } catch {
      file = null;
    }
  }
  $effect(() => {
    root;
    app.activeRun;
    file = null;
    hits = null;
    path = "";
    list("");
  });

  async function search() {
    if (q.trim().length < 2) return (hits = null);
    try {
      hits = (await get("/api/search", { run: app.activeRun, params: p({ q }) })).hits;
    } catch {
      hits = [];
    }
  }
  const crumbs = $derived(path ? path.split("/") : []);
  const join = (a, b) => (a ? a + "/" + b : b);
  const up = () => list(path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
  const lang = $derived(file ? langForPath(file.path) : "");
</script>

<div class="bar">
  <button class="icon-btn" aria-label="run directory" aria-pressed={root === "run"} onclick={() => (root = "run")}><FolderClosed size={14} /></button>
  <button class="icon-btn" aria-label="workspace" aria-pressed={root === "workspace"} onclick={() => (root = "workspace")}><FolderGit2 size={14} /></button>
  <label class="s">
    <Search size={12} />
    <input type="search" aria-label="search files" bind:value={q} onkeydown={(e) => e.key === "Enter" && search()} />
    {#if q}<button class="icon-btn sm" aria-label="clear" onclick={() => ((q = ""), (hits = null))}><X size={11} /></button>{/if}
  </label>
</div>

{#if file}
  <div class="fh">
    <button class="icon-btn sm" aria-label="back" onclick={() => (file = null)}><ArrowUp size={13} style="transform:rotate(-90deg)" /></button>
    <span class="mono trunc">{file.path}</span>
    <span class="grow"></span>
    {#if lang === "markdown"}<button class="chip" aria-pressed={rendered} onclick={() => (rendered = !rendered)}>md</button>{/if}
    <span class="num dim">{fmtBytes(file.size)}</span>
  </div>
  <div class="fv">
    {#if file.binary}
      <div class="dim mono">binary</div>
    {:else if lang === "markdown" && rendered}
      <div class="prose">{@html renderMarkdown(file.text)}</div>
    {:else}
      <Out text={file.text} lang={lang} cap={600} numbered={!lang} />
    {/if}
    {#if file.truncated}<div class="dim mono tr">…</div>{/if}
  </div>
{:else if hits}
  <div class="hits">
    {#each hits as h, i (i)}
      <button class="hit" onclick={() => open(h.path)}>
        <span class="mono trunc hp">{h.path}<span class="dim">:{h.line}</span></span>
        <span class="mono trunc ht dim">{h.text}</span>
      </button>
    {:else}
      <div class="dim none">∅</div>
    {/each}
  </div>
{:else if failed}
  <div class="dim none"><Folder size={16} /></div>
{:else if listing}
  <div class="crumbs mono">
    <button onclick={() => list("")}>/</button>
    {#each crumbs as c, i}
      <ChevronRight size={11} /><button onclick={() => list(crumbs.slice(0, i + 1).join("/"))}>{c}</button>
    {/each}
  </div>
  <div class="ls">
    {#if path}<button class="e" onclick={up}><ArrowUp size={13} /><span class="mono">..</span></button>{/if}
    {#each listing.entries as e (e.name)}
      <button class="e" onclick={() => (e.type === "dir" ? list(join(path, e.name)) : open(join(path, e.name)))}>
        {#if e.type === "dir"}<Folder size={13} />{:else}<File size={13} />{/if}
        <span class="mono trunc n">{e.name}</span>
        <span class="num dim">{e.type === "dir" ? "" : fmtBytes(e.size)}</span>
        <span class="num dim m">{fmtStamp(e.mtime)}</span>
      </button>
    {/each}
  </div>
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: var(--sp-2) var(--sp-3);
    border-bottom: 1px solid var(--line-soft);
  }
  .s {
    display: flex;
    align-items: center;
    gap: 5px;
    flex: 1;
    margin-left: 6px;
    height: 24px;
    padding: 0 6px;
    background: var(--bg-2);
    border-radius: var(--r-1);
    color: var(--fg-2);
  }
  .s input {
    flex: 1;
    background: none;
    border: 0;
    padding: 0;
    outline: none;
    height: 100%;
    min-width: 0;
  }
  .crumbs {
    display: flex;
    align-items: center;
    gap: 2px;
    flex-wrap: wrap;
    padding: var(--sp-2) var(--sp-4);
    font-size: var(--fs-xs);
    color: var(--fg-2);
  }
  .crumbs button:hover {
    color: var(--accent);
  }
  .e {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    height: 26px;
    padding: 0 var(--sp-4);
    text-align: left;
    color: var(--fg-1);
    font-size: var(--fs-sm);
  }
  .e:hover {
    background: var(--bg-2);
  }
  .n {
    flex: 1;
  }
  .m {
    font-size: var(--fs-xs);
    min-width: 70px;
    text-align: right;
  }
  .fh {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px var(--sp-3);
    border-bottom: 1px solid var(--line-soft);
    font-size: var(--fs-sm);
  }
  .fv {
    padding: var(--sp-3) var(--sp-4);
  }
  .tr {
    padding: 6px;
  }
  .hit {
    display: flex;
    flex-direction: column;
    width: 100%;
    padding: 5px var(--sp-4);
    text-align: left;
    gap: 1px;
  }
  .hit:hover {
    background: var(--bg-2);
  }
  .hp {
    font-size: var(--fs-sm);
  }
  .ht {
    font-size: var(--fs-xs);
  }
  .none {
    display: grid;
    place-items: center;
    height: 100px;
  }
</style>
