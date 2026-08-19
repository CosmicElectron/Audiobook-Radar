// Run the REAL extract.js over REAL saved Amazon pages.
//
//   node release-radar-firefox/test/test_fixtures.js
//
// The fixtures in ./fixtures are trimmed copies of live amazon.com.au product
// pages (Aug 2026): released and pre-order, Kindle and audiobook, plus a robot
// check and a sign-in wall. They exist because the detector was fooled by
// three shapes no synthetic test anticipated: audiobook buy boxes with auto-id
// buttons, an audiobook PRE-ORDER that shows "Buy now with 1-Click", and a
// hidden "We will pre-order your items" alert on RELEASED Kindle pages.
//
// Parsing: html5lib (Python) builds a spec-compliant tree, dumped to JSON, and
// minidom.js gives it querySelector/TreeWalker — the same tree DOMParser builds
// in the extension. Requires python + html5lib on PATH; skipped (exit 0) if not.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");
const { makeDoc } = require("./minidom");

const HERE = __dirname;
const FIX = path.join(HERE, "fixtures");
const EXTRACT = path.join(HERE, "..", "extract.js");
const CACHE = path.join(HERE, ".fixture-cache");

// What each real page must yield. `pre` = _preorder, `a` = _isAudio.
const EXPECT = {
  "B0G15BWPZZ-primal-hunter-15-kindle-released": { pre: false, s: "The Primal Hunter", n: 15, d: "2026-03-25", a: false, au: "Zogarth", nar: null },
  "B0GSX6H1XF-lone-wanderer-5-kindle-released":  { pre: false, s: "The Lone Wanderer", n: 5,  d: "2026-08-12", a: false, au: "K. Georgiades aka PathOfPen", nar: null },
  "B0H8ZN7RB8-primal-hunter-15-audio-released":  { pre: false, s: "The Primal Hunter", n: 15, d: "2026-08-19", a: true,  au: "Zogarth", nar: "Travis Baldree" },
  "B0H9TWNX7M-lone-wanderer-5-audio-released":   { pre: false, s: "The Lone Wanderer", n: 5,  d: "2026-08-12", a: true,  au: "K. Georgiades aka PathOfPen", nar: "Austin Rising" },
  "B0H9X7KR4P-primal-hunter-17-kindle-preorder": { pre: true,  s: "The Primal Hunter", n: 17, d: "2026-11-18", a: false, au: "Zogarth", nar: null },
  "B0HCDT7J69-skill-hunter-2-audio-preorder":    { pre: true,  s: "Skill Hunter",      n: 2,  d: "2026-08-25", a: true,  au: "Noct",    nar: "Pavi Proczko" },
  // non-product pages: must be unusable (RR_valid false) and never guessed
  "robot-page":  { invalid: true, pre: null },
  "signin-page": { invalid: true, pre: null },
};

function havePython() {
  try { execFileSync("python", ["-c", "import html5lib"], { stdio: "ignore" }); return true; } catch (e) { return false; }
}
function jsonFor(htmlPath) {
  fs.mkdirSync(CACHE, { recursive: true });
  const out = path.join(CACHE, path.basename(htmlPath, ".html") + ".json");
  const stale = !fs.existsSync(out) || fs.statSync(out).mtimeMs < fs.statSync(htmlPath).mtimeMs;
  if (stale) execFileSync("python", [path.join(HERE, "html2json.py"), htmlPath, out], { stdio: "inherit" });
  return JSON.parse(fs.readFileSync(out, "utf8"));
}

if (!havePython()) {
  console.log("test_fixtures: python+html5lib not available — skipping real-page fixtures");
  process.exit(0);
}

const sandbox = { console, URL, self: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(EXTRACT, "utf8"), sandbox);

let fails = 0;
function chk(name, field, got, want) {
  if (got === want) return;
  fails++;
  console.log(`  FAIL ${name} :: ${field}  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

console.log("extract.js against real saved Amazon pages:");
for (const f of fs.readdirSync(FIX).filter(x => x.endsWith(".html")).sort()) {
  const name = f.replace(/\.html$/, "");
  const e = EXPECT[name];
  if (!e) { console.log(`  skip ${name} (no expectation)`); continue; }
  const doc = makeDoc(jsonFor(path.join(FIX, f)));
  const asin = (name.match(/^(B0[A-Z0-9]{8})/) || [])[1] || "B0TESTTEST";
  const r = sandbox.RR_extract(doc, "https://www.amazon.com.au/dp/" + asin);
  const valid = sandbox.RR_valid(r);
  if (e.invalid) {
    chk(name, "RR_valid", valid, false);
    chk(name, "_preorder", r._preorder, null);
  } else {
    chk(name, "_preorder", r._preorder, e.pre);
    chk(name, "series", r.series, e.s);
    chk(name, "book.n", r.book.n, e.n);
    chk(name, "_date", r._date, e.d);
    chk(name, "_isAudio", r._isAudio, e.a);
    chk(name, "author", r.author, e.au);
    chk(name, "narrator", r.book.narrator, e.nar);
    chk(name, "RR_valid", valid, true);
    chk(name, "_asin", r._asin, asin);
  }
  console.log(`  ${name.padEnd(46)} pre=${String(r._preorder).padEnd(5)} valid=${valid}`);
}
console.log(fails ? `\n${fails} FAILURE(S)` : "\nall real-page fixtures correct");
process.exit(fails ? 1 : 0);
