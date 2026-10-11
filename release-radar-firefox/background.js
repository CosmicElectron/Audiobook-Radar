// Background page. Does all the network work and keeps an exportable log.
//
// IMPORTANT: crawls are SERIALIZED. Opening many tabs at once enqueues each
// series and a single worker crawls them one at a time, paced, with a shared
// visited-set. (Previously every tab launched its own crawl in parallel, which
// made Amazon return bot-check pages and scrambled the progress counters.)
//
// Also: every fetch has a timeout; blocked/timed-out pages are retried once after
// a pause; the page is persistent so long runs aren't killed; start/finish is
// wrapped so the running flag can't get stuck; and a safety net fetches any known
// page a series list didn't reach.

var RADAR_BASE = "http://127.0.0.1:8765";        // change the port here if needed
var RADAR = RADAR_BASE + "/api/ingest";

var A_REFRESH = "rr-refresh", A_CRAWL = "rr-crawl", A_RELEASE = "rr-release-hourly";
var REFRESH_MIN = 24 * 60, CRAWL_MIN = 3 * 24 * 60, RELEASE_MIN = 60;
var CRAWL_CAP = 120;                  // max pages per series in one pass (safety)
var MIN_GAP = 2000, JITTER = 2000;    // pacing between fetches (ms)
var PAGE_TIMEOUT = 25000, APP_TIMEOUT = 10000;
var MAX_TRIES = 2, RETRY_BASE = 6000; // one retry, after a 6s pause, for blocks/timeouts
var BAD_TRIES = 3, BAD_COOLDOWN = 7 * 24 * 60 * 60 * 1000; // skip a page after 3 straight fails, retry after 7 days
var CMD_POLL_MS = 20000;              // how often we ask the app for queued commands
var LOG_MAX = 600;

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function originOf(u) { try { return new URL(u).origin; } catch (e) { return "https://www.amazon.com.au"; } }
function asinOf(u) { var m = (u || "").match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/); return m ? m[1] : null; }
function shortUrl(u) { var m = (u || "").match(/\/(?:dp|gp\/product)\/[A-Z0-9]{10}/); return m ? m[0] : (u || "").slice(0, 64); }
function norm(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }

// Pages that never parse (sign-in walls, bad editions) are remembered so we stop
// re-fetching them every crawl. They get another chance after BAD_COOLDOWN.
function isBad(asin, bad) { var e = bad && bad[asin]; return !!(e && e.n >= BAD_TRIES && (Date.now() - e.t) < BAD_COOLDOWN); }
async function markBad(asin, series, url) {
  if (!asin) return;
  try {
    var o = await browser.storage.local.get("bad"); var b = o.bad || {};
    var e = b[asin] || { n: 0, t: 0 };
    e.n++; e.t = Date.now();
    if (series) e.series = series;            // best-guess series this page belongs to
    if (url) e.url = url;
    b[asin] = e;
    await browser.storage.local.set({ bad: b });
  } catch (e) {}
}
async function clearBad(asin) {
  if (!asin) return;
  try { var o = await browser.storage.local.get("bad"); var b = o.bad || {}; if (b[asin]) { delete b[asin]; await browser.storage.local.set({ bad: b }); } } catch (e) {}
}
async function loadBad() { try { var o = await browser.storage.local.get("bad"); return o.bad || {}; } catch (e) { return {}; } }

// ---- single-worker lock (synchronous, so it actually serializes) ----
var _busy = false;
var _seedQueue = [];
// Work that arrived while the lock was held. Without this, queued crawl seeds
// strand forever (only the crawl worker drains them) and an hourly release check
// that lands during a refresh is silently dropped rather than deferred.
var _pending = { refresh: false, release: false, crawl: false };
// Bumped by the popup's "Reset if stuck": running loops compare against it and
// bail out, so freeing the lock cannot leave two unpaced loops fetching at once.
var _gen = 0;
var _progressAt = 0;                     // last moment a job demonstrably moved (a page read)
function acquire() { if (_busy) return false; _busy = true; _progressAt = Date.now(); return true; }
// Belt and braces for the lock: whatever hangs a job (a stalled response body,
// a thrown finally, a bug in a future path), a worker that has not read a page
// in this long is stuck, and a stuck lock silently starves every scheduled read
// until the browser is restarted. Progress-based on purpose: a full forced
// re-acquire of a big library legitimately runs for a long time, but never
// goes this long between pages (one page is bounded by PAGE_TIMEOUT + retries).
var STALL_MAX_MS = 15 * 60 * 1000;
function lockWatchdog() {
  if (!_busy || Date.now() - _progressAt < STALL_MAX_MS) return;
  var min = Math.round((Date.now() - _progressAt) / 60000);
  _gen++;                                   // running loops bail at their next iteration
  _busy = false; endP();
  // the stuck job may be the one drainPending is awaiting: that drain is a
  // zombie now (its finally only runs if the await ever settles), so the flag
  // it holds must be taken from it or nothing deferred would ever run again
  _draining = false;
  logLine("worker stuck (no page read for " + min + " min) - reset automatically; running the deferred work");
  setTimeout(drainPending, 0);
}
setInterval(lockWatchdog, 60 * 1000);
// Pass the generation captured at acquire() time. A loop cancelled by Reset must
// NOT clear _busy on its way out: Reset already freed the lock, so a new job may
// legitimately hold it and releasing here would let a second loop start.
function release(gen) {
  if (gen !== undefined && gen !== _gen) return;
  _busy = false;
  setTimeout(drainPending, 0);      // async so it never re-enters a finally block
}
// Drain everything that queued up while the lock was held. This must keep going
// rather than dispatch one item: a deferred job can decline to run (the release
// watch returns early when nothing is due), and stopping there would strand the
// rest — a deferred "Crawl all" would simply never happen.
var _draining = false;
async function drainPending() {
  if (_draining) return;
  _draining = true;
  // generation-aware: a drain whose job was cancelled by Reset/lockWatchdog
  // must neither keep dispatching nor clear the flag a newer drain now owns
  var g = _gen;
  try {
    for (var guard = 0; guard < 8 && !_busy && g === _gen; guard++) {
      if (_seedQueue.length) { await runSeedWorker(); continue; }
      if (_pending.release) { _pending.release = false; await checkReleasingToday(); continue; }
      if (_pending.refresh) { var f = _pending.refresh === "force"; _pending.refresh = false; await refreshKnown(f); continue; }
      if (_pending.crawl) { _pending.crawl = false; await recrawlAll(false); continue; }
      break;
    }
  } finally { if (g === _gen) _draining = false; }
}

// ---- log ----
var _log = [];
async function loadLog() { try { var o = await browser.storage.local.get("log"); _log = o.log || []; } catch (e) {} }
function logLine(msg) {
  var line = new Date().toISOString().replace("T", " ").slice(0, 19) + "  " + msg;
  _log.push(line);
  if (_log.length > LOG_MAX) _log = _log.slice(_log.length - LOG_MAX);
  try { browser.storage.local.set({ log: _log }); } catch (e) {}
  try { console.log("[RR] " + msg); } catch (e) {}
}

