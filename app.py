# === GENERATED FILE - DO NOT EDIT ===
# app.py is composed from the parts in src/ by compose_app.py.
# Edit the src/ part, then run:  python compose_app.py
"""
Release Radar - a local audiobook release tracker.

Runs a small local web server, opens a dashboard in your browser, checks your
tracked series once a day for new audiobook announcements/releases, predicts a
likely audiobook window from the historical novel->audiobook gap, and publishes
an RSS feed of your next three releases.

Run it:        python app.py
Build an exe:  see build.bat (produces dist/ReleaseRadar.exe)

The dashboard lives at:   http://127.0.0.1:8765/
The RSS feed lives at:     http://127.0.0.1:8765/feed.xml
"""

import os
import re
import sys
import json
import time
import uuid
import random
import html
import statistics
import threading
import base64
import subprocess
import sqlite3
import smtplib
import hashlib
import secrets
from email.message import EmailMessage
import logging
import webbrowser
from datetime import datetime, date, timedelta
from email.utils import format_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from http.client import InvalidURL

from flask import Flask, request, jsonify, Response

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:  # pragma: no cover - bs4 is in requirements, but degrade anyway
    HAVE_BS4 = False

APP_NAME = "Release Radar"
DEFAULT_PORT = 8765
# Bind address. Default 127.0.0.1 (localhost only — safest). Set RADAR_HOST=0.0.0.0
# only if a Cloudflare tunnel / reverse proxy on a DIFFERENT machine must reach this
# app over the LAN. Doing so exposes an unauthenticated control panel on your
# network, so pair it with auth in front (e.g. Cloudflare Access).
HOST = os.environ.get("RADAR_HOST", "127.0.0.1")
FEED_ITEM_COUNT = 3            # "next three releases", per the brief
BOARD_ITEM_COUNT = 6          # how many to show on the new-tab board view
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Paths / storage
# ---------------------------------------------------------------------------

def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ReleaseRadar")
    else:
        d = os.path.join(os.path.expanduser("~"), ".release-radar")
    os.makedirs(d, exist_ok=True)
    return d

DATA_FILE = os.path.join(data_dir(), "data.json")
LOG_FILE = os.path.join(data_dir(), "radar.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("radar")

_lock = threading.RLock()
_state = {"version": 1, "last_check": None, "series": [], "log": [], "settings": {}}


def default_state():
    return {"version": 1, "last_check": None, "series": [], "log": [], "ext_commands": [], "cmd_seq": 0,
            "settings": {}}


def load_state():
    global _state
    with _lock:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    _state = json.load(f)
                _state.setdefault("series", [])
                _state.setdefault("log", [])
                _state.setdefault("last_check", None)
                _state.setdefault("ext_commands", [])
                _state.setdefault("cmd_seq", 0)
                _state.setdefault("settings", {})
                _state.setdefault("buy_jobs", [])
                _state.setdefault("buy_ledger", {})
                _clean_stored_narrators()
            except Exception as e:
                log.exception("Could not read data file: %s", e)
                _state = default_state()
        else:
            _state = default_state()
            save_state()


def get_settings():
    with _lock:
        s = _state.setdefault("settings", {})
        s.setdefault("show_estimates", True)          # audiobook estimates
        s.setdefault("show_estimates_novel", True)    # novel estimates
        s.setdefault("upcoming_mode", "audiobook")    # which kind the schedule shows
        s.setdefault("discord_webhook", "")           # Discord channel webhook URL
        s.setdefault("discord_on_new", True)          # ping when a new/changed date arrives
        s.setdefault("discord_on_release", True)          # ping when a release goes live
        s.setdefault("discord_daily", False)              # daily forecast digest
        s.setdefault("discord_daily_hour", 9)             # 24h local hour to send it
        s.setdefault("discord_digest_days", 1)            # 1=daily, 2/3, 7=weekly
        # auto-buy (KU borrow -> companion audiobook -> return borrow)
        s.setdefault("buy_master", False)        # master switch: nothing buys while off
        s.setdefault("buy_dry_run", True)        # walk the page, click nothing, report
        s.setdefault("buy_cap_default", BUY_DEFAULT_CAP)
        s.setdefault("buy_weekly_max", BUY_WEEKLY_MAX)
        s.setdefault("buy_email_to", "")         # where approval requests go
        s.setdefault("buy_smtp_user", "")        # Gmail address that sends them
        s.setdefault("buy_smtp_pass", "")        # its App Password (not the login password)
        s.setdefault("buy_smtp_host", "smtp.gmail.com")
        s.setdefault("buy_public_base", "")      # https://your-tunnel-host — for approve links on the phone
        s.setdefault("buy_bat", "")              # Update Audiobooks.bat (auto-found when blank)
        s.setdefault("buy_libation_files", "")   # Libation files dir (auto-found when blank)
        s.setdefault("buy_tab_mode", "foreground")  # foreground | window (unfocused) | background
        # integrations (secrets are write-only: stored here, never echoed back)
        s.setdefault("bot_token", "")            # Discord bot - two-way control
        s.setdefault("ai_provider", "openrouter")  # openrouter | anthropic | openai | grok
        s.setdefault("ai_api_key", "")           # AI series research (web search)
        s.setdefault("ai_model", "")             # blank = provider default
        s.setdefault("abs_url", "")              # Audiobookshelf server, e.g. http://nas:13378
        s.setdefault("abs_token", "")            # ABS API token
        return dict(s)


# auto-buy tunables
BUY_MODES = ("off", "verify", "auto")
BUY_DEFAULT_CAP = 10.0          # above this price, "auto" behaves like "verify"
BUY_TOKEN_TTL_H = 48            # an approval link is good for two days
BUY_PROBE_MAX = 12              # re-check a release-day page this many times...
BUY_PROBE_DAYS = 4              # ...over this many days, waiting for the offer to appear
BUY_PROBE_GAP_H = 3             # ...at most once every few hours
BUY_WEEKLY_MAX = 3              # hard ceiling on AUTOMATIC purchases per 7 days
BUY_BAT_DELAY_S = 150           # give Audible's library a moment to list the title
BUY_BAT_RETRIES = 3             # re-run the bat if Libation didn't see it yet
BUY_BAT_RETRY_GAP_S = 600
BUY_STORE_RETRY_H = 3           # Amazon refused the checkout (title not live on Audible AU yet): try again in...
BUY_STORE_RETRY_MAX = 16        # ...at most this many times (~2 days)

BACKUP_DIR = os.path.join(data_dir(), "backups")
BACKUP_KEEP = 14           # rolling daily snapshots — two weeks of undo


def _daily_backup():
    """Snapshot data.json into backups/ once per day, before the day's first
    write, keeping the newest BACKUP_KEEP. Hand edits stay recoverable even when
    no bulk operation ever wrote a .bak."""
    try:
        if not os.path.exists(DATA_FILE):
            return
        dest = os.path.join(BACKUP_DIR, "data-%s.json" % today().isoformat())
        if os.path.exists(dest):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(DATA_FILE, "rb") as f:
            blob = f.read()
        with open(dest, "wb") as g:
            g.write(blob)
        old = sorted(x for x in os.listdir(BACKUP_DIR)
                     if x.startswith("data-") and x.endswith(".json"))
        for x in old[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, x))
    except Exception as e:
        log.info("daily backup failed: %s", e)


def save_state():
    with _lock:
        _daily_backup()
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, DATA_FILE)


def add_log(msg):
    with _lock:
        _state["log"].insert(0, {"time": datetime.now().isoformat(timespec="seconds"), "msg": msg})
        _state["log"] = _state["log"][:60]
    log.info(msg)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

DATE_INPUT_FORMATS = [
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
    "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%B %Y", "%b %Y",
]


def parse_date(value):
    """Return a date from a flexible string, or None."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # ISO with time
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date()
    except Exception:
        pass
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def iso(d):
    return d.isoformat() if isinstance(d, date) else (d or None)


def today():
    return date.today()


# ---------------------------------------------------------------------------
# Gap maths
# ---------------------------------------------------------------------------

def gap_days_list(series):
    """Novel→audio gaps in CHRONOLOGICAL order (oldest first). The recency
    weighting downstream keys off list position, and hand-edited timelines can
    be stored in any row order — sort here, like novel_gap_days_list does."""
    pairs = []
    for pair in series.get("history", []):
        n = parse_date(pair.get("novel"))
        a = parse_date(pair.get("audiobook"))
        if n and a:
            pairs.append((n, (a - n).days))
    pairs.sort(key=lambda p: p[0])
    return [g for _, g in pairs]


def avg_gap_days(series):
    gaps = gap_days_list(series)
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps))


def _robust_gaps(gaps):
    """(kept_gaps, dropped_count): the gap list with wild outliers removed.

    One freak gap (an audiobook delayed a year, a pandemic-era novel) shouldn't
    wreck every future guess. With 4+ datapoints, gaps outside the Tukey fences
    (1.5×IQR beyond the quartiles) are dropped; fewer than 4 and there's no way
    to call anything an outlier, so everything is kept."""
    if not gaps:
        return [], 0
    if len(gaps) < 4:
        return list(gaps), 0
    # inclusive: with the 4-8 datapoints a series realistically has, exclusive
    # interpolation lets the extreme value drag its own fence out of reach
    q1, _, q3 = statistics.quantiles(gaps, n=4, method="inclusive")
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [g for g in gaps if lo <= g <= hi] or list(gaps)
    return kept, len(gaps) - len(kept)


def predicted_gap_days(gaps):
    """Days to project forward: recency-weighted mean of the outlier-filtered
    gaps, or None. Newest gap weighs ~2x a gap three books back (0.75^age) —
    cadences drift, and the recent books know the current publisher/narrator
    situation better than the launch-era ones."""
    kept, _ = _robust_gaps(gaps)
    if not kept:
        return None
    ws = [0.75 ** (len(kept) - 1 - i) for i in range(len(kept))]
    return round(sum(g * w for g, w in zip(kept, ws)) / sum(ws))


def gap_label(days):
    if days is None:
        return None
    d = abs(days)
    if d < 14:
        return f"{days}d"
    if d < 90:
        return f"~{round(days / 7)} wks"
    return f"~{round(days / 30.4)} mo"


def audio_gap_days_list(series):
    """PER-VOLUME spacing (days) between audiobook releases, chronological. This
    is the series' audio rhythm, which is what actually paces a backlog.

    Normalized by how many volumes separate the two books: when a publisher
    announces book 9's audio while 4-8 are still unannounced, the raw date gap
    spans six volumes, and counting it as one 'gap' would read as a dramatically
    slower cadence instead of the same pace across more books."""
    pts = [(a, _entry_n(e)) for e in series.get("timeline", [])
           for a in [parse_date(e.get("audiobook"))] if a]
    pts.sort(key=lambda p: p[0])
    out = []
    for i in range(1, len(pts)):
        (d0, n0), (d1, n1) = pts[i - 1], pts[i]
        days = (d1 - d0).days
        if days <= 0:
            continue
        steps = (n1 - n0) if (n0 is not None and n1 is not None and n1 > n0) else 1
        out.append(round(days / steps))
    return out


def _audio_cadence(series):
    """How far apart this series' audiobooks land, or None.

    Prefers the audio's own measured rhythm, but only once there are two gaps to
    average — a single gap is usually the launch-era delay (first audiobook lags,
    then the line settles), so with less than that the novel cadence is the better
    proxy for how fast the publisher moves."""
    gaps = audio_gap_days_list(series)
    if len(gaps) >= 2:
        return predicted_gap_days(gaps)
    return predicted_gap_days(novel_gap_days_list(series))


AUDIO_MIN_SEPARATION = 21     # no series ships two of its audiobooks days apart
AUDIO_LAG_TIGHT = 21          # spread under this = a dependable novel→audio lag


def _safe_add_days(d, days):
    """d + days, or None if that runs off the end of the calendar (a hand-typed
    '9999-12-31' must not 500 the whole dashboard)."""
    if d is None or days is None:
        return None
    try:
        return d + timedelta(days=days)
    except (OverflowError, OSError, ValueError):
        return None


def _audio_lag_is_reliable(series):
    """True when this series ships audio at a consistent remove from the novel
    (day-and-date, or a steady lag). Then the NOVEL date — especially a confirmed
    pre-order — is the better predictor and the queue must not override it: for a
    same-day series the 'queue' is just a noisy restatement of the novel cadence,
    and letting it win pushes the audiobook past its own novel."""
    c = _estimate_confidence(gap_days_list(series))
    return bool(c and c["n"] >= 2 and c.get("spread_days") is not None
                and c["spread_days"] <= AUDIO_LAG_TIGHT)


# ---- author-stated dates from the AI research -------------------------------
# A date the author or publisher gave (found by the research) beats the gap
# maths for THAT book: it becomes the estimate, and carries its source so the
# card shows it on hover. Once it has passed by more than a grace period it is
# ignored again - the series is overdue, and gets re-researched.
INTEL_DATE_GRACE_DAYS = 14


def _intel_when(series, kind, n):
    """(date, record) for one book's author-stated date, or None."""
    if n is None:
        return None
    rec = (series.get("intel_dates") or {}).get(f"{kind}:{n}")
    d = parse_date(rec.get("date")) if isinstance(rec, dict) else None
    if not d or (today() - d).days > INTEL_DATE_GRACE_DAYS:
        return None
    return d, rec


def _next_novel_n(series):
    """The book after the latest known NOVEL (an audio-only row with a higher
    number must not shift it)."""
    ns = [_entry_n(e) for e in series.get("timeline", []) if _entry_n(e) is not None and e.get("novel")]
    base = max(ns) if ns else series.get("novel_n")
    return (base + 1) if base is not None else None


def _audio_schedule(series):
    """Projected audiobook dates for every book still missing one, in book order:
    [{n, date, novel, queued, after_n}, ...].

    Two things bind an audiobook's release, and the later one wins:
      production — it can't arrive sooner after its novel than the usual lag
      queue      — a series' audiobooks come out in order, spaced by their cadence
    Each projection becomes the anchor for the next, so a backlog (several novels
    out, audio still catching up) forecasts as a queue draining at the series'
    real pace instead of everything landing at once. A CONFIRMED date — including
    a pre-order for an earlier book — is simply the strongest possible anchor:
    it's where the queue currently stands, which is why an announcement makes the
    following books' estimates sharper rather than deleting them."""
    gap_model = predicted_gap_days(gap_days_list(series))
    cadence = _audio_cadence(series)
    lag_reliable = _audio_lag_is_reliable(series)
    # Numbered books only: an unnumbered row can't be identified on a card or in
    # the schedule, and sorting it to the front would let it hijack the forecast.
    entries = sorted((e for e in series.get("timeline", []) if _entry_n(e) is not None),
                     key=lambda e: (_entry_n(e), e.get("novel") or ""))
    out = []
    last_audio, last_n = None, None
    for e in entries:
        a = parse_date(e.get("audiobook"))
        if a:
            # walk the queue forward book by book, so the anchor is always an
            # EARLIER book — a later volume's announcement must never push an
            # earlier, still-missing audiobook behind it
            if last_audio is None or a > last_audio:
                last_audio, last_n = a, _entry_n(e)
            continue
        nd = parse_date(e.get("novel"))
        prod = _safe_add_days(nd, gap_model) if nd is not None else None
        queue = None
        if last_audio is not None:
            if lag_reliable:
                # trust the lag; the queue only enforces ordering here
                queue = _safe_add_days(last_audio, 1)
            else:
                step = max(cadence if cadence is not None else 0, AUDIO_MIN_SEPARATION)
                queue = _safe_add_days(last_audio, step)
        hit = _intel_when(series, "audiobook", _entry_n(e))
        if hit:
            est, queued = hit[0], False          # the author's word anchors the queue from here
        elif prod is None and queue is None:
            continue
        elif prod is None:
            est, queued = queue, True
        elif queue is None:
            est, queued = prod, False
        else:
            est, queued = (queue, True) if queue > prod else (prod, False)
        out.append({"n": _entry_n(e), "date": est, "novel": nd,
                    "queued": queued, "after_n": last_n, "source": hit[1] if hit else None})
        if est < today():
            # This book's window has passed with nothing announced, and the next
            # volume cannot possibly come out before it — so how late this one
            # ends up running is exactly how little we know about the rest. The
            # honest answer for everything behind it is "no idea", not a number
            # anchored on a date that already failed. It surfaces as overdue.
            break
        last_audio, last_n = est, _entry_n(e)
    return out


def _audio_confidence_gaps(series, fc):
    """The gap list that actually produced this forecast, so the confidence chip
    describes the model that made the call rather than a model that didn't."""
    if fc and fc.get("queued"):
        ag = audio_gap_days_list(series)
        if len(ag) >= 2:
            return ag
        return novel_gap_days_list(series)     # cadence fell back to the novel rhythm
    return gap_days_list(series)


def _audio_schedule_usable(series):
    """The projected schedule, or [] when this series shouldn't be forecast at all
    (its tracked book's audio is already announced/released, or the series has
    visibly stopped producing audiobooks)."""
    status = series.get("audiobook_status", "unknown")
    if status in ("released", "announced"):
        return []
    sched = _audio_schedule(series)
    if not sched:
        return []
    if sched[0].get("source"):
        return sched                         # the author said when: no pattern to doubt
    # Don't keep projecting a pattern the series itself has broken: count novels
    # whose audiobook is well overdue by the novel→audio model (novel + gap + 60d
    # grace, still nothing). When the contradictions match or outnumber the real
    # gap datapoints, that premise is dead — e.g. one simultaneous release followed
    # by three novels with no audio at all should NOT predict a same-day audiobook.
    # Only applies when the forecast actually rests on that model: a queue-paced
    # one is built from the audio's own (slower) rhythm, where a backlog of
    # audio-less novels is exactly what the model already expects.
    if not sched[0]["queued"]:
        gaps = gap_days_list(series)
        avg = predicted_gap_days(gaps)
        if avg is not None and gaps:
            t = today()
            overdue = 0
            for e in series.get("timeline", []):
                n_d = parse_date(e.get("novel"))
                if n_d and n_d < t and not e.get("audiobook") and t > n_d + timedelta(days=avg + 60):
                    overdue += 1
            if overdue >= len(gaps):
                return []
    return sched


def _audio_forecast(series):
    """The FIRST book still missing an audiobook, as the schedule dict, or None.

    First in book order, not first still-in-the-future: audiobooks come out in
    order, so the earliest undated book is genuinely the next one due. When its
    projection has already passed, that book is overdue and the card says so —
    quietly skipping to a later book would hide a late audiobook behind a
    confident-looking date months out."""
    sched = _audio_schedule_usable(series)
    return sched[0] if sched else None


def _audio_next_forecast(series):
    """The soonest projection still ahead of us, or None.

    Distinct from _audio_forecast, which reports the first missing book even when
    that book is overdue. This one answers 'what audiobook is there to look
    forward to', so one late book can't erase the series from the schedule, feed,
    calendar and digest."""
    t = today()
    for r in _audio_schedule_usable(series):
        if r["date"] >= t:
            return r
    return None


def _predicted_audio_pair(series):
    """(predicted_date, the novel date it was predicted from) or (None, None)."""
    f = _audio_forecast(series)
    return (f["date"], f["novel"]) if f else (None, None)


def predicted_audiobook_date(series):
    """Only meaningful when there is no announcement yet."""
    return _predicted_audio_pair(series)[0]


def next_release(series):
    """The next dated event for this series: (date, kind, is_estimate) or None.

    Real dates are gathered from EVERY timeline entry, not just the series-level
    headline fields — those mirror only the latest book, so an announced audiobook
    for an earlier book (status still "none") would otherwise be invisible and an
    estimate would show in its place."""
    t = today()
    real = []
    for e in series.get("timeline", []):
        for key, kind in (("novel", "novel"), ("audiobook", "audiobook")):
            d = parse_date(e.get(key))
            if d and d >= t:
                real.append((d, kind))
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    if status in ("announced", "released") and ad and ad >= t:
        real.append((ad, "audiobook"))
    nd = parse_date(series.get("novel_date"))
    if nd and nd >= t:
        real.append((nd, "novel"))
    if real:
        d, kind = min(real)
        return d, kind, False
    pred = predicted_audiobook_date(series)
    if pred and pred >= t:
        return pred, "audiobook", True
    return None


def novel_gap_days_list(series):
    """Gaps (days) between consecutive NOVEL releases, taken from the timeline."""
    dates = []
    for e in sorted(series.get("timeline", []), key=lambda e: _entry_n(e) or 0):
        nd = parse_date(e.get("novel"))
        if nd:
            dates.append(nd)
    out = []
    for i in range(1, len(dates)):
        d = (dates[i] - dates[i - 1]).days
        if d > 0:
            out.append(d)
    return out


def avg_novel_gap_days(series):
    gaps = novel_gap_days_list(series)
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps))


def predicted_novel_date(series):
    """Predict the NEXT novel, but only when the latest known novel is already out
    (if the latest novel is still upcoming, that pre-order *is* the next novel)."""
    nd = parse_date(series.get("novel_date"))
    if nd is None or nd >= today():
        return None
    hit = _intel_when(series, "novel", _next_novel_n(series))
    if hit:
        return hit[0]
    avg = predicted_gap_days(novel_gap_days_list(series))
    if avg is None:
        return None
    return nd + timedelta(days=avg)


def predicted_novel_source(series):
    """The author-stated record behind predicted_novel_date, or None when the
    prediction is gap maths."""
    nd = parse_date(series.get("novel_date"))
    if nd is None or nd >= today():
        return None
    hit = _intel_when(series, "novel", _next_novel_n(series))
    return hit[1] if hit else None


def _estimate_confidence(gaps):
    """How much to trust a gap-based projection, from the number and consistency of
    the past gaps it averages. Returns {level, n, spread_days, outliers} or None.
    'spread_days' is the typical deviation of those gaps — roughly how far off the
    guess could be. Judged on the same outlier-filtered set the prediction uses
    ('outliers' = how many wild gaps were ignored), so one freak delay doesn't
    tank the confidence of an otherwise clockwork series."""
    kept, dropped = _robust_gaps(gaps)
    n = len(kept)
    if n == 0:
        return None
    if n == 1:
        return {"level": "low", "n": 1, "spread_days": None, "outliers": dropped}
    mean = sum(kept) / n
    sd = (sum((g - mean) ** 2 for g in kept) / n) ** 0.5
    sd_i = int(round(sd))
    if n >= 3 and sd <= 10:
        level = "high"      # several releases, all landing within ~10 days of the average
    elif sd <= 21:
        level = "medium"    # within ~3 weeks
    else:
        level = "low"       # scattered enough that the date is a rough guess
    return {"level": level, "n": n, "spread_days": sd_i, "outliers": dropped}


# ---------------------------------------------------------------------------
# Scraping (best effort - hand-editable fallback always available)
# ---------------------------------------------------------------------------

