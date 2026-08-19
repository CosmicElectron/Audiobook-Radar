// Minimal DOM over the html5lib JSON tree, with a selector engine covering the
// selectors extract.js uses (comma lists, descendant/child combinators, compound
// tag/#id/.class/[attr op "val" i]). querySelector(All) return DOCUMENT ORDER.
"use strict";
function build(json, parent) {
  if (typeof json === "string") return { nodeType: 3, nodeValue: json, parentElement: parent, parentNode: parent };
  const [tag, attrs, kids] = json;
  const e = { nodeType: 1, tagName: tag.toUpperCase(), localName: tag, attrs: attrs || {}, children: [], childNodes: [], parentElement: parent, parentNode: parent };
  e.id = e.attrs.id || ""; e.className = e.attrs.class || "";
  e.getAttribute = (a) => (a in e.attrs ? e.attrs[a] : null);
  e.hasAttribute = (a) => (a in e.attrs);
  for (const k of kids) { const c = build(k, e); e.childNodes.push(c); if (c.nodeType === 1) e.children.push(c); }
  Object.defineProperty(e, "textContent", { get: () => tc(e) });
  e.querySelector = (s) => qsa(e, s, true)[0] || null;
  e.querySelectorAll = (s) => qsa(e, s, false);
  Object.defineProperty(e, "outerHTML", { get: () => ser(e), configurable: true });
  return e;
}
function tc(n) { return n.nodeType === 3 ? n.nodeValue : n.childNodes.map(tc).join(""); }
function ser(n) {
  if (n.nodeType === 3) return n.nodeValue;
  const a = Object.keys(n.attrs).map(k => ` ${k}="${String(n.attrs[k]).replace(/"/g, "&quot;")}"`).join("");
  return `<${n.localName}${a}>` + n.childNodes.map(ser).join("") + `</${n.localName}>`;
}
// ---- selector parsing ----
function parseCompound(s) {
  const c = { tag: null, id: null, classes: [], attrs: [] };
  let re = /^([a-zA-Z][\w-]*|\*)|#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+))\s*)?(\s+[iIsS])?\s*\]/y;
  let m, pos = 0;
  while (pos < s.length) {
    re.lastIndex = pos; m = re.exec(s);
    if (!m) throw new Error("bad selector piece: " + s.slice(pos) + " in " + s);
    pos = re.lastIndex;
    if (m[1] !== undefined) c.tag = m[1] === "*" ? null : m[1].toLowerCase();
    else if (m[2] !== undefined) c.id = m[2];
    else if (m[3] !== undefined) c.classes.push(m[3]);
    else c.attrs.push({ name: m[4], op: m[5] || null, val: m[6] !== undefined ? m[6] : m[7] !== undefined ? m[7] : m[8], ci: !!(m[9] && /i/i.test(m[9])) });
  }
  return c;
}
function parseSelector(sel) {
  // returns list of {compound, comb} from left to right; comb is combinator preceding compound
  // tokenise: compound selectors (bracket groups may contain spaces) and combinators
  const toks = []; const tre = /\s*(>)\s*|\s+|((?:\[[^\]]*\]|[^\s>\[])+)/gy; let mm; tre.lastIndex = 0;
  const str = sel.trim();
  while (tre.lastIndex < str.length && (mm = tre.exec(str))) { if (mm[1]) toks.push(">"); else if (mm[2]) toks.push(mm[2]); }
  const out = []; let comb = null;
  for (const t of toks) { if (t === ">") { comb = ">"; continue; } out.push({ c: parseCompound(t), comb: comb || " " }); comb = null; }
  return out;
}
const cache = new Map();
function parseList(sel) {
  if (cache.has(sel)) return cache.get(sel);
  const r = sel.split(",").map(s => s.trim()).filter(Boolean).map(parseSelector);
  cache.set(sel, r); return r;
}
function matchCompound(n, c) {
  if (n.nodeType !== 1) return false;
  if (c.tag && n.localName !== c.tag) return false;
  if (c.id !== null && n.id !== c.id) return false;
  if (c.classes.length) { const cl = (" " + n.className.replace(/\s+/g, " ") + " "); for (const k of c.classes) if (!cl.includes(" " + k + " ")) return false; }
  for (const a of c.attrs) {
    if (!(a.name in n.attrs)) return false;
    if (!a.op) continue;
    let av = String(n.attrs[a.name]), v = a.val;
    if (a.ci) { av = av.toLowerCase(); v = v.toLowerCase(); }
    if (a.op === "=") { if (av !== v) return false; }
    else if (a.op === "^=") { if (!v || !av.startsWith(v)) return false; }
    else if (a.op === "$=") { if (!v || !av.endsWith(v)) return false; }
    else if (a.op === "*=") { if (!v || !av.includes(v)) return false; }
    else if (a.op === "~=") { if (!(" " + av + " ").includes(" " + v + " ")) return false; }
    else if (a.op === "|=") { if (!(av === v || av.startsWith(v + "-"))) return false; }
  }
  return true;
}
function matchChain(n, chain, root) {
  // right-to-left
  let i = chain.length - 1;
  if (!matchCompound(n, chain[i].c)) return false;
  let cur = n;
  while (i > 0) {
    const comb = chain[i].comb; i--;
    if (comb === ">") { cur = cur.parentElement; if (!cur || cur === root || cur.nodeType !== 1 || !matchCompound(cur, chain[i].c)) return false; }
    else { let p = cur.parentElement, ok = false; while (p && p !== root) { if (matchCompound(p, chain[i].c)) { ok = true; break; } p = p.parentElement; } if (!ok) return false; cur = p; }
  }
  return true;
}
function qsa(root, sel, first) {
  const list = parseList(sel), out = [];
  const stack = [];
  for (let i = root.children.length - 1; i >= 0; i--) stack.push(root.children[i]);
  const rootScope = root.nodeType === 9 ? null : root; // ancestors outside scope may still match for descendant combinator per spec? (spec: yes, ancestors outside the scope element DO count for descendant combinator). Keep spec behaviour: pass null.
  while (stack.length) {
    const n = stack.pop();
    for (const chain of list) { if (matchChain(n, chain, null)) { out.push(n); break; } }
    if (first && out.length) return out;
    for (let i = n.children.length - 1; i >= 0; i--) stack.push(n.children[i]);
  }
  return out;
}
function makeDoc(json) {
  const root = build(json.root, null);
  const doc = { nodeType: 9, title: json.title || "", documentElement: root, children: [root], childNodes: [root] };
  doc.querySelector = (s) => qsa(doc, s, true)[0] || null;
  doc.querySelectorAll = (s) => qsa(doc, s, false);
  doc.createTreeWalker = (scope, what) => {
    const nodes = [];
    (function w(n) { for (const c of n.childNodes) { if (c.nodeType === 3) nodes.push(c); else w(c); } })(scope);
    let i = -1; return { nextNode: () => (++i < nodes.length ? nodes[i] : null) };
  };
  // fake outerHTML length = raw bytes (only used for _diag.htmlLen)
  Object.defineProperty(root, "outerHTML", { get: () => "x".repeat(json.htmlLen || 0) });
  return doc;
}
module.exports = { makeDoc, build, qsa };
