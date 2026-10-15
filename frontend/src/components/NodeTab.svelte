<script>
  import { RotateCcw, Play, Ban, Send, Check, X } from "@lucide/svelte";
  import Section from "./Section.svelte";
  import Out from "./Out.svelte";
  import { app, live, selectNode, viewOf } from "../lib/store.svelte.js";
  import { PHASES, phaseDot } from "../lib/status.js";
  import { fmtClock, fmtDur, fmtTokens } from "../lib/format.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  const control = $derived(app.caps?.control_enabled !== false);
  const id = $derived(view.node);
  const d = $derived(run?.detail && run.detail.id === id ? run.detail : null);
  const row = $derived(id ? run?.node(id) : null);
  const m = $derived(run?.meta ?? {});

  let ask = $state(""); // "reopen" | "say" | "bypass" | "redispatch"
  let text = $state("");
  const nm = (x) => (typeof x === "string" ? x : x?.id || x?.name || x?.item || JSON.stringify(x));

  async function go() {
    const t = text.trim();
    if (ask === "reopen") await run.reopen(id, t);
    else if (ask === "redispatch") await run.redispatch(id, t);
    else if (ask === "bypass") await run.bypass(id, t);
    else if (ask === "say") await run.interject(run.agentsOf(id).find((a) => a.live)?.id || id, t);
    ask = "";
    text = "";
  }
  const auditItems = $derived(Array.isArray(d?.audit?.items) ? d.audit.items : []);
</script>

