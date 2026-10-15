import { app, cycleRun, live, persist, selectNode, viewOf } from "./store.svelte.js";
import { setTab } from "./commands.js";
import { TABS } from "./store.svelte.js";

export const HINTS = {
  new: "n", pause: "p", rail: "⌘\\", inspector: "⌘.", theme: "⌘⇧L",
  "tab-node": "1", "tab-changes": "2", "tab-artifact": "3", "tab-files": "4", "tab-docs": "5", "tab-usage": "6", "tab-events": "7", "tab-tools": "8",
  "view-both": "s",
};

const typing = (e) => {
  const t = e.target;
  return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
};

export function installKeys(getComposer) {
  const onkey = (e) => {
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key.toLowerCase() === "k") {
      e.preventDefault();
      app.palette = !app.palette;
      return;
    }
    if (meta && e.key === "\\") {
      e.preventDefault();
      app.railOpen = !app.railOpen;
      persist();
      return;
    }
    if (meta && e.key === ".") {
      e.preventDefault();
      app.inspectorOpen = !app.inspectorOpen;
      persist();
      return;
    }
    if (e.key === "Escape") {
      if (app.palette) app.palette = false;
      else if (app.newRun) app.newRun = false;
      return;
    }
    if (typing(e) || meta || e.altKey) return;
    const run = live.current;
    const view = viewOf(app.activeRun);
    if (e.key === "/") {
      e.preventDefault();
      getComposer()?.focus();
    } else if (e.key === "n") {
      e.preventDefault();
      app.newRun = true;
    } else if (e.key === "[" || e.key === "]") {
      cycleRun(e.key === "]" ? 1 : -1);
    } else if (/^[1-8]$/.test(e.key)) {
      setTab(TABS[+e.key - 1]);
    } else if (run && (e.key === "j" || e.key === "k")) {
      const ids = run.tree.map((n) => n.id);
      const i = ids.indexOf(view.node);
      const next = ids[Math.max(0, Math.min(ids.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (next) selectNode(next);
    } else if (e.key === "s" && run) {
      const order = ["both", "score", "feed"];
      view.center = order[(order.indexOf(view.center) + 1) % 3];
      persist();
    } else if (e.key === "p" && run?.meta?.attached) {
      run.halt(!run.meta.halted);
    }
  };
  addEventListener("keydown", onkey);
  return () => removeEventListener("keydown", onkey);
}
