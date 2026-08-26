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
        con = sqlite3.connect(f"file:{os.path.join(d, 'LibationContext.db')}?mode=ro", uri=True, timeout=2)
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



