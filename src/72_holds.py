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
    remote = _is_remote()
    out = []
    for h in holds:
        row = {**h, "candidates": _hold_candidates(h.get("reason"))}
        if remote:
            # over the tunnel: the book, never the machine's folder layout
            for key in ("src", "target"):
                if row.get(key):
                    row[key] = os.path.basename(str(row[key]).rstrip("\\/"))
        out.append(row)
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


