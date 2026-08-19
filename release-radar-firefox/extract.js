// Shared by the content script (live page) and the background poller (fetched
// HTML parsed with DOMParser). Takes a Document + the page URL, returns the
// payload Release Radar expects, or null if this isn't a usable book page.

function RR_extract(doc, url) {
  url = url || "";
  var _host = ""; try { _host = new URL(url).hostname; } catch (e0) {}
  var _dayFirst = !/^www\.amazon\.com$/i.test(_host);   // every storefront but the US one writes d/m/y
  var MONTHS = {
    january: 1, february: 2, march: 3, april: 4, may: 5, june: 6, july: 7,
    august: 8, september: 9, october: 10, november: 11, december: 12,
    jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
    oct: 10, nov: 11, dec: 12
  };

  function clean(s) {
    return (s || "").replace(/[\u200e\u200f\u202a-\u202e]/g, "").replace(/\s+/g, " ").trim();
  }
  function pad(n) { n = String(n); return n.length < 2 ? "0" + n : n; }

  function parseDate(s) {
    s = clean(s);
    if (!s) return null;
    var m = s.match(/(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})/);          // 18 March 2026
    if (m && MONTHS[m[2].toLowerCase()]) return valid(m[3], MONTHS[m[2].toLowerCase()], m[1]);
    m = s.match(/([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})/);            // March 18, 2026
    if (m && MONTHS[m[1].toLowerCase()]) return valid(m[3], MONTHS[m[1].toLowerCase()], m[2]);
    m = s.match(/(\d{4})-(\d{2})-(\d{2})/);                          // already ISO
    if (m) return valid(m[1], m[2], m[3]);
    m = s.match(/^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{4})$/);       // 12/08/2026: order depends on storefront
    if (m) return _dayFirst ? valid(m[3], m[2], m[1]) : valid(m[3], m[1], m[2]);
    m = s.match(/^([A-Za-z]+)\.?\s+(\d{4})$/);                        // "August 2026": keep the month, day 1
    if (m && MONTHS[m[1].toLowerCase()]) return valid(m[2], MONTHS[m[1].toLowerCase()], 1);
    // NO bare new Date() fallback: it reads AU day-first numerics as m/d/y and
    // silently invents days. An unknown shape is null; the caller decides.
    return null;
  }
  function valid(y, mo, d) {   // reject impossible days (31 Feb) instead of emitting them
    y = +y; mo = +mo; d = +d;
    if (!(y > 1900 && mo >= 1 && mo <= 12 && d >= 1)) return null;
    if (d > new Date(y, mo, 0).getDate()) return null;
    return y + "-" + pad(mo) + "-" + pad(d);
  }

  function rpiMap() {
    var map = {};
    doc.querySelectorAll(".rpi-attribute-content[data-rpi-attribute-name]").forEach(function (c) {
      var name = c.getAttribute("data-rpi-attribute-name");
      var lab = c.querySelector(".rpi-attribute-label");
      var val = c.querySelector(".rpi-attribute-value");
      map[name] = { label: clean(lab && lab.textContent), value: clean(val && val.textContent) };
    });
    return map;
  }
  function detailBullets() {
    var out = {};
    doc.querySelectorAll("#detailBullets_feature_div li, #productDetailsTable li").forEach(function (li) {
      var t = clean(li.textContent);
      var idx = t.indexOf(":");
      if (idx > 0) out[t.slice(0, idx).trim().toLowerCase()] = t.slice(idx + 1).trim();
    });
    // audiobook pages put the same facts in a th/td table instead of bullets
    doc.querySelectorAll("#audibleProductDetails tr, #audibleproductdetails_feature_div tr").forEach(function (tr) {
      var th = tr.querySelector("th"), td = tr.querySelector("td");
      if (th && td) { var k = clean(th.textContent).replace(/:$/, "").toLowerCase(); if (k) out[k] = clean(td.textContent); }
    });
    return out;
  }
  function titleText() {
    var el = doc.querySelector("#productTitle") || doc.querySelector("#title");
    return clean(el ? el.textContent : (doc.title || ""));
  }
  function coverUrl() {
    var sels = ["#landingImage", "#imgBlkFront", "#ebooksImgBlkFront", "#main-image",
                "#audibleProductImage img", "#imageBlockContainer img", "#imgTagWrapperId img"];
    for (var i = 0; i < sels.length; i++) {
      var el = doc.querySelector(sels[i]);
      if (!el) continue;
      var dyn = el.getAttribute("data-a-dynamic-image");
      if (dyn) { try { var m = JSON.parse(dyn), best = null, area = -1; for (var u in m) { var a = (m[u][0] || 0) * (m[u][1] || 0); if (a > area) { area = a; best = u; } } if (best) return best; } catch (e) {} }
      var hi = el.getAttribute("data-old-hires"); if (hi && /^https?:/.test(hi)) return hi;
      var src = el.getAttribute("src"); if (src && /^https?:/.test(src)) return src;
    }
    return null;
  }

  var host = "", path = "";
  try { var u = new URL(url); host = u.hostname; path = u.pathname; } catch (e) {}

  var m = rpiMap();
  var db = detailBullets();
  var title = titleText();

  // series name + book number — primary source is the "Book N of M" card
  var series = null, n = null, total = null;
  if (m["book_details-series"]) {
    var mm = (m["book_details-series"].label || "").match(/Book\s+(\d+)\s+of\s+(\d+)/i);
    if (mm) { n = parseInt(mm[1], 10); total = parseInt(mm[2], 10); }
    series = m["book_details-series"].value || null;
  }
  if (n == null || !series) {
    // "Book N of M : Series" also appears as a detail bullet / table row
    for (var dk in db) {
      var bm0 = dk.match(/^book\s+(\d+)(?:\s+of\s+(\d+))?$/);
      if (bm0) {
        if (n == null) n = parseInt(bm0[1], 10);
        if (total == null && bm0[2]) total = parseInt(bm0[2], 10);
        if (!series && db[dk]) series = clean(db[dk]);
        break;
      }
    }
  }
  if (n == null) {
    var t1 = title.match(/,\s*Book\s+(\d+)\s*\)/i) || title.match(/\bBook\s+(\d+)\b/i) || title.match(/^[^\d]*?(\d{1,3})\s*[:\-]/);
    if (t1) n = parseInt(t1[1], 10);
  }
  if (!series) {
    var t2 = title.match(/\(([A-Za-z0-9'\u2019 ]+?),\s*Book\s+\d+\)/i) || title.match(/^([A-Za-z0-9'\u2019 ]+?)\s+\d+\s*[:]/);
    if (t2) series = clean(t2[1]);
  }

  var isAudio = !!(m["audiobook_details-program_type"] || m["audiobook_details-listening_length"] ||
                   m["audiobook_details-narrator"]) ||
                !!doc.querySelector("#audibleProductDetails, #audibleproductdetails_feature_div") ||
                /Format:\s*Audible Audiobook/i.test(clean((doc.querySelector("#bylineInfo") || {}).textContent || "")) ||
                /\(Audio Download\)/i.test(doc.title || "") ||
                /audio_download|\/audible|audiobook/i.test(url) || /audible\./i.test(host);

  var dateStr = (m["book_details-publication_date"] && m["book_details-publication_date"].value) ||
                db["publication date"] || db["release date"] || db["audible.com release date"] ||
                db["audible.com.au release date"] || db["audible.co.uk release date"] || db["audible release date"] || null;
  var dt = parseDate(dateStr);

  var author = (m["audiobook_details-author"] && m["audiobook_details-author"].value) || null;
  if (!author) {
    var by = doc.querySelector("#bylineInfo");
    if (by) {
      var bm = clean(by.textContent).match(/^\s*([^()]+?)\s*\(Author\)/);
      author = (bm ? clean(bm[1]) : clean((by.textContent.split("(")[0] || ""))).replace(/^by\s+/i, "") || null;
    }
  }

  // narrator (audiobook pages only) — the app shows it on the card and pings
  // when a series changes voice
  var narrator = (m["audiobook_details-narrator"] && m["audiobook_details-narrator"].value) || null;
  if (!narrator) {
    var bl = clean((doc.querySelector("#bylineInfo") || {}).textContent || "");
    // "(Author, Narrator)", "(narrator)", two narrators: collect every name whose
    // role list mentions narrator
    var names = [], rx = /([^(),]+?)\s*\(([^)]*)\)/g, mm2;
    while ((mm2 = rx.exec(bl))) { if (/narrator/i.test(mm2[2])) { var nn = clean(mm2[1]); if (nn && names.indexOf(nn) < 0) names.push(nn); } }
    if (names.length) narrator = names.join(", ");
  }
  // the byline runs names together with page controls ("…, see all")
  if (narrator) {
    narrator = narrator.replace(/\(\s*narrator[^)]*\)/ig, "");
    for (var np = null; np !== narrator; ) {
      np = narrator;
      narrator = narrator.replace(/\s*(?:,\s*)?(?:see all|see more|and others?|et al\.?|,)\s*$/i, "").trim();
    }
    narrator = narrator || null;
  }

  var asin = (m["book_details-asin"] && m["book_details-asin"].value) ||
             db["asin"] ||
             (path.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1] ||
             (url.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1] || null;
  var canonical = asin ? ("https://" + (host || "www.amazon.com.au") + "/dp/" + asin) : (url.split("?")[0] || url);

  // Pre-order vs live: real DOM elements only (the released page still contains
  // "PreorderButton" strings inside its scripts, so a raw text search would lie).
  // true = pre-order, false = buyable now, null = can't tell (treated as no-change).
  //
  // Everything below was shaped by running this code over REAL saved pages
  // (six fixtures: released/pre-order x Kindle/audiobook, Aug 2026). What broke:
  //   * audiobook buy buttons have auto ids (a-autoid-N) and class
  //     a-button-oneclick; the primary control is "Try Standard free" — an
  //     id/name lookup found nothing, so audiobook releases read as null all day
  //   * an audiobook PRE-ORDER also shows "Buy now with 1-Click"; the only tell
  //     is plain text "Pre-order purchase / Releases 25 August 2026" nearby
  //   * released KINDLE pages carry a HIDDEN series-subscription alert ("We will
  //     pre-order your items…", class aok-hidden) inside #rightCol — scanning
  //     the outermost container flipped every released novel to pre-order
  //   * with no buy box at all, falling back to the whole document let a customer
  //     review ("the next book will be released…") flip a released audiobook
  //
  // Helpers used only here.
  function isHiddenAncestry(node, top) {
    // true if any ancestor up to `top` is hidden (Amazon hides alerts with
    // aok-hidden / a-hidden / hidden attr / display:none)
    var el = node && node.parentElement;
    var hops = 0;
    while (el && el !== top && hops++ < 40) {
      var cls = " " + (typeof el.className === "string" ? el.className : "") + " ";
      if (cls.indexOf(" aok-hidden ") >= 0 || cls.indexOf(" a-hidden ") >= 0 || cls.indexOf(" hidden ") >= 0) return true;
      if (el.getAttribute && (el.getAttribute("hidden") !== null || el.getAttribute("aria-hidden") === "true")) return true;
      var st = el.getAttribute && (el.getAttribute("style") || "");
      if (/display\s*:\s*none|visibility\s*:\s*hidden/i.test(st)) return true;
      el = el.parentElement;
    }
    return false;
  }
  function visibleText(root, cap) {
    // visible text of `root`: no script/style, no hidden subtrees
    var out = "", n = 0;
    try {
      var tw = doc.createTreeWalker(root, 4 /* SHOW_TEXT */), tn;
      while ((tn = tw.nextNode()) && n++ < (cap || 6000)) {
        var pe = tn.parentElement, tag = pe && pe.tagName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE") continue;
        if (isHiddenAncestry(tn, root)) continue;
        var t = clean(tn.nodeValue);
        if (t) out += " " + t;
      }
    } catch (e) {}
    return out;
  }
  function visibleLabels(root, max) {
    var els = root.querySelectorAll('.a-button-text, button, input[type="submit"], [id$="-announce"]');
    var seen = {}, out = [];
    for (var i = 0; i < els.length && out.length < (max || 8); i++) {
      var e = els[i];
      var t = e.tagName === "INPUT" ? clean(e.getAttribute("value") || e.getAttribute("aria-label") || "") : clean(visibleText(e, 200));
      if (!t || t.length > 60 || seen[t]) continue;   // long = script leaked in
      seen[t] = 1; out.push(t);
    }
    return out;
  }

  var preorder = null, scope = null;
  try {
    // INNERMOST buy box first. #rightCol is a last resort because it holds far
    // more than the buy box (carousels, hidden alerts).
    ['#buybox', '#desktop_buybox', '#CombinedBuybox', '#Unified-Buybox-Container', '#rightCol'].some(function (sel) {
      scope = doc.querySelector(sel); return !!scope;
    });
    if (scope) {
      var labels = visibleLabels(scope, 24);
      var annText = " | " + labels.join(" | ") + " | ";
      var boxText = visibleText(scope);
      var preEl = scope.querySelector('[id*="preorder" i], [class*="a-button-preorder"], [name*="preorder" i], [id*="pre-order" i]');
      var PRE = /pre-?order|releases (?:on )?\d|will be released|available (?:on|for pre-?order)|coming soon|not yet (?:released|available)/i;
      if ((preEl && !isHiddenAncestry(preEl, scope) && !isHiddenAncestry({ parentElement: preEl }, scope)) || PRE.test(annText) || PRE.test(boxText)) {
        preorder = true;
      } else {
        var buyEl = scope.querySelector(
          '#one-click-button, #buy-now-button, #add-to-cart-button, [id^="buy-now"], [id^="add-to-cart"],' +
          ' #audibleBuyButton, #adbl_buy_button, [name="submit.add-to-cart"], [name="submit.buy-now"],' +
          ' .a-button-oneclick, #cloudPlayerSampleID, #audible-title-giftv2-button, #newAccordionRow_1 input[name^="submit."]');
        var BUY = /add to (library|cart|basket)|buy now|buy with 1-click|buy for |listen now|read now|read for free|included (?:in|with)|start (?:listening|reading)|try (?:standard|premium|audible|plus) free|free (?:with|trial)|deliver to|give as gift/i;
        if ((buyEl && !isHiddenAncestry({ parentElement: buyEl }, scope)) || BUY.test(annText)) preorder = false;
      }
    }
    // no recognised buy box: leave null ("can't tell") — never guess from the
    // whole page, where reviews and carousels talk about other books
  } catch (e) { preorder = null; }

  // Diagnostic breadcrumb: when the buy box can't be classified, say WHY in the
  // payload so the app-side log shows what the background fetch actually saw.
  // Ordered so the important bits survive the app's log truncation.
  var _diag = null;
  if (preorder === null) {
    try {
      _diag = {
        signin: /Sign in|sign-in|ap\/signin/i.test(doc.title || "") || !!doc.querySelector('#ap_email, form[name="signIn"]'),
        robot: !!doc.querySelector('#captchacharacters, form[action*="validateCaptcha"]'),
        buybox: scope ? (scope.id || scope.tagName) : null,
        buttons: scope ? visibleLabels(scope, 8) : [],
        text: scope ? visibleText(scope, 300).slice(0, 120) : "",
        htmlLen: (doc.documentElement && doc.documentElement.outerHTML || "").length
      };
    } catch (e2) { _diag = { err: String(e2) }; }
  }

  return {
    series: series, author: author, series_total: total,
    book: { n: n, title: title, asin: asin, cover: coverUrl(),
            narrator: isAudio ? narrator : null,
            novel_date: isAudio ? null : dt, audiobook_date: isAudio ? dt : null },
    _isAudio: isAudio, _date: dt, _url: canonical, _asin: asin, _preorder: preorder, _diag: _diag
  };
}

