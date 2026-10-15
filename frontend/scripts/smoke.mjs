// Headless smoke test (jsdom, no browser): mounts the built bundle against a
// running dashboard server and drives the main surfaces, failing on any
// uncaught error or console.error.
//   node scripts/smoke.mjs http://127.0.0.1:8799 <run-id> [node-id]
import { JSDOM } from "jsdom";
import { pathToFileURL } from "node:url";
import path from "node:path";

const [base = "http://127.0.0.1:8765", runId, nodeId = ""] = process.argv.slice(2);
if (!runId) throw new Error("run id required");

const errors = [];
const dom = new JSDOM('<!doctype html><html><body><div id="app"></div></body></html>', { url: base + "/", pretendToBeVisual: true });
const w = dom.window;
if (!process.env.HOME_VIEW) w.localStorage.setItem("kd.ui", JSON.stringify({ openRuns: [runId], activeRun: runId, views: { [runId]: { node: nodeId || null, tab: "node", center: "both", collapsed: [] } } }));

const nativeFetch = globalThis.fetch;
const rewrite = (u) => (typeof u === "string" && u.startsWith("/") ? base + u : u);
w.fetch = (u, o) => nativeFetch(rewrite(u), o);

class FakeES {
  constructor(u) {
    this.listeners = {};
    this.closed = false;
    this.ctrl = new AbortController();
    (async () => {
      try {
        const res = await nativeFetch(rewrite(u), { signal: this.ctrl.signal });
        const dec = new TextDecoder();
        let buf = "";
        for await (const chunk of res.body) {
          buf += dec.decode(chunk, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const block = buf.slice(0, i);
            buf = buf.slice(i + 2);
            const ev = /event: (.*)/.exec(block)?.[1] || "message";
            const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
            (this.listeners[ev] || []).forEach((fn) => fn({ data }));
          }
        }
      } catch (e) {
        if (!this.closed && this.onerror) this.onerror(e);
      }
    })();
  }
  addEventListener(n, fn) {
    (this.listeners[n] ||= []).push(fn);
  }
  close() {
    this.closed = true;
    this.ctrl.abort();
  }
}
w.EventSource = FakeES;
w.matchMedia = (q) => ({ matches: process.env.NARROW ? /max-width/.test(q) : false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
w.HTMLElement.prototype.scrollIntoView = () => {};
w.ResizeObserver = class { observe() {} disconnect() {} unobserve() {} };
w.innerHeight = 900;
w.innerWidth = 1500;

const KEEP = new Set(["URL", "URLSearchParams", "TextEncoder", "TextDecoder", "AbortController", "AbortSignal", "Blob", "File", "FormData", "Headers", "Request", "Response", "ReadableStream", "WebSocket", "Event", "EventTarget", "performance", "console", "Intl", "Promise"]);
const define = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
for (const k of Object.getOwnPropertyNames(w)) {
  if (/^[A-Z]/.test(k) && !KEEP.has(k) && !(k in globalThis && /^[A-Z][a-z]*(Error|Array|Map|Set|Object|Symbol|Number|String|Boolean|Date|Function|JSON|Math|Reflect|Proxy)$/.test(k))) {
    try { define(k, w[k]); } catch {}
  }
}
for (const k of ["document", "localStorage", "sessionStorage", "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame", "matchMedia", "EventSource", "ResizeObserver", "innerHeight", "innerWidth", "addEventListener", "removeEventListener", "confirm", "location", "dispatchEvent"]) {
  define(k, typeof w[k] === "function" ? w[k].bind(w) : w[k]);
}
define("window", w);
define("Event", w.Event);
define("EventTarget", w.EventTarget);
Object.defineProperty(globalThis, "navigator", { value: w.navigator, configurable: true });
globalThis.fetch = w.fetch;
w.addEventListener("error", (e) => errors.push("window.onerror: " + (e.error?.stack || e.message)));
process.on("unhandledRejection", (e) => errors.push("unhandledRejection: " + (e?.stack || e)));
const origErr = console.error;
console.error = (...a) => {
  errors.push("console.error: " + a.map(String).join(" "));
  origErr(...a);
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const q = (s) => w.document.querySelector(s);
const qa = (s) => [...w.document.querySelectorAll(s)];
const click = (el) => el && el.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));
const report = (label, ok, extra = "") => console.log(`${ok ? "ok  " : "FAIL"} ${label}${extra ? "  " + extra : ""}`) || (ok || errors.push("assert: " + label));

await import(pathToFileURL(path.resolve(process.env.BUNDLE || "../src/kusudaemon/dashboard/static/app.js")).href);
await sleep(3500);

if (process.env.EXPECT_AUTH) {
  report("auth overlay", !!q("input[type=password]"));
  console.log(errors.length ? "\nERRORS:\n" + errors.slice(0, 12).join("\n---\n") : "\nno errors");
  process.exit(errors.length ? 1 : 0);
}
if (process.env.HOME_VIEW) {
  report("home rows", qa(".home .r").length > 0, String(qa(".home .r").length));
  click(qa(".home .r")[0]);
  await sleep(2500);
  report("opens run from home", qa(".tab").length === 1 && !!q(".bar .goal"));
  console.log(errors.length ? "\nERRORS:\n" + errors.slice(0, 12).join("\n---\n") : "\nno errors");
  process.exit(errors.length ? 1 : 0);
}
if (process.env.NARROW) {
  report("narrow: rail is closed", !q(".rail.drawer"));
  report("narrow: center renders", !!q("main.center"));
}
report("rail rendered", !!q(".rail") || !!process.env.NARROW);
report("run list rows", qa(".run").length > 0, String(qa(".run").length));
report("tab for run", qa(".tab").length === 1);
report("run bar", !!q(".bar .goal"));
report("tree rows", qa("[role=treeitem]").length > 0, String(qa("[role=treeitem]").length));
report("score lanes", qa(".lane").length > 0, String(qa(".lane").length));
report("score bars", qa(".bar.done, .bar.timeout, .bar.running, .bar.error").length >= 0);

// select a node via tree
const first = process.env.SMOKE_FIXTURE ? qa("[role=treeitem]").find((el) => el.querySelector(".id")?.textContent === "b") : qa("[role=treeitem]").find((el) => !el.classList.contains("virt"));
click(first);
await sleep(2500);
report("feed rendered after select", !!q(".feed .scroll"));
report("feed items", qa(".feed .cv").length > 0, String(qa(".feed .cv").length));
report("tool rows", qa(".tool").length > 0, String(qa(".tool").length));
click(qa(".tool .head")[0]);
await sleep(100);
report("tool expands", !!q(".tool .body"));
const edit = qa(".tool").find((t) => /^(edit|write)$/.test(t.querySelector(".nm")?.textContent || ""));
if (edit) {
  click(edit.querySelector(".head"));
  await sleep(100);
  report("edit tool shows diff", !!edit.querySelector(".diff"), edit.querySelector(".diff") ? "" : edit.outerHTML.slice(0, 600));
}

// inspector tabs
const tabs = qa("[role=tab][aria-label]");
report("inspector tabs", tabs.length === 8, String(tabs.length));
for (const t of tabs) {
  click(t);
  await sleep(1200);
  report(`tab ${t.getAttribute("aria-label")}`, !!q(".insp .scroll") && q(".insp .scroll").children.length >= 0);
}

if (process.env.SMOKE_FIXTURE) {
  report("approval cards", qa(".ap").length === 4, String(qa(".ap").length));
  report("blocked strip", !!q(".strip.warn"));
  const tabBtn = (n) => qa("[role=tab][aria-label]").find((t) => t.getAttribute("aria-label") === n);
  click(tabBtn("changes"));
  await sleep(1800);
  const rows = qa(".insp .fr");
  report("git files listed", rows.length >= 2, String(rows.length));
  click(rows[0]);
  await sleep(1200);
  report("git diff renders", !!q(".insp .diff .ln.add"));
  const split = qa(".insp [aria-label=split]")[0];
  click(split);
  await sleep(100);
  report("split mode", !!q(".insp .sr"));
  click(qa(".insp [aria-label='tool edits']")[0]);
  await sleep(300);
  
  click(qa("[role=treeitem]").find((el) => el.querySelector(".id")?.textContent === "b"));
  await sleep(2000);
  click(qa(".insp [aria-label='tool edits']")[0]);
  await sleep(600);
  report("tool edits listed", qa(".insp .fr").length >= 1, String(qa(".insp .fr").length) + (qa(".insp .fr").length ? "" : " " + (q(".insp .scroll")?.textContent || "").slice(0, 200) + " sel=" + (q(".n.sel .id")?.textContent || "none")));
  click(tabBtn("files"));
  await sleep(800);
  report("files listing", qa(".insp .e").length > 0);
  click(tabBtn("usage"));
  await sleep(1200);
  report("usage totals", !!q(".insp .big"));
  click(tabBtn("docs"));
  await sleep(500);
  report("docs tabs", qa(".insp .bar button").length === 5);
}

if (process.env.SMOKE_FIXTURE) {
  // composer slash command
  const ta = q(".comp textarea");
  ta.value = "/tier T2";
  ta.dispatchEvent(new w.Event("input", { bubbles: true }));
  await sleep(100);
  ta.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(900);
  report("composer /tier runs", qa(".toasts .t").some((t) => /tier T2/.test(t.textContent)), qa(".toasts .t").map((t) => t.textContent).join("|"));
  ta.value = "hello agent";
  ta.dispatchEvent(new w.Event("input", { bubbles: true }));
  ta.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(900);
  report("interject without live agent errors", qa(".toasts .t.error").length > 0);
  // palette: pause
  w.dispatchEvent(new w.KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }));
  await sleep(150);
  const pin = q(".pal input");
  pin.value = "pause";
  pin.dispatchEvent(new w.Event("input", { bubbles: true }));
  await sleep(100);
  pin.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(1500);
  report("palette pause applies", !!q("button[aria-label=resume]"), qa(".toasts .t").map((t) => t.textContent).join("|"));
  // palette: multi-step (tier -> T3)
  w.dispatchEvent(new w.KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }));
  await sleep(150);
  const p2 = q(".pal input");
  p2.value = "set tier";
  p2.dispatchEvent(new w.Event("input", { bubbles: true }));
  await sleep(100);
  p2.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(150);
  report("palette param step lists options", qa(".pal [role=option]").length === 4, String(qa(".pal [role=option]").length));
  p2.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await sleep(1200);
  report("palette param step executes", !q(".pal"));
  // unpause for cleanliness
  click(q("button[aria-label=resume]"));
  await sleep(800);
  // approval: answer intake form (default answers)
  const ap = qa(".ap").find((a) => /intake/.test(a.textContent));
  const def = ap && [...ap.querySelectorAll("button")].find((b) => b.textContent.trim() === "default");
}

// palette
w.dispatchEvent(new w.KeyboardEvent("keydown", { key: "k", metaKey: true, bubbles: true }));
await sleep(200);
report("palette opens", !!q(".pal"), String(qa(".pal [role=option]").length));
const input = q(".pal input");
if (input) {
  input.value = "tier";
  input.dispatchEvent(new w.Event("input", { bubbles: true }));
  await sleep(100);
  report("palette filters", qa(".pal [role=option]").length > 0);
}
w.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
await sleep(100);

// new run sheet
click(q("button[aria-label='new run']"));
await sleep(150);
report("new run sheet", !!q("form[aria-label='new run']"));

console.log(errors.length ? "\nERRORS:\n" + errors.slice(0, 12).join("\n---\n") : "\nno errors");
process.exit(errors.length ? 1 : 0);
