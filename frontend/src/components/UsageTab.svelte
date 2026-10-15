<script>
  import { app, live, selectNode } from "../lib/store.svelte.js";
  import { get } from "../lib/api.js";
  import { poll } from "../lib/poll.js";
  import { fmtClock, fmtTokens } from "../lib/format.js";
  import Section from "./Section.svelte";

  const run = $derived(live.current);
  let data = $state(null);
  let dim = $state("role");

  $effect(() => {
    if (!app.activeRun) return;
    return poll(async () => (data = await get("/api/cost", { run: app.activeRun })), run?.running ? 5000 : 60000);
  });

  const tot = (g) => g.prompt + g.completion + g.reasoning;
  const rows = $derived(data ? data.groups[dim] : []);
  const maxTok = $derived(Math.max(1, ...rows.map(tot)));
  const t = $derived(data?.totals);

  const series = $derived(data?.series ?? []);
  const W = 360;
  const H = 70;
  const path = $derived.by(() => {
    if (series.length < 2) return { area: "", line: "" };
    const x0 = series[0].ts;
    const x1 = series[series.length - 1].ts || x0 + 1;
    const ymax = Math.max(1, series[series.length - 1].tokens);
    const pts = series.map((s) => [((s.ts - x0) / Math.max(1, x1 - x0)) * W, H - (s.tokens / ymax) * (H - 4) - 2]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    return { line, area: `${line}L${W},${H}L0,${H}Z` };
  });
</script>

{#if t}
  <Section>
    <div class="big">
      <div><div class="num v">{fmtTokens(t.prompt + t.completion + t.reasoning)}</div></div>
      <div class="kv">
        <span class="k">prompt</span><span class="num">{fmtTokens(t.prompt)}</span>
        <span class="k">completion</span><span class="num">{fmtTokens(t.completion)}</span>
        <span class="k">reasoning</span><span class="num">{fmtTokens(t.reasoning)}</span>
        <span class="k">calls</span><span class="num">{t.calls}{t.cached ? ` (${t.cached} cached)` : ""}</span>
      </div>
    </div>
    {#if path.line}
      <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="spark" role="img" aria-label="cumulative tokens">
        <path d={path.area} fill="var(--accent-dim)" />
        <path d={path.line} fill="none" stroke="var(--accent)" stroke-width="1.5" vector-effect="non-scaling-stroke" />
      </svg>
      <div class="ax num dim"><span>{fmtClock(series[0].ts)}</span><span>{fmtClock(series[series.length - 1].ts)}</span></div>
    {/if}
  </Section>
  <div class="seg" role="tablist">
    {#each ["role", "phase", "model", "node"] as d}
      <button role="tab" aria-selected={dim === d} class:on={dim === d} onclick={() => (dim = d)}>{d}</button>
    {/each}
  </div>
  <div class="tbl">
    {#each rows as g (g.name)}
      <button class="r" disabled={dim !== "node" || g.name === "-"} onclick={() => selectNode(g.name)}>
        <span class="mono n trunc">{g.name}</span>
        <span class="bar"><i style:width="{(tot(g) / maxTok) * 100}%"></i></span>
        <span class="num dim w">{fmtTokens(tot(g))}</span>
        <span class="num dim c">{g.calls}</span>
      </button>
    {/each}
  </div>
{/if}

<style>
  .big {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 16px;
  }
  .v {
    font-size: 26px;
    font-weight: 560;
    letter-spacing: -0.03em;
  }
  .kv {
    display: grid;
    grid-template-columns: auto auto;
    gap: 2px 12px;
    font-size: var(--fs-sm);
    text-align: right;
  }
  .k {
    color: var(--fg-2);
    text-align: left;
  }
  .spark {
    width: 100%;
    height: 70px;
    margin-top: var(--sp-3);
    display: block;
  }
  .ax {
    display: flex;
    justify-content: space-between;
    font-size: 10px;
  }
  .seg {
    display: flex;
    gap: 2px;
    padding: var(--sp-2) var(--sp-3);
  }
  .seg button {
    padding: 2px 9px;
    border-radius: 11px;
    color: var(--fg-2);
    font-size: var(--fs-sm);
  }
  .seg button.on {
    background: var(--bg-3);
    color: var(--fg);
  }
  .r {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    height: 26px;
    padding: 0 var(--sp-4);
    text-align: left;
    font-size: var(--fs-sm);
  }
  .r:not(:disabled):hover {
    background: var(--bg-2);
  }
  .n {
    width: 110px;
    flex: none;
  }
  .bar {
    flex: 1;
    height: 3px;
    background: var(--bg-3);
    border-radius: 2px;
    overflow: hidden;
  }
  .bar i {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .w {
    width: 46px;
    text-align: right;
  }
  .c {
    width: 26px;
    text-align: right;
    font-size: var(--fs-xs);
  }
</style>
