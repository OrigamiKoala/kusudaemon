<script>
  import { Pause, Play, Square, ChevronsUp, RotateCw, TriangleAlert } from "@lucide/svelte";
  import { app, live } from "../lib/store.svelte.js";
  import { PHASES, phaseDot } from "../lib/status.js";
  import { clip, fmtDur, fmtTokens } from "../lib/format.js";

  const run = $derived(live.current);
  const m = $derived(run?.meta ?? {});
  const control = $derived(app.caps?.control_enabled !== false);
  const backend = $derived(m.backend_override || m.backend || "");
  const models = $derived(app.models.by_backend?.[backend] || []);
  const phases = $derived(PHASES.filter((p) => m.phases && m.phases[p]));
  let armKill = $state(false);

  function kill() {
    if (!armKill) {
      armKill = true;
      setTimeout(() => (armKill = false), 2600);
      return;
    }
    armKill = false;
    run.kill();
  }
  const tierLabel = $derived(m.tier ? (m.tier_override ? `${m.tier}↑` : m.tier) : "");
</script>

{#if run && m.attached}
  <div class="bar">
    <div class="goal trunc" title={m.goal}>{clip(m.goal, 160)}</div>
    <div class="meta">
      <div class="phases" role="list" aria-label="phases">
        {#each phases as p (p)}
          <span class="ph {phaseDot(m.phases[p])}" role="listitem" title="{p} · {m.phases[p]}">
            <i></i>{p}
          </span>
        {/each}
      </div>
      <span class="grow"></span>
      {#if m.stalled}
        <span class="chip bad" title={m.stalled_reason}><TriangleAlert size={11} />stalled</span>
      {:else if m.halted}
        <span class="chip" style="color:var(--warn)"><Pause size={11} />halted</span>
      {/if}
      {#if tierLabel}
        <span class="tier">
          <span class="chip num" title={m.measured_tier && m.measured_tier !== m.tier ? `measured ${m.measured_tier}` : ""}>{tierLabel}</span>
          <button class="icon-btn sm" aria-label="escalate tier" disabled={!control || m.tier === "T3"} onclick={() => run.escalate()}><ChevronsUp size={13} /></button>
        </span>
      {/if}
      <select aria-label="backend" value={m.backend_override || ""} disabled={!control} onchange={(e) => run.setBackend(e.currentTarget.value)}>
        <option value="">{m.backend}</option>
        {#each app.caps?.backends || [] as b}<option value={b}>{b}</option>{/each}
      </select>
      <select class="model" aria-label="model" value={m.model_override || ""} disabled={!control} onchange={(e) => run.setModel(e.currentTarget.value)}>
        <option value="">{m.default_model || "default"}</option>
        {#each models as mod}<option value={mod}>{mod}</option>{/each}
      </select>
      <span class="stat num" title="tokens">{fmtTokens(m.total_tokens || 0)}</span>
      <span class="stat num" title="elapsed">{fmtDur(run.elapsed)}</span>
      <span class="ctl">
        {#if m.halted}
          <button class="icon-btn" aria-label="resume" disabled={!control} onclick={() => run.halt(false)}><Play size={14} /></button>
        {:else}
          <button class="icon-btn" aria-label="pause" disabled={!control} onclick={() => run.halt(true)}><Pause size={14} /></button>
        {/if}
        {#if !m.hosted}
          <button class="icon-btn" aria-label="restart driver" disabled={!control} onclick={() => run.resumeDriver()}><RotateCw size={14} /></button>
        {/if}
        <button class="icon-btn" class:danger-armed={armKill} aria-label={armKill ? "confirm kill" : "kill"} disabled={!control} onclick={kill}><Square size={13} /></button>
      </span>
    </div>
  </div>
{/if}

<style>
  .bar {
    padding: 8px var(--sp-4) 6px;
    border-bottom: 1px solid var(--line-soft);
    display: flex;
    flex-direction: column;
    gap: 6px;
    flex: none;
  }
  .goal {
    color: var(--fg-1);
    font-size: var(--fs-sm);
  }
  .meta {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    row-gap: 4px;
  }
  .phases {
    display: flex;
    align-items: center;
    gap: 2px;
    flex-wrap: wrap;
  }
  .ph {
    display: inline-flex;
    flex-direction: column;
    gap: 3px;
    padding-right: 10px;
    font-size: var(--fs-xs);
    color: var(--fg-2);
  }
  .ph i {
    display: block;
    height: 2px;
    width: 100%;
    min-width: 28px;
    background: var(--line);
    border-radius: 1px;
  }
  .ph.passed { color: var(--fg-1); }
  .ph.passed i { background: var(--ok); }
  .ph.running { color: var(--accent); }
  .ph.running i { background: var(--accent); animation: sweep 1.8s var(--ease) infinite; }
  .ph.failed { color: var(--bad); }
  .ph.failed i { background: var(--bad); }
  .ph.waiting { color: var(--wait); }
  .ph.waiting i { background: var(--wait); }
  .ph.blocked i { background: var(--warn); }
  .ph.split i { background: repeating-linear-gradient(90deg, var(--line) 0 3px, transparent 3px 5px); }
  @keyframes sweep {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  .tier {
    display: inline-flex;
    align-items: center;
    gap: 2px;
  }
  select {
    height: 24px;
    font-size: var(--fs-sm);
    padding-top: 0;
    padding-bottom: 0;
    max-width: 150px;
  }
  select.model {
    max-width: 190px;
  }
  .stat {
    font-size: var(--fs-sm);
    color: var(--fg-1);
    min-width: 36px;
    text-align: right;
  }
  .ctl {
    display: inline-flex;
    gap: 2px;
    margin-left: 4px;
  }
  .danger-armed {
    color: var(--bad);
    background: var(--bad-dim);
  }
</style>
