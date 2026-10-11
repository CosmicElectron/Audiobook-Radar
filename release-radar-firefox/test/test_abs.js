// The Audiobookshelf overlay: which series a page is about, where the chip
// goes, and what it says. Markup shapes are ABS client 2.37 as seen on
// the user's server (11 Oct 2026): the app bar's <h1>audiobookshelf</h1> comes
// first in every document, the series page names the series in a <p> inside
// #toolbar, the item page's series links sit beside the h1 in #item-page-wrapper.
//
//   node release-radar-firefox/test/test_abs.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");
const { makeDoc } = require("./minidom");

process.exitCode = 1;
const HERE = __dirname, CACHE = path.join(HERE, ".fixture-cache");
let fails = 0;
function chk(name, ok, extra) { if (ok) return; fails++; console.log("  FAIL " + name + (extra ? "  " + extra : "")); }
function havePython() { try { execFileSync("python", ["-c", "import html5lib"], { stdio: "ignore" }); return true; } catch (e) { return false; } }
if (!havePython()) { console.log("test_abs: python+html5lib not available — skipping"); process.exit(0); }
function docFor(html, key) {
  fs.mkdirSync(CACHE, { recursive: true });
  const src = path.join(CACHE, key + ".html"), out = path.join(CACHE, key + ".json");
  fs.writeFileSync(src, html);
  execFileSync("python", [path.join(HERE, "html2json.py"), src, out], { stdio: "inherit" });
  return makeDoc(JSON.parse(fs.readFileSync(out, "utf8")));
}

// abs.js wires timers and touches document/location only inside functions;
// the sandbox gives it inert timers and nothing else
const sandbox = { console, Date, Math, JSON, Promise, String, Array, setTimeout: () => 0, clearTimeout: () => 0, setInterval: () => 0, clearInterval: () => 0,
  fetch: () => Promise.resolve({ json: () => Promise.resolve({ series: [] }) }), document: { querySelector: () => null, querySelectorAll: () => [], title: "" }, location: { port: "13378", pathname: "/", href: "" } };
sandbox.self = sandbox; sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(HERE, "..", "abs.js"), "utf8"), sandbox, { filename: "abs.js" });

const APPBAR = `<div id="appbar" role="toolbar"><a href="/audiobookshelf/"><h1 class="text-xl mr-6 hidden lg:block">audiobookshelf</h1></a>
<h1 class="text-lg md:text-2xl px-4">0 items selected</h1>
<div class="globalSearchMenu"><a href="/audiobookshelf/library/L1/series/OTHER">Dual Class</a></div></div>`;

console.log("Audiobookshelf overlay:");
// --- series page: the name is a <p> in the toolbar, there is no heading at all
const series = docFor(`<html><head><title>Audiobookshelf</title></head><body><div id="__nuxt">${APPBAR}
<div id="app-content"><div class="page"><div class="w-full h-20 md:h-10 relative"><div id="toolbar" role="toolbar" class="flex items-center">
<p class="pl-2 text-base md:text-lg">Loopbreaker</p><div class="w-6 h-6 rounded-full"><span class="font-mono">2</span></div><div class="grow"></div></div></div></div></div>
<div class="modal hidden"><h1 class="text-3xl">Changelog</h1></div></div></body></html>`, "abs-series");
let pg = sandbox.rrAbsPage(series, "/audiobookshelf/library/L1/series/939c1909");
chk("series page: the toolbar paragraph names the series, chip goes inline after it",
    pg && pg.kind === "series" && pg.names.join() === "Loopbreaker" && pg.anchor && pg.anchor.tagName === "P" && pg.inline === true, JSON.stringify(pg && [pg.kind, pg.names]));
chk("series page: never the app bar's h1", !pg.anchor.textContent.includes("audiobookshelf"));
chk("the series LIST page (no series id) is not a series page", sandbox.rrAbsPage(series, "/audiobookshelf/library/L1/bookshelf/series") === null);
chk("a library page is nothing to us", sandbox.rrAbsPage(series, "/audiobookshelf/library/L1") === null);
const bare = docFor(`<html><body><div id="__nuxt">${APPBAR}<div id="app-content"><div id="toolbar"><p>12</p></div></div></div></body></html>`, "abs-series-bare");
chk("a toolbar showing only a count is not a series name", sandbox.rrAbsPage(bare, "/audiobookshelf/library/L1/series/x") === null);

