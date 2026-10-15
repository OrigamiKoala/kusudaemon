<script>
  import { ArrowUp, Slash } from "@lucide/svelte";
  import { app, live, viewOf, toast } from "../lib/store.svelte.js";
  import { COMMANDS, byAlias, ctx, execute, fuzzy, missingParams, parseArgs } from "../lib/commands.js";

  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  const control = $derived(app.caps?.control_enabled !== false);
  const liveAgents = $derived((run?.subagents ?? []).filter((s) => s.live));
  let target = $state("");
  const effective = $derived(
    liveAgents.find((a) => a.id === target)?.id || liveAgents.find((a) => view.node && (a.id === view.node || a.id.startsWith(view.node + "~")))?.id || liveAgents[0]?.id || "",
  );

  let text = $state("");
  let box = $state();
  let pick = $state(0);
  let busy = $state(false);

  const slash = $derived(text.startsWith("/"));
  const word = $derived(slash ? text.slice(1).split(/\s/)[0] : "");
  const hasArgs = $derived(slash && /\s/.test(text.trimEnd().slice(1)));
  const suggestions = $derived.by(() => {
    if (!slash || hasArgs) return [];
    return COMMANDS.map((c) => ({ c, s: Math.max(...c.alias.map((a) => fuzzy(word, a)), fuzzy(word, c.title)) }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => b.s - a.s)
      .slice(0, 7)
      .map((x) => x.c);
  });

  function grow() {
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 140) + "px";
  }
  $effect(() => {
    text;
    grow();
  });

  async function submit() {
    const t = text.trim();
    if (!t || busy || !run) return;
    busy = true;
    try {
      if (slash) {
        const [w, ...rest] = t.slice(1).split(/\s+/);
        const cmd = (suggestions[pick] && !hasArgs && !byAlias(w) ? suggestions[pick] : null) || byAlias(w);
        if (!cmd) return toast(`/${w}`, "error");
        const args = parseArgs(cmd, rest.join(" "));
        const miss = missingParams(cmd, args);
        if (miss.length) {
          app.palette = { cmd: cmd.id, args };
          text = "";
          return;
        }
        if (cmd.danger && !confirm(cmd.title + "?")) return;
        if (await execute(cmd, args)) text = "";
      } else {
        if (!effective) return toast("no live agent", "error");
        if (await run.interject(effective, t)) text = "";
      }
    } finally {
      busy = false;
    }
  }

  function onkey(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    } else if (suggestions.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      pick = (pick + (e.key === "ArrowDown" ? 1 : -1) + suggestions.length) % suggestions.length;
    } else if (suggestions.length && e.key === "Tab") {
      e.preventDefault();
      text = "/" + suggestions[pick].alias[0] + " ";
    } else if (e.key === "Escape") {
      text = "";
    }
  }
  export function focus() {
    box?.focus();
  }
</script>

<div class="comp">
  {#if suggestions.length}
    <div class="sug" role="listbox">
      {#each suggestions as c, i (c.id)}
        <button role="option" aria-selected={i === pick} class:on={i === pick} onmousedown={(e) => (e.preventDefault(), (text = "/" + c.alias[0] + " "), box.focus())}>
          <span class="mono">/{c.alias[0]}</span><span class="dim">{c.title}</span>
        </button>
      {/each}
    </div>
  {/if}
  <div class="row">
    {#if slash}<span class="ic"><Slash size={14} /></span>{:else if effective}
      {#if liveAgents.length > 1}
        <select aria-label="target agent" value={effective} onchange={(e) => (target = e.currentTarget.value)}>
          {#each liveAgents as a (a.id)}<option value={a.id}>{a.id}</option>{/each}
        </select>
      {:else}
        <span class="chip live mono">{effective}</span>
      {/if}
    {/if}
    <textarea
      bind:this={box}
      bind:value={text}
      rows="1"
      aria-label="message"
      disabled={!control || !run}
      onkeydown={onkey}
      spellcheck="false"
    ></textarea>
    <button class="send" aria-label="send" disabled={!text.trim() || busy || !control} onclick={submit}><ArrowUp size={15} /></button>
  </div>
</div>

<style>
  .comp {
    position: relative;
    flex: none;
    padding: var(--sp-2) var(--sp-4) var(--sp-3);
    border-top: 1px solid var(--line-soft);
    background: var(--bg);
  }
  .row {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    background: var(--bg-2);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-2);
    padding: 5px 5px 5px 10px;
    transition: border-color var(--dur) var(--ease);
  }
  .row:focus-within {
    border-color: var(--accent);
  }
  textarea {
    flex: 1;
    background: none;
    border: 0;
    padding: 3px 0;
    resize: none;
    outline: none;
    line-height: 1.5;
    max-height: 140px;
  }
  .ic {
    color: var(--accent);
    padding-bottom: 5px;
  }
  .chip {
    margin-bottom: 3px;
  }
  select {
    height: 22px;
    font-size: var(--fs-xs);
    margin-bottom: 2px;
  }
  .send {
    width: 26px;
    height: 26px;
    border-radius: var(--r-1);
    background: var(--accent);
    color: var(--accent-fg);
    display: grid;
    place-items: center;
    flex: none;
  }
  .send:disabled {
    background: var(--bg-3);
    color: var(--fg-2);
    cursor: default;
  }
  .sug {
    position: absolute;
    left: var(--sp-4);
    right: var(--sp-4);
    bottom: calc(100% - 6px);
    background: var(--bg-3);
    border: 1px solid var(--line);
    border-radius: var(--r-2);
    box-shadow: var(--pop-shadow);
    padding: 4px;
    z-index: 20;
  }
  .sug button {
    display: flex;
    gap: 12px;
    width: 100%;
    padding: 4px 8px;
    border-radius: var(--r-1);
    text-align: left;
    font-size: var(--fs-sm);
  }
  .sug button.on {
    background: var(--accent-dim);
  }
</style>