// ---- progress (mirrored to storage for the popup) ----
var P = { running: false, op: "", checked: 0, sent: 0, failed: 0, total: 0, current: "", startedAt: 0, finishedAt: 0 };
function saveP() { try { browser.storage.local.set({ progress: P }); } catch (e) {} }
function startP(op, total) { P = { running: true, op: op, checked: 0, sent: 0, failed: 0, total: total || 0, current: "", startedAt: Date.now(), finishedAt: 0 }; saveP(); }
function endP() { P.running = false; P.current = ""; P.finishedAt = Date.now(); saveP(); }

function fetchTimeout(url, opts, ms) {
  var ctrl = new AbortController();
  var t = setTimeout(function () { ctrl.abort(); }, ms || PAGE_TIMEOUT);
  // The timer must outlive the headers. fetch() resolves as soon as headers
  // arrive; a body that then stalled would hang text()/json() forever with the
  // abort already disarmed, and a hung read holds the worker lock (see
  // lockWatchdog for what that costs). The abort is disarmed only once the body
  // has been read (or the fetch failed).
  return fetch(url, Object.assign({}, opts, { signal: ctrl.signal })).then(function (r) {
    ["text", "json"].forEach(function (m) {
      var orig = r[m].bind(r);
      r[m] = function () { return orig().finally(function () { clearTimeout(t); }); };
    });
    return r;
  }, function (e) { clearTimeout(t); throw e; });
}

function postRadar(data) {
  return fetchTimeout(RADAR, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }, APP_TIMEOUT)
    .then(function (r) { return r.json(); })
    .catch(function (e) { logLine("  -> app POST failed: " + String(e)); return { ok: false, message: "Radar not reachable" }; });
}

// ---- settled bookkeeping ----
function pad2(n) { n = String(n); return n.length < 2 ? "0" + n : n; }
function todayISO() { var t = new Date(); return t.getFullYear() + "-" + pad2(t.getMonth() + 1) + "-" + pad2(t.getDate()); }
function isPastDate(d) { return !!(d && /^\d{4}-\d{2}-\d{2}$/.test(d) && d < todayISO()); }
// Whole days since a date: >0 past, 0 today, <0 future, null if unusable.
function daysSince(d) {
  if (!d || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return null;
  var a = new Date(d + "T00:00:00"), b = new Date(todayISO() + "T00:00:00");
  if (isNaN(a)) return null;
  return Math.round((b - a) / 86400000);
}

// A page is FINISHED once its release has happened AND we either watched it go
// live or waited long enough that nothing more can be learned. Date arithmetic
// alone is not enough: a date can pass while Firefox is closed, and retiring a
// page on that basis throws away the release confirmation for good.
var SETTLE_GRACE_DAYS = 3;
function pageDone(u) {
  var dd = daysSince(u && u.date);
  if (dd === null || dd <= 0) return false;
  return u.live === true || dd > SETTLE_GRACE_DAYS;
}
// Keyed through norm() because the app matches series names fuzzily; an edition
// naming variation must not split one book across two buckets.
function settledIndex(urls) {
  var m = {};
  Object.keys(urls || {}).forEach(function (k) {
    var u = urls[k]; var key = norm(u.series) + "\u0000" + u.n;
    if (!m[key]) m[key] = { novel: null, audio: null };
    if (!u.date) return;
    if (u.isAudio) m[key].audio = pageDone(u); else m[key].novel = pageDone(u);
  });
  return m;
}
function pageIsSettled(u, idx) {
  if (!u) return false;
  var bk = idx[norm(u.series) + "\u0000" + u.n];
  return bk ? (bk.novel === true && bk.audio === true) : false;
}
function nodeSettled(url, urls, idx) { var u = urls[url]; return u ? pageIsSettled(u, idx) : false; }

// A page still worth a daily read: anything whose book hasn't fully finished.
// Once both editions are out AND confirmed, it is never fetched again.
function refreshDue(u, idx) { return !pageIsSettled(u, idx); }

// Already fetched since midnight? Stops a crawl and the daily refresh from
// reading the same page twice on the same day.
function readToday(u) {
  if (!u || !u.lastChecked) return false;
  var d = new Date(u.lastChecked);
  return (d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())) === todayISO();
}

// Has a whole-library job run recently? Firefox does not persist alarms across
// browser sessions, so without this gate every restart re-runs the schedule.
async function ranWithin(key, ms) {
  try {
    var o = await browser.storage.local.get(key);
    var t = o[key] && o[key].at;
    return !!(t && (Date.now() - t) < ms);
  } catch (e) { return false; }
}
var REFRESH_MIN_MS = 20 * 60 * 60 * 1000;      // ~once a day
var CRAWL_MIN_MS = CRAWL_MIN * 60 * 1000;      // the crawl alarm's own period

async function remember(data, url, ok) {
  try {
    var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
    var key = data._url || url;
    var prev = urls[key] || {};
    // `live` is what the watcher stops on, and it is deliberately STICKY and
    // conservative: set only when the page read as buyable AND the app accepted
    // the POST. A read the app never received is not a delivered release, and a
    // later unreadable page (pre-order state comes back null on some layouts)
    // must not un-confirm one. A date that moves back into the future reopens it.
    var live = prev.live === true || (data._preorder === false && ok === true);
    if (prev.date && data._date && prev.date !== data._date) {
      var dd = daysSince(data._date);
      if (dd !== null && dd < 0) live = false;
    }
    urls[key] = { asin: data._asin || asinOf(url), series: data.series, n: data.book.n,
      isAudio: data._isAudio, date: data._date, lastChecked: Date.now(), lastResult: ok ? "ok" : "err",
      // last raw reading: true = pre-order, false = buyable, null = couldn't tell
      preorder: (data._preorder === undefined ? null : data._preorder),
      live: live };
    await browser.storage.local.set({ urls: urls });
  } catch (e) {}
}