{#if !id}
  <Section title="run">
    <div class="kv">
      <span class="k">tier</span><span class="mono">{m.tier}{m.tier_override ? ` (${m.tier_override})` : ""}</span>
      <span class="k">backend</span><span class="mono">{m.backend_override || m.backend}</span>
      <span class="k">hosted</span><span class="num">{m.hosted_count}/{m.max_concurrent_runs}</span>
      <span class="k">events</span><span class="num">{m.events_count}</span>
      <span class="k">tokens</span><span class="num">{fmtTokens(m.total_tokens || 0)}</span>
    </div>
  </Section>
  <Section title="phases">
    {#each PHASES.filter((p) => m.phases?.[p]) as p (p)}
      <div class="ph"><span class="dot {phaseDot(m.phases[p])}"></span><span class="mono">{p}</span><span class="grow"></span><span class="dim">{m.phases[p]}</span></div>
    {/each}
    {#if m.phase_detail}<div class="dim detail">{m.phase_detail}</div>{/if}
  </Section>
  {#if m.escalation_history?.length}
    <Section title="escalations">
      {#each m.escalation_history as e}
        <div class="ph"><span class="mono">{e.from}→{e.to}</span><span class="dim trunc">{e.trigger}</span><span class="grow"></span><span class="num dim">{fmtClock(e.ts)}</span></div>
      {/each}
    </Section>
  {/if}
  {#if run?.jobs?.length}
    <Section title="jobs">
      {#each run.jobs as j (j.job_id)}
        <div class="ph">
          <span class="chip">{j.kind}</span><span class="dim trunc">{j.detail || j.status}</span><span class="grow"></span>
          <span class="chip {j.status === 'failed' ? 'bad' : j.status === 'done' ? 'ok' : ''}">{j.status}</span>
          {#if control && j.status === "running"}<button class="icon-btn sm" aria-label="cancel job" onclick={() => run.cancelJob(j.job_id)}><X size={12} /></button>{/if}
        </div>
      {/each}
    </Section>
  {/if}
{:else if !d && !row}
  <Section><span class="dim">…</span></Section>
{:else}
  {@const st = d?.status || row?.status}
  <Section>
    <div class="head">
      <span class="dot {st === 'dispatched' ? 'running' : st}"></span>
      <span class="mono id">{id}</span>
      <span class="chip">{st}</span>
      {#if d?.shape || row?.shape}<span class="chip">{d?.shape || row?.shape}</span>{/if}
      {#if (d?.attempts ?? row?.attempts) > 0}<span class="chip num">×{d?.attempts ?? row?.attempts}</span>{/if}
    </div>
    {#if d?.brief || row?.brief}<p class="brief">{d?.brief || row?.brief}</p>{/if}
    {#if control && row}
      <div class="acts">
        <button class="btn" onclick={() => (ask = "reopen")}><RotateCcw size={13} />reopen</button>
        <button class="btn" onclick={() => (ask = "redispatch")}><Play size={13} />redispatch</button>
        <button class="btn" onclick={() => (ask = "bypass")}><Ban size={13} />bypass</button>
        <button class="btn" disabled={!run.agentsOf(id).some((a) => a.live)} onclick={() => (ask = "say")}><Send size={13} />message</button>
      </div>
      {#if ask}
        <div class="ask">
          <input type="text" aria-label={ask} bind:value={text} onkeydown={(e) => e.key === "Enter" && go()} />
          <button class="icon-btn" aria-label="confirm" onclick={go}><Check size={14} /></button>
          <button class="icon-btn" aria-label="cancel" onclick={() => ((ask = ""), (text = ""))}><X size={14} /></button>
        </div>
      {/if}
    {/if}
    {#if d?.depends_on?.length}
      <div class="deps">
        {#each d.depends_on as dep}<button class="chip mono" onclick={() => selectNode(dep)}>{dep}</button>{/each}
      </div>
    {/if}
  </Section>

  {#if d}
    {#if d.gate_results?.length}
      <Section title="gates">
        {#each d.gate_results as g}
          <div class="gate" title={g.detail}>
            <span class="dot {g.passed ? 'passed' : 'failed'}"></span>
            <span class="mono">{g.gate}</span>
            <span class="dim trunc">{g.detail}</span>
          </div>
        {/each}
      </Section>
    {/if}
    {#if d.judgment?.length || Object.keys(d.rubric || {}).length}
      <Section title="review">
        {#each d.judgment || [] as j}<div class="li">{nm(j)}</div>{/each}
        {#each Object.entries(d.rubric || {}) as [k, v]}
          <div class="gate"><span class="mono">{k}</span><span class="dim trunc">{typeof v === "string" ? v : JSON.stringify(v)}</span></div>
        {/each}
      </Section>
    {/if}
    {#if auditItems.length}
      <Section title="audit">
        {#each auditItems as it}
          <div class="gate" title={it.defect || ""}>
            <span class="dot {it.passed === false || it.verdict === 'fail' ? 'failed' : 'passed'}"></span>
            <span class="mono">{nm(it.item || it.id || it.name)}</span>
            <span class="dim trunc">{it.defect || it.note || ""}</span>
          </div>
        {/each}
      </Section>
    {/if}
    {#if d.inputs?.length}
      <Section title="inputs">
        {#each d.inputs as i}
          <div class="gate"><span class="dot {i.exists ? 'passed' : 'failed'}"></span><span class="mono trunc">{i.ref}</span><span class="grow"></span><span class="num dim">{fmtTokens(i.tokens)}</span></div>
        {/each}
      </Section>
    {/if}
    <Section title="budget">
      <div class="kv">
        <span class="k">tokens</span><span class="num">{fmtTokens(d.budget?.tokens || 0)}</span>
        <span class="k">calls</span><span class="num">{d.budget?.calls || 0}</span>
        <span class="k">artifact</span><span class="num">{fmtTokens(d.artifact_tokens || 0)}</span>
        {#if row}
          <span class="k">spent</span><span class="num">{fmtTokens(row.tokens || 0)}</span>
        {/if}
        {#if d.promotion}<span class="k">promotion</span><span class="mono">{d.promotion}</span>{/if}
      </div>
    </Section>
    {#if d.split_proposal}
      <Section title="split"><Out text={JSON.stringify(d.split_proposal, null, 2)} lang="json" cap={40} /></Section>
    {/if}
    {#if d.manifest}
      <Section title="manifest"><Out text={JSON.stringify(d.manifest, null, 2)} lang="json" cap={30} /></Section>
    {/if}
    {#if d.audit && Object.keys(d.audit).length}
      <Section title="audit json"><Out text={JSON.stringify(d.audit, null, 2)} lang="json" cap={24} /></Section>
    {/if}
  {/if}
{/if}

<style>
  .kv {
    display: grid;
    grid-template-columns: 90px 1fr;
    gap: 3px 10px;
    font-size: var(--fs-sm);
  }
  .k {
    color: var(--fg-2);
  }
  .ph,
  .gate {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 22px;
    font-size: var(--fs-sm);
  }
  .gate .mono {
    flex: none;
    max-width: 55%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .gate .dim {
    flex: 1;
  }
  .detail {
    font-size: var(--fs-xs);
    margin-top: 4px;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .id {
    font-size: var(--fs-md);
    font-weight: 560;
  }
  .brief {
    margin: 8px 0 0;
    color: var(--fg-1);
    font-size: var(--fs-sm);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .acts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
  }
  .ask {
    display: flex;
    gap: 4px;
    margin-top: 8px;
  }
  .ask input {
    flex: 1;
  }
  .deps {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 8px;
  }
  .deps .chip:hover {
    color: var(--accent);
  }
  .li {
    font-size: var(--fs-sm);
    color: var(--fg-1);
    padding: 1px 0;
  }
</style>
