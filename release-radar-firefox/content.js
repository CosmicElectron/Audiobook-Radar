// Runs on Amazon product pages you open. Opening ONE page of a series is
// enough: it asks the background to crawl the whole series — every book, both the
// Kindle (novel) and audiobook editions — and send the dates to Radar.
//
// If collecting is paused, this stays completely silent so you can browse and look
// at new novels without anything being added or crawled.

(function () {
  "use strict";

  browser.storage.local.get("paused").then(function (o) {
    if (o && o.paused) return;        // paused: do nothing, don't register, don't crawl
    run();
  }).catch(function () { run(); });

  function run() {
    var data = RR_extract(document, location.href);
    if (!RR_valid(data)) return;      // not a usable book/audiobook page — stay quiet

    var kind = data._isAudio ? "audiobook" : "novel";
    panel("Found <b>" + esc(data.series) + "</b> &middot; Book " + data.book.n + " (" + kind + " " + data._date + ")");

    try {
      browser.runtime.sendMessage({ type: "crawl", url: data._url || location.href }).then(function (r) {
        if (r && r.paused) { if (box) box.remove(); return; }
        var more = (r && r.pending > 1) ? (" (" + r.pending + " series queued)") : "";
        panel("\u2713 Queued <b>" + esc(data.series) + "</b> &mdash; crawling its whole series in the background" + more +
              ".<br>Open the Release Radar toolbar icon to watch progress.");
      }, function () { panel("Couldn't reach the Radar extension background."); });
    } catch (e) { panel("Couldn't reach the Radar extension background."); }
  }

  // ---- tiny floating panel ----
  var box;
  function panel(htmlStr) {
    if (!box) {
      box = document.createElement("div");
      box.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;" +
        "background:#1c1740;color:#efe9ff;border:1px solid #3a3170;border-radius:12px;padding:10px 12px;max-width:330px;box-shadow:0 8px 30px rgba(0,0,0,.45)";
      (document.body || document.documentElement).appendChild(box);
    }
    box.innerHTML = htmlStr;
    clearTimeout(box._t);
    box._t = setTimeout(function () {
      if (box) { box.style.transition = "opacity .6s"; box.style.opacity = "0"; setTimeout(function () { if (box && box.parentNode) box.parentNode.removeChild(box); box = null; }, 700); }
    }, 9000);
  }
  function esc(s) { return (s || "").replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
})();
