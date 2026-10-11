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


