<script>
  import { X, PanelLeftOpen, PanelRight, Sun, Moon, Command, Plus } from "@lucide/svelte";
  import { app, closeRun, openRun, persist, toggleTheme } from "../lib/store.svelte.js";
  import { runDot } from "../lib/status.js";
  import { shortId } from "../lib/format.js";

  const byId = $derived(new Map(app.runs.map((r) => [r.id, r])));
  const dark = $derived((app.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")) === "dark");
</script>

<div class="top">
  {#if !app.railOpen}
    <button class="icon-btn" aria-label="show rail" onclick={() => ((app.railOpen = true), persist())}><PanelLeftOpen size={15} /></button>
  {/if}
  <div class="tabs" role="tablist" aria-label="open runs">
    {#each app.openRuns as id (id)}
      <div class="tab" role="tab" tabindex="0" aria-selected={id === app.activeRun} class:active={id === app.activeRun} onclick={() => openRun(id)} onkeydown={(e) => e.key === "Enter" && openRun(id)} title={byId.get(id)?.goal}>
        <span class="dot {runDot(byId.get(id))}"></span>
        <span class="mono trunc">{shortId(id, 26)}</span>
        <button class="x" aria-label="close tab" onclick={(e) => (e.stopPropagation(), closeRun(id))}><X size={12} /></button>
      </div>
    {/each}
    <button class="icon-btn" aria-label="new run" onclick={() => (app.newRun = true)}><Plus size={14} /></button>
  </div>
  <span class="grow"></span>
  <span class="conn" class:off={app.sse !== "live"} title={app.sse} aria-label={app.sse}></span>
  <button class="icon-btn" aria-label="command palette" onclick={() => (app.palette = true)}><Command size={15} /></button>
  <button class="icon-btn" aria-label="toggle theme" onclick={toggleTheme}>{#if dark}<Sun size={15} />{:else}<Moon size={15} />{/if}</button>
  <button class="icon-btn" aria-label="toggle inspector" aria-pressed={app.inspectorOpen} onclick={() => ((app.inspectorOpen = !app.inspectorOpen), persist())}><PanelRight size={15} /></button>
</div>

<style>
  .top {
    display: flex;
    align-items: center;
    gap: 4px;
    height: var(--bar-h);
    padding: 0 var(--sp-2);
    border-bottom: 1px solid var(--line-soft);
    flex: none;
  }
  .tabs {
    display: flex;
    align-items: center;
    gap: 2px;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .tab {
    display: flex;
    align-items: center;
    gap: 7px;
    height: 28px;
    padding: 0 6px 0 10px;
    border-radius: var(--r-1);
    color: var(--fg-2);
    cursor: pointer;
    max-width: 240px;
    position: relative;
  }
  .tab:hover {
    background: var(--bg-2);
    color: var(--fg-1);
  }
  .tab.active {
    color: var(--fg);
    background: var(--bg-2);
  }
  .tab.active::after {
    content: "";
    position: absolute;
    left: 8px;
    right: 8px;
    bottom: -5px;
    height: 1.5px;
    background: var(--accent);
  }
  .tab .mono {
    font-size: var(--fs-sm);
  }
  .x {
    display: grid;
    place-items: center;
    width: 18px;
    height: 18px;
    border-radius: 3px;
    color: var(--fg-2);
    opacity: 0;
  }
  .tab:hover .x,
  .tab.active .x {
    opacity: 1;
  }
  .x:hover {
    background: var(--bg-3);
    color: var(--fg);
  }
  .conn {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--ok);
    margin: 0 6px;
  }
  .conn.off {
    background: var(--warn);
  }
</style>