function RR_valid(d) {
  return !!(d && d.series && d.book && d.book.n != null && (d.book.novel_date || d.book.audiobook_date));
}

// Find the ASINs worth crawling next from this page:
//   siblings = other books in the SAME series (from #seriesCarousel and the
//              "Book N of M" series link) — these never include "also bought".
//   formats  = the Kindle/Audiobook editions of THIS book (paperback/hardcover
//              are skipped). This is the bridge that lets one Kindle page reach
//              the audiobook and vice-versa.
function RR_related(doc) {
  function uniq(a) { var s = {}, r = []; a.forEach(function (x) { if (x && !s[x]) { s[x] = 1; r.push(x); } }); return r; }
  function asinOf(a) { var m = ((a && a.getAttribute("href")) || "").match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/); return m ? m[1] : null; }

  var siblings = [], formats = [];
  doc.querySelectorAll("#seriesCarousel a[href]").forEach(function (a) { var x = asinOf(a); if (x) siblings.push(x); });
  // NOTE: the "Book N of M: <Series>" byline link points at the series *landing* page
  // (a non-book ASIN with no publication date), not a sibling book — so we do NOT collect
  // it. Real siblings come from #seriesCarousel above. Collecting the byline link funneled
  // ~one unparseable series-page ASIN per series into the crawl, where it failed forever.
  ["#tmmSwatches", "#formats"].forEach(function (sel) {
    doc.querySelectorAll(sel + " a[href]").forEach(function (a) {
      var x = asinOf(a); if (!x) return;
      var href = a.getAttribute("href") || "";
      var t = (a.textContent || "").replace(/\s+/g, " ").toLowerCase();
      if (/\/ref=tmm_(kin|aud)_swatch/.test(href)) { formats.push(x); return; }
      // physical audio ("Audio CD", "MP3 CD") is not an audiobook edition; its
      // page would be read as a NOVEL and misfile the timeline
      if (/audio cd|mp3 cd|cd-rom|cassette/.test(t)) return;
      if (/kindle|audible|audiobook/.test(t)) formats.push(x);
    });
  });
  return { siblings: uniq(siblings), formats: uniq(formats) };
}

// expose for non-module contexts
if (typeof self !== "undefined") { self.RR_extract = RR_extract; self.RR_valid = RR_valid; self.RR_related = RR_related; }
