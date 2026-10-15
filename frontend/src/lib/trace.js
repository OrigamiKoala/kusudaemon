// Turns the server's flat trace entries into feed items: tool calls are
// paired with their results, usage lines are folded into totals, and each
// tool call gets a kind, a one-line title and (for edits) a diff.
import { addedFile, diffTexts, parseApplyPatch, parseUnified } from "./diff.js";

const KINDS = {
  read: "read", view: "read", cat: "read", open: "read",
  write: "write", save: "write", append: "write", create: "write",
  edit: "edit", multiedit: "edit", patch: "edit", apply_patch: "edit", str_replace: "edit", str_replace_editor: "edit", str_replace_based_edit_tool: "edit",
  bash: "bash", shell: "bash", exec: "bash", run: "bash", terminal: "bash", tmux: "bash", python: "bash", ipython: "bash",
  grep: "search", glob: "search", find: "search", list: "search", ls: "search", search: "search", rg: "search",
  webfetch: "web", websearch: "web", web_search: "web", browser: "web", fetch: "web",
  todowrite: "todo", todoread: "todo", todo: "todo",
  task: "task", subagent: "task", agent: "task",
};

export function toolKind(name) {
  return KINDS[String(name || "").toLowerCase()] || "other";
}

// The server cuts long tool inputs at 8000 chars and appends "…[truncated]".
const TRUNCATED = /\n?…\[truncated\]\s*$/;

function salvage(s) {
  if (!TRUNCATED.test(s)) return null;
  const t = s.replace(TRUNCATED, "").replace(/\\u[0-9a-fA-F]{0,3}$/, "").replace(/\\$/, "");
  for (const close of ['"}', '"}]', '"}]}', "}", '"]}']) {
    try {
      const o = JSON.parse(t + close);
      Object.defineProperty(o, "_truncated", { value: true, enumerable: false });
      return o;
    } catch {
      /* try the next closer */
    }
  }
  return null;
}

/**
 * gptme-style calls arrive as a fenced block: the tool name and its argument
 * ride in the call text ("save app.py") and the body is the bare tool_input.
 */
export function inputFromCall(name, rawInput, text) {
  const input = parseInput(rawInput);
  if (!input || typeof rawInput !== "string" || !("raw" in input) || Object.keys(input).length !== 1) return input;
  const kind = toolKind(name);
  const arg = String(text || "").slice(String(name || "").length).trim();
  if (kind === "write" && arg) return { path: arg, content: input.raw };
  if (kind === "edit" && arg) return { path: arg, patch: input.raw };
  if (kind === "bash") return { command: input.raw };
  if (kind === "read") return { path: arg || input.raw };
  return input;
}

export function parseInput(raw) {
  if (raw == null) return null;
  if (typeof raw === "object") return raw;
  const s = String(raw).trim();
  if (s.startsWith("{") || s.startsWith("[")) {
    try {
      return JSON.parse(s);
    } catch {
      const o = salvage(s);
      if (o) return o;
    }
  }
  return { raw: s };
}

const pick = (o, keys) => {
  if (!o || typeof o !== "object") return undefined;
  for (const k of keys) if (o[k] != null && o[k] !== "") return o[k];
  return undefined;
};

export const inputPath = (i) => pick(i, ["filePath", "file_path", "path", "file", "filename", "target_file", "notebook_path"]) ?? (Array.isArray(i?.paths) ? i.paths[0] : undefined);
export const inputContent = (i) => pick(i, ["content", "contents", "text", "file_text", "data"]);
export const inputCommand = (i) => pick(i, ["command", "cmd", "code", "script", "raw"]);

