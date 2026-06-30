// Popup dashboard. Reads state from the background, shows live crawl progress,
// and lets you trigger a crawl / refresh / clear.

function $(id) { return document.getElementById(id); }
function ago(ts) {
  if (!ts) return "never";
  var s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return s + "s ago";
  var m = Math.round(s / 60); if (m < 60) return m + "m ago";
  var h = Math.round(m / 60); if (h < 24) return h + "h ago";
  return Math.round(h / 24) + "d ago";
}
function esc(s) { return (s || "").replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
function send(type, extra) { return browser.runtime.sendMessage(Object.assign({ type: type }, extra || {})); }

function renderSeries(urls) {
  var box = $("series");
  var keys = Object.keys(urls || {});
  if (!keys.length) { box.innerHTML = '<div class="empty">No pages registered yet. Open one Amazon/Audible page per series once — it crawls the rest.</div>'; return; }
  var by = {};
  keys.forEach(function (k) {
    var u = urls[k]; var name = u.series || "(unknown)";
    if (!by[name]) by[name] = { novels: 0, audios: 0, last: 0, anyErr: false, allOk: true };
    if (u.isAudio) by[name].audios++; else by[name].novels++;
    if (u.lastChecked && u.lastChecked > by[name].last) by[name].last = u.lastChecked;
    if (u.lastResult === "err") { by[name].anyErr = true; by[name].allOk = false; }
    if (u.lastResult !== "ok") by[name].allOk = false;
  });
  var names = Object.keys(by).sort(function (a, b) { return a.localeCompare(b); });
  box.innerHTML = names.map(function (n) {
    var g = by[n];
    var pill = g.allOk ? '<span class="pill ok">ok</span>' : (g.anyErr ? '<span class="pill err">check</span>' : '<span class="pill">new</span>');
    return '<div class="s-item"><div><div class="s-name">' + esc(n) + '</div>' +
      '<div class="s-sub">' + g.novels + ' novel \u00b7 ' + g.audios + ' audio</div></div>' +
      '<div class="s-right">' + pill + '<div class="s-sub">' + ago(g.last) + '</div></div></div>';
  }).join("");
}

function renderProgress(p) {
  var wrap = $("progWrap"), busy = p && p.running;
  $("crawlBtn").disabled = !!busy; $("refreshBtn").disabled = !!busy;
  if (!busy) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  var opName = p.op === "refresh" ? "Refreshing dates" : "Crawling series";
  $("progText").textContent = opName + " \u2014 " + p.checked + (p.total ? "/" + p.total : "") + " checked, " + p.sent + " sent";
  var bar = $("bar"), fill = $("barFill");
  if (p.total) { bar.classList.remove("indet"); fill.style.width = Math.min(100, Math.round(100 * p.checked / p.total)) + "%"; }
  else { bar.classList.add("indet"); fill.style.width = "100%"; }
  $("cur").textContent = p.current || "";
}

var curPaused = false;

async function refreshView() {
  var st;
  try { st = await send("state"); } catch (e) { return; }
  if (!st) return;
  curPaused = !!st.paused;
  var pt = $("pauseTitle"), ps = $("pauseSub"), pb = $("pauseBtn"), pr = $("pauseRow");
  if (pt) {
    pt.textContent = curPaused ? "Adding new pages: off" : "Adding new pages: on";
    ps.textContent = curPaused
      ? "Browsing freely — opened pages won't be added. Tracked series still update on schedule."
      : "Opening a book page adds and crawls its series.";
    pb.textContent = curPaused ? "Resume" : "Pause";
    pb.style.background = curPaused ? "var(--amber)" : "#241d52";
    pb.style.color = curPaused ? "#1c1740" : "var(--ink)";
    pb.style.borderColor = curPaused ? "var(--amber)" : "var(--line)";
    pr.style.borderColor = curPaused ? "#5a4a25" : "var(--line)";
    pr.style.background = curPaused ? "linear-gradient(180deg,#2c2417,#1f1c2b)" : "transparent";
  }
  var urls = st.urls || {}, n = Object.keys(urls).length;
  $("stat").innerHTML = "<b>" + n + "</b> page" + (n === 1 ? "" : "s") + " registered across <b>" +
    new Set(Object.keys(urls).map(function (k) { return urls[k].series; })).size + "</b> series.";
  $("lastline").textContent = "refresh " + ago(st.lastRefresh && st.lastRefresh.at) + " \u00b7 crawl " + ago(st.lastCrawl && st.lastCrawl.at);
  renderSeries(urls);
  renderProgress(st.progress);
  renderNeedsCheck(st.bad);
}

function escapeHtml(s) { var d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

function renderNeedsCheck(bad) {
  bad = bad || {};
  var items = Object.keys(bad).map(function (a) { var e = bad[a]; return { asin: a, n: e.n || 0, t: e.t || 0, series: e.series, url: e.url }; })
    .filter(function (e) { return e.n >= 3; })
    .sort(function (a, b) { return b.t - a.t; });
  var wrap = $("needsCheckWrap"), box = $("needsCheck"), cnt = $("needsCount");
  if (!items.length) { if (wrap) wrap.style.display = "none"; if (box) box.innerHTML = ""; return; }
  wrap.style.display = "block";
  if (cnt) cnt.textContent = "(" + items.length + ")";
  box.innerHTML = items.map(function (e) {
    var label = e.series ? escapeHtml(e.series) : "Unknown series";
    var url = e.url || ("https://www.amazon.com.au/dp/" + e.asin);
    return '<div class="s-item"><div><div class="s-name">' + label + '</div>' +
      '<div class="s-sub">' + e.asin + ' \u00b7 failed ' + e.n + '\u00d7</div></div>' +
      '<div class="s-right"><a href="#" class="link" data-open="' + escapeHtml(url) + '">Open &rarr;</a></div></div>';
  }).join("");
  box.querySelectorAll("[data-open]").forEach(function (el) {
    el.onclick = function (ev) { ev.preventDefault(); try { browser.tabs.create({ url: el.getAttribute("data-open") }); } catch (e) {} };
  });
}

async function pingApp() {
  var r;
  try { r = await send("appPing"); } catch (e) { r = { ok: false }; }
  var dot = $("appdot");
  if (r && r.ok) { dot.className = "dot on"; $("appState").textContent = "app connected \u00b7 " + (r.series || 0) + " series"; $("appWarn").style.display = "none"; }
  else { dot.className = "dot off"; $("appState").textContent = "app not running"; $("appWarn").style.display = "block"; }
}

$("crawlBtn").onclick = function () { send("recrawl"); setTimeout(refreshView, 300); };
$("pauseBtn").onclick = function () { send("setPaused", { paused: !curPaused }).then(refreshView); };
$("refreshBtn").onclick = function () { send("refresh"); setTimeout(refreshView, 300); };
$("wipeBtn").onclick = function () {
  if (!confirm("Delete ALL series in the Radar app and rebuild ONLY from Amazon?\n\nThe app backs up your data first (recoverable). Then it re-crawls every registered series. This can take a while.")) return;
  $("wipeBtn").disabled = true;
  send("wipeAndCrawl").then(function (r) {
    $("wipeBtn").disabled = false;
    if (r && r.ok) alert("Done. Rebuilt from " + (r.checked || 0) + " page(s); sent " + (r.sent || 0) + " update(s).");
    else if (r && r.error) alert("Couldn't wipe: " + r.error);
    refreshView();
  });
};
$("clearBtn").onclick = function () {
  if (!confirm("Forget all registered pages? You'll need to open one page per series again to re-register them.")) return;
  send("clearList").then(refreshView);
};
$("clearBadBtn").onclick = function () {
  if (!confirm("Clear the needs-a-check list? Those pages will be tried again on the next full crawl.")) return;
  send("clearBadList").then(refreshView);
};

async function getLogText() {
  var r = await send("getLog"); var lines = (r && r.log) || [];
  return "Release Radar extension log\n" + new Date().toString() + "\n" + lines.length + " entries\n\n" + lines.join("\n") + "\n";
}
$("copyLogBtn").onclick = async function () {
  var t = await getLogText();
  try { await navigator.clipboard.writeText(t); $("copyLogBtn").textContent = "Copied!"; setTimeout(function () { $("copyLogBtn").textContent = "Copy log"; }, 1500); }
  catch (e) { alert("Couldn't copy automatically — use Download log instead."); }
};
$("dlLogBtn").onclick = async function () {
  var t = await getLogText();
  var url = URL.createObjectURL(new Blob([t], { type: "text/plain" }));
  var a = document.createElement("a"); a.href = url; a.download = "release-radar-log.txt";
  document.body.appendChild(a); a.click();
  setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 1000);
};
$("clearLogBtn").onclick = function () { send("clearLog"); $("clearLogBtn").textContent = "Cleared"; setTimeout(function () { $("clearLogBtn").textContent = "Clear log"; }, 1200); };
$("resetBtn").onclick = function () {
  if (!confirm("Only do this if the progress bar is stuck and won't move. It clears the 'running' flag so you can start again.")) return;
  send("reset").then(refreshView);
};

refreshView(); pingApp();
setInterval(refreshView, 1000);
setInterval(pingApp, 4000);
