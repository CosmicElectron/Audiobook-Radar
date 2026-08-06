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
import json
import time
import uuid
import random
import html
import statistics
import threading
import logging
import webbrowser
from datetime import datetime, date, timedelta
from email.utils import format_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

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
        return dict(s)


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
        if prod is None and queue is None:
            continue
        if prod is None:
            est, queued = queue, True
        elif queue is None:
            est, queued = prod, False
        else:
            est, queued = (queue, True) if queue > prod else (prod, False)
        out.append({"n": _entry_n(e), "date": est, "novel": nd,
                    "queued": queued, "after_n": last_n})
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
    avg = predicted_gap_days(novel_gap_days_list(series))
    if avg is None:
        return None
    return nd + timedelta(days=avg)


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
    r"\s*(?:,\s*)?(?:see all|see more|and others?|et al\.?|,)\s*$", re.I)


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
    has_future_audio = any(
        (lambda d: d and d >= t)(parse_date(e.get("audiobook")))
        for e in s.get("timeline", []))
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


def _novel_estimate(series):
    """(predicted_date, note) for the next-novel forecast, or None."""
    pred = predicted_novel_date(series)
    if not pred or pred < today():
        return None
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
                "confidence": _estimate_confidence(gaps),
                "countdown_days": (nd - t).days,
                "cover": (ent.get("cover") if ent else None) or s.get("cover_url") or None,
            })
    rows.sort(key=lambda r: (r["date"], r["estimate"]))
    return rows


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
        return True, ""
    except HTTPError as e:
        if embeds:
            try:
                _send({"content": ((fallback_text or text) or "")[:1900]})
                return True, ""
            except Exception:
                pass
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(getattr(e, "reason", e))
    except Exception as e:
        return False, str(e)


def _discord_notify(text, embeds=None):
    """Fire-and-forget Discord message (no-op if no webhook configured)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return

    def _go():
        ok, err = _discord_post(wh, text, embeds)
        if not ok:
            add_log(f"Discord notify failed: {err}")

    threading.Thread(target=_go, daemon=True).start()


def _book_embed(series, n, cover):
    """Small embed that puts the book's cover art beside a ping (None if no art)."""
    if not cover:
        return None
    title = f"{series} — Book {n}" if n is not None else (series or "")
    return {"title": title[:256], "thumbnail": {"url": cover}, "color": KIND_COLOR["audiobook"]}


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
    """Discord lines for dates that were just added or changed (today or later only,
    so backfilling a series' old catalogue doesn't spam)."""
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
            msgs.append(_fmt_line("date_changed", icon=icon[kind], series=name, n=n,
                                  kind=kind, when=when, was=_discord_ts(old, 'D')))
        else:
            msgs.append(_fmt_line("new_date", icon=icon[kind], series=name, n=n,
                                  kind=kind, when=when))
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
}
_LINE_PLACEHOLDERS = {
    "new_date": "{icon} {series} {n} {kind} {when}",
    "date_changed": "{icon} {series} {n} {kind} {when} {was}",
    "out_now": "{icon} {series} {n} {kind}",
    "more": "{count}",
    "narrator": "{icon} {series} {n} {narrator} {was}",
}
# used verbatim if a user-edited template has a typo'd placeholder
_LINE_SAFE = {
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
    else:
        add_log("Sent the daily forecast digest to Discord")


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


@app.after_request
def allow_cors(resp):
    # This is a personal, localhost-only app. Allow any local page (e.g. a
    # custom new-tab extension) to read the feed and state.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
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


@app.get("/favicon.svg")
def favicon_svg():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")


@app.get("/api/state")
def api_state():
    return jsonify(build_state_payload())


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
        released_msgs = []
        narrator_msgs = []
        if pre is not None:
            kind_key = "audio_pre" if ad else "novel_pre"
            kind = "audiobook" if ad else "novel"
            old_pre = ent.get(kind_key)
            ent[kind_key] = bool(pre)
            d_now = ad if ad else nd
            dd = parse_date(d_now) if d_now else None
            fire = False
            if pre is False and dd:
                if old_pre is True and dd <= today():
                    fire = True          # we watched it flip from pre-order to buyable
                elif dd == today():
                    fire = True          # buyable on its release day IS the release,
                                         # even if we never saw its pre-order state
            if fire:
                key = f"release:{s['id']}:{n}:{kind}:{d_now}"
                notified = set(_state.get("discord_notified", []))
                if key not in notified and get_settings().get("discord_on_release", True):
                    notified.add(key)
                    _state["discord_notified"] = list(notified)[-500:]
                    released_msgs.append(_release_live_message(s.get("name", ""), n, kind))
        if nd and not (backfill and ent.get("novel")):
            ent["novel"] = nd                 # Amazon is authoritative for this field
        if ad and not (backfill and ent.get("audiobook")):
            ent["audiobook"] = ad
        narr = clean_narrator(book.get("narrator"))
        if ad and narr:
            # A narrator swap is news to an audiobook listener — compare against
            # the nearest EARLIER book that names one, ping once per book+narrator.
            prev = None
            for e2 in tl:
                n2 = _entry_n(e2)
                if (n2 is not None and n is not None and n2 < n
                        and (e2.get("narrator") or "").strip()):
                    if prev is None or n2 > prev[0]:
                        prev = (n2, e2["narrator"].strip())
            was_new = (ent.get("narrator") or "").strip() != narr
            ent["narrator"] = narr
            adt_n = parse_date(ent.get("audiobook"))
            # today-or-later only — backfilling an old catalogue must not spam a
            # ping per historical swap (same guard as _new_date_messages)
            if was_new and prev and _norm(prev[1]) != _norm(narr) and adt_n and adt_n >= today():
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
        save_state()
    for m in new_date_msgs + released_msgs + narrator_msgs:
        _discord_notify(m, embeds=[_book_embed(series_name, n, cover_for_ping)])
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


@app.post("/api/ext/crawl")
def api_ext_crawl():
    _push_command(type="recrawl")
    return jsonify({"ok": True})


@app.post("/api/ext/refresh")
def api_ext_refresh():
    _push_command(type="refresh")
    return jsonify({"ok": True})


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
    return jsonify({"show_estimates": s.get("show_estimates", True),
                    "show_estimates_novel": s.get("show_estimates_novel", True),
                    "upcoming_mode": s.get("upcoming_mode", "audiobook"),
                    "discord_webhook": s.get("discord_webhook", ""),
                    "discord_on_new": s.get("discord_on_new", True),
                    "discord_on_release": s.get("discord_on_release", True),
                    "discord_daily": s.get("discord_daily", False),
                    "discord_daily_hour": s.get("discord_daily_hour", 9),
                    "discord_digest_days": s.get("discord_digest_days", 1)})


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
            s["discord_webhook"] = (body.get("discord_webhook") or "").strip()
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

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Release Radar</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
:root{
  --bg:#15131f; --bg2:#1b1827; --card:#211d30; --card2:#272237;
  --line:#332c47; --ink:#f1ebdd; --muted:#9b93ac; --faint:#6f6883;
  --amber:#e7b14c; --amber-dim:#3a2f1b;
  --teal:#62bd9c; --teal-dim:#1d3330;
  --violet:#9d8cff; --violet-dim:#272140;
  --rose:#d98a8a;
  --radius:14px; --mono:"SF Mono","Cascadia Code","JetBrains Mono",Consolas,monospace;
  --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,#241f37 0,var(--bg) 60%);
  color:var(--ink);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--amber);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1060px;margin:0 auto;padding:28px 22px 80px}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;margin-bottom:8px}
.brand{display:flex;align-items:center;gap:13px}
.logo{width:40px;height:40px;flex:0 0 auto}
h1{font-family:var(--serif);font-weight:600;font-size:30px;letter-spacing:.2px;margin:0}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{font-family:inherit;font-size:14px;border-radius:10px;border:1px solid var(--line);
  background:var(--card2);color:var(--ink);padding:9px 14px;cursor:pointer;transition:.15s}
