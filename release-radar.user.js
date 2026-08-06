// ==UserScript==
// @name         Release Radar — Amazon reader
// @namespace    release-radar
// @version      1.1
// @description  Reads the book or audiobook on the current Amazon page and sends its release date to your local Release Radar app.
// @author       Release Radar
// @match        https://www.amazon.com.au/*
// @match        https://www.amazon.com/*
// @match        https://www.amazon.co.uk/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  // Where your Release Radar app is listening. Change the port here if you run it
  // on a different one.
  var RADAR = "http://127.0.0.1:8765/api/ingest";
  // Set to false if you'd rather press the button yourself instead of it sending
  // automatically when a book page loads.
  var AUTO_SEND = true;

  var MONTHS = {
    january: 1, february: 2, march: 3, april: 4, may: 5, june: 6, july: 7,
    august: 8, september: 9, october: 10, november: 11, december: 12,
    jan: 1, feb: 2, mar: 3, apr: 4, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9,
    oct: 10, nov: 11, dec: 12
  };

  function clean(s) { return (s || "").replace(/[\u200e\u200f\u202a-\u202e]/g, "").replace(/\s+/g, " ").trim(); }

  function parseDate(s) {
    s = clean(s);
    if (!s) return null;
    var m = s.match(/(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})/);          // 18 March 2026
    if (m && MONTHS[m[2].toLowerCase()]) {
      return m[3] + "-" + pad(MONTHS[m[2].toLowerCase()]) + "-" + pad(m[1]);
    }
    m = s.match(/([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})/);            // March 18, 2026
    if (m && MONTHS[m[1].toLowerCase()]) {
      return m[3] + "-" + pad(MONTHS[m[1].toLowerCase()]) + "-" + pad(m[2]);
    }
    m = s.match(/(\d{4})-(\d{2})-(\d{2})/);                          // already ISO
    if (m) return m[0];
    var d = new Date(s);
    if (!isNaN(d.getTime())) return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return null;
  }
  function pad(n) { n = String(n); return n.length < 2 ? "0" + n : n; }

  // Read Amazon's "Rich Product Information" attribute cards (stable machine names).
  function rpiMap() {
    var map = {};
    document.querySelectorAll(".rpi-attribute-content[data-rpi-attribute-name]").forEach(function (c) {
      var name = c.getAttribute("data-rpi-attribute-name");
      var lab = c.querySelector(".rpi-attribute-label");
      var val = c.querySelector(".rpi-attribute-value");
      map[name] = { label: clean(lab && lab.textContent), value: clean(val && val.textContent) };
    });
    return map;
  }

  // Fallback: the "Product details" bullet list.
  function detailBullets() {
    var out = {};
    document.querySelectorAll("#detailBullets_feature_div li, #productDetailsTable li").forEach(function (li) {
      var t = clean(li.textContent);
      var idx = t.indexOf(":");
      if (idx > 0) out[t.slice(0, idx).trim().toLowerCase()] = t.slice(idx + 1).trim();
    });
    return out;
  }

  function titleText() {
    var el = document.querySelector("#productTitle") || document.querySelector("#title");
    return clean(el ? el.textContent : document.title);
  }

  function extract() {
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
    // fallbacks from the title: "(Chrysalis, Book 7)" or "Chrysalis 8: ..."
    if (n == null) {
      var t1 = title.match(/,\s*Book\s+(\d+)\s*\)/i) || title.match(/\bBook\s+(\d+)\b/i) || title.match(/^[^\d]*?(\d{1,3})\s*[:\-]/);
      if (t1) n = parseInt(t1[1], 10);
    }
    if (!series) {
      var t2 = title.match(/\(([A-Za-z0-9'’ ]+?),\s*Book\s+\d+\)/i) || title.match(/^([A-Za-z0-9'’ ]+?)\s+\d+\s*[:]/);
      if (t2) series = clean(t2[1]);
    }

    // is this the audiobook edition?
    var isAudio = !!(m["audiobook_details-program_type"] || m["audiobook_details-listening_length"] ||
                     m["audiobook_details-narrator"]) ||
                  /audio_download|\/audible|audiobook/i.test(location.href) ||
                  /audible\.com/i.test(location.hostname);

    // release date — Amazon labels both the ebook and the audiobook "Publication date"
    var dateStr = (m["book_details-publication_date"] && m["book_details-publication_date"].value) ||
                  db["publication date"] || db["release date"] || db["audible.com release date"] || null;
    var dt = parseDate(dateStr);

    // author
    var author = (m["audiobook_details-author"] && m["audiobook_details-author"].value) || null;
    if (!author) {
      var by = document.querySelector("#bylineInfo");
      if (by) {
        var bm = clean(by.textContent).match(/^\s*([^()]+?)\s*\(Author\)/);
        author = bm ? clean(bm[1]) : clean((by.textContent.split("(")[0] || ""));
      }
    }

    // narrator (audiobook pages only)
    var narrator = (m["audiobook_details-narrator"] && m["audiobook_details-narrator"].value) || null;

    // ASIN
    var asin = (m["book_details-asin"] && m["book_details-asin"].value) ||
               (location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1] ||
               (location.href.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/) || [])[1] || null;

    return {
      series: series, author: author, series_total: total,
      book: { n: n, title: title, asin: asin,
              narrator: isAudio ? narrator : null,
              novel_date: isAudio ? null : dt, audiobook_date: isAudio ? dt : null },
      _isAudio: isAudio, _date: dt
    };
  }

  function valid(d) {
    return d && d.series && d.book.n != null && (d.book.novel_date || d.book.audiobook_date);
  }

  // ---- tiny floating UI ----
  var box = document.createElement("div");
  box.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;" +
    "background:#1c1740;color:#efe9ff;border:1px solid #3a3170;border-radius:12px;padding:10px 12px;max-width:300px;box-shadow:0 8px 30px rgba(0,0,0,.4)";
  var msg = document.createElement("div");
  var btn = document.createElement("button");
  btn.textContent = "Send to Radar";
  btn.style.cssText = "margin-top:8px;background:#f0a500;color:#1c1740;border:0;border-radius:8px;padding:6px 12px;font-weight:600;cursor:pointer";
  btn.onclick = function () { send(true); };
  box.appendChild(msg); box.appendChild(btn);

  function setMsg(html) { msg.innerHTML = html; }
  function show() { if (!box.parentNode) document.body.appendChild(box); }

  function send(manual) {
    var d = extract();
    if (!valid(d)) {
      setMsg("Radar: couldn't read this page (need a series, book #, and a date). Open a book or audiobook product page.");
      show();
      return;
    }
    var kind = d._isAudio ? "audiobook" : "novel";
    setMsg("Sending <b>" + escapeHtml(d.series) + "</b> · Book " + d.book.n + " · " + kind + " " + d._date + " …");
    show();
    GM_xmlhttpRequest({
      method: "POST", url: RADAR, headers: { "Content-Type": "application/json" },
      data: JSON.stringify(d),
      onload: function (r) {
        var j = null; try { j = JSON.parse(r.responseText); } catch (e) {}
        if (j && j.ok) {
          setMsg("\u2713 <b>" + escapeHtml(j.series) + "</b> · Book " + j.n + " · " + kind + " <b>" + d._date + "</b>" +
                 (j.created ? " (new series added)" : "") + " sent to Radar.");
        } else {
          setMsg("Radar: " + ((j && j.message) || "unexpected response."));
        }
      },
      onerror: function () { setMsg("Radar isn't reachable \u2014 is the app open? (" + RADAR + ")"); },
      ontimeout: function () { setMsg("Radar timed out \u2014 is the app open?"); }
    });
  }

  function escapeHtml(s) { return (s || "").replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  // Show what we detected, and (optionally) auto-send once.
  var d0 = extract();
  if (valid(d0)) {
    var kind0 = d0._isAudio ? "audiobook" : "novel";
    setMsg("Release Radar detected <b>" + escapeHtml(d0.series) + "</b> · Book " + d0.book.n + " · " + kind0 + " " + d0._date + ".");
    show();
    if (AUTO_SEND) setTimeout(function () { send(false); }, 800);
  }
})();
