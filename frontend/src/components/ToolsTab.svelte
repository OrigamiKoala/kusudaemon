<script>
  import { Play, Square, ChevronRight, ChevronDown } from "@lucide/svelte";
  import Out from "./Out.svelte";
  import { app, live, toast } from "../lib/store.svelte.js";
  import { get, post } from "../lib/api.js";
  import { poll } from "../lib/poll.js";
  import { fmtClock, fmtDur } from "../lib/format.js";

  const run = $derived(live.current);
  const control = $derived(app.caps?.control_enabled !== false);
  const FIELDS = {
    status: [{ k: "brief", t: "bool" }],
    resync: [{ k: "source", t: "text", req: true }],
    "cache-clear": [],
    eval: [{ k: "task", t: "text" }, { k: "runs", t: "number" }],
    bench: [
      { k: "goal", t: "area" }, { k: "goal_file", t: "text" }, { k: "workspace", t: "text" },
      { k: "backend", t: "select", o: () => app.caps?.backends || [] }, { k: "model", t: "text" }, { k: "arm", t: "text" },
      { k: "task_id", t: "text" }, { k: "seed", t: "number" }, { k: "tier", t: "select", o: () => ["T0", "T1", "T2", "T3"] },
      { k: "max_parallel", t: "number" }, { k: "budget_tokens", t: "number" }, { k: "output", t: "text" }, { k: "json", t: "bool" },
    ],
  };
  let cmd = $state("status");
  let args = $state({});
  let jobs = $state([]);
  let openJob = $state(null);
  let tails = $state({});

  $effect(() => {
    cmd;
    args = {};
  });
  $effect(() =>
    poll(async () => {
      jobs = (await get("/api/cli")).jobs;
      for (const j of jobs) {
        if (openJob === j.id) await tail(j);
      }
    }, 1800),
  );
  $effect(() => {
    if (app.focusJob) {
      openJob = app.focusJob;
      app.focusJob = null;
    }
  });

  async function tail(j) {
    const t = tails[j.id] || { text: "", next: 0 };
    const r = await get(`/api/cli/${j.id}`, { params: { since: t.next } });
    if (r.text || !tails[j.id]) tails = { ...tails, [j.id]: { text: t.text + r.text, next: r.next } };
  }
  async function start() {
    const missing = FIELDS[cmd].find((f) => f.req && !args[f.k]);
    if (missing) return toast(missing.k, "error");
    if (cmd === "bench" && !args.goal && !args.goal_file) return toast("goal", "error");
    try {
      const j = await post("/api/cli", { command: cmd, args, run_id: app.activeRun || "" });
      openJob = j.id;
      jobs = [j, ...jobs];
    } catch (e) {
      toast(e.message, "error", 6000);
    }
  }
  async function cancel(j) {
    try {
      await post(`/api/cli/${j.id}/cancel`);
    } catch (e) {
      toast(e.message, "error");
    }
  }
  const needsRun = $derived(cmd === "status" || cmd === "resync");
</script>

<div class="form">
  <div class="row">
    <select aria-label="command" bind:value={cmd}>
      {#each Object.keys(FIELDS) as c}<option value={c}>{c}</option>{/each}
    </select>
    <span class="grow"></span>
    <button class="btn primary" disabled={!control || (needsRun && !app.activeRun)} onclick={start}><Play size={13} /></button>
  </div>
  {#each FIELDS[cmd] as f (f.k)}
    <label class:chk={f.t === "bool"}>
      <span class="k mono">{f.k}</span>
      {#if f.t === "bool"}
        <input type="checkbox" bind:checked={args[f.k]} />
      {:else if f.t === "area"}
        <textarea rows="3" bind:value={args[f.k]}></textarea>
      {:else if f.t === "select"}
        <select bind:value={args[f.k]}>
          <option value=""></option>
          {#each f.o() as o}<option value={o}>{o}</option>{/each}
        </select>
      {:else}
        <input type={f.t === "number" ? "number" : "text"} bind:value={args[f.k]} />
      {/if}
    </label>
  {/each}
</div>

<div class="jobs">
  {#each jobs as j (j.id)}
    <div class="job">
      <button class="jh" aria-expanded={openJob === j.id} onclick={() => ((openJob = openJob === j.id ? null : j.id), openJob && tail(j))}>
        {#if openJob === j.id}<ChevronDown size={12} />{:else}<ChevronRight size={12} />{/if}
        <span class="dot {j.status === 'running' ? 'running' : j.status === 'done' ? 'passed' : j.status === 'cancelled' ? 'split' : 'failed'}"></span>
        <span class="mono">{j.command}</span>
        <span class="mono dim trunc grow">{j.run_id}</span>
        <span class="num dim">{fmtClock(j.started)}</span>
        <span class="num dim">{fmtDur((j.ended || Date.now() / 1000) - j.started)}</span>
      </button>
      {#if openJob === j.id}
        <div class="jb">
          <div class="mono dim argv trunc" title={j.argv.join(" ")}>kusudaemon {j.argv.join(" ")}</div>
          <Out text={tails[j.id]?.text || ""} cap={400} />
          {#if j.status === "running" && control}
            <button class="btn danger" onclick={() => cancel(j)}><Square size={12} /></button>
          {/if}
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .form {
    padding: var(--sp-3) var(--sp-4);
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-bottom: 1px solid var(--line-soft);
  }
  label {
    display: grid;
    grid-template-columns: 110px 1fr;
    align-items: center;
    gap: 8px;
    font-size: var(--fs-sm);
  }
  label.chk {
    grid-template-columns: 110px auto;
  }
  .k {
    color: var(--fg-2);
    font-size: var(--fs-xs);
  }
  .jh {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    height: 28px;
    padding: 0 var(--sp-4);
    font-size: var(--fs-sm);
    text-align: left;
  }
  .jh:hover {
    background: var(--bg-2);
  }
  .jb {
    padding: 4px var(--sp-4) var(--sp-3);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .argv {
    font-size: var(--fs-xs);
  }
</style>
