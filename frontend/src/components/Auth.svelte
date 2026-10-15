<script>
  import { KeyRound, ArrowRight } from "@lucide/svelte";
  import { submitToken } from "../lib/store.svelte.js";
  let token = $state("");
</script>

<div class="scrim">
  <form class="box" onsubmit={(e) => (e.preventDefault(), submitToken(token.trim()))}>
    <KeyRound size={16} />
    <input type="password" aria-label="token" autocomplete="current-password" bind:value={token} />
    <button class="btn primary" type="submit" aria-label="continue" disabled={!token.trim()}><ArrowRight size={14} /></button>
  </form>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    z-index: 300;
    background: var(--bg);
    display: grid;
    place-items: center;
  }
  .box {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--fg-2);
    width: min(380px, 90vw);
  }
  input {
    flex: 1;
    height: 34px;
  }
</style>