// Fetch one page, with one retry after a pause if it looks blocked/timed-out.
// opts.backfill marks the ingest as aux-only (app fills missing dates but never
// overwrites existing ones — protects hand edits from backfill re-reads).
// ---- discovery: pages linked from a read that we've never read ----
// Only the 3-day crawl used to follow a page's links, and it skips pages the
// daily refresh already read that day - which, with the crawl running later in
// the day, was always. A brand-new audiobook listing linked from a Kindle page
// the refresh reads daily (Riftside 4, Sep 2026) was therefore never visited.
// Now every successful read remembers the series/format links it saw, and the
// job holding the worker reads the ones not in the registry before it finishes.
var DISCOVER_CAP = 25;                    // new pages per job (each is read once, then known)
var DISCOVER_RETRY_MS = 20 * 3600 * 1000; // a failed discovery isn't retried every job
var _discovered = {};                     // asin -> { origin, series }
var _attempted = {};                      // asin -> last read attempt (ms)
function noteDiscovered(url, related, series) {
  if (!related) return;
  var origin = originOf(url);
  (related.formats || []).concat(related.siblings || []).forEach(function (x) {
    if (x && !_discovered[x]) _discovered[x] = { origin: origin, series: series || "" };
  });
}
async function readDiscovered(gen) {
  var asins = Object.keys(_discovered);
  if (!asins.length) return;
  var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
  var known = {};
  Object.keys(urls).forEach(function (k) { var x = (urls[k] && urls[k].asin) || asinOf(k); if (x) known[x] = 1; });
  var bad = await loadBad(), n = 0;
  for (var i = 0; i < asins.length && n < DISCOVER_CAP && gen === _gen; i++) {
    var x = asins[i], d = _discovered[x];
    delete _discovered[x];
    if (known[x] || isBad(x, bad) || (_attempted[x] && Date.now() - _attempted[x] < DISCOVER_RETRY_MS)) continue;
    logLine("new page linked from " + (d.series || "a read") + ": /dp/" + x);
    await readPage(d.origin + "/dp/" + x, false, d.series);
    known[x] = 1; n++;
    await sleep(MIN_GAP + Math.random() * JITTER);
  }
}

async function readPage(url, force, seriesHint, opts) {
  var started = Date.now(), out = { ok: false }, tries = 0;
  _progressAt = started;                    // feeds lockWatchdog
  if (asinOf(url)) _attempted[asinOf(url)] = started;
  while (true) {
    tries++;
    var status = null, reason = null, blocked = false;
    try {
      var resp = await fetchTimeout(url, { credentials: "include", headers: { "Accept": "text/html,application/xhtml+xml" } }, PAGE_TIMEOUT);
      status = resp.status;
      if (!resp.ok) { reason = "http " + resp.status; blocked = (resp.status === 429 || resp.status === 503); }
      else {
        var doc = new DOMParser().parseFromString(await resp.text(), "text/html");
        var data = RR_extract(doc, url);
        if (data && opts && opts.backfill) data._backfill = true;
        out.related = RR_related(doc);
        if (RR_valid(data)) {
          var res = await postRadar(data);
          out.ok = !!(res && res.ok); out.data = data;
          P.current = data.series + " \u00b7 Book " + data.book.n + " (" + (data._isAudio ? "audio" : "novel") + ")";
          await remember(data, url, out.ok);
          await clearBad(asinOf(url));
          noteDiscovered(url, out.related, data.series);
          logLine((out.ok ? "OK   " : "SENT?") + data.series + " #" + data.book.n + " " + (data._isAudio ? "audio" : "novel") +
                  " " + (data._date || "?") + "  " + shortUrl(url) + "  (" + (Date.now() - started) + "ms)" + (res && res.created ? "  [new series]" : ""));
        } else { reason = "no book details"; blocked = true; }
      }
    } catch (e) {
      if (e && e.name === "AbortError") { reason = "timeout"; blocked = true; }
      else { reason = "error: " + String(e); blocked = false; }
    }
    out.reason = reason;
    if (out.ok) break;
    if (blocked && tries < MAX_TRIES) {
      var pause = RETRY_BASE * tries;
      logLine((reason === "timeout" ? "TIMEOUT" : (reason === "no book details" ? "blocked? no detail" : ("HTTP " + status))) +
              "; retry " + tries + "/" + (MAX_TRIES - 1) + " in " + (pause / 1000) + "s  " + shortUrl(url));
      await sleep(pause); continue;
    }
    if (reason === "timeout") logLine("TIMEOUT after " + (PAGE_TIMEOUT / 1000) + "s (gave up)  " + shortUrl(url));
    else if (reason === "no book details") { logLine("ROBOT/sign-in? no book details (gave up)  " + shortUrl(url)); await markBad(asinOf(url), seriesHint, url); }
    else if (reason && reason.indexOf("http") === 0) logLine("FAIL " + reason + "  " + shortUrl(url));
    else logLine((reason || "failed") + "  " + shortUrl(url));
    break;
  }
  P.checked++; if (out.ok) P.sent++; else P.failed++; saveP();
  return out;
}

async function crawlSeries(seedUrl, visited, urls, idx, force, bad, gen) {
  // the generation that guards this loop is the one captured at acquire() time
  // by the caller; re-reading _gen here would let a job cancelled during the
  // caller's earlier awaits run to completion beside its replacement
  gen = (gen === undefined) ? _gen : gen;
  visited = visited || {}; urls = urls || {}; idx = idx || {}; bad = bad || {};
  var origin = originOf(seedUrl), queue = [seedUrl], local = 0, first = true, skippedBad = 0, skippedToday = 0;
  var seriesHint = (urls[seedUrl] && urls[seedUrl].series) || "";
  while (queue.length && local < CRAWL_CAP && gen === _gen) {
    var url = queue.shift(), a = asinOf(url);
    if (a) { if (visited[a]) continue; visited[a] = 1; }
    if (!first && !force && nodeSettled(url, urls, idx)) { logLine("SKIP settled  " + shortUrl(url)); continue; }
    if (!first && !force && readToday(urls[url])) { skippedToday++; continue; }   // already read since midnight
    if (!first && a && isBad(a, bad)) { skippedBad++; continue; }   // sign-in/unparseable page; don't re-hammer
    first = false;
    var r = await readPage(url, force, seriesHint);
    if (r && r.data && r.data.series) seriesHint = r.data.series;   // learn the series from the first good read
    local++;
    if (r && r.related) {
      (r.related.siblings || []).concat(r.related.formats || []).forEach(function (x) { if (!visited[x] && !isBad(x, bad)) queue.push(origin + "/dp/" + x); });
    }
    await sleep(MIN_GAP + Math.random() * JITTER);
  }
  if (skippedBad) logLine("skipped " + skippedBad + " known-bad page(s) on " + shortUrl(seedUrl) + " (will retry after a week)");
  if (skippedToday) logLine("skipped " + skippedToday + " page(s) already read today on " + shortUrl(seedUrl));
  if (local >= CRAWL_CAP) logLine("note: hit per-series page cap (" + CRAWL_CAP + ") on " + shortUrl(seedUrl));
}

// ---- the single serialized worker ----

// Content-script crawls go through here: enqueue the seed, start the worker if
// it isn't already running. Returns immediately (the popup shows progress).
function enqueueCrawl(url) {
  if (url) _seedQueue.push(url);
  if (!_busy) runSeedWorker();           // fire-and-forget; persistent page keeps it alive
  return Promise.resolve({ queued: true, pending: _seedQueue.length, busy: _busy });
}

