# Release Radar

A small desktop program that tracks novel and audiobook releases for the book
series you follow. It runs locally, shows a dashboard in your browser, predicts
when an unannounced audiobook is likely to land, and publishes an RSS feed and a
calendar feed of what's coming.

There is **no cloud and no account**. Your library, dates and settings live in
a JSON file on your machine and are never uploaded. The app reaches the network
only where you point it: cover images, your Discord webhook/bot if you set one
up, your own Audiobookshelf server, and an AI provider only if you paste a key.
Everything else — reading Amazon pages — happens in your own browser, through
the extension.

**Since 1.3.0** it can also do things, all opt-in and off by default:

- **Auto-buy** the audiobook of a Kindle Unlimited book on release day — borrow
  the Kindle edition on KU, buy the audiobook at the Whispersync companion
  price, return the borrow. Per-series *off / ask-me-first / automatic under a
  price cap*, a master switch, a dry-run mode, an approval link on Discord/
  email, a never-buy-twice ledger, and hard stops on any page surprise.
- **Discord both ways**: webhook pings with a live-updating progress bar for
  downloads, plus an optional **bot** (`!status`, `!buy`, `!approve`, `!download`,
  `!research`, `!help`) so you can drive it from your phone.
- **Library tooling**: `file_audiobooks.py` files freshly downloaded books into
  your existing author/series folders (fuzzy-matched, never overwrites, holds
  anything ambiguous and can be *taught* corrections); `tidy_library.py`
  plans/applies/undoes a one-off cleanup of an existing library.
- **Audiobookshelf**: shows what you're currently listening to on each series
  card, triggers an ABS library scan when new books are filed, and the
  extension overlays release info inside the ABS web UI.
- **AI series research** (bring your own key — OpenRouter/Anthropic/OpenAI/Grok):
  when a series looks stalled, ask an AI with web search what's actually known
  about the next book, shown on the card.
- **Self-watching**: the app and extension watch each other and post to Discord
  if either goes quiet, and releases that pass without ever being seen buyable
  get flagged instead of slipping by silently.

---

## How it gets its data

Everything comes from **the actual Amazon product pages**, read by the
included Firefox extension through your own logged-in browser session. There is
no external book database, no API key, and nothing to sign up for.

1. Run the app — download it from
   [Releases](https://github.com/CosmicElectron/Audiobook-Radar/releases/latest).
2. Install the extension (the `.xpi` from the same release; details in
   `release-radar-firefox/INSTALL.txt`).
3. Open one Amazon page for a series you follow.

That's the whole setup. The extension reads the page, sends the series name, book
number, date and format to the app, then crawls the rest of the series by itself.
From then on it keeps everything current in the background with no tabs open.

Anything it gets wrong you can fix by hand — click **Edit** on a card and every
field is editable, including each row of the release timeline. Your edits are
kept: later reads fill in blanks and add new books, but never overwrite a value
you typed.

## Download

Grab the latest build from the
**[Releases page](https://github.com/CosmicElectron/Audiobook-Radar/releases/latest)**.
Each release ships two files:

| File | What it is |
| --- | --- |
| `ReleaseRadar.exe` | The app itself. No installer, no Python needed — double-click it and it runs from the system tray. |
| `release-radar-firefox.xpi` | The Firefox extension that reads the Amazon pages. Install it via `about:addons` → gear icon → **Install Add-on From File**. |

Already have it? Install the new `.xpi` straight over the top — your registered
pages and settings are kept. Don't remove the old one first, or the extension
forgets which pages it was watching.

## Running from source instead

If you'd rather not use the prebuilt exe:

1. Install Python 3.9+ from https://www.python.org/downloads/ (tick *"Add
   Python to PATH"*).
2. Double-click **run.bat**.

Building your own exe: **build.bat** (note the dashboard now lives in
`static/` and is bundled with `--add-data "static;static"`).

It installs the dependencies the first time, then serves
`http://127.0.0.1:8765/`. Close the console window to stop it.

To build your own exe, double-click **build.bat**; the result is
**dist\ReleaseRadar.exe**. It has to be built on Windows, which is why the
repository holds source plus a build script and the binaries live on the
Releases page.

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
  that runs more often is the release-day watcher — from the day a book is due
  until 2 days after, it re-reads that one page hourly until the page reads as
  buyable, then stops for good. With nothing outstanding it makes no requests
  at all.

Download the `.xpi` from
[Releases](https://github.com/CosmicElectron/Audiobook-Radar/releases/latest)
and install it via `about:addons` → gear → **Install Add-on From File**. Full
steps are in `release-radar-firefox/INSTALL.txt`. Forks generally allow the
unsigned `.xpi`; otherwise load it temporarily from `about:debugging`. If you
edit the extension source yourself, repackage it with
`python pack_extension.py`.

Because it uses your own session at a low frequency, background reads look like
normal browsing rather than scraping. If Amazon returns a sign-in or robot page
to a background fetch, that page is skipped for that round — open it in a tab
once and it recovers. Pages that repeatedly fail are listed in the extension's
popup so you can open them yourself.

### Other browsers

Only Firefox is supported. Porting the extension to Chromium (Chrome, Edge,
Brave, Opera) is a real piece of work rather than a rename, because Manifest V3
replaces the persistent background page with a service worker that gets shut
down while idle — and a full crawl takes 13–17 minutes of paced fetches.
**[CHROMIUM-PORT.md](CHROMIUM-PORT.md)** documents exactly what has to change,
in enough detail to follow by hand or hand to an AI, including the failure modes
that look like success.

### How it lands

The page supplies the series, the book number ("Book 7 of 9"), the date, and
whether it's the ebook or the audiobook, so there is no fuzzy matching. The app
files that date against that book number; Amazon is authoritative for the field
it reports, and your own edits and the other books are untouched.

---

This is a personal tool that reads pages through your own browser session. Be
reasonable with how often you make it check, so you stay a good citizen of the
sites it reads.
