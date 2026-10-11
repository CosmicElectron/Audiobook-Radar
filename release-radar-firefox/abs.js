// Release Radar — Audiobookshelf overlay (content script).
//
// Runs on localhost pages, quietly does nothing unless the page is actually
// Audiobookshelf. On an ABS SERIES page (/library/<id>/series/<id>) or ITEM
// page (/item/<id>) it puts ONE small chip beside the series name with what
// the radar knows that ABS doesn't: an overdue audiobook (and by how much),
// the next dated release, an estimate still ahead, where you are in the
// series — linking back to the dashboard. Read-only; all data comes from the
// local radar API.
//
// ABS is a single-page app (Nuxt): routes change without page loads, so a
// poll watches the URL and re-applies. Markup (ABS client 2.37, verified on
// the user's server 11 Oct 2026): the top app bar carries <h1>audiobookshelf</h1>
// inside #appbar, FIRST in the document, so document.querySelector("h1") is
// never the page title — that is where the chip used to land, twice. The
// series page has no heading at all: the name is the first <p> of #toolbar.
// The item page's title is the h1 inside #item-page-wrapper, and its series
// links are direct children of that h1's parent, text "Name #N"; the app
// bar's search dropdown can hold other /series/ links earlier in the
// document, so the first one on the page is never taken.
"use strict";

var RR_APP = "http://127.0.0.1:8765";
var RR_CHIP_ID = "rr-abs-chip";
var _rrAbsState = null;          // {at, byName: {norm(series): view}}
var _rrAbsLastHref = "";
var _rrAbsIsAbs = null;
var _rrAbsBusy = false;          // one apply at a time: two in flight made two chips

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

// ---- reading the page (pure: a document and a path in, a description out)
function rrInside(el, ids) {
  for (var p = el; p; p = p.parentElement) if (p.id && ids.indexOf(p.id) >= 0) return true;
  return false;
}