def fetch_bytes(url, timeout=15):
    """Raw bytes GET with the browser UA (for image analysis)."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_url(url, timeout=20):
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
    })
    with urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _author_to_str(a):
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return a.get("name", "")
    if isinstance(a, list):
        names = [_author_to_str(x) for x in a]
        return ", ".join(n for n in names if n)
    return ""


def _walk_jsonld(obj, found):
    """Collect dicts that look like books/products from arbitrary JSON-LD."""
    if isinstance(obj, dict):
        t = obj.get("@type", "")
        types = t if isinstance(t, list) else [t]
        if any(x in ("Book", "Audiobook", "Product", "CreativeWork") for x in types):
            found.append(obj)
        for v in obj.values():
            _walk_jsonld(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_jsonld(v, found)


_FORMAT_WORDS = (r"eBook|Kindle Edition|Kindle|Audible Audiobook|Audiobook|Paperback|"
                 r"Hardcover|Mass Market Paperback|Audio CD|Board ?book|Library Binding")


def clean_title(t):
    """Strip Amazon/store boilerplate from a scraped page title."""
    if not t:
        return t
    s = " ".join(str(t).split())
    # Drop "... : Amazon.com.au: Kindle Store" style store tails.
    s = re.sub(r"\s*[:|]\s*Amazon\.[^:|]*\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*[:|]\s*(?:Books|Kindle Store|Audible(?:\.\w+)?|Audiobooks)\s*$", "", s, flags=re.I)
    # Drop a format word and anything after it ("... eBook : Author, Author").
    s = re.sub(r"\s+(?:%s)\b.*$" % _FORMAT_WORDS, "", s, flags=re.I)
    return s.strip(" :|-\u2013\u2014")


def series_from_title(t):
    """Pull a series name out of '(Series Book 5)' / '(Series, Book 5)' / '(Series #5)'."""
    if not t:
        return None
    m = re.search(r"\(([^()]+?)[,\s]+(?:Book|Vol\.?|Volume|Part|#)\s*\d+\)", str(t), re.I)
    if m:
        return m.group(1).strip(" ,")
    return None


def parse_product(html_text, url=""):
    """
    Returns a dict with whatever we could extract:
      {title, author, date, image, is_audiobook}
    Everything is optional. Designed never to raise.
    """
    result = {"title": None, "author": None, "date": None, "image": None, "is_audiobook": None}
    low = html_text.lower()

    # --- Strategy 1: JSON-LD (most stable when present, esp. on Audible) ---
    if HAVE_BS4:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            blocks = soup.find_all("script", attrs={"type": "application/ld+json"})
            candidates = []
            for b in blocks:
                raw = b.string or b.get_text() or ""
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                _walk_jsonld(data, candidates)
            for c in candidates:
                if not result["title"] and c.get("name"):
                    result["title"] = str(c["name"]).strip()
                if not result["author"] and c.get("author"):
                    result["author"] = _author_to_str(c["author"]).strip() or None
                if not result["date"]:
                    d = parse_date(c.get("datePublished") or c.get("dateCreated"))
                    if d:
                        result["date"] = iso(d)
                if not result["image"]:
                    img = c.get("image")
                    if isinstance(img, dict):
                        img = img.get("url")
                    if isinstance(img, list) and img:
                        img = img[0]
                    if isinstance(img, str):
                        result["image"] = img
                ctype = c.get("@type", "")
                ctypes = ctype if isinstance(ctype, list) else [ctype]
                if "Audiobook" in ctypes or str(c.get("bookFormat", "")).lower().endswith("audiobook"):
                    result["is_audiobook"] = True

            # Meta fallbacks
            if not result["title"]:
                m = soup.find("meta", property="og:title")
                if m and m.get("content"):
                    result["title"] = m["content"].strip()
            if not result["title"] and soup.title and soup.title.string:
                result["title"] = soup.title.string.strip()
            if not result["image"]:
                m = soup.find("meta", property="og:image")
                if m and m.get("content"):
                    result["image"] = m["content"].strip()
        except Exception as e:
            log.exception("parse_product soup failure: %s", e)

    # --- Strategy 2: weak audiobook signal from page text ---
    if result["is_audiobook"] is None:
        if "audible audiobook" in low or "audiobook edition" in low or "audible.com" in (url or "").lower():
            result["is_audiobook"] = True

    # Clean up scraped titles (Amazon page titles carry store/format cruft) and
    # try to lift a series name out of the title for the Add form.
    result["series_guess"] = series_from_title(result.get("title"))
    if result.get("title"):
        result["title"] = clean_title(result["title"])
        if not result["series_guess"]:
            result["series_guess"] = series_from_title(result["title"])

    return result


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_NARRATOR_TAIL = re.compile(
    r"\s*(?:,\s*)?(?:see all|see more|and others?|et al\.?|(?:&|and|\+)\s*\d+\s*more|,)\s*$", re.I)


def clean_narrator(s):
    """Strip the store's UI furniture off a scraped narrator credit.

    Amazon's byline runs the names together with page controls, so a read can
    come back as 'Christian J. Gilliland, see all' or '... (Narrator)'."""
    s = " ".join((s or "").split())
    s = re.sub(r"\(\s*(?:narrator|narrated by)[^)]*\)", "", s, flags=re.I)
    prev = None
    while prev != s:
        prev = s
        s = _NARRATOR_TAIL.sub("", s).strip()
    return s or None


def lead_narrator(s):
    """The first-billed narrator. Full-cast productions list a different
    subset of the cast depending on which page area was read ('X' on one
    visit, 'X, Y, Z' on the next), so a swap is judged on the lead only."""
    s = clean_narrator(s) or ""
    return re.split(r"\s*(?:,|&|\band\b)\s*", s, maxsplit=1)[0].strip() or None


def _clean_stored_narrators():
    """Credits captured before clean_narrator existed carry page furniture
    ('X, see all'); compared against a clean read they pinged a narrator
    'change' that never happened (Riftside 4, Sep 2026). Idempotent."""
    for srs in _state.get("series", []):
        for e in srs.get("timeline") or []:
            if e.get("narrator"):
                e["narrator"] = clean_narrator(e["narrator"])


def _entry_n(e):
    try:
        return int(e.get("n"))
    except (TypeError, ValueError):
        return None


def _history_from_timeline(tl):
    return [{"novel": e["novel"], "audiobook": e["audiobook"]}
            for e in tl if e.get("novel") and e.get("audiobook")]


_tray_icon = None  # set when the system-tray icon is running


def _backup_state():
    """Snapshot data.json before a bulk operation, so a bad run is recoverable."""
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "rb") as f:
                blob = f.read()
            with open(DATA_FILE + ".bak", "wb") as g:
                g.write(blob)
            log.info("Backed up data to %s", DATA_FILE + ".bak")
    except Exception as e:
        log.info("backup failed: %s", e)


# ---------------------------------------------------------------------------
# Computed view for the UI / feed
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    "released": "Audiobook out",
    "announced": "Announced",
    "none": "No audiobook yet",
    "unknown": "Unknown",
}


def series_view(s):
    avg = avg_gap_days(s)
    pred = predicted_audiobook_date(s)
    n_avg = avg_novel_gap_days(s)
    n_pred = predicted_novel_date(s)
    audio_fc = _audio_forecast(s)
    audio_next = _audio_next_forecast(s)
    audio_conf = _estimate_confidence(_audio_confidence_gaps(s, audio_fc))
    novel_conf = _estimate_confidence(novel_gap_days_list(s))
    nxt = next_release(s)
    status = s.get("audiobook_status", "unknown")
    # Derived display status: the raw status mirrors only the LATEST book, but a
    # real still-upcoming audiobook for an earlier book is an announcement the
    # card should wear — pill + counts say "Audio pre-order" instead of "No
    # audiobook yet". The raw status is kept untouched (edit modal, ingest).
    t = today()
    # An audiobook for ANY book is "pre-order" only while it is genuinely ahead of
    # us. A date that is today-or-earlier is out — and on release day itself the
    # store's own confirmation (audio_pre == False, set when the page reads as
    # buyable) must win over the calendar, otherwise the pill still says
    # "pre-order" on a book the Audiobook column is already calling OUT NOW.
    def _pending(e):
        d = parse_date(e.get("audiobook"))
        if not d:
            return False
        if e.get("audio_pre") is False:      # confirmed buyable by the store
            return False
        if d > t:
            return True                      # strictly ahead of today
        # dated today and not yet confirmed: still a pre-order until the store
        # says otherwise (US-midnight releases land mid-afternoon here)
        return d == t and e.get("audio_pre") is True
    has_future_audio = any(_pending(e) for e in s.get("timeline", []))
    if status in ("none", "unknown") and has_future_audio:
        status_class, status_label = "announced", "Audio pre-order"
    else:
        status_class, status_label = status, STATUS_LABELS.get(status, "Unknown")
    # Narrator: the most recent book that names one speaks for the series.
    narrator = None
    for e in sorted(s.get("timeline", []), key=lambda e: _entry_n(e) or 0):
        # cleaned on the way out too, so credits captured before the extension
        # learned to strip page furniture still display properly
        cleaned = clean_narrator(e.get("narrator"))
        if cleaned:
            narrator = cleaned
    view = {
        "id": s["id"],
        "name": s.get("name", ""),
        "author": s.get("author", ""),
        "url": s.get("url", ""),
        "novel_title": s.get("novel_title", ""),
        "novel_date": s.get("novel_date"),
        "audiobook_date": s.get("audiobook_date"),
        "audiobook_status": status,
        "status_class": status_class,
        "status_label": status_label,
        "narrator": narrator,
        "cover_url": s.get("cover_url", ""),
        "history": s.get("history", []),
        "timeline": s.get("timeline", []),
        "novel_n": s.get("novel_n"),
        "store_host": s.get("store_host", ""),
        "asins": s.get("asins", {}) or {},
        "asins_novel": s.get("asins_novel", {}) or {},
        "asins_audio": s.get("asins_audio", {}) or {},
        "buy_mode": s.get("buy_mode") or "off",
        "buy_cap": s.get("buy_cap"),
        "audio_own": _ownership_for(s.get("id")),
        "listening": _abs_listening_for(s.get("name", "")),
        "intel": s.get("intel"),
        "edited": bool(s.get("edited")),
        "notes": s.get("notes", ""),
        "last_fetched": s.get("last_fetched"),
        "last_change": s.get("last_change"),
        "last_change_at": s.get("last_change_at"),
        "fetch_error": s.get("fetch_error"),
        "scraped_title": s.get("scraped_title"),
        "avg_gap_days": avg,
        "avg_gap_label": gap_label(avg),
        "history_count": len(gap_days_list(s)),
        "predicted_date": iso(pred),
        "predicted_book_n": audio_fc.get("n") if audio_fc else None,
        "predicted_queued": bool(audio_fc and audio_fc.get("queued")),
        "predicted_after_n": audio_fc.get("after_n") if audio_fc else None,
        "predicted_confidence": pred and audio_conf,
        # the soonest projection still ahead — same as predicted_* unless the
        # first missing book is overdue, in which case the card shows both
        "next_est_date": iso(audio_next["date"]) if audio_next else None,
        "next_est_book_n": audio_next.get("n") if audio_next else None,
        "avg_novel_gap_label": gap_label(n_avg),
        "novel_history_count": len(novel_gap_days_list(s)),
        "predicted_novel_date": iso(n_pred),
        "predicted_novel_confidence": n_pred and novel_conf,
        # who said so, when an estimate is the author's own date (hover asterisk)
        "predicted_novel_source": predicted_novel_source(s),
        "predicted_audio_source": (audio_fc or {}).get("source"),
        "next_est_source": (audio_next or {}).get("source"),
        "next_date": None,
        "next_kind": None,
        "next_estimate": False,
        "countdown_days": None,
    }
    if nxt:
        nd, kind, est = nxt
        view["next_date"] = iso(nd)
        view["next_kind"] = kind
        view["next_estimate"] = est
        view["countdown_days"] = (nd - today()).days
    return view


def next_audiobook(series, include_estimates):
    """Next AUDIOBOOK event for a series: (date, is_estimate, note) or None.
    Announced/released audiobooks always count. Predicted audiobooks count only
    when estimates are on — for day-and-date series the prediction lands on (or
    near) the novel date, which is exactly the 'likely simultaneous' case. Plain
    novels are intentionally excluded so this list stays audiobook-focused."""
    t = today()
    real = [d for d in (parse_date(e.get("audiobook")) for e in series.get("timeline", []))
            if d and d >= t]
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    if status in ("announced", "released") and ad and ad >= t:
        real.append(ad)
    if real:
        return min(real), False, None
    if include_estimates:
        est = _audio_estimate(series)
        if est:
            return est[0], True, est[1]
    return None


def _audio_estimate(series):
    """(predicted_date, note) for the audiobook forecast, or None. Independent of
    any real upcoming audiobook — callers decide precedence (the forecast lists
    show both: real rows for dated books, this row for the next undated one)."""
    f = _audio_next_forecast(series)
    if not f:
        return None
    pred = f["date"]
    if f.get("source"):
        return pred, _intel_note(f["source"])
    if f["queued"]:
        # the audio queue, not the novel, is what's holding this one back — say so
        after = f" book {f['after_n']}'s" if f.get("after_n") is not None else " the previous"
        return pred, f"next in the audio queue after{after}"
    nd = f["novel"] or parse_date(series.get("novel_date"))
    gap = (pred - nd).days if nd else None
    if gap is None:
        note = "estimated from past releases"
    elif gap <= 3:
        note = "likely same day as the novel"
    elif gap < 21:
        note = f"~{gap} days after the novel"
    else:
        note = f"~{round(gap / 7)} weeks after the novel"
    return pred, note


def _intel_note(src):
    """The estimate's basis when it is the author's own word."""
    said = (src.get("said") or src.get("expected") or "").strip()
    return f"author: {said}" if said else "author's stated timing"


def _novel_estimate(series):
    """(predicted_date, note) for the next-novel forecast, or None."""
    pred = predicted_novel_date(series)
    if not pred or pred < today():
        return None
    src = predicted_novel_source(series)
    if src:
        return pred, _intel_note(src)
    avg = predicted_gap_days(novel_gap_days_list(series))
    if avg is None:
        note = "estimated from past releases"
    elif avg < 21:
        note = f"~{avg} days after the last book"
    elif avg < 90:
        note = f"~{round(avg / 7)} weeks after the last book"
    else:
        note = f"~{round(avg / 30.4)} months after the last book"
    return pred, note


def _book_label(s, e, kind):
    """'Chrysalis 10' style label for one timeline entry."""
    n = _entry_n(e)
    base = (e.get("title") or "").strip()
    if not base:
        base = s.get("novel_title", "") if n == s.get("novel_n") else ""
    if not base:
        base = s.get("name", "")
        return f"{base} {n}" if n is not None else base
    return base


def upcoming_of_kind(kind, include_estimates):
    """Upcoming releases of one kind ('novel' or 'audiobook'), soonest first.

    ONE ROW PER BOOK, not per series: a series can have several things pending at
    once (an announced audiobook for book 5 AND a pre-ordered novel for book 6),
    and every one of them deserves its own row. Real dates come from the timeline;
    at most one estimate per series is appended (the soonest predicted book that
    isn't already covered by a real date).
    """
    rows = []
    t = today()
    with _lock:
        for s in _state["series"]:
            seen = set()
            for e in s.get("timeline", []):
                d = parse_date(e.get(kind))
                if not d or d < t:
                    continue
                n = _entry_n(e)
                seen.add(n)
                rows.append({
                    "series_id": s["id"],
                    "series": s.get("name", ""),
                    "title": _book_label(s, e, kind),
                    "book_n": n,
                    "author": s.get("author", ""),
                    "date": iso(d),
                    "kind": kind,
                    "estimate": False,
                    "note": None,
                    "confidence": None,
                    "countdown_days": (d - t).days,
                    "cover": e.get("cover") or s.get("cover_url") or None,
                    # release-day support: is it confirmed live, and at what
                    # local hour is it expected to go live if not yet
                    "live": (e.get("audio_pre" if kind == "audiobook" else "novel_pre") is False),
                    "live_at": e.get("audio_live_at" if kind == "audiobook" else "novel_live_at"),
                    "expected_hour": expected_release_hour(s, kind)[0],
                    "expected_hour_basis": expected_release_hour(s, kind)[1],
                })
            if not include_estimates:
                continue
            est_pair = _novel_estimate(s) if kind == "novel" else _audio_estimate(s)
            if not est_pair:
                continue
            nd, note = est_pair
            if nd < t:
                continue                      # stale guess; real dates came from the timeline
            ent = None
            n = None
            fc = None
            source = predicted_novel_source(s) if kind == "novel" else None
            if kind == "novel":
                # The predicted novel doesn't exist as a timeline entry yet — it's
                # the book AFTER the latest known NOVEL (the estimate anchors on
                # novel_date, so an audio-only pre-order entry with a higher n
                # must not shift the label). Never borrow the current book's title.
                maxn = max((_entry_n(e) for e in s.get("timeline", [])
                            if _entry_n(e) is not None and e.get("novel")), default=None)
                if maxn is None:
                    maxn = s.get("novel_n")
                n = (maxn + 1) if maxn is not None else None
            else:
                # The forecast names the exact book it projected, so take the
                # number straight from the same row that produced the date.
                fc = _audio_next_forecast(s)
                n = fc.get("n") if fc else None
                source = fc.get("source") if fc else None
                ent = next((e for e in s.get("timeline", []) if _entry_n(e) == n), None) if n is not None else None
            if n is not None and n in seen:
                continue                      # this book already has a real date listed
            if ent:
                title = _book_label(s, ent, kind)
            elif kind == "novel":
                # generic label — the released latest book's exact title must not
                # appear on a row for a book that doesn't exist yet
                title = (f"{s.get('name', '')} {n}".strip() if n is not None
                         else s.get("name", ""))
            else:
                title = s.get("novel_title") or s.get("name", "")
            # same gap list the card's chip uses, so one prediction never carries
            # two different confidence badges across surfaces
            gaps = (novel_gap_days_list(s) if kind == "novel"
                    else _audio_confidence_gaps(s, fc))
            rows.append({
                "series_id": s["id"],
                "series": s.get("name", ""),
                "title": title,
                "book_n": n,
                "author": s.get("author", ""),
                "date": iso(nd),
                "kind": kind,
                "estimate": True,
                "note": note,
                # an author-stated date needs no confidence chip: the asterisk says who said it
                "confidence": None if source else _estimate_confidence(gaps),
                "source": source,
                "countdown_days": (nd - t).days,
                "cover": (ent.get("cover") if ent else None) or s.get("cover_url") or None,
            })
    rows.sort(key=lambda r: (r["date"], r["estimate"]))
    return rows


DEFAULT_RELEASE_HOUR = 14.0    # Amazon US midnight Eastern == 2pm AWST; the
                               # global fallback until a series has its own data


def _flip_hours(series_list, kind=None):
    """Local clock hours (float) at which releases were first seen buyable."""
    out = []
    for s in series_list:
        for e in s.get("timeline", []):
            for key in (("audio_live_at",) if kind == "audiobook" else
                        ("novel_live_at",) if kind == "novel" else
                        ("audio_live_at", "novel_live_at")):
                v = e.get(key)
                if not v:
                    continue
                try:
                    t = datetime.fromisoformat(v)
                    out.append(t.hour + t.minute / 60.0)
                except ValueError:
                    pass
    return out


def _circular_mean_hour(hours):
    """Average of clock hours that wraps correctly at midnight."""
    import math
    if not hours:
        return None
    x = sum(math.cos(h / 24.0 * 2 * math.pi) for h in hours)
    y = sum(math.sin(h / 24.0 * 2 * math.pi) for h in hours)
    ang = math.atan2(y, x)
    if ang < 0:
        ang += 2 * math.pi
    return round(ang / (2 * math.pi) * 24.0, 2)


def expected_release_hour(series, kind=None):
    """(hour, basis): the local hour a release is expected to go live.
    basis = 'series' (this series' own confirmed flips), 'global' (every
    series' flips), or 'default' (nothing learned yet)."""
    own = _flip_hours([series], kind)
    if own:
        return _circular_mean_hour(own), "series"
    with _lock:
        everyone = _flip_hours(_state.get("series", []), kind)
    if everyone:
        return _circular_mean_hour(everyone), "global"
    return DEFAULT_RELEASE_HOUR, "default"


def overdue_audiobooks():
    """Books whose projected audiobook window has passed with nothing announced,
    most-overdue first. These are deliberately kept OUT of the upcoming schedule —
    a date that already failed isn't a forecast — and reported on their own so a
    late audiobook is visible rather than silently distorting the queue."""
    t = today()
    rows = []
    with _lock:
        for s in _state["series"]:
            for r in _audio_schedule_usable(s):
                if r["date"] >= t:
                    break            # book order: everything after this is ahead
                ent = next((e for e in s.get("timeline", [])
                            if _entry_n(e) == r["n"]), None)
                rows.append({
                    "series_id": s["id"],
                    "series": s.get("name", ""),
                    "title": _book_label(s, ent, "audiobook") if ent else s.get("name", ""),
                    "book_n": r["n"],
                    "expected": iso(r["date"]),
                    "days_late": (t - r["date"]).days,
                    "novel_date": iso(r["novel"]) if r.get("novel") else None,
                    "cover": (ent.get("cover") if ent else None) or s.get("cover_url") or None,
                })
    # newest overdue first: the one that just slipped is the live news, while a
    # book that's been late for months is old information sitting at the bottom
    rows.sort(key=lambda r: r["days_late"])
    return rows


def _upcoming_settings():
    """(mode, include_estimates) for the active upcoming view."""
    s = get_settings()
    mode = s.get("upcoming_mode", "audiobook")
    if mode not in ("audiobook", "novel"):
        mode = "audiobook"
    est = bool(s.get("show_estimates_novel", True) if mode == "novel"
               else s.get("show_estimates", True))
    return mode, est


# ---------------------------------------------------------------------------
# Discord notifications (via a channel webhook the user pastes into settings)
# ---------------------------------------------------------------------------

KIND_COLOR = {"novel": 0x9D8CFF, "audiobook": 0xE7B14C,   # dashboard violet / amber
              "both": 0xC29BA6}                           # day-and-date blend


def _no_secrets(s):
    """Error text can echo the webhook URL (urllib: "unknown url type: '<the
    whole string>'"). It reaches the activity log, the dashboard and, via
    /api/state, the read-only tunnel - so it must never carry the token."""
    s = re.sub(r"https?://\S+", "<url>", s or "")
    s = re.sub(r"webhooks/\S+", "webhooks/<redacted>", s)
    return re.sub(r"'[^']*'", "'<redacted>'", s)[:200]      # urllib quotes the offending input verbatim


def _discord_post(webhook, text, embeds=None, fallback_text=None):
    """Blocking POST to a Discord webhook. Returns (ok, error_str). If an embed
    payload is rejected, retries once as plain text (fallback_text when given, so
    rich digests can degrade to their full text form) — a ping must never die
    because an embed was malformed."""
    def _send(payload):
        data = json.dumps(payload).encode("utf-8")
        req = Request(webhook, data=data,
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            resp.read()

    embeds = [e for e in (embeds or []) if e]
    payload = {"content": (text or "")[:1900]}
    if embeds:
        payload["embeds"] = embeds[:10]
    try:
        _send(payload)
    except HTTPError as e:
        if embeds:
            try:
                _send({"content": ((fallback_text or text) or "")[:1900]})
                with _lock:
                    _discord_fail["n"] = 0
                return True, ""
            except Exception:
                pass
        return False, f"HTTP {e.code}"
    except (ValueError, InvalidURL):
        # both echo the pasted string in their message: say nothing of it
        return False, "not a valid webhook URL — it must start with https://discord.com/api/webhooks/"
    except URLError as e:
        return False, _no_secrets(str(getattr(e, "reason", e)))
    except Exception as e:
        return False, _no_secrets(f"{type(e).__name__}: {e}")
    with _lock:
        _discord_fail["n"] = 0          # any good post, whoever sent it, ends a failure streak
    return True, ""


_discord_fail = {"n": 0}          # consecutive failed posts (any thread)
DISCORD_FAILS_TO_REPORT = 2       # one blip is weather; two in a row is a problem


def _discord_notify(text, embeds=None):
    """Fire-and-forget Discord message (no-op if no webhook configured)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return

    def _go():
        try:
            ok, err = _discord_post(wh, text, embeds)
            with _lock:
                _discord_fail["n"] = 0 if ok else _discord_fail["n"] + 1
                n = _discord_fail["n"]
            if not ok:
                add_log(f"Discord notify failed: {err}")
                if n >= DISCORD_FAILS_TO_REPORT:
                    # Discord can't be told that Discord is broken: the dashboard banner carries it
                    _problem("discord", what=f"Discord messages are failing ({err}).",
                             detail="Check the webhook in Settings → Discord — was it rotated or deleted?",
                             notify=False)
            else:
                _problem_clear("discord", what="Discord messages are going through again.")
        except Exception as e:                  # noqa: BLE001 - a state write can fail; never die silently
            add_log(f"Discord notify error: {e}")

    threading.Thread(target=_go, daemon=True).start()


def _book_embed(series, n, cover, kind="audiobook"):
    """Small embed that puts the book's cover art beside a ping (None if no art).
    The stripe colour says what the ping is about: violet for a novel, amber
    for an audiobook - the dashboard's own colours."""
    if not cover:
        return None
    title = f"{series} — Book {n}" if n is not None else (series or "")
    return {"title": title[:256], "thumbnail": {"url": cover}, "color": KIND_COLOR.get(kind, KIND_COLOR["audiobook"])}


# ---- alerts that clean up after themselves ---------------------------------
# A warning that later clears ("extension silent" -> "back", a problem -> fixed)
# used to leave two messages behind for good. The warning is now posted as an
# editable message; the all-clear EDITS it, and an hour later it is deleted, so
# a transient outage leaves no clutter. Persisted, so a restart still tidies up.
ALERT_TIDY_S = 3600
_alert_inflight = {}          # key -> Event, set once the post has stored its message URL


def _alert_post(key, text):
    """Post an editable alert under a key (several per key: daily reminders).
    Falls back to a plain message when there is nothing to edit later."""
    if not (get_settings().get("discord_webhook") or "").strip():
        _discord_notify(text)              # nothing to edit later; stays a plain (no-op) message
        return
    ev = threading.Event()
    _alert_inflight[key] = ev

    def _go():
        try:
            url = _discord_live_start(text)
            if url:
                with _lock:
                    _state.setdefault("alerts", {}).setdefault(key, []).append(url)
                    save_state()
            else:
                _discord_notify(text)
        except Exception as e:  # noqa: BLE001
            add_log(f"Discord alert error: {e}")
        finally:
            ev.set()
            _alert_inflight.pop(key, None)

    threading.Thread(target=_go, daemon=True).start()


def _alert_resolve(key, text, fallback=True):
    """Turn the alert into its all-clear and schedule its removal. With nothing
    to edit (older build, no webhook) the all-clear is a plain message, unless
    fallback is False (a silent clear: the warning still goes away)."""
    ev = _alert_inflight.get(key)
    if ev:
        ev.wait(timeout=8)                 # the alert may have been raised seconds ago
    with _lock:
        urls = (_state.get("alerts") or {}).pop(key, None) or []
        if urls:
            due = (datetime.now() + timedelta(seconds=ALERT_TIDY_S)).isoformat(timespec="seconds")
            _state.setdefault("discord_tidy", []).extend({"url": u, "at": due} for u in urls)
        save_state()
    if not urls:
        if fallback:
            _discord_notify(text)
        return False
    threading.Thread(target=_discord_live_edit, args=(urls[-1], text), daemon=True).start()
    return True


def _discord_delete(edit_url):
    """Delete a webhook message; 'already gone' counts as done."""
    try:
        with urlopen(Request(edit_url, method="DELETE", headers={"User-Agent": APP_NAME}), timeout=10) as r:
            r.read()
        return True
    except HTTPError as e:
        return e.code == 404
    except Exception:           # noqa: BLE001
        return False


def _discord_tidy_tick():
    """Delete resolved alerts whose hour is up; a failed delete is retried on
    later ticks, a few times."""
    now = datetime.now()
    with _lock:
        items = list(_state.get("discord_tidy") or [])
    if not items:
        return
    keep, changed = [], False
    for it in items:
        try:
            due = datetime.fromisoformat(it.get("at") or "")
        except ValueError:
            changed = True
            continue
        if due > now:
            keep.append(it)
            continue
        changed = True
        it["tries"] = int(it.get("tries") or 0) + 1
        if not _discord_delete(it["url"]) and it["tries"] < 3:
            keep.append(it)
    if changed:
        with _lock:
            _state["discord_tidy"] = keep
            save_state()


def _discord_ts(iso_str, style="D"):
    """Discord timestamp tag <t:unix:style> — renders in each viewer's own timezone.
    Styles: D = date, R = relative ("in 9 days"). Anchored at local noon so nearby
    timezones don't roll to the wrong day."""
    d = parse_date(iso_str)
    if not d:
        return iso_str or "?"
    ts = int(datetime(d.year, d.month, d.day, 12, 0).timestamp())
    return f"<t:{ts}:{style}>"


def _new_date_messages(s, n, old_novel, new_novel, old_audio, new_audio):
    """[(line, kind)] for dates that were just added or changed (today or later
    only, so backfilling a series' old catalogue doesn't spam)."""
    if not get_settings().get("discord_on_new", True):
        return []
    name = s.get("name", "")
    icon = {"novel": "📖", "audiobook": "🎧"}
    msgs = []
    for kind, old, new in (("novel", old_novel, new_novel), ("audiobook", old_audio, new_audio)):
        if not new or new == old:
            continue
        d = parse_date(new)
        if not d or d < today():
            continue
        when = f"**{_discord_ts(new, 'D')}** ({_discord_ts(new, 'R')})"
        if old:
            msgs.append((_fmt_line("date_changed", icon=icon[kind], series=name, n=n,
                                   kind=kind, when=when, was=_discord_ts(old, 'D')), kind))
        else:
            msgs.append((_fmt_line("new_date", icon=icon[kind], series=name, n=n,
                                   kind=kind, when=when), kind))
    return msgs


def _release_live_message(name, n, kind):
    icon = {"novel": "📖", "audiobook": "🎧"}.get(kind, "📚")
    return _fmt_line("out_now", icon=icon, series=name, n=n, kind=kind)

# Release pings are driven by the page itself: the extension re-reads a releasing
# novel hourly on its release day, and the moment the store page flips from
# pre-order to buyable, the ingest above sends "out now". No clock-based guessing.


# ---------------------------------------------------------------------------
# Daily forecast digest — your schedule, read to you by a deeply reluctant brat.
# ---------------------------------------------------------------------------

_TSUN_OPENERS = [
    "Hmph! I checked your precious book schedule again. You're WELCOME. 💢",
    "Listen up, because I'm only saying this once today: here's your dumb forecast.",
    "I can't believe I'm stuck reading release dates to you every morning. Ugh. Fine. Here:",
    "Oh look who needs ME to remember their books for them. Pathetic. Adorable— I mean pathetic.",
    "It's not like I stayed up organizing your reading calendar or anything! I was bored, okay?!",
    "Your daily forecast, delivered by yours truly, the only competent one around here. Bow.",
    "Baka! You'd forget every single release without me. Admit it. …Don't actually, it's embarrassing.",
    "Fine, FINE. Here's what's coming up. Stop looking at me like that.",
    "I graciously descend once more to manage your little book problem. Gratitude is expected.",
    "Tch. Another day, another schedule you can't keep track of yourself.",
    "You again?! Whatever. Here's the forecast. Not that I made it nice for you or anything.",
    "Daily report! Because SOMEONE has to be responsible around here, and it's obviously not you.",
    "Hmph, I suppose you've waited long enough. Behold: your releases.",
    "I had better things to do, you know! But your schedule doesn't read itself, so:",
]

_TSUN_CLOSERS = [
    "That's it. Now shoo. 😤",
    "Don't thank me. It's weird when you're grateful.",
    "Same time tomorrow. Don't be late, or I'll pretend I don't know you.",
    "Now go pretend you would've remembered all that on your own.",
    "Dismissed! And no, you can't ask follow-up questions.",
    "I expect you to actually READ these, you know. Hmph.",
    "That concludes today's charity work. You're welcome. Again.",
    "If you miss a release after all this, I'm never helping you again. (I will. But I'll be MAD.)",
    "Off you go. Some of us have important schedules to maintain.",
    "And that's everything. Not that I double-checked it for you or anything… I did. Twice.",
    "End of report! Applause is optional but recommended.",
    "Go on, get. And don't come crying to me if you forget— oh wait, you literally can't now.",
]

_TSUN_TODAY = [
    "…And YES, something's out TODAY. Try to contain yourself. I know I ca— YOU can't.",
    "Release day, release day! N-not that I'm excited for you! I just like being right!",
    "There's a TODAY in there, in case you're blind as well as forgetful. GO.",
    "Something drops today. If you don't read it immediately, what was even the point of me?!",
    "TODAY. Did you see it? The word TODAY?! Why are you still here?!",
    "A release! Today! I only reminded you like a million times. Praise me.",
]

_TSUN_SOON = [
    "Something's coming up REAL soon, so don't wander off and forget. I know how you are.",
    "Days away. DAYS. Set an alarm or something, honestly.",
    "The next one's almost here. I'm watching it so you don't have to. You're welcome.",
    "So close now! N-not that I've been counting down. That would be lame.",
    "Almost release time. If you miss it, I will be insufferable about it. More than usual.",
]

_TSUN_EMPTY = [
    "Nothing. There's NOTHING coming up. Congrats, your authors are all asleep. 💢",
    "The forecast is empty, which means you made me check for NOTHING. Unbelievable.",
    "No upcoming releases. Even I can't conjure books out of thin air, dummy.",
    "Zero releases on the horizon. Go touch grass or re-read something.",
    "Empty schedule today. Don't look at ME, I don't write the books!",
    "Nothing upcoming. Your favorite authors owe us BOTH an apology.",
]

_TSUN_EST = [
    "The ~ ones are my predictions. My predictions are excellent. Doubt them and perish.",
    "Squiggle marks mean I'm guessing. Brilliantly. As always.",
    "The estimates are educated guesses. MY education, so basically guaranteed.",
    "The ~ rows aren't announced yet — that's my genius forecasting. Trust it. Or don't. See if I care!",
    "Estimated dates are approximate! Complain to the authors, not me. I'm just gifted.",
]


_TSUN_NEWDATE = [
    "{icon} Big news, listen up! **{series}** Book {n} {kind} finally has a date: {when}. I found out first, obviously.",
    "{icon} Hmph, guess what: **{series}** Book {n} {kind} is set for {when}. You may celebrate. Quietly.",
    "{icon} NEW DATE. **{series}** Book {n} {kind}: {when}. I dropped everything to tell you. …Don't read into that.",
    "{icon} **{series}** Book {n} {kind} just got scheduled: {when}. Mark it down. I'm not reminding you. (I am.)",
    "{icon} Oh? OH? **{series}** Book {n} {kind} — {when}. You'd better look excited.",
    "{icon} Announcement! **{series}** Book {n} {kind} lands {when}. Say 'thank you'. Actually don't, it's weird.",
]

_TSUN_CHANGED = [
    "{icon} Ugh, they MOVED it. **{series}** Book {n} {kind} is now {when} (was {was}). Not my fault!",
    "{icon} Date shuffle! **{series}** Book {n} {kind}: {when} now, not {was}. Keep up.",
    "{icon} Correction, because SOMEONE'S publisher can't commit: **{series}** Book {n} {kind} → {when} (was {was}).",
    "{icon} **{series}** Book {n} {kind} slid to {when} (was {was}). I'm as annoyed as you are. More, probably.",
    "{icon} Heads up: **{series}** Book {n} {kind} changed to {when}. It used to be {was}. Authors, honestly.",
    "{icon} New date alert: **{series}** Book {n} {kind} is {when} now (was {was}). Adjust your little plans.",
]

_TSUN_BOUGHT = [
    "🛒 Fine. I bought **{series}** Book {n} for {price}. With YOUR money. You're welcome, obviously.",
    "🛒 **{series}** Book {n}: purchased for {price}. I haggled it down from full price. (The Kindle trick. My trick.)",
    "🛒 Bought **{series}** Book {n} ({price}). Try to act surprised when it shows up in your library.",
    "🛒 {price} for **{series}** Book {n}. Handled. Quietly. Like a professional. Unlike some people.",
    "🛒 **{series}** Book {n} is yours now — {price}. Yes I checked the price twice. Who do you take me for?",
]
_TSUN_ASK_BUY = [
    "💳 Oi. **{series}** Book {n} is sitting there at {price}. Want it or not? Approve: {link}",
    "💳 {price} for **{series}** Book {n}. I'm not spending your money without a nod. Well? {link}",
    "💳 **{series}** Book {n}, {price}. One tap and it's done. Or ignore me. See if I care. {link}",
]
_TSUN_FILED = [
    "🗂 Downloaded and filed. **{series}** Book {n} is on the shelf where it belongs. Everything in its place.",
    "🗂 **{series}** Book {n}: in your library, named properly, filed properly. I even lined up the folder name.",
    "🗂 Done — **{series}** Book {n} is downloaded, sorted, and shelved. Enjoy. Or whatever.",
]
_TSUN_WATCHDOG = [
    "⚠️ HEY. {what} I can't do my job like this. Fix it. ({detail})",
    "⚠️ Something's wrong: {what} I've been shouting into the void. {detail}",
    "⚠️ {what} Not to alarm you but I'm COMPLETELY BLIND right now. {detail}",
]
_TSUN_RECOVERED = [
    "✅ {what} We're back. I'll pretend that never happened if you do.",
    "✅ {what} Panic over. I had it under control the entire time. Obviously.",
]
_TSUN_OUTNOW = [
    "🎉 {icon} IT'S OUT. **{series}** Book {n} {kind} is live RIGHT NOW. Why are you still reading this?!",
    "🎉 {icon} **{series}** Book {n} {kind} just went live! Go! GO! You can thank me later. (Do thank me.)",
    "🎉 {icon} Release confirmed: **{series}** Book {n} {kind} is out now. I checked the page myself. You're welcome.",
    "🎉 {icon} **{series}** Book {n} {kind}: OUT. NOW. Drop everything. The dishes can wait.",
    "🎉 {icon} It's here, it's here! **{series}** Book {n} {kind} is buyable this very second. Move it!",
    "🎉 {icon} **{series}** Book {n} {kind} is officially out. I watched that page like a hawk for you. Praise is acceptable.",
]

_TSUN_TEST = [
    "✅ Testing, testing… yes, I can reach this channel. Obviously. I'm very competent.",
    "✅ It works. Of course it works, I set it up. You may stop poking the button now.",
    "✅ Test received! Congratulations on pressing a button. The notifications will flow.",
    "✅ Hello? HELLO? …Good, it's connected. Don't make me do that again.",
    "✅ Yes yes, the webhook works. I'll take my applause in book recommendations.",
]

_TSUN_MORE = [
    "…and {count} more. Like I'd list everything for you.",
    "…plus {count} others I'm keeping to myself. Ask nicely tomorrow.",
    "…and {count} more beyond that. My patience has limits, you know.",
    "…{count} more exist. The list stops here because I say so.",
]

_TSUN_NARRATOR = [
    "{icon} Ears up! **{series}** Book {n} has a NEW narrator: **{narrator}** (was {was}). I noticed immediately, obviously.",
    "{icon} Casting news: **{series}** Book {n} is read by **{narrator}** now, not {was}. Don't shoot the messenger. Or do, see if I care!",
    "{icon} They swapped narrators on **{series}** Book {n} — **{narrator}** takes over from {was}. Your ears, your problem.",
    "{icon} PSA: **{series}** Book {n} sounds different. New narrator **{narrator}** (was {was}). You're welcome for the warning.",
]

_LINES_DIR = os.path.join(data_dir(), "digest_lines")
_LINE_FILES = {
    "openers": "_TSUN_OPENERS", "closers": "_TSUN_CLOSERS", "today": "_TSUN_TODAY",
    "soon": "_TSUN_SOON", "empty": "_TSUN_EMPTY", "estimates": "_TSUN_EST",
    "new_date": "_TSUN_NEWDATE", "date_changed": "_TSUN_CHANGED",
    "out_now": "_TSUN_OUTNOW", "test": "_TSUN_TEST", "more": "_TSUN_MORE",
    "narrator": "_TSUN_NARRATOR",
    "bought": "_TSUN_BOUGHT", "ask_buy": "_TSUN_ASK_BUY", "filed": "_TSUN_FILED",
    "watchdog": "_TSUN_WATCHDOG", "recovered": "_TSUN_RECOVERED",
}
_LINE_PLACEHOLDERS = {
    "bought": "{series} {n} {price}",
    "ask_buy": "{series} {n} {price} {link}",
    "filed": "{series} {n}",
    "watchdog": "{what} {detail}",
    "recovered": "{what}",
    "new_date": "{icon} {series} {n} {kind} {when}",
    "date_changed": "{icon} {series} {n} {kind} {when} {was}",
    "out_now": "{icon} {series} {n} {kind}",
    "more": "{count}",
    "narrator": "{icon} {series} {n} {narrator} {was}",
}
# used verbatim if a user-edited template has a typo'd placeholder
_LINE_SAFE = {
    "bought": "🛒 Bought **{series}** — Book {n} for {price}.",
    "ask_buy": "💳 **{series}** — Book {n} at {price}. Approve or deny: {link}",
    "filed": "🗂 **{series}** — Book {n} downloaded and filed into the library.",
    "watchdog": "⚠️ {what} {detail}",
    "recovered": "✅ {what}",
    "new_date": "{icon} **{series}** — Book {n} {kind} now dated {when}",
    "date_changed": "{icon} **{series}** — Book {n} {kind} date changed to {when} (was {was})",
    "out_now": "🎉 {icon} **{series}** — Book {n} {kind} is **out now**!",
    "test": "✅ Release Radar test message — notifications are wired up.",
    "more": "…and {count} more.",
    "narrator": "{icon} **{series}** — Book {n} audiobook narrated by {narrator} (previously {was})",
}
_LINE_HEADER = ("# Release Radar — digest lines: {name}\n"
                "# One message per line. Blank lines and lines starting with # are ignored.\n"
                "# Discord markdown works (**bold**, emoji). Edits apply on the next digest —\n"
                "# no restart needed. Delete this file to get the defaults back on next run.\n")


def _ensure_line_files():
    """Write the default line files (once) so they're there to edit or replace."""
    try:
        os.makedirs(_LINES_DIR, exist_ok=True)
        for name, var in _LINE_FILES.items():
            p = os.path.join(_LINES_DIR, name + ".txt")
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(_LINE_HEADER.format(name=name))
                    if name in _LINE_PLACEHOLDERS:
                        f.write("# Available placeholders (keep the curly braces): "
                                + _LINE_PLACEHOLDERS[name] + "\n")
                    f.write("\n".join(globals()[var]) + "\n")
    except Exception as e:
        add_log(f"couldn't write digest line files: {e}")


def _fmt_line(name, **fields):
    """Random line from a pool, with {placeholders} filled. A template with a bad
    placeholder falls back to a plain factual line instead of crashing."""
    tpl = random.choice(_load_lines(name))
    try:
        return tpl.format(**fields)
    except (KeyError, IndexError, ValueError):
        add_log(f"digest_lines/{name}.txt has a bad placeholder in: {tpl[:60]!r} — used plain wording")
        return _LINE_SAFE.get(name, tpl).format(**fields)


def _load_lines(name):
    """Load one pool from its file; fall back to the built-ins if missing/empty."""
    fallback = globals()[_LINE_FILES[name]]
    try:
        p = os.path.join(_LINES_DIR, name + ".txt")
        if not os.path.exists(p):
            _ensure_line_files()
        with open(p, "r", encoding="utf-8-sig") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip() and not ln.strip().startswith("#")]
        return lines or fallback
    except Exception:
        return fallback



DIGEST_ICON = {"novel": "📖", "audiobook": "🎧", "both": "📖🎧"}
DIGEST_KIND_WORD = {"novel": "novel", "audiobook": "audiobook", "both": "novel + audiobook"}


def _merged_upcoming_rows():
    """Digest rows, soonest first, with a day-and-date novel+audiobook pair for
    the SAME book folded into one kind='both' row — two lines for one release
    event read like a stutter. Only confirmed dates merge; an estimate never
    hides inside a real row (its ~ marking must stay visible)."""
    rows = upcoming_of_kind("novel", True) + upcoming_of_kind("audiobook", True)
    rows.sort(key=lambda r: (r["date"], r["kind"]))
    merged, seen = [], {}
    for r in rows:
        key = (r["series_id"], r.get("book_n"), r["date"])
        mergeable = r.get("book_n") is not None and not r["estimate"]
        prev = seen.get(key)
        if mergeable and prev is not None and prev["kind"] != r["kind"]:
            prev["kind"] = "both"
            continue
        if mergeable:
            r = dict(r)             # don't mutate the shared upcoming row
            seen[key] = r
        merged.append(r)
    # A confirmed novel row also answers the reader's next question: when's the
    # audio? Real announced date if the book has one (a same-day pair already
    # merged above), the gap model's expectation otherwise, or an honest "not
    # announced". ('real'/'est'/'none', iso_date_or_None)
    with _lock:
        by_id = {s["id"]: s for s in _state["series"]}
    for r in merged:
        if r["kind"] != "novel" or r["estimate"]:
            continue
        s = by_id.get(r["series_id"])
        if not s:
            continue
        ent = next((e for e in s.get("timeline", [])
                    if _entry_n(e) == r.get("book_n")), None)
        ad = parse_date(ent.get("audiobook")) if ent else None
        if ad:
            r["audio_follow"] = ("real", iso(ad))
        else:
            # take THIS book's row out of the same schedule the rest of the app
            # uses — recomputing it from the novel date alone would reintroduce
            # the very estimate the queue model exists to correct
            proj = next((x for x in _audio_schedule_usable(s)
                         if x["n"] == r.get("book_n")), None)
            r["audio_follow"] = ("est", iso(proj["date"])) if proj else ("none", None)
    return merged


def _audio_follow_text(r):
    """One short clause describing a novel row's audiobook outlook, or ''."""
    kind, dt = r.get("audio_follow") or (None, None)
    if kind == "real":
        return f"🎧 audiobook {_discord_ts(dt, 'D')}"
    if kind == "est":
        return f"🎧 audio expected ~ {_discord_ts(dt, 'D')}"
    if kind == "none":
        return "🎧 no audiobook announced yet"
    return ""


def _daily_digest_text():
    """The forecast message: both kinds, estimates included (marked ~), soonest first."""
    rows = _merged_upcoming_rows()
    if not rows:
        return random.choice(_load_lines("empty"))
    shown = rows[:5]
    lines = [random.choice(_load_lines("openers")), ""]
    for r in shown:
        ic = DIGEST_ICON.get(r["kind"], "📚")
        kw = DIGEST_KIND_WORD.get(r["kind"], r["kind"])
        if r["estimate"]:
            c = r.get("confidence") or {}
            spread = f" ±{c['spread_days']}d" if c.get("spread_days") is not None else ""
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            lines.append(f"{ic} ~ **{r['series']}**{bk} — {kw}, my guess: around {_discord_ts(r['date'], 'D')}{spread}")
        elif r["countdown_days"] == 0:
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            af = _audio_follow_text(r)
            lines.append(f"{ic} **{r['series']}**{bk} — {kw} **TODAY**" + (f" · {af}" if af else ""))
        else:
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            af = _audio_follow_text(r)
            lines.append(f"{ic} **{r['series']}**{bk} — {kw} {_discord_ts(r['date'], 'R')} ({_discord_ts(r['date'], 'D')})"
                         + (f" · {af}" if af else ""))
    if len(rows) > len(shown):
        lines.append(_fmt_line("more", count=len(rows) - len(shown)))
    lines.append("")
    if any(r["countdown_days"] == 0 for r in shown if not r["estimate"]):
        lines.append(random.choice(_load_lines("today")))
    elif any(0 < r["countdown_days"] <= 3 for r in shown if not r["estimate"]):
        lines.append(random.choice(_load_lines("soon")))
    if any(r["estimate"] for r in shown):
        lines.append(random.choice(_load_lines("estimates")))
    lines.append(random.choice(_load_lines("closers")))
    return "\n".join(lines)


def _daily_digest_payload():
    """(content, embeds) for the rich digest: tsundere opener as the message text,
    each release as an embed with its cover art, closing remarks in a final embed.
    The plain _daily_digest_text stays as the degrade path if Discord rejects it."""
    rows = _merged_upcoming_rows()
    if not rows:
        return random.choice(_load_lines("empty")), []
    shown = rows[:5]
    embeds = []
    for r in shown:
        ic = DIGEST_ICON.get(r["kind"], "📚")
        bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
        title = f"{'~ ' if r['estimate'] else ''}{ic} {r['series']}{bk} — {DIGEST_KIND_WORD.get(r['kind'], r['kind'])}"
        if r["estimate"]:
            c = r.get("confidence") or {}
            spread = f" ±{c['spread_days']}d" if c.get("spread_days") is not None else ""
            desc = f"my guess: around {_discord_ts(r['date'], 'D')}{spread}"
            if r.get("note"):
                desc += f"\n{r['note']}"
        elif r["countdown_days"] == 0:
            desc = "**TODAY**"
        else:
            desc = f"{_discord_ts(r['date'], 'R')} ({_discord_ts(r['date'], 'D')})"
        af = _audio_follow_text(r)
        if af:
            desc += f"\n{af}"
        e = {"title": title[:256], "description": desc,
             "color": KIND_COLOR.get(r["kind"], KIND_COLOR["audiobook"])}
        if r.get("cover"):
            e["thumbnail"] = {"url": r["cover"]}
        embeds.append(e)
    tail = []
    if len(rows) > len(shown):
        tail.append(_fmt_line("more", count=len(rows) - len(shown)))
    if any(r["countdown_days"] == 0 for r in shown if not r["estimate"]):
        tail.append(random.choice(_load_lines("today")))
    elif any(0 < r["countdown_days"] <= 3 for r in shown if not r["estimate"]):
        tail.append(random.choice(_load_lines("soon")))
    if any(r["estimate"] for r in shown):
        tail.append(random.choice(_load_lines("estimates")))
    tail.append(random.choice(_load_lines("closers")))
    embeds.append({"description": "\n".join(tail), "color": 0x332C47})
    return random.choice(_load_lines("openers")), embeds


def _send_digest(webhook):
    """Send the rich digest, degrading to the full plain-text version if Discord
    rejects the embeds. Returns (ok, error_str)."""
    content, embeds = _daily_digest_payload()
    return _discord_post(webhook, content, embeds, fallback_text=_daily_digest_text())


PLACEHOLDER_SAT = 9.5     # mean px saturation below this = "cover to be revealed"


def _cover_bytes_look_placeholder(data):
    """True when the image looks like a publisher's 'cover to be revealed' card.
    Those are near-greyscale; real covers have color. Measured on the live
    library (5 placeholders vs 15 real covers): placeholders' mean saturation
    topped out at 7.3, the most muted real cover sat at 12.5 — the 9.5 line
    splits them with margin. A genuinely greyscale real cover would false-
    positive, which only means the card shows the previous book's art."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64))
        px = list(im.getdata())
        sat = sum(max(c) - min(c) for c in px) / len(px)
        return sat < PLACEHOLDER_SAT
    except Exception:
        return False


def _cover_is_placeholder(url):
    """Cached placeholder check for a cover URL. Fetches (outside the state
    lock) at most once per URL; on any fetch/decode failure the answer is False
    — never block a real cover on a network hiccup."""
    if not url:
        return False
    with _lock:
        cache = _state.setdefault("cover_checks", {})
        if url in cache:
            return cache[url]
    try:
        verdict = _cover_bytes_look_placeholder(fetch_bytes(url))
    except Exception:
        verdict = False
    with _lock:
        cache = _state.setdefault("cover_checks", {})
        cache[url] = verdict
        while len(cache) > 300:
            cache.pop(next(iter(cache)))
    return verdict


RELEASE_PING_GRACE_DAYS = 3   # 'out now' may fire this many days after the date
EXT_QUIET_ALERT_H = 6         # extension silent this long -> one Discord warning
UNCONFIRMED_MIN_DAYS = 3      # after the ping grace expires...
UNCONFIRMED_MAX_DAYS = 7      # ...but before it's old news: flag silent releases

# A healthy extension polls /api/ext-commands every ~20 s. If those polls stop,
# nothing reads Amazon any more and every feature silently starves (this went
# unnoticed for two days in Aug 2026). In-memory only: an app restart resets
# the clock, which is correct - the app can't know how long it was down.
_ext_last_poll = datetime.now()
_ext_alerted = False


def _ext_seen():
    global _ext_last_poll, _ext_alerted
    _ext_last_poll = datetime.now()
    if _ext_alerted:
        _ext_alerted = False
        add_log("extension is back")
        # edits the warning into the all-clear; gone an hour later
        _alert_resolve("ext_silent", _fmt_line("recovered", what="The browser extension is checking in again."))


def _maybe_report_unconfirmed_releases():
    """The 'out now' ping depends on the extension reading the page as buyable
    around release day. When that read never happens (detector blind spot,
    extension dead, browser closed), a release used to pass in permanent
    silence — RE: Maelstrom 2 (10 Aug 2026) was only noticed two weeks late.
    Once the grace window closes, say so ONCE per book: due, never confirmed.
    The 7-day upper bound keeps back-catalogue and long outages quiet."""
    lines = []
    with _lock:
        notified = set(_state.get("discord_notified", []))
        for s2 in _state.get("series", []):
            for e in s2.get("timeline", []):
                n = _entry_n(e)
                if n is None:
                    continue
                for dkey, pkey, kind in (("audiobook", "audio_pre", "audiobook"),
                                          ("novel", "novel_pre", "novel")):
                    dd = parse_date(e.get(dkey))
                    if not dd:
                        continue
                    age = (today() - dd).days
                    if not (UNCONFIRMED_MIN_DAYS < age <= UNCONFIRMED_MAX_DAYS):
                        continue
                    if e.get(pkey) is False:
                        continue                     # confirmed buyable: the normal path owns it
                    rel_key = f"release:{s2['id']}:{n}:{kind}:{e.get(dkey)}"
                    key = f"unconfirmed:{s2['id']}:{n}:{kind}:{e.get(dkey)}"
                    if rel_key in notified or key in notified:
                        continue
                    notified.add(key)
                    lines.append(f"**{s2.get('name', '')}** book {n} {kind} — due {e.get(dkey)}, "
                                 f"never seen buyable")
        if lines:
            _state["discord_notified"] = list(notified)[-500:]
            save_state()
    if lines and get_settings().get("discord_on_release", True):
        _discord_notify("🔎 Due but never confirmed live — worth checking the store page"
                        " (the page may be unreadable, or the extension was down):\n" +
                        "\n".join(lines[:8]))


def _ext_watchdog():
    global _ext_alerted
    quiet = datetime.now() - _ext_last_poll
    if not _ext_alerted and quiet > timedelta(hours=EXT_QUIET_ALERT_H):
        _ext_alerted = True
        h = int(quiet.total_seconds() // 3600)
        add_log(f"extension has been silent for {h} h")
        _alert_post("ext_silent", _fmt_line("watchdog",
                                            what=f"The browser extension hasn't checked in for {h} hours — no release dates are being read.",
                                            detail="Is Floorp open, and is the Release Radar add-on enabled? Reinstalling the .xpi over the top fixes a dead add-on."))


# ---- problems: things only a person can fix ------------------------------
# Every scheduled job used to fail into the in-app activity log and nowhere
# else: the nightly AI research died for four nights in Sep 2026 (the Claude
# CLI had signed itself out) and nobody heard. A problem is raised by key,
# told to Discord ONCE, repeated daily while it persists, and announced when it
# clears. Persisted, so a restart neither re-alerts nor forgets.
PROBLEM_REMIND_H = 24


def _problem(key, what, detail="", notify=True):
    """Raise (or refresh) a problem. Returns True when it was announced."""
    what, detail = _no_secrets(what), _no_secrets(detail)      # this text travels far (tunnel included)
    now = datetime.now()
    stamp = now.isoformat(timespec="seconds")
    with _lock:
        probs = _state.setdefault("problems", {})
        p = probs.get(key)
        if p:
            p["what"], p["detail"] = what, detail
            p["count"] = int(p.get("count") or 0) + 1
            try:
                recent = now - datetime.fromisoformat(p.get("last_alert") or "") < timedelta(hours=PROBLEM_REMIND_H)
            except ValueError:
                recent = False
            if recent:
                return False
            p["last_alert"] = stamp
        else:
            probs[key] = {"what": what, "detail": detail, "count": 1, "since": stamp, "last_alert": stamp}
        save_state()
    add_log(f"PROBLEM ({key}): {what} {detail}".rstrip())
    if notify:
        _alert_post(f"problem:{key}", _fmt_line("watchdog", what=what, detail=detail))
    return True


def _problem_clear(key, what=None, notify=True):
    """Drop a problem; announces the all-clear only if it was actually open."""
    with _lock:
        p = (_state.get("problems") or {}).pop(key, None)
        if p:
            save_state()
    if not p:
        return False
    add_log(f"resolved ({key}): {what or p.get('what', '')}")
    # the warning becomes the all-clear and disappears an hour later; a silent
    # clear (notify=False) still takes the stale warning down, just posts nothing new
    _alert_resolve(f"problem:{key}", _fmt_line("recovered", what=what or (p.get("what", "") + " That's fixed now.")),
                   fallback=notify)
    return True


def _problems_list():
    with _lock:
        probs = _state.get("problems") or {}
        return [dict(v, key=k) for k, v in sorted(probs.items(), key=lambda kv: kv[1].get("since", ""))]


# A polling extension that never reads a page is as blind as a dead one, and
# the poll watchdog above can't see it: Sep 2026 had three days of healthy
# 20-second polls and not one Amazon read (the daily refresh only ever ran
# when Floorp was restarted). Self-heal first — ask for a forced refresh — and
# if that changes nothing, say so.
READ_QUIET_NUDGE_H = 30       # the refresh runs daily; this much silence is abnormal
READ_QUIET_ALERT_H = 6        # a nudge that yields nothing within this -> Discord
_read_nudged_at = None


def _read_watchdog():
    global _read_nudged_at
    now = datetime.now()
    if now - _ext_last_poll > timedelta(minutes=10):
        # not polling at all: _ext_watchdog owns that story - and time spent
        # away must not count against a nudge it never received
        _read_nudged_at = None
        return
    with _lock:
        if not _state.get("series"):
            return
        # last_read is stamped by every accepted page read; last_ingest only by
        # reads that CHANGED something, which a healthy quiet library never does
        last = _state.get("last_read") or _state.get("last_ingest") or ""
        pending = any(c.get("type") == "refresh" for c in _state.get("ext_commands", []))
        open_problem = "no_reads" in (_state.get("problems") or {})
    try:
        quiet = now - datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return                                  # never ingested anything: nothing to compare against
    if quiet < timedelta(hours=READ_QUIET_NUDGE_H):
        if _read_nudged_at is not None or open_problem:
            _read_nudged_at = None
            _problem_clear("no_reads", what="The extension is reading Amazon pages again.")
        return
    h = int(quiet.total_seconds() // 3600)
    if _read_nudged_at is None or now - _read_nudged_at >= timedelta(hours=24):
        _read_nudged_at = now
        if not pending:
            _push_command(type="refresh")
        add_log(f"no page reads for {h} h although the extension is polling — asked it for a forced refresh")
        return
    if now - _read_nudged_at >= timedelta(hours=READ_QUIET_ALERT_H):
        _problem("no_reads",
                 what=f"The extension is alive but hasn't read a single Amazon page in {h} hours, and a forced refresh changed nothing.",
                 detail="Open the Release Radar add-on popup: read its log, try 'Reset if stuck', or restart Floorp. "
                        "Release days can't be detected until it reads pages again.")


COVERFILL_CAP = 20        # backfill page-reads per round; the extension paces them
COVERFILL_EVERY_HOURS = 24  # cover art is cosmetic — trickle it, don't hammer
COVERFILL_MAX_TRIES = 3   # after this many reads with no yield, the page simply
                          # doesn't carry the field — stop spending reads on it
COVERFILL_RETRY_DAYS = 14 # backoff between repeat attempts at the same page


def _maybe_queue_cover_backfill():
    """Once a day, hand the extension a bounded list of book pages to re-read so
    back-catalogue entries pick up their own cover art (and audiobooks their
    narrator) — routine crawls skip settled books, so these fields would
    otherwise stay empty forever on books tracked before per-book capture
    existed. Uses only the extension's normal logged-in page reads."""
    with _lock:
        # Don't stack: if the last list is still waiting for the extension
        # (Firefox closed, worker busy), pushing another would replay near-
        # identical forced crawls back-to-back when it returns.
        if any(c.get("type") == "coverfill" for c in _state.get("ext_commands", [])):
            return
        # Once a day. This is cosmetic backfill of old books, so it rides along
        # with the extension's own daily refresh rather than adding rounds of its
        # own — at COVERFILL_CAP a round it still drains a full library in days.
        last = _state.get("coverfill_last")
        if last:
            try:
                if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=COVERFILL_EVERY_HOURS):
                    return
            except ValueError:
                pass
        urls = []
        for s in _state["series"]:
            host = s.get("store_host")
            if not host:
                continue
            a_nov = s.get("asins_novel") or {}
            a_all = s.get("asins") or {}
            a_aud = s.get("asins_audio") or {}
            for e in s.get("timeline", []):
                n = _entry_n(e)
                if n is None:
                    continue
                key = str(n)
                if not e.get("cover"):
                    asin = a_nov.get(key) or a_all.get(key)
                    if asin:
                        urls.append(f"https://{host}/dp/{asin}")
                if e.get("audiobook") and not (e.get("narrator") or "").strip():
                    asin = a_aud.get(key)
                    if asin:
                        urls.append(f"https://{host}/dp/{asin}")
        candidates = list(dict.fromkeys(urls))
        now = datetime.now()
        tried = _state.setdefault("coverfill_tried", {})
        # forget tracking for pages that are no longer candidates (field filled,
        # or the book/series is gone) so the map stays small and honest
        for u in list(tried):
            if u not in candidates:
                del tried[u]

        def _eligible(u):
            e = tried.get(u)
            if not e:
                return True
            if e.get("n", 0) >= COVERFILL_MAX_TRIES:
                return False
            try:
                t = datetime.fromisoformat(e.get("t", ""))
            except ValueError:
                return True
            return now - t >= timedelta(days=COVERFILL_RETRY_DAYS)

        # never-tried pages first so the tail of a long list can't be starved by
        # head-of-list pages that keep yielding nothing; retries queue behind
        fresh = [u for u in candidates if u not in tried]
        retry = [u for u in candidates if u in tried and _eligible(u)]
        urls = (fresh + retry)[:COVERFILL_CAP]
        _state["coverfill_last"] = now.isoformat(timespec="seconds")
        if not urls:
            save_state()
            return
        for u in urls:
            e = tried.setdefault(u, {"n": 0})
            e["n"] = e.get("n", 0) + 1
            e["t"] = now.isoformat(timespec="seconds")
    _push_command(type="coverfill", urls=urls)
    add_log(f"Asked the extension to backfill covers/narrators for {len(urls)} book page(s)")


def _maybe_fix_placeholder_covers():
    """Once a day: a card whose cover is a 'to be revealed' placeholder falls
    back to the newest book that has real art (per-book covers arrive via the
    backfill). The revealed cover replaces it automatically later through the
    normal newest-book ingest path."""
    with _lock:
        if _state.get("coverfix_last") == today().isoformat():
            return
        _state["coverfix_last"] = today().isoformat()
        work = [(s, s.get("cover_url"),
                 [e.get("cover") for e in sorted(s.get("timeline", []),
                                                 key=lambda e: _entry_n(e) or 0, reverse=True)
                  if e.get("cover")])
                for s in _state["series"] if s.get("cover_url")]
    changed = 0
    for s, url, cands in work:
        if not _cover_is_placeholder(url):
            continue
        best = next((c for c in cands if c != url and not _cover_is_placeholder(c)), None)
        if best:
            with _lock:
                s["cover_url"] = best
            changed += 1
            add_log(f"'{s.get('name', '?')}': cover not revealed yet — showing the previous book's art")
    if changed:
        with _lock:
            save_state()


def _maybe_send_daily_digest():
    st = get_settings()
    wh = (st.get("discord_webhook") or "").strip()
    if not st.get("discord_daily") or not wh:
        return
    if datetime.now().hour < int(st.get("discord_daily_hour", 9)):
        return
    every = int(st.get("discord_digest_days", 1) or 1)
    ts = today()
    with _lock:
        last = parse_date(_state.get("discord_daily_last") or "")
        if last and (ts - last).days < every:
            return
        _state["discord_daily_last"] = ts.isoformat()    # mark first so a race can't double-send
        save_state()
    ok, err = _send_digest(wh)
    if not ok:
        add_log(f"Daily digest failed: {err}")
        _problem("digest", what=f"The forecast digest didn't send ({err}).",
                 detail="Check the webhook in Settings → Discord.")
    else:
        add_log("Sent the daily forecast digest to Discord")
        _problem_clear("digest", what="The forecast digest is sending again.")


_digest_thread_started = False


def _start_daily_digest():
    global _digest_thread_started
    if _digest_thread_started:
        return
    _digest_thread_started = True

    def _loop():
        time.sleep(30)
        while True:
            try:
                _maybe_send_daily_digest()
            except Exception as e:
                add_log(f"daily digest error: {e}")
            try:
                _maybe_queue_cover_backfill()
            except Exception as e:
                add_log(f"cover backfill error: {e}")
            try:
                _maybe_fix_placeholder_covers()
            except Exception as e:
                add_log(f"placeholder cover fix error: {e}")
            try:
                _buy_tick()
            except Exception as e:
                add_log(f"auto-buy tick error: {e}")
            try:
                _ext_watchdog()
            except Exception as e:
                add_log(f"extension watchdog error: {e}")
            try:
                _read_watchdog()
            except Exception as e:
                add_log(f"read watchdog error: {e}")
            try:
                _discord_tidy_tick()
            except Exception as e:
                add_log(f"Discord tidy error: {e}")
            try:
                _maybe_report_unconfirmed_releases()
            except Exception as e:
                add_log(f"unconfirmed-release check error: {e}")
            try:
                _refresh_ownership()
            except Exception as e:
                add_log(f"ownership scan error: {e}")
            try:
                _release_card_prune()
            except Exception:
                pass
            try:
                _abs_refresh_progress()
            except Exception as e:
                add_log(f"Audiobookshelf progress error: {e}")
            try:
                _abs_sync_status()
            except Exception as e:
                add_log(f"Audiobookshelf status write error: {e}")
            try:
                _ai_tick()
            except Exception as e:
                add_log(f"AI research tick error: {e}")
            time.sleep(300)   # check every 5 minutes

    threading.Thread(target=_loop, daemon=True).start()


def build_state_payload():
    with _lock:
        views = [series_view(s) for s in _state["series"]]
        last_check = _state.get("last_check")
        log_rows = list(_state["log"])[:20]
    mode, est = _upcoming_settings()
    up = upcoming_of_kind(mode, est)
    return {
        "app_name": APP_NAME,
        "last_check": last_check,
        "last_ingest": _state.get("last_ingest"),
        "series": views,
        "upcoming": up[:FEED_ITEM_COUNT],
        "all_upcoming": up,
        "overdue": overdue_audiobooks(),
        "upcoming_mode": mode,
        "show_estimates": get_settings().get("show_estimates", True),
        "show_estimates_novel": get_settings().get("show_estimates_novel", True),
        "log": log_rows,
        "problems": _problems_list(),
        "counts": {
            "total": len(views),
            "out": sum(1 for v in views if v["status_class"] == "released"),
            "announced": sum(1 for v in views if v["status_class"] == "announced"),
            "waiting": sum(1 for v in views if v["status_class"] in ("none", "unknown")),
        },
    }


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------

def build_rss(base_url):
    mode, est = _upcoming_settings()
    items = upcoming_of_kind(mode, est)[:FEED_ITEM_COUNT]
    kind_word = "novel" if mode == "novel" else "audiobook"
    now = format_datetime(datetime.now().astimezone())
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{html.escape(APP_NAME)} - Next {FEED_ITEM_COUNT} releases</title>",
        f"<link>{html.escape(base_url)}</link>",
        f"<description>Upcoming {kind_word} releases you are tracking.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        d = parse_date(it["date"])
        pub = format_datetime(datetime(d.year, d.month, d.day).astimezone()) if d else now
        kind = "Audiobook" if it["kind"] == "audiobook" else "Novel"
        est = " (estimated)" if it["estimate"] else ""
        title = f"{it['title']} - {kind} - {it['date']}{est}"
        desc = (
            f"{html.escape(it['series'])}"
            + (f" by {html.escape(it['author'])}" if it['author'] else "")
            + f". {kind} releasing {it['date']}{est}. "
            + f"{it['countdown_days']} day(s) to go."
        )
        guid = f"{it['series_id']}-{it['date']}-{it['kind']}-b{it.get('book_n')}"
        parts += [
            "<item>",
            f"<title>{html.escape(title)}</title>",
            f"<description>{desc}</description>",
            f"<pubDate>{pub}</pubDate>",
            f'<guid isPermaLink="false">{html.escape(guid)}</guid>',
            "</item>",
        ]
    parts.append("</channel></rss>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# iCalendar feed — subscribe from any calendar app (phone included, via the
# tunnel). REAL releases only, both kinds, as all-day events — a calendar entry
# reads as a commitment, so estimates stay on the dashboard/digest instead.
# ---------------------------------------------------------------------------

def _ics_escape(s):
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line):
    """RFC 5545 §3.1 line folding: content lines SHOULD stay within 75 octets;
    continuation lines start with a single space. Folds on UTF-8 byte length
    without ever splitting inside a multi-byte character (emoji in summaries)."""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    out, cur, cur_len = [], [], 0
    limit = 75
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_len + b > limit:
            out.append("".join(cur))
            cur, cur_len = [" ", ch], 1 + b
        else:
            cur.append(ch)
            cur_len += b
    if cur:
        out.append("".join(cur))
    return out


def build_ics():
    rows = [r for r in upcoming_of_kind("novel", True) + upcoming_of_kind("audiobook", True)
            if not r["estimate"]]
    rows.sort(key=lambda r: (r["date"], r["kind"]))
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Release Radar//local//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Release Radar",
        "X-WR-CALDESC:Upcoming novel & audiobook releases",
    ]
    for r in rows:
        d = parse_date(r["date"])
        if not d:
            continue
        icon = "🎧" if r["kind"] == "audiobook" else "📖"
        summary = f"{icon} {r['title']} — {r['kind']}"
        desc_bits = [r["series"]]
        if r.get("author"):
            # scraped bylines sometimes keep their own "by " prefix
            desc_bits.append("by " + re.sub(r"^by\s+", "", r["author"], flags=re.I))
        # book_n is part of the key: one series can release two same-kind books on
        # one day, and calendar clients dedupe by UID — a collision loses an event
        uid = f"{r['series_id']}-{r['date']}-{r['kind']}-b{r.get('book_n')}@release-radar"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(' · '.join(desc_bits))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fl for ln in lines for fl in _ics_fold(ln)) + "\r\n"


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Changes on every app start. Open dashboard tabs compare it each refresh and
# reload themselves, so UI fixes reach the page instead of running stale JS.
APP_BUILD = datetime.now().strftime("%Y%m%d%H%M%S")