button:hover{border-color:#4a4067;background:#2c2640}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.btn-primary{background:var(--amber);color:#1a1408;border-color:var(--amber);font-weight:600}
.btn-primary:hover{background:#f0c062;border-color:#f0c062}
.ghost{background:transparent}
.meta-line{display:flex;gap:16px;align-items:center;color:var(--faint);font-size:12.5px;margin:14px 0 22px;flex-wrap:wrap}
.meta-line .mono{font-family:var(--mono)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--teal);display:inline-block;box-shadow:0 0 0 3px var(--teal-dim)}
.pulse{animation:p 2.4s infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.4}}

.section-eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--faint);margin:30px 0 12px}

/* hero: next three */
.hero{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.up{position:relative;background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);
  border-radius:var(--radius);padding:16px;min-height:128px;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}
.up.soonest{border-color:#5a4a25;background:linear-gradient(180deg,#2c2417,var(--card))}
.up .count{font-family:var(--mono);font-size:30px;font-weight:600;line-height:1}
.up.soonest .count{color:var(--amber)}
.up .count small{font-size:13px;color:var(--muted);font-weight:400;margin-left:6px;font-family:var(--sans)}
.up .t{font-family:var(--serif);font-size:17px;margin-top:10px;line-height:1.25}
.up .d{color:var(--muted);font-size:12.5px;margin-top:4px}
.kind{display:inline-block;font-size:10.5px;font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase;
  padding:2px 7px;border-radius:6px;border:1px solid var(--line);color:var(--muted)}
.kind.audio{color:var(--amber);border-color:#5a4a25;background:var(--amber-dim)}
.kind.novel{color:var(--violet);border-color:#3a3360;background:var(--violet-dim)}
.kind.est{color:var(--faint)}

/* top row: header + meta on the left, the overdue panel filling the space right */
.topgrid{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,380px);
  gap:20px;align-items:start}
@media(max-width:900px){.topgrid{grid-template-columns:1fr}}

/* overdue panel */
.overdue{display:flex;flex-direction:column;gap:5px;max-height:196px;overflow-y:auto;
  padding-right:4px;scrollbar-width:thin;scrollbar-color:#4a4067 transparent}
.overdue::-webkit-scrollbar{width:8px}
.overdue::-webkit-scrollbar-thumb{background:#4a4067;border-radius:4px}
.overdue::-webkit-scrollbar-track{background:transparent}
.od{display:flex;align-items:center;gap:9px;padding:6px 9px;border:1px solid #5a3540;
  border-radius:10px;background:linear-gradient(180deg,#2b1d24,var(--card))}
.od .late{font-family:var(--mono);font-size:17px;font-weight:600;line-height:1;
  color:var(--rose);min-width:40px;text-align:center;flex:0 0 auto}
.od .late small{display:block;font-size:9px;color:var(--muted);font-weight:400;
  margin-top:2px;font-family:var(--sans);letter-spacing:.02em}
.od .cov{width:24px;height:36px;object-fit:cover;border-radius:3px;border:1px solid var(--line);flex:0 0 auto}
.od .info{flex:1;min-width:0}
.od .t{font-family:var(--serif);font-size:12.5px;line-height:1.25;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.od .d{color:var(--faint);font-size:10px;font-family:var(--mono);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* series cards */
.sortbar{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:2px 0 10px}
.sortbar .left{display:flex;align-items:center;gap:8px}
.sortbar .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
.sortbar select{width:auto;padding:5px 9px;font-size:12.5px}
.grid{display:flex;flex-direction:column;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:10px 12px;
  display:flex;gap:11px;align-items:flex-start;flex-wrap:wrap}
.card.dragging{opacity:.4}
.card.dragover{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber) inset}
.grip{cursor:grab;color:var(--faint);font-size:14px;line-height:1.1;align-self:center;user-select:none;letter-spacing:1px}
.grip:active{cursor:grabbing}
.cover{width:60px;height:90px;object-fit:cover;border-radius:6px;border:1px solid var(--line);background:var(--bg2);flex:0 0 auto}
.cover.ph{display:flex;align-items:center;justify-content:center;color:var(--faint);font-family:var(--serif);font-size:24px}
.cmain{flex:1 1 240px;min-width:0}
.card h3{font-family:var(--serif);font-weight:600;font-size:16px;margin:0 0 1px;line-height:1.2}
.byline{color:var(--muted);font-size:11.5px;margin-bottom:7px}
.facts{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:7px}
.fact .k{font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--faint)}
.fact .v{font-family:var(--mono);font-size:12px;margin-top:1px}
/* which volume this column is about — often not the one on the Novel line */
.fact .bk{color:var(--faint);font-size:11px}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;border:1px solid}
.pill.released{color:var(--teal);border-color:#2c5147;background:var(--teal-dim)}
.pill.announced{color:var(--amber);border-color:#5a4a25;background:var(--amber-dim)}
.pill.none,.pill.unknown{color:var(--muted);border-color:var(--line);background:var(--bg2)}
.predict{margin-top:5px;font-size:11.5px;color:var(--violet)}
.predict .mono{font-family:var(--mono)}
.err{margin-top:6px;font-size:11.5px;color:var(--rose)}
.changed{margin-top:6px;font-size:11.5px;color:var(--teal)}
.chg-time{color:var(--faint)}
.right{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex:0 0 auto;margin-left:auto}
.rowbtns{display:flex;gap:5px}
.rowbtns button{padding:5px 9px;font-size:12px}

/* gap timeline signature */
.timeline{margin-top:7px;padding-top:7px;border-top:1px dashed var(--line)}
.tl-row{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:10.5px;color:var(--muted);margin:3px 0}
.track{position:relative;flex:1;height:2px;background:var(--line);border-radius:2px;min-width:80px;margin-right:40px}
.tl-end{position:absolute;top:7px;font:500 10px/1 var(--mono,monospace);color:var(--faint);white-space:nowrap;letter-spacing:.02em}
.tl-row{padding-bottom:12px}
.tl-today{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--faint);opacity:.7;transform:translateX(-50%)}
.tl-today::after{content:'';position:absolute;top:-2px;left:-2px;width:5px;height:5px;border-radius:50%;background:var(--faint);opacity:.9}
a.plain{color:inherit;text-decoration:none}a.plain:hover h3,a.plain:hover{text-decoration:underline;text-decoration-color:var(--faint)}
a.mark{text-decoration:none}
.mark{position:absolute;top:50%;transform:translate(-50%,-50%);width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 0 1.5px var(--card)}
.mark.novel{color:var(--violet)}
.mark.audio{color:var(--amber)}
.mark.both{width:11px;height:11px;color:var(--amber);background:linear-gradient(135deg,var(--violet) 0 50%,var(--amber) 50% 100%)}
.mark.est{color:var(--amber);background:transparent;border:1.5px dashed currentColor;box-shadow:none}
.mark.future{background:transparent;border:2px solid currentColor;box-shadow:none}
/* hollow = upcoming, but a same-day novel+audiobook must stay half violet /
   half amber: the transparent background drops the gradient, so the split is
   carried by the border instead (violet top-left, amber bottom-right) */
.mark.future.both{background:transparent;border:2px solid;
  border-color:var(--violet) var(--amber) var(--amber) var(--violet)}
.gaplabel{flex:0 0 auto;color:var(--ink)}
.hovercard{position:fixed;z-index:70;display:flex;gap:10px;align-items:flex-start;background:var(--card2);
  border:1px solid #4a4067;border-radius:10px;padding:9px 11px;max-width:270px;
  box-shadow:0 10px 30px rgba(0,0,0,.5);pointer-events:none}
.hovercard img{width:44px;height:66px;object-fit:cover;border-radius:5px;border:1px solid var(--line);flex:0 0 auto}
.hovercard .hc-t{font-family:var(--serif);font-size:13px;line-height:1.25;margin-bottom:3px}
.hovercard .hc-d{font-family:var(--mono);font-size:10.5px;color:var(--muted);line-height:1.4}
.hovercard .hc-n{font-size:10.5px;color:var(--faint);margin-top:3px}
.legend{display:flex;gap:14px;margin:-2px 0 10px;font-size:10.5px;color:var(--faint);font-family:var(--mono);flex-wrap:wrap}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend .sw{width:9px;height:9px;border-radius:50%;display:inline-block}
.legend .sw.novel{background:var(--violet)}
.legend .sw.audio{background:var(--amber)}
.legend .sw.both{width:11px;height:11px;background:linear-gradient(135deg,var(--violet) 0 50%,var(--amber) 50% 100%)}
.legend .sw.est{background:transparent;border:1.5px dashed var(--amber)}
.legend .sw.upc{background:transparent;border:2px solid var(--muted)}
.legend .sw.bothupc{width:11px;height:11px;background:transparent;border:2px solid;
  border-color:var(--violet) var(--amber) var(--amber) var(--violet)}
.legend .sw.today{width:1px;height:11px;border-radius:0;background:var(--faint)}

/* empty */
.empty{text-align:center;padding:64px 20px;border:1px dashed var(--line);border-radius:var(--radius);background:var(--bg2)}
.empty h2{font-family:var(--serif);font-weight:600;margin:0 0 8px}
.empty p{color:var(--muted);max-width:440px;margin:0 auto 18px}

/* modal */
.scrim{position:fixed;inset:0;background:rgba(8,6,14,.66);display:none;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto;z-index:50}
.scrim.open{display:flex}
.modal{background:var(--bg2);border:1px solid var(--line);border-radius:16px;width:100%;max-width:560px;padding:22px}
.modal h2{font-family:var(--serif);font-weight:600;margin:0 0 16px;font-size:22px}
label{display:block;font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:var(--faint);margin:12px 0 5px}
input,select,textarea{width:100%;background:var(--card);border:1px solid var(--line);color:var(--ink);
  border-radius:9px;padding:9px 11px;font-family:inherit;font-size:14px}
textarea{resize:vertical;min-height:54px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hist-row{display:grid;grid-template-columns:46px 1fr 1fr auto;gap:8px;margin-bottom:8px;align-items:center}
.hist-row input{font-family:var(--mono);font-size:12.5px}
.linkrow{display:flex;gap:8px;align-items:flex-end}
.linkrow input{flex:1}
.hint{font-size:12px;color:var(--faint);margin-top:6px}
.modal-actions{display:flex;justify-content:space-between;gap:10px;margin-top:22px;align-items:center}
.del-link{color:var(--rose);background:none;border:none;cursor:pointer;font-size:13px;padding:6px}
.small{padding:6px 10px;font-size:12.5px}
.activity{margin-top:14px;font-family:var(--mono);font-size:11.5px;color:var(--faint)}
.activity div{padding:2px 0}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--card2);border:1px solid var(--line);
  padding:10px 16px;border-radius:10px;font-size:13px;display:none;z-index:60}
.toast.show{display:block}
.cand{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;border:1px solid var(--line);
  border-radius:10px;margin-bottom:6px;cursor:pointer;transition:.12s}
.cand:hover{border-color:#4a4067;background:var(--card2)}
.cand.sel{border-color:var(--amber);background:var(--amber-dim)}
.cand .meta{color:var(--faint);font-size:12px;font-family:var(--mono);white-space:nowrap}
@media(max-width:760px){.hero{grid-template-columns:1fr}.card{grid-template-columns:1fr}.right{flex-direction:row;align-items:center;flex-wrap:wrap}.two{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
 <div class="topgrid">
  <div>
  <header>
    <div class="brand">
      <svg class="logo" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <circle cx="20" cy="20" r="18.5" stroke="#332c47"/>
        <circle cx="20" cy="20" r="12" stroke="#4a4067"/>
        <path d="M8 20a12 12 0 0 1 12-12" stroke="#e7b14c" stroke-width="2.4" stroke-linecap="round"/>
        <circle cx="20" cy="20" r="3.2" fill="#e7b14c"/>
      </svg>
      <div>
        <h1>Release Radar</h1>
        <div class="sub">Your audiobook &amp; novel release shelf — collected automatically by the Release Radar browser extension.</div>
      </div>
    </div>
    <div class="toolbar">
      <a class="ghost" id="feedLink" href="/feed.xml" target="_blank"><button class="ghost small">RSS feed</button></a>
      <a class="ghost" id="calLink" href="/calendar.ics" target="_blank" title="iCalendar feed — subscribe from Google/Outlook/your phone's calendar app using this URL. Confirmed releases only, as all-day events."><button class="ghost small">&#128197; Calendar</button></a>
      <button class="ghost small" id="settingsBtn">&#9881; Settings</button>
      <button class="ghost small" id="quitBtn">&#11096; Quit</button>
      <button class="ghost small" id="extRefreshBtn" title="Ask the browser extension to re-check pending pages (pre-orders / missing audiobooks) for new dates. Takes a few seconds to start.">&#8635; Refresh dates</button>
      <button class="btn-primary" id="extCrawlBtn" title="Ask the browser extension to re-crawl every series and find newly-released books. Slower; takes a few seconds to start.">Crawl all</button>
    </div>
  </header>

  <div class="meta-line">
    <span><span class="dot pulse"></span> <span id="lastCheck">waiting for the extension…</span></span>
    <span id="countLine" class="mono"></span>
  </div>
  </div>

  <div id="overdueWrap" style="display:none">
    <div class="section-eyebrow" style="margin:2px 0 8px" title="Audiobooks whose expected window has passed with no announcement. They're kept out of the upcoming schedule — a date that already failed isn't a forecast — and nothing behind them can be predicted until they land.">Overdue audiobooks <span id="overdueCount"></span></div>
    <div class="overdue" id="overdue"></div>
  </div>
 </div>

  <div id="heroWrap" style="display:none">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px">
      <div class="section-eyebrow" id="heroTitle" style="margin:0">Next audiobook releases</div>
      <div style="display:flex;gap:8px">
        <button id="modeToggle" type="button" title="Switch the upcoming list between novels and audiobooks" style="font:inherit;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;padding:4px 11px;font-size:12px">⇄ Show novels</button>
        <button id="estToggle" type="button" style="font:inherit;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;padding:4px 11px;font-size:12px">Estimates</button>
      </div>
    </div>
    <div class="hero" id="hero"></div>
  </div>

  <div class="section-eyebrow" id="seriesEyebrow" style="display:none">Tracked series</div>
  <div class="sortbar" id="sortbar" style="display:none">
    <div class="left">
      <span class="lbl">Sort</span>
      <select id="sortSel">
        <option value="manual">Manual (drag to reorder)</option>
        <option value="next">Next release</option>
        <option value="title">Title A–Z</option>
        <option value="status">Status</option>
      </select>
    </div>
  </div>
  <div class="legend" id="legend" style="display:none">
    <span><i class="sw novel"></i>novel</span>
    <span><i class="sw audio"></i>audiobook</span>
    <span><i class="sw both"></i>same day</span>
    <span><i class="sw bothupc"></i>same day, upcoming</span>
    <span><i class="sw upc"></i>upcoming (hollow)</span>
    <span><i class="sw est"></i>estimated</span>
    <span><i class="sw today"></i>today</span>
  </div>
  <div class="grid" id="grid"></div>

  <div id="empty" class="empty" style="display:none">
    <h2>Nothing on the radar yet</h2>
    <p>Open an Amazon page for any book in a series (with the Release Radar extension installed) and it'll be collected automatically. Or hit <b>Crawl all</b> once you've opened a few.</p>
  </div>

  <div class="activity" id="activity"></div>
</div>

<!-- modal -->
<div class="scrim" id="scrim">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="mTitle">
    <h2 id="mTitle">Add series</h2>
    <input type="hidden" id="f_id">

    <label>Amazon link</label>
    <div class="linkrow">
      <input id="f_url" placeholder="https://www.amazon.com.au/dp/..." >
      <button class="small" id="autofillBtn" type="button">Auto-fill</button>
    </div>
    <div class="hint">Auto-fill reads the page itself. Anything it misses, type by hand.</div>

    <div class="two">
      <div><label>Series / book name</label><input id="f_name" placeholder="The Stormlight Archive"></div>
      <div><label>Author</label><input id="f_author" placeholder="Brandon Sanderson"></div>
    </div>

    <label>Latest novel title</label>
    <input id="f_novel_title" placeholder="Wind and Truth">

    <div class="two">
      <div><label>Novel release date</label><input id="f_novel_date" placeholder="2024-12-06" type="text"></div>
      <div><label>Audiobook status</label>
        <select id="f_status">
          <option value="unknown">Unknown</option>
          <option value="none">No audiobook yet</option>
          <option value="announced">Announced / preorder</option>
          <option value="released">Released</option>
        </select>
      </div>
    </div>

    <label>Audiobook date (if announced or released)</label>
    <input id="f_audio_date" placeholder="2024-12-06" type="text">

    <label>Release timeline — every book's dates</label>
    <div id="histRows"></div>
    <button class="small ghost" type="button" id="addHistBtn">+ Add a release</button>
    <div class="hint">One row per book. <b>#</b> is the book's number in the series — it's how new info is matched to the right book, so fixes stick while new releases still fill in. Leave the audiobook blank if there isn't one yet. Rows with both dates also feed the gap average.</div>

    <label>Notes</label>
    <textarea id="f_notes" placeholder="Anything you want to remember"></textarea>

    <div class="modal-actions">
      <button class="del-link" id="deleteBtn" type="button" style="visibility:hidden">Delete</button>
      <div style="display:flex;gap:10px">
        <button class="ghost" type="button" onclick="closeModal()">Cancel</button>
        <button class="btn-primary" id="saveBtn" type="button">Save</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<!-- settings modal -->
<div class="scrim" id="settingsScrim">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="setTitle">
    <h2 id="setTitle">Settings</h2>

    <label>Discord notifications</label>
    <div class="hint" style="margin:0 0 6px">Paste a channel <b>webhook URL</b> (in Discord: channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL). Messages post into that channel.</div>
    <input id="discWebhook" type="text" placeholder="https://discord.com/api/webhooks/…" autocomplete="off" spellcheck="false">
    <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
      <button class="small ghost" type="button" id="discTestBtn" style="width:auto">Send test</button>
      <span id="discTestMsg" class="hint" style="margin:0"></span>
    </div>
    <label style="text-transform:none;letter-spacing:0;color:var(--ink);display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px">
      <input type="checkbox" id="discOnNew" style="width:auto;margin:0"> Ping when a new or changed release date arrives
    </label>
    <label style="text-transform:none;letter-spacing:0;color:var(--ink);display:flex;align-items:center;gap:8px;margin:8px 0 0;font-size:13px">
      <input type="checkbox" id="discOnRelease" style="width:auto;margin:0"> Ping when a release goes live (the store page flips from pre-order to buyable)
    </label>
    <label style="text-transform:none;letter-spacing:0;color:var(--ink);display:flex;align-items:center;gap:8px;margin:8px 0 0;font-size:13px;flex-wrap:wrap">
      <input type="checkbox" id="discDaily" style="width:auto;margin:0"> Forecast digest
      <select id="discDigestDays" style="width:auto;margin:0;padding:4px 6px">
        <option value="1">every day</option><option value="2">every 2 days</option>
        <option value="3">every 3 days</option><option value="7">weekly</option>
      </select> at
      <input type="number" id="discDailyHour" min="0" max="23" style="width:54px;margin:0;padding:4px 6px"> <span style="color:var(--faint)">:00 — upcoming novels &amp; audiobooks, estimates included. Delivered by a very reluctant, very bratty assistant. Her lines live in <b>digest_lines</b> next to your data.json — edit or replace them freely.</span>
    </label>
    <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
      <button class="small ghost" type="button" id="digNowBtn" style="width:auto">Send one now</button>
      <span id="digNowMsg" class="hint" style="margin:0"></span>
    </div>
    <div style="border-bottom:1px solid var(--line);margin:16px 0"></div>

    <label style="color:#ff9b9b">Danger zone</label>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="small ghost" type="button" id="clearAllBtn" style="border-color:#5a3157;color:#ffb0b0">Clear all tracked series</button>
      <span class="hint" style="margin:0">Wipes every series so you can rebuild from Amazon only.</span>
    </div>
    <div class="hint">Your <code>data.json</code> is backed up first (recoverable by renaming <code>data.json.bak</code>), and settings are kept. After clearing, open one Amazon page per series (or hit "Crawl all now" in the extension) and the crawl repopulates everything.</div>

    <div class="modal-actions">
      <button class="del-link" style="visibility:hidden">.</button>
      <div style="display:flex;gap:10px">
        <button class="btn-primary" type="button" onclick="closeSettings()">Close</button>
      </div>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const api=(u,o)=>fetch(u,o).then(r=>r.json());
let STATE={series:[]};

function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600);}
function fmtDate(s){if(!s)return '--';const d=new Date(s+'T00:00:00');if(isNaN(d))return s;
  return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}
function isReleased(s){if(!s)return null;const d=new Date(s+'T00:00:00');if(isNaN(d))return null;const t=new Date();t.setHours(0,0,0,0);return d<t;}
function isTodayStr(sd){if(!sd)return false;const d=new Date(sd+'T00:00:00');if(isNaN(d))return false;const t=new Date();t.setHours(0,0,0,0);return d.getTime()===t.getTime();}
function storeLink(s,n,kind){                     // page URL for book n's edition, or null
  if(n==null) return null;
  const host=s.store_host; if(!host) return (s.url||null);
  const key=String(n);
  const asin=(kind==='audio'?(s.asins_audio||{})[key]:(s.asins_novel||{})[key])||(s.asins||{})[key];
  return asin?('https://'+host+'/dp/'+asin):(s.url||null);
}
function authorLink(s){
  if(!s.store_host||!s.author) return null;
  return 'https://'+s.store_host+'/s?k='+encodeURIComponent(s.author);
}
function wrapLink(html,url,title){ return url?`<a class="plain" href="${esc(url)}" target="_blank" rel="noopener" ${title?`title="${esc(title)}"`:''}>${html}</a>`:html; }
function latestEntry(s){let best=null,bn=-1;(s.timeline||[]).forEach(e=>{const n=(e&&e.n!=null)?+e.n:NaN;if(!isNaN(n)&&n>bn){bn=n;best=e;}});return best;}
function daysLate(iso){      // whole days since a date that has already passed
  const dt=new Date(iso+'T00:00:00');if(isNaN(dt))return 0;
  const t=new Date();t.setHours(0,0,0,0);
  return Math.max(0,Math.round((t-dt)/86400000));
}
function bkTag(n){ return n!=null?`<span class="bk">Book ${n}</span> `:''; }
function audioFact(s){
  // The Audiobook column tracks whatever we actually know, in order of strength:
  // the tracked book's own date, then a confirmed pre-order for an earlier book
  // (the series-level field only ever mirrors the LATEST book, which is why this
  // read blank), then how far past due an overdue one is, as a red count. Each
  // names its book, because it often isn't the one on the Novel line.
  if(s.audiobook_date)
    return bkTag(s.novel_n)+fmtDate(s.audiobook_date)+dateTag(s.audiobook_date,(latestEntry(s)||{}).audio_pre===false);
  const ra=nextRealAudio(s);
  if(ra){
    const val=bkTag(ra.n)+fmtDate(ra.date)+dateTag(ra.date,ra.live);
    const t=ra.n!=null?`Book ${ra.n}'s audiobook`:'The next audiobook';
    return wrapLink(val,storeLink(s,ra.n,'audio'),t+' — open on Amazon');
  }
  if(s.predicted_date&&isReleased(s.predicted_date)===true){
    const n=daysLate(s.predicted_date);
    const who=s.predicted_book_n!=null?`Book ${s.predicted_book_n}'s audiobook is `:'Overdue by ';
    return bkTag(s.predicted_book_n)
      +`<span style="color:var(--rose)" title="${attrEsc(who+n+' day'+(n===1?'':'s')+' past its expected window (expected ~ '+fmtDate(s.predicted_date)+'). Later books cannot be predicted until it lands.')}">−${n}d</span>`;
  }
  return '—';
}
function nextRealAudio(s){   // soonest still-upcoming REAL audiobook date in the timeline, or null
  let best=null;
  (s.timeline||[]).forEach(e=>{
    if(!e||!e.audiobook||isReleased(e.audiobook)!==false) return;
    if(!best||e.audiobook<best.date) best={date:e.audiobook,n:e.n!=null?+e.n:null,live:e.audio_pre===false};
  });
  return best;
}
function dateTag(d,live){const past=isReleased(d);if(past===null)return '';
  const out=past||(live===true&&isTodayStr(d));   // confirmed buyable on release day counts as out
  const label=past?'out':(out?'out now':'pre-order');
  const css=out?'color:#56d18a;border-color:#2c5a3f;background:#16271d':'color:#e7b14c;border-color:#5a4a25;background:#262017';
  return `<span style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid;border-radius:6px;margin-left:6px;vertical-align:middle;${css}">${label}</span>`;}
function confChip(c){
  if(!c) return '';
  const M={high:['high','#56d18a','#16271d','#2c5a3f'],medium:['med','#f0a500','#262017','#5a4a25'],low:['low','#b9a6d6','#221d3a','#3a3360']};
  const m=M[c.level]||M.low;
  const pm=(c.spread_days!=null)?` ±${c.spread_days}d`:'';
  const drop=c.outliers?`, ignoring ${c.outliers} freak gap${c.outliers===1?'':'s'}`:'';
  const basis=(c.spread_days!=null)
    ? `${c.level} confidence — based on ${c.n} past gap${c.n===1?'':'s'} that varied by about ±${c.spread_days} days${drop}`
    : `${c.level} confidence — only ${c.n} past release to go on, so the spread is unknown${drop}`;
  return ` <span title="${esc(basis)}" style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid ${m[3]};background:${m[2]};color:${m[1]};border-radius:6px;vertical-align:middle">${m[0]}${pm}</span>`;}
function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML;}
function attrEsc(s){return (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');}

/* ---- timeline hover card: cover art + details for a release dot ---- */
let HOVERCARD=null;
function hideHover(){if(HOVERCARD)HOVERCARD.style.display='none';}
function showHover(mark){
  let tip;try{tip=JSON.parse(mark.dataset.tip||'null');}catch(_){tip=null;}
  if(!tip)return;
  if(!HOVERCARD){HOVERCARD=document.createElement('div');HOVERCARD.className='hovercard';document.body.appendChild(HOVERCARD);}
  const name=tip.est?('Estimated audiobook'+(tip.estN!=null?` · book ${tip.estN}`:''))
    :(tip.t||((tip.sn||'')+(tip.nN!=null?' '+tip.nN:(tip.aN!=null?' '+tip.aN:''))));
  // a broken per-book cover URL degrades to the series cover, then to no image
  const img=(tip.cover&&!tip.est)?`<img src="${attrEsc(tip.cover)}" alt="" data-fb="${attrEsc(tip.cover2||'')}" onerror="if(this.dataset.fb){this.src=this.dataset.fb;this.dataset.fb='';}else{this.remove();}">`:'';
  const narr=tip.narr?`<div class="hc-n">Narrated by ${esc(tip.narr)}</div>`:'';
  const hint=(tip.est&&tip.estWhy)?`<div class="hc-n">${esc(tip.estWhy)}</div>`:'';
  HOVERCARD.innerHTML=`${img}<div><div class="hc-t">${esc(name)}</div><div class="hc-d">${esc(tip.label)}</div>${narr}${hint}</div>`;
  HOVERCARD.style.display='flex';HOVERCARD.style.visibility='hidden';HOVERCARD.style.left='0px';HOVERCARD.style.top='0px';
  const r=mark.getBoundingClientRect(),hw=HOVERCARD.offsetWidth,hh=HOVERCARD.offsetHeight;
  let x=Math.min(Math.max(8,r.left+r.width/2-hw/2),window.innerWidth-hw-8);
  let y=r.top-hh-10; if(y<8)y=r.bottom+10;
  HOVERCARD.style.left=x+'px';HOVERCARD.style.top=y+'px';HOVERCARD.style.visibility='visible';
}
document.addEventListener('mouseover',e=>{const m=e.target.closest&&e.target.closest('.mark[data-tip]');if(m)showHover(m);});
document.addEventListener('mouseout',e=>{const m=e.target.closest&&e.target.closest('.mark[data-tip]');if(m)hideHover();});
window.addEventListener('scroll',hideHover,true);

let LAST_CHANGES=null;   // map id -> last_change, to spot new updates
function checkForNotifications(){
  const cur={};STATE.series.forEach(s=>cur[s.id]=s.last_change||'');
  if(LAST_CHANGES!==null && 'Notification' in window && Notification.permission==='granted'){
    STATE.series.forEach(s=>{
      const prev=LAST_CHANGES[s.id];
      if(s.last_change && prev!==undefined && s.last_change!==prev){
        try{new Notification('Release Radar — '+s.name,{body:s.last_change});}catch(_){}
      }
    });
  }
  LAST_CHANGES=cur;
}

async function refresh(){
  STATE=await api('/api/state');
  const lc=$('#lastCheck');
  if(lc) lc.textContent = STATE.last_ingest
      ? ('Last updated '+timeAgo(STATE.last_ingest)+' · via the browser extension')
      : 'No data yet · open a series page in your browser (with the extension installed) to collect it';
  const c=STATE.counts;
  $('#countLine').textContent=`${c.total} tracked · ${c.out} out · ${c.announced} announced · ${c.waiting} waiting`;
  renderHero();renderOverdue();renderGrid();renderActivity();
  checkForNotifications();
}

function renderOverdue(){
  const list=STATE.overdue||[];
  const wrap=$('#overdueWrap'),box=$('#overdue');
  if(!wrap||!box) return;
  if(!list.length){wrap.style.display='none';box.innerHTML='';return;}
  wrap.style.display='block';
  const cnt=$('#overdueCount'); if(cnt) cnt.textContent='('+list.length+')';
  box.innerHTML=list.map(o=>{
    // count the days here rather than trusting the payload, so the number stays
    // right on a dashboard left open across midnight
    const exp=new Date(o.expected+'T00:00:00');
    const t=new Date();t.setHours(0,0,0,0);
    const days=isNaN(exp)?(o.days_late||0):Math.max(0,Math.round((t-exp)/86400000));
    const cov=o.cover?`<img class="cov" src="${attrEsc(o.cover)}" alt="" onerror="this.remove()">`:'';
    const bk=o.book_n!=null?` · book ${o.book_n}`:'';
    const s=STATE.series.find(x=>x.id===o.series_id)||{};
    const title=wrapLink(esc(o.title),storeLink(s,o.book_n,'novel'),'Open on Amazon');
    return `<div class="od"><div class="late">${days}<small>${days===1?'day':'days'} late</small></div>${cov}
      <div class="info"><div class="t">${title}</div>
      <div class="d">expected ~ ${esc(fmtDate(o.expected))}${bk} · ${esc(o.series)}</div></div></div>`;
  }).join('');
}

function renderHero(){
  const up=STATE.upcoming||[];
  const wrap=$('#heroWrap');const hero=$('#hero');
  const hasSeries=(STATE.series||[]).length>0;
  if(!hasSeries){wrap.style.display='none';return;}
  wrap.style.display='block';
  const mode=STATE.upcoming_mode||'audiobook';
  const isNovel=mode==='novel';
  const ht=$('#heroTitle'); if(ht) ht.textContent=isNovel?'Next novel releases':'Next audiobook releases';
  const mt=$('#modeToggle'); if(mt) mt.textContent=isNovel?'⇄ Show audiobooks':'⇄ Show novels';
  const estOn=isNovel?!!STATE.show_estimates_novel:!!STATE.show_estimates;
  const tb=$('#estToggle');
  if(tb){
    tb.textContent=estOn?'Estimates: on':'Estimates: off';
    tb.style.color=estOn?'var(--amber)':'var(--muted)';
    tb.style.borderColor=estOn?'#5a4a25':'var(--line)';
    tb.title=isNovel
      ? "Include the next book in each series, estimated from how far apart past novels released"
      : "Include likely audiobooks not yet announced, estimated from each series' past novel-to-audio timing";
  }
  hero.innerHTML='';
  if(!up.length){
    const kindWord=isNovel?'novels':'audiobooks';
    hero.innerHTML='<div style="color:var(--faint);padding:14px 2px;font-size:13px">No upcoming '+kindWord+(estOn?'':' — turn on estimates to include predicted releases')+'.</div>';
    return;}
  up.forEach((u,i)=>{
    const el=document.createElement('div');el.className='up'+(i===0?' soonest':'');
    const note=(u.estimate&&u.note)?` <span class="hint" style="margin:0">· ${esc(u.note)}</span>`:'';
    const conf=u.estimate?confChip(u.confidence):'';
    const kindCls=u.kind==='novel'?'novel':'audio';
    const kindLbl=u.kind==='novel'?'Novel':'Audiobook';
    el.innerHTML=`<div><div class="count">${u.countdown_days}<small>${u.countdown_days===1?'day':'days'}</small></div></div>
      <div><div class="t">${esc(u.title)}</div>
      <div class="d">${fmtDate(u.date)} · <span class="kind ${kindCls} ${u.estimate?'est':''}">${kindLbl}${u.estimate?' · est':''}</span>${conf}${note}</div></div>`;
    hero.appendChild(el);
  });
}

function timelineHtml(s){
  // one entry per unique date, tracking the book number(s) on that date plus
  // whatever the entry knows for the hover card (title, cover, narrator)
  const ev={};
  const add=(d,k,num,meta)=>{if(!d)return;const e=(ev[d]=ev[d]||{});e[k]=true;
    if(num!=null){if(k==='novel')e.nN=num;else if(k==='audio')e.aN=num;}
    if(meta){if(meta.title&&!e.title)e.title=meta.title;if(meta.cover&&!e.cover)e.cover=meta.cover;
      if(k==='audio'&&meta.narrator)e.narr=meta.narrator;}};
  const tl=(s.timeline&&s.timeline.length)?s.timeline:null;
  const live={};                                                 // date -> {n:true,a:true} = confirmed buyable
  const mklive=(d,k)=>{if(d)(live[d]=live[d]||{})[k]=true;};
  if(tl){tl.forEach(t=>{add(t.novel,'novel',t.n,t);add(t.audiobook,'audio',t.n,t);
    if(t.novel_pre===false)mklive(t.novel,'n');if(t.audio_pre===false)mklive(t.audiobook,'a');});}
  else{(s.history||[]).forEach(h=>{add(h.novel,'novel',h.n);add(h.audiobook,'audio',h.n);});}
  add(s.novel_date,'novel',s.novel_n);                          // always include the tracked book
  if(s.audiobook_status!=='none') add(s.audiobook_date,'audio',s.novel_n);
  // dashed ring only for a still-future estimate; a lapsed one is reported as
  // "overdue" in the predict line, not drawn as if it were ahead of us
  if((s.audiobook_status==='none'||s.audiobook_status==='unknown')&&s.predicted_date&&isReleased(s.predicted_date)===false) add(s.predicted_date,'est',null);

  const dates=Object.keys(ev).filter(d=>!isNaN(+new Date(d+'T00:00:00')));
  if(!dates.length) return '';
  const times=dates.map(d=>+new Date(d+'T00:00:00'));
  const min=Math.min(...times),max=Math.max(...times),span=(max-min)||1;
  const isLive=(d,e)=>{const lv=live[d]||{};return (!e.novel||lv.n)&&(!e.audio||lv.a);};
  const rel=(d,e)=>{const r=isReleased(d);return r===null?'':(r?' (out)':(isTodayStr(d)&&isLive(d,e)?' (out now)':' (pre-order)'));};
  const marks=dates.map(d=>{
    const t=+new Date(d+'T00:00:00');
    const left=dates.length===1?50:((t-min)/span*100);
    const e=ev[d];let cls,title;
    const nn=e.nN!=null?(' #'+e.nN):'', an=e.aN!=null?(' #'+e.aN):'';
    if(e.novel&&e.audio){cls='both';title=`Novel${nn} + Audiobook${an} · ${fmtDate(d)}${rel(d,e)}`;}
    else if(e.novel){cls='novel';title=`Novel${nn} · ${fmtDate(d)}${rel(d,e)}`;}
    else if(e.audio){cls='audio';title=`Audiobook${an} · ${fmtDate(d)}${rel(d,e)}`;}
    else{cls='est';title=`Estimated audiobook · likely ~${fmtDate(d)}`;}
    if(cls!=='est'&&isReleased(d)===false&&!(isTodayStr(d)&&isLive(d,e))) cls+=' future';   // hollow = still upcoming; confirmed-live today renders solid
    const mUrl=e.novel?storeLink(s,e.nN,'novel'):(e.audio?storeLink(s,e.aN,'audio'):null);
    const isEst=cls==='est';
    const tip={sn:s.name||'',label:title,t:e.title||null,cover:e.cover||s.cover_url||null,
      cover2:(e.cover&&s.cover_url&&e.cover!==s.cover_url)?s.cover_url:null,
      narr:e.narr||null,nN:e.nN??null,aN:e.aN??null,est:isEst,
      estN:isEst?(s.predicted_book_n??null):null,
      estWhy:isEst?(s.predicted_queued
        ? (s.predicted_after_n!=null?`queued behind book ${s.predicted_after_n}'s audiobook, at this series' usual pace`:'next in this series’ audio queue')
        : 'projected from this series’ past novel→audio gaps'):null};
    const attrs=`class="mark ${cls}" style="left:${left.toFixed(1)}%" aria-label="${esc(title)}" data-tip="${attrEsc(JSON.stringify(tip))}"`;
    return mUrl?`<a ${attrs} href="${esc(mUrl)}" target="_blank" rel="noopener"></a>`:`<span ${attrs}></span>`;
  }).join('');
  // "today" line: only when it falls within the span, so past sits left / upcoming right
  const now=Date.now();let today='';
  if(dates.length>1&&now>=min&&now<=max){const tl=((now-min)/span*100);today=`<span class="tl-today" style="left:${tl.toFixed(1)}%" title="today"></span>`;}
  const lbl=s.avg_gap_label?`avg gap ${s.avg_gap_label}`:(dates.length>1?'releases':'release');
  // pin the latest (rightmost) date under its mark, always visible
  let endLbl='';
  {
    const lastD=dates.find(d=>(+new Date(d+'T00:00:00'))===max);
    if(lastD){
      const e=ev[lastD]||{};
      const isEst=!e.novel&&!e.audio;
      const left=dates.length===1?50:100;
      endLbl=`<span class="tl-end" style="left:${left}%;transform:translateX(-50%)">${isEst?'~':''}${esc(fmtDate(lastD))}</span>`;
    }
  }
  return `<div class="timeline"><div class="tl-row">
      <span class="gaplabel">${lbl}</span>
      <span class="track">${today}${marks}${endLbl}</span>
    </div></div>`;
}

function timeAgo(iso){
  if(!iso) return 'not yet';
  const t=new Date(iso); if(isNaN(t)) return '';
  const s=Math.floor((Date.now()-t)/1000);
  if(s<60) return 'just now';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function initials(name){return (name||'?').replace(/^(the|a|an)\s+/i,'').trim().charAt(0).toUpperCase()||'?';}

function sortedSeries(){
  const arr=STATE.series.slice();
  const mode=$('#sortSel')?$('#sortSel').value:'manual';
  if(mode==='title') arr.sort((a,b)=>(a.name||'').localeCompare(b.name||''));
  else if(mode==='next') arr.sort((a,b)=>{
    const x=a.next_date||'9999',y=b.next_date||'9999';return x<y?-1:x>y?1:0;});
  else if(mode==='status'){const rank={announced:0,none:1,unknown:1,released:2};
    arr.sort((a,b)=>(rank[a.status_class||a.audiobook_status]??1)-(rank[b.status_class||b.audiobook_status]??1));}
  return arr; // 'manual' keeps stored order
}

function renderGrid(){
  const grid=$('#grid');const empty=$('#empty');
  const has=STATE.series.length>0;
  $('#seriesEyebrow').style.display=has?'block':'none';
  $('#sortbar').style.display=has?'flex':'none';
  const lg=$('#legend'); if(lg) lg.style.display=has?'flex':'none';
  if(!has){empty.style.display='block';grid.innerHTML='';return;}
  empty.style.display='none';grid.innerHTML='';
  const manual=!$('#sortSel')||$('#sortSel').value==='manual';
  sortedSeries().forEach(s=>{
    const card=document.createElement('div');card.className='card';card.dataset.id=s.id;
    if(manual) card.setAttribute('draggable','true');
    const st=s.audiobook_status;
    const mode=STATE.upcoming_mode||'audiobook';
    let predict='';
    if(mode==='novel'){
      if(STATE.show_estimates_novel && s.predicted_novel_date){
        predict=`<div class="predict">Likely next novel <span class="mono">~ ${fmtDate(s.predicted_novel_date)}</span> · ${s.novel_history_count} past gap${s.novel_history_count===1?'':'s'} (${s.avg_novel_gap_label})${confChip(s.predicted_novel_confidence)}</div>`;
      }
    }else{
      if(st==='none'||st==='unknown'){
        // Confirmed dates and overdue counts both live in the Audiobook column
        // now, so this line carries only a genuine forward-looking forecast. A
        // lapsed projection says nothing here — it's the column's red count and
        // the Overdue panel that report it.
        if(s.predicted_date&&isReleased(s.predicted_date)===false){
          const bk=s.predicted_book_n!=null?` book ${s.predicted_book_n}`:'';
          // say what the guess is built on: the audio queue when a confirmed date
          // ahead of it sets the pace, the novel→audio gap history otherwise
          const basis=s.predicted_queued
            ? (s.predicted_after_n!=null?`queued after book ${s.predicted_after_n}'s audio`:'next in the audio queue')
            : `${s.history_count} past gap${s.history_count===1?'':'s'} (${s.avg_gap_label})`;
          predict=`<div class="predict">Likely audiobook${bk} <span class="mono">~ ${fmtDate(s.predicted_date)}</span> · ${basis}${confChip(s.predicted_confidence)}</div>`;
        }
      }
    }
    const err=s.fetch_error?`<div class="err">⚠ ${esc(s.fetch_error)}</div>`:'';
    const chgTime=s.last_change_at?` <span class="chg-time" title="${esc(new Date(s.last_change_at).toLocaleString())}">· ${timeAgo(s.last_change_at)}</span>`:'';
    const chg=s.last_change?`<div class="changed">● ${esc(s.last_change)}${chgTime}</div>`:'';
    const cover=s.cover_url
      ? `<img class="cover" src="${esc(s.cover_url)}" alt="" onerror="this.outerHTML='<div class=&quot;cover ph&quot;>${esc(initials(s.name))}</div>'">`
      : `<div class="cover ph">${esc(initials(s.name))}</div>`;
    const bookUrl=storeLink(s,(latestEntry(s)||{}).n!=null?(latestEntry(s)||{}).n:s.novel_n,'novel');
    card.innerHTML=`
      ${manual?'<div class="grip" title="Drag to reorder">⋮⋮</div>':''}
      ${wrapLink(cover,bookUrl,'Open on Amazon')}
      <div class="cmain">
        ${wrapLink(`<h3>${esc(s.name)}</h3>`,bookUrl,'Open the latest book on Amazon')}
        <div class="byline">${s.author?wrapLink(esc(s.author),authorLink(s),'Search this author on Amazon'):'<span style="color:var(--faint)">unknown author</span>'}${s.novel_title?' · '+wrapLink(esc(s.novel_title),bookUrl):''}</div>
        <div class="facts">
          <div class="fact"><div class="k">Novel</div><div class="v">${bkTag(s.novel_n)}${fmtDate(s.novel_date)}${dateTag(s.novel_date,(latestEntry(s)||{}).novel_pre===false)}</div></div>
          <div class="fact"><div class="k">Audiobook</div><div class="v">${audioFact(s)}</div></div>
          <div class="fact"><div class="k">Avg gap</div><div class="v">${s.avg_gap_label||'—'}</div></div>
          ${s.narrator?`<div class="fact"><div class="k">Narrator</div><div class="v">${esc(s.narrator)}</div></div>`:''}
        </div>
        <span class="pill ${s.status_class||st}">${esc(s.status_label)}</span>
        ${predict}${chg}${err}
        ${timelineHtml(s)}
      </div>
      <div class="right">
        <div class="rowbtns">
          <button class="small" data-check="${s.id}" title="Ask the extension to re-check just this series for new or newly-released books. Needs the extension running with Firefox open; takes a few seconds.">Check</button>
          <button class="small" data-edit="${s.id}">Edit</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
  wireDrag();
  grid.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openModal(b.dataset.edit));
  grid.querySelectorAll('[data-check]').forEach(b=>b.onclick=()=>{
    const nm=(STATE.series.find(x=>x.id===b.dataset.check)||{}).name||'this series';
    askExtension('/api/ext/crawl-series','Checking \u201c'+nm+'\u201d',{id:b.dataset.check});
  });
}

/* ---- drag to reorder ---- */
let DRAG_ID=null;
function wireDrag(){
  const grid=$('#grid');
  grid.querySelectorAll('.card[draggable="true"]').forEach(card=>{
    card.ondragstart=e=>{
      // don't start a drag from a button
      if(e.target.closest('button,a,select,input')){e.preventDefault();return;}
      DRAG_ID=card.dataset.id;card.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
      try{e.dataTransfer.setData('text/plain',DRAG_ID);}catch(_){}
    };
    card.ondragend=()=>{card.classList.remove('dragging');grid.querySelectorAll('.card').forEach(c=>c.classList.remove('dragover'));};
    card.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='move';
      grid.querySelectorAll('.card').forEach(c=>c.classList.remove('dragover'));card.classList.add('dragover');};
    card.ondrop=e=>{
      e.preventDefault();card.classList.remove('dragover');
      if(!DRAG_ID||DRAG_ID===card.dataset.id)return;
      const src=grid.querySelector('.card[data-id="'+DRAG_ID+'"]');
      if(!src)return;
      const rect=card.getBoundingClientRect();
      const after=e.clientY>rect.top+rect.height/2;
      grid.insertBefore(src, after?card.nextSibling:card);
      persistOrder();
    };
  });
}
async function persistOrder(){
  const ids=[...$('#grid').querySelectorAll('.card')].map(c=>c.dataset.id);
  // reorder STATE to match so re-renders keep the new order
  STATE.series.sort((a,b)=>ids.indexOf(a.id)-ids.indexOf(b.id));
  try{await api('/api/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});}catch(_){}
}

function renderActivity(){
  const a=$('#activity');const rows=(STATE.log||[]).slice(0,6);
  a.innerHTML=rows.map(r=>`<div>${new Date(r.time).toLocaleString()} — ${esc(r.msg)}</div>`).join('');
}

/* ---- modal ---- */
function histRow(num='',nov='',aud=''){
  const div=document.createElement('div');div.className='hist-row';
  div.innerHTML=`<input class="bnum" placeholder="#" value="${esc(num)}" title="Book number" style="max-width:46px;text-align:center">
    <input placeholder="novel date" value="${esc(nov)}"><input placeholder="audiobook date" value="${esc(aud)}">
    <button class="small ghost" type="button">✕</button>`;
  div.querySelector('button').onclick=()=>div.remove();
  return div;
}
function openModal(id){
  $('#histRows').innerHTML='';
  if(id){
    const s=STATE.series.find(x=>x.id===id);
    $('#mTitle').textContent='Edit series';
    $('#f_id').value=s.id;$('#f_url').value=s.url||'';$('#f_name').value=s.name||'';
    $('#f_author').value=s.author||'';$('#f_novel_title').value=s.novel_title||'';
    $('#f_novel_date').value=s.novel_date||'';$('#f_status').value=s.audiobook_status||'unknown';
    $('#f_audio_date').value=s.audiobook_date||'';$('#f_notes').value=s.notes||'';
    const rows=(s.timeline&&s.timeline.length)?s.timeline:(s.history||[]);
    rows.forEach(h=>$('#histRows').appendChild(histRow(h.n??'',h.novel||'',h.audiobook||'')));
    $('#deleteBtn').style.visibility='visible';
  }else{
    $('#mTitle').textContent='Add series';
    ['f_id','f_url','f_name','f_author','f_novel_title','f_novel_date','f_audio_date','f_notes'].forEach(k=>$('#'+k).value='');
    $('#f_status').value='unknown';
    $('#deleteBtn').style.visibility='hidden';
  }
  $('#scrim').classList.add('open');
}
function closeModal(){$('#scrim').classList.remove('open');}
$('#addHistBtn').onclick=()=>$('#histRows').appendChild(histRow());

$('#autofillBtn').onclick=async()=>{
  const url=$('#f_url').value.trim();if(!url){toast('Paste a link first');return;}
  $('#autofillBtn').textContent='…';
  const r=await api('/api/fetch-url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
  $('#autofillBtn').textContent='Auto-fill';
  if(r.error){toast(r.error);return;}
  if(!$('#f_name').value)$('#f_name').value=r.series_guess||r.title||'';
  if(r.title&&!$('#f_novel_title').value)$('#f_novel_title').value=r.title;
  if(r.author&&!$('#f_author').value)$('#f_author').value=r.author;
  if(r.date&&!$('#f_novel_date').value)$('#f_novel_date').value=r.date;
  if(r.audiobook_date&&!$('#f_audio_date').value){
    $('#f_audio_date').value=r.audiobook_date;
    const ad=new Date(r.audiobook_date+'T00:00:00');
    $('#f_status').value=(!isNaN(ad)&&ad<=new Date())?'released':'announced';
  } else if(r.is_audiobook&&r.date&&!$('#f_audio_date').value){
    $('#f_audio_date').value=r.date;$('#f_status').value='released';
  }
  const src=(r.sources||[]).filter(s=>s!=='page');
  toast(src.length?('Filled from '+src.join(' + ')):'Filled what I could find');
};

$('#saveBtn').onclick=async()=>{
  const timeline=[...document.querySelectorAll('.hist-row')].map(r=>{
    const i=r.querySelectorAll('input');
    const num=i[0].value.trim();
    return {n:num===''?null:parseInt(num,10),novel:i[1].value.trim(),audiobook:i[2].value.trim()};
  }).filter(h=>h.novel||h.audiobook||h.n!=null);
  const body={
    name:$('#f_name').value.trim(),author:$('#f_author').value.trim(),url:$('#f_url').value.trim(),
    novel_title:$('#f_novel_title').value.trim(),novel_date:$('#f_novel_date').value.trim(),
    audiobook_date:$('#f_audio_date').value.trim(),audiobook_status:$('#f_status').value,
    notes:$('#f_notes').value.trim(),timeline
  };
  if(!body.name){toast('A name is required');return;}
  const id=$('#f_id').value;
  const res=await api(id?('/api/series/'+id):'/api/series',
    {method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(res.error){toast(res.error);return;}
  closeModal();toast(id?'Saved':'Added');refresh();
};
$('#deleteBtn').onclick=async()=>{
  const id=$('#f_id').value;if(!id)return;
  if(!confirm('Remove this series from the radar?'))return;
  await api('/api/series/'+id,{method:'DELETE'});closeModal();toast('Removed');refresh();
};
$('#scrim').onclick=e=>{if(e.target===$('#scrim'))closeModal();};

/* ---- settings ---- */
async function loadDiscordSettings(){
  try{
    const s=await api('/api/settings');
    $('#discWebhook').value=s.discord_webhook||'';
    $('#discOnNew').checked=!!s.discord_on_new;
    $('#discOnRelease').checked=!!s.discord_on_release;
    $('#discDaily').checked=!!s.discord_daily;
    $('#discDailyHour').value=(s.discord_daily_hour!=null?s.discord_daily_hour:9);
    $('#discDigestDays').value=String(s.discord_digest_days||1);
  }catch(_){}
}
async function saveDiscord(){
  const body={
    discord_webhook:$('#discWebhook').value.trim(),
    discord_on_new:$('#discOnNew').checked,
    discord_on_release:$('#discOnRelease').checked,
    discord_daily:$('#discDaily').checked,
    discord_daily_hour:parseInt($('#discDailyHour').value,10),
    discord_digest_days:parseInt($('#discDigestDays').value,10)
  };
  try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(_){}
}
['#discWebhook','#discOnNew','#discOnRelease','#discDaily','#discDailyHour','#discDigestDays'].forEach(sel=>{
  const el=$(sel); if(el) el.addEventListener('change',saveDiscord);
});
const _discTest=$('#discTestBtn');
if(_discTest) _discTest.onclick=async()=>{
  const msg=$('#discTestMsg'); msg.textContent='Sending…'; msg.style.color='var(--faint)';
  await saveDiscord();
  try{
    const d=await api('/api/discord/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({discord_webhook:$('#discWebhook').value.trim()})});
    msg.textContent=d.message||(d.ok?'Sent!':'Failed'); msg.style.color=d.ok?'var(--green)':'var(--red)';
  }catch(e){ msg.textContent='Failed: '+e; msg.style.color='var(--red)'; }
};
const _digNow=$('#digNowBtn');
if(_digNow) _digNow.onclick=async()=>{
  const msg=$('#digNowMsg'); msg.textContent='Sending\u2026'; msg.style.color='var(--faint)';
  await saveDiscord();
  try{ const d=await api('/api/discord/digest-now',{method:'POST'});
       msg.textContent=d.message||(d.ok?'Sent!':'Failed'); msg.style.color=d.ok?'var(--green)':'var(--red)'; }
  catch(e){ msg.textContent='Failed: '+e; msg.style.color='var(--red)'; }
};
function openSettings(){ loadDiscordSettings(); $('#settingsScrim').classList.add('open'); }
function closeSettings(){$('#settingsScrim').classList.remove('open');}
$('#settingsBtn').onclick=openSettings;
$('#estToggle').onclick=async()=>{
  const mode=STATE.upcoming_mode||'audiobook';
  const body=(mode==='novel')
    ? {show_estimates_novel:!STATE.show_estimates_novel}
    : {show_estimates:!STATE.show_estimates};
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(typeof refresh==='function') refresh();
};
$('#modeToggle').onclick=async()=>{
  const next=((STATE.upcoming_mode||'audiobook')==='audiobook')?'novel':'audiobook';
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upcoming_mode:next})});
  if(typeof refresh==='function') refresh();
};
$('#clearAllBtn').onclick=async()=>{
  if(!confirm("Delete ALL tracked series from the app AND tell the extension to forget every page?\n\nUse this for a true reset. (To just rebuild the app's data from pages the extension already knows, use 'Crawl all now' in the extension instead.)\n\nYour data.json is backed up first; settings are kept.")) return;
  const r=await api('/api/series/clear',{method:'POST',body:JSON.stringify({forget:true})});
  closeSettings();
  toast((r&&r.ok)?('Cleared '+r.removed+' series \u2014 extension will forget its pages shortly'):'Could not clear');
  if(typeof refresh==='function') refresh(); else location.reload();
};

/* ---- quit ---- */
$('#quitBtn').onclick=async()=>{
  if(!confirm('Close Release Radar? It stops collecting until you reopen it.'))return;
  try{await api('/api/quit',{method:'POST'});}catch(e){}
  document.body.innerHTML='<div style="min-height:100vh;display:flex;align-items:center;justify-content:center;'+
    'font-family:var(--serif);font-size:22px;color:var(--muted);text-align:center;padding:40px">'+
    'Release Radar has stopped.<br><span style="font-size:15px">You can close this tab.</span></div>';
};

async function askExtension(path,label,body){
  try{ const opt=body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{method:'POST'};
       const r=await api(path,opt); if(!r||!r.ok){toast('Could not reach the app');return;} }
  catch(e){ toast('Could not reach the app'); return; }
  toast(label+' \u2014 the extension will start within ~20s. Make sure it\u2019s installed and Firefox is open.');
  // poll a little faster for a couple of minutes so results show as they land
  if(window.__extPoll) clearInterval(window.__extPoll);
  let n=0; window.__extPoll=setInterval(()=>{ refresh(); if(++n>40){clearInterval(window.__extPoll);window.__extPoll=null;} }, 3000);
}
$('#extRefreshBtn').onclick=()=>askExtension('/api/ext/refresh','Asked the extension to refresh dates');
$('#extCrawlBtn').onclick=()=>askExtension('/api/ext/crawl','Asked the extension to crawl all series');

$('#settingsScrim').onclick=e=>{if(e.target===$('#settingsScrim'))$('#settingsScrim').classList.remove('open');};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeSettings();}});

if($('#sortSel')) $('#sortSel').onchange=renderGrid;

refresh();
setInterval(refresh,60000);
</script>
</body>
</html>"""


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


def main():
    load_state()
    _ensure_line_files()
    _start_daily_digest()
    port = pick_port(int(os.environ.get("RADAR_PORT", DEFAULT_PORT)))
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
