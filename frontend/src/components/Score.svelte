<script>
  import { ZoomIn, ZoomOut, Radio, Maximize2 } from "@lucide/svelte";
  import { app, live, selectNode, viewOf } from "../lib/store.svelte.js";
  import { buildScore } from "../lib/score.js";
  import { fmtClock, fmtDur } from "../lib/format.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  const model = $derived(buildScore(run?.events ?? [], run?.tree ?? [], { now: run?.now ?? 0, running: !!run?.running }));
  const span = $derived(Math.max(1, model.t1 - model.t0));

  let zoom = $state(1);
  let follow = $state(true);
  let scroller = $state();
  let tip = $state(null);

  const pos = (t) => ((t - model.t0) / span) * 100;
  const treeIds = $derived(new Set((run?.tree ?? []).map((n) => n.id)));

  const STEPS = [10, 30, 60, 300, 600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 3 * 86400];
  const ticks = $derived.by(() => {
    const target = 7 * zoom;
    const step = STEPS.find((s) => span / s <= target) || STEPS[STEPS.length - 1];
    const out = [];
    const first = Math.ceil(model.t0 / step) * step;
    for (let t = first; t <= model.t1; t += step) out.push(t);
    return out.slice(0, 200);
  });

  function setZoom(z) {
    zoom = Math.max(1, Math.min(64, z));
  }
  function wheel(e) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setZoom(e.deltaY < 0 ? zoom * 1.4 : zoom / 1.4);
  }
  $effect(() => {
    model.t1;
    zoom;
    if (follow && scroller && zoom > 1) requestAnimationFrame(() => scroller && (scroller.scrollLeft = scroller.scrollWidth));
  });

  function showTip(e, lines) {
    const r = e.currentTarget.getBoundingClientRect();
    tip = { x: Math.min(r.left, innerWidth - 260), y: r.bottom + 6, lines };
  }
  function epTip(e, ep) {
    showTip(e, [`#${ep.attempt} ${ep.status}`, `${fmtClock(ep.start)} → ${fmtClock(ep.end)}`, fmtDur(ep.end - ep.start), ep.error || ""].filter(Boolean));
  }
  function markTip(e, m) {
    showTip(e, [m.type, fmtClock(m.ts), m.detail].filter(Boolean));
  }
</script>

