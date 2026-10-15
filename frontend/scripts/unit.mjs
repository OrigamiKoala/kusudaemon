// Unit tests for the pure frontend modules (no DOM, no framework).  node scripts/unit.mjs
import assert from "node:assert/strict";
import { addedFile, diffTexts, pairChanges, parseApplyPatch, parseUnified, serverLinesToFile, wordSplit } from "../src/lib/diff.js";
import { buildFeed, changedPaths, inputFromCall, parseInput, toolDiffFiles, toolKind, toolTitle } from "../src/lib/trace.js";
import { buildScore, buildTree, eventTone, flattenTree } from "../src/lib/score.js";
import { fuzzy, missingParams, parseArgs } from "../src/lib/cmdparse.js";
import { clip, fmtDur, fmtTokens, shortId } from "../src/lib/format.js";

let n = 0;
const test = (name, fn) => {
  try {
    fn();
    n++;
  } catch (e) {
    console.error("FAIL", name, "\n", e.stack);
    process.exitCode = 1;
  }
};

test("diffTexts finds the changed line with context", () => {
  const f = diffTexts("a.txt", "one\ntwo\nthree\nfour\nfive\nsix\nseven", "one\ntwo\nTHREE\nfour\nfive\nsix\nseven");
  assert.equal(f.added, 1);
  assert.equal(f.deleted, 1);
  const kinds = f.hunks[0].lines.map((l) => l.kind);
  assert.deepEqual(kinds.filter((k) => k !== "context"), ["remove", "add"]);
  const rem = f.hunks[0].lines.find((l) => l.kind === "remove");
  assert.equal(rem.old, 3);
});

test("diffTexts of equal text has no hunks; empty old is all adds", () => {
  assert.equal(diffTexts("x", "a\nb", "a\nb").hunks.length, 0);
  const f = diffTexts("x", "", "a\nb");
  assert.equal(f.added, 2);
  assert.equal(f.deleted, 0);
});

test("pairChanges pairs removes with following adds; wordSplit isolates the middle", () => {
  const lines = [{ kind: "context", text: "a" }, { kind: "remove", text: "foo bar baz" }, { kind: "add", text: "foo QUX baz" }];
  const pairs = pairChanges(lines);
  assert.equal(pairs.get(1), "foo QUX baz");
  assert.equal(pairs.get(2), "foo bar baz");
  assert.deepEqual(wordSplit("foo bar baz", "foo QUX baz"), { pre: "foo ", a: "bar", b: "QUX", post: " baz" });
});

test("serverLinesToFile numbers lines from the hunk header", () => {
  const f = serverLinesToFile("n", [
    { kind: "header", text: "--- a" }, { kind: "hunk", text: "@@ -5,2 +5,2 @@" },
    { kind: "context", text: " keep" }, { kind: "remove", text: "-old" }, { kind: "add", text: "+new" },
  ]);
  const l = f.hunks[0].lines;
  assert.deepEqual([l[0].old, l[0].new, l[1].old, l[2].new], [5, 5, 6, 6]);
  assert.equal(l[1].text, "old");
});

test("parseUnified and parseApplyPatch", () => {
  const u = parseUnified("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n");
  assert.equal(u[0].path, "x");
  assert.equal(u[0].added + u[0].deleted, 2);
  const p = parseApplyPatch("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x = 1\n+x = 2\n*** End Patch");
  assert.equal(p[0].path, "src/a.py");
  assert.equal(p[0].added, 1);
});

test("parseInput survives the server's 8000-char truncation", () => {
  const body = "x".repeat(50);
  const raw = `{"filePath": "/a/b.md", "content": "${body}\n…[truncated]`;
  const i = parseInput(raw.replace('"content": "', '"content": "'));
  assert.equal(i.filePath, "/a/b.md");
  assert.ok(i.content.startsWith("xxx"));
  assert.equal(i._truncated, true);
  assert.deepEqual(parseInput('{"a":1}'), { a: 1 });
  assert.deepEqual(parseInput("ls -la"), { raw: "ls -la" });
});

