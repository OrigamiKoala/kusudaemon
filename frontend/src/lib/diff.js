// Line diff helpers. Hunk shape is shared with the server's git parser:
// file = { path, old_path, status, added, deleted, binary, hunks }
// hunk = { header, context, lines: [{ kind: add|remove|context|meta, old, new, text }] }

const MAX_CELLS = 2_500_000;

function lcsOps(a, b) {
  // trim common prefix/suffix first: edits are usually local
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let ea = a.length;
  let eb = b.length;
  while (ea > s && eb > s && a[ea - 1] === b[eb - 1]) {
    ea--;
    eb--;
  }
  const ops = [];
  for (let i = 0; i < s; i++) ops.push({ kind: "context", text: a[i] });
  const n = ea - s;
  const m = eb - s;
  if (n * m > MAX_CELLS) {
    for (let i = s; i < ea; i++) ops.push({ kind: "remove", text: a[i] });
    for (let j = s; j < eb; j++) ops.push({ kind: "add", text: b[j] });
  } else if (n === 0 || m === 0) {
    for (let i = s; i < ea; i++) ops.push({ kind: "remove", text: a[i] });
    for (let j = s; j < eb; j++) ops.push({ kind: "add", text: b[j] });
  } else {
    const w = m + 1;
    const dp = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i * w + j] = a[s + i] === b[s + j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[s + i] === b[s + j]) {
        ops.push({ kind: "context", text: a[s + i] });
        i++;
        j++;
      } else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
        ops.push({ kind: "remove", text: a[s + i++] });
      } else {
        ops.push({ kind: "add", text: b[s + j++] });
      }
    }
    while (i < n) ops.push({ kind: "remove", text: a[s + i++] });
    while (j < m) ops.push({ kind: "add", text: b[s + j++] });
  }
  for (let i = ea; i < a.length; i++) ops.push({ kind: "context", text: a[i] });
  return ops;
}

function opsToHunks(ops, ctx = 3) {
  const changed = [];
  ops.forEach((o, i) => o.kind !== "context" && changed.push(i));
  if (!changed.length) return [];
  const ranges = [];
  let start = Math.max(0, changed[0] - ctx);
  let end = Math.min(ops.length, changed[0] + ctx + 1);
  for (let k = 1; k < changed.length; k++) {
    const i = changed[k];
    if (i - ctx <= end) end = Math.min(ops.length, i + ctx + 1);
    else {
      ranges.push([start, end]);
      start = Math.max(0, i - ctx);
      end = Math.min(ops.length, i + ctx + 1);
    }
  }
  ranges.push([start, end]);
  // line numbers
  const oldAt = new Array(ops.length);
  const newAt = new Array(ops.length);
  let o = 1;
  let n = 1;
  ops.forEach((op, i) => {
    oldAt[i] = o;
    newAt[i] = n;
    if (op.kind !== "add") o++;
    if (op.kind !== "remove") n++;
  });
  return ranges.map(([a, b]) => {
    const lines = ops.slice(a, b).map((op, k) => ({
      kind: op.kind,
      old: op.kind === "add" ? null : oldAt[a + k],
      new: op.kind === "remove" ? null : newAt[a + k],
      text: op.text,
    }));
    const oc = lines.filter((l) => l.kind !== "add").length;
    const nc = lines.filter((l) => l.kind !== "remove").length;
    const first = lines[0];
    return { header: `@@ -${first.old ?? oldAt[a]},${oc} +${first.new ?? newAt[a]},${nc} @@`, context: "", lines };
  });
}

/** Diff two texts into a single-file structure (empty hunks when equal). */
export function diffTexts(path, oldText, newText, { status = "modified", ctx = 3 } = {}) {
  const a = oldText === "" ? [] : String(oldText).split("\n");
  const b = newText === "" ? [] : String(newText).split("\n");
  const ops = lcsOps(a, b);
  const hunks = opsToHunks(ops, ctx);
  let added = 0;
  let deleted = 0;
  for (const op of ops) {
    if (op.kind === "add") added++;
    else if (op.kind === "remove") deleted++;
  }
  return { path, old_path: path, status, added, deleted, binary: false, hunks };
}

/** A file shown entirely as additions (new file / write tool). */
export function addedFile(path, text) {
  const lines = String(text).split("\n").map((t, i) => ({ kind: "add", old: null, new: i + 1, text: t }));
  return {
    path,
    old_path: "",
    status: "added",
    added: lines.length,
    deleted: 0,
    binary: false,
    hunks: [{ header: `@@ -0,0 +1,${lines.length} @@`, context: "", lines }],
  };
}

/** Server `/api/node/<id>/diff` lines -> a file structure. */
export function serverLinesToFile(path, rows) {
  const hunks = [];
  let cur = null;
  let o = 0;
  let n = 0;
  let added = 0;
  let deleted = 0;
  for (const r of rows) {
    if (r.kind === "header") continue;
    if (r.kind === "hunk") {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(r.text);
      o = m ? +m[1] : 1;
      n = m ? +m[2] : 1;
      cur = { header: r.text, context: "", lines: [] };
      hunks.push(cur);
      continue;
    }
    if (!cur) continue;
    const text = r.text.slice(1);
    if (r.kind === "add") {
      cur.lines.push({ kind: "add", old: null, new: n++, text });
      added++;
    } else if (r.kind === "remove") {
      cur.lines.push({ kind: "remove", old: o++, new: null, text });
      deleted++;
    } else {
      cur.lines.push({ kind: "context", old: o++, new: n++, text });
    }
  }
  return { path, old_path: path, status: "modified", added, deleted, binary: false, hunks };
}

