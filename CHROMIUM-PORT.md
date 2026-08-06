# Porting the extension to Chromium (Chrome / Edge / Brave / Opera)

The Firefox extension in `release-radar-firefox/` is Manifest V2 with a
**persistent background page**. Chromium requires **Manifest V3**, where the
background is a **service worker that gets terminated when idle**. That one
difference is the whole job — everything else is a rename.

This document is written to be followed by a person or handed to an AI as a
spec. Read the "Why this is not a 15-minute port" section before starting; the
failure mode if you skip it is an extension that appears to work and silently
stops halfway through every crawl.

---

## What the extension actually does

Three background jobs, all serialised behind a single worker lock so Amazon
never sees parallel bursts:

| Job | Trigger | Work |
| --- | --- | --- |
| Crawl a series | You open an Amazon book page | Walk that series' carousel, fetch each book page |
| Daily refresh | `chrome.alarms`, every 24h | Re-read every page whose book hasn't fully released |
| Release watch | `chrome.alarms`, hourly | Re-read only books due now, until confirmed buyable |

Plus a 20-second poll of the local app (`http://127.0.0.1:8765/api/ext-commands`)
for user-triggered commands, and a POST of every parsed page to `/api/ingest`.

Pages are fetched with `credentials: "include"` so Amazon returns the real page
for your logged-in session, paced at `MIN_GAP + random*JITTER` (2–4 s).

---

## Why this is not a 15-minute port

**A full crawl of a 20-series library is 13–17 minutes of paced fetches.** An MV3
service worker is killed after ~30 seconds without an event. Critically,
`setTimeout`/`await sleep(...)` does **not** keep a service worker alive — this is
the single most common way MV3 ports break. Network requests and extension API
calls reset the idle timer, but a bare 3-second sleep between fetches is a window
in which the worker can be torn down.

When the worker dies mid-job, every one of these in-memory globals is silently
lost, because a restarted worker re-runs the top of the file with fresh values:

| Global | What breaks when it resets |
| --- | --- |
| `_busy` | The lock is released without the loop stopping — two crawls can run in parallel, unpaced. Amazon starts returning bot-check pages. |
| `_seedQueue` | Queued series are dropped. The content script already told the user "queued". |
| `_pending` | Deferred refresh/release work never resumes. |
| `_gen` | The "Reset if stuck" cancellation signal is lost. |
| `_log` | The exportable log is truncated (it is mirrored to storage, but the in-memory copy is the source of truth). |
| `P` | The popup's progress bar freezes showing a run that is no longer happening. |
| `_handledCmds` | A command whose ack POST failed is executed a second time. |
| `_polling` | Re-entrant command polling. |

So the port is: **move all coordination state into `chrome.storage.local`, and
restructure the paced loops so any single step is resumable.**

---

## Step 1 — the manifest

```jsonc
{
  "manifest_version": 3,
  "name": "Release Radar — Amazon reader",
  "version": "1.8.0",                     // MV3 rejects some MV2 version strings
  "description": "…",

  // MV2 "permissions" mixed API and host permissions. MV3 splits them.
  "permissions": ["alarms", "storage", "clipboardWrite"],
  "host_permissions": [
    "https://www.amazon.com.au/*",
    "https://www.amazon.com/*",
    "https://www.amazon.co.uk/*",
    "http://127.0.0.1/*"
  ],

  // MV2: {"scripts": [...], "persistent": true}
  "background": { "service_worker": "sw.js", "type": "module" },

  // MV2 "browser_action" -> MV3 "action"
  "action": {
    "default_title": "Release Radar",
    "default_popup": "popup.html",
    "default_icon": { "16": "icons/icon-16.png", "32": "icons/icon-32.png" }
  },

  "icons": { "48": "icons/icon-48.png", "96": "icons/icon-96.png", "128": "icons/icon-128.png" },
  "content_scripts": [{
    "matches": ["https://www.amazon.com.au/*", "https://www.amazon.com/*", "https://www.amazon.co.uk/*"],
    "js": ["extract.js", "content.js"],
    "run_at": "document_idle"
  }]
  // drop "browser_specific_settings" — that is Gecko-only
}
```

A service worker cannot use `"scripts": [a, b]`. Either make `sw.js` an ES module
that does `import "./extract.js"` (and add `export` statements to `extract.js`),
or concatenate the two files at package time. `pack_extension.py` is the obvious
place to do the concatenation if you prefer to keep `extract.js` shared verbatim
between the content script and the worker.

## Step 2 — the API namespace

Chromium exposes `chrome.*`; Firefox exposes `browser.*` (promise-based) and also
`chrome.*` (callback-based). The whole codebase uses `browser.*` with promises,
so the cheapest correct move is a one-line shim at the top of the worker and the
popup, keeping the source identical for both browsers:

```js
// Chromium's chrome.* returns promises for these APIs in MV3.
if (typeof browser === "undefined") { var browser = chrome; }
```

Verify the specific APIs used here return promises in your target Chromium
version rather than assuming it — `chrome.storage`, `chrome.alarms`,
`chrome.tabs` and `chrome.runtime.sendMessage` do in current Chrome, but confirm
before relying on it. If any does not, wrap it rather than rewriting call sites.

`browser.windows.update` and `browser.tabs.*` behave the same; no change beyond
the namespace.

