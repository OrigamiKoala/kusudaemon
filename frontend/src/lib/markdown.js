import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import json from "highlight.js/lib/languages/json";
import bash from "highlight.js/lib/languages/bash";
import markdown from "highlight.js/lib/languages/markdown";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import rust from "highlight.js/lib/languages/rust";
import go from "highlight.js/lib/languages/go";
import sql from "highlight.js/lib/languages/sql";
import latex from "highlight.js/lib/languages/latex";
import ini from "highlight.js/lib/languages/ini";
import cpp from "highlight.js/lib/languages/cpp";
import java from "highlight.js/lib/languages/java";

const langs = { python, javascript, typescript, json, bash, markdown, css, xml, yaml, rust, go, sql, latex, ini, cpp, java };
for (const [name, def] of Object.entries(langs)) hljs.registerLanguage(name, def);
const alias = {
  py: "python", js: "javascript", jsx: "javascript", mjs: "javascript", ts: "typescript", tsx: "typescript",
  sh: "bash", zsh: "bash", shell: "bash", md: "markdown", html: "xml", svelte: "xml", vue: "xml", yml: "yaml",
  tex: "latex", toml: "ini", rs: "rust", c: "cpp", h: "cpp", hpp: "cpp", cc: "cpp", jsonl: "json",
};

const EXT = {
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
  json: "json", jsonl: "json", sh: "bash", md: "markdown", css: "css", html: "xml", xml: "xml", svg: "xml", svelte: "xml",
  yml: "yaml", yaml: "yaml", rs: "rust", go: "go", sql: "sql", tex: "latex", toml: "ini", ini: "ini", cfg: "ini",
  c: "cpp", h: "cpp", cpp: "cpp", hpp: "cpp", cc: "cpp", java: "java",
};

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function langForPath(path) {
  const m = /\.([A-Za-z0-9]+)$/.exec(path || "");
  return m ? EXT[m[1].toLowerCase()] || "" : "";
}

/** Highlighted HTML for a code string (escaped when the language is unknown). */
export function highlight(code, lang) {
  const name = alias[lang] || lang;
  if (name && hljs.getLanguage(name)) {
    try {
      return hljs.highlight(code, { language: name, ignoreIllegals: true }).value;
    } catch {
      /* fall through */
    }
  }
  return esc(code);
}

marked.setOptions({ gfm: true, breaks: false });
const renderer = new marked.Renderer();
renderer.code = ({ text, lang }) => {
  const l = (lang || "").split(/\s+/)[0];
  return `<pre><code class="hljs">${highlight(text, l)}</code></pre>`;
};
marked.use({ renderer });

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

/** Markdown -> sanitized HTML. */
export function renderMarkdown(text) {
  if (!text) return "";
  return DOMPurify.sanitize(marked.parse(text, { async: false }), { USE_PROFILES: { html: true } });
}