# Read-only surfaces intended to be embedded elsewhere (a new-tab widget, a feed
# reader, a calendar app). None of them expose a credential.
PUBLIC_READ_PATHS = {"/api/state", "/feed.xml", "/calendar.ics", "/board"}


def _origin_kind(origin):
    """Classify a request's Origin: none | local | extension | web."""
    if not origin:
        return "none"                      # direct fetch, the extension, curl
    try:
        u = urlparse(origin)
    except Exception:
        return "web"
    if u.scheme in ("moz-extension", "chrome-extension", "safari-web-extension"):
        return "extension"
    if u.hostname in ("127.0.0.1", "localhost", "::1"):
        return "local"
    return "web"                           # a real website — treat as hostile


def _is_own_origin(origin):
    """The app's own public origin: buy_public_base, or the host this request
    was addressed to. The approval page posts back to itself over the tunnel,
    and the browser stamps that POST with the public hostname as its Origin,
    which _origin_kind calls "a real website" (29 Sep 2026: Approve answered
    'cross-site request refused'). Only consulted for the token-gated /buy/
    routes; a foreign page can't forge Origin, and the token is still needed."""
    try:
        o = urlparse(origin or "")
    except Exception:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    own = set()
    base = (get_settings().get("buy_public_base") or "").strip()
    if base:
        own.add(urlparse(base).netloc.lower())
    if request.host:
        own.add(request.host.lower())
    return o.netloc.lower() in own


def _is_remote():
    """True when the request did NOT come from this machine - i.e. it arrived
    through the Cloudflare tunnel (Cf-* headers) or was addressed to any host
    other than loopback. The phone view is served this way."""
    h = request.headers
    if h.get("Cf-Connecting-Ip") or h.get("Cf-Ray") or h.get("X-Forwarded-For"):
        return True
    host = (request.host or "").strip().lower()
    if host.startswith("["):                       # [::1]:8765
        host = host[1:host.find("]")] if "]" in host else host
    else:
        host = host.split(":")[0]
    return host not in ("127.0.0.1", "localhost", "::1", "")


