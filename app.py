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
import sys
import re
import json
import time
import uuid
import html
import threading
import logging
import webbrowser
from datetime import datetime, date, timedelta
from email.utils import format_datetime
from urllib.parse import urlencode
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
            "settings": {"hardcover_token": "", "region": "us"}}


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
        s.setdefault("discord_on_release", True)      # ping on release day after the set hour
        s.setdefault("discord_release_hour", 15)      # 24h local hour (15 = 3pm)
        return dict(s)


def save_state():
    with _lock:
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
    out = []
    for pair in series.get("history", []):
        n = parse_date(pair.get("novel"))
        a = parse_date(pair.get("audiobook"))
        if n and a:
            out.append((a - n).days)
    return out


def avg_gap_days(series):
    gaps = gap_days_list(series)
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps))


def gap_label(days):
    if days is None:
        return None
    d = abs(days)
    if d < 14:
        return f"{days}d"
    if d < 90:
        return f"~{round(days / 7)} wks"
    return f"~{round(days / 30.4)} mo"


def predicted_audiobook_date(series):
    """Only meaningful when there is no announcement yet."""
    status = series.get("audiobook_status", "unknown")
    if status in ("released", "announced"):
        return None
    avg = avg_gap_days(series)
    nd = parse_date(series.get("novel_date"))
    if avg is None or nd is None:
        return None
    return nd + timedelta(days=avg)


def next_release(series):
    """The next dated event for this series: (date, kind, is_estimate) or None."""
    t = today()
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    nd = parse_date(series.get("novel_date"))

    if status in ("announced", "released") and ad and ad >= t:
        return ad, "audiobook", False
    if nd and nd >= t:
        return nd, "novel", False
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
    avg = avg_novel_gap_days(series)
    if avg is None:
        return None
    return nd + timedelta(days=avg)


def _estimate_confidence(gaps):
    """How much to trust a gap-based projection, from the number and consistency of
    the past gaps it averages. Returns {level, n, spread_days} or None.
    'spread_days' is the typical deviation of those gaps — roughly how far off the
    guess could be. More data + tighter spread -> higher confidence."""
    n = len(gaps)
    if n == 0:
        return None
    if n == 1:
        return {"level": "low", "n": 1, "spread_days": None}
    mean = sum(gaps) / n
    sd = (sum((g - mean) ** 2 for g in gaps) / n) ** 0.5
    sd_i = int(round(sd))
    if n >= 3 and sd <= 10:
        level = "high"      # several releases, all landing within ~10 days of the average
    elif sd <= 21:
        level = "medium"    # within ~3 weeks
    else:
        level = "low"       # scattered enough that the date is a rough guess
    return {"level": level, "n": n, "spread_days": sd_i}


# ---------------------------------------------------------------------------
# Scraping (best effort - hand-editable fallback always available)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# External data sources (cross-referenced)
#   - Apple iTunes Search API : keyless, audiobook release dates + covers
#   - Audnexus (audnex.us)    : keyless, Audible data by ASIN
#   - Hardcover GraphQL       : needs a free token, enumerates a whole series
# None of these raise; on any failure they return None / an {"error": ...}.
# ---------------------------------------------------------------------------


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# Hardcover blocks pattern-matching operators, so we use its Typesense-backed
# search to resolve a name to a series id, then fetch that id's books (the
# allowed _eq operator). Search is fuzzy and typo-tolerant.
HC_SEARCH_QUERY = """
query SearchSeries($q: String!) {
  search(query: $q, query_type: "Series", per_page: 8, page: 1) { ids results }
}
""".strip()

_HC_BOOK_FIELDS = """
      position
      book {
        id
        title
        release_date
        release_year
        default_audio_edition { release_date }
      }
"""

HC_BOOKS_QUERY = """
query SeriesBooks($id: Int!) {
  series(where: {id: {_eq: $id}}, limit: 1) {
    id
    name
    author { name }
    book_series(
      distinct_on: position
      order_by: [{position: asc}, {book: {users_count: desc}}]
      where: {book: {canonical_id: {_is_null: true}, is_partial_book: {_eq: false}}, compilation: {_eq: false}}
    ) {
__FIELDS__
    }
  }
}
""".replace("__FIELDS__", _HC_BOOK_FIELDS).strip()

