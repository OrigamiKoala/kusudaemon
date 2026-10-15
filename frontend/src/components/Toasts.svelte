<script>
  import { X } from "@lucide/svelte";
  import { app, dismissToast } from "../lib/store.svelte.js";
</script>

<div class="toasts" aria-live="polite">
  {#each app.toasts as t (t.id)}
    <div class="t {t.kind}" role="status">
      <span>{t.message}</span>
      <button class="icon-btn sm" aria-label="dismiss" onclick={() => dismissToast(t.id)}><X size={12} /></button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    right: 14px;
    bottom: 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    z-index: 200;
    max-width: min(420px, 90vw);
  }
  .t {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 7px 8px 7px 12px;
    background: var(--bg-3);
    border: 1px solid var(--line);
    border-radius: var(--r-2);
    box-shadow: var(--pop-shadow);
    font-size: var(--fs-sm);
    animation: in 180ms var(--ease);
    overflow-wrap: anywhere;
  }
  .t.error {
    border-color: color-mix(in oklch, var(--bad) 55%, transparent);
    color: var(--bad);
  }
  .t span {
    flex: 1;
  }
  @keyframes in {
    from {
      transform: translateY(6px);
      opacity: 0;
    }
  }
</style>
