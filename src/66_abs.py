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
# untouched across its updates, a patched phone app shows it under the
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