@app.before_request
def _refuse_cross_site_writes():
    """Two gates.

    1. A foreign web page must not be able to change anything here (cross-site
       POSTs are sent and executed even when the reply is unreadable).
    2. Anything reaching the app from OFF this machine - the Cloudflare tunnel
       that serves the phone - is read-only. The tunnel hostname has no login
       in front of it, so without this, anyone who learned the hostname could
       post to /api/quit or /api/series/clear, and GET /api/settings handed out
       the Discord webhook (it is masked for remote readers below)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if _origin_kind(origin) == "web" and not (request.path.startswith("/buy/") and _is_own_origin(origin)):
        add_log(f"refused a cross-site {request.method} to {request.path}")
        return jsonify({"error": "cross-site request refused"}), 403
    if _is_remote() and not request.path.startswith("/buy/"):
        # /buy/<token>/... is gated by its single-use token instead
        add_log(f"refused a remote {request.method} to {request.path} "
                f"(from {request.headers.get('Cf-Connecting-Ip') or request.remote_addr})")
        return jsonify({"error": "read-only over the tunnel - make changes on the PC"}), 403
    return None


@app.after_request
def allow_cors(resp):
    """Cross-origin rules.

    This used to answer every request with `Access-Control-Allow-Origin: *`,
    which meant ANY page you happened to visit could read http://127.0.0.1:8477
    — including /api/settings, which hands back the Discord webhook — and could
    POST to /api/series/clear or /api/quit to wipe the library or kill the app.
    Firefox does not block private-network requests, so that was live.

    Now: the browser extension and pages served from this app get full access;
    the handful of read-only surfaces meant for embedding stay public because
    they carry no credentials; everything else answers with no CORS header at
    all, so a foreign page cannot read the response."""
    origin = request.headers.get("Origin")
    kind = _origin_kind(origin)
    if kind in ("extension", "local"):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    elif request.path in PUBLIC_READ_PATHS:
        # the new-tab widget may run from an opaque origin; none of these
        # responses contain a secret
        resp.headers["Access-Control-Allow-Origin"] = "*"
    # Never let Cloudflare (or a browser) cache API answers or the dashboard:
    # the tunnel was found serving a 4-hour-old /api/settings from its edge.
    if request.path == "/" or request.path.startswith("/api/") or request.path.startswith("/buy/"):
        resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.get("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#15131f'/>"
    "<circle cx='32' cy='32' r='22' fill='none' stroke='#e7b14c' stroke-width='4'/>"
    "<circle cx='32' cy='32' r='10' fill='none' stroke='#4a4067' stroke-width='3'/>"
    "<circle cx='32' cy='32' r='4.5' fill='#e7b14c'/>"
    "</svg>"
)


@app.get("/api/instance")
def api_instance():
    """Identity marker so a second launch can tell this port is already us."""
    return jsonify({"app": "release-radar", "pid": os.getpid()})


@app.get("/favicon.svg")
def favicon_svg():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.get("/api/state")
def api_state():
    p = build_state_payload()
    p["app_build"] = APP_BUILD
    p["ext_seen_s"] = int((datetime.now() - _ext_last_poll).total_seconds())
    return jsonify(p)


@app.post("/api/series")
def api_add():
    body = request.get_json(force=True, silent=True) or {}
    # Normalize timeline rows to ISO on the way in, exactly like PUT does — raw
    # hand-typed dates ("Aug 25, 2026") parse fine server-side but are invisible
    # to the dashboard JS, which reads timeline dates as strict ISO.
    tl = _clean_timeline(body.get("timeline"))
    s = {
        "id": uuid.uuid4().hex[:12],
        "name": (body.get("name") or "").strip(),
        "author": (body.get("author") or "").strip(),
        "url": (body.get("url") or "").strip(),
        "novel_title": (body.get("novel_title") or "").strip(),
        "novel_date": iso(parse_date(body.get("novel_date"))),
        "audiobook_date": iso(parse_date(body.get("audiobook_date"))),
        "audiobook_status": body.get("audiobook_status") or "unknown",
        "cover_url": (body.get("cover_url") or "").strip(),
        "history": _history_from_timeline(tl) if tl else _clean_history(body.get("history")),
        "timeline": tl,
        "notes": (body.get("notes") or "").strip(),
        "last_fetched": None,
        "last_change": None,
        "fetch_error": None,
        "buy_mode": _clean_buy_mode(body.get("buy_mode")),
        "buy_cap": _clean_buy_cap(body.get("buy_cap")),
    }
    if not s["name"]:
        return jsonify({"error": "A series or book name is required."}), 400
    with _lock:
        _state["series"].append(s)
    save_state()
    add_log(f"Added: {s['name']}")
    return jsonify(series_view(s))


@app.put("/api/series/<sid>")
def api_update(sid):
    body = request.get_json(force=True, silent=True) or {}
    with _lock:
        s = _find(sid)
        if not s:
            return jsonify({"error": "Not found."}), 404
        for key in ("name", "author", "url", "novel_title", "cover_url", "notes", "audiobook_status"):
            if key in body:
                s[key] = (body.get(key) or "").strip() if isinstance(body.get(key), str) else body.get(key)
        if "novel_date" in body:
            s["novel_date"] = iso(parse_date(body.get("novel_date")))
        if "audiobook_date" in body:
            s["audiobook_date"] = iso(parse_date(body.get("audiobook_date")))
        if "buy_mode" in body:
            s["buy_mode"] = _clean_buy_mode(body.get("buy_mode"))
        if "buy_cap" in body:
            s["buy_cap"] = _clean_buy_cap(body.get("buy_cap"))
        if "timeline" in body:
            tl = _clean_timeline(body.get("timeline"), s.get("timeline"))   # rows of {n, novel, audiobook}
            s["timeline"] = tl
            s["history"] = _history_from_timeline(tl)
            s["edited"] = True          # your dates win; new info still merges in by number
        elif "history" in body:
            s["history"] = _clean_history(body.get("history"))
    save_state()
    return jsonify(series_view(s))


@app.delete("/api/series/<sid>")
def api_delete(sid):
    with _lock:
        victim = next((x for x in _state["series"] if x["id"] == sid), None)
        before = len(_state["series"])
        _state["series"] = [x for x in _state["series"] if x["id"] != sid]
        removed = before != len(_state["series"])
    if removed:
        save_state()
        if victim:
            asins = list((victim.get("asins") or {}).values())
            _push_command(type="forget", series=victim.get("name", ""), asins=asins)
        add_log("Removed a tracked series (asked the extension to forget it too).")
    return jsonify({"ok": removed})


@app.post("/api/reorder")
def api_reorder():
    body = request.get_json(force=True, silent=True) or {}
    ids = body.get("ids") or []
    with _lock:
        index = {s["id"]: s for s in _state["series"]}
        new = [index[i] for i in ids if i in index]
        for s in _state["series"]:           # keep any not in the list, at the end
            if s["id"] not in ids:
                new.append(s)
        _state["series"] = new
    save_state()
    return jsonify({"ok": True})


def _match_tracked_series(name):
    """Find a tracked series by name (exact-normalised or strong containment)."""
    want = _norm(name)
    if not want:
        return None
    for s in _state["series"]:
        sn = _norm(s.get("name") or "")
        if sn and (sn == want or want in sn or sn in want):
            return s
    return None


@app.route("/api/ingest", methods=["POST", "OPTIONS"])
def api_ingest():
    """Accept one book read from an Amazon page (via the extension) and
    fold it into the matching tracked series by book number. Amazon is treated as
    authoritative for the field it reports, so it sets that book's novel/audiobook
    date directly; everything else is left alone. Creates the series if it's new."""
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    series = (body.get("series") or "").strip()
    author = (body.get("author") or "").strip()
    book = body.get("book") or {}
    try:
        n = int(book.get("n"))
    except (TypeError, ValueError):
        n = None
    nd = iso(parse_date(book.get("novel_date")))
    ad = iso(parse_date(book.get("audiobook_date")))
    cover = (book.get("cover") or "").strip() or None
    # Backfill reads exist to collect aux data (cover/narrator) from old pages —
    # they FILL missing dates but never overwrite existing ones, so a hand-fixed
    # date on a settled book can't be reverted by the daily backfill pass.
    backfill = bool(body.get("_backfill"))
    # network check happens here, BEFORE the state lock (cached per URL)
    cover_is_ph = _cover_is_placeholder(cover) if cover else False
    if not series or n is None or (not nd and not ad):
        return jsonify({"ok": False, "message": "Need a series, a book number, and a date."})
    with _lock:
        _state["last_read"] = datetime.now().isoformat(timespec="seconds")   # a page was read, changed or not
        s = _match_tracked_series(series)
        created = False
        if not s:
            s = {"id": uuid.uuid4().hex[:12], "name": series, "author": author,
                 "source": "amazon", "audiobook_status": "none", "history": [],
                 "timeline": [], "edited": True,
                 "last_fetched": datetime.now().isoformat(timespec="seconds")}
            _state["series"].append(s)
            created = True
        tl = s.setdefault("timeline", [])
        # Amazon sometimes positions an audiobook at the wrong series number (this is
        # real: book 1's audio labelled "Book 2 of 2"). The audio's own title is the
        # tie-breaker: if it exactly matches a DIFFERENT book's known novel title (and
        # not the claimed book's), it belongs to that book.
        title = (book.get("title") or "").strip()
        if ad and not nd and title:
            tn = _norm(title)
            own = next((e for e in tl if _entry_n(e) == n), None)
            if not (own and _norm(own.get("title") or "") == tn):
                home = next((e for e in tl if _entry_n(e) != n and _norm(e.get("title") or "") == tn), None)
                if home:
                    add_log(f"Reattributed audiobook '{title}' from book {n} to "
                            f"book {_entry_n(home)} (its title matches that novel)")
                    n = _entry_n(home)
        ent = next((e for e in tl if _entry_n(e) == n), None)
        if not ent:
            ent = {"n": n, "novel": None, "audiobook": None}
            tl.append(ent)
        old_novel = ent.get("novel")
        old_audio = ent.get("audiobook")
        before = (json.dumps(ent, sort_keys=True), s.get("cover_url"), s.get("novel_title"),
                  s.get("novel_date"), s.get("audiobook_date"), s.get("audiobook_status"),
                  json.dumps(s.get("asins") or {}, sort_keys=True),
                  json.dumps(s.get("asins_novel") or {}, sort_keys=True),
                  json.dumps(s.get("asins_audio") or {}, sort_keys=True))
        if nd and title:
            ent["title"] = title              # per-book novel title (attribution anchor)
        if isinstance(body.get("_ku"), bool) and nd:
            ent["ku"] = body["_ku"]           # Kindle Unlimited? gates the hover-card Buy
        # Guard against Amazon cross-linking one audiobook edition onto multiple books
        # (e.g. book 1's audio shown as a "format" of book 2). Each audio ASIN belongs
        # to exactly one row: the first book it was recorded under keeps it.
        if ad and book.get("asin"):
            amap = s.setdefault("asins_audio", {})
            prev_n = next((k for k, v in amap.items() if v == book["asin"]), None)
            if prev_n is not None and prev_n != str(n):
                add_log(f"Ignored audiobook date for {series} book {n}: that audiobook "
                        f"({book['asin']}) is already recorded as book {prev_n}'s audio")
                ad = None
            else:
                amap[str(n)] = book["asin"]
        if not nd and not ad:
            # the only date this page carried was the ignored audio — record nothing,
            # and drop the empty row if we only just created it
            if ent.get("novel") is None and ent.get("audiobook") is None:
                try:
                    tl.remove(ent)
                except ValueError:
                    pass
            save_state()
            return jsonify({"ok": True, "message": "ignored: that audiobook belongs to another book"})
        # Pre-order → live tracking. The page reports one kind per read; remember its
        # pre-order state so the moment it flips to buyable we can say "out now".
        pre = body.get("_preorder", None)          # true / false / null(unknown)
        # A null pre-order reading on a page dated today or later means the
        # extension could not classify the buy box — the one thing the release
        # watcher depends on. Log what it saw so this is diagnosable from the
        # app's activity log without opening the browser.
        if pre is None and body.get("_diag") and (ad or nd):
            _dd = parse_date(ad or nd)
            if _dd and _dd >= today() - timedelta(days=3):
                add_log(f"buy box unreadable for {series} book {n}: {json.dumps(body.get('_diag'))[:400]}")
        released_msgs = []
        narrator_msgs = []
        if pre is not None:
            kind_key = "audio_pre" if ad else "novel_pre"
            kind = "audiobook" if ad else "novel"
            old_pre = ent.get(kind_key)
            ent[kind_key] = bool(pre)
            # Remember WHEN the store first showed it buyable. Publishers release
            # at a consistent local hour (Amazon US = 2pm here), and that hour is
            # what a release-day countdown should aim at — learned per series.
            live_key = "audio_live_at" if ad else "novel_live_at"
            d_now = ad if ad else nd
            dd = parse_date(d_now) if d_now else None
            # A flip only means "released right now" when it happens AROUND the
            # release date. A months-old book flipping preorder->buyable is a
            # stale wrong state being corrected (the pre-v1.11 detector marked
            # every released Kindle page as a pre-order); correcting it is not
            # news and its timestamp teaches the release-hour model nothing.
            recent = dd is not None and 0 <= (today() - dd).days <= RELEASE_PING_GRACE_DAYS
            if pre is False and old_pre is not False and recent and not ent.get(live_key):
                ent[live_key] = datetime.now().isoformat(timespec="seconds")
            fire = False
            if pre is False and dd:
                if old_pre is True and recent:
                    fire = True          # we watched it flip from pre-order to buyable
                elif 0 <= (today() - dd).days <= RELEASE_PING_GRACE_DAYS:
                    # Buyable on (or just after) its release day IS the release, even
                    # if we never saw its pre-order state. The grace window matters:
                    # the extension's release watch keeps reading a page for a few
                    # days after its date precisely because a page can go buyable
                    # after local midnight, or the first successful read can land a
                    # day late (browser closed, a detector fix installed the next
                    # morning). A same-day-only test would confirm the release and
                    # then say nothing about it. Backfill of old catalogue stays
                    # silent: anything older than the window never fires.
                    fire = True
            if fire:
                key = f"release:{s['id']}:{n}:{kind}:{d_now}"
                notified = set(_state.get("discord_notified", []))
                if key not in notified and get_settings().get("discord_on_release", True):
                    notified.add(key)
                    _state["discord_notified"] = list(notified)[-500:]
                    released_msgs.append((_release_live_message(s.get("name", ""), n, kind), kind))
        if nd and not (backfill and ent.get("novel")):
            ent["novel"] = nd                 # Amazon is authoritative for this field
        if ad and not (backfill and ent.get("audiobook")):
            ent["audiobook"] = ad
        if ad and pre is False and not backfill:
            try:
                _maybe_create_buy_job(s, n, ent, ad)
            except Exception as e:            # never let auto-buy break an ingest
                add_log(f"auto-buy: could not queue a job for {series} book {n}: {e}")
        narr = clean_narrator(book.get("narrator"))
        if ad and narr:
            # A narrator swap is news to an audiobook listener — compare against
            # the nearest EARLIER book that names one, ping once per book+narrator.
            prev = None
            for e2 in tl:
                n2 = _entry_n(e2)
                if (n2 is not None and n is not None and n2 < n
                        and clean_narrator(e2.get("narrator"))):
                    if prev is None or n2 > prev[0]:
                        prev = (n2, clean_narrator(e2["narrator"]))
            was_new = (ent.get("narrator") or "").strip() != narr
            ent["narrator"] = narr
            adt_n = parse_date(ent.get("audiobook"))
            # today-or-later only — backfilling an old catalogue must not spam a
            # ping per historical swap (same guard as _new_date_messages). Judged
            # on the cleaned LEAD narrator: stored credits may carry old page
            # furniture, and a full cast is listed differently read to read.
            if (was_new and prev and _norm(lead_narrator(prev[1])) != _norm(lead_narrator(narr))
                    and adt_n and adt_n >= today()):
                key = f"narrator:{s['id']}:{n}:{_norm(narr)}"
                notified = set(_state.get("discord_notified", []))
                if key not in notified and get_settings().get("discord_on_new", True):
                    notified.add(key)
                    _state["discord_notified"] = list(notified)[-500:]
                    narrator_msgs.append(_fmt_line("narrator", icon="\U0001f399️",
                                                   series=s.get("name", ""), n=n,
                                                   narrator=narr, was=prev[1]))
        if cover and (nd or not ent.get("cover")):
            ent["cover"] = cover              # per-book art for the timeline hover card
        tl.sort(key=lambda e: (e.get("novel") or e.get("audiobook") or "", _entry_n(e) or 0))
        s["history"] = _history_from_timeline(tl)
        if author and not s.get("author"):
            s["author"] = author
        if cover and not s.get("cover_url"):
            s["cover_url"] = cover           # first cover we see for a new series
        if book.get("asin"):
            s.setdefault("asins", {})[str(n)] = book["asin"]
            if nd:
                s.setdefault("asins_novel", {})[str(n)] = book["asin"]
        page_url = (body.get("_url") or "").strip()
        if page_url:
            h = urlparse(page_url).netloc.lower()
            if "amazon." in h or "audible." in h:
                s["store_host"] = h
        # If this is the latest book in the series, reflect it on the card headline.
        maxn = max((_entry_n(e) for e in tl if _entry_n(e) is not None), default=None)
        if n == maxn:
            s["novel_n"] = n
            if book.get("title"):
                s["novel_title"] = book["title"]
            if cover and (nd or not s.get("cover_url")):
                # newest book's NOVEL art; an audio read must not swap in Audible
                # art, and a "cover to be revealed" placeholder must not replace
                # real art already on the card (it takes over automatically once
                # Amazon serves the real image, because that one passes the check)
                if not s.get("cover_url") or not cover_is_ph:
                    s["cover_url"] = cover
            if ent.get("novel"):
                s["novel_date"] = ent["novel"]
            s["audiobook_date"] = ent.get("audiobook")
            adt = parse_date(ent.get("audiobook"))
            s["audiobook_status"] = ("released" if (adt and adt <= today())
                                     else ("announced" if adt else "none"))
        after = (json.dumps(ent, sort_keys=True), s.get("cover_url"), s.get("novel_title"),
                 s.get("novel_date"), s.get("audiobook_date"), s.get("audiobook_status"),
                 json.dumps(s.get("asins") or {}, sort_keys=True),
                 json.dumps(s.get("asins_novel") or {}, sort_keys=True),
                 json.dumps(s.get("asins_audio") or {}, sort_keys=True))
        if created or before != after:
            # only a real change earns the "Updated from Amazon" line — routine
            # re-reads (release-day polls, backfill passes) must not churn it
            s["edited"] = True
            s["last_change"] = f"Updated from Amazon: book {n}"
            s["last_change_at"] = datetime.now().isoformat(timespec="seconds")
            _state["last_ingest"] = s["last_change_at"]
        new_date_msgs = _new_date_messages(s, n, old_novel, ent.get("novel"), old_audio, ent.get("audiobook"))
        cover_for_ping = ent.get("cover") or s.get("cover_url") or None
        series_name = s.get("name", "")
        sid_for_card = s.get("id")
        save_state()
    for m, kind in new_date_msgs + [(m, "audiobook") for m in narrator_msgs]:
        _discord_notify(m, embeds=[_book_embed(series_name, n, cover_for_ping, kind)])
    for m, kind in released_msgs:
        # audiobook releases get a LIVING card: later stages (approval, bought,
        # downloaded, filed) edit this same message instead of new pings
        if ad and kind == "audiobook" and _release_card_create(sid_for_card, n, m, _book_embed(series_name, n, cover_for_ping, kind)):
            continue
        _discord_notify(m, embeds=[_book_embed(series_name, n, cover_for_ping, kind)])
    bits = " ".join(filter(None, [f"novel {nd}" if nd else "", f"audiobook {ad}" if ad else ""]))
    add_log(f"Ingested from Amazon: {series} book {n} ({bits})")
    return jsonify({"ok": True, "created": created, "series": s["name"], "n": n,
                    "novel_date": nd, "audiobook_date": ad})


def _push_command(**cmd):
    """Queue a command for the browser extension to pick up on its next poll."""
    with _lock:
        _state["cmd_seq"] = _state.get("cmd_seq", 0) + 1
        cmd["id"] = str(_state["cmd_seq"])
        cmd["at"] = datetime.now().isoformat(timespec="seconds")
        cmds = _state.setdefault("ext_commands", [])
        cmds.append(cmd)
        if len(cmds) > 50:
            del cmds[:len(cmds) - 50]
    save_state()
    return cmd["id"]


@app.get("/api/ext-commands")
def api_ext_commands_get():
    _ext_seen()
    with _lock:
        cmds = list(_state.get("ext_commands", []))
    return jsonify({"commands": cmds})


@app.post("/api/ext-commands/ack")
def api_ext_commands_ack():
    body = request.get_json(force=True, silent=True) or {}
    ids = set(body.get("ids") or [])
    with _lock:
        _state["ext_commands"] = [c for c in _state.get("ext_commands", []) if c.get("id") not in ids]
    save_state()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Auto-buy: KU borrow -> buy the companion audiobook -> return the borrow
#
# The extension does the clicking (it is the logged-in browser); the server
# owns the decision and the record:
#   probing          asked the extension to read the Kindle page (signed in):
#                    is it KU, is the audiobook already owned, what's the
#                    companion price
#   waiting_offer    page read fine but no companion offer yet -> re-probe later
#   awaiting_approval  verify mode (or auto above the cap): email + Discord sent,
#                    token link approves/denies; expires after BUY_TOKEN_TTL_H
#   approved/running extension is borrowing + buying + returning, reporting
#                    each step
#   dry_done         dry-run walked the page and reported what it WOULD click
#   done             audiobook confirmed bought (ledger written), borrow returned,
#                    Update Audiobooks.bat ran
#   done_unreturned  bought, but the KU return failed -> return it by hand
#   uncertain        a buy click happened but no confirmation could be read.
#                    Ledger-marked so it can never be retried automatically.
#   owned/failed/denied/expired/cancelled/needs_signin   terminal, with a note
# ---------------------------------------------------------------------------

try:
    import pick_downloads      # the ONE definition of "a tracked book", shared with Update Audiobooks.bat
except ImportError:            # a copy without the file next to app.py: Libation checks answer "can't tell"
    pick_downloads = None


def _clean_buy_mode(v):
    v = (v or "off") if isinstance(v, str) else "off"
    v = v.strip().lower()
    return v if v in BUY_MODES else "off"


def _clean_buy_cap(v):
    if v in (None, "", False):
        return None
    try:
        f = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return round(f, 2) if f >= 0 else None


def _buy_now():
    return datetime.now().isoformat(timespec="seconds")


def _buy_job_for(sid, n):
    return next((j for j in _state.get("buy_jobs", []) if j.get("series_id") == sid and j.get("n") == n), None)


def _buy_job_by_id(jid):
    return next((j for j in _state.get("buy_jobs", []) if j.get("id") == jid), None)


BUY_PAGE_CFG = {
    "borrow_label": r"add to library|read for free|^borrow",
    "borrow_id": r"borrow|kindle-unlimited|ku-?borrow|kubuy",
    "buy_label": r"^buy now with 1-click|^buy now$|^buy for|^pre-order",
    "add_audio_label": r"add (?:the )?audiobook|add (?:audible )?narration|upgrade with audio|get the audiobook|add narration",
    "narr_chk": "#narration-checkbox, input[type='checkbox'][id*='narration' i], input[type='checkbox'][name*='narration' i]",
    "narr_block": "#audibleNarration, #audible-narration, [id*='narration' i]",
    "expander": "",
    "checkout_label": r"place (?:your )?order|complete (?:your )?purchase|^buy now$|confirm (?:your )?(?:purchase|order)|pay now",
}
BUY_TAB_MODES = ("foreground", "window", "background")


def _job_cmd(job):
    """The slice of a job the extension needs."""
    return {"id": job["id"], "title": job.get("title", ""), "series": job.get("series", ""), "n": job.get("n"),
            "audio_asin": job.get("audio_asin"), "kindle_asin": job.get("kindle_asin"),
            "store_host": job.get("store_host") or "www.amazon.com.au",
            "cap": _job_cap(job), "price": job.get("price"), "dry_run": bool(job.get("dry_run")),
            "ku_borrowed_by_us": bool(job.get("ku_borrowed_by_us")),
            "cfg": dict(BUY_PAGE_CFG, tab_mode=(get_settings().get("buy_tab_mode") or "foreground"))}


def _buy_save_capture(name, html):
    """Keep the raw buy-box markup the extension saw (signed in) under
    %APPDATA%/ReleaseRadar/autobuy/ so the click rules can be tuned offline.
    Local only - it can contain session tokens."""
    if not html:
        return None
    try:
        d = os.path.join(data_dir(), "autobuy")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(html[:200000])
        return p
    except OSError:
        return None


def _job_cap(job):
    """A job's price cap; a missing/zero cap means the default from Settings."""
    try:
        c = float(job.get("cap") or 0)
    except (TypeError, ValueError):
        c = 0
    return c if c > 0 else (_clean_buy_cap(get_settings().get("buy_cap_default")) or BUY_DEFAULT_CAP)


def _job_step(job, step, ok, detail=""):
    job.setdefault("steps", []).append({"at": _buy_now(), "step": step, "ok": bool(ok), "detail": str(detail)[:400]})
    job["steps"] = job["steps"][-40:]
    job["updated"] = _buy_now()


def _job_set(job, status, note=None):
    job["status"] = status
    job["updated"] = _buy_now()
    if note is not None:
        job["note"] = str(note)[:300]


# ---- ownership: Libation's own library db is the truth for "already have it"
def _libation_ro_uri(files_dir):
    db = os.path.join(files_dir, "LibationContext.db")
    return pick_downloads.ro_uri(db) if pick_downloads else f"file:{db}?mode=ro"


