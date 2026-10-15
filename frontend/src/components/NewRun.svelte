<script>
  import { X, Play, ChevronDown, ChevronRight, Loader } from "@lucide/svelte";
  import { app, startRun, toast } from "../lib/store.svelte.js";

  const backends = $derived(app.caps?.backends || ["gptme", "claude", "codex", "opencode", "antigravity"]);
  const f = $state({
    goal: "", run_id: "", source: "", workspace: "", backend: "gptme", model: "", compile_command: "", tier_override: "",
    dispatch_policy: "orchestrator", survey_mode: "auto", max_rounds: "", max_attempts: "", max_parallel: "",
    document_review: false, inline_spans: true, auto_probe_plan: true, disable_review: false, no_intake: false, episode_cache: true,
    budget_tokens: "", max_cost_usd: "", wall_clock_budget: "", output_dir: "", research_plan: "", provider: "",
  });
  let more = $state(false);
  let busy = $state(false);

  const models = $derived(app.models.by_backend?.[f.backend] || []);
  const defaultModel = $derived(app.models.default_by_backend?.[f.backend] || "");

  async function submit() {
    if (busy) return;
    const body = {};
    for (const [k, v] of Object.entries(f)) {
      if (v === "" || v === null) continue;
      body[k] = v;
    }
    for (const k of ["max_rounds", "max_attempts", "max_parallel", "budget_tokens", "wall_clock_budget"]) if (body[k] !== undefined) body[k] = parseInt(body[k], 10);
    if (body.max_cost_usd !== undefined) body.max_cost_usd = parseFloat(body.max_cost_usd);
    if (body.tier_override) body.tier_floor = body.tier_override;
    if (!body.goal && !body.run_id) return toast("goal", "error");
    busy = true;
    try {
      await startRun(body);
      app.newRun = false;
    } catch (e) {
      toast(e.message, "error", 7000);
    } finally {
      busy = false;
    }
  }
</script>

<div class="scrim" role="presentation" onmousedown={(e) => e.target === e.currentTarget && (app.newRun = false)}>
  <form class="sheet" aria-label="new run" onsubmit={(e) => (e.preventDefault(), submit())}>
    <header>
      <span class="grow"></span>
      <button type="button" class="icon-btn" aria-label="close" onclick={() => (app.newRun = false)}><X size={15} /></button>
    </header>
    <div class="body">
      <label class="full"><span>goal</span><textarea rows="6" bind:value={f.goal} spellcheck="true"></textarea></label>
      <label><span>run id</span><input type="text" bind:value={f.run_id} /></label>
      <label><span>source</span><input type="text" bind:value={f.source} /></label>
      <label class="full"><span>workspace</span><input type="text" bind:value={f.workspace} /></label>
      <label><span>backend</span>
        <select bind:value={f.backend} onchange={() => (f.model = "")}>{#each backends as b}<option value={b}>{b}</option>{/each}</select>
      </label>
      <label><span>model</span>
        <select bind:value={f.model}><option value="">{defaultModel}</option>{#each models as m}<option value={m}>{m}</option>{/each}</select>
      </label>
      <label><span>tier floor</span>
        <select bind:value={f.tier_override}><option value=""></option>{#each ["T0", "T1", "T2", "T3"] as t}<option value={t}>{t}</option>{/each}</select>
      </label>
      <label><span>max parallel</span><input type="number" min="1" bind:value={f.max_parallel} /></label>
      <label class="full"><span>compile command</span><input type="text" bind:value={f.compile_command} /></label>

      <div class="flags full">
        <label class="chk"><input type="checkbox" bind:checked={f.document_review} /><span>document review</span></label>
        <label class="chk"><input type="checkbox" bind:checked={f.disable_review} /><span>disable review</span></label>
        <label class="chk"><input type="checkbox" bind:checked={f.no_intake} /><span>no intake</span></label>
        <label class="chk"><input type="checkbox" bind:checked={f.inline_spans} /><span>inline spans</span></label>
        <label class="chk"><input type="checkbox" bind:checked={f.auto_probe_plan} /><span>auto probe plan</span></label>
        <label class="chk"><input type="checkbox" bind:checked={f.episode_cache} /><span>episode cache</span></label>
      </div>

      <button type="button" class="more full" aria-expanded={more} onclick={() => (more = !more)}>
        {#if more}<ChevronDown size={13} />{:else}<ChevronRight size={13} />{/if}
      </button>
      {#if more}
        <label><span>dispatch policy</span>
          <select bind:value={f.dispatch_policy}>{#each app.caps?.dispatch_policies || ["orchestrator", "model", "document_order"] as p}<option value={p}>{p}</option>{/each}</select>
        </label>
        <label><span>survey mode</span>
          <select bind:value={f.survey_mode}>{#each app.caps?.survey_modes || ["auto", "deterministic", "structural", "model"] as p}<option value={p}>{p}</option>{/each}</select>
        </label>
        <label><span>max rounds</span><input type="number" min="1" bind:value={f.max_rounds} /></label>
        <label><span>max attempts</span><input type="number" min="1" bind:value={f.max_attempts} /></label>
        <label><span>budget tokens</span><input type="number" min="0" bind:value={f.budget_tokens} /></label>
        <label><span>max cost usd</span><input type="number" min="0" step="0.01" bind:value={f.max_cost_usd} /></label>
        <label><span>wall clock s</span><input type="number" min="0" bind:value={f.wall_clock_budget} /></label>
        <label><span>provider</span><input type="text" bind:value={f.provider} /></label>
        <label class="full"><span>output dir</span><input type="text" bind:value={f.output_dir} /></label>
        <label class="full"><span>research plan</span><textarea rows="3" bind:value={f.research_plan} class="mono"></textarea></label>
      {/if}
    </div>
    <footer>
      <button class="btn primary" type="submit" disabled={busy}>
        {#if busy}<Loader size={13} class="spin" />{:else}<Play size={13} />{/if}
      </button>
    </footer>
  </form>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 90;
    background: var(--scrim);
    display: flex;
    justify-content: flex-end;
    animation: fade var(--dur) var(--ease);
  }
  @keyframes fade {
    from {
      opacity: 0;
    }
  }
  .sheet {
    width: min(500px, 100vw);
    height: 100%;
    background: var(--bg-1);
    border-left: 1px solid var(--line);
    display: flex;
    flex-direction: column;
    animation: slide 220ms var(--ease);
    box-shadow: var(--pop-shadow);
  }
  @keyframes slide {
    from {
      transform: translateX(24px);
      opacity: 0;
    }
  }
  header {
    display: flex;
    height: var(--bar-h);
    align-items: center;
    padding: 0 var(--sp-2);
    border-bottom: 1px solid var(--line-soft);
  }
  .body {
    flex: 1;
    overflow: auto;
    padding: var(--sp-4);
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    align-content: start;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  label > span {
    font-size: var(--fs-xs);
    color: var(--fg-2);
  }
  .full {
    grid-column: 1 / -1;
  }
  .flags {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 12px;
  }
  label.chk {
    flex-direction: row;
    align-items: center;
    gap: 8px;
  }
  label.chk > span {
    font-size: var(--fs-sm);
    color: var(--fg-1);
  }
  .more {
    display: flex;
    justify-content: center;
    color: var(--fg-2);
    padding: 2px;
    border-top: 1px solid var(--line-soft);
  }
  footer {
    padding: var(--sp-3) var(--sp-4);
    border-top: 1px solid var(--line-soft);
    display: flex;
    justify-content: flex-end;
  }
  footer .btn {
    height: 32px;
    padding: 0 22px;
  }
</style>
