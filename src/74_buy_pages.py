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
        # this page is served on the public hostname: every field escaped, the
        # note scrubbed like anything else that leaves the machine
        esc = lambda v: html.escape(str(v or ""))                # noqa: E731
        series, n, title = esc(job.get("series")), esc(job.get("n")), esc(job.get("title"))
        note, status = esc(_public_text(job.get("note") or "")), esc(job["status"])
        if job["status"] != "awaiting_approval":
            body = f"<h1>Already decided</h1><div class='t'>{series} — book {n}</div><div class='m'>Status: {status}. {note}</div>"
            return Response(_BUY_PAGE % body, mimetype="text/html")
        body = (f"<h1>Buy this audiobook?</h1><div class='t'>{series} — book {n}</div>"
                f"<div class='m'>{title}</div><div class='p'>{ptxt}</div>"
                f"<div class='m'>Cap ${float(job.get('cap') or 0):.2f} · {note}</div>"
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
                    # local folder layout never leaves the machine
                    "buy_bat": ("" if remote else s.get("buy_bat", "")),
                    "buy_bat_found": ("" if remote else (_bat_path() or "")),
                    "tunnel_require_access": bool(s.get("tunnel_require_access")),
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
                    "buy_libation_found": ("" if remote else (_libation_files_dir() or ""))})


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
        if "tunnel_require_access" in body and not _is_remote():
            s["tunnel_require_access"] = bool(body.get("tunnel_require_access"))
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


