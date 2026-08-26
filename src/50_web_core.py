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
    if _origin_kind(request.headers.get("Origin")) == "web":
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
                    released_msgs.append(_release_live_message(s.get("name", ""), n, kind))
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
        sid_for_card = s.get("id")
        save_state()
    for m in new_date_msgs + narrator_msgs:
        _discord_notify(m, embeds=[_book_embed(series_name, n, cover_for_ping)])
    for m in released_msgs:
        # audiobook releases get a LIVING card: later stages (approval, bought,
        # downloaded, filed) edit this same message instead of new pings
        if ad and _release_card_create(sid_for_card, n, m, _book_embed(series_name, n, cover_for_ping)):
            continue
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


