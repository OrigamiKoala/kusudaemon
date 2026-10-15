<script>
  import { Activity, SplitSquareHorizontal, PinOff } from "@lucide/svelte";
  import FeedList from "./FeedList.svelte";
  import Spark from "./Spark.svelte";
  import { ListChecks, ChevronDown, ChevronRight } from "@lucide/svelte";
  import { app, live, persist, selectNode, viewOf } from "../lib/store.svelte.js";
  import { buildFeed, usageTotals } from "../lib/trace.js";
  import { fmtTokens } from "../lib/format.js";

  let { agentId, pinnable = true } = $props();
  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));

  $effect(() => {
    if (!run || !agentId) return;
    return run.watch(agentId);
  });

  const feed = $derived(run?.feeds[agentId]);
  const items = $derived(feed ? buildFeed(feed.entries, { live: !!run?.agent(agentId)?.live }) : []);
  const totals = $derived(feed ? usageTotals(feed.entries) : null);
  const agent = $derived(run?.agent(agentId));
  const siblings = $derived(run && view.node ? run.agentsOf(view.node) : []);
  // context meter: prompt size per model turn; todo list: the agent's latest plan
  const ctxSeries = $derived((feed?.entries ?? []).filter((e) => e.role === "usage" && e.prompt_tokens).map((e) => e.prompt_tokens).slice(-60));
  const plan = $derived.by(() => {
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === "tool" && it.toolKind === "todo" && Array.isArray(it.input?.todos)) return it.input.todos;
    }
    return null;
  });
  const planDone = $derived(plan ? plan.filter((t) => t.status === "completed").length : 0);
  let planOpen = $state(false);
</script>

<FeedList {items} streaming={!!agent?.live} kinds={["thinking", "tool", "assistant", "usage"]}>
  {#snippet top()}
    {#if plan?.length}
      <div class="plan">
        <button class="ph" aria-expanded={planOpen} onclick={() => (planOpen = !planOpen)}>
          {#if planOpen}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
          <ListChecks size={13} />
          <span class="num">{planDone}/{plan.length}</span>
          <span class="track"><i style:width="{(planDone / plan.length) * 100}%"></i></span>
        </button>
        {#if planOpen}
          <ul>
            {#each plan as t}<li class={t.status}><span class="box"></span>{t.content}</li>{/each}
          </ul>
        {/if}
      </div>
    {/if}
  {/snippet}
  {#snippet head()}
    <button class="icon-btn sm" aria-label="run feed" onclick={() => selectNode(null)}><Activity size={13} /></button>
    <span class="who mono">{agentId}</span>
    {#if agent?.live}<span class="dot running"></span>{/if}
    {#if siblings.length > 1}
      <select aria-label="agent" value={agentId} onchange={(e) => ((view.agent = e.currentTarget.value), persist())}>
        {#each siblings as s (s.id)}<option value={s.id}>{s.id}</option>{/each}
      </select>
    {/if}
    {#if ctxSeries.length > 1}
      <span class="ctx" title="context {ctxSeries[ctxSeries.length - 1]}"><Spark values={ctxSeries} label="context" /><span class="num dim">{fmtTokens(ctxSeries[ctxSeries.length - 1])}</span></span>
    {/if}
    {#if totals && totals.total}
      <span class="num dim tot" title="p{totals.prompt} c{totals.completion} r{totals.reasoning}">{fmtTokens(totals.total)}</span>
    {/if}
    {#if pinnable}
      <button class="icon-btn sm" aria-label={view.pin === agentId ? "unpin" : "pin beside"} aria-pressed={view.pin === agentId} onclick={() => ((view.pin = view.pin === agentId ? null : agentId), persist())}>
        {#if view.pin === agentId}<PinOff size={13} />{:else}<SplitSquareHorizontal size={13} />{/if}
      </button>
    {/if}
  {/snippet}
</FeedList>

<style>
  .who {
    font-size: var(--fs-sm);
    color: var(--fg);
    margin-right: 6px;
  }
  .ctx {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin: 0 6px;
    font-size: var(--fs-xs);
  }
  .plan {
    border-bottom: 1px solid var(--line-soft);
    padding: 0 var(--sp-4);
    flex: none;
  }
  .ph {
    display: flex;
    align-items: center;
    gap: 7px;
    height: 26px;
    width: 100%;
    color: var(--fg-1);
    font-size: var(--fs-xs);
  }
  .track {
    flex: 1;
    max-width: 120px;
    height: 2px;
    background: var(--bg-3);
    border-radius: 1px;
    overflow: hidden;
  }
  .track i {
    display: block;
    height: 100%;
    background: var(--ok);
  }
  .plan ul {
    list-style: none;
    margin: 0 0 8px;
    padding: 0 0 0 20px;
    font-size: var(--fs-sm);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .plan li {
    display: flex;
    gap: 8px;
    align-items: baseline;
    color: var(--fg-1);
  }
  .plan .box {
    width: 9px;
    height: 9px;
    border: 1px solid var(--fg-2);
    border-radius: 2px;
    flex: none;
    transform: translateY(1px);
  }
  .plan li.completed { color: var(--fg-2); text-decoration: line-through; }
  .plan li.completed .box { background: var(--ok); border-color: var(--ok); }
  .plan li.in_progress .box { border-color: var(--accent); background: var(--accent-dim); }
  .tot {
    margin: 0 6px;
    font-size: var(--fs-xs);
  }
  select {
    height: 22px;
    font-size: var(--fs-xs);
    margin-left: 6px;
  }
</style>
