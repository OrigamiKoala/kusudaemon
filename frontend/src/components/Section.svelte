<script>
  let { title = "", children, actions } = $props();
</script>

<section class="sec">
  {#if title || actions}
    <header>
      <span>{title}</span>
      {#if actions}<span class="acts">{@render actions()}</span>{/if}
    </header>
  {/if}
  {@render children()}
</section>

<style>
  .sec {
    padding: var(--sp-3) var(--sp-4);
    border-bottom: 1px solid var(--line-soft);
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--sp-2);
    color: var(--fg-1);
    font-weight: 560;
    font-size: var(--fs-sm);
  }
  .acts {
    display: inline-flex;
    gap: 4px;
  }
</style>