## Step 3 — make the coordination state survive a restart

Replace the module-level globals with a single storage-backed record, read at
the start of each step and written at the end:

```js
// { busy: false, owner: null, heartbeat: 0, gen: 0,
//   seedQueue: [], pending: {refresh,release,crawl}, handledCmds: {} }
async function getRun()      { return (await browser.storage.local.get("run")).run || DEFAULT_RUN; }
async function setRun(patch) { const run = await getRun(); await browser.storage.local.set({ run: { ...run, ...patch } }); }
```

The lock needs a **heartbeat and a stale-lock takeover**, because a worker that
dies holding the lock can no longer release it:

```js
const LOCK_STALE_MS = 90_000;
async function acquire(owner) {
  const run = await getRun();
  if (run.busy && Date.now() - run.heartbeat < LOCK_STALE_MS) return false;  // genuinely held
  await setRun({ busy: true, owner, heartbeat: Date.now(), gen: run.gen });
  return true;
}
```
Every loop iteration must refresh `heartbeat` (this doubles as the progress
write the popup already reads) and re-check `gen` so "Reset if stuck" still
cancels an in-flight run.

## Step 4 — make the paced loops resumable

Do **not** port `for (…) { await readPage(); await sleep(3000); }` as-is.

Two workable designs:

**A. Alarm-driven work queue (recommended — survives termination by design).**
Persist the pending URL list, fetch **one** page per wake-up, then schedule the
next. `chrome.alarms` has a minimum period of about 30 seconds, which is slower
than the current 2–4 s pacing but is strictly kinder to Amazon and needs no
keep-alive hack. A 40-page refresh becomes ~20 minutes of wall clock, entirely
in the background, and it resumes correctly after a browser restart.

```js
async function step() {
  const q = (await browser.storage.local.get("queue")).queue || [];
  if (!q.length) return endRun();
  const [url, ...rest] = q;
  await browser.storage.local.set({ queue: rest });
  await readPage(url);                 // the existing function, unchanged
  await browser.alarms.create("rr-step", { delayInMinutes: 0.5 });
}
browser.alarms.onAlarm.addListener(a => { if (a.name === "rr-step") step(); });
```

**B. Keep the worker alive for the duration.** Hold an open port or issue a
`chrome.storage` call every ~20 s so the idle timer keeps resetting, and keep the
existing tight loop. This preserves the 2–4 s pacing but relies on
keep-alive behaviour that has changed repeatedly across Chrome versions, and it
still dies on browser restart. Only pick this if the faster pacing matters, and
re-test it on each major Chrome release.

Whichever you choose, the app-side contract is unchanged: one POST to
`/api/ingest` per page read, and the same `_url` / `_asin` / `_preorder` /
`cover` / `narrator` fields (see `extract.js`). The server does not care how the
page was scheduled.

## Step 5 — check the cookie behaviour

The extension's value depends on `fetch(url, { credentials: "include" })`
returning your **logged-in** Amazon page. Verify this from an MV3 service worker
in your target browser before assuming parity — extension-initiated requests have
different cookie and initiator semantics under MV3, and third-party cookie policy
has been changing. If the fetched HTML lacks the `rpi-attribute` blocks (the
release date fields), you are getting a logged-out or bot-check page and the port
is not working, however green the logs look.

---

## Verification checklist

The MV3 failure modes are silent, so check behaviour, not absence of errors.

- [ ] Open one Amazon series page; confirm the whole series is crawled and the
      app receives every book. **Watch `chrome://serviceworker-internals` or the
      extension's console for the worker being torn down mid-crawl.**
- [ ] Start a crawl, then leave the browser untouched for 5 minutes. Does it
      finish, or stop silently partway?
- [ ] Restart the browser mid-crawl. Does it resume, or is the queue lost?
- [ ] Confirm a restart does **not** trigger a fresh full refresh and full crawl
      (`ranWithin` / `lastRefresh` / `lastCrawl` gates must still apply).
- [ ] Kill the worker while it holds the lock, then trigger a job: the stale-lock
      takeover should let the new job start, and exactly one loop should run.
- [ ] Verify two crawls never run in parallel (grep the log for interleaved
      series names; the pacing depends on it).
- [ ] Confirm the popup's progress bar reflects reality after a worker restart
      rather than freezing on a dead run.
- [ ] Check `/api/ingest` receives `_preorder` as `true`/`false` (not `null`) on
      both a Kindle page and an audiobook page — a null there means release
      confirmation is broken, which is invisible until a release day.

## Things that need no change

`extract.js` (pure DOM parsing), `content.js`, `popup.html`/`popup.js`, the
icons, and the entire app side. The parsing and the ingest contract are
browser-agnostic.

## Scope

There is deliberately no userscript fallback. One existed and was removed: it
could only read a page you had open, so it delivered none of the background
refresh, release-day watching or series crawling that the app is built around,
and it silently drifted behind the ingest contract every time the extension
gained a field. A half-working second client is worse than none.

If you only want the "read the page I am looking at" half on Chromium, that is
about forty lines against `/api/ingest` — but read `extract.js` for the payload
the server expects, and send `_url`, `_asin`, `_preorder` and `cover` as well as
the dates, or the app loses store links, cover art and release confirmation.
