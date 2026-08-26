// buy.js probe against the REAL saved Amazon pages (logged-out captures) and a
// few synthetic shapes for the signed-in markers. Clicks nothing: RR_buyProbe
// is a pure reader.
//
//   node release-radar-firefox/test/test_buy.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");
const { makeDoc } = require("./minidom");

const HERE = __dirname;
const FIX = path.join(HERE, "fixtures");
const CACHE = path.join(HERE, ".fixture-cache");

function havePython() { try { execFileSync("python", ["-c", "import html5lib"], { stdio: "ignore" }); return true; } catch (e) { return false; } }
function jsonFor(htmlPath) {
  fs.mkdirSync(CACHE, { recursive: true });
  const out = path.join(CACHE, path.basename(htmlPath, ".html") + ".json");
  const stale = !fs.existsSync(out) || fs.statSync(out).mtimeMs < fs.statSync(htmlPath).mtimeMs;
  if (stale) execFileSync("python", [path.join(HERE, "html2json.py"), htmlPath, out], { stdio: "inherit" });
  return JSON.parse(fs.readFileSync(out, "utf8"));
}
function jsonForHtml(html, key) {
  fs.mkdirSync(CACHE, { recursive: true });
  const src = path.join(CACHE, key + ".html"), out = path.join(CACHE, key + ".json");
  fs.writeFileSync(src, html);
  execFileSync("python", [path.join(HERE, "html2json.py"), src, out], { stdio: "inherit" });
  return JSON.parse(fs.readFileSync(out, "utf8"));
}
if (!havePython()) { console.log("test_buy: python+html5lib not available — skipping"); process.exit(0); }

const sandbox = { console, URL, self: {} };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(HERE, "..", "buy.js"), "utf8"), sandbox);

