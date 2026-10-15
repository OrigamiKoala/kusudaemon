/** Run `fn` now and then every `ms` until the returned stop function is called. */
export function poll(fn, ms) {
  let stopped = false;
  let t;
  const tick = async () => {
    if (stopped) return;
    try {
      await fn();
    } catch {
      /* next tick retries */
    }
    if (!stopped) t = setTimeout(tick, ms);
  };
  tick();
  return () => {
    stopped = true;
    clearTimeout(t);
  };
}
