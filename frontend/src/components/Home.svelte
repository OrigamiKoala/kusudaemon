<script>
  import { Plus } from "@lucide/svelte";
  import { app, openRun } from "../lib/store.svelte.js";
  import { runDot } from "../lib/status.js";
  import { clip, fmtStamp, fmtTokens } from "../lib/format.js";
</script>

<div class="home">
  <div class="bar">
    <span class="num dim">{app.runs.length}</span>
    <span class="grow"></span>
    <button class="btn primary" aria-label="new run" onclick={() => (app.newRun = true)}><Plus size={14} /></button>
  </div>
  <div class="tbl">
    {#each app.runs as r (r.id)}
      <button class="r" onclick={() => openRun(r.id)}>
        <span class="dot {runDot(r)}"></span>
        <span class="mono id trunc">{r.id}</span>
        <span class="trunc goal dim">{clip(r.goal, 120)}</span>
        <span class="mono ph dim">{r.phase}</span>
        <span class="num w dim">{fmtTokens(r.total_tokens)}</span>
        <span class="num w dim">{fmtStamp(r.mtime)}</span>
      </button>
    {/each}
  </div>
</div>

<style>
  .home {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .bar {
    display: flex;
    align-items: center;
    padding: var(--sp-3) var(--sp-4);
    border-bottom: 1px solid var(--line-soft);
  }
  .tbl {
    overflow: auto;
    flex: 1;
  }
  .r {
    display: grid;
    grid-template-columns: 14px minmax(160px, 280px) minmax(0, 1fr) 70px 54px 96px;
    align-items: center;
    gap: 12px;
    width: 100%;
    height: 34px;
    padding: 0 var(--sp-4);
    text-align: left;
    font-size: var(--fs-sm);
    border-bottom: 1px solid var(--line-soft);
  }
  .r:hover {
    background: var(--bg-1);
  }
  .w {
    text-align: right;
  }
  @media (max-width: 760px) {
    .r {
      grid-template-columns: 14px minmax(0, 1fr) 64px;
    }
    .goal,
    .ph,
    .w:nth-of-type(n + 2) {
      display: none;
    }
  }
</style>
