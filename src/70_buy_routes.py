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
            job["dry_run"] = bool(get_settings().get("buy_dry_run", True))
            _job_set(job, "probing", "retry from dashboard")
            _buy_push_probe(job)
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
        if audio and _libation_owns(audio):
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


