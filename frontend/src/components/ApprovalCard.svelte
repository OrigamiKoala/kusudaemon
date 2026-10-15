<script>
  import { TriangleAlert, ChevronDown, ChevronRight } from "@lucide/svelte";
  import Diff from "./Diff.svelte";
  import { app, live, selectNode } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { diffTexts } from "../lib/diff.js";
  import { fmtStamp } from "../lib/format.js";

  let { a } = $props();
  const run = $derived(live.current);
  const control = $derived(app.caps?.control_enabled !== false);
  const pending = $derived(a.status === "pending");
  const ctx = $derived(a.context || {});

  let input = $state("");
  let answers = $state({});
  let triageOpen = $state("");
  let busy = $state(false);
  let showMsg = $state(false);

  // pilot editor
  let pilot = $state(null);
  let draft = $state("");
  $effect(() => {
    if (a.kind !== "pilot" || !pending || !ctx.node_id || pilot) return;
    get(`/api/node/${encodeURIComponent(ctx.node_id)}`, { run: app.activeRun }).then((d) => {
      pilot = d;
      draft = d.artifact || "";
    });
  });
  const pilotFiles = $derived(pilot?.pilot_original != null ? [diffTexts(ctx.node_id || "pilot", pilot.pilot_original, draft)] : []);

  async function resolve(payload, ok) {
    busy = true;
    await run.resolveApproval(a, payload, ok);
    busy = false;
  }
  async function savePilot() {
    busy = true;
    await run.pilotSave(a, ctx.node_id, draft);
    busy = false;
  }
  const triageNodes = (cls) => Object.entries(ctx.triage || {}).filter(([, r]) => (r.classification || r.class || "regenerate") === cls).map(([id]) => id);
  const msgLong = $derived((a.message || "").length > 360);
</script>

<article class="ap" class:pending>
  <header>
    <span class="chip {pending ? 'wait' : ''}">{a.kind}</span>
    <span class="ti trunc">{a.title}</span>
    <span class="num dim">{fmtStamp(a.resolved_at || a.created_at)}</span>
  </header>

  {#if a.message}
    <div class="msg" class:clamp={msgLong && !showMsg}>{a.message}</div>
    {#if msgLong}
      <button class="more" aria-label="toggle message" onclick={() => (showMsg = !showMsg)}>
        {#if showMsg}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
      </button>
    {/if}
  {/if}

  {#each ctx.objections || [] as o}
    <div class="obj">
      <TriangleAlert size={13} />
      <div>
        <div>{o.claim}</div>
        {#if o.why}<div class="dim">{o.why}</div>{/if}
        {#if (o.options || []).length}<div class="dim mono">{o.options.join(" · ")}</div>{/if}
      </div>
    </div>
  {/each}

  {#if pending && a.kind === "triage" && ctx.counts}
    <div class="tri">
      {#each ["clean", "patchable", "regenerate"].filter((c) => ctx.counts[c] !== undefined) as c}
        <button class="chip {c === 'clean' ? 'ok' : c === 'regenerate' ? 'bad' : 'live'}" onclick={() => (triageOpen = triageOpen === c ? "" : c)}>
          <span class="num">{ctx.counts[c]}</span>{c}
        </button>
      {/each}
    </div>
    {#if triageOpen}
      <div class="nodes">
        {#each triageNodes(triageOpen) as id}<button class="mono" onclick={() => selectNode(id)}>{id}</button>{/each}
      </div>
    {/if}
  {/if}

  {#if pending && a.kind === "pilot" && pilot?.pilot_original != null}
    <div class="pilot">
      <textarea rows="10" aria-label="pilot artifact" bind:value={draft} spellcheck="false"></textarea>
      {#if pilotFiles[0]?.hunks.length}<Diff files={pilotFiles} compact />{/if}
    </div>
  {/if}

  {#if pending && control}
    {#if (a.questions || []).length}
      <div class="qs">
        {#each a.questions as q (q.id)}
          <label>
            <span>{q.text || q.id}</span>
            <input type="text" placeholder={q.default_assumption || ""} bind:value={answers[q.id]} />
          </label>
        {/each}
      </div>
    {:else if a.allow_input && a.kind !== "pilot"}
      <label class="free">
        <span class="dim">{a.input_label}</span>
        <textarea rows="2" bind:value={input}></textarea>
      </label>
    {/if}
    <div class="acts">
      {#each a.options || [] as opt (opt.value)}
        <button class="btn" class:primary={opt.style === "primary"} class:danger={opt.style === "danger"} disabled={busy} onclick={() => resolve({ action: opt.value })}>{opt.label}</button>
      {/each}
      {#if (a.questions || []).length}
        <button class="btn primary" disabled={busy} onclick={() => resolve({ action: "answer", answers: Object.fromEntries(a.questions.map((q) => [q.id, (answers[q.id] || "").trim()])) })}>{a.questions.length}</button>
        <button class="btn" disabled={busy} onclick={() => resolve({ action: "answer", answers: {} })}>default</button>
      {:else if a.kind === "pilot"}
        <button class="btn primary" disabled={busy || !pilot} onclick={savePilot}>save</button>
        <button class="btn" disabled={busy} onclick={() => resolve({ action: "approve" })}>approve</button>
      {:else if a.allow_input}
        <button class="btn primary" disabled={busy} onclick={() => resolve({ action: "answer", user_input: input })}>send</button>
        <button class="btn" disabled={busy} onclick={() => resolve({ action: "answer", user_input: "" })}>default</button>
      {/if}
    </div>
  {:else if !pending}
    <div class="res dim">
      {#if a.answers && Object.keys(a.answers).length}
        {#each Object.entries(a.answers) as [k, v]}
          <div><span class="mono">{(a.questions || []).find((q) => q.id === k)?.text || k}</span> {v}</div>
        {/each}
      {:else if a.user_input}
        <div>{a.user_input}</div>
      {:else}
        <span class="mono">{a.action}</span>
      {/if}
    </div>
  {/if}
</article>

<style>
  .ap {
    padding: var(--sp-3) var(--sp-4);
    border-bottom: 1px solid var(--line-soft);
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
  }
  .ap.pending {
    background: color-mix(in oklch, var(--wait) 7%, transparent);
  }
  header {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
  }
  .ti {
    flex: 1;
    font-weight: 560;
  }
  .msg {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--fg-1);
    font-size: var(--fs-sm);
  }
  .msg.clamp {
    display: -webkit-box;
    -webkit-line-clamp: 4;
    line-clamp: 4;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .more {
    align-self: flex-start;
    color: var(--fg-2);
  }
  .obj {
    display: flex;
    gap: 8px;
    color: var(--warn);
    font-size: var(--fs-sm);
  }
  .obj .dim {
    color: var(--fg-2);
  }
  .tri,
  .nodes,
  .acts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .nodes button {
    color: var(--accent);
    font-size: var(--fs-xs);
  }
  .qs {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: var(--fs-sm);
  }
  .pilot {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .pilot textarea {
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    min-height: 160px;
  }
  .res {
    font-size: var(--fs-sm);
  }
</style>
