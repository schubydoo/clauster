#!/usr/bin/env node
// CSP-build expression guard (#533).
//
// The vendored @alpinejs/csp build evaluates directive expressions with its own
// parser instead of `new Function()`. Verified against @alpinejs/csp@3.15.12
// dist: the parser accepts ONE expression (ternary, && || !, arithmetic /
// comparison, string concat, method calls, computed indexing, object/array
// literals, `=` assignment incl. member targets, ++/--) and REJECTS template
// literals, arrow fns, `?.`, `??`, spread, `new`, `function`, `if` statements,
// and multi-statement `;` sequences. Bare identifiers resolve ONLY against the
// component data-stack — `throw "Undefined variable"`, no window fallback — and
// `x-data` factories resolve ONLY through the Alpine.data() registry.
//
// This script mechanically proves the templates comply:
//   (a) no directive expression contains an unsupported construct;
//   (b) every bare root identifier in a directive is a property of a registered
//       component (dashboard / projectRow / reaper / loginShepherd), an inline
//       x-data key, an Alpine magic, or an x-for iterator variable.
//
// Run: node scripts/check_csp_expressions.mjs   (exit 1 on any violation)

import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const TEMPLATES = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "clauster", "templates");

// ---------- gather component properties from the dashboard script ----------

const scriptSrc = readFileSync(join(TEMPLATES, "_dashboard_script.html"), "utf8");

function componentProps(fnName) {
  const start = scriptSrc.indexOf(`function ${fnName}(`);
  if (start === -1) throw new Error(`component factory not found: ${fnName}`);
  // Brace-match from the factory's opening brace to its close. Braces inside string
  // literals ('...', "...", `...`), regex literals, and // or /* */ comments must NOT
  // move `depth` — a small char-by-char state machine skips over them so a `{`/`}` in
  // a string or comment can't truncate the extracted body at the wrong brace.
  const open = scriptSrc.indexOf("{", start);
  let depth = 0;
  let end = open;
  let mode = "code"; // code | line-comment | block-comment | sq | dq | tpl | regex
  for (; end < scriptSrc.length; end++) {
    const ch = scriptSrc[end];
    const next = scriptSrc[end + 1];
    const prevNonWs = () => {
      let j = end - 1;
      while (j >= 0 && /\s/.test(scriptSrc[j])) j--;
      return scriptSrc[j];
    };
    switch (mode) {
      case "line-comment":
        if (ch === "\n") mode = "code";
        break;
      case "block-comment":
        if (ch === "*" && next === "/") { mode = "code"; end++; }
        break;
      case "sq":
        if (ch === "\\") end++;
        else if (ch === "'") mode = "code";
        break;
      case "dq":
        if (ch === "\\") end++;
        else if (ch === '"') mode = "code";
        break;
      case "tpl":
        if (ch === "\\") end++;
        else if (ch === "`") mode = "code";
        break;
      case "regex":
        if (ch === "\\") end++;
        else if (ch === "/") mode = "code";
        break;
      default: // code
        if (ch === "/" && next === "/") { mode = "line-comment"; end++; }
        else if (ch === "/" && next === "*") { mode = "block-comment"; end++; }
        else if (ch === "'") mode = "sq";
        else if (ch === '"') mode = "dq";
        else if (ch === "`") mode = "tpl";
        // Regex literal vs division: a `/` starting a regex follows an operator, `(`,
        // `,`, `=`, `:`, `[`, `!`, `&`, `|`, `?`, `{`, `;`, `return`, or line start —
        // never a value/identifier/`)`/`]`. Good enough for this codebase's scripts.
        else if (ch === "/" && !/[\w$)\].]/.test(prevNonWs() || "")) mode = "regex";
        else if (ch === "{") depth++;
        else if (ch === "}") { depth--; if (depth === 0) { end++; break; } }
    }
    if (mode === "code" && depth === 0 && end > open) break;
  }
  const body = scriptSrc.slice(open, end);
  // Top-level members of the returned object literal sit at exactly 6-space indent.
  const props = new Set();
  for (const m of body.matchAll(/^ {6}(?:async |get )?([$A-Za-z_][\w$]*)\s*[:(]/gm)) {
    props.add(m[1]);
  }
  // Multi-prop state lines (`open: false, loading: false, …` — reaper/projectRow style):
  // collect the comma-separated keys too. Only lines that START with a key qualify, so
  // method bodies on 6-space lines don't leak locals into the allowed set.
  for (const lineMatch of body.matchAll(/^ {6}([$A-Za-z_][\w$]*\s*:.*)$/gm)) {
    for (const k of lineMatch[1].matchAll(/(?:^|,\s*)([$A-Za-z_][\w$]*)\s*:/g)) props.add(k[1]);
  }
  return props;
}

const componentPropSets = ["dashboard", "projectRow", "reaper", "loginShepherd"].map(
  componentProps,
);

