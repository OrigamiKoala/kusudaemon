const pad = (n) => String(n).padStart(2, "0");

/** Epoch seconds -> HH:MM:SS (local). */
export function fmtClock(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Epoch seconds -> "Mon DD HH:MM" for older entries, clock for today. */
export function fmtStamp(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return fmtClock(ts);
  return `${d.toLocaleString(undefined, { month: "short" })} ${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Seconds -> 1h02m / 4m07s / 12s. */
export function fmtDur(secs) {
  if (secs == null || !isFinite(secs)) return "";
  secs = Math.max(0, Math.round(secs));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h) return `${h}h${pad(m)}m`;
  if (m) return `${m}m${pad(s)}s`;
  return `${s}s`;
}

export const fmtMs = (ms) => (ms == null ? "" : fmtDur(ms / 1000));

export function fmtTokens(n) {
  if (n == null) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
  return String(Math.round(n));
}

export function fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return n + "B";
  if (n < 1048576) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + "K";
  return (n / 1048576).toFixed(1) + "M";
}

export function relTime(ts, now = Date.now() / 1000) {
  if (!ts) return "";
  const d = now - ts;
  if (d < 5) return "now";
  return fmtDur(d);
}

export function shortId(id, max = 28) {
  if (!id || id.length <= max) return id || "";
  const keep = max - 1;
  const head = Math.ceil(keep * 0.55);
  return id.slice(0, head) + "…" + id.slice(id.length - (keep - head));
}

export function clip(text, n) {
  if (!text) return "";
  const t = String(text).replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

export function pct(n, d) {
  return d ? Math.round((n / d) * 100) : 0;
}
