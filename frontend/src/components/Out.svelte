<script>
  import { Copy, Check } from "@lucide/svelte";
  import { highlight } from "../lib/markdown.js";

  let { text = "", lang = "", cap = 160, numbered = false } = $props();
  let all = $state(false);
  let copied = $state(false);
  const lines = $derived(String(text).split("\n"));
  const shown = $derived(all ? lines : lines.slice(0, cap));
  const html = $derived(lang ? highlight(shown.join("\n"), lang) : null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
      setTimeout(() => (copied = false), 1200);
    } catch {
      /* clipboard unavailable */
    }
  }
</script>

<div class="out">
  <button class="icon-btn sm copy" aria-label="copy" onclick={copy}>{#if copied}<Check size={12} />{:else}<Copy size={12} />{/if}</button>
  {#if numbered}
    <pre class="mono"><code>{#each shown as l, i}<span class="n num">{i + 1}</span>{l}
{/each}</code></pre>
  {:else if html != null}
    <pre class="mono"><code class="hljs">{@html html}</code></pre>
  {:else}
    <pre class="mono"><code>{shown.join("\n")}</code></pre>
  {/if}
  {#if lines.length > shown.length}
    <button class="more" onclick={() => (all = true)}>+{lines.length - shown.length}</button>
  {/if}
</div>

<style>
  .out {
    position: relative;
    background: var(--bg-1);
    border: 1px solid var(--line-soft);
    border-radius: var(--r-2);
    overflow: hidden;
  }
  pre {
    margin: 0;
    padding: 8px 10px;
    overflow: auto;
    max-height: 420px;
    font-size: var(--fs-sm);
    line-height: 1.5;
    white-space: pre;
    tab-size: 4;
  }
  .n {
    display: inline-block;
    min-width: 3ch;
    margin-right: 1.2ch;
    text-align: right;
    color: var(--fg-2);
    user-select: none;
  }
  .copy {
    position: absolute;
    top: 3px;
    right: 3px;
    opacity: 0;
    background: var(--bg-1);
  }
  .out:hover .copy {
    opacity: 1;
  }
  .more {
    width: 100%;
    padding: 4px;
    border-top: 1px solid var(--line-soft);
    color: var(--fg-2);
    font-size: var(--fs-xs);
  }
  .more:hover {
    color: var(--fg);
  }
</style>