// Factories registered on the Alpine.data() registry — the ONLY names an x-data
// expression can resolve under the CSP build.
const REGISTERED = new Set([...scriptSrc.matchAll(/Alpine\.data\("(\w+)"/g)].map((m) => m[1]));
if (REGISTERED.size === 0) {
  console.error("CSP expression check: no Alpine.data() registrations found — the CSP build cannot resolve any x-data factory.");
  process.exit(1);
}

// Alpine magics + expression-level keywords/literals the parser understands.
const MAGICS = new Set([
  "$el", "$refs", "$store", "$watch", "$dispatch", "$nextTick", "$root", "$data", "$id", "$event",
]);
const KEYWORDS = new Set(["true", "false", "null", "undefined", "in", "typeof"]);
// Jinja placeholders (see neutralization below).
const NEUTRAL = new Set(["__JINJA__"]);

// ---------- template scanning ----------

// Attribute names that never carry an evaluated expression.
const SKIP_ATTRS = /^(x-ref|x-cloak|x-ignore|x-transition|x-transition[.:].*|x-id)$/;

const UNSUPPORTED = [
  [/`/, "template literal (backtick)"],
  [/=>/, "arrow function"],
  [/\?\./, "optional chaining ?."],
  [/\?\?/, "nullish coalescing ??"],
  [/\.\.\./, "spread operator"],
  [/\bnew\b/, "new expression"],
  [/\bfunction\b/, "function expression"],
  [/\bif\s*\(/, "if statement"],
  [/;/, "multi-statement ; sequence"],
];

function neutralizeJinja(src) {
  return src
    .replace(/\{#[\s\S]*?#\}/g, "")
    .replace(/\{%[\s\S]*?%\}/g, "")
    .replace(/\{\{[\s\S]*?\}\}/g, "__JINJA__");
}

// Directives live in HTML markup only — <script> bodies (component implementations,
// whose comments may quote old directive syntax) must not be scanned.
function stripScripts(src) {
  // Blank the script BODY line-preservingly so reported line numbers stay true.
  return src.replace(/(<script\b[^>]*>)([\s\S]*?)(<\/script>)/g, (whole, open, body, close) =>
    open + body.replace(/[^\n]/g, "") + close);
}

function stripStrings(expr) {
  // HTML attribute values are double-quoted, so embedded JS strings are single-quoted.
  return expr.replace(/'(?:[^'\\]|\\.)*'/g, "''");
}

function rootIdentifiers(expr) {
  const s = stripStrings(expr);
  const roots = [];
  for (const m of s.matchAll(/[$A-Za-z_][\w$]*/g)) {
    const name = m[0];
    // Member access (a.b): skip when preceded by a dot.
    let j = m.index - 1;
    while (j >= 0 && /\s/.test(s[j])) j--;
    if (j >= 0 && s[j] === ".") continue;
    // Object-literal key ({ key: v } / , key: v): skip when framed by {|, and followed by :.
    let k = m.index + name.length;
    while (k < s.length && /\s/.test(s[k])) k++;
    if (s[k] === ":" && j >= 0 && (s[j] === "{" || s[j] === ",")) continue;
    roots.push(name);
  }
  return roots;
}

const files = readdirSync(TEMPLATES).filter((f) => f.endsWith(".html"));
const violations = [];
let checked = 0;

for (const file of files) {
  const src = stripScripts(neutralizeJinja(readFileSync(join(TEMPLATES, file), "utf8")));
  // Collect x-for iterator variables (allowed file-wide — a coarse but safe scope).
  const iterVars = new Set();
  for (const m of src.matchAll(/x-for="\(?\s*([\w$]+)\s*(?:,\s*([\w$]+)\s*)?\)?\s+in\s/g)) {
    iterVars.add(m[1]);
    if (m[2]) iterVars.add(m[2]);
  }
  // Collect inline x-data object keys (row-local ad-hoc state).
  const inlineKeys = new Set();
  for (const m of src.matchAll(/x-data="\{([^"]*)\}"/g)) {
    for (const k of m[1].matchAll(/([$A-Za-z_][\w$]*)\s*:/g)) inlineKeys.add(k[1]);
  }

  for (const m of src.matchAll(/\s((?:x-[\w.:-]+|[:@][\w.:@-]+))="([^"]*)"/g)) {
    const [, attr, rawValue] = m;
    if (SKIP_ATTRS.test(attr)) continue;
    let expr = rawValue.trim();
    if (!expr) continue;
    // x-for: the iterators are declarations, only the iterated expression evaluates.
    if (attr === "x-for") {
      const it = expr.match(/^\(?\s*[\w$]+\s*(?:,\s*[\w$]+\s*)?\)?\s+in\s+([\s\S]+)$/);
      if (it) expr = it[1];
    }
    checked++;
    const line = src.slice(0, m.index).split("\n").length;
    const stripped = stripStrings(expr).replace(/;\s*$/, "");
    for (const [re, label] of UNSUPPORTED) {
      if (re.test(stripped)) violations.push({ file, line, attr, label, expr });
    }
    for (const name of rootIdentifiers(expr)) {
      // x-data resolves against the Alpine.data() registry (plus inline object keys),
      // NOT the component data-stack.
      const known = attr === "x-data"
        ? (REGISTERED.has(name) || KEYWORDS.has(name) || NEUTRAL.has(name))
        : (KEYWORDS.has(name) || MAGICS.has(name) || NEUTRAL.has(name) ||
           iterVars.has(name) || inlineKeys.has(name) ||
           componentPropSets.some((s) => s.has(name)));
      if (!known) {
        violations.push({ file, line, attr, label: `unresolvable bare identifier '${name}'`, expr });
      }
    }
  }
}

if (violations.length) {
  console.error(`CSP expression check: ${violations.length} violation(s) in ${checked} directive expression(s)\n`);
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line} [${v.attr}] ${v.label}`);
    console.error(`      ${v.expr.slice(0, 140)}`);
  }
  process.exit(1);
}
console.log(`CSP expression check: OK — ${checked} directive expressions across ${files.length} templates, 0 violations.`);