async function runSeedWorker() {
  if (!acquire()) return;                 // busy; release() drains the queue afterwards
  var gen = _gen;
  startP("crawl", 0);
  logLine("crawl worker started (" + _seedQueue.length + " series queued)");
  var visited = {}, bad = await loadBad();
  try {
    while (_seedQueue.length && gen === _gen) {
      var url = _seedQueue.shift(), a = asinOf(url);
      if (a && visited[a]) continue;      // same series already covered this run
      logLine("series from " + shortUrl(url) + (_seedQueue.length ? ("  (" + _seedQueue.length + " more queued)") : ""));
      var o = await browser.storage.local.get("urls"); var urls = o.urls || {}; var idx = settledIndex(urls);
      await crawlSeries(url, visited, urls, idx, false, bad, gen);
    }
    await readDiscovered(gen);
  } finally { if (gen === _gen) endP(); release(gen); }
  logLine("crawl worker finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : ""));
  try { await browser.storage.local.set({ lastCrawl: { at: Date.now(), checked: P.checked, sent: P.sent } }); } catch (e) {}
  // no self-restart here: release() already scheduled drainPending(), which picks
  // up seeds that arrived late without racing for the lock
}

// ---- release day first ----
// The daily refresh, Refresh dates and Crawl all read ~30-300 pages at ~4 s
// each. A book coming out today must not wait behind them: its pages are read
// first, so the flip from pre-order to buyable - and the "out now" ping - lands
// a minute after the job starts, not at the end of it. Same window as the hourly
// release watch: today, or up to RELEASE_WATCH_DAYS back while still not
// confirmed live (a US title on the AU store can flip late). Returns how many
// days ago the release was, or null when the page isn't due.
function releaseDue(u) {
  if (!u || u.live === true) return null;
  var dd = daysSince(u.date);
  return (dd !== null && dd >= 0 && dd <= RELEASE_WATCH_DAYS) ? dd : null;
}
function releaseDayFirst(keys, urls) {
  var due = keys.filter(function (k) { return releaseDue(urls[k]) !== null; })
                .sort(function (a, b) { return releaseDue(urls[a]) - releaseDue(urls[b]); });
  return { due: due, all: due.concat(keys.filter(function (k) { return releaseDue(urls[k]) === null; })) };
}

async function refreshKnown(force) {
  if (!force && await ranWithin("lastRefresh", REFRESH_MIN_MS)) {
    logLine("refresh skipped (already ran within the day)"); return { skipped: true };
  }
  // remember HOW it was asked for: a deferred daily refresh replayed as forced
  // would re-read every page the blocking crawl just fetched
  if (!acquire()) { _pending.refresh = force ? "force" : "auto"; logLine("refresh deferred (worker busy)"); return { busy: true }; }
  var gen = _gen;
  startP("refresh", 0);
  try {
    var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
    var idx = settledIndex(urls), bad = await loadBad();
    var skippedBad = 0, skippedToday = 0;
    var list = Object.keys(urls).filter(function (k) {
      var u = urls[k];
      if (!refreshDue(u, idx)) return false;
      // honour the same cooldowns the other paths do — this is the highest
      // frequency path, so it must not be the one that hammers a dead page
      if (u.asin && isBad(u.asin, bad)) { skippedBad++; return false; }
      // a release-day page is re-read even if something read it earlier today
      if (!force && readToday(u) && releaseDue(u) === null) { skippedToday++; return false; }
      return true;
    });
    var ord = releaseDayFirst(list, urls);
    list = ord.all;
    P.total = list.length; saveP();
    if (ord.due.length) logLine("release day: " + ord.due.length + " page(s) read first");
    logLine("refresh started: " + list.length + " due of " + Object.keys(urls).length + " known pages" +
            (skippedToday ? (", " + skippedToday + " already read today") : "") +
            (skippedBad ? (", " + skippedBad + " on the needs-a-check list") : ""));
    for (var i = 0; i < list.length && gen === _gen; i++) { await readPage(list[i], false, (urls[list[i]] || {}).series || ""); await sleep(MIN_GAP + Math.random() * JITTER); }
    await readDiscovered(gen);
  } finally { if (gen === _gen) endP(); release(gen); }
  logLine("refresh finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : ""));
  try { await browser.storage.local.set({ lastRefresh: { at: Date.now(), checked: P.checked, sent: P.sent } }); } catch (e) {}
  return { checked: P.checked, sent: P.sent, failed: P.failed };
}

// inner re-crawl (assumes lock held + startP already called)
async function _recrawlInner(force, gen) {
  gen = (gen === undefined) ? _gen : gen;
  var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
  var idx = settledIndex(urls);
  var seeds = {};
  Object.keys(urls).forEach(function (k) { var u = urls[k], s = u.series || ""; if (!seeds[s] || (u.n || 0) > (seeds[s].n || 0)) seeds[s] = { url: k, n: u.n || 0 }; });
  var visited = {}, names = Object.keys(seeds), bad = await loadBad();
  logLine((force ? "re-acquire (full): " : "re-crawl: ") + names.length + " series, " + Object.keys(urls).length + " known pages");
  // release day first (see releaseDue), before any series crawl. Marked read
  // today in this snapshot so a carousel or the safety net doesn't fetch it
  // again; a series SEED is still read, because its links drive the crawl.
  var due = releaseDayFirst(Object.keys(urls), urls).due.filter(function (k) { var x = asinOf(k); return !(x && isBad(x, bad)); });
  if (due.length) logLine("release day: " + due.length + " page(s) read first");
  for (var r = 0; r < due.length && gen === _gen; r++) {
    await readPage(due[r], true, (urls[due[r]] || {}).series || "");
    if (urls[due[r]]) urls[due[r]].lastChecked = Date.now();
    await sleep(MIN_GAP + Math.random() * JITTER);
  }
  for (var i = 0; i < names.length && gen === _gen; i++) {
    var su = seeds[names[i]].url, a = asinOf(su);
    if (a && visited[a]) continue;
    logLine("series seed [" + (i + 1) + "/" + names.length + "] " + (names[i] || "?") + "  " + shortUrl(su));
    await crawlSeries(su, visited, urls, idx, force, bad, gen);
  }
  var leftover = Object.keys(urls).filter(function (k) {
    var x = asinOf(k); if (!x || visited[x]) return false;
    if (!force && readToday(urls[k])) return false;      // already read since midnight
    return force || !pageIsSettled(urls[k], idx);
  });
  if (leftover.length) {
    logLine("safety net: " + leftover.length + " known page(s) not reached via carousels; fetching directly");
    for (var j = 0; j < leftover.length && gen === _gen; j++) { var lu = leftover[j]; await readPage(lu, force); var la = asinOf(lu); if (la) visited[la] = 1; await sleep(MIN_GAP + Math.random() * JITTER); }
  }
  await readDiscovered(gen);
}

// On and just after a book's release day, re-check that book's page every hour so
// the moment it flips from pre-order to buyable is caught promptly. Deliberately
// narrow, because this is the only thing that runs more than once a day:
//   * only pages dated today, or up to RELEASE_WATCH_DAYS past (a US publisher on
//     an AU storefront can flip hours after local midnight, and Firefox may have
//     been closed on the day itself)
//   * only until the release is CONFIRMED — a page that read as buyable and whose
//     reading the app accepted is done; nothing re-arms it
//   * with nothing outstanding it makes no network calls at all
var RELEASE_WATCH_DAYS = 2;
async function checkReleasingToday() {
  var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
  var due = Object.keys(urls).filter(function (k) {
    var u = urls[k];
    if (!u || u.live === true) return false;          // already confirmed live
    var dd = daysSince(u.date);
    return dd !== null && dd >= 0 && dd <= RELEASE_WATCH_DAYS;
  });
  if (!due.length) return;                            // nothing outstanding — stay quiet
  if (!acquire()) { _pending.release = true; logLine("release watch deferred (worker busy)"); return; }
  var gen = _gen;
  startP("release", due.length);
  try {
    logLine("release watch: " + due.length + " unconfirmed book" + (due.length === 1 ? "" : "s") + " due now or in the last " + RELEASE_WATCH_DAYS + " days");
    var bad = await loadBad();
    for (var i = 0; i < due.length && gen === _gen; i++) {
      var k = due[i], u = urls[k], a = asinOf(k);
      if (a && isBad(a, bad)) { logLine("  skip (known-bad) " + shortUrl(k)); continue; }
      await readPage(k, true, (u && u.series) || "");
      await sleep(MIN_GAP + Math.random() * JITTER);
    }
    await readDiscovered(gen);
  } finally { if (gen === _gen) endP(); release(gen); }
  logLine("release watch done: fetched " + P.checked + ", sent " + P.sent);
}

async function recrawlAll(force, auto) {
  if (auto && await ranWithin("lastCrawl", CRAWL_MIN_MS)) {
    logLine("crawl skipped (one already ran inside the crawl interval)"); return { skipped: true };
  }
  if (!acquire()) { _pending.crawl = true; logLine("re-crawl deferred (worker busy)"); return { busy: true }; }
  // `gen` was never declared here: the finally threw ReferenceError before
  // release() ran and every crawl-all left the lock held for good (the
  // three-day "deferred (worker busy)" outage of Sep 2026).
  var gen = _gen;
  startP("crawl", 0);
  try { await _recrawlInner(force, gen); } finally { if (gen === _gen) endP(); release(gen); }
  logLine("finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : "") + "  (" + Math.round((Date.now() - (P.startedAt || Date.now())) / 1000) + "s)");
  try { await browser.storage.local.set({ lastCrawl: { at: Date.now(), checked: P.checked, sent: P.sent } }); } catch (e) {}
  return { checked: P.checked, sent: P.sent, failed: P.failed };
}

// Re-crawl ONE series (the app's per-series "Check" button). Same smart pass as a
// full crawl — re-reads that series' latest known book + its "in this series" list to
// catch new/updated books, skipping ones already fully out — but scoped to a single
// series, matched by normalized name (with ASINs as a fallback), like forgetSeries.
async function crawlOneSeries(name, asins) {
  if (!acquire()) { logLine("check-one skipped (worker busy)"); return { busy: true }; }
  var gen = _gen;
  startP("crawl", 0);
  try {
    var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
    var target = norm(name || ""), asinList = asins || [];
    var seed = null, seedN = -1, matched = 0;
    Object.keys(urls).forEach(function (k) {
      var u = urls[k];
      var mine = (target && norm(u.series) === target) || (u.asin && asinList.indexOf(u.asin) >= 0);
      if (mine) { matched++; if ((u.n || 0) > seedN) { seedN = (u.n || 0); seed = k; } }
    });
    if (!seed) { logLine("check \"" + (name || "?") + "\": no pages known for it yet \u2014 open one of its Amazon pages once so I learn a URL"); return { ok: false, reason: "no-known-pages" }; }
    var idx = settledIndex(urls), bad = await loadBad(), visited = {};
    logLine("check one series: \"" + (name || "?") + "\" (" + matched + " known page(s))  seed " + shortUrl(seed));
    await crawlSeries(seed, visited, urls, idx, false, bad, gen);
    // safety net: this series' unsettled pages not reached via the seed's carousel
    var leftover = Object.keys(urls).filter(function (k) {
      var u = urls[k], mine = (target && norm(u.series) === target) || (u.asin && asinList.indexOf(u.asin) >= 0);
      if (!mine) return false; var x = asinOf(k); if (!x || visited[x]) return false; return !pageIsSettled(u, idx);
    });
    for (var j = 0; j < leftover.length && gen === _gen; j++) { await readPage(leftover[j], false, name || ""); var la = asinOf(leftover[j]); if (la) visited[la] = 1; await sleep(MIN_GAP + Math.random() * JITTER); }
    await readDiscovered(gen);
  } finally { if (gen === _gen) endP(); release(gen); }
  logLine("check finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : ""));
  return { checked: P.checked, sent: P.sent, failed: P.failed };
}

// Re-read a specific list of pages the app says are missing cover art or
// narrator info (books tracked before per-book capture existed). Settled-page
// skipping doesn't apply — the whole point is re-reading old books, once.
async function coverFill(list) {
  list = list || [];
  if (!list.length) return { ok: true, checked: 0 };
  if (!acquire()) { logLine("cover backfill skipped (worker busy, will retry on next poll)"); return { busy: true }; }
  var gen = _gen;
  startP("refresh", list.length);
  try {
    logLine("cover/narrator backfill: " + list.length + " page(s)");
    var bad = await loadBad();
    for (var i = 0; i < list.length && gen === _gen; i++) {
      var a = asinOf(list[i]);
      if (a && isBad(a, bad)) { logLine("  skip (known-bad) " + shortUrl(list[i])); continue; }
      await readPage(list[i], true, "", { backfill: true });
      await sleep(MIN_GAP + Math.random() * JITTER);
    }
    await readDiscovered(gen);
  } finally { if (gen === _gen) endP(); release(gen); }
  logLine("backfill finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : ""));
  return { checked: P.checked, sent: P.sent, failed: P.failed };
}

async function wipeAndCrawl() {
  if (!acquire()) return { busy: true };
  var gen = _gen;
  logLine("WIPE requested: clearing app series, then full re-acquire");
  try {
    try {
      var r = await fetchTimeout(RADAR_BASE + "/api/series/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }, APP_TIMEOUT);
      if (!r.ok) { logLine("wipe failed: app returned " + r.status); return { ok: false, error: "app returned " + r.status }; }
    } catch (e) { logLine("wipe failed: app not reachable"); return { ok: false, error: "Radar app not reachable" }; }
    startP("crawl", 0);
    try { await _recrawlInner(true, gen); } finally { if (gen === _gen) endP(); }
    logLine("re-acquire finished: fetched " + P.checked + ", sent " + P.sent + (P.failed ? (", failed " + P.failed) : ""));
    try { await browser.storage.local.set({ lastCrawl: { at: Date.now(), checked: P.checked, sent: P.sent } }); } catch (e) {}
    return { ok: true, wiped: true, checked: P.checked, sent: P.sent };
  } finally { release(gen); }
}

function appPing() {
  return fetchTimeout(RADAR_BASE + "/api/state", { method: "GET" }, APP_TIMEOUT)
    .then(function (r) { return r.json(); })
    .then(function (j) { return { ok: true, series: (j && j.series ? j.series.length : 0) }; })
    .catch(function () { return { ok: false }; });
}

// ---- command bridge: the app can't push to us, so we poll it for commands ----
// (crawl/refresh requested from the app's buttons; "forget" when a series is
// deleted in the app; "forgetAll" on a full app reset.)
var _polling = false;
// Commands the app keeps re-delivering because an ack POST failed must not be
// executed again — the app only drops a command once it is acked.
var _handledCmds = {};
function markHandled(id) {
  _handledCmds[id] = Date.now();
  var keys = Object.keys(_handledCmds);
  if (keys.length > 200) keys.slice(0, keys.length - 200).forEach(function (k) { delete _handledCmds[k]; });
}

async function forgetSeries(c) {
  var o = await browser.storage.local.get("urls"); var urls = o.urls || {};
  var target = norm(c.series), asins = c.asins || [], removed = 0;
  Object.keys(urls).forEach(function (k) {
    var u = urls[k];
    if ((target && norm(u.series) === target) || (u.asin && asins.indexOf(u.asin) >= 0)) { delete urls[k]; removed++; }
  });
  await browser.storage.local.set({ urls: urls });
  logLine("app deleted \"" + (c.series || "?") + "\" \u2014 forgot " + removed + " registered page(s)");
}

// ---- Royal Road: the author's own words, for the app's research ----------
// Search engines don't index them and the research tool gets a 403 from Royal
// Road, but the browser reads it fine. Announcement chapters ("Book 10 is out",
// "Audiobook update"), author's notes, and the author's replies in the chapter
// comments are where "the audiobooks land in November, then back to back" gets
// said. Collected on the app's request (command "rrnotes") and posted back.
var RR_NOTES_ANNOUNCE = /release|announce|update|audiobook|audio book|book \d+|kindle|amazon|audible|patreon|hiatus|schedule|news|out now|is live|delay/i;
var RR_NOTES_STORY = 3, RR_NOTES_ANNOUNCEMENTS = 6, RR_NOTES_COMMENT_PAGES = 2;

function RR_rrChapters(html) {
  var m = (html || "").match(/window\.chapters\s*=\s*(\[[\s\S]*?\]);/);
  try { return m ? JSON.parse(m[1]) : []; } catch (e) { return []; }
}
function RR_rrAuthor(html) {
  var m = (html || "").match(/<h4[^>]*>[\s\S]*?<a href="\/profile\/(\d+)"[^>]*>\s*([^<]+?)\s*<\/a>/);
  return m ? { id: m[1], name: m[2] } : null;
}
function RR_rrIsAnnouncement(title) { return RR_NOTES_ANNOUNCE.test(title || "") && !/^chapter \d+/i.test(title || ""); }
// which chapters to read: the newest announcement posts, plus the newest story chapters
function RR_rrPick(chapters) {
  var ann = chapters.filter(function (c) { return RR_rrIsAnnouncement(c.title); }).slice(-RR_NOTES_ANNOUNCEMENTS);
  var story = chapters.filter(function (c) { return !RR_rrIsAnnouncement(c.title); }).slice(-RR_NOTES_STORY);
  var seen = {}, out = [];
  ann.concat(story).forEach(function (c) {
    if (seen[c.id]) return; seen[c.id] = 1;
    out.push({ id: c.id, url: c.url, title: c.title, date: c.date, announcement: RR_rrIsAnnouncement(c.title) });
  });
  return out;
}
function _rrText(el) { return (el ? (el.textContent || "") : "").replace(/\s+/g, " ").trim(); }
function _rrByClass(root, needle) {
  var all = root.querySelectorAll("div"), out = [];
  for (var i = 0; i < all.length; i++) { var cn = all[i].className || ""; if (typeof cn === "string" && cn.indexOf(needle) >= 0) out.push(all[i]); }
  return out;
}
function RR_rrNotesFromChapter(doc, ch) {
  var items = [];
  if (ch.announcement) {
    var body = _rrByClass(doc, "chapter-content")[0] || _rrByClass(doc, "chapter-inner")[0];
    var t = _rrText(body); if (t) items.push({ chapter: ch.title, date: ch.date, kind: "announcement", text: t.slice(0, 1200) });
  }
  var notes = _rrByClass(doc, "author-note");
  for (var i = 0; i < notes.length; i++) {
    if (notes[i].parentElement && (notes[i].parentElement.className || "").indexOf("author-note") >= 0) continue;   // inner box of a portlet already taken
    var nt = _rrText(notes[i]); if (nt) items.push({ chapter: ch.title, date: ch.date, kind: "note", text: nt.slice(0, 800) });
  }
  return items;
}
function RR_rrRepliesFromComments(doc, authorId, ch) {
  var items = [], cms = doc.querySelectorAll(".comment");
  for (var i = 0; i < cms.length; i++) {
    var head = cms[i].querySelector(".media-heading"); if (!head) continue;
    var links = head.querySelectorAll("a"), mine = false;
    for (var j = 0; j < links.length; j++) if (links[j].getAttribute("href") === "/profile/" + authorId) mine = true;
    if (!mine) continue;
    var body = cms[i].querySelector(".comment-body"), tm = head.querySelector("time");
    var txt = _rrText(body); if (!txt) continue;
    items.push({ chapter: ch.title, date: (tm && tm.getAttribute("datetime")) || ch.date, kind: "reply", text: txt.slice(0, 600) });
  }
  return items;
}
async function rrNotes(c) {
  if (!acquire()) { logLine("Royal Road notes deferred (worker busy)"); return { busy: true }; }
  var gen = _gen, items = [], url = c.url, fetched = 0, author = null;
  startP("rrnotes", 0);
  try {
    var origin = originOf(url);
    var fr = await fetchTimeout(url, { credentials: "include" }, PAGE_TIMEOUT); var fhtml = await fr.text(); fetched++;
    var chapters = RR_rrChapters(fhtml); author = RR_rrAuthor(fhtml);
    var pick = RR_rrPick(chapters);
    logLine("Royal Road notes: " + shortUrl(url) + " - " + chapters.length + " chapters, author " + (author ? author.name : "?") + ", reading " + pick.length);
    for (var i = 0; i < pick.length && gen === _gen; i++) {
      var ch = pick[i];
      _progressAt = Date.now();
      try {
        var r = await fetchTimeout(origin + ch.url, { credentials: "include" }, PAGE_TIMEOUT); fetched++;
        var doc = new DOMParser().parseFromString(await r.text(), "text/html");
        items = items.concat(RR_rrNotesFromChapter(doc, ch));
        if (author) for (var p = 1; p <= RR_NOTES_COMMENT_PAGES && gen === _gen; p++) {
          await sleep(MIN_GAP);
          var cr = await fetchTimeout(origin + "/fiction/chapter/" + ch.id + "/commentsLegacy/" + p + "?fiction=&sorting=newest", { credentials: "include" }, PAGE_TIMEOUT); fetched++;
          var cdoc = new DOMParser().parseFromString(await cr.text(), "text/html");
          items = items.concat(RR_rrRepliesFromComments(cdoc, author.id, ch));
          if (!cdoc.querySelectorAll(".comment").length) break;
        }
      } catch (e) { logLine("Royal Road notes: " + shortUrl(ch.url) + " failed: " + String(e)); }
      await sleep(MIN_GAP + Math.random() * JITTER);
    }
  } catch (e) { logLine("Royal Road notes failed: " + String(e)); }
  finally { if (gen === _gen) endP(); release(gen); }
  try {
    await fetchTimeout(RADAR_BASE + "/api/rr-notes", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: c.sid, url: url, author: author ? author.name : null, items: items.slice(0, 40) }) }, APP_TIMEOUT);
  } catch (e) { logLine("Royal Road notes: could not reach the app: " + String(e)); }
  logLine("Royal Road notes: " + items.length + " item(s) from " + fetched + " page(s) sent to the app");
  return { ok: true, items: items.length };
}

// ---- app watchdog: the app watches this extension; this is the mirror.
// If ReleaseRadar.exe stops answering for hours, nothing else can say so -
// so the extension posts to the Discord webhook itself (cached daily from
// the app's settings while it is healthy).
var APP_QUIET_ALERT_MS = 6 * 3600 * 1000;
var _appLastOk = Date.now();
var _appAlerted = false;
var _webhookCachedAt = 0;

async function _cacheWebhook() {
  if (Date.now() - _webhookCachedAt < 20 * 3600 * 1000) return;
  try {
    var r = await fetchTimeout(RADAR_BASE + "/api/settings", { method: "GET" }, APP_TIMEOUT);
    var j = await r.json();
    if (j && j.discord_webhook) { await browser.storage.local.set({ cachedWebhook: j.discord_webhook }); _webhookCachedAt = Date.now(); }
  } catch (e) {}
}

// Posts to the cached webhook. Returns the message's edit URL (so the warning
// can later be turned into its all-clear and removed), true if posted but not
// addressable, false if nothing was posted.
async function _discordDirect(text) {
  try {
    var o = await browser.storage.local.get("cachedWebhook");
    if (!o.cachedWebhook) return false;
    var wh = o.cachedWebhook;
    var r = await fetch(wh + (wh.indexOf("?") >= 0 ? "&" : "?") + "wait=true",
                        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: text }) });
    try { var j = await r.json(); if (j && j.id) return wh.split("?")[0] + "/messages/" + j.id; } catch (e) {}
    return true;
  } catch (e) { return false; }
}
// A warning that clears must not leave two messages behind: the all-clear
// EDITS the warning, and an hour later the message is deleted (same rule as
// the app's own alerts). The deletion is remembered in storage so a browser
// restart doesn't lose it.
var ALERT_TIDY_MS = 3600 * 1000;
async function _discordTidy() {
  var o = await browser.storage.local.get("discordTidy"); var list = o.discordTidy || [];
  if (!list.length) return;
  var keep = [], now = Date.now();
  for (var i = 0; i < list.length; i++) {
    var it = list[i];
    if (it.at > now) { keep.push(it); continue; }
    var ok = false;
    try { var r = await fetch(it.url, { method: "DELETE" }); ok = r.ok || r.status === 404; } catch (e) {}
    if (!ok && (it.tries || 0) < 2) { it.tries = (it.tries || 0) + 1; keep.push(it); }
  }
  await browser.storage.local.set({ discordTidy: keep });
}
setInterval(_discordTidy, 60 * 1000);

