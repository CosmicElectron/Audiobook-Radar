// Loads the real background page scripts under node with a stubbed browser and
// exercises every worker-lock path end to end. Exists because recrawlAll()'s
// finally block referenced an undeclared `gen`, threw ReferenceError, and left
// the lock held for three days (Sep 2026) - a bug no static check found and no
// test could, because nothing ever executed background.js outside Firefox.
//
//   node release-radar-firefox/test/test_background.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Silence is failure: a hung check must not let node drain its event loop and
// exit 0 (pack_extension.py only looks at the exit code). Only the final
// verdict flips this back.
process.exitCode = 1;
const DEADLINE_MS = 5000;
function withDeadline(p, what) {
  return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("harness deadline: " + what + " never settled")), DEADLINE_MS))]);
}

const HERE = __dirname;
let fails = 0;
function chk(name, ok, extra) { if (ok) return; fails++; console.log("  FAIL " + name + (extra ? "  " + extra : "")); }

// ---- browser stub: any API call resolves to {}; addListener is a no-op ----
function stub() {
  const fn = function () { return Promise.resolve({}); };
  return new Proxy(fn, {
    get(t, k) {
      if (k === "then") return undefined;                       // not a thenable
      if (k === "addListener" || k === "removeListener") return function () {};
      return stub();
    },
    apply() { return Promise.resolve({}); },
  });
}
const store = {};
const browser = stub();
// storage.local must round-trip values (lastCrawl, log, progress...)
const storageLocal = {
  get(k) { const out = {}; (Array.isArray(k) ? k : [k]).forEach(x => { if (x in store) out[x] = store[x]; }); return Promise.resolve(out); },
  set(o) { Object.assign(store, o); return Promise.resolve(); },
};
const browserWithStorage = new Proxy(browser, { get(t, k) { return k === "storage" ? { local: storageLocal } : browser[k]; } });

// fetch stub: app endpoints answer ok; the body reader honours the abort signal
// (like a real fetch) so the timeout wrapper can be tested
function makeFetch(opts) {
  opts = opts || {};
  return function (url, init) {
    const signal = init && init.signal;
    const body = { ok: true, commands: [] };
    const read = () => new Promise((res, rej) => {
      if (opts.stallBody) { if (signal) signal.addEventListener("abort", () => rej(new Error("AbortError"))); return; }
      res(JSON.stringify(body));
    });
    return Promise.resolve({ ok: true, status: 200, text: read, json: () => read().then(JSON.parse) });
  };
}

const sandbox = {
  console, URL, AbortController, setTimeout, clearTimeout, Date, Math, JSON, Promise,
  setInterval: () => 0, clearInterval: () => 0,      // no timers keeping node alive
  browser: browserWithStorage, fetch: makeFetch(),
  DOMParser: function () { this.parseFromString = () => ({ querySelector: () => null, querySelectorAll: () => [] }); },
};
sandbox.self = sandbox; sandbox.window = sandbox;
vm.createContext(sandbox);
for (const f of ["extract.js", "buy.js", "background.js"]) {
  vm.runInContext(fs.readFileSync(path.join(HERE, "..", f), "utf8"), sandbox, { filename: f });
}

