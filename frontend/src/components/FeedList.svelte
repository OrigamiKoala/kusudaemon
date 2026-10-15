<script>
  import { Brain, Wrench, Bot, Gauge, Activity, ArrowDownToLine, Search, ChevronUp } from "@lucide/svelte";
  import FeedItem from "./FeedItem.svelte";
  import { toolTitle } from "../lib/trace.js";

  let { items = [], streaming = false, head = null, top = null, kinds = ["thinking", "tool", "assistant", "usage", "event"] } = $props();

  const FILTERS = [
    { id: "thinking", icon: Brain },
    { id: "tool", icon: Wrench },
    { id: "assistant", icon: Bot },
    { id: "usage", icon: Gauge },
    { id: "event", icon: Activity },
  ];
  let hidden = $state({ usage: true });
  let q = $state("");
  let searching = $state(false);
  let cap = $state(260);
  let box = $state();
  let stick = $state(true);

  const groupOf = (it) => (it.kind === "thinking" || it.kind === "tool" || it.kind === "usage" || it.kind === "event" ? it.kind : it.kind === "assistant" || it.kind === "user" ? "assistant" : "other");

  const filtered = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    return items.filter((it) => {
      if (hidden[groupOf(it)]) return false;
      if (!needle) return true;
      const hay = it.kind === "tool" ? `${it.name} ${toolTitle(it.name, it.input)} ${it.result ?? ""}` : it.kind === "event" ? `${it.type} ${it.node} ${it.detail}` : it.text || "";
      return hay.toLowerCase().includes(needle);
    });
  });
  const visible = $derived(filtered.slice(-cap));
  const lastKey = $derived(filtered.length ? filtered[filtered.length - 1].key : null);
  // the reasoning still being written is the newest thinking item
  const liveThink = $derived.by(() => {
    for (let i = filtered.length - 1; i >= 0 && i > filtered.length - 6; i--) if (filtered[i].kind === "thinking") return filtered[i].key;
    return null;
  });

  function onscroll() {
    if (!box) return;
    stick = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  }
  function toBottom() {
    if (box) box.scrollTop = box.scrollHeight;
    stick = true;
  }
  $effect(() => {
    visible.length;
    lastKey;
    items;
    if (stick && box) requestAnimationFrame(() => box && (box.scrollTop = box.scrollHeight));
  });
</script>

<div class="feed">
  <div class="tb">
    {#if head}{@render head()}{/if}
    <span class="grow"></span>
    {#if searching}
      <input type="search" class="q" aria-label="search feed" bind:value={q} onblur={() => !q && (searching = false)} />
    {:else}
      <button class="icon-btn sm" aria-label="search feed" onclick={() => (searching = true)}><Search size={13} /></button>
    {/if}
    {#each FILTERS.filter((f) => kinds.includes(f.id)) as f (f.id)}
      {@const I = f.icon}
      <button class="icon-btn sm" aria-label={f.id} aria-pressed={!hidden[f.id]} onclick={() => (hidden[f.id] = !hidden[f.id])}><I size={13} /></button>
    {/each}
  </div>
  {#if top}{@render top()}{/if}
  <div class="scroll" bind:this={box} {onscroll}>
    {#if filtered.length > visible.length}
      <button class="earlier" onclick={() => (cap += 300)}><ChevronUp size={12} />{filtered.length - visible.length}</button>
    {/if}
    {#each visible as it (it.key)}
      <div class="cv"><FeedItem item={it} streaming={streaming && it.key === liveThink} /></div>
    {/each}
  </div>
  {#if !stick}
    <button class="latest" aria-label="latest" onclick={toBottom}><ArrowDownToLine size={14} /></button>
  {/if}
</div>

<style>
  .feed {
    display: flex;
    flex-direction: column;
    min-height: 0;
    flex: 1;
    position: relative;
  }
  .tb {
    display: flex;
    align-items: center;
    gap: 2px;
    height: 32px;
    padding: 0 var(--sp-3) 0 var(--sp-4);
    border-bottom: 1px solid var(--line-soft);
    flex: none;
  }
  .q {
    height: 22px;
    width: 160px;
    padding: 0 8px;
    font-size: var(--fs-sm);
  }
  .scroll {
    overflow: auto;
    flex: 1;
    padding: var(--sp-3) var(--sp-4) var(--sp-4);
    min-height: 0;
  }
  .cv {
    content-visibility: auto;
    contain-intrinsic-size: auto 28px;
  }
  .earlier {
    display: flex;
    align-items: center;
    gap: 4px;
    margin: 0 auto var(--sp-3);
    padding: 3px 10px;
    border-radius: 12px;
    border: 1px solid var(--line-soft);
    color: var(--fg-2);
    font-size: var(--fs-xs);
  }
  .earlier:hover {
    color: var(--fg);
    border-color: var(--line);
  }
  .latest {
    position: absolute;
    right: 18px;
    bottom: 14px;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    display: grid;
    place-items: center;
    background: var(--bg-3);
    border: 1px solid var(--line);
    color: var(--fg);
    box-shadow: var(--pop-shadow);
  }
  .latest:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
</style>