async function _appSeen() {
  if (_appAlerted) {
    _appAlerted = false;
    logLine("app is answering again");
    var text = "✅ ReleaseRadar app is answering again.";
    var o = await browser.storage.local.get(["appAlert", "discordTidy"]);
    if (o.appAlert && o.appAlert.url) {
      try { await fetch(o.appAlert.url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: text }) }); } catch (e) {}
      await browser.storage.local.set({ appAlert: null, discordTidy: (o.discordTidy || []).concat([{ url: o.appAlert.url, at: Date.now() + ALERT_TIDY_MS }]) });
    } else await _discordDirect(text);
  }
  _appLastOk = Date.now();
  _cacheWebhook();
}

async function _appMissing() {
  if (!_appAlerted && Date.now() - _appLastOk > APP_QUIET_ALERT_MS) {
    _appAlerted = true;
    var h = Math.round((Date.now() - _appLastOk) / 3600000);
    logLine("app unreachable for " + h + "h - telling Discord directly");
    var posted = await _discordDirect("⚠️ The ReleaseRadar app hasn't answered the extension for " + h +
                                      " hours - release dates are still being read, but nothing is being recorded. " +
                                      "Is ReleaseRadar.exe running on the PC?");
    if (typeof posted === "string") { try { await browser.storage.local.set({ appAlert: { url: posted } }); } catch (e) {} }
  }
}

