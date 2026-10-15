<script>
  import { Search, Trash2, Plus } from "@lucide/svelte";
  import { app, openRun, deleteRun, toast } from "../lib/store.svelte.js";
  import { runDot } from "../lib/status.js";
  import { fmtTokens, shortId } from "../lib/format.js";

  let q = $state("");
  let confirmId = $state("");

  const rows = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    return app.runs.filter((r) => !needle || r.id.toLowerCase().includes(needle) || (r.goal || "").toLowerCase().includes(needle));
  });

  async function remove(e, id) {
    e.stopPropagation();
    if (confirmId !== id) {
      confirmId = id;
      setTimeout(() => confirmId === id && (confirmId = ""), 2600);
      return;
    }
    confirmId = "";
    try {
      await deleteRun(id);
    } catch (err) {
      toast(`delete: ${err.message}`, "error");
    }
  }
</script>

<div class="runs">
  <label class="search">
    <Search size={13} />
    <input type="search" aria-label="filter runs" bind:value={q} />
  </label>
  <div class="list" role="listbox" aria-label="runs">
    {#each rows as r (r.id)}
      <div
        role="option"
        tabindex="0"
        aria-selected={r.id === app.activeRun}
        class="run"
        class:active={r.id === app.activeRun}
        title={r.goal}
        onclick={() => openRun(r.id)}
        onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), openRun(r.id))}
      >
        <span class="dot {runDot(r)}"></span>
        <span class="id trunc mono">{shortId(r.id, 30)}</span>
        {#if r.pending_approvals}<span class="chip wait num">{r.pending_approvals}</span>{/if}
        <span class="meta num">{fmtTokens(r.total_tokens)}</span>
        <button class="icon-btn sm del" class:armed={confirmId === r.id} aria-label="delete run" onclick={(e) => remove(e, r.id)}>
          <Trash2 size={12} />
        </button>
      </div>
    {/each}
    {#if app.runsLoaded && !rows.length}
      <div class="none dim"><Plus size={14} /></div>
    {/if}
  </div>
</div>

<style>
  .runs {
    display: flex;
    flex-direction: column;
    min-height: 0;
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
  .list {
    overflow: auto;
    max-height: 34vh;
    padding: 0 var(--sp-2);
  }
  .run {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 28px;
    padding: 0 6px 0 8px;
    border-radius: var(--r-1);
    color: var(--fg-1);
    cursor: pointer;
  }
  .run:hover {
    background: var(--bg-2);
  }
  .run.active {
    background: var(--bg-3);
    color: var(--fg);
  }
  .id {
    flex: 1;
    font-size: var(--fs-sm);
  }
  .meta {
    font-size: var(--fs-xs);
    color: var(--fg-2);
  }
  .del {
    display: none;
  }
  .run:hover .del,
  .del.armed {
    display: inline-grid;
  }
  .run:hover .meta {
    display: none;
  }
  .del.armed {
    color: var(--bad);
    background: var(--bad-dim);
  }
  .none {
    display: grid;
    place-items: center;
    height: 48px;
  }
</style>
