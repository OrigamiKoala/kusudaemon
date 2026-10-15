<script>
  import { Plus, PanelLeftClose } from "@lucide/svelte";
  import { app, persist } from "../lib/store.svelte.js";
  import RunList from "./RunList.svelte";
  import TreeNav from "./TreeNav.svelte";
</script>

<aside class="rail" aria-label="runs and nodes">
  <header>
    <span class="mark">kusudaemon</span>
    <span class="grow"></span>
    <button class="icon-btn" aria-label="new run" onclick={() => (app.newRun = true)}><Plus size={15} /></button>
    <button class="icon-btn" aria-label="hide rail" onclick={() => ((app.railOpen = false), persist())}><PanelLeftClose size={15} /></button>
  </header>
  <RunList />
  <div class="sep"></div>
  <TreeNav />
</aside>

<style>
  .rail {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg-1);
    border-right: 1px solid var(--line-soft);
    padding-top: 0;
  }
  header {
    display: flex;
    align-items: center;
    gap: 2px;
    height: var(--bar-h);
    padding: 0 var(--sp-2) 0 var(--sp-4);
    flex: none;
  }
  .mark {
    font-weight: 620;
    letter-spacing: -0.02em;
    font-size: var(--fs-lg);
  }
  .sep {
    margin: var(--sp-2) 0;
  }
</style>
