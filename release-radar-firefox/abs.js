// Release Radar — Audiobookshelf overlay (content script).
//
// Runs on localhost pages, quietly does nothing unless the page is actually
// Audiobookshelf. On an ABS SERIES page or ITEM page it injects one small
// chip under the title with what the radar knows about that series: the next
// book, its date (or estimate), audio status, overdue state — linking back to
// the radar dashboard. Read-only; all data comes from the local radar API.
//
// ABS is a single-page app: routes change without page loads, so a poll
// watches the URL and re-applies. Selector notes (ABS web UI, 2026): page
// title is the first <h1> in the main content; item pages link their series
// as <a href=".../library/<id>/series/<seriesId>">Name #N</a>.
"use strict";

var RR_APP = "http://127.0.0.1:8765";
var RR_CHIP_ID = "rr-abs-chip";
var _rrAbsState = null;          // {at, byName: {norm(series): view}}
var _rrAbsLastHref = "";
var _rrAbsIsAbs = null;

function rrNorm(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/^(the|a|an) /, "").trim(); }

function rrLooksLikeAbs() {
  if (_rrAbsIsAbs !== null) return _rrAbsIsAbs;
  // ABS ships a distinctive root: <div id="__nuxt">, plus its own metadata
  var nuxt = document.querySelector("#__nuxt, #__layout");
  var title = (document.title || "").toLowerCase();
  var abs = !!nuxt && (title.includes("audiobookshelf") || !!document.querySelector('meta[name="apple-mobile-web-app-title"][content*="udiobookshelf" i]') ||
                       !!document.querySelector('link[rel="manifest"]'));
  // never run on the radar's own dashboard
  if (location.port === "8765") abs = false;
  _rrAbsIsAbs = abs;
  return abs;
}

async function rrAbsRadarState() {
  var now = Date.now();
  if (_rrAbsState && now - _rrAbsState.at < 60000) return _rrAbsState;
  try {
    var r = await fetch(RR_APP + "/api/state");
    var d = await r.json();
    var by = {};
    (d.series || []).forEach(function (s) { by[rrNorm(s.name)] = s; });
    _rrAbsState = { at: now, byName: by };
  } catch (e) { _rrAbsState = { at: now, byName: {} }; }
  return _rrAbsState;
}

function rrAbsSeriesNameFromPage() {
  var path = location.pathname;
  var h1 = document.querySelector("h1");
  if (/\/series\//.test(path) && h1) return (h1.textContent || "").trim();
  if (/\/item\//.test(path)) {
    var a = document.querySelector('a[href*="/series/"]');
    if (a) return (a.textContent || "").replace(/#\S+\s*$/, "").trim();
  }
  return null;
}

function rrAbsChipHtml(s) {
  var bits = [];
  var nx = s.next_date ? ((s.next_kind === "audiobook" ? "🎧 " : "📖 ") + "next " + (s.next_kind || "") + ": " + s.next_date) : null;
  if (nx) bits.push(nx);
  if (s.predicted_date) bits.push("~audio est " + s.predicted_date + (s.predicted_book_n != null ? " (#" + s.predicted_book_n + ")" : ""));
  if (s.listening) bits.push("🎧 you're on #" + s.listening.n + " (" + Math.round(s.listening.pct) + "%)");
  if (!bits.length) bits.push(s.status_label || "tracked");
  return bits.join(" · ");
}

async function rrAbsApply() {
  if (!rrLooksLikeAbs()) return;
  var name = rrAbsSeriesNameFromPage();
  var old = document.getElementById(RR_CHIP_ID);
  if (!name) { if (old) old.remove(); return; }
  var st = await rrAbsRadarState();
  var s = st.byName[rrNorm(name)];
  if (!s) { if (old) old.remove(); return; }
  var h1 = document.querySelector("h1");
  if (!h1) return;
  var chip = old || document.createElement("a");
  if (!old) {
    chip.id = RR_CHIP_ID;
    chip.href = RR_APP + "/";
    chip.target = "_blank";
    chip.rel = "noopener";
    chip.style.cssText = "display:inline-block;margin:6px 0 0;padding:3px 10px;border-radius:999px;" +
      "font-size:12px;line-height:1.5;text-decoration:none;background:#2d2544;color:#cfc4f4;" +
      "border:1px solid #4a4067;vertical-align:middle;";
    chip.title = "Release Radar — open the dashboard";
    if (h1.parentElement) h1.parentElement.insertBefore(chip, h1.nextSibling);
  }
  chip.textContent = "📡 " + rrAbsChipHtml(s);
}

setInterval(function () {
  if (location.href !== _rrAbsLastHref) { _rrAbsLastHref = location.href; setTimeout(rrAbsApply, 800); }
}, 700);
setTimeout(rrAbsApply, 1500);
setInterval(rrAbsApply, 20000);      // data refresh while sitting on a page