def _libation_files_dir():
    cands = [get_settings().get("buy_libation_files") or "",
             os.path.join(os.path.expanduser("~"), "Libation"),
             os.path.join(os.environ.get("APPDATA", ""), "Libation"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Libation")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "LibationContext.db")):
            return c
    return None


def _series_key(name):
    """Series names as Audible and Amazon spell them differently ('The Unfallen
    Series' / 'The Unfallen'); same rule as pick_downloads.py."""
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    s = re.sub(r"^(?:the|a|an) ", "", s)
    return re.sub(r" (?:series|saga|trilogy|cycle)$", "", s).strip()


def _libation_match(asin=None, series=None, n=None, srs=None):
    """Libation's record of one book: {"in_library": bool, "downloaded": bool},
    or None when it can't be told. Recognition is pick_downloads' (series
    name + number, a recorded product ID, author + title with the date
    agreeing): Audible's ID is not the Amazon listing ASIN the radar knows
    (Riftside 4: B0HJ9CQDHW vs B0HJ9DX4S2), and a brand-new release can arrive
    with no series on Audible's record at all (Arcane Academy 5, 2 Oct 2026).
    srs: the radar's series record (author, the book's title, its audiobook
    date). "downloaded" follows the strongest match: Audible's own series
    number or a recorded ID outrank an author+title guess, so a liberated
    sibling can never make a failed download look complete."""
    d = _libation_files_dir()
    if not d or pick_downloads is None or not (asin or ((srs or series) and n is not None)):
        if d and pick_downloads is None:
            log.warning("pick_downloads.py is missing next to app.py - Libation checks can't tell")
        return None
    try:
        books = pick_downloads.library_books(d, pending_only=False)
        rec = dict(srs) if isinstance(srs, dict) else {}
        rec.setdefault("name", series or "")
        ent = pick_downloads.series_entry(rec, n=n) if n is not None else pick_downloads.series_entry({"name": ""})
        if asin:
            ent["asins"].add(asin)
        hits = []
        for b in books:
            m = pick_downloads.match_book(b, [ent])
            if m:
                hits.append((m[1] in (ent["name"], "matched by ID"), b["status"]))
    except Exception:
        return None
    strong = [st for sure, st in hits if sure]
    use = strong or [st for _, st in hits]
    return {"in_library": bool(hits),
            "downloaded": bool(use) and all(st == pick_downloads.LIBERATED for st in use)}


def _libation_owns(asin, series=None, n=None, srs=None):
    """True/False from Libation's library, None when it can't be checked."""
    m = _libation_match(asin, series, n, srs=srs)
    return None if m is None else m["in_library"]


# ---- creation (called from ingest, under the lock)
def _maybe_create_buy_job(s, n, ent, audio_date):
    mode = _clean_buy_mode(s.get("buy_mode"))
    if mode == "off":
        return None
    st = get_settings()
    if not st.get("buy_master"):
        return None                              # master switch off: nothing is queued
    if _buy_job_for(s["id"], n):
        return None
    audio_asin = (s.get("asins_audio") or {}).get(str(n))
    if not audio_asin:
        return None
    if audio_asin in _state.get("buy_ledger", {}):
        return None
    dd = parse_date(audio_date)
    if not dd or dd > today() or (today() - dd).days > BUY_PROBE_DAYS:
        return None                              # only around release day, never back-catalogue
    job = {"id": uuid.uuid4().hex[:10], "series_id": s["id"], "series": s.get("name", ""), "n": n,
           "title": (ent.get("title") or f"{s.get('name', '')} {n}"),
           "audio_asin": audio_asin, "kindle_asin": (s.get("asins_novel") or {}).get(str(n)),
           "store_host": s.get("store_host") or "www.amazon.com.au",
           "mode": mode, "cap": _clean_buy_cap(s.get("buy_cap")) or _clean_buy_cap(st.get("buy_cap_default")) or BUY_DEFAULT_CAP,
           "dry_run": bool(st.get("buy_dry_run", True)), "status": "probing", "price": None,
           "created": _buy_now(), "updated": _buy_now(), "probes": 0, "last_probe": None,
           "steps": [], "note": ""}
    owned = _libation_owns(audio_asin, series=s.get("name"), n=n, srs=s)
    if owned:
        _job_set(job, "owned", "Libation already has this audiobook")
    _state.setdefault("buy_jobs", []).append(job)
    _state["buy_jobs"] = _state["buy_jobs"][-100:]
    if job["status"] == "probing":
        _buy_push_probe(job)
    add_log(f"auto-buy: {job['status']} — {job['series']} book {n} ({mode}, cap ${job['cap']:.2f}"
            f"{', DRY RUN' if job['dry_run'] else ''})")
    return job


def _buy_push_probe(job):
    job["probes"] = int(job.get("probes") or 0) + 1
    job["last_probe"] = _buy_now()
    _push_command(type="buy", phase="probe", job=_job_cmd(job))


# a job parked or stopped for a reason a person can clear (signed back in, the
# store now sells it) - what the dashboard's Retry, !retry and the Discord
# button accept
BUY_RETRYABLE = ("needs_signin", "retry_later", "failed", "expired")


def _buy_retry(job, who):
    """Re-check a parked job NOW instead of at the next 3-hourly probe. It gets
    a fresh probe window, and a still-signed-out answer is announced again
    (with a new button). Caller holds _lock."""
    job["dry_run"] = bool(get_settings().get("buy_dry_run", True))
    job["probes"] = 0
    job["window_start"] = _buy_now()
    job.pop("signin_notified", None)
    job.pop("signin_button_at", None)
    _job_step(job, "retry", True, f"retry from {who}")
    _job_set(job, "probing", f"retry from {who}")
    _buy_push_probe(job)


def _buy_week_count():
    cutoff = datetime.now() - timedelta(days=7)
    n = 0
    for v in _state.get("buy_ledger", {}).values():
        try:
            if v.get("auto") and datetime.fromisoformat(v.get("at")) >= cutoff:
                n += 1
        except (TypeError, ValueError):
            pass
    return n


# ---- decision after a successful probe
def _buy_decide(job):
    st = get_settings()
    price = job.get("price")
    if job.get("dry_run"):
        _job_set(job, "running", "dry run: borrows on KU, finds the audiobook offer, returns the borrow - buys nothing")
        _push_command(type="buy", phase="dry", job=_job_cmd(job))
        _buy_live_begin(job)
        return
    if job.get("mode") == "auto" and price is not None and price <= _job_cap(job):
        if _buy_week_count() >= int(st.get("buy_weekly_max", BUY_WEEKLY_MAX)):
            _job_step(job, "decide", True, "weekly automatic limit reached — asking instead")
            _buy_request_approval(job, why="weekly limit")
            return
        _buy_approve(job, who="auto")
        return
    why = "verify mode" if job.get("mode") == "verify" else f"price ${price:.2f} is above the cap ${float(job.get('cap') or 0):.2f}"
    _buy_request_approval(job, why=why)


def _buy_public_base():
    b = (get_settings().get("buy_public_base") or "").strip().rstrip("/")
    return b or "http://127.0.0.1:8765"


def _buy_request_approval(job, why=""):
    token = secrets.token_urlsafe(32)
    job["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    job["expires"] = (datetime.now() + timedelta(hours=BUY_TOKEN_TTL_H)).isoformat(timespec="seconds")
    _job_set(job, "awaiting_approval", why)
    _release_card_line(job.get("series_id"), job.get("n"),
                       f"💳 waiting for your OK" + (f" at ${job.get('price'):.2f}" if isinstance(job.get('price'), (int, float)) else ""))
    link = f"{_buy_public_base()}/buy/{token}"
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else "price unknown"
    subject = f"Release Radar: buy the audiobook? {job.get('title', '')} — {ptxt}"
    text = (f"{job.get('series', '')} — book {job.get('n')}\n{job.get('title', '')}\n\n"
            f"Companion audiobook price: {ptxt}  (cap ${float(job.get('cap') or 0):.2f})\n"
            f"Why you're being asked: {why}\n\n"
            f"Approve or deny here (link works for {BUY_TOKEN_TTL_H} h, one decision only):\n{link}\n\n"
            f"What approving does: borrows the Kindle edition on Kindle Unlimited, buys the audiobook at the "
            f"companion price, returns the borrow, then runs Update Audiobooks.\n")
    _send_email_async(job["id"], subject, text)
    _discord_notify(_fmt_line("ask_buy", series=job.get("series", ""), n=job.get("n"),
                              price=ptxt, link=link) + f" ({why})")


def _buy_approve(job, who="link"):
    _job_step(job, "approve", True, f"approved by {who}")
    _release_card_line(job.get("series_id"), job.get("n"), "💳 approved — buying", replace_last=True)
    _job_set(job, "approved", f"approved by {who}")
    job["approved_at"] = _buy_now()
    _push_command(type="buy", phase="execute", job=_job_cmd(job))
    _job_set(job, "running", f"approved by {who}; extension is working")
    _buy_live_begin(job)


def _buy_deny(job, who="link"):
    _job_step(job, "deny", True, f"denied by {who}")
    _job_set(job, "denied", f"denied by {who}")


_EMAIL_ASYNC = True       # tests flip this to run inline


def _send_email_async(job_id, subject, text):
    """SMTP can block for many seconds: never do it under the state lock or
    inside a request. The outcome lands on the job as an 'email' step."""
    def _go():
        ok, msg = _send_email(subject, text)
        with _lock:
            job = _buy_job_by_id(job_id)
            if job:
                _job_step(job, "email", ok, msg)
            save_state()
    if _EMAIL_ASYNC:
        threading.Thread(target=_go, daemon=True).start()
    else:
        _go()


def _send_email(subject, text):
    st = get_settings()
    to, user, pw = st.get("buy_email_to"), st.get("buy_smtp_user"), st.get("buy_smtp_pass")
    if not (to and user and pw):
        return False, "email not configured (address, sender, app password)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    try:
        with smtplib.SMTP_SSL(st.get("buy_smtp_host") or "smtp.gmail.com", 465, timeout=25) as srv:
            srv.login(user, pw)
            srv.send_message(msg)
        return True, "sent"
    except Exception as e:        # noqa: BLE001
        return False, f"email failed: {e}"


# ---- reports from the extension
def _buy_handle_report(job, phase, ok, detail):
    """Apply one report from the extension to a job. Runs under the lock."""
    detail = detail if isinstance(detail, dict) else {"info": str(detail)}
    info = str(detail.get("info") or "")[:300]
    if phase == "progress":
        job["now"] = info                     # what it is trying right now: the live message, not a step
        return
    if phase == "probe":
        job["probes_ok"] = int(job.get("probes_ok") or 0) + (1 if ok else 0)
        if detail.get("buybox_html"):
            _buy_save_capture(f"probe-{detail.get('kindle_asin') or job.get('kindle_asin') or job.get('audio_asin')}",
                              detail.pop("buybox_html"))
        if detail.get("kindle_asin"):
            job["kindle_asin"] = detail["kindle_asin"]
        if detail.get("buttons") is not None:
            job["buttons"] = detail.get("buttons")
        _job_step(job, "probe", ok, info or json.dumps({k: v for k, v in detail.items() if k != "buttons"})[:300])
        if detail.get("signed_in") is False:
            _buy_needs_signin(job)
            return
        if not ok:
            _job_set(job, "waiting_offer", info or "probe failed; will retry")
            return
        if detail.get("owned"):
            _job_set(job, "owned", "the store says you already own this audiobook")
            return
        if isinstance(detail.get("ku"), bool):
            srs = _find(job.get("series_id")) if job.get("series_id") else None
            if srs:
                row = next((e for e in srs.get("timeline", []) if _entry_n(e) == job.get("n")), None)
                if row is not None:
                    row["ku"] = detail["ku"]
            if detail["ku"] is False:
                _job_set(job, "failed", "not a Kindle Unlimited title - the discounted "
                                        "companion price needs a KU borrow, so I won't buy this one")
                return
        price = detail.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None:
            _job_set(job, "waiting_offer", "no companion-audiobook offer on the page yet")
            return
        job["price"] = round(price, 2)
        job["price_currency"] = detail.get("currency") or ""
        _buy_decide(job)
        return
    if phase == "dry":
        step = str(detail.get("step") or "dry")
        if detail.get("borrowed_by_us"):
            job["ku_borrowed_by_us"] = True
        if detail.get("html"):
            _buy_save_capture(f"dry-{re.sub(r'[^a-z0-9]+', '_', step.lower())}-{job.get('kindle_asin') or job.get('audio_asin')}", detail.pop("html"))
        _job_step(job, step, ok, info)
        if detail.get("final") or step == "dry":
            _job_set(job, "dry_done" if ok else "failed", info)
        return
    if phase == "execute":
        step = str(detail.get("step") or "step")
        if detail.get("html"):
            _buy_save_capture(f"exec-{re.sub(r'[^a-z0-9]+', '_', step.lower())}-{job.get('audio_asin')}", detail.pop("html"))
        _job_step(job, step, ok, info)
        if not ok:
            if job.get("purchased"):
                _job_set(job, "done_unreturned", f"{step} failed after the purchase: {info} — return the KU borrow by hand")
                _buy_after_purchase(job)
            elif step == "checkout" and "refused" in info.lower():
                n = int(job.get("store_retries") or 0) + 1
                job["store_retries"] = n
                if n > BUY_STORE_RETRY_MAX:
                    _job_set(job, "failed", f"Amazon kept refusing the checkout ({n - 1} tries): {info}")
                else:
                    job["retry_at"] = (datetime.now() + timedelta(hours=BUY_STORE_RETRY_H)).isoformat(timespec="seconds")
                    _job_set(job, "retry_later", f"Amazon refused the checkout (try {n}); trying again in {BUY_STORE_RETRY_H} h — "
                                                 f"usually the audiobook isn't on Audible AU yet")
                    _release_card_line(job.get("series_id"), job.get("n"),
                                       f"⏳ store refused (not on Audible AU yet) — retrying every {BUY_STORE_RETRY_H} h", replace_last=True)
                    if n == 1:
                        _discord_notify(f"\u23f3 Amazon couldn't process {job.get('series', '')} — book {job.get('n')} yet "
                                        f"('{info[:120]}'). Usually it's not on Audible AU yet; I'll retry every {BUY_STORE_RETRY_H} h.")
            elif detail.get("clicked"):
                _release_card_line(job.get("series_id"), job.get("n"), "⚠️ buy attempt unconfirmed — check Amazon")
                _job_set(job, "uncertain", f"a buy click happened but {step} could not be confirmed: {info}")
                _state.setdefault("buy_ledger", {})[job["audio_asin"]] = {
                    "series": job.get("series"), "n": job.get("n"), "at": _buy_now(), "job": job["id"],
                    "price": job.get("price"), "auto": job.get("mode") == "auto", "uncertain": True}
            else:
                _job_set(job, "failed", f"{step}: {info}")
            return
        if step == "confirm":
            job["purchased"] = True
            job["purchased_at"] = _buy_now()
            _release_card_line(job.get("series_id"), job.get("n"),
                               f"✅ bought" + (f" ${job.get('price'):.2f}" if isinstance(job.get('price'), (int, float)) else ""),
                               replace_last=True)
            _state.setdefault("buy_ledger", {})[job["audio_asin"]] = {
                "series": job.get("series"), "n": job.get("n"), "at": _buy_now(), "job": job["id"],
                "price": job.get("price"), "auto": job.get("mode") == "auto"}
        if step == "return":
            if job.get("purchased"):
                _release_card_line(job.get("series_id"), job.get("n"), "↩️ KU borrow returned")
                _job_set(job, "done", "bought, borrow returned")
                _buy_after_purchase(job)
        return
    if phase == "return":
        _job_step(job, "return", ok, info)
        if ok:
            job["ku_borrowed_by_us"] = False
            job["note"] = (job.get("note") or "") + " | KU borrow returned"
        return
    _job_step(job, phase, ok, info)


def _buy_needs_signin(job):
    """Firefox is signed out of Amazon. Park the job, tell the user ONCE (Discord +
    email), and keep re-probing on the normal cadence so the moment he signs in
    again the job carries on by itself. Reading dates still works signed out;
    only buying needs the session."""
    _job_set(job, "needs_signin", "Firefox is signed out of Amazon — sign in on the PC; I'll keep checking")
    if job.get("signin_notified"):
        return
    job["signin_notified"] = _buy_now()
    job["signin_button_at"] = _buy_now()
    head = (f"\U0001f511 Firefox is signed out of Amazon, so I can't buy {job.get('series', '')} — book {job.get('n')}. "
            f"Sign in again on the PC (any Amazon page)")
    tail = f"; otherwise I re-check every {BUY_PROBE_GAP_H} h for {BUY_PROBE_DAYS} days and carry on by myself."
    # the bot posts it with a Retry button (a webhook can't carry one); without
    # the bot, the webhook says where the Retry lives instead
    _notify_with_button(head + ", then hit the button" + tail,
                        [("I've signed in — retry now", f"rr:retry:{job['id']}")],
                        fallback=head + ", then hit Retry on the dashboard's purchase card" + tail)
    _send_email_async(job["id"], f"Release Radar: sign in to Amazon to buy {job.get('title', '')}", head + tail)


def _buy_after_purchase(job):
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else ""
    if not _release_card_line(job.get("series_id"), job.get("n"),
                              f"⬇️ downloading via Libation in {BUY_BAT_DELAY_S // 60} min…"):
        _discord_notify(_fmt_line("bought", series=job.get("series", ""), n=job.get("n"),
                                  price=ptxt or "the companion price")
                        + ("" if job.get("status") == "done" else " (Return the KU borrow by hand.)"))
    _run_update_audiobooks(job)


# ---- the bat
# ---- live Discord progress for Update Audiobooks -------------------------
def _discord_live_start(text, embeds=None):
    """Post a webhook message we can edit later. Returns the edit URL or None."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return None
    try:
        payload = {"content": (text or "")[:1900]}
        if embeds:
            payload["embeds"] = [e for e in embeds if e][:10]
        data = json.dumps(payload).encode("utf-8")
        url = wh + ("&" if "?" in wh else "?") + "wait=true"
        req = Request(url, data=data,
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            mid = json.loads(resp.read().decode("utf-8", "replace")).get("id")
        return f"{wh.split('?')[0]}/messages/{mid}" if mid else None
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord live message failed: {e}")
        return None


def _discord_live_edit(edit_url, text):
    if not edit_url:
        return False
    try:
        data = json.dumps({"content": (text or "")[:1900]}).encode("utf-8")
        req = Request(edit_url, data=data, method="PATCH",
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:           # noqa: BLE001
        return False


class _BatProgress:
    """Turns Update Audiobooks.bat output into one live progress message.
    Line shapes from real runs: '1/3  Scanning...', 'Scanning Audible library:
    2 accounts...', 'Total processed: 321', 'New: 1',
    'DownloadDecryptBook Begin: date [ASIN] Title', '...Completed: ...',
    then the filer's FILED/NEW/ADOPT/HOLD lines."""
    BAR_W = 12

    def __init__(self):
        self.phase = "starting"
        self.accounts = None
        self.processed = None
        self.new = None
        self.total = 0
        self.begun = 0
        self.done_dl = 0
        self.failed_dl = 0
        self.picked = None          # what the tracked-series picker let through (None: picker not seen)
        self.skipped = 0            # ...and what it refused
        self.picked_ids = {}        # Audible product ID -> why the picker took it
        self.current = ""
        self.current_asin = None
        self.book_pct = None        # how far the current book's download is (sampled from Libation's temp file)
        self.filed = []
        self.finished = False
        self.rc = None
        self.attempt = 1
        self._buf = ""

    def feed(self, chunk):
        self._buf += chunk.replace("\r", "")
        *lines, self._buf = self._buf.split("\n")
        for l in lines:
            self._line(l.strip())

    def _line(self, l):
        if not l:
            return
        if l.startswith("1/3"):
            self.phase = "scan"
        elif l.startswith("2/3"):
            self.phase = "download"
        elif l.startswith("3/3"):
            self.phase = "file"
        m = re.match(r"Scanning Audible library:\s*(\d+)\s*account", l)
        if m:
            self.accounts = int(m.group(1))
        m = re.match(r"Total processed:\s*(\d+)", l)
        if m:
            self.processed = int(m.group(1))
        m = re.match(r"New:\s*(\d+)", l)
        if m:
            self.new = int(m.group(1))
            if self.picked is None:
                self.total = max(self.total, self.new)
        # the tracked-series picker decides what is actually downloaded: its
        # count is the bar's total, not Libation's "New: N" (a 1/2 bar that
        # never filled, 7 Oct 2026, was one tracked book out of two new ones)
        if l.startswith("Download (tracked):"):
            self.picked = (self.picked or 0) + 1
            self.total = self.picked
            m = re.search(r"\[([A-Z0-9]+)\]\s*-\s*(.+?)\s*$", l)
            if m:
                self.picked_ids[m.group(1)] = m.group(2)
        elif l.startswith("Not downloading (not in a tracked series):"):
            self.skipped = len([x for x in l.split(":", 1)[1].split(";") if x.strip()])
        elif l.startswith("Nothing new from the series Release Radar tracks"):
            self.picked = 0
            self.total = 0
        m = re.match(r"DownloadDecryptBook Begin:\s*\S+\s*\[([A-Z0-9]+)\]\s*(.+)", l)
        if m:
            self.begun += 1
            self.total = max(self.total, self.begun)
            self.current_asin, self.current = m.group(1), m.group(2).strip()
            self.book_pct = None
        if re.match(r"DownloadDecryptBook Completed:", l):
            self.done_dl += 1
            self.book_pct = None
        if l.startswith("Error processing book"):
            self.failed_dl += 1
            self.done_dl = max(0, self.done_dl - 1)       # Libation says Completed before it says Error
        if re.match(r"(FILED|NEW|ADOPT|HOLD)\b", l) and not re.match(r"New:", l):
            self.filed.append(l[:120])

    def bar(self):
        if not self.total:
            return ""
        done = min(self.done_dl, self.total)
        # the book in flight moves the bar too, by how far its download is
        partial = (self.book_pct or 0) / 100 if (done < self.total and self.book_pct) else 0
        f = int(self.BAR_W * (done + partial) / self.total)
        return "\u25b0" * f + "\u25b1" * (self.BAR_W - f) + f"  {done}/{self.total}"

    def book_bar(self):
        if self.book_pct is None:
            return ""
        f = int(8 * self.book_pct / 100)
        return " " + "\u25b0" * f + "\u25b1" * (8 - f) + f" {self.book_pct}%"

    def render(self):
        rows = ["\U0001f4e6 **Update Audiobooks**" + (f" (attempt {self.attempt})" if self.attempt > 1 else "")]
        if self.processed is not None:
            tracked = f" \u00b7 {self.picked} tracked" if self.picked is not None and self.new else ""
            rows.append(f"\u2705 Scan: {self.processed} titles, {self.new or 0} new{tracked}")
        elif self.phase == "starting":
            rows.append("\u23f3 starting\u2026")
        else:
            rows.append("\U0001f50e Scanning the Audible library\u2026"
                        + (f" ({self.accounts} accounts)" if self.accounts else ""))
        if self.processed is not None:
            if self.total == 0:
                rows.append("\U0001f4a4 Nothing new from the series you track"
                            + (f" ({self.skipped} skipped, not tracked)" if self.skipped else ""))
            else:
                # a tick only when every book came through; a download that
                # Libation gave up on is a warning, not a tick
                all_done = self.done_dl >= self.total and not self.failed_dl
                downloading = self.phase == "download" and not self.finished and not all_done and not self.failed_dl
                icon = "\u2b07\ufe0f" if downloading else ("\u2705" if all_done else "\u26a0\ufe0f")
                cur = f" \u00b7 *{self.current}*{self.book_bar()}" if downloading and self.current else ""
                fail = f" \u00b7 {self.failed_dl} failed (Libation will retry)" if self.failed_dl else ""
                skip = f" \u00b7 {self.skipped} skipped, not tracked" if self.skipped else ""
                rows.append(f"{icon} Download  {self.bar()}{cur}{fail}{skip}")
        if self.phase == "file" and not self.finished:
            rows.append("\U0001f5c2\ufe0f Filing into the library\u2026")
        for l in self.filed[-6:]:
            rows.append("\u2003" + l)
        if self.finished:
            rows.append({0: "\u2705 **Done.**",
                         2: "\u26a0\ufe0f **Done \u2014 some books are HELD** (see the lines above)."}
                        .get(self.rc, f"\u274c **Failed** (rc={self.rc}) \u2014 see autobuy-bat.log"))
        return "\n".join(rows)[:1900]


class _LiveEditor:
    """Throttled edits: at most one PATCH per interval, only when the text changed."""
    def __init__(self, edit_url, interval=2.5):
        self.url = edit_url
        self.interval = interval
        self._last_t = 0.0
        self._last_text = ""

    def push(self, text, force=False):
        if not self.url or text == self._last_text:
            return
        now = time.monotonic()
        if not force and (now - self._last_t) < self.interval:
            return
        if _discord_live_edit(self.url, text):
            self._last_t = now
            self._last_text = text


# ---- live Discord progress for a purchase run ------------------------------
# The outcome alone wasn't enough (29 Sep 2026: a purchase stopped mid-way and
# the only trace was the dashboard card). One message per run, edited in place
# as the extension reports: each step's result, what it is trying right now,
# then the outcome. A failure also gets a Retry button (bot) of its own.
BUY_STEP_TEXT = {"approve": "Approved", "retry": "Retry", "borrow": "Kindle Unlimited borrow",
                 "borrow-ajax": "Borrow (fallback)", "borrow-verify": "Borrow confirmed",
                 "buy-click": "Pressed Buy", "payment": "Payment method", "review": "Review page",
                 "checkout": "Checkout", "confirm": "Order confirmation", "return": "Kindle Unlimited return",
                 "offer": "Audiobook offer"}
BUY_RUN_STATES = ("probing", "awaiting_approval", "approved", "running")
_BUY_OUTCOME = {
    "done": "✅ Bought — Update Audiobooks runs next.",
    "done_unreturned": "✅ Bought — but return the KU borrow by hand.",
    "failed": "❌ Stopped — nothing was bought.",
    "uncertain": "⚠️ A Buy click happened but I couldn't see a confirmation — check your Amazon orders before retrying.",
    "retry_later": "⏳ Amazon refused the checkout — I'll try again automatically.",
    "dry_done": "\U0001f9ea Dry run finished — nothing was bought.",
}
_buy_live = {}


def _buy_step_text(name):
    if name.startswith("offer on "):
        return "Audiobook offer on the " + name[len("offer on "):]
    return BUY_STEP_TEXT.get(name, name)


def _buy_live_render(job):
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else "price ?"
    lines = [f"\U0001f6d2 **{job.get('series', '')} — book {job.get('n')}** · {ptxt}"
             + (" · dry run" if job.get("dry_run") else "")]
    since = job.get("run_started") or ""
    for st in job.get("steps") or []:
        if (st.get("at") or "") < since:
            continue
        name, detail = str(st.get("step") or ""), str(st.get("detail") or "")
        mark = "✅" if st.get("ok") else "❌"
        if name.startswith("offer on ") and detail.startswith("not here"):
            mark = "➖"                   # looked, not on this page: the search moves on
        lines.append(f"{mark} {_buy_step_text(name)} — {detail[:150]}")
    status = job.get("status")
    if status in BUY_RUN_STATES:
        if job.get("now"):
            lines.append(f"⏳ now: {str(job['now'])[:160]}")
    else:
        out = _BUY_OUTCOME.get(status, f"Finished: {status}.")
        if status in ("failed", "uncertain") and job.get("note"):
            out += f"\n({str(job['note'])[:200]})"
        lines.append(out)
    return "\n".join(lines)[:1900]


class _BuyLive:
    """One live Discord message per purchase run. Network happens on its own
    thread; callers (holding _lock) only drop the latest text in."""
    IDLE_LIMIT_S = 3 * 3600

    def __init__(self, text):
        self.text, self.final, self.ev = text, False, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def set(self, text, final=False):
        self.text = text
        self.final = self.final or final
        self.ev.set()

    def _run(self):
        ed = _LiveEditor(_discord_live_start(self.text))
        shown, idle = self.text, 0
        while True:
            if not self.ev.wait(timeout=60):
                idle += 60
                if idle >= self.IDLE_LIMIT_S:
                    return                    # an abandoned run: stop watching
                continue
            self.ev.clear()
            idle = 0
            t = self.text
            if t != shown:
                ed.push(t, force=True)
                shown = t
            if self.final and self.text == shown:
                return
            time.sleep(1.2)                   # pace the edits; anything newer is picked up next loop


def _buy_live_begin(job):
    """A purchase run (dry or real) was handed to the extension."""
    job["run_started"] = _buy_now()
    job["now"] = "handed to the browser extension — waiting for it to pick the job up"
    _buy_live[job["id"]] = _BuyLive(_buy_live_render(job))


def _buy_live_update(job):
    lv = _buy_live.get(job.get("id"))
    if not lv:
        return
    final = job.get("status") not in BUY_RUN_STATES
    if final:
        job.pop("now", None)
    lv.set(_buy_live_render(job), final=final)
    if final:
        _buy_live.pop(job["id"], None)
        if job.get("status") == "failed":
            head = (f"❌ {job.get('series', '')} — book {job.get('n')}: the purchase stopped "
                    f"({str(job.get('note') or '')[:160]}). Nothing was bought.")
            _notify_with_button(head + " Retry once it's sorted:",
                                [("Retry", f"rr:retry:{job['id']}")],
                                fallback=head + " Retry it from the dashboard's purchase card (or `!retry`).")



def _bat_path():
    p = (get_settings().get("buy_bat") or "").strip()
    if p and os.path.isfile(p):
        return p
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for c in (os.path.join(base, "Update Audiobooks.bat"),
              os.path.join(os.path.dirname(base), "Update Audiobooks.bat")):
        if os.path.isfile(c):
            return c
    return None


LIBATION_MB_PER_MIN = 0.91      # measured: Riftside 4 1202 min -> 1093 MB, Loopbreaker 1 1313 min -> 1194 MB
_libation_len_cache = {}


def _libation_length_minutes(asin):
    if asin in _libation_len_cache:
        return _libation_len_cache[asin]
    d = _libation_files_dir()
    mins = None
    if d:
        try:
            con = sqlite3.connect(_libation_ro_uri(d), uri=True, timeout=2)
            try:
                row = con.execute("select LengthInMinutes from Books where AudibleProductId = ?", (asin,)).fetchone()
            finally:
                con.close()
            mins = int(row[0]) if row and row[0] else None
        except Exception:       # noqa: BLE001
            mins = None
    _libation_len_cache[asin] = mins
    return mins


def _libation_download_pct(asin):
    """How far Libation's in-flight download of a book is, 0-99, or None.
    LibationCli prints nothing between 'Begin' and 'Completed'; the growing
    file in its DownloadsInProgress folder, against the book's length, is the
    only progress there is (the encrypted download is ~0.91 MB per minute)."""
    if not asin:
        return None
    temp = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Temp")
    size = None
    try:
        for name in os.listdir(temp):
            if not name.lower().startswith("libation"):
                continue
            d = os.path.join(temp, name, "DownloadsInProgress")
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.upper().startswith(asin.upper() + "."):
                    size = max(size or 0, os.path.getsize(os.path.join(d, f)))
    except OSError:
        return None
    if size is None:
        return None
    mins = _libation_length_minutes(asin)
    if not mins:
        return None
    return int(min(99, 100 * size / (mins * LIBATION_MB_PER_MIN * 1048576)))


def _run_bat_once(bat, on_output=None, on_tick=None):
    """Run Update Audiobooks.bat --auto hidden; returns (rc, tail of its output).
    on_output, when given, receives each new chunk of the bat's output while it
    runs (drives the live Discord progress message); on_tick runs every couple
    of seconds regardless of output (a download prints nothing while it runs)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logf = os.path.join(data_dir(), "autobuy-bat.log")
    with open(logf, "ab") as f:
        f.write(f"\n=== {_buy_now()} {bat} --auto ===\n".encode("utf-8", "replace"))
        f.flush()
        ofs = f.tell()
        p = subprocess.Popen(["cmd.exe", "/c", bat, "--auto"], stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, creationflags=flags, cwd=os.path.dirname(bat))

        def _drain():
            nonlocal ofs
            if not on_output:
                return
            try:
                with open(logf, "rb") as rf:
                    rf.seek(ofs)
                    chunk = rf.read()
                if chunk:
                    ofs += len(chunk)
                    on_output(chunk.decode("utf-8", "replace"))
            except OSError:
                pass

        deadline = time.monotonic() + 3600
        while True:
            try:
                rc = p.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                _drain()
                if on_tick:
                    try:
                        on_tick()
                    except Exception:   # noqa: BLE001 - progress sampling must never stop the run
                        pass
                if time.monotonic() > deadline:
                    p.kill()
                    rc = -1
                    break
        _drain()
    try:
        with open(logf, "rb") as f:
            f.seek(max(0, os.path.getsize(logf) - 1500))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        tail = ""
    return rc, tail


def _run_update_audiobooks(job=None, delay=None):
    """After a confirmed purchase (or on demand): wait for Audible to list it,
    run the bat, re-run a couple of times if Libation didn't see the title yet."""
    def _go():
        time.sleep(BUY_BAT_DELAY_S if delay is None else delay)
        bat = _bat_path()
        if not bat:
            add_log("auto-buy: Update Audiobooks.bat not found — set its path in Settings")
            _discord_notify("\u26a0\ufe0f Update Audiobooks.bat not found — set its path in Settings.")
            return
        asin = job.get("audio_asin") if job else None
        prog = _BatProgress()
        editor = _LiveEditor(_discord_live_start(prog.render()))

        def _on_output(chunk):
            prog.feed(chunk)
            editor.push(prog.render())

        def _on_tick():
            # the book in flight: sample Libation's temp file for a per-book bar
            if prog.phase == "download" and prog.current_asin and not prog.finished:
                pct = _libation_download_pct(prog.current_asin)
                if pct is not None and pct != prog.book_pct:
                    prog.book_pct = pct
                    editor.push(prog.render())

        downloaded = None
        for attempt in range(1, BUY_BAT_RETRIES + 1):
            prog.attempt = attempt
            rc, tail = _run_bat_once(bat, on_output=_on_output, on_tick=_on_tick)
            # DOWNLOADED, not merely in the library: Audible's licence service
            # can fail right after a purchase (29 Sep 2026) while the book
            # already sits in Libation's list
            got = (_libation_match(asin, job.get("series"), job.get("n"),
                                   srs=_find(job.get("series_id")) if job.get("series_id") else None)
                   if job else None)
            if job and got is not None and not got["downloaded"]:
                # the picker's own word: the ONE book it took for THIS series, now in
                # Libation. Two picks for the series and it can't tell which was bought.
                ours = [pid for pid, why in prog.picked_ids.items()
                        if _series_key(why.split(" (")[0]) == _series_key(job.get("series"))]
                if len(ours) == 1:
                    m = _libation_match(ours[0])
                    if m and m["downloaded"]:
                        got = m
            downloaded = None if got is None else got["downloaded"]
            held = " (some books HELD — see the bat log)" if rc == 2 else ""
            said = {True: "yes", False: "not yet", None: "can't tell"}[downloaded]
            with _lock:
                if job:
                    _job_step(job, "bat", rc in (0, 2) and downloaded is not False,
                              f"attempt {attempt}: rc={rc}{held}; downloaded: {said}")
                save_state()
            add_log(f"Update Audiobooks ran (rc={rc}){held}" +
                    (f"; {job.get('series', '')} {job.get('n')} downloaded: {said}" if job else ""))
            if downloaded or downloaded is None or not job:
                break
            time.sleep(BUY_BAT_RETRY_GAP_S)
        prog.finished = True
        prog.rc = rc
        _own_cache["at"] = 0.0        # new files on disk: recount ownership soon
        if rc in (0, 2):
            try:
                threading.Thread(target=_abs_trigger_scan, daemon=True).start()
            except Exception:
                pass
        if job:
            if downloaded is False:
                line = "❌ bought, but Libation couldn't download it — see below"
            elif rc in (0, 2):
                line = "🗂️ filed into the library — complete ✔"
            else:
                line = "❌ download/filing failed — see autobuy-bat.log"
            _release_card_line(job.get("series_id"), job.get("n"), line, replace_last=True)
            if downloaded is False:
                head = (f"⚠️ {job.get('series', '')} — book {job.get('n')} is bought, but Libation couldn't "
                        f"download it after {BUY_BAT_RETRIES} tries (usually Audible's licence service right after "
                        f"a purchase).")
                _notify_with_button(head + " Try the download again:",
                                    [("Download again", f"rr:download:{job['id']}")],
                                    fallback=head + " Send `!download` later, or hit Liberate in Libation.")
        if editor.url:
            editor.push(prog.render(), force=True)      # the live message becomes the summary
        else:
            filed = [ln.strip() for ln in tail.splitlines() if ln.startswith(("FILED", "NEW", "ADOPT", "HOLD"))][-4:]
            _discord_notify("\U0001f4e6 Update Audiobooks finished (rc=%s).%s" % (rc, ("\n" + "\n".join(filed)) if filed else ""))
    threading.Thread(target=_go, daemon=True).start()


# ---- periodic
# ---------------------------------------------------------------------------
# Discord bot: two-way control. The webhook talks AT you; with a bot token in
# Settings the radar also LISTENS - !buy, !approve, !status etc. from any
# device with Discord. Commands run in-process against the same state and
# guardrails as the dashboard. The gateway runs in its own thread; without a
# token nothing starts.
# ---------------------------------------------------------------------------
_bot_state = {"running": False, "err": ""}
_bot_channel = {"webhook": None, "id": None}


def _bot_channel_id():
    """The channel the webhook posts into - the bot posts there too, so buttons
    land next to the rest of the radar's messages. A GET on the webhook URL
    answers with its channel id; cached per webhook."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return None
    if _bot_channel["webhook"] == wh and _bot_channel["id"]:
        return _bot_channel["id"]
    try:
        with urlopen(Request(wh, headers={"User-Agent": APP_NAME}), timeout=10) as r:
            cid = int(json.loads(r.read().decode("utf-8", "replace")).get("channel_id"))
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord bot: couldn't look up the webhook's channel ({_no_secrets(str(e))})")
        return None
    _bot_channel.update(webhook=wh, id=cid)
    return cid


async def _bot_send(client, channel_id, text, buttons=()):
    import discord
    ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    view = None
    if buttons:
        view = discord.ui.View(timeout=None)
        for label, cid in buttons:
            view.add_item(discord.ui.Button(label=label, custom_id=cid, style=discord.ButtonStyle.primary))
    await ch.send(text[:1900], view=view)


def _bot_post(text, buttons=()):
    """Post as the BOT - a webhook message can't carry a button anyone can
    click. Blocking: call from a worker thread, never from the bot's own loop.
    True when it was sent."""
    import asyncio
    client, loop = _bot_state.get("client"), _bot_state.get("loop")
    if not (_bot_state.get("running") and client and loop):
        return False
    ch_id = _bot_channel_id()
    if not ch_id:
        return False
    try:
        asyncio.run_coroutine_threadsafe(_bot_send(client, ch_id, text, buttons), loop).result(timeout=20)
        return True
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord bot post failed: {_no_secrets(str(e))}")
        return False


def _notify_with_button(text, buttons, fallback):
    """The message with its buttons via the bot, or the plain fallback text via
    the webhook when the bot isn't available. Fire-and-forget."""
    def _go():
        try:
            if not _bot_post(text, buttons):
                _discord_notify(fallback)
        except Exception as e:  # noqa: BLE001
            add_log(f"Discord notify error: {e}")
    threading.Thread(target=_go, daemon=True).start()


def handle_bot_button(custom_id, who="discord"):
    """A button press ('rr:<action>:<arg>') -> reply text. Pure dispatch, like
    the ! commands, so it is testable without Discord."""
    parts = (custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == "rr" and parts[1] == "retry":
        with _lock:
            job = _buy_job_by_id(parts[2])
            if not job:
                return "that purchase job no longer exists."
            if job.get("status") not in BUY_RETRYABLE:
                return f"nothing to retry — {job.get('series', '?')} #{job.get('n')} is {job.get('status')} now."
            _buy_retry(job, who=f"Discord ({who})")
            save_state()
        return (f"\U0001f501 checking Amazon again for {job.get('series', '?')} #{job.get('n')} — "
                "if you're signed in, it carries on by itself.")
    if len(parts) == 3 and parts[0] == "rr" and parts[1] == "download":
        with _lock:
            job = _buy_job_by_id(parts[2])
        if not _bat_path():
            return "Update Audiobooks.bat not found — set its path in Settings."
        _run_update_audiobooks(job, delay=0)
        return "\U0001f4e6 running Update Audiobooks again (tracked series only) — progress appears here."
    return "that button isn't one of mine."


async def _bot_announce_parked(client):
    """Jobs parked for a sign-in without a button yet (posted while the bot was
    offline, or before buttons existed) get one when the bot connects."""
    import asyncio
    with _lock:
        jobs = [j for j in _state.get("buy_jobs", [])
                if j.get("status") == "needs_signin" and not j.get("signin_button_at")]
    if not jobs:
        return
    ch_id = await asyncio.get_running_loop().run_in_executor(None, _bot_channel_id)
    if not ch_id:
        return
    for j in jobs:
        await _bot_send(client, ch_id,
                        f"\U0001f511 Still waiting on an Amazon sign-in to buy {j.get('series', '?')} — book {j.get('n')}. "
                        "Sign in on the PC, then hit the button.",
                        [("I've signed in — retry now", f"rr:retry:{j['id']}")])
        with _lock:
            j["signin_button_at"] = _buy_now()
    with _lock:
        save_state()


def _bot_invite_url():
    """The OAuth invite link for the bot - the application id is public and is
    simply the base64 first segment of the token. Being online (gateway) and
    being IN a server are different things; the server owner must open this
    link once. Permissions: view channels + send messages + read history."""
    tok = (get_settings().get("bot_token") or "").strip()
    if not tok:
        return ""
    try:
        seg = tok.split(".")[0]
        app_id = base64.b64decode(seg + "=" * (-len(seg) % 4)).decode("ascii")
        if not app_id.isdigit():
            return ""
        return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions=68608"
    except Exception:      # noqa: BLE001
        return ""


def _bot_match_series(frag):
    """Series whose name contains the fragment (normalised). Returns (series|None, err)."""
    f = _norm(frag or "")
    if not f:
        return None, "which series? e.g. `!buy primal hunter 15`"
    with _lock:
        hits = [x for x in _state.get("series", []) if f in _norm(x.get("name", ""))]
    if not hits:
        return None, f"no tracked series matches '{frag}'"
    if len(hits) > 1:
        exact = [x for x in hits if _norm(x.get("name", "")) == f]
        if len(exact) == 1:
            return exact[0], None
        return None, "that matches " + ", ".join(sorted(x.get("name", "?") for x in hits)[:5]) + " - be more specific"
    return hits[0], None


def _bot_find_job(frag, statuses):
    with _lock:
        jobs = [j for j in _state.get("buy_jobs", []) if j.get("status") in statuses]
        if frag:
            f = _norm(frag)
            jobs = [j for j in jobs if f in _norm(j.get("series", "")) or f == str(j.get("n"))]
    if not jobs:
        return None, ("nothing is waiting" + (f" that matches '{frag}'" if frag else ""))
    if len(jobs) > 1:
        return None, ("more than one matches: "
                      + "; ".join(f"{j.get('series','?')} #{j.get('n')} ({j.get('status')})" for j in jobs[:5]))
    return jobs[0], None


def _bot_fmt_job(j):
    price = f"${j['price']:.2f}" if isinstance(j.get("price"), (int, float)) else "price ?"
    return f"{j.get('series','?')} #{j.get('n')} \u00b7 {price} \u00b7 {j.get('status')}"


def handle_bot_command(text):
    """'!command args' -> reply string, or None when the message isn't ours.
    Pure dispatch over the same internals the dashboard uses."""
    t = (text or "").strip()
    if not t.startswith("!"):
        return None
    parts = t[1:].split(None, 1)
    cmd = (parts[0] if parts else "").lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("help", "commands"):
        return ("**Release Radar commands**\n"
                "`!status` - radar health + what's next\n"
                "`!jobs` - purchase jobs\n"
                "`!buy <series> [book#]` - price-check a book's audiobook (asks before buying)\n"
                "`!approve [series]` / `!deny [series]` - decide a waiting purchase\n"
                "`!retry [series]` - re-check a stopped purchase now (e.g. after signing back in to Amazon)\n"
                "`!download` - run Update Audiobooks now\n"
                "`!research <series>` - AI web-research on the next book\n"
                "`!holds` - books the filer couldn't place")

    if cmd == "status":
        with _lock:
            n_series = len(_state.get("series", []))
            jobs = _state.get("buy_jobs", [])
        ext_s = int((datetime.now() - _ext_last_poll).total_seconds())
        ext = ("checking in" if ext_s < 120 else f"SILENT for {ext_s // 3600}h {ext_s % 3600 // 60}m")
        up = [r for r in upcoming_of_kind("audiobook", False)[:3]]
        nxt = "\n".join(f"\u2022 {r.get('series','?')} #{r.get('book_n','?')} \u2014 {r.get('date')}" for r in up) or "\u2022 nothing scheduled"
        waiting = [j for j in jobs if j.get("status") == "awaiting_approval"]
        return (f"**Release Radar** \u00b7 {n_series} series \u00b7 extension {ext}\n"
                f"Next audiobooks:\n{nxt}\n"
                + (f"\u23f3 {len(waiting)} purchase(s) waiting for approval \u2014 `!approve`" if waiting else "No purchases waiting."))

    if cmd == "jobs":
        with _lock:
            jobs = list(reversed(_state.get("buy_jobs", [])))[:8]
        if not jobs:
            return "no purchase jobs."
        return "\n".join("\u2022 " + _bot_fmt_job(j) for j in jobs)

    if cmd == "approve":
        job, err = _bot_find_job(arg, ("awaiting_approval",))
        if err:
            return err
        with _lock:
            job["dry_run"] = False
            _buy_approve(job, who="discord")
            save_state()
        return f"\u2705 approved: {_bot_fmt_job(job)} \u2014 buying now."

    if cmd == "retry":
        job, err = _bot_find_job(arg, BUY_RETRYABLE)
        if err:
            return err
        return handle_bot_button(f"rr:retry:{job['id']}", who="!retry")

    if cmd == "deny":
        job, err = _bot_find_job(arg, ("awaiting_approval", "retry_later", "probing", "waiting_offer"))
        if err:
            return err
        with _lock:
            _buy_deny(job, who="discord")
            save_state()
        return f"\u274c denied: {job.get('series','?')} #{job.get('n')}."

    if cmd == "buy":
        m = re.match(r"^(.*?)(?:\s+(\d+))?$", arg)
        frag, n_txt = (m.group(1) or "").strip(), m.group(2)
        srs, err = _bot_match_series(frag)
        if err:
            return err
        if n_txt:
            n = int(n_txt)
        else:
            with _lock:
                ns = [e for e in srs.get("timeline", []) if _entry_n(e) is not None and e.get("audiobook")]
            if not ns:
                return f"{srs.get('name','?')}: no audiobook recorded yet - give a book number"
            n = max(_entry_n(e) for e in ns)
        ok, msg = _buy_for_book(srs["id"], n, source="Discord")
        return (f"\U0001f6d2 {srs.get('name','?')} #{n}: {msg}" if ok else f"can't: {msg}")

    if cmd == "download":
        if not _bat_path():
            return "Update Audiobooks.bat not found - set its path in Settings."
        _run_update_audiobooks(None, delay=0)
        return "\U0001f4e6 running Update Audiobooks - progress appears here."

    if cmd == "research":
        srs, err = _bot_match_series(arg)
        if err:
            return err
        if not _ai_conf():
            return "no AI key set (Settings \u2192 Integrations)."
        threading.Thread(target=_ai_research_series, args=(srs["id"],), kwargs={"manual": True}, daemon=True).start()
        return f"\U0001f50e researching {srs.get('name','?')} - the result lands on its card and in the log."

    if cmd == "holds":
        held = _load_holds()
        if not held:
            return "no held books."
        return "\n".join(f"\u2022 {h.get('book','?')} \u2014 {h.get('reason','')[:120]}" for h in held[:8]) \
               + "\nresolve them in Settings \u2192 Held books."

    return f"unknown command `!{cmd}` - try `!help`"


def _start_discord_bot():
    tok = (get_settings().get("bot_token") or "").strip()
    if not tok:
        return

    def _run():
        try:
            import asyncio
            import discord
        except ImportError:
            _bot_state["err"] = "discord.py is not installed"
            add_log("Discord bot: discord.py is not installed (pip install discord.py)")
            return
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            _bot_state.update(running=True, err="", client=client, loop=asyncio.get_running_loop())
            add_log(f"Discord bot online as {client.user}")
            try:
                await _bot_announce_parked(client)
            except Exception as e:      # noqa: BLE001
                add_log(f"Discord bot: couldn't post retry buttons ({_no_secrets(str(e))})")

        @client.event
        async def on_interaction(interaction):
            # button presses; handled here rather than by View callbacks so a
            # button still works after the app restarts
            cid = (getattr(interaction, "data", None) or {}).get("custom_id") or ""
            if not cid.startswith("rr:"):
                return
            try:
                reply = await asyncio.get_running_loop().run_in_executor(
                    None, handle_bot_button, cid, str(interaction.user))
            except Exception as e:      # noqa: BLE001
                reply = f"error: {e}"
            try:
                base = interaction.message.content if interaction.message else ""
                await interaction.response.edit_message(content=(base + f"\n→ {reply}")[:1990], view=None)
            except Exception:           # noqa: BLE001
                if not interaction.response.is_done():
                    await interaction.response.send_message(reply[:1900])

        @client.event
        async def on_message(message):
            if message.author.bot:
                return
            try:
                reply = handle_bot_command(message.content)
            except Exception as e:      # noqa: BLE001
                reply = f"error: {e}"
            if reply:
                for i in range(0, len(reply), 1900):
                    await message.channel.send(reply[i:i + 1900])

        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            client.run(tok, log_handler=None)
        except Exception as e:          # noqa: BLE001
            hint = " - enable the MESSAGE CONTENT intent in the Developer Portal (Bot page)" \
                   if "intent" in str(e).lower() else ""
            _bot_state.update(running=False, err=str(e))
            add_log(f"Discord bot stopped: {e}{hint}")

    threading.Thread(target=_run, daemon=True).start()



def _find_orphaned_bat_run(update_log, bat_log, max_age_h=2):
    """An Update Audiobooks run the app launched but whose watcher thread died
    (exe swapped mid-run, 25 Aug 2026: the 22:54 run finished at 23:01 into
    silence). Returns the byte offset of the run's header in bat_log, or None.

    In flight = the bat's own log ends with a 'started' line and no DONE/ERROR
    after it, AND the app-side bat_log has a run header newer than the last
    completed run (manual console runs never write bat_log - not ours to watch)."""
    try:
        with open(update_log, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    except OSError:
        return None
    started_at = None
    for l in lines:
        if " Update Audiobooks started" in l:
            started_at = l
        elif started_at and re.search(r"\b(DONE|ERROR)\b", l):
            started_at = None
    if not started_at:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})", started_at)
    if m:
        dd, mo, yy, hh, mi, ss = (int(x) for x in m.groups())
        try:
            if (datetime.now() - datetime(yy, mo, dd, hh, mi, ss)) > timedelta(hours=max_age_h):
                return None                    # stale: the run died long ago
        except ValueError:
            pass
    try:
        with open(bat_log, "rb") as f:
            data = f.read()
    except OSError:
        return None
    i = data.rfind(b"\n=== ")
    if i < 0:
        i = 0 if data.startswith(b"=== ") else -1
    return (i + 1 if i > 0 else i) if i >= 0 else None


def _reattach_orphaned_bat_run():
    """Called at startup: if a bat run is in flight with no watcher, adopt it -
    feed its output so far into a fresh live progress message and post the
    summary when it finishes."""
    bat = _bat_path()
    if not bat:
        return
    update_log = os.path.join(os.path.dirname(bat), "Update Audiobooks.log")
    bat_log = os.path.join(data_dir(), "autobuy-bat.log")
    ofs = _find_orphaned_bat_run(update_log, bat_log)
    if ofs is None:
        return
    add_log("adopting an Update Audiobooks run that was in flight when the app restarted")

    def _watch():
        prog = _BatProgress()
        pos = ofs
        editor = None
        deadline = time.monotonic() + 2 * 3600
        while time.monotonic() < deadline:
            try:
                with open(bat_log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                if chunk:
                    pos += len(chunk)
                    prog.feed(chunk.decode("utf-8", "replace"))
                    if editor is None:
                        editor = _LiveEditor(_discord_live_start(prog.render()))
                    editor.push(prog.render())
            except OSError:
                pass
            try:
                with open(update_log, encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-400:]
                done = None
                for l in tail.splitlines():
                    if " Update Audiobooks started" in l:
                        done = None
                    elif re.search(r"\b(DONE|ERROR)\b", l):
                        done = l
                if done:
                    prog.finished = True
                    prog.rc = 1 if "ERROR" in done else (2 if "held" in done.lower() else 0)
                    if editor is None:
                        editor = _LiveEditor(_discord_live_start(prog.render()))
                    editor.push(prog.render(), force=True)
                    return
            except OSError:
                pass
            time.sleep(5)

    threading.Thread(target=_watch, daemon=True).start()



# ---- audiobook ownership (feeds the per-series "owned x/y" meter) --------
# Owned = Libation's database has the ASIN, OR the book number exists in the
# AudioBooks library folder for the matching series (catches books obtained
# outside Audible and adopted by the filer). Cached: a sqlite hit plus a
# directory walk must never run per page load.
OWN_CACHE_TTL_S = 30 * 60
_own_cache = {"at": 0.0, "series": {}}      # sid -> {"owned": [n...], "missing": [n...]}


def _library_series_index():
    """{normalised series name: set of owned book numbers} from the AudioBooks folder."""
    try:
        import file_audiobooks as _fa
    except Exception:
        return {}
    out = {}
    try:
        _, lib = _fa.scan_library(_fa.DEFAULT_DST)
    except (FileNotFoundError, OSError):
        return {}
    for sd in lib:
        ns = _fa.norm(sd.series)
        nums = out.setdefault(ns, set())
        for b in sd.books:
            k = _fa.book_key(b)
            if k[1] and "-" not in k[1]:
                try:
                    nums.add(int(float(k[1])))
                except ValueError:
                    pass
    return out


def _libation_asin_set():
    d = _libation_files_dir()
    if not d:
        return set()
    try:
        con = sqlite3.connect(_libation_ro_uri(d), uri=True, timeout=2)
        try:
            rows = con.execute("select AudibleProductId from Books").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def _refresh_ownership(force=False):
    now = time.monotonic()
    if not force and (now - _own_cache["at"]) < OWN_CACHE_TTL_S:
        return
    _own_cache["at"] = now                      # even on failure: don't hammer
    owned_asins = _libation_asin_set()
    folder_idx = _library_series_index()
    try:
        import file_audiobooks as _fa
        norm = _fa.norm
    except Exception:
        norm = lambda x: (x or "").lower()      # noqa: E731
    result = {}
    with _lock:
        series_list = [(s.get("id"), s.get("name", ""), dict(s.get("asins_audio") or {}),
                        list(s.get("timeline") or [])) for s in _state.get("series", [])]
    for sid, name, amap, tl in series_list:
        folder_nums = folder_idx.get(norm(name), set())
        owned, missing = [], []
        for e in tl:
            n = _entry_n(e)
            ad = parse_date(e.get("audiobook"))
            if n is None or not ad or ad > today():
                continue                        # only released audiobooks count
            has = (amap.get(str(n)) in owned_asins) or (n in folder_nums)
            (owned if has else missing).append(n)
        result[sid] = {"owned": sorted(owned), "missing": sorted(missing)}
    _own_cache["series"] = result


def _ownership_for(sid):
    return _own_cache["series"].get(sid)



# ---- living release card --------------------------------------------------
# One Discord message per audiobook release that keeps its whole story:
# detected -> approval -> bought -> downloaded -> filed. Appended lines edit
# the same message instead of posting new ones.
RELEASE_CARD_KEEP_DAYS = 7


def _release_card_create(sid, n, text, embed=None):
    url = _discord_live_start(text, embeds=[embed] if embed else None)
    if not url:
        return False
    with _lock:
        _state.setdefault("release_live", {})[f"{sid}:{n}"] = {
            "url": url, "base": text, "lines": [], "at": _buy_now()}
        save_state()
    return True


def _release_card_line(sid, n, line, replace_last=False):
    """Append one progress line to the release card (no-op if none exists)."""
    if sid is None or n is None:
        return False
    with _lock:
        rec = _state.get("release_live", {}).get(f"{sid}:{n}")
        if not rec:
            return False
        lines = rec.setdefault("lines", [])
        if replace_last and lines:
            lines[-1] = line
        elif not lines or lines[-1] != line:
            lines.append(line)
        rec["lines"] = lines[-12:]
        text = (rec["base"] + "\n" + "\n".join("\u2003" + l for l in rec["lines"]))[:1900]
        url = rec["url"]
        save_state()
    threading.Thread(target=_discord_live_edit, args=(url, text), daemon=True).start()
    return True


def _release_card_prune():
    with _lock:
        live = _state.get("release_live", {})
        cutoff = datetime.now() - timedelta(days=RELEASE_CARD_KEEP_DAYS)
        stale = []
        for k, rec in live.items():
            try:
                if datetime.fromisoformat(rec.get("at")) < cutoff:
                    stale.append(k)
            except (TypeError, ValueError):
                stale.append(k)
        for k in stale:
            del live[k]



# ---- Audiobookshelf ------------------------------------------------------
# With abs_url + abs_token set: (1) after new books are filed, poke ABS to
# re-scan its library so they appear in the player immediately; (2) pull
# listening progress so cards can say "listening: book 14 - 82%".
ABS_PROGRESS_TTL_S = 30 * 60
_abs_cache = {"at": 0.0, "series": {}, "lib_id": None, "err": None}


def _abs_conf():
    st = get_settings()
    u, t = (st.get("abs_url") or "").strip().rstrip("/"), (st.get("abs_token") or "").strip()
    if not (u and t):
        return None, None
    return _abs_resolve_base(u, t), t


def _abs_resolve_base(u, t):
    """People paste whatever URL their browser shows — a config page, a library
    view. Probe downwards until /api/libraries answers, cache the winner.
    'http://localhost:13378/audiobookshelf/config' -> 'http://localhost:13378'."""
    if _abs_cache.get("base") and _abs_cache.get("base_src") == u:
        return _abs_cache["base"]
    from urllib.parse import urlparse
    p = urlparse(u if "://" in u else "http://" + u)
    origin = f"{p.scheme}://{p.netloc}"
    segs = [s for s in p.path.split("/") if s]
    cands = [origin + ("/" + "/".join(segs[:k]) if k else "") for k in range(len(segs), -1, -1)]
    for cand in dict.fromkeys(cands):
        try:
            req = Request(cand + "/api/libraries",
                          headers={"Authorization": f"Bearer {t}", "Accept": "application/json",
                                   "User-Agent": APP_NAME})
            with urlopen(req, timeout=6) as resp:
                if json.loads(resp.read().decode("utf-8", "replace")).get("libraries") is not None:
                    _abs_cache["base"], _abs_cache["base_src"] = cand, u
                    if cand != u:
                        add_log(f"Audiobookshelf base resolved: {u} -> {cand}")
                    return cand
        except Exception:       # noqa: BLE001
            continue
    return u                     # let the caller fail loudly with the pasted value


def _abs_req(path, method="GET", timeout=15, payload=None):
    u, t = _abs_conf()
    if not u:
        return None
    headers = {"Authorization": f"Bearer {t}", "Accept": "application/json", "User-Agent": APP_NAME}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(u + path, data=data, method=method, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}


def _abs_library_id():
    if _abs_cache["lib_id"]:
        return _abs_cache["lib_id"]
    try:
        d = _abs_req("/api/libraries") or {}
        libs = d.get("libraries") or []
        book_libs = [l for l in libs if (l.get("mediaType") or "book") == "book"]
        # several book libraries (e.g. "Audiobook Library" + "Dropped Books"):
        # prefer the one that sounds like the main one, never a "dropped" bin
        book_libs.sort(key=lambda l: ("dropped" in (l.get("name") or "").lower(),
                                      "audiobook" not in (l.get("name") or "").lower()))
        if book_libs:
            _abs_cache["lib_id"] = book_libs[0].get("id")
    except Exception as e:      # noqa: BLE001
        _abs_cache["err"] = str(e)
    return _abs_cache["lib_id"]


def _abs_trigger_scan():
    """Ask ABS to rescan its book library (after the filer added something)."""
    lid = _abs_library_id()
    if not lid:
        return False
    try:
        _abs_req(f"/api/libraries/{lid}/scan", method="POST")
        add_log("asked Audiobookshelf to rescan the library")
        return True
    except Exception as e:      # noqa: BLE001
        add_log(f"Audiobookshelf scan request failed: {e}")
        return False


def _abs_refresh_progress(force=False):
    """{norm(series name): {"n": book#, "pct": 0-100, "title": ...}} for books
    currently in progress (not finished)."""
    now = time.monotonic()
    if not force and (now - _abs_cache["at"]) < ABS_PROGRESS_TTL_S:
        return
    _abs_cache["at"] = now
    u, t = _abs_conf()
    if not u:
        return
    try:
        import file_audiobooks as _fa
        normf = _fa.norm
    except Exception:
        normf = lambda x: (x or "").lower()     # noqa: E731
    try:
        me = _abs_req("/api/me") or {}
        prog = [p for p in (me.get("mediaProgress") or [])
                if not p.get("isFinished") and (p.get("progress") or 0) > 0.005]
        prog.sort(key=lambda p: p.get("lastUpdate") or 0, reverse=True)
        out = {}
        for p in prog[:10]:                     # only the most recent listens
            item_id = p.get("libraryItemId")
            if not item_id:
                continue
            try:
                item = _abs_req(f"/api/items/{item_id}") or {}
            except Exception:
                continue
            md = ((item.get("media") or {}).get("metadata") or {})
            series = md.get("seriesName") or ""
            seq = None
            m = re.search(r"#([\d.]+)", series)
            if m:
                seq = m.group(1)
            series = re.sub(r"\s*#[\d.]+\s*$", "", series).strip()
            if not series:
                continue
            key = normf(series)
            if key in out:
                continue                        # keep the most recent per series
            try:
                nnum = int(float(seq)) if seq else None
            except ValueError:
                nnum = None
            out[key] = {"n": nnum, "pct": round((p.get("progress") or 0) * 100),
                        "title": md.get("title") or ""}
        _abs_cache["series"] = out
        _abs_cache["err"] = None
    except Exception as e:      # noqa: BLE001
        _abs_cache["err"] = str(e)


def _abs_short_date(iso, today_iso):
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return str(iso or "")
    s = f"{d.day} {d.strftime('%b')}"
    return s if str(today_iso)[:4] == str(d.year) else f"{s} {d.year}"


def _abs_status_line(v, today_iso=None):
    """ONE plain fact the radar knows that Audiobookshelf doesn't, for a
    series view, or "" when there is nothing to add: an overdue audiobook
    (and by how much), else the next dated release, else the estimate still
    ahead. No emoji, no listening progress (that is ABS's own data). The same
    words as the extension's overlay chip (abs.js rrAbsChipText)."""
    t = today_iso or today().isoformat()
    n = f" {v.get('predicted_book_n')}" if v.get("predicted_book_n") is not None else ""
    est = v.get("predicted_date") or None
    if est and est < t:
        try:
            late = (date.fromisoformat(t) - date.fromisoformat(est[:10])).days
        except ValueError:
            late = 0
        return f"Audio{n} overdue by {late} day{'' if late == 1 else 's'}"
    dated = v.get("next_date") if (v.get("next_date") and not v.get("next_estimate") and v["next_date"] >= t) else None
    # audio facts first - this is an audiobook player; a novel date only when
    # the radar has no audio projection at all
    if dated and v.get("next_kind") == "audiobook":
        return f"Audiobook {_abs_short_date(dated, t)}"
    if est:
        return f"Audio{n} est. {_abs_short_date(est, t)}"
    if v.get("next_est_date") and v["next_est_date"] >= t:
        m = f" {v.get('next_est_book_n')}" if v.get("next_est_book_n") is not None else ""
        return f"Audio{m} est. {_abs_short_date(v['next_est_date'], t)}"
    if dated:
        return f"{'Novel' if v.get('next_kind') == 'novel' else 'Next'} {_abs_short_date(dated, t)}"
    return ""


def _abs_listening_for(series_name):
    try:
        import file_audiobooks as _fa
        key = _fa.norm(series_name)
    except Exception:
        key = (series_name or "").lower()
    return _abs_cache["series"].get(key)


# ---- the radar's line INSIDE Audiobookshelf ---------------------------------
# ABS keeps a per-series description (PATCH /api/series/:id) that none of its
# clients show yet (11 Oct 2026: the web client is being rewritten in React
# and frontend pull requests are paused). It is the official, scan-proof home
# for one line like "Audio 2 overdue by 10 days": the server stores it
# untouched across its updates, the user's patched phone app shows it under the
# series name, and any future client can. A description the radar did not
# write is never touched; a line the radar wrote is removed again the moment
# there is nothing to say.
ABS_SYNC_TTL_S = 3600


def _abs_series_listing():
    """[{id, name, description}] for every series in the book library."""
    lid = _abs_library_id()
    if not lid:
        return []
    out, page = [], 0
    while True:
        d = _abs_req(f"/api/libraries/{lid}/series?limit=500&page={page}&minified=1") or {}
        res = d.get("results") or []
        out.extend({"id": r.get("id"), "name": r.get("name") or "", "description": r.get("description") or ""}
                   for r in res if r.get("id"))
        page += 1
        if not res or len(out) >= (d.get("total") or 0) or page > 20:
            break
    return out


def _abs_status_plan(views, listing, written, today_iso=None):
    """Pure. views: the radar's series views; listing: _abs_series_listing();
    written: {ABS series id: {"text": ...}} the radar put there before.
    -> (writes [(id, name, text)], clears [(id, name)], foreign [(id, name)])
    foreign = a tracked series whose ABS description holds someone else's words."""
    by_key = {}
    for a in listing:
        by_key.setdefault(_series_key(a["name"]), a)

    def find(name):
        key = _series_key(name)
        if not key:
            return None
        if key in by_key:
            return by_key[key]
        # ABS names a series differently ("Stormweaver Series" for "Warformed:
        # Stormweaver"): one name inside the other, when that leaves exactly one
        near = [a for k, a in by_key.items() if len(k) >= 6 and (k in key or key in k)]
        return near[0] if len(near) == 1 else None

    want = {}
    for v in views:
        a = find(v.get("name"))
        if not a:
            continue
        line = _abs_status_line(v, today_iso)
        if line:
            want[a["id"]] = (a, line)
    writes, clears, foreign = [], [], []
    for sid, (a, line) in want.items():
        cur = a.get("description") or ""
        ours = (written.get(sid) or {}).get("text") or ""
        if cur == line:
            continue
        if cur and cur != ours:
            foreign.append((sid, a["name"]))
            continue
        writes.append((sid, a["name"], line))
    for sid, rec in written.items():
        if sid in want:
            continue
        a = next((x for x in listing if x["id"] == sid), None)
        if a is not None and (a.get("description") or "") == (rec.get("text") or "") and rec.get("text"):
            clears.append((sid, a["name"]))
    return writes, clears, foreign


def _abs_sync_status(force=False):
    """Hourly (or on demand): put the radar's line in each tracked series'
    Audiobookshelf description, refresh it when it changes, remove it when
    there is nothing to say. Returns the counts, or None when it didn't run."""
    if not get_settings().get("abs_write_status", True):
        return None
    now = time.monotonic()
    if not force and now - _abs_cache.get("sync_at", 0.0) < ABS_SYNC_TTL_S:
        return None
    _abs_cache["sync_at"] = now
    u, _t = _abs_conf()
    if not u:
        return None
    try:
        listing = _abs_series_listing()
    except Exception as e:      # noqa: BLE001
        _abs_cache["err"] = str(e)
        return None
    with _lock:
        views = [series_view(s) for s in _state.get("series", [])]
        written = dict(_state.get("abs_written") or {})
    writes, clears, foreign = _abs_status_plan(views, listing, written)
    done = {"written": 0, "cleared": 0, "left_alone": len(foreign), "failed": 0}
    for sid, name, text in writes:
        try:
            _abs_req(f"/api/series/{sid}", method="PATCH", payload={"description": text})
            written[sid] = {"text": text, "name": name, "at": datetime.now().isoformat(timespec="seconds")}
            add_log(f"Audiobookshelf: {name} — \"{text}\"")
            done["written"] += 1
        except Exception as e:  # noqa: BLE001
            done["failed"] += 1
            _abs_cache["err"] = str(e)
    for sid, name in clears:
        try:
            _abs_req(f"/api/series/{sid}", method="PATCH", payload={"description": ""})
            written.pop(sid, None)
            add_log(f"Audiobookshelf: {name} — nothing to add now, line removed")
            done["cleared"] += 1
        except Exception as e:  # noqa: BLE001
            done["failed"] += 1
            _abs_cache["err"] = str(e)
    # forget records for series ABS no longer has, or whose words are no longer ours
    just_wrote = {w[0] for w in writes}
    for sid in list(written):
        if sid in just_wrote:
            continue
        a = next((x for x in listing if x["id"] == sid), None)
        if a is None or (a.get("description") or "") != (written[sid].get("text") or ""):
            written.pop(sid, None)
    with _lock:
        _state["abs_written"] = written
        save_state()
    return done


@app.post("/api/abs/sync-now")
def api_abs_sync_now():
    """The Settings button: write the radar's lines into Audiobookshelf now."""
    u, _t = _abs_conf()
    if not u:
        return jsonify({"ok": False, "error": "Audiobookshelf URL and token are not set"}), 400
    if not get_settings().get("abs_write_status", True):
        return jsonify({"ok": False, "error": "writing to Audiobookshelf is switched off"}), 400
    done = _abs_sync_status(force=True) or {}
    return jsonify({"ok": True, **done, "err": _abs_cache.get("err")})


def _abs_clear_status():
    """Take every line the radar wrote back out (the setting was turned off)."""
    with _lock:
        written = dict(_state.get("abs_written") or {})
    for sid, rec in written.items():
        try:
            _abs_req(f"/api/series/{sid}", method="PATCH", payload={"description": ""})
        except Exception as e:  # noqa: BLE001
            _abs_cache["err"] = str(e)
    with _lock:
        _state["abs_written"] = {}
        save_state()
    if written:
        add_log(f"Audiobookshelf: removed the radar's line from {len(written)} series")



# ---- AI series research --------------------------------------------------
# For dormant/overdue series: ask an LLM with WEB SEARCH what's actually going
# on (author announced a date? series concluded? publisher dropped it?) and pin
# the answer to the card instead of guessing from cadence maths alone.
# Provider + key live in Settings (write-only). Raw HTTP on purpose: the exe
# bundles no SDKs, and four providers share one tiny call shape.
AI_INTEL_TTL_DAYS = 14          # re-research a series at most this often
AI_DAILY_MAX = 3                # automatic research calls per day (cost bound)
AI_TIMEOUT_S = 120
AI_AUTH_CACHE_S = 3600          # a CLI sign-in check stays trusted this long
AI_EFFORT = "max"               # a few calls a night; thoroughness is the whole point
AI_FAIL_NIGHTS_ALERT = 2        # whole batches failing this many nights running -> a person

# One headless CLI at a time. Three parallel runs raced to refresh the same
# OAuth token on 11 Sep 2026; the refresh rotates the token, the losers hit the
# server with a stale one and the CLI signed itself out for good - four silent
# nights of "OAuth session expired and could not be refreshed".
_ai_cli_lock = threading.Lock()
_ai_cli_auth = {"ok": None, "at": 0.0}
_ai_worker_busy = False
_ai_worker_since = 0.0
AI_HUNG_S = 1800                # a batch older than this is stuck: say so (the CLI timeouts should make it impossible)

AI_AUTH_WHAT = ("AI research is down: the Claude Code CLI on this PC is signed out, "
                "so the nightly series research can't run.")
AI_AUTH_DETAIL = ("On the PC, open a terminal and run `claude auth login` — or run `claude setup-token` "
                  "and paste the token into Settings → Integrations → AI key. Research resumes by itself.")
AI_AUTH_HINT = ("the Claude Code CLI is signed out on this PC — run `claude auth login` in a terminal "
                "(or paste a `claude setup-token` token in Settings), then try again")
# the same failure class for the API providers is a bad key, not a sign-out
AI_KEY_WHAT = "AI research is down: the {prov} API rejected the key."
AI_KEY_DETAIL = "Paste a valid key in Settings → Integrations → AI key."
AI_KEY_HINT = "the {prov} API rejected the key — paste a valid one in Settings → Integrations, then try again"
_SHIM_EXT = (".cmd", ".bat", ".ps1")


def _ai_cli_exe():
    """The real claude executable. npm installs a claude.cmd shim on Windows;
    running that goes through cmd.exe, which ends the command line at the first
    newline of the (multi-line) prompt - every flag and the JSON instructions
    were silently dropped for weeks (the 7 Sep 2026 'permission' apology).
    Resolve the binary the shim points at; if that fails, hand the shim back so
    the UI still says 'configured' and _ai_call can explain the refusal."""
    import shutil as _sh
    exe = _sh.which("claude")
    if not exe or os.path.splitext(exe)[1].lower() not in _SHIM_EXT:
        return exe
    for cand in (os.path.join(os.path.dirname(exe), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
                 os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")):
        if os.path.isfile(cand):
            return cand
    return exe


def _ai_env(conf):
    """Child environment for the CLI: ours minus every Claude Code / Anthropic
    session variable. Started from a Claude Code session, the app would pass
    CLAUDECODE / CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH etc. down, and the CLI then
    hands its OAuth refresh to a 'host' that doesn't exist; an ANTHROPIC_API_KEY
    would silently bill the API instead of the subscription. A configured
    long-lived token goes in explicitly."""
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith(("CLAUDE", "ANTHROPIC")) or k.upper() in ("TRACEPARENT", "TRACESTATE"))}
    if conf.get("token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = conf["token"]
    return env

AI_DEFAULT_MODELS = {
    "claude-code": "opus",         # a few calls a night, and the answer's quality is the whole point
    "anthropic": "claude-opus-5",
    "openrouter": "perplexity/sonar",
    "openai": "gpt-5-mini",
    "grok": "grok-3-mini",
}

_ai_day = {"date": None, "used": 0}


def _ai_conf():
    st = get_settings()
    key = (st.get("ai_api_key") or "").strip()
    prov = st.get("ai_provider") or "openrouter"
    if prov == "claude-code":
        # runs the local Claude Code CLI under someone's own subscription login -
        # no API key involved; configured iff the CLI is installed
        exe = _ai_cli_exe()
        if not exe:
            return None
        model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS["claude-code"]
        # the key field doubles as an optional long-lived token (`claude setup-token`)
        return {"provider": prov, "key": "", "token": key, "model": model, "exe": exe}
    if not key:
        return None
    model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS.get(prov, "")
    return {"provider": prov, "key": key, "model": model}


def _ai_http(url, payload, headers):
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json", "User-Agent": APP_NAME, **headers})
    with urlopen(req, timeout=AI_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ai_error_kind(msg):
    """auth = a person must sign the CLI in again; limit = wait; other."""
    m = (msg or "").lower()
    if re.search(r"authenticat|oauth|logged in|log in|login|sign in|signed in|credential|\b401\b|\b403\b", m):
        return "auth"
    if re.search(r"usage limit|rate limit|\b429\b|overloaded|too many requests|quota", m):
        return "limit"
    return "other"


def _ai_cli_logged_in(force=False):
    """Cheap pre-flight (no model call): is the Claude Code CLI signed in?
    True/False, or None when it can't tell (old CLI, timeout)."""
    conf = _ai_conf()
    if not conf or conf["provider"] != "claude-code":
        return None
    if conf.get("token"):
        # A long-lived token bypasses the CLI's own session, and `auth status`
        # can't vouch for it - only a real call can. Unknown, not "signed in":
        # otherwise a bad token would be cleared as fixed five minutes after
        # every failure.
        return None
    now = time.time()
    if not force and _ai_cli_auth["at"] and now - _ai_cli_auth["at"] < AI_AUTH_CACHE_S:
        return _ai_cli_auth["ok"]
    if not _ai_cli_lock.acquire(timeout=5):
        return None                     # a research call is running: can't tell, don't wait
    ok = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([conf["exe"], "auth", "status", "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, cwd=data_dir(), creationflags=flags, stdin=subprocess.DEVNULL,
                           env=_ai_env(conf))
        ok = bool(json.loads(r.stdout or "{}").get("loggedIn"))   # exit code is 1 when signed out
    except Exception:                                             # noqa: BLE001
        ok = None
    finally:
        _ai_cli_lock.release()
    _ai_cli_auth.update({"ok": ok, "at": now})
    return ok


def _ai_call(prompt):
    """One web-search-capable completion; returns the model's text or raises."""
    conf = _ai_conf()
    if not conf:
        raise RuntimeError("no AI key configured")
    prov, key, model = conf["provider"], conf["key"], conf["model"]
    if prov == "claude-code":
        # Headless Claude Code with web search, billed to the subscription the
        # CLI is logged into (usage limits apply; a maxed-out window errors).
        # Run from data_dir so it never picks up a project's CLAUDE.md context.
        # The tool set is pinned to web search/fetch (no shell, files or MCP
        # servers), which is what makes skipping the permission prompts safe:
        # in print mode an unapproved tool is silently denied and the model
        # then answers with a "permission hasn't come through" apology instead
        # of research (7 Sep 2026). One CLI at a time - see _ai_cli_lock.
        if os.path.splitext(conf["exe"])[1].lower() in _SHIM_EXT:
            raise RuntimeError("only the npm claude.cmd shim was found and it mangles multi-line "
                               "prompts — reinstall the Claude Code CLI")
        env = _ai_env(conf)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with _ai_cli_lock:
            r = subprocess.run(
                [conf["exe"], "-p", prompt, "--model", model,
                 "--tools", "WebSearch", "WebFetch",
                 "--allowedTools", "WebSearch", "WebFetch",
                 "--permission-mode", "bypassPermissions", "--allow-dangerously-skip-permissions",
                 "--strict-mcp-config", "--effort", AI_EFFORT],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=max(AI_TIMEOUT_S, 240), cwd=data_dir(), creationflags=flags,
                stdin=subprocess.DEVNULL, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({r.returncode}): "
                               f"{(r.stderr or r.stdout or '').strip()[:200] or 'no output'}")
        out = (r.stdout or "").strip()
        if not out:
            raise RuntimeError("claude CLI returned nothing")
        return out
    if prov == "anthropic":
        d = _ai_http("https://api.anthropic.com/v1/messages",
                     {"model": model, "max_tokens": 2048,
                      "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
                      "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
        if d.get("stop_reason") == "refusal":
            raise RuntimeError("model declined the request")
        return "\n".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    if prov == "openai":
        d = _ai_http("https://api.openai.com/v1/responses",
                     {"model": model, "tools": [{"type": "web_search"}], "input": prompt},
                     {"Authorization": f"Bearer {key}"})
        out = []
        for item in d.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        out.append(c.get("text", ""))
        return "\n".join(out)
    if prov == "grok":
        d = _ai_http("https://api.x.ai/v1/chat/completions",
                     {"model": model, "search_parameters": {"mode": "auto"},
                      "messages": [{"role": "user", "content": prompt}]},
                     {"Authorization": f"Bearer {key}"})
        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    # openrouter (default): sonar-style models search on their own
    d = _ai_http("https://openrouter.ai/api/v1/chat/completions",
                 {"model": model, "messages": [{"role": "user", "content": prompt}]},
                 {"Authorization": f"Bearer {key}"})
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def _ai_prompt_for(srs, missed=None):
    tl = sorted([e for e in srs.get("timeline", []) if _entry_n(e) is not None],
                key=lambda e: _entry_n(e))
    hist = "; ".join(f"book {_entry_n(e)}: novel {e.get('novel') or '?'}"
                     + (f", audio {e.get('audiobook')}" if e.get("audiobook") else "")
                     for e in tl[-5:])
    focus = ""
    if missed:
        focus = (f"Based on the author's usual pace, book {(srs.get('novel_n') or 0) + 1} was expected around "
                 f"{missed.isoformat()} and has not appeared - find out why and when it is now expected.\n")
    # the specific gaps in the data are the questions - "research this series"
    # came back as a restatement of the Amazon listings we already had
    questions = []
    novels = [e for e in tl if parse_date(e.get("novel"))]
    if novels:
        last = novels[-1]
        if parse_date(last["novel"]) <= today():
            questions.append(f"novel {_entry_n(last) + 1}: nothing known - when?")
        else:
            questions.append(f"anything announced beyond book {_entry_n(last)} (due {last['novel']})?")
    if any(e.get("audiobook") for e in tl):
        undated = [str(_entry_n(e)) for e in tl if e.get("novel") and not e.get("audiobook")]
        if undated:
            questions.append(f"audiobook{'s' if len(undated) > 1 else ''} for book{'s' if len(undated) > 1 else ''} "
                             f"{', '.join(undated)}: no date known - what has the author or the studio said?")
    if questions:
        focus += "Open questions, in order of importance: " + " / ".join(questions) + "\n"
    author = re.sub(r"^by\s+", "", srs.get("author") or "", flags=re.I) or "unknown author"
    name = srs.get("name", "")
    searches = (f'"{author} {name} audiobook", "{name} book {(_entry_n(novels[-1]) + 1) if novels else "next"} release", '
                f'"site:reddit.com {name} audiobook", "site:royalroad.com {name} author note", "{author} patreon {name}"')
    # links the user keeps in the series notes (its Royal Road page, Patreon, a
    # Discord announcement mirror) are the author's own places: read them first
    links = re.findall(r"https?://\S+", srs.get("notes") or "")
    if links:
        focus += "Read these pages FIRST, they are the author's own: " + " ".join(links[:4]) + "\n"
    # what the browser extension harvested from Royal Road (the research tool
    # itself gets a 403 there): the author's announcements, notes and replies
    rr = srs.get("rr_notes") or {}
    if rr.get("items"):
        lines, size = [], 0
        for it in sorted(rr["items"], key=lambda i: i.get("date") or "", reverse=True):
            line = f"[{(it.get('date') or '')[:10]} · {it.get('chapter', '')[:60]} · {it.get('kind', '')}] {it.get('text', '')[:700]}"
            if size + len(line) > 3500:
                break
            lines.append(line)
            size += len(line)
        focus += ("The author's own words on Royal Road, collected by the browser (newest first) - timing stated "
                  "here is authoritative, quote it in 'said':\n" + "\n".join(lines) + "\n")
    # Schedule-only brief. The first version asked for "1-2 factual sentences"
    # and got trivia back (where the series is serialised, what it is about);
    # the user wants what he'd find himself: the author's own words on WHEN -
    # "book 10 in November, then 11-13 back to back" - with the source.
    return (
        f"You research release dates for ONE book series. Find what the AUTHOR or PUBLISHER has said about "
        f"WHEN the next books come out - novels and audiobooks. Nothing else matters: no plot, no trivia "
        f"(where it is serialised, Kindle Unlimited, awards, narrators), nothing about books already out.\n"
        f"Series: '{name}' by {author}. "
        f"Already known - do NOT repeat any of it, a pre-order listed here is not news: "
        f"{hist or 'nothing recorded'}. Today is {today().isoformat()}.\n{focus}"
        f"Run at least four different searches before answering, such as: {searches}. "
        "Then READ pages, don't stop at search snippets: on Royal Road open the fiction's newest chapters - "
        "the author's note and the author's own replies in the chapter COMMENTS are where 'book 10 is with "
        "the narrator, out in November' gets said. Also the author's Patreon posts, their Discord or Reddit "
        "posts (r/litrpg, r/ProgressionFantasy), their newsletter or website, and studio/publisher announcements "
        "(Aethon, Podium, Soundbooth, Tantor). An Amazon or Audible listing alone is not an answer. "
        "Prefer the author's own words and note WHEN they said it. A plan like 'book 10 in November, then "
        "11 to 13 back to back' is exactly what is wanted - give each book its own entry, with the month. "
        "If all you find is progress (drafting, chapters going up on Patreon), report that as the summary "
        "with its month and give no dated entries. Found nothing beyond what is already known? Then "
        "releases is [] and the summary is exactly \"no release news found\" - never fill the space with "
        "the series' history or where it is published.\n\n"
        "Answer with ONLY this JSON, no other text, no padding:\n"
        '{"status": "ongoing|completed|hiatus|cancelled|unknown",\n'
        ' "releases": [{"n": <book number>, "kind": "novel|audiobook", '
        '"expected": "<YYYY-MM-DD, or YYYY-MM, or \\"early 2027\\" / \\"Q1 2027\\" / \\"spring 2027\\">", '
        '"said": "<their words, max 15 words>", "source": "<who, where, when - e.g. author on Patreon, Sep 2026>"}],\n'
        ' "royalroad": "<the fiction\'s Royal Road URL if you came across it, else null>",\n'
        ' "summary": "<ONE sentence, max 25 words, release timing only; exactly \\"no release news found\\" if there is none>",\n'
        ' "confidence": "high|medium|low"}'
    )


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PART_DAY = {"early": 5, "mid": 15, "late": 25}
_SEASON_MONTH = {"spring": 4, "summer": 7, "autumn": 10, "fall": 10, "winter": 1}


def _intel_parse_when(text):
    """An author's 'when' as (ISO date, precision) or None. Vague forms land on
    a representative day - the 15th of a month, the middle of a quarter or
    season, mid-year - and the precision says how much to trust the day."""
    t = re.sub(r"[,.]", " ", (text or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t or t in ("null", "none", "tba", "tbd", "tbc", "unknown", "unannounced"):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return (t, "day") if parse_date(t) else None
    m = re.fullmatch(r"(\d{4})-(\d{2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{m.group(2)}-15", "month"
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", t):
        return None                                   # ISO-shaped but not a date (2026-13)
    m = re.search(r"\b(?:(early|mid|late)[- ])?([a-z]+) (\d{4})\b", t)
    if m and m.group(2)[:3] in _MONTHS and (len(m.group(2)) <= 3 or m.group(2).startswith(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "sept"))):
        return f"{m.group(3)}-{_MONTHS[m.group(2)[:3]]:02d}-{_PART_DAY.get(m.group(1), 15):02d}", "month"
    m = re.search(r"\bq([1-4]) (\d{4})\b", t)
    if m:
        return f"{m.group(2)}-{int(m.group(1)) * 3 - 1:02d}-15", "quarter"
    m = re.search(r"\bh([12]) (\d{4})\b", t)
    if m:
        return (f"{m.group(2)}-03-31", "half") if m.group(1) == "1" else (f"{m.group(2)}-09-30", "half")
    m = re.search(r"\b(spring|summer|autumn|fall|winter) (?:of )?(\d{4})\b", t)
    if m:
        return f"{m.group(2)}-{_SEASON_MONTH[m.group(1)]:02d}-15", "season"
    m = re.search(r"\b(?:(early|mid|late)[- ])?(\d{4})\b", t)
    if m:
        day = {"early": "02-15", "mid": "06-30", "late": "11-15"}.get(m.group(1), "07-01")
        return f"{m.group(2)}-{day}", "year"
    return None


def _intel_apply(srs, intel):
    """Store each author-stated date on the series ('kind:n' -> record) so the
    estimates use it. A book that already has a real date is left alone.
    Returns the entries applied, for the log."""
    now = _buy_now()
    tl = srs.get("timeline") or []
    out = srs.setdefault("intel_dates", {})
    applied = []
    rr = (intel.get("royalroad") or "").strip()
    if rr.startswith("https://www.royalroad.com/fiction/") and not srs.get("rr_url"):
        srs["rr_url"] = rr                           # next time the browser reads the author's notes first
    for r in intel.get("releases") or []:
        n, kind = r.get("n"), r.get("kind")
        if n is None or kind not in ("novel", "audiobook"):
            continue
        when = _intel_parse_when(r.get("expected"))
        if not when:
            continue
        if any(_entry_n(e) == n and e.get(kind) for e in tl):
            continue                                  # the store already dated it
        out[f"{kind}:{n}"] = {"date": when[0], "precision": when[1], "expected": r.get("expected") or "",
                              "said": r.get("said") or "", "source": r.get("source") or "", "at": now}
        applied.append(f"{kind} {n} ~{r.get('expected')}")
    return applied


def _ai_parse(text):
    m = re.search(r"\{(?:.|\n)*\}", text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict) or "status" not in d:
        return None

    def _s(v, cap):
        return "" if v is None or str(v).strip().lower() in ("", "null", "none") else str(v).strip()[:cap]

    releases = []
    for r in (d.get("releases") or []) if isinstance(d.get("releases"), list) else []:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError):
            n = None
        kind = _s(r.get("kind"), 12).lower()
        releases.append({"n": n, "kind": "audiobook" if kind.startswith("audio") else ("novel" if kind.startswith("novel") else kind),
                         "expected": _s(r.get("expected"), 60), "said": _s(r.get("said"), 160), "source": _s(r.get("source"), 120)})
    return {"status": str(d.get("status", "unknown"))[:12],
            "releases": releases,
            "royalroad": _s(d.get("royalroad"), 200),
            "summary": _s(d.get("summary"), 200),
            "confidence": str(d.get("confidence", ""))[:8]}


def _ai_intel_line(intel):
    """One line for the activity log: the dated entries, else the summary."""
    rel = [f"{r['kind']} {r['n']} ~{r['expected']}" + (f" ({r['source']})" if r.get("source") else "")
           for r in intel.get("releases") or [] if r.get("expected")]
    main = "; ".join(rel) or intel.get("summary") or ""
    if not main or main.lower().startswith("no release news"):
        main = "no release news found"
    st = intel.get("status")
    return f"series {st}: {main}" if st in ("completed", "hiatus", "cancelled") else main


def _ai_research_series(sid, manual=False, why=None):
    """Run one research pass for a series; stores the result on the series and
    pings Discord when the picture changed - always, when the pass was
    triggered by a missed estimate (why="overdue"). Returns (ok, message)."""
    if not _ai_conf():
        return False, "no AI key configured (Settings \u2192 Integrations)"
    with _lock:
        srs = _find(sid)
        if not srs:
            return False, "unknown series"
        missed = predicted_novel_date(srs) if why == "overdue" else None
        n_next = (srs.get("novel_n") or 0) + 1
        prompt = _ai_prompt_for(srs, missed=missed)
        old = (srs.get("intel") or {}).get("status")
        name = srs.get("name", "")
    try:
        raw = _ai_call(prompt)
    except Exception as e:      # noqa: BLE001 - HTTP/auth/parse all end up here
        add_log(f"AI research failed for {name}: {e}")
        if not manual:
            _ai_mark_tried(sid)             # a hand-triggered try spent no budget: keep tonight's slot
        kind = _ai_error_kind(str(e))
        if kind == "auth":
            prov = (_ai_conf() or {}).get("provider") or "claude-code"
            if prov == "claude-code":
                _ai_cli_auth.update({"ok": False, "at": time.time()})
                _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
                return False, AI_AUTH_HINT
            _problem("ai_key", what=AI_KEY_WHAT.format(prov=prov), detail=AI_KEY_DETAIL)
            return False, AI_KEY_HINT.format(prov=prov)
        if kind == "limit":
            return False, "the AI provider's usage limit is hit right now — try again later"
        return False, f"research failed: {e}"
    intel = _ai_parse(raw)
    if not intel:
        add_log(f"AI research for {name}: unparseable answer ({(raw or '')[:120]!r})")
        if not manual:
            _ai_mark_tried(sid)
        return False, "the model's answer wasn't parseable"
    intel["at"] = _buy_now()
    # Nothing is posted anywhere: an author-stated date becomes the card's
    # estimate (with its source on hover) and the schedule, hero and digest
    # pick it up from there - the user reads dates, not research prose.
    with _lock:
        srs = _find(sid)
        if srs is not None:
            srs["intel"] = intel
            applied = _intel_apply(srs, intel)
            save_state()
        else:
            applied = []
    add_log(f"AI research: {name} -> " + ("; ".join(applied) if applied else _ai_intel_line(intel)))
    _ai_cli_auth.update({"ok": True, "at": time.time()})
    _problem_clear("ai_auth", what="AI research is working again.")
    _problem_clear("ai_key", what="AI research is working again.")
    _problem_clear("ai_failing", what="AI research is producing answers again.")
    return True, intel["status"]


# ---- the author's Royal Road notes come first --------------------------------
# Royal Road refuses the research tool (403) but not the browser, so the
# extension collects the author's announcements, notes and comment replies and
# the research reads them. Research for a series with a known Royal Road page
# waits (briefly) for a fresh harvest.
RR_NOTES_FRESH_H = 20
RR_WAIT_S = 600


def _rr_url(srs):
    u = (srs.get("rr_url") or "").strip()
    if not u:
        m = re.search(r"https?://(?:www\.)?royalroad\.com/fiction/\d+[^\s]*", srs.get("notes") or "")
        u = m.group(0) if m else ""
    return u if "royalroad.com/fiction/" in u else ""


def _rr_notes_fresh(srs):
    try:
        return datetime.now() - datetime.fromisoformat((srs.get("rr_notes") or {}).get("at") or "") < timedelta(hours=RR_NOTES_FRESH_H)
    except (TypeError, ValueError):
        return False


def _rr_request(srs):
    """Ask the extension for the author's Royal Road notes when they are stale.
    True while the research should wait for them. Caller holds _lock."""
    if not _rr_url(srs) or _rr_notes_fresh(srs):
        return False
    if datetime.now() - _ext_last_poll > timedelta(minutes=10):
        return False                                  # nobody to fetch them: research without
    try:
        asked = datetime.fromisoformat(srs.get("rr_requested_at") or "")
    except (TypeError, ValueError):
        asked = None
    if asked and datetime.now() - asked < timedelta(seconds=RR_WAIT_S):
        return True                                   # on its way
    if asked and datetime.now() - asked < timedelta(hours=24):
        return False                                  # asked, nothing came: don't hold research up again today
    srs["rr_requested_at"] = _buy_now()
    _push_command(type="rrnotes", sid=srs["id"], url=_rr_url(srs))
    add_log(f"asked the extension for {srs.get('name', '')}'s Royal Road notes before researching")
    return True


def _ai_mark_tried(sid):
    """A series that failed today leaves the automatic batch for the day, so the
    budget isn't spent re-running one broken call every five minutes."""
    with _lock:
        srs = _find(sid)
        if srs is not None:
            srs["ai_tried"] = today().isoformat()


def _novel_overdue(srs):
    """The next NOVEL is overdue: the forecast the card would show has passed
    and nothing is pre-ordered. Only this earns an automatic research call
    (the user's rule) - a missing audiobook or a merely quiet series does not;
    the manual Check button stays available for those."""
    pred = predicted_novel_date(srs)
    if not pred or pred >= today():
        return False
    return not any((parse_date(e.get("novel")) or date.min) >= today() for e in srs.get("timeline") or [])


def _ai_candidates():
    """[(sid, urgent)] for every series with an overdue novel, urgent first.

    Urgent = the estimate was missed since the series was last researched (or
    it never was): that is researched the night the estimate passes, cap or no
    cap - the trigger is the estimate itself, nothing else. The rest are
    periodic re-checks of long-overdue series, every AI_INTEL_TTL_DAYS, within
    the daily cap. Most-overdue first within each group."""
    out = []
    cutoff = datetime.now() - timedelta(days=AI_INTEL_TTL_DAYS)
    with _lock:
        for srs in _state.get("series", []):
            if srs.get("ai_tried") == today().isoformat() or not _novel_overdue(srs):
                continue
            pred = predicted_novel_date(srs)
            try:
                at = datetime.fromisoformat((srs.get("intel") or {}).get("at", ""))
            except (TypeError, ValueError):
                at = None
            urgent = at is None or at.date() <= pred      # nothing researched since the miss
            if not urgent and at > cutoff:
                continue                                   # re-check not due yet
            out.append((0 if urgent else 1, pred, srs["id"], urgent))
    out.sort()
    return [(sid, urgent) for _, _, sid, urgent in out]


def _ai_note_night(results):
    """Two consecutive all-failed batches (parse errors, timeouts, a bad model
    name...) are worth a person; one bad night isn't. Sign-out is reported on
    its own and mustn't be double-counted here."""
    if not results:
        return
    d = today().isoformat()
    with _lock:
        if any(results):
            _state["ai_fail_nights"] = 0
        elif _state.get("ai_fail_day") != d:          # several batches in a day count once
            _state["ai_fail_nights"] = int(_state.get("ai_fail_nights") or 0) + 1
        _state["ai_fail_day"] = d
        n = _state["ai_fail_nights"]
        signed_out = "ai_auth" in (_state.get("problems") or {})
        save_state()
    if n == 0:
        _problem_clear("ai_failing", what="AI research is producing answers again.")
    elif n >= AI_FAIL_NIGHTS_ALERT and not signed_out:
        _problem("ai_failing", what=f"AI research has failed {n} nights running.",
                 detail="The dashboard's activity log has the error; Settings → Integrations has the provider and model.")


def _ai_tick():
    global _ai_worker_busy, _ai_worker_since
    conf = _ai_conf()
    # problems that describe a configuration the user has since walked away
    # from must not outlive it (the banner would prescribe the wrong fix)
    if not conf:
        for key in ("ai_auth", "ai_key", "ai_failing"):
            _problem_clear(key, notify=False)
        return
    _problem_clear("ai_key" if conf["provider"] == "claude-code" else "ai_auth", notify=False)
    if _ai_worker_busy:
        if time.time() - _ai_worker_since > AI_HUNG_S:
            _problem("ai_hung", what="An AI research call has been stuck for over half an hour.",
                     detail="Quit Release Radar from the tray and start it again to free it.")
        return
    _problem_clear("ai_hung", notify=False)   # no worker: whatever was stuck is gone (restart included)
    d = today().isoformat()
    if _ai_day["date"] != d:
        _ai_day.update({"date": d, "used": 0})
    cands = _ai_candidates()
    urgent = [sid for sid, u in cands if u]
    rechecks = [sid for sid, u in cands if not u][: max(0, AI_DAILY_MAX - _ai_day["used"])]
    sids = urgent + rechecks
    with _lock:
        signed_out_open = "ai_auth" in (_state.get("problems") or {})
    if conf["provider"] == "claude-code" and (sids or signed_out_open):
        # The (cached, cheap) sign-in check runs whenever there is work or an
        # open sign-out problem, so research resumes - and the banner clears -
        # within the hour of a re-login instead of waiting for tomorrow's batch.
        signed = _ai_cli_logged_in()
        if signed is False:
            _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
            return                              # the day's budget is kept for when it works
        if signed:
            _problem_clear("ai_auth", what="The Claude Code CLI is signed in again — AI research is back.")
    if not sids:
        return
    # a series with a Royal Road page gets its author's notes harvested first;
    # it joins a later tick once they have arrived (or the wait ran out)
    with _lock:
        waiting = {sid for sid in sids if (srs := _find(sid)) is not None and _rr_request(srs)}
        if waiting:
            save_state()
    urgent = [sid for sid in urgent if sid not in waiting]
    rechecks = [sid for sid in rechecks if sid not in waiting]
    sids = urgent + rechecks
    if not sids:
        return
    _ai_day["used"] += len(rechecks)            # a just-missed estimate is researched cap or no cap
    _ai_worker_busy, _ai_worker_since = True, time.time()

    def _run():
        global _ai_worker_busy
        results = []
        try:
            for sid in sids:                    # one at a time, never in parallel
                results.append(_ai_research_series(sid, why="overdue" if sid in urgent else None)[0])
        except Exception as e:                  # noqa: BLE001 - a state write can fail; never die silently
            add_log(f"AI research batch error: {e}")
        finally:
            _ai_worker_busy = False
        try:
            _problem_clear("ai_hung", what="The stuck AI research call has finished.")
            _ai_note_night(results)
        except Exception as e:                  # noqa: BLE001
            add_log(f"AI research bookkeeping error: {e}")

    threading.Thread(target=_run, daemon=True).start()


_ai_running = set()


@app.post("/api/series/<sid>/research")
def api_series_research(sid):
    """Kick one research pass off in the background and answer at once - the
    call takes 1-3 minutes (web search) and the Check popup must not hang."""
    conf = _ai_conf()
    if not conf:
        return jsonify({"ok": False, "message": "no AI configured (Settings \u2192 Integrations)"}), 400
    if conf["provider"] == "claude-code" and _ai_cli_logged_in(force=True) is False:
        # a two-minute wait for a certain failure helps nobody: say so now
        _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
        return jsonify({"ok": False, "message": AI_AUTH_HINT}), 400
    with _lock:
        if not _find(sid):
            return jsonify({"ok": False, "message": "unknown series"}), 404
        if sid in _ai_running:
            return jsonify({"ok": True, "message": "already researching \u2014 give it a couple of minutes"})
        _ai_running.add(sid)

    with _lock:
        srs = _find(sid)
        wait = bool(srs and _rr_request(srs))
        if wait:
            save_state()

    def _go():
        try:
            if wait:
                for _ in range(RR_WAIT_S // 5):      # the harvest takes a minute or two
                    time.sleep(5)
                    with _lock:
                        s2 = _find(sid)
                        if s2 is None or _rr_notes_fresh(s2):
                            break
            _ai_research_series(sid, manual=True)
        finally:
            _ai_running.discard(sid)

    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"ok": True, "message": ("AI check started \u2014 reading the author's Royal Road notes first; "
                                            "the card's estimate updates in a few minutes." if wait else
                                            "AI check started \u2014 the card's estimate updates in a couple of minutes.")})



def _buy_tick():
    with _lock:
        now = datetime.now()
        changed = False
        for job in _state.get("buy_jobs", []):
            st = job.get("status")
            if st in ("waiting_offer", "needs_signin"):
                try:
                    # a Retry restarts the probe window (window_start)
                    created = datetime.fromisoformat(job.get("window_start") or job.get("created"))
                    last = datetime.fromisoformat(job.get("last_probe") or job.get("created"))
                except (TypeError, ValueError):
                    continue
                if (now - created).days >= BUY_PROBE_DAYS or int(job.get("probes") or 0) >= BUY_PROBE_MAX:
                    _job_set(job, "failed", "still signed out when the window closed — buy it by hand" if st == "needs_signin"
                             else "no companion offer appeared within the window")
                    changed = True
                elif (now - last) >= timedelta(hours=BUY_PROBE_GAP_H):
                    _buy_push_probe(job)
                    changed = True
            elif st == "retry_later":
                try:
                    due = datetime.fromisoformat(job.get("retry_at"))
                except (TypeError, ValueError):
                    due = now
                if now >= due:
                    _job_set(job, "running", f"retrying the purchase (try {int(job.get('store_retries') or 0) + 1})")
                    _push_command(type="buy", phase="execute", job=_job_cmd(job))
                    _buy_live_begin(job)
                    changed = True
            elif st == "awaiting_approval":
                try:
                    if datetime.fromisoformat(job.get("expires")) < now:
                        _job_set(job, "expired", "approval link expired")
                        changed = True
                except (TypeError, ValueError):
                    pass
        if changed:
            save_state()


# ---- routes (local: dashboard + extension)
@app.get("/api/buy/jobs")
def api_buy_jobs():
    with _lock:
        jobs = [dict(j, token_hash=None) for j in reversed(_state.get("buy_jobs", []))]
        ledger = dict(_state.get("buy_ledger", {}))
    return jsonify({"jobs": jobs[:30], "ledger": ledger, "week_auto": _buy_week_count(),
                    "bat": _bat_path() or "", "libation": _libation_files_dir() or ""})


@app.post("/api/buy/report")
def api_buy_report():
    body = request.get_json(force=True, silent=True) or {}
    with _lock:
        job = _buy_job_by_id(str(body.get("job_id") or ""))
        if not job:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        _buy_handle_report(job, str(body.get("phase") or ""), bool(body.get("ok")), body.get("detail") or {})
        _buy_live_update(job)
        save_state()
        status = job["status"]
    return jsonify({"ok": True, "status": status})


@app.post("/api/buy/<jid>/<action>")
def api_buy_action(jid, action):
    with _lock:
        job = _buy_job_by_id(jid)
        if not job:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        if action == "approve" and job["status"] in ("awaiting_approval", "waiting_offer", "dry_done", "failed", "expired", "denied", "cancelled", "retry_later"):
            if job.get("price") is None:
                return jsonify({"ok": False, "error": "no price known yet — probe first"}), 400
            job["dry_run"] = False
            _buy_approve(job, who="dashboard")
        elif action == "deny" and job["status"] in ("awaiting_approval", "probing", "waiting_offer", "running", "retry_later"):
            _buy_deny(job, who="dashboard")
        elif action == "retry":
            _buy_retry(job, who="dashboard")
        elif action == "cancel":
            _job_set(job, "cancelled", "cancelled from dashboard")
        elif action == "returnku":
            if not job.get("ku_borrowed_by_us"):
                return jsonify({"ok": False, "error": "that borrow wasn't made by me"}), 400
            _push_command(type="buy", phase="return", job=_job_cmd(job))
            _job_step(job, "return", True, "asked the extension to return the KU borrow")
        elif action == "notbought" and job["status"] in ("uncertain", "failed", "done_unreturned"):
            led = _state.setdefault("buy_ledger", {})
            if job.get("audio_asin") in led and led[job["audio_asin"]].get("job") == job["id"]:
                del led[job["audio_asin"]]
            job.pop("purchased", None)
            _job_set(job, "failed", "you checked: not bought (unblocked)")
        elif action == "bought" and job["status"] in ("uncertain", "failed", "done_unreturned", "dry_done"):
            _state.setdefault("buy_ledger", {})[job["audio_asin"]] = {
                "series": job.get("series"), "n": job.get("n"), "at": _buy_now(), "job": job["id"],
                "price": job.get("price"), "auto": False}
            job["purchased"] = True
            job["purchased_at"] = _buy_now()
            _job_set(job, "done_unreturned" if job.get("ku_borrowed_by_us") else "done",
                     "you checked: bought" + (" - return the KU borrow by hand" if job.get("ku_borrowed_by_us") else ""))
            _run_update_audiobooks(job, delay=5)
        elif action == "forget":
            _state["buy_jobs"] = [j for j in _state.get("buy_jobs", []) if j.get("id") != jid]
        else:
            return jsonify({"ok": False, "error": f"can't {action} a job that is {job['status']}"}), 400
        save_state()
    return jsonify({"ok": True})


def _buy_for_book(sid, n, source="the timeline"):
    """Queue a verify-mode purchase for one tracked book (any age). Shared by
    the hover-card endpoint and the Discord bot. Returns (ok, message)."""
    st = get_settings()
    if not st.get("buy_master"):
        return False, "auto-buy master switch is off (Settings)"
    with _lock:
        srs = _find(sid)
        if not srs:
            return False, "unknown series"
        audio = (srs.get("asins_audio") or {}).get(str(n))
        kindle = (srs.get("asins_novel") or {}).get(str(n)) or (srs.get("asins") or {}).get(str(n))
        if not audio and not kindle:
            return False, "no store page recorded for this book yet"
        if audio and audio in _state.get("buy_ledger", {}):
            return False, "that audiobook was already bought through the radar"
        existing = _buy_job_for(sid, n)
        if existing:
            return False, f"a job for this book already exists ({existing['status']})"
        if audio and _libation_owns(audio, series=srs.get("name"), n=n, srs=srs):
            return False, "Libation already has this audiobook"
        ent = next((e for e in srs.get("timeline", []) if _entry_n(e) == n), {}) or {}
        if ent.get("ku") is False:
            return False, "not a Kindle Unlimited title - the companion-price flow only works for KU books"
        job = {"id": uuid.uuid4().hex[:10], "series_id": sid, "series": srs.get("name", ""), "n": n,
               "title": (ent.get("title") or f"{srs.get('name', '')} {n}"),
               "audio_asin": audio, "kindle_asin": kindle,
               "store_host": srs.get("store_host") or "www.amazon.com.au",
               "mode": "verify",
               "cap": _clean_buy_cap(srs.get("buy_cap")) or _clean_buy_cap(st.get("buy_cap_default")) or BUY_DEFAULT_CAP,
               "dry_run": False, "status": "probing", "price": None,
               "created": _buy_now(), "updated": _buy_now(), "probes": 0, "last_probe": None,
               "steps": [], "note": f"requested from {source}"}
        _state.setdefault("buy_jobs", []).append(job)
        _state["buy_jobs"] = _state["buy_jobs"][-100:]
        _buy_push_probe(job)
        save_state()
    add_log(f"buy requested from {source}: {job['series']} book {n}")
    return True, ("Checking the live price \u2014 approve from the Discord link or "
                  "Settings \u2192 Purchase jobs (about a minute).")


@app.post("/api/buy/for-book")
def api_buy_for_book():
    """Hover-card Buy button."""
    body = request.get_json(force=True, silent=True) or {}
    sid = str(body.get("series_id") or "")
    try:
        n = int(body.get("n"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "need a book number"}), 400
    ok, msg = _buy_for_book(sid, n)
    if not ok:
        return jsonify({"ok": False, "error": msg}), (404 if msg == "unknown series" else 400)
    return jsonify({"ok": True, "message": msg})


# ---- held books (filer HOLDs): list, resolve, teach ----------------------
def _filer_report_path():
    return os.path.join(data_dir(), "filer-report.json")


def _load_holds():
    """Current HOLDs from the last filer run, only those still on disk."""
    try:
        with open(_filer_report_path(), encoding="utf-8") as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return []
    holds = []
    for r in rep.get("results", []):
        if r.get("action") != "HELD" or not r.get("src"):
            continue
        if not os.path.exists(r["src"]):
            continue                       # resolved by hand / a later run
        holds.append(r)
    return holds


def _hold_candidates(reason):
    """Folder names offered by an 'ambiguous: ... under ['A', 'B']' reason."""
    m = re.search(r"\[([^\]]*)\]", reason or "")
    if not m:
        return []
    return [c.strip().strip("'\"") for c in m.group(1).split(",") if c.strip().strip("'\"")]


@app.get("/api/holds")
def api_holds():
    holds = _load_holds()
    try:
        import file_audiobooks as _fa
        authors = _fa._subdirs(_fa.DEFAULT_DST)
    except Exception:
        authors = []
    out = []
    for h in holds:
        out.append({**h, "candidates": _hold_candidates(h.get("reason"))})
    return jsonify({"holds": out, "authors": authors})


@app.post("/api/holds/resolve")
def api_holds_resolve():
    """Move one held book under the chosen author folder (series kept from the
    hold, or overridden), and TEACH the correction so this spelling never
    holds again."""
    body = request.get_json(force=True, silent=True) or {}
    src = (body.get("src") or "").strip()
    author_folder = (body.get("author") or "").strip()
    series_folder = (body.get("series") or "").strip()
    if not (src and author_folder):
        return jsonify({"ok": False, "error": "need the held path and an author folder"}), 400
    holds = {h["src"]: h for h in _load_holds()}
    h = holds.get(src)
    if not h:
        return jsonify({"ok": False, "error": "that hold is gone (already resolved?)"}), 404
    try:
        import file_audiobooks as _fa
    except Exception as e:
        return jsonify({"ok": False, "error": f"filer module unavailable: {e}"}), 500
    series_folder = series_folder or (h.get("series") or "")
    book = h.get("book") or os.path.basename(src)
    want = _fa.schema_book_name(book, series_folder) or book
    if series_folder:
        dst_dir = os.path.join(_fa.DEFAULT_DST, author_folder, series_folder, want)
    else:
        dst_dir = os.path.join(_fa.DEFAULT_DST, author_folder, want)
    if os.path.exists(dst_dir):
        return jsonify({"ok": False, "error": f"already exists: {dst_dir}"}), 409
    try:
        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        import shutil as _sh
        _sh.move(src, dst_dir)
        _fa._rename_single_audio(dst_dir)
    except OSError as e:
        return jsonify({"ok": False, "error": f"move failed: {e}"}), 500
    # teach: the hold's author string now always means this author folder
    taught = []
    try:
        if h.get("author") and _fa.norm(h["author"]) != _fa.norm(author_folder):
            _fa.save_alias("authors", h["author"], author_folder)
            taught.append(f"author '{h['author']}' -> '{author_folder}'")
        if h.get("series") and series_folder and _fa.norm(h["series"]) != _fa.norm(series_folder):
            _fa.save_alias("series", h["series"], series_folder)
            taught.append(f"series '{h['series']}' -> '{series_folder}'")
    except OSError:
        pass
    _own_cache["at"] = 0.0
    threading.Thread(target=_abs_trigger_scan, daemon=True).start()
    add_log(f"hold resolved: {book} -> {author_folder}/{series_folder or ''}"
            + (f" (learned: {'; '.join(taught)})" if taught else ""))
    return jsonify({"ok": True, "moved_to": dst_dir, "learned": taught})




@app.post("/api/buy/test-email")
def api_buy_test_email():
    ok, msg = _send_email("Release Radar: test email", "If you can read this, approval emails will work.")
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/buy/run-bat")
def api_buy_run_bat():
    if not _bat_path():
        return jsonify({"ok": False, "message": "Update Audiobooks.bat not found"}), 404
    _run_update_audiobooks(None, delay=0)
    return jsonify({"ok": True, "message": "running in the background — outcome goes to Discord and the log"})


@app.post("/api/buy/test-probe")
def api_buy_test_probe():
    """Dashboard: probe any audiobook ASIN once (dry, no job side effects) to see
    what the extension can read on your signed-in pages."""
    body = request.get_json(force=True, silent=True) or {}
    asin = (body.get("audio_asin") or "").strip().upper()
    if not re.fullmatch(r"B0[0-9A-Z]{8}", asin):
        return jsonify({"ok": False, "error": "need an audiobook ASIN like B0H8ZN7RB8"}), 400
    with _lock:
        job = {"id": "probe-" + uuid.uuid4().hex[:6], "series_id": None, "series": "(test probe)", "n": 0,
               "title": asin, "audio_asin": asin, "kindle_asin": (body.get("kindle_asin") or "").strip() or None,
               "store_host": (body.get("store_host") or "www.amazon.com.au"), "mode": "verify",
               "cap": _clean_buy_cap(get_settings().get("buy_cap_default")) or BUY_DEFAULT_CAP,
               "dry_run": True, "status": "probing", "price": None, "created": _buy_now(),
               "updated": _buy_now(), "probes": 0, "steps": [], "note": "manual test probe"}
        _state.setdefault("buy_jobs", []).append(job)
        _buy_push_probe(job)
        save_state()
    return jsonify({"ok": True, "job_id": job["id"]})


# ---- the token page (the one thing the tunnel may POST to)
_BUY_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Release Radar — approve purchase</title><style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#14121a;color:#f1ecff;margin:0;padding:28px 18px}
.card{max-width:520px;margin:0 auto;background:#1e1a29;border:1px solid #35304a;border-radius:14px;padding:22px}
h1{font-size:18px;margin:0 0 12px} .t{font-size:20px;margin:6px 0} .p{font-size:28px;color:#ffd27a;margin:8px 0}
.m{color:#b8b0d0;font-size:14px} form{display:inline-block;margin:16px 8px 0 0}
button{font-size:16px;padding:12px 22px;border-radius:10px;border:0;cursor:pointer}
.ok{background:#3fae6a;color:#fff} .no{background:#5a3157;color:#ffb0b0} .done{font-size:18px;margin-top:14px}
</style></head><body><div class="card">%s</div></body></html>"""


def _buy_find_by_token(token):
    h = hashlib.sha256((token or "").encode()).hexdigest()
    return next((j for j in _state.get("buy_jobs", []) if j.get("token_hash") == h), None)


@app.get("/buy/<token>")
def buy_page(token):
    with _lock:
        job = _buy_find_by_token(token)
        if not job:
            return Response(_BUY_PAGE % "<h1>Unknown or used link</h1><div class='m'>Nothing was done.</div>", mimetype="text/html"), 404
        price = job.get("price")
        ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else "price unknown"
        if job["status"] != "awaiting_approval":
            body = f"<h1>Already decided</h1><div class='t'>{job.get('series','')} — book {job.get('n')}</div><div class='m'>Status: {job['status']}. {job.get('note','')}</div>"
            return Response(_BUY_PAGE % body, mimetype="text/html")
        body = (f"<h1>Buy this audiobook?</h1><div class='t'>{job.get('series','')} — book {job.get('n')}</div>"
                f"<div class='m'>{job.get('title','')}</div><div class='p'>{ptxt}</div>"
                f"<div class='m'>Cap ${float(job.get('cap') or 0):.2f} · {job.get('note','')}</div>"
                f"<div class='m'>Borrows the Kindle edition on KU, buys the audiobook at the companion price, returns the borrow, runs Update Audiobooks.</div>"
                f"<form method='post' action='/buy/{token}/approve'><button class='ok'>Approve</button></form>"
                f"<form method='post' action='/buy/{token}/deny'><button class='no'>Deny</button></form>")
    return Response(_BUY_PAGE % body, mimetype="text/html")


@app.post("/buy/<token>/<decision>")
def buy_decide_route(token, decision):
    with _lock:
        job = _buy_find_by_token(token)
        if not job or job["status"] != "awaiting_approval":
            return Response(_BUY_PAGE % "<h1>Link already used or expired</h1><div class='m'>Nothing was done.</div>", mimetype="text/html"), 410
        try:
            if datetime.fromisoformat(job.get("expires")) < datetime.now():
                _job_set(job, "expired", "approval link expired")
                save_state()
                return Response(_BUY_PAGE % "<h1>This link has expired</h1><div class='m'>Nothing was done.</div>", mimetype="text/html"), 410
        except (TypeError, ValueError):
            pass
        job["token_hash"] = None                      # single use
        if decision == "approve":
            _buy_approve(job, who="link")
            body = f"<h1>Approved</h1><div class='done'>\u2705 Buying {job.get('series','')} — book {job.get('n')}. You'll get a Discord message when it's done.</div>"
        else:
            _buy_deny(job, who="link")
            body = f"<h1>Denied</h1><div class='done'>Nothing will be bought for {job.get('series','')} — book {job.get('n')}.</div>"
        save_state()
    return Response(_BUY_PAGE % body, mimetype="text/html")



@app.post("/api/ext/crawl")
def api_ext_crawl():
    _push_command(type="recrawl")
    return jsonify({"ok": True})


@app.post("/api/ext/refresh")
def api_ext_refresh():
    _push_command(type="refresh")
    return jsonify({"ok": True})


@app.post("/api/rr-notes")
def api_rr_notes():
    """The extension's harvest of the author's own words on Royal Road
    (announcement chapters, author's notes, replies in the comments), kept on
    the series for the research to quote."""
    body = request.get_json(force=True, silent=True) or {}
    items = []
    for it in (body.get("items") or [])[:40]:
        if isinstance(it, dict) and str(it.get("text") or "").strip():
            items.append({"chapter": str(it.get("chapter") or "")[:120], "date": str(it.get("date") or "")[:25],
                          "kind": str(it.get("kind") or "")[:16], "text": str(it.get("text") or "")[:1200]})
    with _lock:
        srs = _find(str(body.get("sid") or ""))
        if not srs:
            return jsonify({"ok": False, "error": "unknown series"}), 404
        srs["rr_notes"] = {"at": _buy_now(), "url": str(body.get("url") or "")[:300],
                           "author": str(body.get("author") or "")[:80], "items": items}
        srs.pop("rr_requested_at", None)
        name = srs.get("name", "")
        save_state()
    add_log(f"Royal Road notes for {name}: {len(items)} item(s)")
    return jsonify({"ok": True, "items": len(items)})


@app.post("/api/ext/crawl-series")
def api_ext_crawl_series():
    """Ask the extension to re-crawl a SINGLE series (find new/updated books for it),
    identified the same way a delete is: by name, with its ASINs as a fallback."""
    body = request.get_json(force=True, silent=True) or {}
    sid = body.get("id") or ""
    with _lock:
        s = next((x for x in _state["series"] if x.get("id") == sid), None)
        if not s:
            return jsonify({"ok": False, "message": "series not found"})
        name = s.get("name", "")
        asins = list((s.get("asins") or {}).values())
    _push_command(type="crawlSeries", series=name, asins=asins)
    add_log(f"Asked the extension to check one series: {name}")
    return jsonify({"ok": True})


@app.route("/api/series/clear", methods=["POST", "OPTIONS"])
def api_series_clear():
    """Wipe ALL tracked series so the radar can be rebuilt purely from Amazon
    crawl data. Backs up data.json first (recoverable by renaming the .bak), and
    keeps your settings. This is what 'use only Amazon data' calls."""
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    forget = bool(body.get("forget"))   # app UI sets this; the extension's own wipe does not
    with _lock:
        _backup_state()
        removed = len(_state.get("series", []))
        _state["series"] = []
        save_state()
    if forget:
        _push_command(type="forgetAll")
    add_log(f"Cleared all tracked series ({removed})." + (" Asked extension to forget all pages." if forget else " (extension keeps its pages for re-crawl.)"))
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/quit")
def api_quit():
    add_log("Quit requested.")

    def _die():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/fetch-url")
def api_fetch_url():
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    parsed = {"title": None, "author": None, "date": None, "audiobook_date": None,
              "image": None, "is_audiobook": None}
    try:
        html_text = fetch_url(url)
        for k, v in parse_product(html_text, url).items():
            if v is not None:
                parsed[k] = v
        parsed["sources"] = ["page"]
    except Exception as e:
        log.info("fetch-url scrape failed: %s", e)
        parsed["sources"] = []
    if not any([parsed.get("title"), parsed.get("audiobook_date"), parsed.get("date")]):
        parsed["error"] = "Couldn't read this link automatically. Fill the fields by hand."
    return jsonify(parsed)


@app.get("/api/settings")
def api_get_settings():
    s = get_settings()
    remote = _is_remote()
    wh = s.get("discord_webhook", "")
    return jsonify({"show_estimates": s.get("show_estimates", True),
                    "show_estimates_novel": s.get("show_estimates_novel", True),
                    "upcoming_mode": s.get("upcoming_mode", "audiobook"),
                    # the webhook is a credential: never hand it out over the tunnel
                    "discord_webhook": ("" if remote else wh),
                    "discord_webhook_set": bool(wh),
                    "remote_readonly": remote,
                    "discord_on_new": s.get("discord_on_new", True),
                    "discord_on_release": s.get("discord_on_release", True),
                    "discord_daily": s.get("discord_daily", False),
                    "discord_daily_hour": s.get("discord_daily_hour", 9),
                    "discord_digest_days": s.get("discord_digest_days", 1),
                    # auto-buy (the app password is write-only: never echoed back)
                    "buy_master": bool(s.get("buy_master")),
                    "buy_dry_run": bool(s.get("buy_dry_run", True)),
                    "buy_cap_default": s.get("buy_cap_default", BUY_DEFAULT_CAP),
                    "buy_weekly_max": s.get("buy_weekly_max", BUY_WEEKLY_MAX),
                    "buy_email_to": ("" if remote else s.get("buy_email_to", "")),
                    "buy_smtp_user": ("" if remote else s.get("buy_smtp_user", "")),
                    "buy_smtp_pass_set": bool(s.get("buy_smtp_pass")),
                    "buy_public_base": s.get("buy_public_base", ""),
                    "buy_bat": s.get("buy_bat", ""),
                    "buy_bat_found": _bat_path() or "",
                    "buy_tab_mode": s.get("buy_tab_mode", "foreground"),
                    "bot_token_set": bool(s.get("bot_token")),
                    "bot_running": _bot_state["running"], "bot_err": _bot_state["err"],
                    "bot_invite": ("" if remote else _bot_invite_url()),
                    "ai_provider": s.get("ai_provider", "openrouter"),
                    "ai_api_key_set": bool(s.get("ai_api_key")),
                    "ai_model": s.get("ai_model", ""),
                    "ai_signed_out": "ai_auth" in (_state.get("problems") or {}),
                    "abs_url": ("" if remote else s.get("abs_url", "")),
                    "abs_token_set": bool(s.get("abs_token")),
                    "abs_write_status": bool(s.get("abs_write_status", True)),
                    "abs_written_count": len(_state.get("abs_written") or {}),
                    "buy_libation_found": _libation_files_dir() or ""})


@app.post("/api/settings")
def api_set_settings():
    body = request.get_json(force=True, silent=True) or {}
    with _lock:
        s = _state.setdefault("settings", {})
        if "show_estimates" in body:
            s["show_estimates"] = bool(body.get("show_estimates"))
        if "show_estimates_novel" in body:
            s["show_estimates_novel"] = bool(body.get("show_estimates_novel"))
        if "upcoming_mode" in body:
            m = body.get("upcoming_mode")
            if m in ("audiobook", "novel"):
                s["upcoming_mode"] = m
        if "discord_webhook" in body:
            new_wh = (body.get("discord_webhook") or "").strip()
            if new_wh != s.get("discord_webhook", ""):
                # a different (or no) webhook: the old one's failures are history
                for key in ("discord", "digest"):
                    (_state.get("problems") or {}).pop(key, None)
                _discord_fail["n"] = 0
            s["discord_webhook"] = new_wh
        for k in ("buy_master", "buy_dry_run"):
            if k in body:
                s[k] = bool(body.get(k))
        for k in ("buy_email_to", "buy_smtp_user", "buy_public_base", "buy_bat", "buy_libation_files", "buy_smtp_host"):
            if k in body:
                s[k] = (body.get(k) or "").strip()
        if body.get("buy_tab_mode") in BUY_TAB_MODES:
            s["buy_tab_mode"] = body["buy_tab_mode"]
        if "buy_smtp_pass" in body and (body.get("buy_smtp_pass") or "").strip():
            s["buy_smtp_pass"] = body["buy_smtp_pass"].strip().replace(" ", "")
        if body.get("buy_smtp_pass_clear"):
            s["buy_smtp_pass"] = ""
        # integration secrets: same write-only rules
        ai_before = (s.get("ai_provider"), s.get("ai_api_key"))
        for sec in ("bot_token", "ai_api_key", "abs_token"):
            if sec in body and (body.get(sec) or "").strip():
                s[sec] = body[sec].strip()
            if body.get(sec + "_clear"):
                s[sec] = ""
        if body.get("ai_provider") in ("openrouter", "anthropic", "openai", "grok", "claude-code"):
            s["ai_provider"] = body["ai_provider"]
        if (s.get("ai_provider"), s.get("ai_api_key")) != ai_before:
            # a new provider/key: whatever the old one's problem was, it's gone,
            # and a cached sign-in verdict must not outlive the config it judged
            for key in ("ai_auth", "ai_key", "ai_failing"):
                (_state.get("problems") or {}).pop(key, None)
            _ai_cli_auth.update({"ok": None, "at": 0.0})
        if "ai_model" in body:
            s["ai_model"] = (body.get("ai_model") or "").strip()
        if "abs_url" in body:
            s["abs_url"] = (body.get("abs_url") or "").strip().rstrip("/")
        if "abs_write_status" in body:
            want = bool(body.get("abs_write_status"))
            if want != bool(s.get("abs_write_status", True)):
                s["abs_write_status"] = want
                # off: take the radar's lines back out of ABS; on: put them in now
                threading.Thread(target=_abs_clear_status if not want else (lambda: _abs_sync_status(force=True)),
                                 daemon=True).start()
        if "buy_cap_default" in body:
            s["buy_cap_default"] = _clean_buy_cap(body.get("buy_cap_default")) or BUY_DEFAULT_CAP
        if "buy_weekly_max" in body:
            try:
                s["buy_weekly_max"] = max(0, int(body.get("buy_weekly_max")))
            except (TypeError, ValueError):
                pass
        if "discord_on_new" in body:
            s["discord_on_new"] = bool(body.get("discord_on_new"))
        if "discord_on_release" in body:
            s["discord_on_release"] = bool(body.get("discord_on_release"))
        if "discord_daily" in body:
            s["discord_daily"] = bool(body.get("discord_daily"))
        if "discord_daily_hour" in body:
            try:
                h = int(body.get("discord_daily_hour"))
                if 0 <= h <= 23:
                    s["discord_daily_hour"] = h
            except (TypeError, ValueError):
                pass
        if "discord_digest_days" in body:
            try:
                dd = int(body.get("discord_digest_days"))
                if dd in (1, 2, 3, 7):
                    s["discord_digest_days"] = dd
            except (TypeError, ValueError):
                pass
    save_state()
    return jsonify({"ok": True})


@app.post("/api/discord/test")
def api_discord_test():
    """Send a test line to the configured (or just-entered) webhook so the user can
    confirm it works before relying on it."""
    body = request.get_json(force=True, silent=True) or {}
    wh = (body.get("discord_webhook") or get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return jsonify({"ok": False, "message": "No webhook URL set."})
    ok, err = _discord_post(wh, _fmt_line("test"))
    if ok:
        _problem_clear("discord", notify=False)
    return jsonify({"ok": ok, "message": "Sent! Check your Discord channel." if ok else f"Failed: {err}"})


@app.post("/api/discord/digest-now")
def api_discord_digest_now():
    """Build and send today's forecast digest immediately (independent of the daily
    schedule — sending one now doesn't skip the scheduled one)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return jsonify({"ok": False, "message": "No webhook URL set."})
    ok, err = _send_digest(wh)
    if ok:
        add_log("Sent a forecast digest to Discord (manual)")
        _problem_clear("digest", notify=False)
        _problem_clear("discord", notify=False)
    return jsonify({"ok": ok, "message": "Sent! Go see what she has to say." if ok else f"Failed: {err}"})


@app.get("/feed.xml")
def feed():
    base = request.host_url.rstrip("/")
    return Response(build_rss(base), mimetype="application/rss+xml",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/calendar.ics")
def calendar_ics():
    # no-cache matters: Cloudflare's edge caches .ics by default (4h TTL), so
    # without this, calendar apps fetching through the tunnel get stale feeds
    return Response(build_ics(), mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=release-radar.ics",
                             "Cache-Control": "no-cache, no-store, must-revalidate"})


BOARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#f1ebdd;
  background:radial-gradient(1100px 700px at 50% -20%,#241f37 0,#15131f 60%);
  min-height:100%;display:flex;align-items:center;justify-content:center;padding:6vh 20px}
.board{width:100%;max-width:560px}
.eyebrow{font-family:"SF Mono","Cascadia Code",Consolas,monospace;font-size:11px;letter-spacing:.22em;
  text-transform:uppercase;color:#6f6883;text-align:center;margin-bottom:6px}
h1{font-family:"Iowan Old Style","Palatino Linotype",Georgia,serif;font-weight:600;font-size:30px;
  text-align:center;margin-bottom:26px;letter-spacing:.2px}
.row{display:flex;align-items:center;gap:16px;padding:15px 18px;border:1px solid #332c47;border-radius:14px;
  background:#1f1c2b;margin-bottom:10px}
.row.soon{border-color:#5a4a25;background:linear-gradient(180deg,#2c2417,#1f1c2b)}
.cd{font-family:"SF Mono","Cascadia Code",Consolas,monospace;font-size:26px;font-weight:600;line-height:1;
  min-width:58px;text-align:center}
.row.soon .cd{color:#e7b14c}
.cd small{display:block;font-size:11px;color:#9b93ac;font-weight:400;margin-top:3px;font-family:system-ui,sans-serif}
.info{flex:1;min-width:0}
.title{font-family:"Iowan Old Style",Georgia,serif;font-size:18px;line-height:1.25}
.meta{color:#9b93ac;font-size:12.5px;margin-top:3px}
.tag{display:inline-block;font-family:"SF Mono",Consolas,monospace;font-size:10px;letter-spacing:.06em;
  text-transform:uppercase;padding:2px 7px;border-radius:6px;margin-left:6px;border:1px solid #5a4a25;
  color:#e7b14c;background:#3a2f1b}
.tag.novel{color:#9d8cff;border-color:#3a3360;background:#272140}
.tag.est{color:#6f6883;border-color:#332c47;background:transparent}
.empty{text-align:center;color:#9b93ac;padding:40px 0}
.foot{text-align:center;margin-top:18px}
.foot a{color:#6f6883;font-size:12px;text-decoration:none;font-family:"SF Mono",Consolas,monospace}
.foot a:hover{color:#e7b14c}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def build_board_html():
    _mode, _est = _upcoming_settings()
    items = upcoming_of_kind(_mode, _est)[:BOARD_ITEM_COUNT]
    rows = []
    for i, it in enumerate(items):
        est = ' <span class="tag est">est</span>' if it["estimate"] else ""
        d = parse_date(it["date"])
        nice = f"{d.strftime('%b')} {d.day}, {d.year}" if d else it["date"]
        cd = it["countdown_days"]
        cd_word = "today" if cd == 0 else ("day" if cd == 1 else "days")
        cd_num = "" if cd == 0 else str(cd)
        kind_lbl = "Audiobook" if it["kind"] == "audiobook" else "Novel"
        kind_cls = "" if it["kind"] == "audiobook" else " novel"
        rows.append(
            f'<div class="row{" soon" if i == 0 else ""}">'
            f'<div class="cd">{cd_num}<small>{cd_word}</small></div>'
            f'<div class="info"><div class="title">{html.escape(it["title"])}</div>'
            f'<div class="meta">{html.escape(nice)}'
            f'<span class="tag{kind_cls}">{kind_lbl}</span>{est}'
            + (f' &middot; {html.escape(it["series"])}' if it["series"] and it["series"] != it["title"] else "")
            + "</div></div></div>"
        )
    kind_word = "novels" if _mode == "novel" else "audiobooks"
    body = "".join(rows) if rows else f'<div class="empty">No {kind_word} upcoming. Turn on estimates in Release Radar, or check back later.</div>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Up Next - Release Radar</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{BOARD_CSS}</style></head>
<body><div class="board">
<div class="eyebrow">Release Radar</div>
<h1>Up next</h1>
{body}
<div class="foot"><a href="/">open full dashboard &rarr;</a></div>
</div></body></html>"""


@app.get("/board")
def board():
    return Response(build_board_html(), mimetype="text/html")


def _find(sid):
    for s in _state["series"]:
        if s["id"] == sid:
            return s
    return None


def _clean_history(raw):
    out = []
    if isinstance(raw, list):
        for pair in raw:
            if not isinstance(pair, dict):
                continue
            n = iso(parse_date(pair.get("novel")))
            a = iso(parse_date(pair.get("audiobook")))
            if n or a:
                out.append({"novel": n, "audiobook": a})
    return out


def _clean_timeline(raw, existing=None):
    """Clean editor rows into timeline entries, preserving the book number (n).
    The editor only shows n/novel/audiobook, but entries carry more (title,
    narrator, cover, pre-order flags) — merge those back in so a hand edit
    doesn't silently wipe them. The novel DATE is the primary identity (it
    follows the row when the user renumbers books to fix an off-by-one); the
    book number is only the fallback. Output is sorted chronologically, the
    same order ingest keeps, so gap recency-weighting stays correct."""
    by_novel, by_n = {}, {}
    for e in (existing or []):
        aux = {k: v for k, v in e.items()
               if k not in ("n", "novel", "audiobook") and v is not None}
        if not aux:
            continue
        nov = iso(parse_date(e.get("novel")))
        if nov:
            # two existing books on one novel date would be ambiguous — disable
            by_novel[nov] = None if nov in by_novel else aux
        n = _entry_n(e)
        if n is not None:
            by_n[n] = aux
    rows = []
    if isinstance(raw, list):
        for e in raw:
            if not isinstance(e, dict):
                continue
            try:
                num = int(e.get("n"))
            except (TypeError, ValueError):
                num = None
            nov = iso(parse_date(e.get("novel")))
            aud = iso(parse_date(e.get("audiobook")))
            if nov or aud or num is not None:
                rows.append((num, nov, aud))
    # Two passes: date identity first, then number fallback — and aux claimed by
    # a date match can't ALSO be handed to another row via its old number.
    assigned, used = {}, set()
    for i, (num, nov, aud) in enumerate(rows):
        aux = by_novel.get(nov) if nov else None
        if aux:
            assigned[i] = aux
            used.add(id(aux))
    for i, (num, nov, aud) in enumerate(rows):
        if i in assigned or num is None:
            continue
        aux = by_n.get(num)
        if aux and id(aux) not in used:
            assigned[i] = aux
            used.add(id(aux))
    out = []
    for i, (num, nov, aud) in enumerate(rows):
        row = dict(assigned.get(i, {}))
        row.update({"n": num, "novel": nov, "audiobook": aud})
        out.append(row)
    out.sort(key=lambda e: (e.get("novel") or e.get("audiobook") or "", _entry_n(e) or 0))
    return out


# ---------------------------------------------------------------------------
# Front-end (inlined so the exe is a single self-contained file)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard page. The real, editable sources live in static/dashboard.html/
# .css/.js (lintable, diffable); they are composed here into the same single
# self-contained page this app has always served. PyInstaller bundles the
# folder via:  --add-data "static;static"  (see build.bat).
# ---------------------------------------------------------------------------
_STATIC_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "static")


def _load_page_html():
    def rd(name):
        # universal newlines: the old r\"\"\"-literal went through Python's source
        # parser, which normalised CRLF - the served page must stay identical
        with open(os.path.join(_STATIC_DIR, name), encoding="utf-8") as f:
            return f.read()
    return (rd("dashboard.html")
            .replace("__RR_CSS__", rd("dashboard.css"))
            .replace("__RR_JS__", rd("dashboard.js")))


PAGE_HTML = _load_page_html()


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def pick_port(preferred):
    import socket
    for p in range(preferred, preferred + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


def _make_tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (21, 19, 31, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), outline=(231, 177, 76, 255), width=4)
    d.ellipse((22, 22, 42, 42), outline=(74, 64, 103, 255), width=3)
    d.ellipse((28, 28, 36, 36), fill=(231, 177, 76, 255))
    return img


def run_in_tray(port, url):
    """Run the server with a system-tray icon (Open / Quit). Returns False if a
    tray can't be created, so the caller can fall back to a plain server."""
    if os.environ.get("RADAR_NO_TRAY") == "1":
        return False
    try:
        import pystray
    except Exception as e:
        log.info("Tray unavailable (%s); running without it.", e)
        return False

    def serve():
        try:
            app.run(host=HOST, port=port, threaded=True, use_reloader=False)
        except Exception as e:
            log.exception("server thread crashed: %s", e)
            os._exit(1)

    threading.Thread(target=serve, daemon=True).start()
    # No auto-open on startup. If a dashboard tab already exists (e.g. your pinned
    # one, even when Firefox has it idle/unloaded), the extension leaves it alone;
    # it only opens a tab when there genuinely isn't one. The tray "Open" below is
    # the explicit way to bring the dashboard up.

    def on_open(icon, item):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def on_quit(icon, item):
        try:
            icon.stop()
        finally:
            os._exit(0)

    try:
        global _tray_icon
        icon = pystray.Icon(
            "ReleaseRadar", _make_tray_image(), "Release Radar",
            menu=pystray.Menu(
                pystray.MenuItem("Open Release Radar", on_open, default=True),
                pystray.MenuItem("Quit", on_quit),
            ),
        )
        _tray_icon = icon
        icon.run()  # blocks until Quit
        return True
    except Exception as e:
        log.info("Could not start tray icon (%s); running without it.", e)
        return False


def _radar_already_on(port):
    """True when another Release Radar instance already answers on this port."""
    try:
        req = Request(f"http://127.0.0.1:{port}/api/instance",
                      headers={"User-Agent": APP_NAME})
        with urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}").get("app") == "release-radar"
    except Exception:
        return False


def main():
    # SINGLE INSTANCE. Two copies sharing one data.json is corruption waiting to
    # happen (each holds its own in-memory state and saves over the other's) and
    # every timed job — the Discord digest included — fires once per copy. This
    # happened: an instance from 11 Aug sat on port 8766 next to the real one and
    # duplicated the digest daily. A second launch now just fronts the first.
    preferred = int(os.environ.get("RADAR_PORT", DEFAULT_PORT))
    if _radar_already_on(preferred):
        print(f"{APP_NAME} is already running at http://127.0.0.1:{preferred}/ — opening it.")
        try:
            webbrowser.open(f"http://127.0.0.1:{preferred}/")
        except Exception:
            pass
        return

    load_state()
    _ensure_line_files()
    _start_daily_digest()
    _reattach_orphaned_bat_run()
    _start_discord_bot()
    port = pick_port(preferred)
    url = f"http://127.0.0.1:{port}/"
    add_log(f"{APP_NAME} started on {url}")

    print(f"{APP_NAME} running at {url}")
    print(f"RSS feed:    {url}feed.xml")
    print(f"New-tab view: {url}board")
    print("Quit from the system-tray icon, the dashboard's Quit button, or close this window.")
    print("Your data lives in:")
    print("   " + DATA_FILE)

    # Prefer a tray icon (so the no-console exe is quittable); fall back to a
    # plain server that you quit from the dashboard or by closing the window.
    if not run_in_tray(port, url):
        app.run(host=HOST, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
