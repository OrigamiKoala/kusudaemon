<script>
  import { ChevronDown, ChevronUp, Ban, Loader, X } from "@lucide/svelte";
  import ApprovalCard from "./ApprovalCard.svelte";
  import { app, live, selectNode, viewOf } from "../lib/store.svelte.js";

  const run = $derived(live.current);
  const control = $derived(app.caps?.control_enabled !== false);
  const pending = $derived(run?.pending ?? []);
  const blocked = $derived((run?.tree ?? []).filter((n) => n.status === "blocked"));
  const jobs = $derived((run?.jobs ?? []).filter((j) => j.status === "running" || j.status === "queued"));
  let shut = $state(false);

  function open(id) {
    selectNode(id);
    viewOf(app.activeRun).tab = "node";
  }
</script>

{#if pending.length || blocked.length || jobs.length}
  <div class="tray">
    {#if blocked.length}
      <div class="strip warn">
        <Ban size={13} />
        <span class="num">{blocked.length}</span>
        <div class="chips">
          {#each blocked.slice(0, 8) as n (n.id)}<button class="mono" onclick={() => open(n.id)}>{n.id}</button>{/each}
        </div>
      </div>
    {/if}
    {#each jobs as j (j.job_id)}
      <div class="strip">
        <Loader size={13} class="spin" />
        <span class="mono">{j.kind}</span>
        <span class="dim trunc grow">{j.detail}</span>
        {#if control}<button class="icon-btn sm" aria-label="cancel job" onclick={() => run.cancelJob(j.job_id)}><X size={12} /></button>{/if}
      </div>
    {/each}
    {#if pending.length}
      <button class="head" aria-expanded={!shut} onclick={() => (shut = !shut)}>
        <span class="dot waiting"></span>
        <span class="num">{pending.length}</span>
        <span class="grow"></span>
        {#if shut}<ChevronDown size={13} />{:else}<ChevronUp size={13} />{/if}
      </button>
      {#if !shut}
        <div class="list">
          {#each pending as a (a.approval_id)}<ApprovalCard {a} />{/each}
        </div>
      {/if}
    {/if}
  </div>
{/if}

<style>
  .tray {
    flex: none;
    border-bottom: 1px solid var(--line);
    background: var(--bg);
  }
  .strip {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 28px;
    padding: 0 var(--sp-4);
    font-size: var(--fs-sm);
    color: var(--fg-1);
    border-bottom: 1px solid var(--line-soft);
  }
  .strip.warn {
    color: var(--warn);
  }
  .chips {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }
  .chips button {
    font-size: var(--fs-xs);
    color: var(--fg-1);
  }
  .chips button:hover {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    height: 28px;
    padding: 0 var(--sp-4);
    color: var(--wait);
  }
  .list {
    max-height: 46vh;
    overflow: auto;
  }
</style>