async function pollCommands() {
  if (_polling) return;
  _polling = true;
  try {
    var r = await fetchTimeout(RADAR_BASE + "/api/ext-commands", { method: "GET" }, APP_TIMEOUT);
    if (r.ok) _appSeen(); else _appMissing();
    var j = await r.json();
    var cmds = j && j.commands;
    if (!cmds || !cmds.length) return;
    var done = [];
    for (var i = 0; i < cmds.length; i++) {
      var c = cmds[i];
      if (_handledCmds[c.id]) { done.push(c.id); continue; }   // already ran; just re-ack
      try {
        if (c.type === "forget") { await forgetSeries(c); done.push(c.id); }
        else if (c.type === "forgetAll") { await browser.storage.local.set({ urls: {} }); logLine("app reset \u2014 forgot ALL registered pages"); done.push(c.id); }
        else if (c.type === "refresh") { if (_busy) { logLine("app asked to refresh \u2014 waiting, worker busy"); } else { logLine("app requested: refresh dates"); done.push(c.id); markHandled(c.id); refreshKnown(true); } }
        else if (c.type === "recrawl") { if (_busy) { logLine("app asked to crawl \u2014 waiting, worker busy"); } else { logLine("app requested: crawl all"); done.push(c.id); markHandled(c.id); recrawlAll(false); } }
        else if (c.type === "crawlSeries") { if (_busy) { logLine("app asked to check a series \u2014 waiting, worker busy"); } else { logLine("app requested: check series \"" + (c.series || "?") + "\""); done.push(c.id); markHandled(c.id); crawlOneSeries(c.series, c.asins); } }
        else if (c.type === "coverfill") { if (_busy) { logLine("app asked for cover backfill \u2014 waiting, worker busy"); } else { done.push(c.id); markHandled(c.id); coverFill(c.urls); } }
        else if (c.type === "rrnotes") { if (_busy) { logLine("app asked for Royal Road notes \u2014 waiting, worker busy"); } else { done.push(c.id); markHandled(c.id); rrNotes(c); } }
        else if (c.type === "buy") { done.push(c.id); markHandled(c.id); RR_buyEnqueue(c); }   // serialised in buy.js; reports back itself
        else { done.push(c.id); }   // unknown type: drop it
      } catch (e) { _appMissing(); /* leave unacked; retry next poll */ }
    }
    if (done.length) {
      try { await fetchTimeout(RADAR_BASE + "/api/ext-commands/ack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: done }) }, APP_TIMEOUT); } catch (e) {}
    }
  } catch (e) { /* app not running; ignore */ }
  finally { _polling = false; }
}

// ---- dashboard tab: open one only if there isn't already one ----
// We can read tab URLs because the extension holds the 127.0.0.1 host permission,
// so a pinned dashboard tab is detected even when Firefox keeps it idle/unloaded.
async function ensureDashboardTab(focus) {
  try {
    var tabs = await browser.tabs.query({});
    var dash = null;
    for (var i = 0; i < tabs.length; i++) {
      var u = tabs[i].url || tabs[i].pendingUrl || "";
      if (u.indexOf(RADAR_BASE) === 0) { dash = tabs[i]; break; }
    }
    if (dash) {
      logLine("dashboard tab already open" + (dash.discarded ? " (idle)" : "") + " \u2014 not opening another");
      if (focus) {
        try { await browser.tabs.update(dash.id, { active: true }); } catch (e) {}
        try { await browser.windows.update(dash.windowId, { focused: true }); } catch (e) {}
      }
      return dash;
    }
    var ping = await appPing();
    if (!ping || !ping.ok) { logLine("no dashboard tab, and the app isn't reachable \u2014 not auto-opening"); return null; }
    var t = await browser.tabs.create({ url: RADAR_BASE + "/", pinned: true, active: !!focus });
    logLine("no dashboard tab found \u2014 opened one (pinned)");
    return t;
  } catch (e) { logLine("ensureDashboardTab failed: " + e); return null; }
}

// ---- schedule ----
function ensureAlarms() {
  browser.alarms.get(A_REFRESH).then(function (x) { if (!x) browser.alarms.create(A_REFRESH, { periodInMinutes: REFRESH_MIN, delayInMinutes: 2 }); });
  browser.alarms.get(A_CRAWL).then(function (x) { if (!x) browser.alarms.create(A_CRAWL, { periodInMinutes: CRAWL_MIN, delayInMinutes: 10 }); });
  browser.alarms.get(A_RELEASE).then(function (x) { if (!x) browser.alarms.create(A_RELEASE, { periodInMinutes: RELEASE_MIN, delayInMinutes: 5 }); });
}
browser.runtime.onInstalled.addListener(function () { logLine("extension installed/updated"); ensureAlarms(); });
browser.runtime.onStartup.addListener(function () {
  ensureAlarms();
  setTimeout(function () { ensureDashboardTab(false); }, 6000);  // wait for session restore to bring back the pinned tab
  setTimeout(function () { refreshKnown(); }, 25000);   // self-gating: no-op if one already ran today
});
browser.alarms.onAlarm.addListener(function (al) {
  // scheduled runs pass no force / auto=true so they honour the interval gates —
  // Firefox recreates alarms on every browser start, which without this made a
  // restart re-trigger the whole schedule
  if (al.name === A_REFRESH) refreshKnown();
  else if (al.name === A_CRAWL) recrawlAll(false, true);
  else if (al.name === A_RELEASE) checkReleasingToday();
});

// poll the app for commands (persistent page keeps this interval alive)
setInterval(pollCommands, CMD_POLL_MS);
setTimeout(pollCommands, 5000);

// startup: load log + clear any stuck flag left by a crash/suspension
(async function init() {
  await loadLog();
  try { var o = await browser.storage.local.get("progress"); if (o.progress && o.progress.running) { P = o.progress; P.running = false; P.current = ""; saveP(); logLine("cleared a stuck run left from a previous session"); } } catch (e) {}
  ensureAlarms();
})();

// ---- messages ----
browser.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg) return;
  if (msg.type === "crawl") { enqueueCrawl(msg.url); sendResponse({ queued: true, pending: _seedQueue.length }); return false; }
  if (msg.type === "recrawl") { recrawlAll(false).then(sendResponse); return true; }   // user-triggered: no auto gate
  if (msg.type === "wipeAndCrawl") { wipeAndCrawl().then(sendResponse); return true; }
  if (msg.type === "refresh") { refreshKnown(true).then(sendResponse); return true; }  // user-triggered: force
  if (msg.type === "appPing") { appPing().then(sendResponse); return true; }
  if (msg.type === "clearList") { browser.storage.local.set({ urls: {} }).then(function () { logLine("registered pages cleared by user"); sendResponse({ ok: true }); }); return true; }
  if (msg.type === "getLog") { sendResponse({ log: _log }); return true; }
  if (msg.type === "clearLog") { _log = []; browser.storage.local.set({ log: [] }); sendResponse({ ok: true }); return true; }
  if (msg.type === "reset") {
    // bump the generation FIRST: running loops check it each iteration and bail,
    // so freeing the lock cannot leave two unpaced loops fetching in parallel
    _gen++; _seedQueue = []; _pending.refresh = _pending.release = _pending.crawl = false;
    _draining = false;                       // see lockWatchdog
    release(); endP(); logLine("manual reset (running work cancelled, queue cleared)");
    sendResponse({ ok: true }); return true;
  }
  if (msg.type === "setPaused") {
    browser.storage.local.set({ paused: !!msg.paused }).then(function () {
      logLine(msg.paused ? "auto-add of opened pages PAUSED (browsing freely)" : "auto-add of opened pages resumed");
      sendResponse({ ok: true, paused: !!msg.paused });
    });
    return true;
  }
  if (msg.type === "clearBadList") { browser.storage.local.set({ bad: {} }).then(function () { logLine("cleared the needs-a-check list"); sendResponse({ ok: true }); }); return true; }
  if (msg.type === "state") {
    browser.storage.local.get(["urls", "progress", "lastRefresh", "lastCrawl", "paused", "bad"]).then(function (o) {
      sendResponse({ urls: o.urls || {}, progress: o.progress || P, lastRefresh: o.lastRefresh || null, lastCrawl: o.lastCrawl || null, paused: !!o.paused, queued: _seedQueue.length, bad: o.bad || {} });
    });
    return true;
  }
});
