<script>
  import { Brain, CircleUser, TriangleAlert, Bot, ChevronDown } from "@lucide/svelte";
  import Tool from "./Tool.svelte";
  import Out from "./Out.svelte";
  import Diff from "./Diff.svelte";
  import { parseUnified } from "../lib/diff.js";
  import { renderMarkdown } from "../lib/markdown.js";
  import { clip, fmtClock, fmtTokens } from "../lib/format.js";
  import { selectNode } from "../lib/store.svelte.js";
  import { eventTone } from "../lib/score.js";

  let { item, streaming = false } = $props();
  let open = $state(false);

  const long = $derived((item.text || "").length > 420 || (item.text || "").split("\n").length > 7);
  const isOpen = $derived(open || (streaming && item.kind === "thinking"));
  const rawDiff = $derived(item.kind === "diffraw" ? parseUnified(item.text) : []);
  const html = $derived(item.kind === "assistant" && (isOpen || !long) ? renderMarkdown(item.text) : "");
</script>

{#if item.kind === "tool"}
  <Tool {item} />
{:else if item.kind === "thinking"}
  <div class="think" class:open={isOpen}>
    <span class="gut"><Brain size={13} /></span>
    <button class="txt" aria-expanded={isOpen} onclick={() => (open = !open)} class:clamp={!isOpen && long}>{item.text}</button>
  </div>
{:else if item.kind === "assistant"}
  <div class="asst">
    <span class="gut"><Bot size={13} /></span>
    <div class="col">
      {#if isOpen || !long}
        <div class="prose">{@html html}</div>
      {:else}
        <button class="txt clamp plain" onclick={() => (open = true)}>{item.text}</button>
      {/if}
      {#if long && isOpen}<button class="fold" aria-label="collapse" onclick={() => (open = false)}><ChevronDown size={12} style="transform:rotate(180deg)" /></button>{/if}
    </div>
  </div>
{:else if item.kind === "user"}
  <div class="user">
    <span class="gut"><CircleUser size={13} /></span>
    <div class="txt plain">{item.text}</div>
  </div>
{:else if item.kind === "system"}
  <div class="sys"><span>{clip(item.text, 220)}</span></div>
{:else if item.kind === "error"}
  <div class="err">
    <span class="gut"><TriangleAlert size={13} /></span>
    <button class="txt mono" class:clamp={!isOpen && long} onclick={() => (open = !open)}>{item.text}</button>
  </div>
{:else if item.kind === "usage"}
  <div class="use num">
    <span>{fmtTokens(item.tokens ?? 0)}</span>
    {#if item.prompt != null}<span>p{fmtTokens(item.prompt)}</span>{/if}
    {#if item.completion != null}<span>c{fmtTokens(item.completion)}</span>{/if}
    {#if item.reasoning}<span>r{fmtTokens(item.reasoning)}</span>{/if}
  </div>
{:else if item.kind === "logdir"}
  <div class="dir mono">{clip(item.text, 140)}</div>
{:else if item.kind === "event"}
  {@const tone = eventTone(item.type)}
  <div class="ev">
    <span class="t num">{fmtClock(item.ts)}</span>
    <span class="tn {tone}"></span>
    <span class="ty mono">{item.type}</span>
    {#if item.node && item.node !== "-"}<button class="nid mono" onclick={() => selectNode(item.node)}>{item.node}</button>{/if}
    <span class="dt trunc dim">{item.detail}</span>
  </div>
{:else if item.kind === "diffraw"}
  {#if rawDiff.length}<Diff files={rawDiff} compact />{:else}<Out text={item.text} lang="diff" cap={60} />{/if}
{:else}
  <div class="raw mono dim">{clip(item.text, 300)}</div>
{/if}

<style>
  .think,
  .asst,
  .user,
  .err {
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr);
    gap: 4px;
    padding: 5px 0 5px 2px;
  }
  .gut {
    color: var(--fg-2);
    padding-top: 2px;
    display: flex;
    justify-content: center;
  }
  .txt {
    text-align: left;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--fg-1);
    font-size: var(--fs-md);
    line-height: 1.55;
    cursor: pointer;
    width: 100%;
  }
  .think .txt {
    color: var(--fg-2);
    font-size: var(--fs-sm);
  }
  .think.open .txt {
    color: var(--fg-1);
  }
  .txt.clamp {
    display: -webkit-box;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .plain {
    cursor: default;
    color: var(--fg);
  }
  .asst .txt.plain {
    cursor: pointer;
  }
  .col {
    min-width: 0;
    position: relative;
  }
  .fold {
    color: var(--fg-2);
    margin-top: 2px;
  }
  .user {
    background: var(--bg-1);
    border-radius: var(--r-2);
    padding: 7px 8px 7px 2px;
    margin: 4px 0;
  }
  .user .gut {
    color: var(--accent);
  }
  .sys {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--fg-2);
    font-size: var(--fs-xs);
    padding: 8px 0;
  }
  .sys::before,
  .sys::after {
    content: "";
    flex: 1;
    height: 1px;
    background: var(--line-soft);
  }
  .err .txt {
    color: var(--bad);
    font-size: var(--fs-sm);
  }
  .err .gut {
    color: var(--bad);
  }
  .use {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    color: var(--fg-2);
    font-size: var(--fs-xs);
    padding: 1px 4px;
  }
  .dir,
  .raw {
    font-size: var(--fs-xs);
    padding: 2px 4px;
  }
  .ev {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 22px;
    font-size: var(--fs-xs);
    padding: 0 4px;
  }
  .ev .t {
    color: var(--fg-2);
    width: 58px;
  }
  .tn {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--fg-2);
    flex: none;
  }
  .tn.ok { background: var(--ok); }
  .tn.bad { background: var(--bad); }
  .tn.warn { background: var(--warn); }
  .tn.wait { background: var(--wait); }
  .ty {
    color: var(--fg-1);
  }
  .nid {
    color: var(--accent);
  }
  .nid:hover {
    text-decoration: underline;
  }
  .dt {
    flex: 1;
  }
</style>
