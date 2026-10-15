<script>
  import { PanelLeftOpen } from "@lucide/svelte";
  import TopBar from "./TopBar.svelte";
  import RunBar from "./RunBar.svelte";
  import Waiting from "./Waiting.svelte";
  import Score from "./Score.svelte";
  import Feed from "./Feed.svelte";
  import RunFeed from "./RunFeed.svelte";
  import Composer from "./Composer.svelte";
  import Home from "./Home.svelte";
  import Resizer from "./Resizer.svelte";
  import { app, live, persist, viewOf } from "../lib/store.svelte.js";

  let { composer = $bindable() } = $props();
  const run = $derived(live.current);
  const view = $derived(viewOf(app.activeRun));
  const attached = $derived(!!run?.meta?.attached);

  const agents = $derived(view.node && run ? run.agentsOf(view.node) : []);
  const agentId = $derived(
    agents.find((a) => a.id === view.agent)?.id || agents.find((a) => a.live)?.id || agents.find((a) => a.id === view.node)?.id || agents[0]?.id || "",
  );
  const pinned = $derived(view.pin && view.pin !== agentId && run?.agent(view.pin) ? view.pin : "");
  const center = $derived(view.center || "both");
</script>

<main class="center">
  <TopBar />
  {#if app.activeRun && attached}
    <RunBar />
    <Waiting />
    <div class="stack">
      {#if center !== "feed"}
        <div class="score" style:height={center === "score" ? "auto" : app.scoreH + "px"} style:flex={center === "score" ? 1 : "none"}>
          <Score />
        </div>
      {/if}
      {#if center === "both"}
        <Resizer axis="y" onresize={(d) => (app.scoreH = Math.max(110, Math.min(innerHeight - 280, app.scoreH + d)))} onend={persist} />
      {/if}
      {#if center !== "score"}
        <div class="feeds">
          <div class="pane">
            {#if agentId}
              {#key agentId}<Feed {agentId} />{/key}
            {:else}
              <RunFeed node={view.node || ''} />
            {/if}
          </div>
          {#if pinned}
            <div class="pane side">{#key pinned}<Feed agentId={pinned} />{/key}</div>
          {/if}
        </div>
      {/if}
    </div>
    <Composer bind:this={composer} />
  {:else if app.activeRun}
    <div class="loading"></div>
  {:else}
    <Home />
  {/if}
</main>

<style>
  .center {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
    background: var(--bg);
  }
  .stack {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }
  .score {
    min-height: 0;
    border-bottom: 1px solid var(--line-soft);
    overflow: hidden;
  }
  .feeds {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  .pane {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .pane.side {
    border-left: 1px solid var(--line-soft);
  }
  .loading {
    flex: 1;
  }
</style>
