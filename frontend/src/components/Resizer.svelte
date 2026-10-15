<script>
  // axis "x": vertical bar between columns; "y": horizontal bar between rows.
  let { axis = "x", onresize, onend = () => {}, reverse = false } = $props();
  let dragging = $state(false);

  function down(e) {
    e.preventDefault();
    dragging = true;
    let last = axis === "x" ? e.clientX : e.clientY;
    const move = (ev) => {
      const cur = axis === "x" ? ev.clientX : ev.clientY;
      onresize((reverse ? -1 : 1) * (cur - last));
      last = cur;
    };
    const up = () => {
      dragging = false;
      removeEventListener("pointermove", move);
      removeEventListener("pointerup", up);
      onend();
    };
    addEventListener("pointermove", move);
    addEventListener("pointerup", up);
  }
  function key(e) {
    const step = e.shiftKey ? 40 : 12;
    const d = axis === "x" ? (e.key === "ArrowRight" ? step : e.key === "ArrowLeft" ? -step : 0) : e.key === "ArrowDown" ? step : e.key === "ArrowUp" ? -step : 0;
    if (d) {
      e.preventDefault();
      onresize((reverse ? -1 : 1) * d);
      onend();
    }
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_no_noninteractive_tabindex -->
<div class="rz {axis}" class:on={dragging} role="separator" aria-orientation={axis === "x" ? "vertical" : "horizontal"} tabindex="0" onpointerdown={down} onkeydown={key}></div>

<style>
  .rz {
    position: relative;
    flex: none;
    z-index: 5;
    touch-action: none;
  }
  .rz.x {
    width: 0;
    cursor: col-resize;
  }
  .rz.y {
    height: 0;
    cursor: row-resize;
  }
  .rz::after {
    content: "";
    position: absolute;
    background: transparent;
    transition: background var(--dur) var(--ease);
  }
  .rz.x::after {
    top: 0;
    bottom: 0;
    left: -3px;
    width: 6px;
  }
  .rz.y::after {
    left: 0;
    right: 0;
    top: -3px;
    height: 6px;
  }
  .rz:hover::after,
  .rz.on::after,
  .rz:focus-visible::after {
    background: var(--accent);
    opacity: 0.55;
  }
</style>