# Looser fallback (no book-level filters) for sparsely-catalogued series.
HC_BOOKS_QUERY_LOOSE = """
query SeriesBooksLoose($id: Int!) {
  series(where: {id: {_eq: $id}}, limit: 1) {
    id
    name
    author { name }
    book_series(distinct_on: position, order_by: [{position: asc}, {book: {users_count: desc}}]) {
__FIELDS__
    }
  }
}
""".replace("__FIELDS__", _HC_BOOK_FIELDS).strip()


HC_BOOK_QUERY = """
query BookById($id: Int!) {
  books(where: {id: {_eq: $id}}, limit: 1) {
    id
    title
    release_date
    release_year
    default_audio_edition { release_date }
    contributions(limit: 1) { author { name } }
    book_series(limit: 5) { series { id name books_count author { name } } }
  }
}
""".strip()


def _entry_n(e):
    try:
        return int(e.get("n"))
    except (TypeError, ValueError):
        return None


def _history_from_timeline(tl):
    return [{"novel": e["novel"], "audiobook": e["audiobook"]}
            for e in tl if e.get("novel") and e.get("audiobook")]


# ---------------------------------------------------------------------------
# Per-series refresh (re-checks against the databases, no URL needed)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Daily check + scheduler
# ---------------------------------------------------------------------------

_tray_icon = None  # set when the system-tray icon is running


# ---------------------------------------------------------------------------
# Re-import all (re-pull every tracked series from Hardcover)
# ---------------------------------------------------------------------------