/** Diff files an edit-like tool call would produce, or null. */
export function toolDiffFiles(name, input) {
  const kind = toolKind(name);
  if (!input || (kind !== "edit" && kind !== "write")) return null;
  const path = inputPath(input) || "";
  if (kind === "write") {
    const c = inputContent(input);
    return c == null ? null : [addedFile(path, String(c))];
  }
  const patchText = pick(input, ["patch", "patchText", "diff", "input", "raw"]);
  if (typeof patchText === "string") {
    if (patchText.includes("*** Begin Patch") || /^\*\*\* (Update|Add|Delete) File:/m.test(patchText)) return parseApplyPatch(patchText);
    if (/^(diff --git|--- |@@ )/m.test(patchText)) {
      const f = parseUnified(patchText);
      if (f.length) return f;
    }
    const blocks = [...patchText.matchAll(/<<<<<<< ORIGINAL\n([\s\S]*?)\n=======\n([\s\S]*?)\n>>>>>>> UPDATED/g)];
    if (blocks.length) {
      const parts = blocks.map((m) => blind(diffTexts(path, m[1], m[2], { ctx: 2 })));
      return [{ ...parts[0], hunks: parts.flatMap((f) => f.hunks), added: parts.reduce((a, f) => a + f.added, 0), deleted: parts.reduce((a, f) => a + f.deleted, 0) }];
    }
  }
  const edits = Array.isArray(input.edits) ? input.edits : [input];
  const out = [];
  for (const e of edits) {
    const oldS = pick(e, ["oldString", "old_string", "old_str", "old", "original"]);
    const newS = pick(e, ["newString", "new_string", "new_str", "new", "updated"]);
    if (oldS == null && newS == null) continue;
    out.push(blind(diffTexts(path, oldS ?? "", newS ?? "", { ctx: 2 })));
  }
  if (!out.length) return null;
  // one file entry, many hunks
  const file = { ...out[0], hunks: out.flatMap((f) => f.hunks), added: out.reduce((a, f) => a + f.added, 0), deleted: out.reduce((a, f) => a + f.deleted, 0) };
  return [file];
}

// snippet-relative line numbers would mislead: hide them
function blind(file) {
  for (const h of file.hunks) for (const l of h.lines) (l.old = null), (l.new = null);
  return file;
}

export function toolTitle(name, input) {
  const kind = toolKind(name);
  if (!input) return "";
  if (kind === "read" || kind === "write" || kind === "edit") {
    const p = inputPath(input);
    if (p) {
      const off = input.offset ?? input.line;
      const lim = input.limit;
      return off != null || lim != null ? `${p}:${off ?? 1}${lim ? "+" + lim : ""}` : p;
    }
    const patch = pick(input, ["patch", "patchText", "input", "raw"]);
    if (typeof patch === "string") {
      const m = /^\*\*\* (?:Update|Add|Delete) File: (.+)$/m.exec(patch) || /^\+\+\+ (?:b\/)?(.+)$/m.exec(patch);
      if (m) return m[1];
    }
    return "";
  }
  if (kind === "bash") return String(inputCommand(input) ?? "").split("\n")[0];
  if (kind === "search") {
    const pat = pick(input, ["pattern", "query", "glob", "regex", "raw"]);
    const p = inputPath(input) || input.dir || input.directory;
    return [pat, p].filter(Boolean).join("  ");
  }
  if (kind === "web") return String(pick(input, ["url", "query", "q", "raw"]) ?? "");
  if (kind === "todo") return Array.isArray(input.todos) ? `${input.todos.length}` : "";
  if (kind === "task") return String(pick(input, ["description", "prompt", "task", "raw"]) ?? "").split("\n")[0];
  const first = Object.values(input).find((v) => typeof v === "string");
  return first ? String(first).split("\n")[0] : "";
}

export function toolResult(entry) {
  if (!entry) return "";
  let out = entry.tool_output;
  if (out == null || out === "") out = entry.text || "";
  return String(out).replace(/^tool_result:\s?/, "");
}

/**
 * entries -> feed items.
 * item: { key, kind, ts, ...kind-specific }
 * kinds: thinking | assistant | user | system | error | tool | usage | logdir | raw
 */
