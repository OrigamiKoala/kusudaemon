<script>
  import { Info, GitCompareArrows, FileText, Folder, BookOpen, Gauge, Activity, Terminal, PanelRightClose } from "@lucide/svelte";
  import { app, persist, TABS, viewOf } from "../lib/store.svelte.js";
  import NodeTab from "./NodeTab.svelte";
  import ChangesTab from "./ChangesTab.svelte";
  import ArtifactTab from "./ArtifactTab.svelte";
  import FilesTab from "./FilesTab.svelte";
  import DocsTab from "./DocsTab.svelte";
  import UsageTab from "./UsageTab.svelte";
  import EventsTab from "./EventsTab.svelte";
  import ToolsTab from "./ToolsTab.svelte";

  const view = $derived(viewOf(app.activeRun));
  const ICONS = { node: Info, changes: GitCompareArrows, artifact: FileText, files: Folder, docs: BookOpen, usage: Gauge, events: Activity, tools: Terminal };
  const tab = $derived(TABS.includes(view.tab) ? view.tab : "node");
</script>

<aside class="insp" aria-label="inspector">
  <div class="tabs" role="tablist">
    {#each TABS as t, i (t)}
      {@const I = ICONS[t]}
      <button role="tab" class="icon-btn" aria-label={t} aria-selected={tab === t} class:on={tab === t} title="{t}  {i + 1}" onclick={() => ((view.tab = t), persist())}>
        <I size={15} />
      </button>
    {/each}
    <span class="grow"></span>
    <button class="icon-btn" aria-label="hide inspector" onclick={() => ((app.inspectorOpen = false), persist())}><PanelRightClose size={15} /></button>
  </div>
  <div class="scroll">
    {#if tab === "node"}<NodeTab />
    {:else if tab === "changes"}<ChangesTab />
    {:else if tab === "artifact"}<ArtifactTab />
    {:else if tab === "files"}<FilesTab />
    {:else if tab === "docs"}<DocsTab />
    {:else if tab === "usage"}<UsageTab />
    {:else if tab === "events"}<EventsTab />
    {:else}<ToolsTab />
    {/if}
  </div>
</aside>

<style>
  .insp {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg-1);
    border-left: 1px solid var(--line-soft);
  }
  .tabs {
    display: flex;
    align-items: center;
    gap: 2px;
    height: var(--bar-h);
    padding: 0 var(--sp-2);
    border-bottom: 1px solid var(--line-soft);
    flex: none;
  }
  .tabs .icon-btn.on {
    color: var(--accent);
    background: var(--accent-dim);
  }
  .scroll {
    overflow: auto;
    flex: 1;
    min-height: 0;
  }
</style>
