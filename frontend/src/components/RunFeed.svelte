<script>
  import FeedList from "./FeedList.svelte";
  import { live } from "../lib/store.svelte.js";
  import { buildFeed } from "../lib/trace.js";

  // `node` narrows the run feed to one node's events (a node that has no agent yet)
  let { node = "" } = $props();
  const run = $derived(live.current);
  const phaseAgents = $derived(node ? [] : (run?.subagents ?? []).filter((s) => s.kind === "phase" || s.id.startsWith("phase-")).map((s) => s.id));

  const phaseKey = $derived(phaseAgents.join("|"));
  $effect(() => {
    if (!run) return;
    const ids = phaseKey ? phaseKey.split("|") : [];
    const releases = ids.map((id) => run.watch(id));
    return () => releases.forEach((r) => r());
  });

  const items = $derived.by(() => {
    if (!run) return [];
    const out = [];
    let seq = 0;
    for (const e of run.events) {
      if (node && e.node_id !== node) continue;
      if (e.type === "heartbeat" || e.type === "session_captured" || e.type === "capability_unavailable") continue;
      out.push({ kind: "event", key: `e${seq++}`, ts: e.ts, type: e.type, node: e.node_id, detail: e.detail || e.reason || e.phase || e.status || (e.unmet ? e.unmet.join(", ") : "") || e.message || "" });
    }
    for (const id of phaseAgents) {
      const f = run.feeds[id];
      if (!f) continue;
      for (const it of buildFeed(f.entries, { live: !!run.agent(id)?.live })) out.push({ ...it, key: `${id}:${it.key}`, phase: id });
    }
    // order by time; undated items inherit the previous item's time
    let last = 0;
    const dated = out.map((it, i) => {
      const t = it.ts || last;
      last = t || last;
      return { it, t, i };
    });
    dated.sort((a, b) => a.t - b.t || a.i - b.i);
    return dated.map((d) => d.it);
  });
</script>

<FeedList {items} streaming={phaseAgents.some((id) => run?.agent(id)?.live)} kinds={["thinking", "tool", "assistant", "event"]}>
  {#snippet head()}<span class="who mono">{node || "run"}</span>{/snippet}
</FeedList>

<style>
  .who {
    font-size: var(--fs-sm);
    color: var(--fg);
  }
</style>
