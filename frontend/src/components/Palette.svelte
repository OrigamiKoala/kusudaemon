<script>
  import { Search, CornerDownLeft, Copy } from "@lucide/svelte";
  import { app, live, openRun, selectNode, toast, viewOf } from "../lib/store.svelte.js";
  import { COMMANDS, ctx, execute, fuzzy, missingParams, optionsOf } from "../lib/commands.js";
  import { HINTS } from "../lib/keys.js";
  import { clip, shortId } from "../lib/format.js";

  let input = $state("");
  let sel = $state(0);
  let step = $state(null); // { cmd, args, idx, confirm }
  let box = $state();

  const c = $derived(ctx());
  const hasRun = $derived(!!c.run?.meta?.attached);

  $effect(() => {
    if (app.palette && typeof app.palette === "object") {
      const cmd = COMMANDS.find((x) => x.id === app.palette.cmd);
      if (cmd) startStep(cmd, app.palette.args || {});
      app.palette = true;
    }
    queueMicrotask(() => box?.focus());
  });

  function startStep(cmd, args = {}) {
    const miss = missingParams(cmd, args);
    step = { cmd, args: { ...args }, idx: 0, confirm: false, miss: cmd.params || [] };
    const first = (cmd.params || []).findIndex((p) => args[p.key] === undefined || args[p.key] === "");
    step.idx = first < 0 ? (cmd.params || []).length : first;
    input = "";
    sel = 0;
    if (!miss.length && !(cmd.params || []).length) finish();
  }

  const param = $derived(step ? (step.cmd.params || [])[step.idx] : null);
  const choices = $derived.by(() => {
    if (!param) return [];
    const opts = optionsOf(param, c);
    if (!opts.length) return [];
    return opts.map((o) => ({ o, s: fuzzy(input, o) })).filter((x) => x.s >= 0).sort((a, b) => b.s - a.s).slice(0, 40).map((x) => x.o);
  });

  const items = $derived.by(() => {
    if (step) return [];
    const needle = input.trim();
    const out = [];
    for (const cmd of COMMANDS) {
      if (cmd.needsRun && !hasRun) continue;
      const s = Math.max(fuzzy(needle, cmd.title), ...cmd.alias.map((a) => fuzzy(needle, a)));
      if (s >= 0) out.push({ type: "cmd", cmd, s: s + 20 });
    }
    if (needle) {
      for (const n of c.nodes) {
        const s = Math.max(fuzzy(needle, n.id), fuzzy(needle, n.brief || "") - 30);
        if (s >= 0) out.push({ type: "node", n, s });
      }
      for (const r of app.runs) {
        const s = Math.max(fuzzy(needle, r.id), fuzzy(needle, r.goal || "") - 30);
        if (s >= 0) out.push({ type: "run", r, s });
      }
    }
    return out.sort((a, b) => b.s - a.s).slice(0, 60);
  });

  const total = $derived(step ? (param && choices.length ? choices.length : 1) : items.length);
  $effect(() => {
    input;
    sel = 0;
  });

  function close() {
    app.palette = false;
    step = null;
    input = "";
  }

  async function finish() {
    if (!step) return;
    const { cmd, args } = step;
    if (cmd.danger && !step.confirm) {
      step.confirm = true;
      return;
    }
    close();
    await execute(cmd, args);
  }

  function advance(value) {
    if (!step) return;
    if (param) {
      const v = (value ?? input).trim();
      if (!v && !param.optional) return;
      step.args[param.key] = v;
    }
    step.idx += 1;
    input = "";
    if (step.idx >= (step.cmd.params || []).length) finish();
  }

  function run(item) {
    if (item.type === "cmd") {
      if ((item.cmd.params || []).length) startStep(item.cmd);
      else {
        step = { cmd: item.cmd, args: {}, idx: 0, confirm: false };
        finish();
      }
    } else if (item.type === "node") (close(), selectNode(item.n.id));
    else (close(), openRun(item.r.id));
  }

  function onkey(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      if (step) {
        step = null;
        input = "";
      } else close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      sel = Math.min(total - 1, sel + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      sel = Math.max(0, sel - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (step) {
        if (step.idx >= (step.cmd.params || []).length) finish();
        else if (param && choices.length && (optionsOf(param, c).length)) advance(choices[sel]);
        else advance();
      } else if (items[sel]) run(items[sel]);
    }
  }

  const cliLine = $derived(step?.cmd.cli ? step.cmd.cli(step.args, c) : "");
  async function copyCli() {
    try {
      await navigator.clipboard.writeText(cliLine);
      toast("copied");
    } catch {
      /* ignore */
    }
  }
</script>

<div class="scrim" role="presentation" onmousedown={(e) => e.target === e.currentTarget && close()}>
  <div class="pal" role="dialog" aria-label="command palette">
    <div class="in">
      <Search size={15} />
      {#if step}
        <span class="chip live">{step.cmd.title}</span>
        {#each (step.cmd.params || []).slice(0, step.idx) as p}<span class="chip mono">{step.args[p.key] || "—"}</span>{/each}
      {/if}
      <input bind:this={box} bind:value={input} onkeydown={onkey} aria-label={param ? param.label : "command"} placeholder={param ? param.label : ""} spellcheck="false" />
      <kbd>esc</kbd>
    </div>
    <div class="list" role="listbox">
      {#if step}
        {#if param && choices.length}
          {#each choices as o, i (o)}
            <button role="option" aria-selected={i === sel} class:on={i === sel} onmouseenter={() => (sel = i)} onclick={() => advance(o)}><span class="mono">{o}</span></button>
          {/each}
        {:else if step.idx >= (step.cmd.params || []).length}
          <div class="done">
            {#if step.confirm}<span class="chip bad">{step.cmd.title}</span>{/if}
            <span class="grow"></span>
            <CornerDownLeft size={14} />
          </div>
        {:else}
          <div class="done dim"><CornerDownLeft size={14} /></div>
        {/if}
        {#if cliLine}
          <div class="cli"><code class="mono trunc">{cliLine}</code><button class="icon-btn sm" aria-label="copy command" onclick={copyCli}><Copy size={12} /></button></div>
        {/if}
      {:else}
        {#each items as it, i (it.type + (it.cmd?.id || it.n?.id || it.r?.id))}
          <button role="option" aria-selected={i === sel} class:on={i === sel} onmouseenter={() => (sel = i)} onclick={() => run(it)}>
            {#if it.type === "cmd"}
              <span class="g dim">{it.cmd.group}</span>
              <span class="t">{it.cmd.title}</span>
              {#if HINTS[it.cmd.id]}<kbd>{HINTS[it.cmd.id]}</kbd>{/if}
            {:else if it.type === "node"}
              <span class="dot {it.n.status === 'dispatched' ? 'running' : it.n.status}"></span>
              <span class="t mono">{it.n.id}</span>
              <span class="dim trunc">{clip(it.n.brief, 70)}</span>
            {:else}
              <span class="g dim">run</span>
              <span class="t mono">{shortId(it.r.id, 40)}</span>
              <span class="dim trunc">{clip(it.r.goal, 60)}</span>
            {/if}
          </button>
        {/each}
      {/if}
    </div>
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: var(--scrim);
    z-index: 100;
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding-top: 12vh;
    animation: fade var(--dur) var(--ease);
  }
  @keyframes fade {
    from { opacity: 0; }
  }
  .pal {
    width: min(640px, 92vw);
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: 10px;
    box-shadow: var(--pop-shadow);
    overflow: hidden;
    animation: rise 180ms var(--ease);
  }
  @keyframes rise {
    from { transform: translateY(-6px); opacity: 0; }
  }
  .in {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    height: 46px;
    border-bottom: 1px solid var(--line-soft);
    color: var(--fg-2);
  }
  .in input {
    flex: 1;
    background: none;
    border: 0;
    outline: none;
    font-size: var(--fs-lg);
    color: var(--fg);
    padding: 0;
  }
  .list {
    max-height: 52vh;
    overflow: auto;
    padding: 4px;
  }
  .list button {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    height: 32px;
    padding: 0 10px;
    border-radius: var(--r-1);
    text-align: left;
    color: var(--fg-1);
  }
  .list button.on {
    background: var(--accent-dim);
    color: var(--fg);
  }
  .g {
    width: 44px;
    font-size: var(--fs-xs);
  }
  .t {
    flex: none;
  }
  .list kbd {
    margin-left: auto;
  }
  .done {
    display: flex;
    align-items: center;
    padding: 10px;
    gap: 8px;
  }
  .cli {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 4px 6px 6px;
    padding: 4px 6px 4px 10px;
    background: var(--bg-2);
    border-radius: var(--r-1);
    color: var(--fg-1);
    font-size: var(--fs-xs);
  }
  .cli code {
    flex: 1;
  }
</style>