/** Common prefix/suffix split for intra-line highlighting of a -/+ pair. */
export function wordSplit(a, b) {
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let ea = a.length;
  let eb = b.length;
  while (ea > s && eb > s && a[ea - 1] === b[eb - 1]) {
    ea--;
    eb--;
  }
  return { pre: a.slice(0, s), a: a.slice(s, ea), b: b.slice(s, eb), post: a.slice(ea) };
}

/** Pair each run of removes with the following run of adds (for word marks). */
export function pairChanges(lines) {
  const pairs = new Map(); // line index -> counterpart text
  let i = 0;
  while (i < lines.length) {
    if (lines[i].kind !== "remove") {
      i++;
      continue;
    }
    let r = i;
    while (r < lines.length && lines[r].kind === "remove") r++;
    let a = r;
    while (a < lines.length && lines[a].kind === "add") a++;
    const nr = r - i;
    const na = a - r;
    for (let k = 0; k < Math.min(nr, na); k++) {
      pairs.set(i + k, lines[r + k].text);
      pairs.set(r + k, lines[i + k].text);
    }
    i = a;
  }
  return pairs;
}

/** Parse a unified diff (git or plain) into file structures. */
export function parseUnified(text) {
  const files = [];
  let cur = null;
  let hunk = null;
  let o = 0;
  let n = 0;
  const start = (path, old_path = path) => {
    cur = { path, old_path, status: "modified", added: 0, deleted: 0, binary: false, hunks: [] };
    files.push(cur);
    hunk = null;
  };
  for (const raw of String(text).split("\n")) {
    let m;
    if ((m = /^diff --git a\/(.*) b\/(.*)$/.exec(raw))) {
      start(m[2], m[1]);
      continue;
    }
    if (!cur && (m = /^--- (?:a\/)?(.+)$/.exec(raw))) {
      start(m[1].replace(/\t.*$/, ""));
      continue;
    }
    if (!cur) continue;
    if (!hunk) {
      if (raw.startsWith("new file mode")) cur.status = "added";
      else if (raw.startsWith("deleted file mode")) cur.status = "deleted";
      else if (raw.startsWith("Binary files")) cur.binary = true;
      else if ((m = /^\+\+\+ (?:b\/)?(.+)$/.exec(raw)) && m[1] !== "/dev/null") cur.path = m[1].replace(/\t.*$/, "");
    }
    if ((m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/.exec(raw))) {
      o = +m[1];
      n = +m[2];
      hunk = { header: raw, context: m[3], lines: [] };
      cur.hunks.push(hunk);
      continue;
    }
    if (!hunk) continue;
    if (raw.startsWith("+")) {
      hunk.lines.push({ kind: "add", old: null, new: n++, text: raw.slice(1) });
      cur.added++;
    } else if (raw.startsWith("-")) {
      hunk.lines.push({ kind: "remove", old: o++, new: null, text: raw.slice(1) });
      cur.deleted++;
    } else if (raw.startsWith("\\")) {
      hunk.lines.push({ kind: "meta", old: null, new: null, text: raw });
    } else if (raw.startsWith(" ")) {
      hunk.lines.push({ kind: "context", old: o++, new: n++, text: raw.slice(1) });
    }
  }
  return files;
}

/** Codex-style "*** Begin Patch" blocks -> file structures. */
export function parseApplyPatch(text) {
  const files = [];
  let cur = null;
  let hunk = null;
  for (const raw of String(text).split("\n")) {
    let m;
    if ((m = /^\*\*\* (Update|Add|Delete) File: (.+)$/.exec(raw))) {
      cur = {
        path: m[2].trim(),
        old_path: m[2].trim(),
        status: m[1] === "Add" ? "added" : m[1] === "Delete" ? "deleted" : "modified",
        added: 0,
        deleted: 0,
        binary: false,
        hunks: [],
      };
      files.push(cur);
      hunk = null;
      continue;
    }
    if (!cur || raw.startsWith("*** ")) continue;
    if (raw.startsWith("@@")) {
      hunk = { header: raw, context: "", lines: [] };
      cur.hunks.push(hunk);
      continue;
    }
    if (!hunk) {
      hunk = { header: "", context: "", lines: [] };
      cur.hunks.push(hunk);
    }
    if (raw.startsWith("+")) {
      hunk.lines.push({ kind: "add", old: null, new: null, text: raw.slice(1) });
      cur.added++;
    } else if (raw.startsWith("-")) {
      hunk.lines.push({ kind: "remove", old: null, new: null, text: raw.slice(1) });
      cur.deleted++;
    } else if (raw.startsWith(" ")) {
      hunk.lines.push({ kind: "context", old: null, new: null, text: raw.slice(1) });
    }
  }
  return files;
}
