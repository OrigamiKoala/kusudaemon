// Pure command-line parsing helpers (kept free of store imports so they are unit-testable).

/** Parse the text after "/cmd " into args by param order (last text param takes the rest). */
export function parseArgs(cmd, rest) {
  const params = cmd.params || [];
  const args = {};
  let s = rest.trim();
  params.forEach((p, i) => {
    if (!s) return;
    if (i === params.length - 1 || p.type === "text") {
      if (p.type === "text" && i < params.length - 1) {
        // text in a non-final slot: take one token
        const m = /^(\S+)\s*(.*)$/s.exec(s);
        args[p.key] = m[1];
        s = m[2];
      } else {
        args[p.key] = s;
        s = "";
      }
      return;
    }
    const m = /^(\S+)\s*(.*)$/s.exec(s);
    args[p.key] = m[1];
    s = m[2];
  });
  return args;
}

export function missingParams(cmd, args) {
  return (cmd.params || []).filter((p) => !p.optional && (args[p.key] === undefined || args[p.key] === ""));
}

/** Subsequence fuzzy score: higher is better, -1 means no match. */
export function fuzzy(needle, hay) {
  const n = needle.toLowerCase();
  const h = hay.toLowerCase();
  if (!n) return 0;
  const at = h.indexOf(n);
  if (at >= 0) return 200 - at - h.length * 0.1;
  let i = 0;
  let score = 0;
  let last = -2;
  for (let j = 0; j < h.length && i < n.length; j++) {
    if (h[j] === n[i]) {
      score += j === last + 1 ? 6 : 2;
      last = j;
      i++;
    }
  }
  return i === n.length ? score : -1;
}

