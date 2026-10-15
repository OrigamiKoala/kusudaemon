<script>
  import { FileText, FilePlus2, Pencil, Terminal, Search, Globe, ListChecks, Workflow, Wrench, Check, X, Loader, ChevronRight, Minus } from "@lucide/svelte";
  import Diff from "./Diff.svelte";
  import Out from "./Out.svelte";
  import { highlight, langForPath } from "../lib/markdown.js";
  import { inputCommand, inputPath, toolDiffFiles, toolTitle } from "../lib/trace.js";
  import { parseUnified } from "../lib/diff.js";
  import { fmtMs } from "../lib/format.js";

  let { item, forceOpen = false } = $props();
  let open = $state(false);

  const ICON = { read: FileText, write: FilePlus2, edit: Pencil, bash: Terminal, search: Search, web: Globe, todo: ListChecks, task: Workflow, other: Wrench };
  const Icon = $derived(ICON[item.toolKind] || Wrench);
  const title = $derived(toolTitle(item.name, item.input));
  const diffOf = () => toolDiffFiles(item.name, item.input) || (item.serverDiff ? parseUnified(item.serverDiff) : null);
  const files = $derived(open || forceOpen ? diffOf() : null);
  const stat = $derived.by(() => {
    if (item.toolKind !== "edit" && item.toolKind !== "write") return null;
    const f = diffOf();
    if (!f || !f.length) return null;
    return { added: f.reduce((a, x) => a + x.added, 0), deleted: f.reduce((a, x) => a + x.deleted, 0) };
  });
  const pending = $derived(!item.done);
  const isOpen = $derived(open || forceOpen);
  const todos = $derived(item.toolKind === "todo" && Array.isArray(item.input?.todos) ? item.input.todos : null);
  const path = $derived(item.input ? inputPath(item.input) : "");
  const rawInput = $derived(item.input ? JSON.stringify(item.input, null, 2) : "");
</script>

<div class="tool" class:err={item.error} class:pending class:unk={item.unknown}>
  <button class="head" aria-expanded={isOpen} onclick={() => (open = !open)}>
    <span class="chev" class:rot={isOpen}><ChevronRight size={12} /></span>
    <span class="ic {item.toolKind}"><Icon size={13} /></span>
    <span class="nm">{item.name}</span>
    <span class="ti mono trunc">{title}</span>
    {#if item.input?._truncated}<span class="chip">…</span>{/if}
    {#if stat}
      <span class="add num">+{stat.added}</span><span class="del num">−{stat.deleted}</span>
    {/if}
    <span class="grow"></span>
    {#if item.exit != null && item.exit !== 0}<span class="num bad">{item.exit}</span>{/if}
    {#if item.durationMs}<span class="num dim">{fmtMs(item.durationMs)}</span>{/if}
    <span class="state">
      {#if pending}<Loader size={12} class="spin" />{:else if item.error}<X size={12} />{:else if item.unknown}<Minus size={12} />{:else}<Check size={12} />{/if}
    </span>
  </button>
  {#if isOpen}
    <div class="body">
      {#if item.toolKind === "bash"}
        <Out text={String(inputCommand(item.input) ?? "")} lang="bash" cap={40} />
      {/if}
      {#if files}
        <Diff {files} compact />
      {:else if todos}
        <ul class="todos">
          {#each todos as t}
            <li class={t.status}><span class="box"></span>{t.content}</li>
          {/each}
        </ul>
      {:else if item.toolKind === "other" || item.toolKind === "task" || item.toolKind === "web"}
        {#if rawInput}<Out text={rawInput} lang="json" cap={30} />{/if}
      {/if}
      {#if item.result != null && !todos && !(files && !item.error)}
        <Out text={item.result} lang={item.toolKind === "read" ? langForPath(path) : ""} cap={item.toolKind === "bash" ? 120 : 80} />
      {:else if pending && !files}
        <div class="dim wait">…</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .tool {
    border-radius: var(--r-2);
    margin: 1px 0;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 7px;
    width: 100%;
    height: 26px;
    padding: 0 8px 0 2px;
    border-radius: var(--r-1);
    text-align: left;
    color: var(--fg-1);
  }
  .head:hover {
    background: var(--bg-2);
  }
  .chev {
    display: grid;
    place-items: center;
    color: var(--fg-2);
    transition: transform var(--dur) var(--ease);
  }
  .chev.rot {
    transform: rotate(90deg);
  }
  .ic {
    display: inline-flex;
    color: var(--fg-2);
  }
  .ic.edit, .ic.write { color: var(--accent); }
  .ic.bash { color: var(--wait); }
  .nm {
    font-weight: 540;
    color: var(--fg);
    font-size: var(--fs-sm);
  }
  .ti {
    color: var(--fg-1);
    font-size: var(--fs-sm);
    flex: 0 1 auto;
  }
  .add { color: var(--add-fg); font-size: var(--fs-xs); }
  .del { color: var(--del-fg); font-size: var(--fs-xs); }
  .bad { color: var(--bad); font-size: var(--fs-xs); }
  .state {
    display: inline-flex;
    color: var(--ok);
  }
  .err .state { color: var(--bad); }
  .unk .state { color: var(--fg-2); }
  .pending .state { color: var(--accent); }
  :global(.spin) {
    animation: spin 1.1s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  .body {
    margin: 2px 0 8px 22px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .todos {
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: var(--fs-sm);
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .todos li {
    display: flex;
    align-items: baseline;
    gap: 8px;
    color: var(--fg-1);
  }
  .todos .box {
    width: 9px;
    height: 9px;
    border: 1px solid var(--fg-2);
    border-radius: 2px;
    flex: none;
    transform: translateY(1px);
  }
  .todos li.completed { color: var(--fg-2); text-decoration: line-through; }
  .todos li.completed .box { background: var(--ok); border-color: var(--ok); }
  .todos li.in_progress .box { border-color: var(--accent); background: var(--accent-dim); }
  .wait { padding: 2px 4px; }
</style>