test("tool kinds, titles and diffs", () => {
  assert.equal(toolKind("Edit"), "edit");
  assert.equal(toolKind("shell"), "bash");
  assert.equal(toolKind("mystery"), "other");
  assert.equal(toolTitle("bash", { command: "ls -la\necho" }), "ls -la");
  assert.equal(toolTitle("read", { filePath: "/x.py", offset: 10, limit: 5 }), "/x.py:10+5");
  const e = toolDiffFiles("edit", { filePath: "/x.py", oldString: "a = 1", newString: "a = 2" });
  assert.equal(e[0].added, 1);
  assert.equal(e[0].hunks[0].lines[0].old, null); // snippet numbers are hidden
  const w = toolDiffFiles("write", { filePath: "/y.md", content: "l1\nl2" });
  assert.equal(w[0].status, "added");
  assert.equal(toolDiffFiles("bash", { command: "ls" }), null);
});

test("buildFeed pairs calls with results and drops tool_use markers", () => {
  const entries = [
    { role: "tool_call", tool_name: "read", tool_id: "c1", tool_input: '{"filePath":"/a"}', text: "read", ts: 0 },
    { role: "tool_call", tool_name: "read", tool_id: "", tool_input: null, text: "tool_use read", ts: 1 },
    { role: "tool", tool_name: "read", tool_id: "c1", text: "tool_result: contents", tool_output: null, ts: 2 },
    { role: "tool_call", tool_name: "bash", tool_id: "c2", tool_input: '{"command":"ls"}', text: "bash", ts: 3 },
    { role: "thinking", text: "hm", ts: 4 },
    { role: "usage", text: "", tokens: 10, prompt_tokens: 8, completion_tokens: 2, ts: 5 },
  ];
  const items = buildFeed(entries, { live: true });
  assert.deepEqual(items.map((i) => i.kind), ["tool", "tool", "thinking", "usage"]);
  assert.equal(items[0].result, "contents");
  assert.equal(items[0].done, true);
  assert.equal(items[1].done, false); // newest unresolved call on a live agent is still running
  assert.equal(buildFeed(entries, { live: false })[1].unknown, true);
});

test("gptme-style calls carry the path in the call text", () => {
  const save = inputFromCall("save", "line1\nline2", "save out/a.md");
  assert.deepEqual(save, { path: "out/a.md", content: "line1\nline2" });
  const patch = "<<<<<<< ORIGINAL\nx = 1\n=======\nx = 2\n>>>>>>> UPDATED\n<<<<<<< ORIGINAL\ny\n=======\nz\n>>>>>>> UPDATED";
  const files = toolDiffFiles("patch", inputFromCall("patch", patch, "patch app.py"));
  assert.equal(files[0].path, "app.py");
  assert.equal(files[0].hunks.length, 2);
  assert.equal(files[0].added, 2);
  assert.deepEqual(inputFromCall("shell", "ls -la", "shell"), { command: "ls -la" });
  assert.equal(toolTitle("save", save), "out/a.md");
});

test("gptme results (system entries) and server diffs attach to their call", () => {
  const items = buildFeed([
    { role: "tool_call", tool_name: "save", tool_input: "a\nb", text: "save f.md", ts: 0 },
    { role: "diff", text: "--- f.md\n+++ f.md\n@@ -0,0 +1,2 @@\n+a\n+b", ts: 1 },
    { role: "system", text: "Saved to f.md", ts: 2 },
    { role: "system", text: "── new attempt ──", ts: 3 },
  ]);
  assert.deepEqual(items.map((i) => i.kind), ["tool", "system"]);
  assert.equal(items[0].result, "Saved to f.md");
  assert.equal(items[0].done, true);
  assert.ok(items[0].serverDiff.includes("@@"));
});