let fails = 0;
function chk(name, field, got, want) {
  if (got === want) return;
  fails++;
  console.log(`  FAIL ${name} :: ${field}  got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

// What the real (logged-out) pages must yield. price = companion price if shown.
const EXPECT = {
  "B0GSX6H1XF-lone-wanderer-5-kindle-released":  { page: "kindle", signed: false, ku: true, price: 7.47, audio: "B0H9TWNX7M", kindle: "B0GSX6H1XF" },
  "B0G15BWPZZ-primal-hunter-15-kindle-released": { page: "kindle", signed: false, ku: true, price: null, kindle: "B0G15BWPZZ" },
  "B0H9X7KR4P-primal-hunter-17-kindle-preorder": { page: "kindle", signed: false, price: null, kindle: "B0H9X7KR4P" },
  "B0H9TWNX7M-lone-wanderer-5-audio-released":   { page: "audio",  signed: false, price: 7.47, audio: "B0H9TWNX7M", kindle: "B0GSX6H1XF" },
  "B0H8ZN7RB8-primal-hunter-15-audio-released":  { page: "audio",  signed: false, price: 7.47, audio: "B0H8ZN7RB8", kindle: "B0G15BWPZZ" },
  "B0HCDT7J69-skill-hunter-2-audio-preorder":    { page: "audio",  signed: false, audio: "B0HCDT7J69" },
  "robot-page":  { robot: true },
  "signin-page": { signed: false },
};

console.log("buy.js probe against real saved Amazon pages:");
for (const f of fs.readdirSync(FIX).filter(x => x.endsWith(".html")).sort()) {
  const name = f.replace(/\.html$/, "");
  const e = EXPECT[name]; if (!e) continue;
  const doc = makeDoc(jsonFor(path.join(FIX, f)));
  const asin = (name.match(/^(B0[A-Z0-9]{8})/) || [])[1] || "B0TESTTEST";
  const r = sandbox.RR_buyProbe(doc, "https://www.amazon.com.au/dp/" + asin);
  if ("page" in e) chk(name, "page", r.page, e.page);
  if ("signed" in e) chk(name, "signed_in", r.signed_in, e.signed);
  if ("robot" in e) chk(name, "robot", r.robot, e.robot);
  if ("ku" in e) chk(name, "ku", r.ku, e.ku);
  if ("price" in e) chk(name, "price", r.price, e.price);
  if ("audio" in e) chk(name, "audio_asin", r.audio_asin, e.audio);
  if ("kindle" in e) chk(name, "kindle_asin", r.kindle_asin, e.kindle);
  chk(name, "owned_audio(logged out)", r.owned_audio, null);
  console.log(`  ${name.padEnd(46)} page=${String(r.page).padEnd(7)} ku=${String(r.ku).padEnd(5)} price=${String(r.price).padEnd(5)} btns=${r.buttons.length} ${r.buttons.slice(0, 3).map(b => b.label).join(" | ")}`);
}

// Synthetic signed-in shapes (the logged-out captures can't show these)
const signedIn = `<html><head><title>Book: Kindle edition</title></head><body>
<span id="nav-link-accountList-nav-line-1">Hello, Reader</span>
<div id="tmmSwatches"><div id="tmm-grid-swatch-KINDLE" class="selected"><a href="/dp/B0KINDLE01/ref=tmm_kin_swatch_0">Kindle <span class="a-color-price">$0.00</span><i class="a-icon a-icon-kindle-unlimited"></i></a></div>
<div id="tmm-grid-swatch-AUDIO_DOWNLOAD"><a href="/dp/B0AUDIO001/ref=tmm_aud_swatch_0">Audiobook $0.00</a></div></div>
<div id="buybox"><form id="buyOneClick"><input type="hidden" id="audibleAsin" value="B0AUDIO001">
<span id="borrowButton" class="a-button"><input type="submit" class="a-button-input" value="Read for Free"></span>
<div id="audibleNarration"><label><input id="narration-checkbox" type="checkbox"><span>Add audiobook for <span class="a-color-price">$7.47</span> <span class="a-color-price">$39.76</span></span></label></div>
<div class="aok-hidden"><span class="a-color-price">$99.99</span>We will pre-order your items</div>
<p>You borrowed this title on 19 August 2026.</p></form></div></body></html>`;
const r1 = sandbox.RR_buyProbe(makeDoc(jsonForHtml(signedIn, "synthetic-signed-in")), "https://www.amazon.com.au/dp/B0KINDLE01");
chk("signed-in", "signed_in", r1.signed_in, true);
chk("signed-in", "page", r1.page, "kindle");
chk("signed-in", "ku", r1.ku, true);
chk("signed-in", "price", r1.price, 7.47);
chk("signed-in", "audio_asin", r1.audio_asin, "B0AUDIO001");
chk("signed-in", "borrowed", r1.borrowed, true);
chk("signed-in", "owned_audio", r1.owned_audio, null);
chk("signed-in", "borrow button seen", r1.buttons.some(b => /read for free/i.test(b.label) && b.id === "borrowButton"), true);
console.log(`  synthetic signed-in kindle                     price=${r1.price} borrowed=${r1.borrowed} btns=${r1.buttons.map(b => b.label).join(" | ")}`);

const owned = `<html><head><title>Audiobook</title></head><body><span id="nav-link-accountList-nav-line-1">Hello, Reader</span>
<div id="buyBoxAccordion"><div class="a-alert">You already own this title. <a>Listen now</a></div></div></body></html>`;
const r2 = sandbox.RR_buyProbe(makeDoc(jsonForHtml(owned, "synthetic-owned")), "https://www.amazon.com.au/dp/B0AUDIO001");
chk("owned", "owned_audio", r2.owned_audio, true);
chk("owned", "page", r2.page, "audio");

// the page-step function must be serialisable (it runs inside the tab)
const src = sandbox.RR_pageStep.toString();
chk("pageStep", "self-contained (no _rr helpers)", /_rr[A-Z]/.test(src), false);
chk("pageStep", "starts as a function", /^function RR_pageStep\(/.test(src), true);

console.log(fails ? `\n${fails} FAILURE(S)` : "\nall buy-probe checks correct");
process.exit(fails ? 1 : 0);
