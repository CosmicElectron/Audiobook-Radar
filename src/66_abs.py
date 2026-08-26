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


def _abs_req(path, method="GET", timeout=15):
    u, t = _abs_conf()
    if not u:
        return None
    req = Request(u + path, method=method,
                  headers={"Authorization": f"Bearer {t}", "Accept": "application/json",
                           "User-Agent": APP_NAME})
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


def _abs_listening_for(series_name):
    try:
        import file_audiobooks as _fa
        key = _fa.norm(series_name)
    except Exception:
        key = (series_name or "").lower()
    return _abs_cache["series"].get(key)



