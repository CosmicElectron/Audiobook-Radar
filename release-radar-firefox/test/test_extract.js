// Pin the pre-order/buyable detector against buy-box shapes captured from live
// Amazon pages. Run:  node release-radar-firefox/test/test_extract.js
//
// Both shapes fooled earlier detectors:
//   * a RELEASED audiobook whose only controls were "Try Standard free",
//     "Buy now with 1-Click" (auto-id button, class a-button-oneclick), a
//     "Preview" sample player and "Give as Gift" -> read as null for a week,
//     so the release ping never fired
//   * an audiobook PRE-ORDER that also shows "Buy now with 1-Click", with the
//     words "Pre-order purchase / Releases 25 August 2026" only as plain text
//     in an accordion row -> a buttons-only detector calls it released
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// ---- a tiny DOM: just enough for RR_extract's selectors and text walk ----
function el(tag, attrs, children) {
  const e = { tagName: tag.toUpperCase(), attrs: attrs || {}, children: children || [], parentElement: null };
  e.id = e.attrs.id || ""; e.className = e.attrs.class || "";
  e.getAttribute = (a) => (a in e.attrs ? e.attrs[a] : null);
  return e;
}
function text(s) { return { nodeType: 3, nodeValue: s, parentElement: null }; }
function link(node) { (node.children || []).forEach(c => { c.parentElement = node; if (c.children) link(c); }); }
function walk(node, fn) { fn(node); (node.children || []).forEach(c => { if (c.children) walk(c, fn); }); }
function tc(node) { return node.nodeType === 3 ? node.nodeValue : (node.children || []).map(tc).join(""); }
function matches(node, sel) {
  if (!node.tagName) return false;
  sel = sel.trim();
  let m;
  if ((m = sel.match(/^#([\w-]+)$/))) return node.id === m[1];
  if ((m = sel.match(/^\.([\w-]+)$/))) return (" " + node.className + " ").includes(" " + m[1] + " ");
  if ((m = sel.match(/^([a-z]+)$/i))) return node.tagName === m[1].toUpperCase();
  if ((m = sel.match(/^([a-z]+)?\[([\w-]+)(?:([\^$*]?)="([^"]*)")?(\s+i)?\]$/i))) {
    const [, tag, attr, op, val, ci] = m;
    if (tag && node.tagName !== tag.toUpperCase()) return false;
    let a = attr === "id" ? node.id : attr === "class" ? node.className : node.attrs[attr];
    if (a === undefined || a === null || (a === "" && !(attr in node.attrs))) return false;
    if (val === undefined) return true;
    a = String(a); let v = val;
    if (ci) { a = a.toLowerCase(); v = v.toLowerCase(); }
    if (op === "^") return a.startsWith(v); if (op === "$") return a.endsWith(v); if (op === "*") return a.includes(v);
    return a === v;
  }
  return false;
}
function qsa(root, selector) {
  const out = [];
  selector.split(",").map(s => s.trim()).filter(Boolean).forEach(sel => {
    const parts = sel.split(/\s+/);
    walk(root, n => {
      if (parts.length === 1) { if (matches(n, sel) && !out.includes(n)) out.push(n); return; }
      if (!matches(n, parts[parts.length - 1])) return;
      let p = n.parentElement;
      while (p) { if (matches(p, parts[0])) { if (!out.includes(n)) out.push(n); break; } p = p.parentElement; }
    });
  });
  return out;
}
function makeDoc(body) {
  link(body);
  walk(body, n => {
    Object.defineProperty(n, "textContent", { get: () => tc(n) });
    // RR_extract scopes its search to the buy box element, so elements need
    // querySelector/All just like the document does
    n.querySelector = s => qsa(n, s)[0] || null;
    n.querySelectorAll = s => qsa(n, s);
  });
  return {
    body, title: "",
    querySelector: s => qsa(body, s)[0] || null,
    querySelectorAll: s => qsa(body, s),
    createTreeWalker: (root) => {
      const nodes = []; walk(root, n => (n.children || []).forEach(c => { if (c.nodeType === 3) nodes.push(c); }));
      let i = -1; return { nextNode: () => (++i < nodes.length ? nodes[i] : null) };
    },
  };
}
const btn = (label, cls, id) => el("span", { class: "a-button " + (cls || ""), id: id || "" }, [
  el("span", { class: "a-button-inner" }, [el("span", { class: "a-button-text" }, [text(label)])])]);
const rpi = (name, value) => el("div", { class: "rpi-attribute-content", "data-rpi-attribute-name": name }, [
  el("span", { class: "rpi-attribute-label" }, [text("")]), el("span", { class: "rpi-attribute-value" }, [text(value)])]);
const common = () => [
  el("span", { id: "productTitle" }, [text("Some Series 15: A LitRPG Adventure")]),
  rpi("book_details-series", "Some Series"),
  rpi("book_details-publication_date", "19 August 2026"),
  rpi("audiobook_details-narrator", "A Narrator"),
  rpi("audiobook_details-program_type", "Audiobook"),
  rpi("book_details-asin", "B0TESTAUD1"),
];

// fixture 1: released audiobook (Primal Hunter 15 shape, captured 19 Aug 2026)
const released = makeDoc(el("body", {}, [
  ...common(),
  el("div", { id: "rightCol" }, [
    el("div", { id: "buybox" }, [
      el("script", {}, [text("if (typeof uet == 'function') { uet('bb', 'audibleMediaMatrix'); } PreorderButton")]),
      btn("Kindle $0.00 or $7.99 to buy", "a-button-toggle format"),
      btn("Try Standard free", "a-button-primary", "a-autoid-3"),
      btn("More purchase options", "a-button-base", "a-autoid-4"),
      btn("Buy now with 1-Click", "a-button-oneclick a-button-icon", "a-autoid-5"),
      btn("Give as Gift", "a-button-base", "audible-title-giftv2-button"),
      btn("Preview", "a-button-base cloud-sample-player-width", "cloudPlayerSampleID"),
    ]),
  ]),
]));

// fixture 2: audiobook PRE-ORDER (Skill Hunter 2 shape) — same 1-Click button, plain-text label
const preorder = makeDoc(el("body", {}, [
  ...common(),
  el("div", { id: "rightCol" }, [
    el("div", { id: "buybox" }, [
      el("script", {}, [text("PreorderButton config")]),
      btn("Kindle $0.00 or $4.99 to buy", "a-button-toggle format"),
      el("div", { id: "audibleUpsellAccordionRow", class: "a-box a-accordion-active" }, [
        el("div", { class: "a-accordion-inner accordion-row-content" }, [
          el("span", {}, [text("Pre-order purchase")]), el("span", {}, [text("Releases 25 August 2026")]),
          btn("Try for $0", "a-button-primary", "a-autoid-3"),
        ]),
      ]),
      btn("Buy now with 1-Click", "a-button-oneclick a-button-icon", "a-autoid-5"),
      btn("Add to Wish List", "a-button-base", "wishListMainButton"),
    ]),
  ]),
]));

// fixture 3: Kindle pre-order with the classic pre-order button
const kindlePre = makeDoc(el("body", {}, [
  ...common(),
  el("div", { id: "buybox" }, [
    el("span", { id: "buy-now-button-announce" }, [text("Pre-order now with 1-Click")]),
    el("input", { id: "one-click-button", type: "submit", class: "a-button-input" }),
    el("div", { id: "availability" }, [text("This title will be released on 25 August 2026.")]),
  ]),
]));

// fixture 4: no buy box at all -> must be null, never a guess
const unknown = makeDoc(el("body", {}, [...common()]));

// fixture 5: released, but a SCRIPT in the buy box mentions pre-orders -> still released
const scriptNoise = makeDoc(el("body", {}, [
  ...common(),
  el("div", { id: "buybox" }, [
    el("script", {}, [text("var cfg = {preorder: false, label: 'Pre-order'};")]),
    btn("Buy now with 1-Click", "a-button-oneclick", "a-autoid-5"),
  ]),
]));

// ---- load the real extractor ----
const src = fs.readFileSync(path.join(__dirname, "..", "extract.js"), "utf8");
const sandbox = { console, URL, self: {} }; vm.createContext(sandbox); vm.runInContext(src, sandbox);
const RR = sandbox.RR_extract;
const url = "https://www.amazon.com.au/dp/B0TESTAUD1";

let fails = 0;
function check(name, doc, want) {
  const got = RR(doc, url)._preorder;
  const ok = got === want; if (!ok) fails++;
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name.padEnd(52)} got=${String(got).padEnd(5)} want=${want}`);
}
console.log("RR_extract pre-order detection:");
check("released audiobook (Primal Hunter 15 shape)", released, false);
check("audiobook pre-order with 1-Click button (Skill Hunter 2)", preorder, true);
check("Kindle pre-order (classic button + availability)", kindlePre, true);
check("no buy box at all", unknown, null);
check("released, script in buy box mentions pre-order", scriptNoise, false);
console.log(fails ? `\n${fails} FAILURE(S)` : "\nall detector cases correct");
process.exit(fails ? 1 : 0);
