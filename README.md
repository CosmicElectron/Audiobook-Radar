# Release Radar

A small, local desktop app that tracks **book and audiobook release dates** for the
series you follow — with a dashboard, an RSS feed, predicted release windows, and
optional Discord pings. It runs on your own machine; there's no cloud and no account.

> **Heads up — this was "vibe coded."** It was built conversationally with Claude
> (Anthropic's AI assistant) across many iterative sessions, by someone who isn't a
> professional developer. It works and gets used daily, but expect the occasional
> rough edge, and the code carries some lived-in bits from all that iteration.
> Shared in case it's useful to anyone who wants the same kind of tool. PRs welcome;
> no promises about polish.

## What it does

- Tracks the series you follow and shows, per series, the next novel and audiobook
  dates on a dashboard at `http://127.0.0.1:8765/`.
- **Predicts** a likely next audiobook (and next novel) date from the historical gap
  between past releases, with a confidence badge, until a real date is announced.
- Publishes an **RSS feed** of your upcoming releases (`/feed.xml`) and a clean
  **"Up next" board** (`/board`) you can use as a browser new-tab page.
- Optional **Discord notifications**: a ping when a new or changed date arrives, and
  a ping on release day.
- Lives in the **system tray** — no console window needed for the built `.exe`.

It grew up around long-running LitRPG / light-novel series, but works for any book series.

## Everything stays local

No accounts, no telemetry, no cloud. Your data is a plain JSON file on your disk. The
only outbound traffic is the app/extension reading the product pages you ask it to,
and — if you turn it on — posting to your own Discord webhook.

## Quick start (no building)

1. Install **Python 3.9+** from https://www.python.org/downloads/ (tick *"Add Python
   to PATH"* on Windows).
2. Double-click **`run.bat`** (Windows), or run `python app.py` on any OS.

It installs a few small libraries the first time, then serves `http://127.0.0.1:8765/`.

## Build a standalone .exe (Windows)

Prefer a single double-clickable program with no visible Python?

1. Double-click **`build.bat`**.
2. When it finishes, your program is at **`dist\ReleaseRadar.exe`**.

(The `.exe` must be built on Windows, which is why this ships as source + a build
script rather than a prebuilt binary.)

## How release dates get in

Amazon and Audible are the source of truth, but they block plain server-side page
reads — so the reliable path is a small browser helper that reads pages using *your
own logged-in browser session*.

1. **Firefox extension (recommended — hands-off)** — in `release-radar-firefox/`. When
   you open an Amazon/Audible book or audiobook page it reads the date and sends it to
   the app; and on a background schedule (with no tabs open) it re-checks every page
   you've visited once, so pre-orders and newly-announced audiobooks update themselves.
   Full install steps are in `release-radar-firefox/INSTALL.txt`. Works on Firefox and
   forks (LibreWolf, Waterfox, Mullvad, …).

2. **Tampermonkey userscript (simpler, live-only)** — `release-radar.user.js`. If you'd
   rather not install an extension, this reads the page when you open or refresh it and
   sends the date. It does **not** run in the background. Install via
   Tampermonkey/Violentmonkey.

3. **Add by hand** — in the dashboard, add a series manually and paste an Amazon/Audible
   link, then hit **Auto-fill**. The app fetches and parses that page to pre-fill what
   it can (title, cover, date, ebook vs audiobook); anything it misses, you type.

However the data arrives, dates are matched to the right book **by its number** in the
series, so your manual edits and other books are never clobbered. Every field —
including each row of the release timeline — stays editable, and edited entries are
protected from being overwritten on later updates.

## Discord notifications (optional)

In **Settings → Discord notifications**, paste a channel **webhook URL** (in Discord:
channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL) and hit
**Send test**. Then choose:

- a ping when a new or changed release date arrives, and/or
- a ping on release day, after an hour you set (default 3 PM, local time).

Backfilled past dates and re-reads of an already-known date stay silent, so it doesn't
spam you.

## Accessing it remotely — please read

By default the app listens only on `127.0.0.1` (your machine). **It has no login of its
own** — it was built to be reachable only by you, locally. If you put it on the internet
(via a Cloudflare Tunnel, a reverse proxy, or a port-forward), anyone who finds the URL
can not just view it but control it: delete series, change settings, overwrite your
Discord webhook, or quit the app.

So if you expose it, **put authentication in front of it** — for example Cloudflare
Access (Zero Trust), which shows a login before the app is ever reachable. Two
environment variables help:

- `RADAR_PORT` — change the port (default `8765`).
- `RADAR_HOST` — set to `0.0.0.0` only if a proxy on a *different* machine must reach it
  over your LAN. This exposes the unauthenticated panel on your network, so pair it with
  auth.

## Where your data lives

- Windows: `%APPDATA%\ReleaseRadar\data.json` (with `radar.log` beside it).
- Other OSes: `~/.release-radar/`.

It's plain JSON — back it up or hand-edit it freely. A `data.json.bak` is written before
any destructive operation.

## Quitting

- **Tray icon** — right-click the Release Radar icon near the clock → **Quit**
  (left-click opens the dashboard). Easiest for the built `.exe`.
- **Quit button** in the dashboard toolbar.
- Close the console window (only the `python app.py` / `run.bat` way has one).

## Browser new-tab view

Point a custom-new-tab extension at `http://127.0.0.1:8765/board` for a styled "Up next"
page — or, if your extension takes pasted HTML, use the included `newtab.html`. The app
has to be running either way.

## Honest caveats

This was vibe-coded with an AI, in an environment that **couldn't reach Amazon or
Discord, or run a real Firefox / Windows tray** — so those live paths were written
against saved sample pages and reasoned through, rather than exercised end-to-end in
testing. They've since been used in real life, but if something misbehaves on first run,
the cause is logged in `radar.log`, and it's usually a small fix. If Amazon changes their
page layout, the bit to update is the selectors in `release-radar-firefox/extract.js`.

There are also a couple of inert leftovers in the data model from an earlier version
(e.g. a `hardcover_token` setting) that nothing uses — harmless, just not cleaned up yet.

It's a personal tool — be reasonable about how often you check, so you're a good citizen
of the sites you read from.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.
