<script>
  import { ChevronDown, ChevronRight, Columns2, Rows2, FilePlus2, FileMinus2, FilePen } from "@lucide/svelte";
  import { highlight, langForPath } from "../lib/markdown.js";
  import { pairChanges, wordSplit } from "../lib/diff.js";

  // `split` undefined = the diff owns its unified/split toggle; a boolean = the caller does.
  let { files = [], split = undefined, onopen = null, compact = false, collapseAll = false } = $props();

  let own = $state("unified");
  const mode = $derived(split === undefined ? own : split ? "split" : "unified");
  let shut = $state({});
  let expanded = $state({});
  const LIMIT = 600;

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function lineHtml(file, line, pairs, idx) {
    const other = pairs.get(idx);
    if (other !== undefined && (line.kind === "add" || line.kind === "remove")) {
      const w = line.kind === "remove" ? wordSplit(line.text, other) : wordSplit(other, line.text);
      const mid = line.kind === "remove" ? w.a : w.b;
      if (mid.length && mid.length < line.text.length) return `${esc(w.pre)}<mark>${esc(mid)}</mark>${esc(w.post)}`;
    }
    return highlight(line.text, langForPath(file.path));
  }

  function splitRows(lines, pairs) {
    const rows = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (l.kind === "context" || l.kind === "meta") {
        rows.push({ l, r: l, li: i, ri: i });
        i++;
        continue;
      }
      const rem = [];
      const add = [];
      while (i < lines.length && lines[i].kind === "remove") rem.push(i++);
      while (i < lines.length && lines[i].kind === "add") add.push(i++);
      const n = Math.max(rem.length, add.length);
      for (let k = 0; k < n; k++) rows.push({ l: rem[k] != null ? lines[rem[k]] : null, r: add[k] != null ? lines[add[k]] : null, li: rem[k], ri: add[k] });
    }
    return rows;
  }

  const icon = (s) => (s === "added" ? FilePlus2 : s === "deleted" ? FileMinus2 : FilePen);
  const key = (f, i) => `${f.path}:${i}`;
</script>

