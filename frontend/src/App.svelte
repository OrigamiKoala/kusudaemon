<script>
  import { onMount } from "svelte";
  import Rail from "./components/Rail.svelte";
  import Center from "./components/Center.svelte";
  import Inspector from "./components/Inspector.svelte";
  import Resizer from "./components/Resizer.svelte";
  import Palette from "./components/Palette.svelte";
  import NewRun from "./components/NewRun.svelte";
  import Toasts from "./components/Toasts.svelte";
  import Auth from "./components/Auth.svelte";
  import { app, boot, live, persist } from "./lib/store.svelte.js";
  import { installKeys } from "./lib/keys.js";

  let composer = $state();
  const showInspector = $derived(app.inspectorOpen && !!app.activeRun);

  onMount(() => {
    boot();
    return installKeys(() => composer);
  });
  const waiting = $derived(app.runs.reduce((n, r) => n + (r.pending_approvals || 0), 0));
  $effect(() => {
    const base = live.current?.meta?.run_id ? `${live.current.meta.run_id} · kusudaemon` : "Kusudaemon";
    document.title = waiting ? `(${waiting}) ${base}` : base;
  });
  function closeDrawers() {
    app.railOpen = false;
    app.inspectorOpen = false;
    persist();
  }
</script>

<div
  class="app"
  class:narrow={app.narrow}
  style:grid-template-columns="{app.railOpen && !app.narrow ? app.railW + 'px' : '0px'} minmax(0, 1fr) {showInspector && !app.narrow ? app.inspectorW + 'px' : '0px'}"
>
  <div class="col rail" class:hidden={!app.railOpen} class:drawer={app.narrow && app.railOpen}>
    {#if app.railOpen}<Rail />{/if}
    {#if app.railOpen && !app.narrow}
      <div class="rz-slot r"><Resizer axis="x" onresize={(d) => (app.railW = Math.max(190, Math.min(460, app.railW + d)))} onend={persist} /></div>
    {/if}
  </div>
  <div class="mid"><Center bind:composer /></div>
  <div class="col insp" class:hidden={!showInspector} class:drawer={app.narrow && showInspector}>
    {#if showInspector}
      {#if !app.narrow}
        <div class="rz-slot l"><Resizer axis="x" reverse onresize={(d) => (app.inspectorW = Math.max(300, Math.min(780, app.inspectorW + d)))} onend={persist} /></div>
      {/if}
      <Inspector />
    {/if}
  </div>
</div>

{#if app.narrow && (app.railOpen || showInspector)}
  <div class="drawer-scrim" role="presentation" onclick={closeDrawers}></div>
{/if}
{#if app.palette}<Palette />{/if}
{#if app.newRun}<NewRun />{/if}
<Toasts />
{#if app.authRequired}<Auth />{/if}

<style>
  .app {
    display: grid;
    height: 100vh;
    height: 100dvh;
    grid-template-rows: minmax(0, 1fr);
    overflow: hidden;
  }
  .col {
    min-width: 0;
    min-height: 0;
    position: relative;
    display: flex;
    flex-direction: column;
  }
  .col.rail {
    grid-column: 1;
  }
  .mid {
    grid-column: 2;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .mid > :global(main) {
    flex: 1;
  }
  .col.insp {
    grid-column: 3;
  }
  .col.hidden {
    display: none;
  }
  .col > :global(aside) {
    flex: 1;
  }
  .rz-slot {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 0;
    z-index: 10;
  }
  .rz-slot.r {
    right: 0;
  }
  .rz-slot.l {
    left: 0;
  }
  .rz-slot :global(.rz) {
    position: absolute;
    top: 0;
    bottom: 0;
  }
  .narrow .drawer {
    position: fixed;
    top: 0;
    bottom: 0;
    z-index: 60;
    width: min(88vw, 400px);
    box-shadow: var(--pop-shadow);
  }
  .drawer-scrim {
    position: fixed;
    inset: 0;
    z-index: 55;
    background: var(--scrim);
  }
  .narrow .rail.drawer {
    left: 0;
  }
  .narrow .insp.drawer {
    right: 0;
  }
</style>
