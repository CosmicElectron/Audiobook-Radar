// RR_pageStep (the in-tab borrow step) against small fake Kindle pages.
// Exists because Riftside 4's purchase stopped at "NO borrow / Add-to-library
// button found" (29 Sep 2026): the book was already in the Kindle library, so
// the page showed "Read Now" instead - and nothing tested this step at all.
//
//   node release-radar-firefox/test/test_pagestep.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

process.exitCode = 1;
let fails = 0;
function chk(name, ok, extra) { if (ok) return; fails++; console.log("  FAIL " + name + (extra ? "  " + extra : "")); }

// ---- a DOM just big enough for RR_pageStep's borrow path ----
function el(tag, props, kids) {
  const e = Object.assign({ tagName: tag.toUpperCase(), id: "", className: "", attrs: {}, children: [], parentElement: null, text: "" }, props || {});
  (kids || []).forEach(k => { k.parentElement = e; e.children.push(k); });
  e.getAttribute = n => (n === "id" ? e.id || null : (n in e.attrs ? e.attrs[n] : null));
  e.getBoundingClientRect = () => ({ width: 80, height: 20 });
  e.dispatchEvent = ev => { (e.events = e.events || []).push(ev.type); return true; };
  e.scrollIntoView = () => {}; e.focus = () => {}; e.click = () => { (e.events = e.events || []).push("click"); };
  Object.defineProperty(e, "textContent", { get: () => e.text + e.children.map(c => c.textContent).join(" ") });
  Object.defineProperty(e, "innerText", { get: () => e.textContent });
  Object.defineProperty(e, "outerHTML", { get: () => "<" + tag + ">" + e.textContent + "</" + tag + ">" });
  e.closest = sel => { for (let n = e; n; n = n.parentElement) if (matches(n, sel)) return n; return null; };
  e.querySelectorAll = sel => all(e).filter(n => n !== e && matches(n, sel));
  e.querySelector = sel => e.querySelectorAll(sel)[0] || null;
  return e;
}
function all(root) { return [root].concat(...root.children.map(all)); }
function matchOne(n, s) {
  s = s.trim();
  const m = s.match(/^([a-z]+)?((?:[#.][\w-]+)*)((?:\[[^\]]+\])*)$/i);
  if (!m) return false;                               // selectors the fake doesn't speak never match
  if (m[1] && n.tagName !== m[1].toUpperCase()) return false;
  for (const part of (m[2].match(/[#.][\w-]+/g) || [])) {
    if (part[0] === "#" && n.id !== part.slice(1)) return false;
    if (part[0] === "." && !(" " + n.className + " ").includes(" " + part.slice(1) + " ")) return false;
  }
  for (const a of (m[3].match(/\[[^\]]+\]/g) || [])) {
    const am = a.match(/^\[([\w-]+)(?:([*]?=)["']([^"']*)["'](?:\s+i)?)?\]$/);
    if (!am) return false;
    const v = n.getAttribute(am[1]);
    if (v == null) return false;
    if (am[2] === "=" && String(v).toLowerCase() !== am[3].toLowerCase()) return false;
    if (am[2] === "*=" && !String(v).toLowerCase().includes(am[3].toLowerCase())) return false;
  }
  return true;
}
function matches(n, sel) { return sel.split(",").some(s => matchOne(n, s)); }
function button(label, id) {
  const inp = el("input", { attrs: { type: "submit", value: label }, className: "a-button-input" });
  return el("span", { id: id || "", className: "a-button" }, [inp]);
}
function page(buttons, bodyText) {
  const buybox = el("div", { id: "buybox" }, buttons);
  const body = el("body", { text: bodyText || "" }, [buybox]);
  const doc = el("html", {}, [body]);
  doc.title = "Riftside 4: A LitRPG Fantasy Adventure eBook";
  doc.body = body; doc.documentElement = doc;
  return doc;
}

const sandbox = {
  console, URL, self: {},
  window: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) },
  location: { href: "https://www.amazon.com.au/dp/B0FSS4Z6BN" },
  MouseEvent: function (type) { this.type = type; }, Event: function (type) { this.type = type; },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "buy.js"), "utf8"), sandbox);
const step = (doc, mode) => { sandbox.document = doc; return sandbox.RR_pageStep({ mode: mode, price: 7.47, cap: 10, audio_asin: "B0HJ9DX4S2", cfg: {} }); };

console.log("RR_pageStep borrow path against fake Kindle pages:");

// 1. already in the library: "Read Now", no borrow button
let d = page([button("Read Now", "read-now-button"), button("Buy now with 1-Click", "one-click-button")]);
let r = step(d, "borrow");
chk("'Read Now' page: borrow step succeeds", r.ok === true, r.info);
chk("'Read Now' page: nothing clicked (the borrow isn't ours)", r.clicked === false);
chk("'Read Now' page: moves on to the audiobook offer", r.needs_offer === true);
chk("'Read Now' page: flagged in_library", r.found.in_library === true);
chk("'Read Now' page: says why", /already in your Kindle library/.test(r.info), r.info);
chk("'Read Now' page: look mode agrees", step(d, "look").ok === true);

// 2. a normal KU title: one "Add to library" button is clicked
d = page([button("Add to library", "borrow-button"), button("Buy now with 1-Click", "one-click-button")]);
r = step(d, "borrow");
chk("KU page: borrow button clicked", r.ok === true && r.clicked === true, r.info);
chk("KU page: not treated as in the library", r.found.in_library === false);

// 3. neither: stop, clearly
d = page([button("Buy now with 1-Click", "one-click-button")]);
r = step(d, "borrow");
chk("no borrow and no Read Now: refuses", r.ok === false && r.clicked === false && /NO borrow/.test(r.info), r.info);

// 4. the old "you borrowed this" text path still works
d = page([button("Buy now with 1-Click", "one-click-button")], "You borrowed this title on 3 March 2026.");
r = step(d, "borrow");
chk("'you borrowed this' text: skips the borrow", r.ok === true && r.clicked === false && r.needs_offer === true, r.info);

// ---- confirm: evidence for THIS purchase, never the first price on the page ----
function thankYou(text) {
  const d = page([], text);
  d.title = "Amazon.com.au: Audible Thank You";
  return d;
}
const confirm = (doc, extra) => { sandbox.document = doc; return sandbox.RR_pageStep(Object.assign({ mode: "confirm", price: 7.47, cfg: {},
  title: "Riftside 4: A LitRPG Fantasy Adventure", audio_asin: "B0HJ9DX4S2" }, extra || {})); };
const carousel = " Listeners are also looking for Verity Audible Audiobook $16.45/mo for Audible membership";
r = confirm(thankYou("Thank you for choosing this listen. Thank you for choosing Riftside 4: A LitRPG Fantasy Adventure." + carousel));
chk("real Audible thank-you page confirms", r.ok === true, r.info);
chk("...and says it names this book", /names this book/.test(r.info), r.info);
chk("...never reports a carousel price as the charge", !/16\.45/.test(r.info), r.info);
r = confirm(thankYou("Thank you! Start listening in the Audible app." + carousel));
chk("a thank-you page about something else is NOT a confirmation", r.ok === false && /doesn't name this audiobook/.test(r.info), r.info);
r = confirm(thankYou("Order placed. Order total: $7.47 Audible audiobook."));
chk("the approved price on a thank-you page confirms", r.ok === true && /shows the \$7\.47/.test(r.info), r.info);
r = confirm(thankYou("Order placed. Order total: $17.47 Audible audiobook."));
chk("a different price that merely ends in 7.47 doesn't", r.ok === false, r.info);

console.log(fails ? "\n" + fails + " FAILURE(S)" : "\nall page-step checks correct");
process.exit(fails ? 1 : 0);
