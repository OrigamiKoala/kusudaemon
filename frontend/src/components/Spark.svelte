<script>
  let { values = [], width = 72, height = 16, label = "" } = $props();
  const d = $derived.by(() => {
    if (values.length < 2) return "";
    const max = Math.max(1, ...values);
    return values.map((v, i) => `${i ? "L" : "M"}${((i / (values.length - 1)) * width).toFixed(1)},${(height - 1 - (v / max) * (height - 3)).toFixed(1)}`).join("");
  });
</script>

{#if d}
  <svg viewBox="0 0 {width} {height}" {width} {height} role="img" aria-label={label}>
    <path {d} fill="none" stroke="var(--fg-2)" stroke-width="1.2" vector-effect="non-scaling-stroke" />
    <circle cx={width} cy={height - 1 - (values[values.length - 1] / Math.max(1, ...values)) * (height - 3)} r="1.8" fill="var(--accent)" />
  </svg>
{/if}