test("changedPaths sums edits per file", () => {
  const items = buildFeed([
    { role: "tool_call", tool_name: "edit", tool_id: "a", tool_input: '{"filePath":"/f","oldString":"1","newString":"2"}', text: "edit", ts: 0 },
    { role: "tool", tool_name: "edit", tool_id: "a", text: "tool_result: ok", ts: 1 },
    { role: "tool_call", tool_name: "write", tool_id: "b", tool_input: '{"filePath":"/f","content":"x\\ny"}', text: "write", ts: 2 },
    { role: "tool", tool_name: "write", tool_id: "b", text: "tool_result: ok", ts: 3 },
  ]);
  const c = changedPaths(items);
  assert.equal(c.length, 1);
  assert.equal(c[0].count, 2);
  assert.equal(c[0].added, 3);
});

test("buildTree groups dotted ids under virtual folders and honours parents", () => {
  const rows = [
    { id: "c03.thermo.a", status: "passed" }, { id: "c03.thermo.b", status: "failed" },
    { id: "root", status: "split" }, { id: "kid", parent: "root", status: "pending" },
  ];
  const roots = buildTree(rows);
  const names = roots.map((r) => r.id).sort();
  assert.deepEqual(names, ["c03", "root"]);
  assert.deepEqual(roots.map((r) => r.id), ["c03", "root"]); // plan order, folders where their first child is
  const c03 = roots.find((r) => r.id === "c03");
  assert.equal(c03.virtual, true);
  assert.equal(c03.children[0].id, "c03.thermo");
  assert.deepEqual(c03.counts, { passed: 1, failed: 1 });
  const flat = flattenTree(roots, new Set(["c03"]));
  assert.ok(!flat.some((r) => r.id === "c03.thermo"));
  assert.ok(flat.some((r) => r.id === "kid" && r.depth === 1));
});

test("buildScore turns events into episodes, marks and phase spans", () => {
  const events = [
    { type: "phase_started", phase: "execute", node_id: "-", ts: 100 },
    { type: "node_dispatched", node_id: "u1", ts: 101 },
    { type: "node_gate_failed", node_id: "u1", ts: 150, unmet: ["units_min"] },
    { type: "episode_completed", node_id: "u1", ts: 160, status: "timeout", duration_ms: 59000 },
    { type: "node_redispatched", node_id: "u1", ts: 165 },
    { type: "phase_done", phase: "execute", node_id: "-", ts: 200, status: "done" },
  ];
  const s = buildScore(events, [{ id: "u1" }, { id: "u2" }], { now: 300, running: true });
  const u1 = s.lanes.find((l) => l.id === "u1");
  assert.equal(u1.episodes.length, 2);
  assert.equal(u1.episodes[0].status, "timeout");
  assert.equal(u1.episodes[1].status, "running");
  assert.equal(u1.marks[0].tone, "bad");
  assert.equal(s.phases[0].end, 200);
  assert.equal(s.t1, 300);
  assert.equal(buildScore([], [], { now: 1, running: false }).empty, true);
  assert.equal(eventTone("node_review_unavailable"), "warn");
});

test("command parsing", () => {
  const cmd = { params: [{ key: "node", type: "node" }, { key: "defect", type: "text", optional: true }] };
  assert.deepEqual(parseArgs(cmd, "u1 it broke badly"), { node: "u1", defect: "it broke badly" });
  assert.deepEqual(missingParams(cmd, {}).map((p) => p.key), ["node"]);
  assert.ok(fuzzy("rdsp", "redispatch") > 0);
  assert.equal(fuzzy("zzz", "redispatch"), -1);
});

test("formatters", () => {
  assert.equal(fmtDur(3725), "1h02m");
  assert.equal(fmtDur(65), "1m05s");
  assert.equal(fmtTokens(12345), "12.3k");
  assert.equal(shortId("abcdefghij", 6).length, 6);
  assert.equal(clip("a   b\nc", 10), "a b c");
});

console.log(`${n} passed`);