export function buildFeed(entries, { live = false } = {}) {
  const items = [];
  const open = new Map(); // tool_id -> item
  const unresolved = []; // FIFO of items awaiting a result
  let lastCall = null; // gptme: the system/diff entries right after a call belong to it
  let lastCallAt = -1;
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    const base = { key: i, ts: e.timestamp ?? null, idx: e.ts ?? i };
    switch (e.role) {
      case "tool_call": {
        // the worker also logs a bare "tool_use <name>" marker beside each real call
        if (!e.tool_id && e.tool_input == null && /^tool_use\b/.test(e.text || "")) break;
        const input = inputFromCall(e.tool_name, e.tool_input, e.text);
        const item = {
          ...base, kind: "tool", name: e.tool_name || e.text || "tool", input, result: null, done: false,
          exit: null, durationMs: e.duration_ms ?? null, toolKind: toolKind(e.tool_name || e.text), error: false,
        };
        items.push(item);
        if (e.tool_id) open.set(e.tool_id, item);
        unresolved.push(item);
        lastCall = item;
        lastCallAt = items.length - 1;
        break;
      }
      case "tool": {
        let item = (e.tool_id && open.get(e.tool_id)) || null;
        if (!item) {
          const j = unresolved.findIndex((u) => !u.done && (u.name === e.tool_name || !e.tool_name));
          if (j >= 0) item = unresolved[j];
        }
        const text = toolResult(e);
        if (item) {
          item.result = text;
          item.done = true;
          item.exit = e.exit_code ?? null;
          item.durationMs = e.duration_ms ?? item.durationMs;
          item.resultTs = e.timestamp ?? null;
          item.error = (e.exit_code != null && e.exit_code !== 0) || /^(error|failed|traceback)/i.test(text.trim());
          const k = unresolved.indexOf(item);
          if (k >= 0) unresolved.splice(k, 1);
        } else {
          items.push({
            ...base, kind: "tool", name: e.tool_name || "tool", input: parseInput(e.tool_input), result: text, done: true,
            exit: e.exit_code ?? null, durationMs: e.duration_ms ?? null, toolKind: toolKind(e.tool_name), error: false,
          });
        }
        break;
      }
      case "usage":
        items.push({
          ...base, kind: "usage", tokens: e.tokens, prompt: e.prompt_tokens, completion: e.completion_tokens,
          reasoning: e.reasoning_tokens,
        });
        break;
      case "diff":
        if (lastCall && items.length - 1 === lastCallAt) {
          lastCall.serverDiff = e.text || "";
          break;
        }
        items.push({ ...base, kind: "diffraw", text: e.text || "" });
        break;
      case "system":
        if (lastCall && !lastCall.done && items.length - 1 === lastCallAt && !/^[─-]{2}/.test(e.text || "") ) {
          lastCall.result = e.text || "";
          lastCall.done = true;
          lastCall.error = /^(error|tool operation error|traceback|exception)/i.test((e.text || "").trim());
          const k = unresolved.indexOf(lastCall);
          if (k >= 0) unresolved.splice(k, 1);
          lastCall = null;
          break;
        }
        items.push({ ...base, kind: "system", text: e.text || "" });
        break;
      case "thinking":
      case "assistant":
      case "user":
      case "error":
      case "logdir":
        items.push({ ...base, kind: e.role, text: e.text || "" });
        break;
      default:
        items.push({ ...base, kind: "raw", text: e.text || "" });
    }
  }
  // a call that never got a result: still running only if it is the newest one on a live agent
  for (let k = 0; k < unresolved.length; k++) {
    if (live && k === unresolved.length - 1) continue;
    unresolved[k].done = true;
    unresolved[k].unknown = true;
  }
  return items;
}

/** Sum of usage entries. */
export function usageTotals(entries) {
  let prompt = 0;
  let completion = 0;
  let reasoning = 0;
  for (const e of entries) {
    if (e.role !== "usage") continue;
    prompt += e.prompt_tokens || 0;
    completion += e.completion_tokens || 0;
    reasoning += e.reasoning_tokens || 0;
  }
  return { prompt, completion, reasoning, total: prompt + completion + reasoning };
}

/** Every edit/write the trace made: [{path, added, deleted, key}] summed per path. */
export function changedPaths(items) {
  const byPath = new Map();
  for (const it of items) {
    if (it.kind !== "tool" || !it.done || it.error) continue;
    const files = toolDiffFiles(it.name, it.input) || (it.serverDiff ? parseUnified(it.serverDiff) : null);
    if (!files) continue;
    for (const f of files) {
      const rec = byPath.get(f.path) || { path: f.path, added: 0, deleted: 0, count: 0, last: it.key };
      rec.added += f.added;
      rec.deleted += f.deleted;
      rec.count += 1;
      rec.last = it.key;
      byPath.set(f.path, rec);
    }
  }
  return [...byPath.values()];
}
