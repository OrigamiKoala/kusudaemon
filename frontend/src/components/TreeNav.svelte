<script>
  import { ChevronRight, ChevronDown, Search, Folder } from "@lucide/svelte";
  import { app, live, persist, selectNode, viewOf } from "../lib/store.svelte.js";
  import { buildTree, flattenTree } from "../lib/score.js";
  import { fmtTokens } from "../lib/format.js";
  import { NODE_ORDER } from "../lib/status.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  let q = $state("");
  let only = $state("");

  const roots = $derived(buildTree(run?.tree ?? []));
  const collapsed = $derived(new Set(view.collapsed || []));
  const counts = $derived.by(() => {
    const c = {};
    for (const n of run?.tree ?? []) c[n.status] = (c[n.status] || 0) + 1;
    return c;
  });
  const rows = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    if (!needle && !only) return flattenTree(roots, collapsed);
    return (run?.tree ?? [])
      .filter((n) => (!only || n.status === only) && (!needle || n.id.toLowerCase().includes(needle) || (n.brief || "").toLowerCase().includes(needle)))
      .map((n) => ({ id: n.id, row: n, virtual: false, children: [], depth: 0, open: false }));
  });

  function toggle(id) {
    const set = new Set(view.collapsed || []);
    set.has(id) ? set.delete(id) : set.add(id);
    view.collapsed = [...set];
    persist();
  }

  function label(r) {
    if (!r.row) return r.id.slice(r.id.lastIndexOf(".") + 1);
    return q || only ? r.id : r.id.includes(".") && r.depth > 0 ? r.id.slice(r.id.lastIndexOf(".") + 1) : r.id;
  }

  function onkey(e) {
    const i = rows.findIndex((r) => r.id === view.node);
    const go = (j) => {
      const r = rows[Math.max(0, Math.min(rows.length - 1, j))];
      if (r && r.row) selectNode(r.id);
      else if (r) toggle(r.id);
    };
    if (e.key === "ArrowDown") (e.preventDefault(), go(i + 1));
    else if (e.key === "ArrowUp") (e.preventDefault(), go(i < 0 ? 0 : i - 1));
    else if (e.key === "ArrowRight" && rows[i]?.children.length && collapsed.has(rows[i].id)) toggle(rows[i].id);
    else if (e.key === "ArrowLeft" && rows[i]?.children.length && !collapsed.has(rows[i].id)) toggle(rows[i].id);
  }

  const dotOf = (s) => (s === "dispatched" ? "running" : s);
</script>

<div class="tree">
  <div class="chips">
    {#each NODE_ORDER.filter((s) => counts[s]) as s (s)}
      <button class="cnt" class:on={only === s} aria-pressed={only === s} aria-label={s} onclick={() => (only = only === s ? "" : s)}>
        <span class="dot {dotOf(s)}"></span><span class="num">{counts[s]}</span>
      </button>
    {/each}
  </div>
  <label class="search">
    <Search size={13} />
    <input type="search" aria-label="filter nodes" bind:value={q} />
  </label>
  <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
  <div class="rows" role="tree" tabindex="0" aria-label="nodes" onkeydown={onkey}>
    {#each rows as r (r.id)}
      <div
        role="treeitem"
        aria-selected={r.id === view.node}
        aria-expanded={r.children.length ? r.open : undefined}
        tabindex="-1"
        class="n"
        class:sel={r.id === view.node}
        class:virt={!r.row}
        style:padding-left="{6 + r.depth * 12}px"
        title={r.row?.brief || r.id}
        onclick={() => (r.row ? selectNode(r.id) : toggle(r.id))}
        onkeydown={() => {}}
      >
        {#if r.children.length}
          <button class="caret" aria-label="toggle" onclick={(e) => (e.stopPropagation(), toggle(r.id))}>
            {#if r.open}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
          </button>
        {:else}
          <span class="caret"></span>
        {/if}
        {#if r.row}
          <span class="dot {dotOf(r.row.status)}"></span>
        {:else}
          <Folder size={12} />
        {/if}
        <span class="id trunc mono">{label(r)}</span>
        {#if r.row}
          {#if r.row.attempts > 1}<span class="att num">×{r.row.attempts}</span>{/if}
          <span class="pips" aria-hidden="true">
            {#each (r.row.gate_results || []).slice(0, 8) as g}<i class:ok={g.passed}></i>{/each}
          </span>
          <span class="meta num">{r.row.tokens ? fmtTokens(r.row.tokens) : ""}</span>
        {:else if r.counts}
          <span class="meta num">{Object.values(r.counts).reduce((a, b) => a + b, 0)}</span>
        {/if}
      </div>
    {/each}
  </div>
</div>

<style>
  .tree {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    padding: 0 var(--sp-3) var(--sp-2);
  }
  .cnt {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 6px;
    border-radius: 10px;
    color: var(--fg-1);
    font-size: var(--fs-xs);
  }
  .cnt:hover {
    background: var(--bg-2);
  }
  .cnt.on {
    background: var(--bg-3);
    color: var(--fg);
  }
  .search {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 var(--sp-3) var(--sp-2);
    padding: 0 8px;
    height: 26px;
    background: var(--bg-2);
    border: 1px solid transparent;
    border-radius: var(--r-1);
    color: var(--fg-2);
  }
  .search:focus-within {
    border-color: var(--accent);
  }
  .search input {
    background: none;
    border: 0;
    padding: 0;
    flex: 1;
    height: 100%;
    outline: none;
  }
  .rows {
    overflow: auto;
    flex: 1;
    padding: 0 var(--sp-2) var(--sp-3);
    outline: none;
  }
  .n {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 24px;
    padding-right: 6px;
    border-radius: var(--r-1);
    color: var(--fg-1);
    cursor: pointer;
  }
  .n:hover {
    background: var(--bg-2);
  }
  .n.sel {
    background: var(--accent-dim);
    color: var(--fg);
  }
  .n.virt {
    color: var(--fg-2);
  }
  .caret {
    width: 12px;
    height: 12px;
    display: grid;
    place-items: center;
    color: var(--fg-2);
  }
  .id {
    flex: 1;
    font-size: var(--fs-sm);
  }
  .att {
    font-size: var(--fs-xs);
    color: var(--warn);
  }
  .pips {
    display: inline-flex;
    gap: 2px;
  }
  .pips i {
    width: 5px;
    height: 5px;
    border: 1px solid var(--bad);
    border-radius: 1px;
  }
  .pips i.ok {
    background: var(--ok);
    border-color: var(--ok);
  }
  .meta {
    font-size: var(--fs-xs);
    color: var(--fg-2);
    min-width: 28px;
    text-align: right;
  }
</style>
