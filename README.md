# Release Radar

A small desktop program that tracks novel and audiobook releases for the book
series you follow. It runs locally, shows a dashboard in your browser, predicts
when an unannounced audiobook is likely to land, and publishes an RSS feed and a
calendar feed of what's coming.

There is **no cloud, no account, nothing leaves your machine** — the app itself
makes no outbound calls except to Discord, if you set up a webhook.

---

## How it gets its data

Everything comes from **the actual Amazon product pages**, read by the
included Firefox extension through your own logged-in browser session. There is
no external book database, no API key, and nothing to sign up for.

1. Run the app (below).
2. Install the extension (`release-radar-firefox/`, see INSTALL.txt).
3. Open one Amazon page for a series you follow.

That's the whole setup. The extension reads the page, sends the series name, book
number, date and format to the app, then crawls the rest of the series by itself.
From then on it keeps everything current in the background with no tabs open.

Anything it gets wrong you can fix by hand — click **Edit** on a card and every
field is editable, including each row of the release timeline. Your edits are
kept: later reads fill in blanks and add new books, but never overwrite a value
you typed.

## Quick start

1. Install Python 3.9+ from https://www.python.org/downloads/ (tick *"Add
   Python to PATH"*).
2. Double-click **run.bat**.

It installs the dependencies the first time, then serves
`http://127.0.0.1:8765/`. Close the console window to stop it.

### Build a standalone .exe

Double-click **build.bat**; the result is **dist\ReleaseRadar.exe**, a single
double-clickable program with no console window that sits in the system tray.
(It has to be built on Windows, which is why this ships as source plus a build
script rather than a prebuilt binary.)

## Quitting

- **System-tray icon** — left-click opens the dashboard, right-click → **Quit**.
  This is the one to use for the built exe, since it has no console window.
- **Quit** button in the dashboard toolbar.
- Close the console window (only the `run.bat` / `python app.py` way has one).

## The dashboard

**Next releases** — the three soonest, with a countdown. `⇄ Show novels`
switches between audiobooks and novels; **Estimates** includes or hides
predicted dates in that list.

**Overdue audiobooks** — top right. Any audiobook whose expected window has
passed with no announcement, with a day counter that keeps climbing, newest
slip first.

**Tracked series** — one card each, showing the cover, the tracked novel and
audiobook (each labelled with its book number), the average novel→audiobook gap,
the narrator, a status pill, and the next prediction. **Drag** cards by the grip
to reorder, or use the **Sort** dropdown (next release / title / status).

Each card has a **release timeline**: a violet dot per novel, amber per
audiobook, split violet/amber when both land the same day, hollow while still
upcoming, and a dashed amber ring for a predicted date. Hovering any dot shows
that book's cover art and details. The colour key sits above the list.

**Refresh dates** and **Crawl all** ask the extension to go and re-read pages
now; **Check** on a card does the same for one series. They need the extension
installed and the browser open.

## How the prediction works

A confirmed date always wins; a prediction only appears where nothing has been
announced. Audiobooks are forecast as a **queue**: each undated book is
projected as the later of two constraints — it can't arrive sooner after its own
novel than the series' usual lag, and it can't jump ahead of the previous
audiobook, spaced by the series' own audio cadence. Each projection anchors the
next, so a backlog forecasts as a queue draining at the real pace.

The averages are recency-weighted with freak outliers ignored (the confidence
chip's tooltip says when one was). If a series releases day-and-date, a
confirmed novel date beats the queue rather than being pushed past its own book.
And when a book misses its window entirely, it's reported as overdue rather than
being given a fictional new date — nothing behind it can be predicted until it
lands, so nothing is.

## Discord notifications (optional)

In **Settings**, paste a channel webhook URL (Discord: channel → Edit Channel →
Integrations → Webhooks → New Webhook → Copy URL). Then choose any of:

- a ping when a new or changed release date arrives,
- a ping when a release goes live (the store page flips from pre-order to
  buyable),
- a recurring forecast digest at an hour you pick.

Messages arrive with cover art, and a same-day novel+audiobook pair is reported
as one entry. A narrator change gets its own ping. All the wording lives in
plain text files under `digest_lines\` next to your data — edit or replace any
of it; changes apply on the next message with no restart.

## Feeds

- **RSS** — `/feed.xml`, your next three releases, for any feed reader.
- **Calendar** — `/calendar.ics`, upcoming **confirmed** releases as all-day
  events (estimates deliberately stay out of a calendar, where an entry reads as
  a commitment). Subscribe by URL from Google Calendar, Outlook, or your phone.
  Note that Google fetches server-side, so it needs a publicly reachable URL —
  not `127.0.0.1` — and the path must not sit behind a login.
- **New tab** — `/board`, a clean "Up next" page for a new-tab extension that
  takes a URL. If yours takes pasted HTML instead, use the included
  `newtab.html`.

The app has to be running for any of these to serve.

## Your data

- `%APPDATA%\ReleaseRadar\data.json` — plain JSON you can edit directly.
- `%APPDATA%\ReleaseRadar\radar.log` — activity log.
- `%APPDATA%\ReleaseRadar\backups\` — a snapshot per day, newest 14 kept, taken
  before the first write of each day. A `.bak` is also written before bulk
  operations such as **Clear all tracked series**.

## Reading pages automatically

### Firefox extension (recommended) — `release-radar-firefox/`

For Firefox and its forks (LibreWolf, Waterfox, Mullvad). It does two things:

- **When you open** an Amazon book page it reads the release date, sends
  it, registers the page, and crawls that whole series in the background.
- **On a schedule, with no tabs open**, it re-reads pages through your own
  logged-in session: once a day for every book that hasn't fully released, and
  never again once a book's novel and audiobook are both out. The only thing
  that runs more often is the release-day watcher — on the day a book is due it
  re-reads that one page hourly until the page reads as buyable, then stops.
  With nothing due today it makes no requests at all.

Install steps are in `release-radar-firefox/INSTALL.txt`. Forks generally allow
the unsigned `.xpi`; otherwise load it temporarily from `about:debugging`. After
editing the extension, repackage it with `python pack_extension.py`.

Because it uses your own session at a low frequency, background reads look like
normal browsing rather than scraping. If Amazon returns a sign-in or robot page
to a background fetch, that page is skipped for that round — open it in a tab
once and it recovers. Pages that repeatedly fail are listed in the extension's
popup so you can open them yourself.

### Tampermonkey userscript — `release-radar.user.js`

If you'd rather not install an extension, this does the live-page half only:
open or refresh a book page and it sends the date. It does **not** run in the
background, so nothing updates unless you visit a page.

### How it lands

The page supplies the series, the book number ("Book 7 of 9"), the date, and
whether it's the ebook or the audiobook, so there is no fuzzy matching. The app
files that date against that book number; Amazon is authoritative for the field
it reports, and your own edits and the other books are untouched.

---

This is a personal tool that reads pages through your own browser session. Be
reasonable with how often you make it check, so you stay a good citizen of the
sites it reads.
