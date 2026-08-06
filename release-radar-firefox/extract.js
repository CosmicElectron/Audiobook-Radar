// Shared by the content script (live page) and the background poller (fetched
// HTML parsed with DOMParser). Takes a Document + the page URL, returns the
// payload Release Radar expects, or null if this isn't a usable book page.

function RR_extract(doc, url) {
  url = url || "";
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
    if (m && MONTHS[m[2].toLowerCase()]) return m[3] + "-" + pad(MONTHS[m[2].toLowerCase()]) + "-" + pad(m[1]);
    m = s.match(/([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})/);            // March 18, 2026
    if (m && MONTHS[m[1].toLowerCase()]) return m[3] + "-" + pad(MONTHS[m[1].toLowerCase()]) + "-" + pad(m[2]);
    m = s.match(/(\d{4})-(\d{2})-(\d{2})/);                          // already ISO
    if (m) return m[0];
    var d = new Date(s);
    if (!isNaN(d.getTime())) return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return null;
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
                /audio_download|\/audible|audiobook/i.test(url) || /audible\./i.test(host);

  var dateStr = (m["book_details-publication_date"] && m["book_details-publication_date"].value) ||
                db["publication date"] || db["release date"] || db["audible.com release date"] || null;
  var dt = parseDate(dateStr);

  var author = (m["audiobook_details-author"] && m["audiobook_details-author"].value) || null;
  if (!author) {
    var by = doc.querySelector("#bylineInfo");
    if (by) {
      var bm = clean(by.textContent).match(/^\s*([^()]+?)\s*\(Author\)/);
      author = bm ? clean(bm[1]) : clean((by.textContent.split("(")[0] || ""));
    }
  }

  // narrator (audiobook pages only) — the app shows it on the card and pings
  // when a series changes voice
  var narrator = (m["audiobook_details-narrator"] && m["audiobook_details-narrator"].value) || null;
  if (!narrator) {
    var bl = clean((doc.querySelector("#bylineInfo") || {}).textContent || "");
    var nm = bl.match(/([^(),]+?)\s*\(Narrator\)/);
    if (nm) narrator = clean(nm[1]);
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
  // The buy box differs per format, and the original selectors only covered the
  // Kindle one — so audiobook pages always came back null and their releases were
  // never confirmed. Audible-edition pages carry their own buttons/labels, hence
  // the extra selectors and phrases below.
  var preorder = null;
  try {
    var annText = "";
    var anns = doc.querySelectorAll('[id$="-announce"], #buybox .a-button-text, #audibleBuyBoxLabel, #buyboxLabel');
    for (var ai = 0; ai < anns.length; ai++) annText += " | " + clean(anns[ai].textContent);
    var preEl = doc.querySelector('[id*="preorder"], [id*="preOrder"], [class*="a-button-preorder"], [name*="preorder"]');
    if (preEl || /pre-?order|releases on|available (?:on|for pre-?order)/i.test(annText)) preorder = true;
    else {
      var buyEl = doc.querySelector(
        '#one-click-button, #buy-now-button, #add-to-cart-button, [id^="buy-now"], [id^="add-to-cart"],' +
        ' #audibleBuyButton, #adbl_buy_button, [name="submit.add-to-cart"], [name="submit.buy-now"],' +
        ' #newAccordionRow_1 input[name^="submit."]');
      if (buyEl || /add to (library|cart|basket)|buy now|buy with 1-click|buy for |listen now|read now|read for free|included (?:in|with)|start (?:listening|reading)|deliver to/i.test(annText)) preorder = false;
    }
  } catch (e) { preorder = null; }

  return {
    series: series, author: author, series_total: total,
    book: { n: n, title: title, asin: asin, cover: coverUrl(),
            narrator: isAudio ? narrator : null,
            novel_date: isAudio ? null : dt, audiobook_date: isAudio ? dt : null },
    _isAudio: isAudio, _date: dt, _url: canonical, _asin: asin, _preorder: preorder
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
      var t = (a.textContent || "").toLowerCase();
      if (/kindle|audio|audible/.test(t)) formats.push(x);
    });
  });
  return { siblings: uniq(siblings), formats: uniq(formats) };
}

// expose for non-module contexts
if (typeof self !== "undefined") { self.RR_extract = RR_extract; self.RR_valid = RR_valid; self.RR_related = RR_related; }
