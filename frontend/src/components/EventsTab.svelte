<script>
  import { Search } from "@lucide/svelte";
  import FeedItem from "./FeedItem.svelte";
  import ApprovalCard from "./ApprovalCard.svelte";
  import { live } from "../lib/store.svelte.js";

  const run = $derived(live.current);
  let type = $state("");
  let q = $state("");
  let cap = $state(300);
  let showApprovals = $state(false);

  const types = $derived.by(() => {
    const c = {};
    for (const e of run?.events ?? []) c[e.type] = (c[e.type] || 0) + 1;
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  });
  const rows = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    const out = [];
    const evs = run?.events ?? [];
    for (let i = evs.length - 1; i >= 0 && out.length < cap; i--) {
      const e = evs[i];
      if (type && e.type !== type) continue;
      const detail = e.detail || e.reason || e.phase || e.status || (e.unmet ? e.unmet.join(", ") : "") || e.message || "";
      if (needle && !`${e.type} ${e.node_id} ${detail}`.toLowerCase().includes(needle)) continue;
      out.push({ kind: "event", key: i, ts: e.ts, type: e.type, node: e.node_id, detail });
    }
    return out;
  });
  const resolved = $derived((run?.approvals ?? []).filter((a) => a.status !== "pending").slice().reverse());
</script>

<div class="bar">
  <label class="s">
    <Search size={12} />
    <input type="search" aria-label="filter events" bind:value={q} />
  </label>
  <select aria-label="event type" bind:value={type}>
    <option value="">{run?.events.length ?? 0}</option>
    {#each types as [t, n]}<option value={t}>{t} · {n}</option>{/each}
  </select>
  <button class="chip" aria-pressed={showApprovals} onclick={() => (showApprovals = !showApprovals)}>{resolved.length}</button>
</div>
{#if showApprovals}
  {#each resolved as a (a.approval_id)}<ApprovalCard {a} />{/each}
{:else}
  <div class="list">
    {#each rows as it (it.key)}<FeedItem item={it} />{/each}
    {#if rows.length >= cap}<button class="more" onclick={() => (cap += 400)}>+</button>{/if}
  </div>
{/if}

<style>
  .bar {
    display: flex;
    gap: 6px;
    align-items: center;
    padding: var(--sp-2) var(--sp-3);
    border-bottom: 1px solid var(--line-soft);
    position: sticky;
    top: 0;
    background: var(--bg-1);
    z-index: 2;
  }
  .s {
    display: flex;
    align-items: center;
    gap: 5px;
    flex: 1;
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
  select {
    height: 24px;
    font-size: var(--fs-xs);
    max-width: 150px;
  }
  .list {
    padding: var(--sp-2) var(--sp-3);
  }
  .more {
    width: 100%;
    padding: 6px;
    color: var(--fg-2);
  }
</style>