<div class="diff" class:compact>
  {#if files.length > 0 && !compact && split === undefined}
    <div class="tools">
      <button class="icon-btn sm" aria-label="unified" aria-pressed={mode === "unified"} onclick={() => (own = "unified")}><Rows2 size={13} /></button>
      <button class="icon-btn sm" aria-label="split" aria-pressed={mode === "split"} onclick={() => (own = "split")}><Columns2 size={13} /></button>
    </div>
  {/if}
  {#each files as f, fi (key(f, fi))}
    {@const Icon = icon(f.status)}
    {@const closed = shut[key(f, fi)] ?? collapseAll}
    {@const hasNums = f.hunks.some((h) => h.lines.some((l) => l.old != null || l.new != null))}
    <section class="file">
      <header>
        <button class="fold" aria-label="toggle file" aria-expanded={!closed} onclick={() => (shut[key(f, fi)] = !closed)}>
          {#if closed}<ChevronRight size={13} />{:else}<ChevronDown size={13} />{/if}
        </button>
        <span class="st {f.status}"><Icon size={13} /></span>
        {#if onopen}
          <button class="path mono trunc" onclick={() => onopen(f.path)}>{f.path}</button>
        {:else}
          <span class="path mono trunc">{f.path}</span>
        {/if}
        <span class="grow"></span>
        {#if f.binary}<span class="chip">bin</span>{/if}
        <span class="add num">+{f.added}</span>
        <span class="del num">−{f.deleted}</span>
      </header>
      {#if !closed}
        {#each f.hunks as h, hi (hi)}
          {@const pairs = pairChanges(h.lines)}
          {@const cap = expanded[key(f, fi) + hi] ? Infinity : LIMIT}
          {#if h.header}
            <div class="hh mono"><span class="trunc">{h.header}{h.context ? " " + h.context : ""}</span></div>
          {/if}
          {#if mode === "unified"}
            <div class="lines u" class:nonum={!hasNums}>
              {#each h.lines.slice(0, cap) as l, li (li)}
                <div class="ln {l.kind}">
                  {#if hasNums}<span class="g num">{l.old ?? ""}</span><span class="g num">{l.new ?? ""}</span>{/if}
                  <span class="sg">{l.kind === "add" ? "+" : l.kind === "remove" ? "−" : ""}</span>
                  <code class="tx">{@html lineHtml(f, l, pairs, li) || "&nbsp;"}</code>
                </div>
              {/each}
            </div>
          {:else}
            <div class="lines s" class:nonum={!hasNums}>
              {#each splitRows(h.lines, pairs).slice(0, cap) as row, ri (ri)}
                <div class="sr">
                  <div class="ln half {row.l ? row.l.kind : 'empty'}">
                    {#if hasNums}<span class="g num">{row.l?.old ?? ""}</span>{/if}
                    <code class="tx">{#if row.l}{@html lineHtml(f, row.l, pairs, row.li) || "&nbsp;"}{/if}</code>
                  </div>
                  <div class="ln half {row.r ? row.r.kind : 'empty'}">
                    {#if hasNums}<span class="g num">{row.r?.new ?? ""}</span>{/if}
                    <code class="tx">{#if row.r}{@html lineHtml(f, row.r, pairs, row.ri) || "&nbsp;"}{/if}</code>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
          {#if h.lines.length > cap}
            <button class="more" onclick={() => (expanded[key(f, fi) + hi] = true)}>+{h.lines.length - cap}</button>
          {/if}
        {/each}
        {#if f.binary && !f.hunks.length}<div class="bin dim mono">binary</div>{/if}
      {/if}
    </section>
  {/each}
</div>

<style>
  .diff {
    font-size: var(--fs-sm);
  }
  .tools {
    display: flex;
    justify-content: flex-end;
    gap: 2px;
    margin-bottom: 4px;
  }
  .file {
    border: 1px solid var(--line-soft);
    border-radius: var(--r-2);
    overflow: hidden;
    margin-bottom: var(--sp-3);
    background: var(--bg);
  }
  .compact .file {
    margin-bottom: var(--sp-2);
  }
  header {
    display: flex;
    align-items: center;
    gap: 6px;
    height: 28px;
    padding: 0 8px 0 4px;
    background: var(--bg-1);
    border-bottom: 1px solid var(--line-soft);
  }
  .fold {
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    color: var(--fg-2);
  }
  .st {
    display: inline-flex;
    color: var(--fg-2);
  }
  .st.added { color: var(--ok); }
  .st.deleted { color: var(--bad); }
  .path {
    font-size: var(--fs-sm);
    text-align: left;
    color: var(--fg);
    flex: 0 1 auto;
  }
  button.path:hover {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .add { color: var(--add-fg); }
  .del { color: var(--del-fg); }
  .hh {
    padding: 2px 10px;
    color: var(--fg-2);
    background: var(--bg-1);
    font-size: var(--fs-xs);
    border-top: 1px solid var(--line-soft);
  }
  .hh:first-child { border-top: 0; }
  .lines {
    overflow-x: auto;
  }
  .ln {
    display: grid;
    grid-template-columns: 42px 42px 14px minmax(0, 1fr);
    min-width: max-content;
    width: 100%;
    line-height: 1.5;
  }
  .nonum .ln {
    grid-template-columns: 14px minmax(0, 1fr);
  }
  .s .ln.half {
    grid-template-columns: 42px minmax(0, 1fr);
  }
  .s.nonum .ln.half {
    grid-template-columns: minmax(0, 1fr);
  }
  .sr {
    display: grid;
    grid-template-columns: 1fr 1fr;
    min-width: max-content;
    width: 100%;
  }
  .sr .ln.half:first-child {
    border-right: 1px solid var(--line-soft);
  }
  .g {
    text-align: right;
    padding-right: 8px;
    color: var(--fg-2);
    font-size: var(--fs-xs);
    user-select: none;
    line-height: 1.7;
  }
  .sg {
    color: var(--fg-2);
    user-select: none;
    text-align: center;
  }
  .tx {
    white-space: pre;
    padding-right: 12px;
    font-size: var(--fs-sm);
  }
  .ln.add { background: var(--add-bg); }
  .ln.remove { background: var(--del-bg); }
  .ln.add .sg { color: var(--add-fg); }
  .ln.remove .sg { color: var(--del-fg); }
  .ln.empty { background: var(--bg-1); }
  .ln.meta { color: var(--fg-2); }
  :global(.diff mark) {
    background: var(--add-word);
    color: inherit;
    border-radius: 2px;
  }
  :global(.diff .ln.remove mark) {
    background: var(--del-word);
  }
  .more {
    width: 100%;
    padding: 4px;
    color: var(--fg-2);
    font-size: var(--fs-xs);
    border-top: 1px solid var(--line-soft);
    background: var(--bg-1);
  }
  .more:hover { color: var(--fg); }
  .bin { padding: 8px 12px; }
</style>