// --- item page: title h1 inside #item-page-wrapper, series links beside it; the
// search dropdown's /series/ link earlier in the document must not win
const item = docFor(`<html><head><title>Audiobookshelf</title></head><body><div id="__nuxt">${APPBAR}
<div id="app-content"><div id="page-wrapper" class="bg-bg page"><div id="item-page-wrapper"><div class="flex"><div class="grow"><div class="flex justify-center"><div class="mb-4">
<h1 class="text-2xl md:text-3xl font-semibold"><div class="flex items-center">
                Loopbreaker
                <span class="material-symbols">explicit</span></div></h1>
<p class="text-gray-200 text-xl">A Time Loop LitRPG</p>
<a href="/audiobookshelf/library/L1/series/939c1909" class="hover:underline">Loopbreaker #1</a><span>, </span><a href="/audiobookshelf/library/L1/series/abc" class="hover:underline">Time Loops #3</a>
</div></div><p class="mb-2">by <a href="/audiobookshelf/author/x">Shawn Wilson</a></p></div></div></div></div></div></div></body></html>`, "abs-item");
pg = sandbox.rrAbsPage(item, "/audiobookshelf/item/121c1d82");
chk("item page: the item's own series links, in order, '#N' stripped",
    pg && pg.kind === "item" && pg.names.join("|") === "Loopbreaker|Time Loops", JSON.stringify(pg && pg.names));
chk("item page: chip goes inline after the last series link", pg.anchor.tagName === "A" && pg.anchor.textContent === "Time Loops #3" && pg.inline === true);
chk("item page: the search dropdown's Dual Class link is ignored", pg.names.indexOf("Dual Class") < 0);
const noLinks = docFor(`<html><body><div id="__nuxt">${APPBAR}<div id="app-content"><div id="page-wrapper"><div id="item-page-wrapper"><div class="mb-4"><h1><div>Standalone</div></h1></div></div></div></div></div></body></html>`, "abs-item-nolinks");
pg = sandbox.rrAbsPage(noLinks, "/audiobookshelf/item/zzz");
chk("item without series links: no names, chip would sit under the title", pg && pg.names.length === 0 && pg.anchor.tagName === "H1" && pg.inline === false);

// --- the words: ONE plain fact ABS doesn't have, no emoji, or nothing at all
const T = "2026-10-11";
const txt = (v) => { const f = sandbox.rrAbsChipText(v, T); return f && f.icon + "|" + f.text; };
const loop = { name: "Loopbreaker", predicted_date: "2026-10-01", predicted_book_n: 2, next_date: "2026-10-01", next_kind: "audiobook", next_estimate: true, status_label: "No audiobook yet", listening: { n: 1, pct: 82 } };
chk("a past estimate is OVERDUE, by how much - not a date in the past, and not ABS's own listening progress", txt(loop) === "schedule|Audio 2 overdue by 10 days", txt(loop));
chk("one day, singular", txt({ predicted_date: "2026-10-10", predicted_book_n: 2 }) === "schedule|Audio 2 overdue by 1 day", txt({ predicted_date: "2026-10-10", predicted_book_n: 2 }));
const ahead = { predicted_date: "2027-02-13", predicted_book_n: 6, next_date: "2027-02-13", next_kind: "audiobook", next_estimate: true };
chk("an estimate still ahead reads as an estimate, with the year when it differs", txt(ahead) === "schedule|Audio 6 est. 13 Feb 2027", txt(ahead));
const dated = { predicted_date: "2026-11-03", predicted_book_n: 3, next_date: "2026-11-03", next_kind: "audiobook", next_estimate: false };
chk("a confirmed date beats the estimate", txt(dated) === "event|Audiobook 3 Nov", txt(dated));
const both = { predicted_date: "2026-10-01", predicted_book_n: 2, next_est_date: "2027-01-20", next_est_book_n: 3 };
chk("overdue outranks the next projection: one fact only", txt(both) === "schedule|Audio 2 overdue by 10 days", txt(both));
chk("nothing to add: no chip (null), never a status label", sandbox.rrAbsChipText({ status_label: "Audio pre-order", listening: { n: 1, pct: 50 } }, T) === null);
chk("a novel next, not an audiobook", txt({ next_date: "2026-12-01", next_kind: "novel", next_estimate: false }) === "event|Novel 1 Dec");
chk("an audiobook player wants the audio projection before a novel date",
    txt({ next_date: "2026-10-14", next_kind: "novel", next_estimate: false, predicted_date: "2027-02-13", predicted_book_n: 8 }) === "schedule|Audio 8 est. 13 Feb 2027");
for (const v of [loop, ahead, dated, both]) chk("no emoji anywhere", !/[\u{1F000}-\u{1FFFF}☀-➿]/u.test(txt(v)), txt(v));

if (fails) { console.log(`\n${fails} failure(s)`); process.exit(1); }
console.log("test_abs: all good");
process.exitCode = 0;