<div class="score">
  <div class="tb">
    <button class="icon-btn sm" aria-label="zoom out" disabled={zoom <= 1} onclick={() => setZoom(zoom / 1.6)}><ZoomOut size={13} /></button>
    <span class="num dim z">{zoom === 1 ? "fit" : zoom.toFixed(1) + "×"}</span>
    <button class="icon-btn sm" aria-label="zoom in" onclick={() => setZoom(zoom * 1.6)}><ZoomIn size={13} /></button>
    <button class="icon-btn sm" aria-label="fit" disabled={zoom === 1} onclick={() => setZoom(1)}><Maximize2 size={13} /></button>
    <button class="icon-btn sm" aria-label="follow" aria-pressed={follow} onclick={() => (follow = !follow)}><Radio size={13} /></button>
    <span class="grow"></span>
    <span class="num dim">{fmtClock(model.t0)}</span>
    <span class="dim">→</span>
    <span class="num dim">{fmtDur(span)}</span>
  </div>

  {#if model.empty}
    <div class="empty"></div>
  {:else}
    <div class="body">
      <div class="labels">
        <div class="lh"></div>
        <div class="lh2"></div>
        {#each model.lanes as l (l.id)}
          {@const row = run.node(l.id)}
          <button class="lab" class:sel={l.id === view.node} class:extra={!treeIds.has(l.id)} onclick={() => selectNode(l.id)}>
            <span class="dot {row ? (row.status === 'dispatched' ? 'running' : row.status) : run.isLive(l.id) ? 'running' : 'pending'}"></span>
            <span class="mono trunc">{l.id}</span>
            {#if l.episodes.length > 1}<span class="num dim">×{l.episodes.length}</span>{/if}
          </button>
        {/each}
      </div>
      <div class="scroller" bind:this={scroller} onwheel={wheel} onscroll={(e) => {
        const el = e.currentTarget;
        follow = follow && el.scrollWidth - el.scrollLeft - el.clientWidth < 8;
      }}>
        <div class="plot" style:width="{zoom * 100}%">
          <div class="axis">
            {#each ticks as t (t)}
              <span class="tick num" style:left="{pos(t)}%">{fmtClock(t)}</span>
            {/each}
          </div>
          <div class="phases">
            {#each model.phases as p, i (i)}
              <span class="pspan {p.status}" style:left="{pos(p.start)}%" style:width="{Math.max(0.3, pos(p.end ?? model.t1) - pos(p.start))}%" title="{p.phase} · {fmtDur((p.end ?? model.t1) - p.start)}">
                <span class="trunc">{p.phase}</span>
              </span>
            {/each}
          </div>
          <div class="grid">
            {#each ticks as t (t)}<i style:left="{pos(t)}%"></i>{/each}
          </div>
          {#each model.lanes as l (l.id)}
            <div class="lane" class:sel={l.id === view.node}>
              {#each l.episodes as ep, i (i)}
                <button
                  class="bar {ep.status}"
                  aria-label="{l.id} #{ep.attempt}"
                  style:left="{pos(ep.start)}%"
                  style:width="max(3px, {pos(ep.end) - pos(ep.start)}%)"
                  onclick={() => selectNode(l.id)}
                  onmouseenter={(e) => epTip(e, ep)}
                  onmouseleave={() => (tip = null)}
                ></button>
              {/each}
              {#each l.marks as m, i (i)}
                <span class="mk {m.tone}" role="img" aria-label={m.type} style:left="{pos(m.ts)}%" onmouseenter={(e) => markTip(e, m)} onmouseleave={() => (tip = null)}></span>
              {/each}
            </div>
          {/each}
          {#if run.running}<div class="now" style:left="{pos(model.t1)}%"></div>{/if}
        </div>
      </div>
    </div>
  {/if}
  {#if tip}
    <div class="tip" style:left="{tip.x}px" style:top="{tip.y}px">
      {#each tip.lines as l, i}<div class:mono={i === 0}>{l}</div>{/each}
    </div>
  {/if}
</div>

<style>
  .score {
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
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
    font-size: var(--fs-xs);
  }
  .z {
    min-width: 34px;
    text-align: center;
  }
  .body {
    display: flex;
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
  }
  .labels {
    width: 148px;
    flex: none;
    position: sticky;
    left: 0;
    z-index: 3;
    background: var(--bg);
    border-right: 1px solid var(--line-soft);
  }
  .lh {
    height: 18px;
  }
  .lh2 {
    height: 18px;
  }
  .lab {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    height: 22px;
    padding: 0 8px 0 var(--sp-4);
    color: var(--fg-1);
    font-size: var(--fs-xs);
    text-align: left;
  }
  .lab:hover {
    background: var(--bg-2);
  }
  .lab.sel {
    background: var(--accent-dim);
    color: var(--fg);
  }
  .lab.extra {
    color: var(--fg-2);
  }
  .lab .mono {
    flex: 1;
  }
  .scroller {
    flex: 1;
    min-width: 0;
    overflow-x: auto;
    overflow-y: hidden;
  }
  .plot {
    position: relative;
    min-width: 100%;
    padding-right: 2px;
  }
  .axis,
  .phases {
    position: relative;
    height: 18px;
  }
  .tick {
    position: absolute;
    top: 3px;
    transform: translateX(-50%);
    font-size: 10px;
    color: var(--fg-2);
    white-space: nowrap;
  }
  .pspan {
    position: absolute;
    top: 2px;
    height: 14px;
    border-radius: 2px;
    background: var(--bg-3);
    color: var(--fg-1);
    font-size: 10px;
    padding: 0 5px;
    line-height: 14px;
    overflow: hidden;
    min-width: 2px;
  }
  .pspan.running { background: var(--accent-dim); color: var(--accent); }
  .pspan.failed { background: var(--bad-dim); color: var(--bad); }
  .pspan.skipped { background: transparent; border: 1px dashed var(--line); color: var(--fg-2); }
  .grid {
    position: absolute;
    top: 36px;
    bottom: 0;
    left: 0;
    right: 0;
    pointer-events: none;
  }
  .grid i {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--line-soft);
    opacity: 0.6;
  }
  .lane {
    position: relative;
    height: 22px;
    border-bottom: 1px solid var(--line-soft);
  }
  .lane.sel {
    background: var(--accent-dim);
  }
  .bar {
    position: absolute;
    top: 6px;
    height: 10px;
    border-radius: 2px;
    background: var(--ok);
    opacity: 0.78;
    transition: opacity var(--dur) var(--ease);
  }
  .bar:hover {
    opacity: 1;
  }
  .bar.running {
    background: var(--accent);
    opacity: 1;
    animation: breathe 1.8s var(--ease) infinite;
  }
  .bar.error, .bar.failed {
    background: var(--bad);
  }
  .bar.timeout, .bar.cut {
    background: repeating-linear-gradient(135deg, var(--bad) 0 3px, color-mix(in oklch, var(--bad) 40%, transparent) 3px 6px);
  }
  .bar.interrupted {
    background: var(--fg-2);
  }
  @keyframes breathe {
    50% { opacity: 0.6; }
  }
  .mk {
    position: absolute;
    top: 3px;
    width: 2px;
    height: 16px;
    margin-left: -1px;
    background: var(--fg-2);
    border-radius: 1px;
    z-index: 1;
  }
  .mk.bad { background: var(--bad); }
  .mk.warn { background: var(--warn); }
  .mk.ok { background: var(--ok); }
  .mk.wait { background: var(--wait); }
  .now {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 1px;
    background: var(--accent);
    z-index: 2;
    pointer-events: none;
  }
  .tip {
    position: fixed;
    z-index: 50;
    background: var(--bg-3);
    border: 1px solid var(--line);
    border-radius: var(--r-2);
    padding: 6px 9px;
    font-size: var(--fs-xs);
    max-width: 280px;
    box-shadow: var(--pop-shadow);
    pointer-events: none;
    overflow-wrap: anywhere;
  }
  .empty {
    flex: 1;
  }
</style>