_reimport_lock = threading.Lock()


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
    audio_conf = _estimate_confidence(gap_days_list(s))
    novel_conf = _estimate_confidence(novel_gap_days_list(s))
    nxt = next_release(s)
    status = s.get("audiobook_status", "unknown")
    view = {
        "id": s["id"],
        "name": s.get("name", ""),
        "author": s.get("author", ""),
        "url": s.get("url", ""),
        "novel_title": s.get("novel_title", ""),
        "novel_date": s.get("novel_date"),
        "audiobook_date": s.get("audiobook_date"),
        "audiobook_status": status,
        "status_label": STATUS_LABELS.get(status, "Unknown"),
        "cover_url": s.get("cover_url", ""),
        "history": s.get("history", []),
        "timeline": s.get("timeline", []),
        "edited": bool(s.get("edited")),
        "linked": bool(s.get("hardcover_series_id") or s.get("hardcover_book_id")),
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
        "predicted_confidence": pred and audio_conf,
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
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    if status in ("announced", "released") and ad and ad >= t:
        return ad, False, None
    if include_estimates:
        pred = predicted_audiobook_date(series)
        if pred and pred >= t:
            nd = parse_date(series.get("novel_date"))
            gap = (pred - nd).days if nd else None
            if gap is None:
                note = "estimated from past releases"
            elif gap <= 3:
                note = "likely same day as the novel"
            elif gap < 21:
                note = f"~{gap} days after the novel"
            else:
                note = f"~{round(gap / 7)} weeks after the novel"
            return pred, True, note
    return None


def next_novel(series, include_estimates):
    """Next NOVEL event: (date, is_estimate, note) or None. The latest known novel
    counts when it's still upcoming (pre-order/announced). When it's already out, a
    predicted next book counts only when novel estimates are on."""
    t = today()
    nd = parse_date(series.get("novel_date"))
    if nd and nd >= t:
        return nd, False, None
    if include_estimates:
        pred = predicted_novel_date(series)
        if pred and pred >= t:
            avg = avg_novel_gap_days(series)
            if avg is None:
                note = "estimated from past releases"
            elif avg < 21:
                note = f"~{avg} days after the last book"
            elif avg < 90:
                note = f"~{round(avg / 7)} weeks after the last book"
            else:
                note = f"~{round(avg / 30.4)} months after the last book"
            return pred, True, note
    return None


def next_of_kind(series, kind, include_estimates):
    return next_novel(series, include_estimates) if kind == "novel" else next_audiobook(series, include_estimates)


def upcoming_of_kind(kind, include_estimates):
    """Upcoming releases of one kind ('novel' or 'audiobook'), soonest first.
    Backs the schedule view, the board, and the RSS feed."""
    rows = []
    with _lock:
        for s in _state["series"]:
            nxt = next_of_kind(s, kind, include_estimates)
            if not nxt:
                continue
            nd, est, note = nxt
            conf = None
            if est:
                gaps = novel_gap_days_list(s) if kind == "novel" else gap_days_list(s)
                conf = _estimate_confidence(gaps)
            rows.append({
                "series_id": s["id"],
                "series": s.get("name", ""),
                "title": s.get("novel_title") or s.get("name", ""),
                "author": s.get("author", ""),
                "date": iso(nd),
                "kind": kind,
                "estimate": est,
                "note": note,
                "confidence": conf,
                "countdown_days": (nd - today()).days,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def upcoming_audiobooks(include_estimates):
    return upcoming_of_kind("audiobook", include_estimates)


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

def _discord_post(webhook, text):
    """Blocking POST to a Discord webhook. Returns (ok, error_str)."""
    try:
        data = json.dumps({"content": text[:1900]}).encode("utf-8")
        req = Request(webhook, data=data,
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            resp.read()
        return True, ""
    except HTTPError as e:
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(getattr(e, "reason", e))
    except Exception as e:
        return False, str(e)


def _discord_notify(text):
    """Fire-and-forget Discord message (no-op if no webhook configured)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return

    def _go():
        ok, err = _discord_post(wh, text)
        if not ok:
            add_log(f"Discord notify failed: {err}")

    threading.Thread(target=_go, daemon=True).start()


def _nice_date(iso_str):
    d = parse_date(iso_str)
    return d.strftime("%b %-d, %Y") if d else (iso_str or "?")


def _countdown_phrase(iso_str):
    d = parse_date(iso_str)
    if not d:
        return ""
    n = (d - today()).days
    if n == 0:
        return " — **today**"
    if n == 1:
        return " — tomorrow"
    if n > 1:
        return f" — in {n} days"
    return ""


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
        when = _nice_date(new) + _countdown_phrase(new)
        if old:
            msgs.append(f"{icon[kind]} **{name}** — Book {n} {kind} date changed to **{when}** (was {_nice_date(old)})")
        else:
            msgs.append(f"{icon[kind]} **{name}** — Book {n} {kind} now dated **{when}**")
    return msgs


def _release_today_message(name, n, kind):
    icon = {"novel": "📖", "audiobook": "🎧"}.get(kind, "📚")
    return f"🎉 {icon} **{name}** — Book {n} {kind} releases **today**!"


def _release_day_keys_due(release_hour):
    """Find (key, name, n, kind) tuples for releases dated today, once it's past the
    release hour, that haven't been announced yet. Caller is responsible for the lock."""
    now = datetime.now()
    if now.hour < int(release_hour):
        return []
    td = today()
    notified = set(_state.get("discord_notified", []))
    due = []
    for s in _state["series"]:
        for e in s.get("timeline", []):
            n = _entry_n(e)
            for kind in ("novel", "audiobook"):
                val = e.get(kind)
                if parse_date(val) == td:
                    key = f"release:{s['id']}:{n}:{kind}:{val}"
                    if key not in notified:
                        due.append((key, s.get("name", ""), n, kind))
    return due


def _do_release_check():
    st = get_settings()
    if not st.get("discord_on_release", True) or not (st.get("discord_webhook") or "").strip():
        return
    msgs = []
    with _lock:
        due = _release_day_keys_due(st.get("discord_release_hour", 15))
        if due:
            notified = set(_state.get("discord_notified", []))
            for key, name, n, kind in due:
                notified.add(key)
                msgs.append(_release_today_message(name, n, kind))
            # keep the announced-list from growing without bound
            _state["discord_notified"] = list(notified)[-500:]
            save_state()
    for m in msgs:
        _discord_notify(m)


_release_thread_started = False


def _start_release_watcher():
    global _release_thread_started
    if _release_thread_started:
        return
    _release_thread_started = True

    def _loop():
        time.sleep(20)  # let things settle on startup
        while True:
            try:
                _do_release_check()
            except Exception as e:
                add_log(f"release check error: {e}")
            time.sleep(600)  # re-check every 10 minutes

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
        "upcoming_mode": mode,
        "show_estimates": get_settings().get("show_estimates", True),
        "show_estimates_novel": get_settings().get("show_estimates_novel", True),
        "log": log_rows,
        "counts": {
            "total": len(views),
            "out": sum(1 for v in views if v["audiobook_status"] == "released"),
            "announced": sum(1 for v in views if v["audiobook_status"] == "announced"),
            "waiting": sum(1 for v in views if v["audiobook_status"] in ("none", "unknown")),
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
        guid = f"{it['series_id']}-{it['date']}-{it['kind']}"
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

    def _int_or_none(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    hsid = _int_or_none(body.get("hardcover_series_id"))
    hbid = _int_or_none(body.get("hardcover_book_id"))
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
        "history": _clean_history(body.get("history")),
        "timeline": body.get("timeline") or [],
        "notes": (body.get("notes") or "").strip(),
        "hardcover_series_id": hsid,
        "hardcover_book_id": hbid,
        "source": body.get("source") or ("hardcover" if (hsid or hbid) else "manual"),
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
            tl = _clean_timeline(body.get("timeline"))   # rows of {n, novel, audiobook}
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
    """Accept one book read from an Amazon/Audible page (via the userscript) and
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
        ent = next((e for e in tl if _entry_n(e) == n), None)
        if not ent:
            ent = {"n": n, "novel": None, "audiobook": None}
            tl.append(ent)
        old_novel = ent.get("novel")
        old_audio = ent.get("audiobook")
        if nd:
            ent["novel"] = nd                 # Amazon is authoritative for this field
        if ad:
            ent["audiobook"] = ad
        tl.sort(key=lambda e: (e.get("novel") or e.get("audiobook") or "", _entry_n(e) or 0))
        s["history"] = _history_from_timeline(tl)
        if author and not s.get("author"):
            s["author"] = author
        if cover and not s.get("cover_url"):
            s["cover_url"] = cover           # first cover we see for a new series
        if book.get("asin"):
            s.setdefault("asins", {})[str(n)] = book["asin"]
        # If this is the latest book in the series, reflect it on the card headline.
        maxn = max((_entry_n(e) for e in tl if _entry_n(e) is not None), default=None)
        if n == maxn:
            if book.get("title"):
                s["novel_title"] = book["title"]
            if cover:
                s["cover_url"] = cover        # keep the card cover on the newest book
            if ent.get("novel"):
                s["novel_date"] = ent["novel"]
            s["audiobook_date"] = ent.get("audiobook")
            adt = parse_date(ent.get("audiobook"))
            s["audiobook_status"] = ("released" if (adt and adt <= today())
                                     else ("announced" if adt else "none"))
        s["edited"] = True
        s["last_change"] = f"Updated from Amazon: book {n}"
        s["last_change_at"] = datetime.now().isoformat(timespec="seconds")
        _state["last_ingest"] = s["last_change_at"]
        new_date_msgs = _new_date_messages(s, n, old_novel, ent.get("novel"), old_audio, ent.get("audiobook"))
        save_state()
    for m in new_date_msgs:
        _discord_notify(m)
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


@app.after_request
def _ingest_cors(resp):
    if request.path in ("/api/ingest", "/api/series/clear"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


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
                    "discord_release_hour": s.get("discord_release_hour", 15)})


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
        if "discord_release_hour" in body:
            try:
                h = int(body.get("discord_release_hour"))
                if 0 <= h <= 23:
                    s["discord_release_hour"] = h
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
    ok, err = _discord_post(wh, f"✅ **{APP_NAME}** test message — notifications are wired up.")
    return jsonify({"ok": ok, "message": "Sent! Check your Discord channel." if ok else f"Failed: {err}"})


@app.get("/feed.xml")
def feed():
    base = request.host_url.rstrip("/")
    return Response(build_rss(base), mimetype="application/rss+xml")


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
        nice = d.strftime("%b %-d, %Y") if d else it["date"]
        cd = it["countdown_days"]
        cd_word = "today" if cd == 0 else ("day" if cd == 1 else "days")
        cd_num = "" if cd == 0 else str(cd)
        rows.append(
            f'<div class="row{" soon" if i == 0 else ""}">'
            f'<div class="cd">{cd_num}<small>{cd_word}</small></div>'
            f'<div class="info"><div class="title">{html.escape(it["title"])}</div>'
            f'<div class="meta">{html.escape(nice)}'
            f'<span class="tag">Audiobook</span>{est}'
            + (f' &middot; {html.escape(it["series"])}' if it["series"] and it["series"] != it["title"] else "")
            + "</div></div></div>"
        )
    body = "".join(rows) if rows else '<div class="empty">No audiobooks upcoming. Turn on estimates in Release Radar, or check back later.</div>'
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


def _clean_timeline(raw):
    """Clean editor rows into timeline entries, preserving the book number (n)."""
    out = []
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
                out.append({"n": num, "novel": nov, "audiobook": aud})
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
.checked{font-size:10px;color:var(--faint);font-family:var(--mono);white-space:nowrap}
.rowbtns{display:flex;gap:5px}
.rowbtns button{padding:5px 9px;font-size:12px}

/* gap timeline signature */
.timeline{margin-top:7px;padding-top:7px;border-top:1px dashed var(--line)}
.tl-row{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:10.5px;color:var(--muted);margin:3px 0}
.track{position:relative;flex:1;height:2px;background:var(--line);border-radius:2px;min-width:80px}
.tl-today{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--faint);opacity:.7;transform:translateX(-50%)}
.tl-today::after{content:'';position:absolute;top:-2px;left:-2px;width:5px;height:5px;border-radius:50%;background:var(--faint);opacity:.9}
.mark{position:absolute;top:50%;transform:translate(-50%,-50%);width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 0 1.5px var(--card)}
.mark.novel{color:var(--violet)}
.mark.audio{color:var(--amber)}
.mark.both{width:11px;height:11px;color:var(--amber);background:linear-gradient(135deg,var(--violet) 0 50%,var(--amber) 50% 100%)}
.mark.est{color:var(--amber);background:transparent;border:1.5px dashed currentColor;box-shadow:none}
.mark.future{background:transparent;border:2px solid currentColor;box-shadow:none}
.mark.future.both{background:transparent;border:2px solid var(--amber)}
.gaplabel{flex:0 0 auto;color:var(--ink)}
.legend{display:flex;gap:14px;margin:-2px 0 10px;font-size:10.5px;color:var(--faint);font-family:var(--mono);flex-wrap:wrap}
.legend span{display:inline-flex;align-items:center;gap:5px}
.legend .sw{width:9px;height:9px;border-radius:50%;display:inline-block}
.legend .sw.novel{background:var(--violet)}
.legend .sw.audio{background:var(--amber)}
.legend .sw.both{width:11px;height:11px;background:linear-gradient(135deg,var(--violet) 0 50%,var(--amber) 50% 100%)}
.legend .sw.est{background:transparent;border:1.5px dashed var(--amber)}
.legend .sw.upc{background:transparent;border:2px solid var(--muted)}
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
    <span><i class="sw upc"></i>upcoming (hollow)</span>
    <span><i class="sw est"></i>estimated</span>
    <span><i class="sw today"></i>today</span>
  </div>
  <div class="grid" id="grid"></div>

  <div id="empty" class="empty" style="display:none">
    <h2>Nothing on the radar yet</h2>
    <p>Open an Amazon or Audible page for any book in a series (with the Release Radar extension installed) and it'll be collected automatically. Or hit <b>Crawl all</b> once you've opened a few.</p>
  </div>

  <div class="activity" id="activity"></div>
</div>

<!-- modal -->
<div class="scrim" id="scrim">
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="mTitle">
    <h2 id="mTitle">Add series</h2>
    <input type="hidden" id="f_id">

    <label>Amazon / Audible link</label>
    <div class="linkrow">
      <input id="f_url" placeholder="https://www.audible.com/pd/..." >
      <button class="small" id="autofillBtn" type="button">Auto-fill</button>
    </div>
    <div class="hint">Auto-fill now cross-checks Apple Books and Audnexus too, so it often works even when the page itself can't be read. Anything it misses, type by hand.</div>

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
      <input type="checkbox" id="discOnRelease" style="width:auto;margin:0"> Ping on release day, after
      <input type="number" id="discHour" min="0" max="23" style="width:54px;margin:0;padding:4px 6px"> <span style="color:var(--faint)">:00 (local time)</span>
    </label>
    <div style="border-bottom:1px solid var(--line);margin:16px 0"></div>

    <label style="color:#ff9b9b">Danger zone</label>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="small ghost" type="button" id="clearAllBtn" style="border-color:#5a3157;color:#ffb0b0">Clear all tracked series</button>
      <span class="hint" style="margin:0">Wipes every series so you can rebuild from Amazon only.</span>
    </div>
    <div class="hint">Your <code>data.json</code> is backed up first (recoverable by renaming <code>data.json.bak</code>), and settings are kept. After clearing, open one Amazon/Audible page per series (or hit "Crawl all now" in the extension) and the crawl repopulates everything.</div>

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
function dateTag(d){const r=isReleased(d);if(r===null)return '';
  const css=r?'color:#56d18a;border-color:#2c5a3f;background:#16271d':'color:#e7b14c;border-color:#5a4a25;background:#262017';
  return `<span style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid;border-radius:6px;margin-left:6px;vertical-align:middle;${css}">${r?'out':'pre-order'}</span>`;}
function confChip(c){
  if(!c) return '';
  const M={high:['high','#56d18a','#16271d','#2c5a3f'],medium:['med','#f0a500','#262017','#5a4a25'],low:['low','#b9a6d6','#221d3a','#3a3360']};
  const m=M[c.level]||M.low;
  const pm=(c.spread_days!=null)?` ±${c.spread_days}d`:'';
  const basis=(c.spread_days!=null)
    ? `${c.level} confidence — based on ${c.n} past gap${c.n===1?'':'s'} that varied by about ±${c.spread_days} days`
    : `${c.level} confidence — only ${c.n} past release to go on, so the spread is unknown`;
  return ` <span title="${esc(basis)}" style="font:600 9px/1 var(--mono,monospace);letter-spacing:.04em;text-transform:uppercase;padding:1px 5px;border:1px solid ${m[3]};background:${m[2]};color:${m[1]};border-radius:6px;vertical-align:middle">${m[0]}${pm}</span>`;}
function esc(s){const e=document.createElement('div');e.textContent=s||'';return e.innerHTML;}
function countWord(n){if(n===0)return 'today';if(n===1)return 'in 1 day';return 'in '+n+' days';}

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
  renderHero();renderGrid();renderActivity();
  checkForNotifications();
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
  // one entry per unique date, tracking the book number(s) on that date
  const ev={};
  const add=(d,k,num)=>{if(!d)return;const e=(ev[d]=ev[d]||{});e[k]=true;if(num!=null){if(k==='novel')e.nN=num;else if(k==='audio')e.aN=num;}};
  const tl=(s.timeline&&s.timeline.length)?s.timeline:null;
  if(tl){tl.forEach(t=>{add(t.novel,'novel',t.n);add(t.audiobook,'audio',t.n);});}
  else{(s.history||[]).forEach(h=>{add(h.novel,'novel',h.n);add(h.audiobook,'audio',h.n);});}
  add(s.novel_date,'novel',s.novel_n);                          // always include the tracked book
  if(s.audiobook_status!=='none') add(s.audiobook_date,'audio',s.novel_n);
  if((s.audiobook_status==='none'||s.audiobook_status==='unknown')&&s.predicted_date) add(s.predicted_date,'est',null);

  const dates=Object.keys(ev).filter(d=>!isNaN(+new Date(d+'T00:00:00')));
  if(!dates.length) return '';
  const times=dates.map(d=>+new Date(d+'T00:00:00'));
  const min=Math.min(...times),max=Math.max(...times),span=(max-min)||1;
  const rel=(d)=>{const r=isReleased(d);return r===null?'':(r?' (out)':' (pre-order)');};
  const marks=dates.map(d=>{
    const t=+new Date(d+'T00:00:00');
    const left=dates.length===1?50:((t-min)/span*100);
    const e=ev[d];let cls,title;
    const nn=e.nN!=null?(' #'+e.nN):'', an=e.aN!=null?(' #'+e.aN):'';
    if(e.novel&&e.audio){cls='both';title=`Novel${nn} + Audiobook${an} · ${fmtDate(d)}${rel(d)}`;}
    else if(e.novel){cls='novel';title=`Novel${nn} · ${fmtDate(d)}${rel(d)}`;}
    else if(e.audio){cls='audio';title=`Audiobook${an} · ${fmtDate(d)}${rel(d)}`;}
    else{cls='est';title=`Estimated audiobook · likely ~${fmtDate(d)}`;}
    if(cls!=='est'&&isReleased(d)===false) cls+=' future';   // hollow = upcoming / pre-order
    return `<span class="mark ${cls}" style="left:${left.toFixed(1)}%" title="${esc(title)}"></span>`;
  }).join('');
  // "today" line: only when it falls within the span, so past sits left / upcoming right
  const now=Date.now();let today='';
  if(dates.length>1&&now>=min&&now<=max){const tl=((now-min)/span*100);today=`<span class="tl-today" style="left:${tl.toFixed(1)}%" title="today"></span>`;}
  const lbl=s.avg_gap_label?`avg gap ${s.avg_gap_label}`:(dates.length>1?'releases':'release');
  return `<div class="timeline"><div class="tl-row">
      <span class="gaplabel">${lbl}</span>
      <span class="track">${today}${marks}</span>
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
    arr.sort((a,b)=>(rank[a.audiobook_status]??1)-(rank[b.audiobook_status]??1));}
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
      if((st==='none'||st==='unknown')&&s.predicted_date){
        predict=`<div class="predict">Likely audiobook <span class="mono">~ ${fmtDate(s.predicted_date)}</span> · ${s.history_count} past gap${s.history_count===1?'':'s'} (${s.avg_gap_label})${confChip(s.predicted_confidence)}</div>`;
      }
    }
    const err=s.fetch_error?`<div class="err">⚠ ${esc(s.fetch_error)}</div>`:'';
    const chgTime=s.last_change_at?` <span class="chg-time" title="${esc(new Date(s.last_change_at).toLocaleString())}">· ${timeAgo(s.last_change_at)}</span>`:'';
    const chg=s.last_change?`<div class="changed">● ${esc(s.last_change)}${chgTime}</div>`:'';
    const cover=s.cover_url
      ? `<img class="cover" src="${esc(s.cover_url)}" alt="" onerror="this.outerHTML='<div class=&quot;cover ph&quot;>${esc(initials(s.name))}</div>'">`
      : `<div class="cover ph">${esc(initials(s.name))}</div>`;
    card.innerHTML=`
      ${manual?'<div class="grip" title="Drag to reorder">⋮⋮</div>':''}
      ${cover}
      <div class="cmain">
        <h3>${esc(s.name)}</h3>
        <div class="byline">${s.author?esc(s.author):'<span style="color:var(--faint)">unknown author</span>'}${s.novel_title?' · '+esc(s.novel_title):''}</div>
        <div class="facts">
          <div class="fact"><div class="k">Novel</div><div class="v">${fmtDate(s.novel_date)}${dateTag(s.novel_date)}</div></div>
          <div class="fact"><div class="k">Audiobook</div><div class="v">${st==='none'?'—':(fmtDate(s.audiobook_date)+dateTag(s.audiobook_date))}</div></div>
          <div class="fact"><div class="k">Avg gap</div><div class="v">${s.avg_gap_label||'—'}</div></div>
        </div>
        <span class="pill ${st}">${esc(s.status_label)}</span>
        ${predict}${chg}${err}
        ${timelineHtml(s)}
      </div>
      <div class="right">
        <div class="rowbtns">
          <button class="small" data-edit="${s.id}">Edit</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
  wireDrag();
  grid.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openModal(b.dataset.edit));
  grid.querySelectorAll('[data-fetch]').forEach(b=>b.onclick=async()=>{
    const before=(STATE.series.find(x=>x.id===b.dataset.fetch)||{}).last_change||null;
    b.textContent='…';
    const v=await api('/api/series/'+b.dataset.fetch+'/fetch',{method:'POST'});
    if(v.fetch_error) toast('Couldn\'t reach a source — see the card');
    else if(v.last_change && v.last_change!==before) toast('Update: '+v.last_change);
    else toast('Checked — nothing new');
    refresh();
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
    $('#discHour').value=(s.discord_release_hour!=null?s.discord_release_hour:15);
  }catch(_){}
}
async function saveDiscord(){
  const body={
    discord_webhook:$('#discWebhook').value.trim(),
    discord_on_new:$('#discOnNew').checked,
    discord_on_release:$('#discOnRelease').checked,
    discord_release_hour:parseInt($('#discHour').value,10)
  };
  try{await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch(_){}
}
['#discWebhook','#discOnNew','#discOnRelease','#discHour'].forEach(sel=>{
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

async function askExtension(path,label){
  try{ const r=await api(path,{method:'POST'}); if(!r||!r.ok){toast('Could not reach the app');return;} }
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
    _start_release_watcher()
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