(async function main() {
  console.log("background.js worker-lock paths under node:");
  const paths = [
    ["recrawlAll(false)", () => sandbox.recrawlAll(false)],
    ["recrawlAll(false, true)", () => sandbox.recrawlAll(false, true)],
    ["crawlOneSeries (unknown series)", () => sandbox.crawlOneSeries("Nothing Known", [])],
    ["refreshKnown(true)", () => sandbox.refreshKnown(true)],
    ["checkReleasingToday()", () => sandbox.checkReleasingToday()],
    ["coverFill([])", () => sandbox.coverFill([])],
    ["wipeAndCrawl()", () => sandbox.wipeAndCrawl()],
  ];
  for (const [name, run] of paths) {
    let threw = null;
    try { await withDeadline(run(), name); } catch (e) { threw = e; }
    chk(name + " resolves", !threw, threw && String(threw));
    chk(name + " releases the lock", sandbox._busy === false, "_busy=" + sandbox._busy);
    sandbox._busy = false;
  }

  // lockWatchdog: a held lock with no page progress for 15 min is force-released,
  // a busy-but-progressing one is left alone
  sandbox._busy = true; sandbox._progressAt = Date.now() - 2 * 60 * 1000;
  sandbox.lockWatchdog();
  chk("watchdog leaves a progressing job alone", sandbox._busy === true);
  const gen0 = sandbox._gen;
  sandbox._progressAt = Date.now() - 16 * 60 * 1000;
  sandbox.lockWatchdog();
  chk("watchdog frees a stalled lock", sandbox._busy === false);
  chk("watchdog cancels the running loop", sandbox._gen === gen0 + 1);
  await new Promise(r => setTimeout(r, 20));           // let drainPending run
  sandbox._busy = false;

  // the watchdog must also take the drain flag off a zombie drain (one whose
  // awaited job is the stuck job), or nothing deferred ever runs again
  sandbox._draining = true;                            // as if drainPending were awaiting the hung job
  sandbox._busy = true; sandbox._progressAt = Date.now() - 16 * 60 * 1000;
  sandbox._pending.crawl = true;
  sandbox.lockWatchdog();
  await withDeadline(new Promise(r => setTimeout(r, 50)), "post-watchdog drain");
  chk("watchdog revives a zombie drain (deferred crawl ran)", sandbox._pending.crawl === false && sandbox._draining === false,
      "crawl=" + sandbox._pending.crawl + " draining=" + sandbox._draining);
  sandbox._busy = false;

  // a stale loop's finally must not clobber the job that replaced it
  const staleGen = sandbox._gen - 1;
  sandbox._busy = true;
  sandbox.release(staleGen);
  chk("release() with a stale generation is a no-op", sandbox._busy === true);
  sandbox.release(sandbox._gen);
  chk("release() with the live generation frees the lock", sandbox._busy === false);
  await new Promise(r => setTimeout(r, 20));

  // a deferred daily (unforced) refresh is replayed unforced, a forced one forced
  sandbox._busy = true;
  delete store.lastRefresh;                            // the once-a-day gate must not pre-empt the lock check
  await sandbox.refreshKnown();                        // alarm-style call while busy
  chk("deferred alarm refresh remembered as auto", sandbox._pending.refresh === "auto", String(sandbox._pending.refresh));
  await sandbox.refreshKnown(true);
  chk("deferred forced refresh remembered as force", sandbox._pending.refresh === "force", String(sandbox._pending.refresh));
  sandbox._pending.refresh = false; sandbox._busy = false;

  // ---- page-order checks run against a stubbed readPage and no pacing ----
  const origReadPage = sandbox.readPage, origSleep = sandbox.sleep, readUrls = [];
  sandbox.readPage = async (url) => { readUrls.push(url); sandbox._attempted[sandbox.asinOf(url)] = Date.now(); return { ok: true }; };
  sandbox.sleep = () => Promise.resolve();
  const P_ = "https://www.amazon.com.au/dp/";

  // discovery (v1.27): a page linked from a read that isn't in the registry is
  // read before the job lets go - once; known and just-tried pages are not
  store.urls = {};
  store.urls[P_ + "B0KINDLE04"] = { asin: "B0KINDLE04", series: "Riftside", n: 4 };
  store.urls[P_ + "B0KINDLE05"] = { asin: "B0KINDLE05", series: "Riftside", n: 5 };
  sandbox.noteDiscovered(P_ + "B0KINDLE04", { siblings: ["B0KINDLE05", "B0KINDLE04"], formats: ["B0AUDIO004"] }, "Riftside");
  await withDeadline(sandbox.readDiscovered(sandbox._gen), "readDiscovered");
  chk("discovery reads only the unknown linked page", JSON.stringify(readUrls) === JSON.stringify([P_ + "B0AUDIO004"]), JSON.stringify(readUrls));
  sandbox.noteDiscovered(P_ + "B0KINDLE04", { siblings: [], formats: ["B0AUDIO004"] }, "Riftside");
  await withDeadline(sandbox.readDiscovered(sandbox._gen), "readDiscovered again");
  chk("a just-tried page is not re-read by the next job", readUrls.length === 1, JSON.stringify(readUrls));
  for (const fn of ["refreshKnown", "checkReleasingToday", "coverFill", "crawlOneSeries", "runSeedWorker", "_recrawlInner"]) {
    chk(fn + " drains discoveries before letting go", sandbox[fn].toString().indexOf("readDiscovered(gen)") > 0);
  }

  // release day first: Refresh dates and Crawl all read today's (and not-yet-
  // confirmed recent) releases before anything else
  const iso = d => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  const day = n => { const d = new Date(); d.setDate(d.getDate() + n); return iso(d); };
  const seedLibrary = () => {
    store.urls = {};
    store.urls[P_ + "B0OLDBOOK1"] = { asin: "B0OLDBOOK1", series: "Alpha", n: 1, date: day(-400) };
    store.urls[P_ + "B0NEXTBOOK"] = { asin: "B0NEXTBOOK", series: "Alpha", n: 3, date: day(60), preorder: true };
    store.urls[P_ + "B0TODAYNOV"] = { asin: "B0TODAYNOV", series: "Beta", n: 7, date: day(0), preorder: true };
    store.urls[P_ + "B0BETANEXT"] = { asin: "B0BETANEXT", series: "Beta", n: 8, date: day(90), preorder: true };
    store.urls[P_ + "B0YESTAUD1"] = { asin: "B0YESTAUD1", series: "Gamma", n: 2, date: day(-1), isAudio: true, preorder: true, lastChecked: Date.now() };
    store.urls[P_ + "B0LIVEDONE"] = { asin: "B0LIVEDONE", series: "Delta", n: 4, date: day(0), live: true };
    readUrls.length = 0; sandbox._attempted = {};
  };
  seedLibrary();
  await withDeadline(sandbox.refreshKnown(true), "Refresh dates on release day");
  chk("Refresh dates reads today's release first", readUrls[0] === P_ + "B0TODAYNOV", JSON.stringify(readUrls));
  chk("...then yesterday's unconfirmed one", readUrls[1] === P_ + "B0YESTAUD1", JSON.stringify(readUrls));
  chk("a release already confirmed live isn't pushed forward", readUrls.indexOf(P_ + "B0LIVEDONE") > 1, JSON.stringify(readUrls));
  seedLibrary(); delete store.lastRefresh;
  await withDeadline(sandbox.refreshKnown(), "daily refresh on release day");
  chk("the daily refresh re-reads a release-day page read earlier today", readUrls.indexOf(P_ + "B0YESTAUD1") === 1, JSON.stringify(readUrls));
  seedLibrary(); delete store.lastCrawl;
  await withDeadline(sandbox.recrawlAll(false), "Crawl all on release day");
  chk("Crawl all reads today's release before any series crawl", readUrls[0] === P_ + "B0TODAYNOV" && readUrls[1] === P_ + "B0YESTAUD1", JSON.stringify(readUrls));
  chk("...and doesn't fetch it a second time", readUrls.filter(u => u === P_ + "B0TODAYNOV").length === 1, JSON.stringify(readUrls));
  sandbox.readPage = origReadPage; sandbox.sleep = origSleep; store.urls = {};

  // app-silent alert: the all-clear EDITS the warning and deletes it an hour later
  const calls = [];
  sandbox.fetch = (url, init) => { calls.push({ url, method: (init && init.method) || "GET", body: init && init.body });
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ id: "777", ok: true, commands: [] }), text: () => Promise.resolve("{}") }); };
  store.cachedWebhook = "https://discord.com/api/webhooks/1/tok";
  store.discordTidy = [];
  sandbox._appAlerted = false; sandbox._appLastOk = Date.now() - 7 * 3600 * 1000;
  await withDeadline(sandbox._appMissing(), "appMissing");
  const warn = calls.find(c => c.method === "POST");
  chk("warning posted so it can be edited later (wait=true)", !!warn && /wait=true/.test(warn.url), JSON.stringify(calls));
  chk("warning's message URL remembered", !!(store.appAlert && store.appAlert.url === "https://discord.com/api/webhooks/1/tok/messages/777"), JSON.stringify(store.appAlert));
  calls.length = 0;
  await withDeadline(sandbox._appSeen(), "appSeen");
  const edit = calls.find(c => c.method === "PATCH");
  chk("all-clear edits the warning instead of posting again", !!edit && /answering again/.test(edit.body) && !calls.some(c => c.method === "POST"), JSON.stringify(calls));
  chk("deletion scheduled about an hour out", store.discordTidy.length === 1 && store.discordTidy[0].at > Date.now() + 3500 * 1000 && !store.appAlert);
  calls.length = 0;
  await withDeadline(sandbox._discordTidy(), "tidy (not due)");
  chk("not deleted before its hour is up", !calls.some(c => c.method === "DELETE"));
  store.discordTidy[0].at = Date.now() - 1000;
  await withDeadline(sandbox._discordTidy(), "tidy (due)");
  chk("deleted once its hour is up", calls.some(c => c.method === "DELETE" && /messages\/777/.test(c.url)) && store.discordTidy.length === 0, JSON.stringify(calls));
  sandbox.fetch = makeFetch();

  // fetchTimeout: the abort must still be armed while the body is being read
  sandbox.fetch = makeFetch({ stallBody: true });
  const t0 = Date.now();
  let bodyErr = null;
  try { const r = await sandbox.fetchTimeout("https://www.amazon.com.au/dp/B0TESTTEST", {}, 60); await withDeadline(r.text(), "stalled body"); }
  catch (e) { bodyErr = e; }
  chk("stalled body is aborted by the timeout", !!bodyErr && /AbortError/.test(String(bodyErr)) && Date.now() - t0 < 2000,
      bodyErr ? String(bodyErr) : "hung/never rejected");
  sandbox.fetch = makeFetch();
  const r2 = await sandbox.fetchTimeout("https://www.amazon.com.au/dp/B0TESTTEST", {}, 1000);
  chk("healthy body reads through the wrapper", (await r2.text()).indexOf("commands") > 0);

  console.log(fails ? "\n" + fails + " FAILURE(S)" : "\nall background-lock checks correct");
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log("harness crashed: " + (e && e.stack || e)); process.exit(1); });