function rrChildSeriesLinks(block) {
  var out = [];
  var kids = block.children || [];
  for (var i = 0; i < kids.length; i++) {
    var k = kids[i];
    if (k.tagName === "A" && /\/series\//.test(k.getAttribute("href") || "")) out.push(k);
  }
  return out;
}

function rrAbsPage(doc, path) {
  // -> {kind, names: [series names, best first], anchor, inline} or null
  if (/\/library\/[^/]+\/series\/[^/?#]+/.test(path)) {
    var p = doc.querySelector("#toolbar > p") || doc.querySelector("#toolbar p");
    var name = p ? (p.textContent || "").trim() : "";
    if (!name || /^\d+$/.test(name) || rrNorm(name) === "audiobookshelf") return null;
    return { kind: "series", names: [name], anchor: p, inline: true };
  }
  if (/\/item\/[^/?#]+/.test(path)) {
    var h1 = doc.querySelector("#item-page-wrapper h1") || doc.querySelector("#page-wrapper h1");
    if (!h1) {
      var all = doc.querySelectorAll("#app-content h1");
      for (var i = 0; i < all.length; i++) {
        if (!rrInside(all[i], ["appbar", "reader"]) && rrNorm(all[i].textContent) !== "audiobookshelf") { h1 = all[i]; break; }
      }
    }
    if (!h1) return null;
    var links = h1.parentElement ? rrChildSeriesLinks(h1.parentElement) : [];
    if (!links.length) {
      var scoped = doc.querySelector("#item-page-wrapper");
      links = scoped ? Array.prototype.slice.call(scoped.querySelectorAll('a[href*="/series/"]')) : [];
    }
    var names = links.map(function (a) { return (a.textContent || "").replace(/\s*#[^#]*$/, "").trim(); }).filter(Boolean);
    return { kind: "item", names: names, anchor: links.length ? links[links.length - 1] : h1, inline: !!links.length };
  }
  return null;
}

// ---- the words
var RR_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function rrToday() {
  var d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

function rrShort(iso, todayIso) {
  var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return iso || "";
  var s = String(parseInt(m[3], 10)) + " " + RR_MON[parseInt(m[2], 10) - 1];
  return m[1] === (todayIso || "").slice(0, 4) ? s : s + " " + m[1];
}

function rrDays(fromIso, toIso) {
  var a = /^(\d{4})-(\d{2})-(\d{2})/.exec(fromIso), b = /^(\d{4})-(\d{2})-(\d{2})/.exec(toIso);
  if (!a || !b) return 0;
  return Math.round((Date.UTC(+b[1], +b[2] - 1, +b[3]) - Date.UTC(+a[1], +a[2] - 1, +a[3])) / 864e5);
}

// One plain fact ABS doesn't have, or nothing. No emoji (the user, 11 Oct 2026:
// "cluttered, not visually appealing"); the icon is ABS's own Material
// Symbols font. Listening progress is ABS's own data, so it is not repeated.
function rrAbsChipText(s, todayIso) {
  var today = todayIso || rrToday();
  var n = s.predicted_book_n != null ? " " + s.predicted_book_n : "";
  var est = s.predicted_date || null;
  if (est && est < today) {
    var late = rrDays(est, today);
    return { icon: "schedule", text: "Audio" + n + " overdue by " + late + (late === 1 ? " day" : " days") };
  }
  var dated = (s.next_date && !s.next_estimate && s.next_date >= today) ? s.next_date : null;
  // audio facts first (this is an audiobook player); a novel date only when
  // the radar has no audio projection at all
  if (dated && s.next_kind === "audiobook") return { icon: "event", text: "Audiobook " + rrShort(dated, today) };
  if (est) return { icon: "schedule", text: "Audio" + n + " est. " + rrShort(est, today) };
  if (s.next_est_date && s.next_est_date >= today) {
    return { icon: "schedule", text: "Audio" + (s.next_est_book_n != null ? " " + s.next_est_book_n : "") + " est. " + rrShort(s.next_est_date, today) };
  }
  if (dated) return { icon: "event", text: (s.next_kind === "novel" ? "Novel " : "Next ") + rrShort(dated, today) };
  return null;
}

// ---- putting it on the page
function rrAbsRemoveChips() {
  var chips = document.querySelectorAll("#" + RR_CHIP_ID);
  for (var i = 0; i < chips.length; i++) chips[i].remove();
}

async function rrAbsApply() {
  if (!rrLooksLikeAbs() || _rrAbsBusy) return;
  _rrAbsBusy = true;
  try {
    var page = rrAbsPage(document, location.pathname);
    if (!page) { rrAbsRemoveChips(); return; }
    var st = await rrAbsRadarState();
    var s = null;
    for (var i = 0; i < page.names.length && !s; i++) s = st.byName[rrNorm(page.names[i])] || null;
    if (!s) { rrAbsRemoveChips(); return; }
    var fact = rrAbsChipText(s);
    if (!fact) { rrAbsRemoveChips(); return; }                      // nothing to add: no chip at all
    page = rrAbsPage(document, location.pathname) || page;        // the SPA may have re-rendered meanwhile
    if (!page.anchor || !page.anchor.parentElement) return;
    var chips = document.querySelectorAll("#" + RR_CHIP_ID);
    for (var k = 1; k < chips.length; k++) chips[k].remove();       // never two
    var chip = chips[0] || document.createElement("a");
    if (!chips[0]) {
      chip.id = RR_CHIP_ID;
      chip.href = RR_APP + "/";
      chip.target = "_blank";
      chip.rel = "noopener";
      // ABS's own look: its grey surfaces, its text colour, its icon font
      chip.style.cssText = "display:inline-flex;align-items:center;gap:5px;padding:1px 9px 1px 7px;border-radius:6px;" +
        "font-size:12px;line-height:1.6;text-decoration:none;color:#e5e7eb;background:rgba(255,255,255,0.08);" +
        "border:1px solid rgba(255,255,255,0.14);vertical-align:middle;white-space:nowrap;";
      chip.title = "Release Radar";
    }
    // beside the series name — and back beside it if ABS re-rendered around it
    if (chip.previousElementSibling !== page.anchor) page.anchor.parentElement.insertBefore(chip, page.anchor.nextSibling);
    chip.style.margin = page.inline ? "0 0 0 10px" : "6px 0 0";
    while (chip.firstChild) chip.removeChild(chip.firstChild);
    if (document.querySelector(".material-symbols")) {              // the icon font is on the page: use it
      var ic = document.createElement("span");
      ic.className = "material-symbols";
      ic.style.cssText = "font-size:15px;line-height:1;opacity:0.85;";
      ic.textContent = fact.icon;
      chip.appendChild(ic);
    }
    chip.appendChild(document.createTextNode(fact.text));
  } finally {
    _rrAbsBusy = false;
  }
}

setInterval(function () {
  if (location.href !== _rrAbsLastHref) { _rrAbsLastHref = location.href; setTimeout(rrAbsApply, 800); }
}, 700);
setTimeout(rrAbsApply, 1500);
setInterval(rrAbsApply, 20000);      // data refresh while sitting on a page
