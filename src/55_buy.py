# ---------------------------------------------------------------------------
# Auto-buy: KU borrow -> buy the companion audiobook -> return the borrow
#
# The extension does the clicking (it is the logged-in browser); the server
# owns the decision and the record:
#   probing          asked the extension to read the Kindle page (signed in):
#                    is it KU, is the audiobook already owned, what's the
#                    companion price
#   waiting_offer    page read fine but no companion offer yet -> re-probe later
#   awaiting_approval  verify mode (or auto above the cap): email + Discord sent,
#                    token link approves/denies; expires after BUY_TOKEN_TTL_H
#   approved/running extension is borrowing + buying + returning, reporting
#                    each step
#   dry_done         dry-run walked the page and reported what it WOULD click
#   done             audiobook confirmed bought (ledger written), borrow returned,
#                    Update Audiobooks.bat ran
#   done_unreturned  bought, but the KU return failed -> return it by hand
#   uncertain        a buy click happened but no confirmation could be read.
#                    Ledger-marked so it can never be retried automatically.
#   owned/failed/denied/expired/cancelled/needs_signin   terminal, with a note
# ---------------------------------------------------------------------------

try:
    import pick_downloads      # the ONE definition of "a tracked book", shared with Update Audiobooks.bat
except ImportError:            # a copy without the file next to app.py: Libation checks answer "can't tell"
    pick_downloads = None


def _clean_buy_mode(v):
    v = (v or "off") if isinstance(v, str) else "off"
    v = v.strip().lower()
    return v if v in BUY_MODES else "off"


def _clean_buy_cap(v):
    if v in (None, "", False):
        return None
    try:
        f = float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return round(f, 2) if f >= 0 else None


def _buy_now():
    return datetime.now().isoformat(timespec="seconds")


def _buy_job_for(sid, n):
    return next((j for j in _state.get("buy_jobs", []) if j.get("series_id") == sid and j.get("n") == n), None)


def _buy_job_by_id(jid):
    return next((j for j in _state.get("buy_jobs", []) if j.get("id") == jid), None)


BUY_PAGE_CFG = {
    "borrow_label": r"add to library|read for free|^borrow",
    "borrow_id": r"borrow|kindle-unlimited|ku-?borrow|kubuy",
    "buy_label": r"^buy now with 1-click|^buy now$|^buy for|^pre-order",
    "add_audio_label": r"add (?:the )?audiobook|add (?:audible )?narration|upgrade with audio|get the audiobook|add narration",
    "narr_chk": "#narration-checkbox, input[type='checkbox'][id*='narration' i], input[type='checkbox'][name*='narration' i]",
    "narr_block": "#audibleNarration, #audible-narration, [id*='narration' i]",
    "expander": "",
    "checkout_label": r"place (?:your )?order|complete (?:your )?purchase|^buy now$|confirm (?:your )?(?:purchase|order)|pay now",
}
BUY_TAB_MODES = ("foreground", "window", "background")


def _job_cmd(job):
    """The slice of a job the extension needs."""
    return {"id": job["id"], "title": job.get("title", ""), "series": job.get("series", ""), "n": job.get("n"),
            "audio_asin": job.get("audio_asin"), "kindle_asin": job.get("kindle_asin"),
            "store_host": job.get("store_host") or "www.amazon.com.au",
            "cap": _job_cap(job), "price": job.get("price"), "dry_run": bool(job.get("dry_run")),
            "ku_borrowed_by_us": bool(job.get("ku_borrowed_by_us")),
            "cfg": dict(BUY_PAGE_CFG, tab_mode=(get_settings().get("buy_tab_mode") or "foreground"))}


def _buy_save_capture(name, html):
    """Keep the raw buy-box markup the extension saw (signed in) under
    %APPDATA%/ReleaseRadar/autobuy/ so the click rules can be tuned offline.
    Local only - it can contain session tokens."""
    if not html:
        return None
    try:
        d = os.path.join(data_dir(), "autobuy")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".html")
        with open(p, "w", encoding="utf-8") as f:
            f.write(html[:200000])
        return p
    except OSError:
        return None


def _job_cap(job):
    """A job's price cap; a missing/zero cap means the default from Settings."""
    try:
        c = float(job.get("cap") or 0)
    except (TypeError, ValueError):
        c = 0
    return c if c > 0 else (_clean_buy_cap(get_settings().get("buy_cap_default")) or BUY_DEFAULT_CAP)


def _job_step(job, step, ok, detail=""):
    job.setdefault("steps", []).append({"at": _buy_now(), "step": step, "ok": bool(ok), "detail": str(detail)[:400]})
    job["steps"] = job["steps"][-40:]
    job["updated"] = _buy_now()


def _job_set(job, status, note=None):
    job["status"] = status
    job["updated"] = _buy_now()
    if note is not None:
        job["note"] = str(note)[:300]


# ---- ownership: Libation's own library db is the truth for "already have it"
def _libation_ro_uri(files_dir):
    db = os.path.join(files_dir, "LibationContext.db")
    return pick_downloads.ro_uri(db) if pick_downloads else f"file:{db}?mode=ro"


def _libation_files_dir():
    cands = [get_settings().get("buy_libation_files") or "",
             os.path.join(os.path.expanduser("~"), "Libation"),
             os.path.join(os.environ.get("APPDATA", ""), "Libation"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Libation")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "LibationContext.db")):
            return c
    return None


def _series_key(name):
    """Series names as Audible and Amazon spell them differently ('The Unfallen
    Series' / 'The Unfallen'); same rule as pick_downloads.py."""
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    s = re.sub(r"^(?:the|a|an) ", "", s)
    return re.sub(r" (?:series|saga|trilogy|cycle)$", "", s).strip()


def _libation_match(asin=None, series=None, n=None, srs=None):
    """Libation's record of one book: {"in_library": bool, "downloaded": bool},
    or None when it can't be told. Recognition is pick_downloads' (series
    name + number, a recorded product ID, author + title with the date
    agreeing): Audible's ID is not the Amazon listing ASIN the radar knows
    (Riftside 4: B0HJ9CQDHW vs B0HJ9DX4S2), and a brand-new release can arrive
    with no series on Audible's record at all (Arcane Academy 5, 2 Oct 2026).
    srs: the radar's series record (author, the book's title, its audiobook
    date). "downloaded" follows the strongest match: Audible's own series
    number or a recorded ID outrank an author+title guess, so a liberated
    sibling can never make a failed download look complete."""
    d = _libation_files_dir()
    if not d or pick_downloads is None or not (asin or ((srs or series) and n is not None)):
        if d and pick_downloads is None:
            log.warning("pick_downloads.py is missing next to app.py - Libation checks can't tell")
        return None
    try:
        books = pick_downloads.library_books(d, pending_only=False)
        rec = dict(srs) if isinstance(srs, dict) else {}
        rec.setdefault("name", series or "")
        ent = pick_downloads.series_entry(rec, n=n) if n is not None else pick_downloads.series_entry({"name": ""})
        if asin:
            ent["asins"].add(asin)
        hits = []
        for b in books:
            m = pick_downloads.match_book(b, [ent])
            if m:
                hits.append((m[1] in (ent["name"], "matched by ID"), b["status"]))
    except Exception:
        return None
    strong = [st for sure, st in hits if sure]
    use = strong or [st for _, st in hits]
    return {"in_library": bool(hits),
            "downloaded": bool(use) and all(st == pick_downloads.LIBERATED for st in use)}


def _libation_owns(asin, series=None, n=None, srs=None):
    """True/False from Libation's library, None when it can't be checked."""
    m = _libation_match(asin, series, n, srs=srs)
    return None if m is None else m["in_library"]


# ---- creation (called from ingest, under the lock)
def _maybe_create_buy_job(s, n, ent, audio_date):
    mode = _clean_buy_mode(s.get("buy_mode"))
    if mode == "off":
        return None
    st = get_settings()
    if not st.get("buy_master"):
        return None                              # master switch off: nothing is queued
    if _buy_job_for(s["id"], n):
        return None
    audio_asin = (s.get("asins_audio") or {}).get(str(n))
    if not audio_asin:
        return None
    if audio_asin in _state.get("buy_ledger", {}):
        return None
    dd = parse_date(audio_date)
    if not dd or dd > today() or (today() - dd).days > BUY_PROBE_DAYS:
        return None                              # only around release day, never back-catalogue
    job = {"id": uuid.uuid4().hex[:10], "series_id": s["id"], "series": s.get("name", ""), "n": n,
           "title": (ent.get("title") or f"{s.get('name', '')} {n}"),
           "audio_asin": audio_asin, "kindle_asin": (s.get("asins_novel") or {}).get(str(n)),
           "store_host": s.get("store_host") or "www.amazon.com.au",
           "mode": mode, "cap": _clean_buy_cap(s.get("buy_cap")) or _clean_buy_cap(st.get("buy_cap_default")) or BUY_DEFAULT_CAP,
           "dry_run": bool(st.get("buy_dry_run", True)), "status": "probing", "price": None,
           "created": _buy_now(), "updated": _buy_now(), "probes": 0, "last_probe": None,
           "steps": [], "note": ""}
    owned = _libation_owns(audio_asin, series=s.get("name"), n=n, srs=s)
    if owned:
        _job_set(job, "owned", "Libation already has this audiobook")
    _state.setdefault("buy_jobs", []).append(job)
    _state["buy_jobs"] = _state["buy_jobs"][-100:]
    if job["status"] == "probing":
        _buy_push_probe(job)
    add_log(f"auto-buy: {job['status']} — {job['series']} book {n} ({mode}, cap ${job['cap']:.2f}"
            f"{', DRY RUN' if job['dry_run'] else ''})")
    return job


def _buy_push_probe(job):
    job["probes"] = int(job.get("probes") or 0) + 1
    job["last_probe"] = _buy_now()
    _push_command(type="buy", phase="probe", job=_job_cmd(job))


# a job parked or stopped for a reason a person can clear (signed back in, the
# store now sells it) - what the dashboard's Retry, !retry and the Discord
# button accept
BUY_RETRYABLE = ("needs_signin", "retry_later", "failed", "expired")


def _buy_retry(job, who):
    """Re-check a parked job NOW instead of at the next 3-hourly probe. It gets
    a fresh probe window, and a still-signed-out answer is announced again
    (with a new button). Caller holds _lock."""
    job["dry_run"] = bool(get_settings().get("buy_dry_run", True))
    job["probes"] = 0
    job["window_start"] = _buy_now()
    job.pop("signin_notified", None)
    job.pop("signin_button_at", None)
    _job_step(job, "retry", True, f"retry from {who}")
    _job_set(job, "probing", f"retry from {who}")
    _buy_push_probe(job)


def _buy_week_count():
    cutoff = datetime.now() - timedelta(days=7)
    n = 0
    for v in _state.get("buy_ledger", {}).values():
        try:
            if v.get("auto") and datetime.fromisoformat(v.get("at")) >= cutoff:
                n += 1
        except (TypeError, ValueError):
            pass
    return n


# ---- decision after a successful probe
def _buy_decide(job):
    st = get_settings()
    price = job.get("price")
    if job.get("dry_run"):
        _job_set(job, "running", "dry run: borrows on KU, finds the audiobook offer, returns the borrow - buys nothing")
        _push_command(type="buy", phase="dry", job=_job_cmd(job))
        _buy_live_begin(job)
        return
    if job.get("mode") == "auto" and price is not None and price <= _job_cap(job):
        if _buy_week_count() >= int(st.get("buy_weekly_max", BUY_WEEKLY_MAX)):
            _job_step(job, "decide", True, "weekly automatic limit reached — asking instead")
            _buy_request_approval(job, why="weekly limit")
            return
        _buy_approve(job, who="auto")
        return
    why = "verify mode" if job.get("mode") == "verify" else f"price ${price:.2f} is above the cap ${float(job.get('cap') or 0):.2f}"
    _buy_request_approval(job, why=why)


def _buy_public_base():
    b = (get_settings().get("buy_public_base") or "").strip().rstrip("/")
    return b or "http://127.0.0.1:8765"


def _buy_request_approval(job, why=""):
    token = secrets.token_urlsafe(32)
    job["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    job["expires"] = (datetime.now() + timedelta(hours=BUY_TOKEN_TTL_H)).isoformat(timespec="seconds")
    _job_set(job, "awaiting_approval", why)
    _release_card_line(job.get("series_id"), job.get("n"),
                       f"💳 waiting for your OK" + (f" at ${job.get('price'):.2f}" if isinstance(job.get('price'), (int, float)) else ""))
    link = f"{_buy_public_base()}/buy/{token}"
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else "price unknown"
    subject = f"Release Radar: buy the audiobook? {job.get('title', '')} — {ptxt}"
    text = (f"{job.get('series', '')} — book {job.get('n')}\n{job.get('title', '')}\n\n"
            f"Companion audiobook price: {ptxt}  (cap ${float(job.get('cap') or 0):.2f})\n"
            f"Why you're being asked: {why}\n\n"
            f"Approve or deny here (link works for {BUY_TOKEN_TTL_H} h, one decision only):\n{link}\n\n"
            f"What approving does: borrows the Kindle edition on Kindle Unlimited, buys the audiobook at the "
            f"companion price, returns the borrow, then runs Update Audiobooks.\n")
    _send_email_async(job["id"], subject, text)
    _discord_notify(_fmt_line("ask_buy", series=job.get("series", ""), n=job.get("n"),
                              price=ptxt, link=link) + f" ({why})")


def _buy_approve(job, who="link"):
    _job_step(job, "approve", True, f"approved by {who}")
    _release_card_line(job.get("series_id"), job.get("n"), "💳 approved — buying", replace_last=True)
    _job_set(job, "approved", f"approved by {who}")
    job["approved_at"] = _buy_now()
    _push_command(type="buy", phase="execute", job=_job_cmd(job))
    _job_set(job, "running", f"approved by {who}; extension is working")
    _buy_live_begin(job)


def _buy_deny(job, who="link"):
    _job_step(job, "deny", True, f"denied by {who}")
    _job_set(job, "denied", f"denied by {who}")


_EMAIL_ASYNC = True       # tests flip this to run inline


def _send_email_async(job_id, subject, text):
    """SMTP can block for many seconds: never do it under the state lock or
    inside a request. The outcome lands on the job as an 'email' step."""
    def _go():
        ok, msg = _send_email(subject, text)
        with _lock:
            job = _buy_job_by_id(job_id)
            if job:
                _job_step(job, "email", ok, msg)
            save_state()
    if _EMAIL_ASYNC:
        threading.Thread(target=_go, daemon=True).start()
    else:
        _go()


def _send_email(subject, text):
    st = get_settings()
    to, user, pw = st.get("buy_email_to"), st.get("buy_smtp_user"), st.get("buy_smtp_pass")
    if not (to and user and pw):
        return False, "email not configured (address, sender, app password)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    try:
        with smtplib.SMTP_SSL(st.get("buy_smtp_host") or "smtp.gmail.com", 465, timeout=25) as srv:
            srv.login(user, pw)
            srv.send_message(msg)
        return True, "sent"
    except Exception as e:        # noqa: BLE001
        return False, f"email failed: {e}"


# ---- reports from the extension
def _buy_handle_report(job, phase, ok, detail):
    """Apply one report from the extension to a job. Runs under the lock."""
    detail = detail if isinstance(detail, dict) else {"info": str(detail)}
    info = str(detail.get("info") or "")[:300]
    if phase == "progress":
        job["now"] = info                     # what it is trying right now: the live message, not a step
        return
    if phase == "probe":
        job["probes_ok"] = int(job.get("probes_ok") or 0) + (1 if ok else 0)
        if detail.get("buybox_html"):
            _buy_save_capture(f"probe-{detail.get('kindle_asin') or job.get('kindle_asin') or job.get('audio_asin')}",
                              detail.pop("buybox_html"))
        if detail.get("kindle_asin"):
            job["kindle_asin"] = detail["kindle_asin"]
        if detail.get("buttons") is not None:
            job["buttons"] = detail.get("buttons")
        _job_step(job, "probe", ok, info or json.dumps({k: v for k, v in detail.items() if k != "buttons"})[:300])
        if detail.get("signed_in") is False:
            _buy_needs_signin(job)
            return
        if not ok:
            _job_set(job, "waiting_offer", info or "probe failed; will retry")
            return
        if detail.get("owned"):
            _job_set(job, "owned", "the store says you already own this audiobook")
            return
        if isinstance(detail.get("ku"), bool):
            srs = _find(job.get("series_id")) if job.get("series_id") else None
            if srs:
                row = next((e for e in srs.get("timeline", []) if _entry_n(e) == job.get("n")), None)
                if row is not None:
                    row["ku"] = detail["ku"]
            if detail["ku"] is False:
                _job_set(job, "failed", "not a Kindle Unlimited title - the discounted "
                                        "companion price needs a KU borrow, so I won't buy this one")
                return
        price = detail.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is None:
            _job_set(job, "waiting_offer", "no companion-audiobook offer on the page yet")
            return
        job["price"] = round(price, 2)
        job["price_currency"] = detail.get("currency") or ""
        _buy_decide(job)
        return
    if phase == "dry":
        step = str(detail.get("step") or "dry")
        if detail.get("borrowed_by_us"):
            job["ku_borrowed_by_us"] = True
        if detail.get("html"):
            _buy_save_capture(f"dry-{re.sub(r'[^a-z0-9]+', '_', step.lower())}-{job.get('kindle_asin') or job.get('audio_asin')}", detail.pop("html"))
        _job_step(job, step, ok, info)
        if detail.get("final") or step == "dry":
            _job_set(job, "dry_done" if ok else "failed", info)
        return
    if phase == "execute":
        step = str(detail.get("step") or "step")
        if detail.get("html"):
            _buy_save_capture(f"exec-{re.sub(r'[^a-z0-9]+', '_', step.lower())}-{job.get('audio_asin')}", detail.pop("html"))
        _job_step(job, step, ok, info)
        if not ok:
            if job.get("purchased"):
                _job_set(job, "done_unreturned", f"{step} failed after the purchase: {info} — return the KU borrow by hand")
                _buy_after_purchase(job)
            elif step == "checkout" and "refused" in info.lower():
                n = int(job.get("store_retries") or 0) + 1
                job["store_retries"] = n
                if n > BUY_STORE_RETRY_MAX:
                    _job_set(job, "failed", f"Amazon kept refusing the checkout ({n - 1} tries): {info}")
                else:
                    job["retry_at"] = (datetime.now() + timedelta(hours=BUY_STORE_RETRY_H)).isoformat(timespec="seconds")
                    _job_set(job, "retry_later", f"Amazon refused the checkout (try {n}); trying again in {BUY_STORE_RETRY_H} h — "
                                                 f"usually the audiobook isn't on Audible AU yet")
                    _release_card_line(job.get("series_id"), job.get("n"),
                                       f"⏳ store refused (not on Audible AU yet) — retrying every {BUY_STORE_RETRY_H} h", replace_last=True)
                    if n == 1:
                        _discord_notify(f"\u23f3 Amazon couldn't process {job.get('series', '')} — book {job.get('n')} yet "
                                        f"('{info[:120]}'). Usually it's not on Audible AU yet; I'll retry every {BUY_STORE_RETRY_H} h.")
            elif detail.get("clicked"):
                _release_card_line(job.get("series_id"), job.get("n"), "⚠️ buy attempt unconfirmed — check Amazon")
                _job_set(job, "uncertain", f"a buy click happened but {step} could not be confirmed: {info}")
                _state.setdefault("buy_ledger", {})[job["audio_asin"]] = {
                    "series": job.get("series"), "n": job.get("n"), "at": _buy_now(), "job": job["id"],
                    "price": job.get("price"), "auto": job.get("mode") == "auto", "uncertain": True}
            else:
                _job_set(job, "failed", f"{step}: {info}")
            return
        if step == "confirm":
            job["purchased"] = True
            job["purchased_at"] = _buy_now()
            _release_card_line(job.get("series_id"), job.get("n"),
                               f"✅ bought" + (f" ${job.get('price'):.2f}" if isinstance(job.get('price'), (int, float)) else ""),
                               replace_last=True)
            _state.setdefault("buy_ledger", {})[job["audio_asin"]] = {
                "series": job.get("series"), "n": job.get("n"), "at": _buy_now(), "job": job["id"],
                "price": job.get("price"), "auto": job.get("mode") == "auto"}
        if step == "return":
            if job.get("purchased"):
                _release_card_line(job.get("series_id"), job.get("n"), "↩️ KU borrow returned")
                _job_set(job, "done", "bought, borrow returned")
                _buy_after_purchase(job)
        return
    if phase == "return":
        _job_step(job, "return", ok, info)
        if ok:
            job["ku_borrowed_by_us"] = False
            job["note"] = (job.get("note") or "") + " | KU borrow returned"
        return
    _job_step(job, phase, ok, info)


def _buy_needs_signin(job):
    """Firefox is signed out of Amazon. Park the job, tell the user ONCE (Discord +
    email), and keep re-probing on the normal cadence so the moment he signs in
    again the job carries on by itself. Reading dates still works signed out;
    only buying needs the session."""
    _job_set(job, "needs_signin", "Firefox is signed out of Amazon — sign in on the PC; I'll keep checking")
    if job.get("signin_notified"):
        return
    job["signin_notified"] = _buy_now()
    job["signin_button_at"] = _buy_now()
    head = (f"\U0001f511 Firefox is signed out of Amazon, so I can't buy {job.get('series', '')} — book {job.get('n')}. "
            f"Sign in again on the PC (any Amazon page)")
    tail = f"; otherwise I re-check every {BUY_PROBE_GAP_H} h for {BUY_PROBE_DAYS} days and carry on by myself."
    # the bot posts it with a Retry button (a webhook can't carry one); without
    # the bot, the webhook says where the Retry lives instead
    _notify_with_button(head + ", then hit the button" + tail,
                        [("I've signed in — retry now", f"rr:retry:{job['id']}")],
                        fallback=head + ", then hit Retry on the dashboard's purchase card" + tail)
    _send_email_async(job["id"], f"Release Radar: sign in to Amazon to buy {job.get('title', '')}", head + tail)


def _buy_after_purchase(job):
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else ""
    if not _release_card_line(job.get("series_id"), job.get("n"),
                              f"⬇️ downloading via Libation in {BUY_BAT_DELAY_S // 60} min…"):
        _discord_notify(_fmt_line("bought", series=job.get("series", ""), n=job.get("n"),
                                  price=ptxt or "the companion price")
                        + ("" if job.get("status") == "done" else " (Return the KU borrow by hand.)"))
    _run_update_audiobooks(job)


# ---- the bat
# ---- live Discord progress for Update Audiobooks -------------------------
def _discord_live_start(text, embeds=None):
    """Post a webhook message we can edit later. Returns the edit URL or None."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return None
    try:
        payload = {"content": (text or "")[:1900]}
        if embeds:
            payload["embeds"] = [e for e in embeds if e][:10]
        data = json.dumps(payload).encode("utf-8")
        url = wh + ("&" if "?" in wh else "?") + "wait=true"
        req = Request(url, data=data,
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            mid = json.loads(resp.read().decode("utf-8", "replace")).get("id")
        return f"{wh.split('?')[0]}/messages/{mid}" if mid else None
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord live message failed: {_no_secrets(str(e))}")
        return None


def _discord_live_edit(edit_url, text):
    if not edit_url:
        return False
    try:
        data = json.dumps({"content": (text or "")[:1900]}).encode("utf-8")
        req = Request(edit_url, data=data, method="PATCH",
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:           # noqa: BLE001
        return False


class _BatProgress:
    """Turns Update Audiobooks.bat output into one live progress message.
    Line shapes from real runs: '1/3  Scanning...', 'Scanning Audible library:
    2 accounts...', 'Total processed: 321', 'New: 1',
    'DownloadDecryptBook Begin: date [ASIN] Title', '...Completed: ...',
    then the filer's FILED/NEW/ADOPT/HOLD lines."""
    BAR_W = 12

    def __init__(self):
        self.phase = "starting"
        self.accounts = None
        self.processed = None
        self.new = None
        self.total = 0
        self.begun = 0
        self.done_dl = 0
        self.failed_dl = 0
        self.picked = None          # what the tracked-series picker let through (None: picker not seen)
        self.skipped = 0            # ...and what it refused
        self.picked_ids = {}        # Audible product ID -> why the picker took it
        self.current = ""
        self.current_asin = None
        self.book_pct = None        # how far the current book's download is (sampled from Libation's temp file)
        self.filed = []
        self.finished = False
        self.rc = None
        self.attempt = 1
        self._buf = ""

    def feed(self, chunk):
        self._buf += chunk.replace("\r", "")
        *lines, self._buf = self._buf.split("\n")
        for l in lines:
            self._line(l.strip())

    def _line(self, l):
        if not l:
            return
        if l.startswith("1/3"):
            self.phase = "scan"
        elif l.startswith("2/3"):
            self.phase = "download"
        elif l.startswith("3/3"):
            self.phase = "file"
        m = re.match(r"Scanning Audible library:\s*(\d+)\s*account", l)
        if m:
            self.accounts = int(m.group(1))
        m = re.match(r"Total processed:\s*(\d+)", l)
        if m:
            self.processed = int(m.group(1))
        m = re.match(r"New:\s*(\d+)", l)
        if m:
            self.new = int(m.group(1))
            if self.picked is None:
                self.total = max(self.total, self.new)
        # the tracked-series picker decides what is actually downloaded: its
        # count is the bar's total, not Libation's "New: N" (a 1/2 bar that
        # never filled, 7 Oct 2026, was one tracked book out of two new ones)
        if l.startswith("Download (tracked):"):
            self.picked = (self.picked or 0) + 1
            self.total = self.picked
            m = re.search(r"\[([A-Z0-9]+)\]\s*-\s*(.+?)\s*$", l)
            if m:
                self.picked_ids[m.group(1)] = m.group(2)
        elif l.startswith("Not downloading (not in a tracked series):"):
            self.skipped = len([x for x in l.split(":", 1)[1].split(";") if x.strip()])
        elif l.startswith("Nothing new from the series Release Radar tracks"):
            self.picked = 0
            self.total = 0
        m = re.match(r"DownloadDecryptBook Begin:\s*\S+\s*\[([A-Z0-9]+)\]\s*(.+)", l)
        if m:
            self.begun += 1
            self.total = max(self.total, self.begun)
            self.current_asin, self.current = m.group(1), m.group(2).strip()
            self.book_pct = None
        if re.match(r"DownloadDecryptBook Completed:", l):
            self.done_dl += 1
            self.book_pct = None
        if l.startswith("Error processing book"):
            self.failed_dl += 1
            self.done_dl = max(0, self.done_dl - 1)       # Libation says Completed before it says Error
        if re.match(r"(FILED|NEW|ADOPT|HOLD)\b", l) and not re.match(r"New:", l):
            self.filed.append(l[:120])

    def bar(self):
        if not self.total:
            return ""
        done = min(self.done_dl, self.total)
        # the book in flight moves the bar too, by how far its download is
        partial = (self.book_pct or 0) / 100 if (done < self.total and self.book_pct) else 0
        f = int(self.BAR_W * (done + partial) / self.total)
        return "\u25b0" * f + "\u25b1" * (self.BAR_W - f) + f"  {done}/{self.total}"

    def book_bar(self):
        if self.book_pct is None:
            return ""
        f = int(8 * self.book_pct / 100)
        return " " + "\u25b0" * f + "\u25b1" * (8 - f) + f" {self.book_pct}%"

    def render(self):
        rows = ["\U0001f4e6 **Update Audiobooks**" + (f" (attempt {self.attempt})" if self.attempt > 1 else "")]
        if self.processed is not None:
            tracked = f" \u00b7 {self.picked} tracked" if self.picked is not None and self.new else ""
            rows.append(f"\u2705 Scan: {self.processed} titles, {self.new or 0} new{tracked}")
        elif self.phase == "starting":
            rows.append("\u23f3 starting\u2026")
        else:
            rows.append("\U0001f50e Scanning the Audible library\u2026"
                        + (f" ({self.accounts} accounts)" if self.accounts else ""))
        if self.processed is not None:
            if self.total == 0:
                rows.append("\U0001f4a4 Nothing new from the series you track"
                            + (f" ({self.skipped} skipped, not tracked)" if self.skipped else ""))
            else:
                # a tick only when every book came through; a download that
                # Libation gave up on is a warning, not a tick
                all_done = self.done_dl >= self.total and not self.failed_dl
                downloading = self.phase == "download" and not self.finished and not all_done and not self.failed_dl
                icon = "\u2b07\ufe0f" if downloading else ("\u2705" if all_done else "\u26a0\ufe0f")
                cur = f" \u00b7 *{self.current}*{self.book_bar()}" if downloading and self.current else ""
                fail = f" \u00b7 {self.failed_dl} failed (Libation will retry)" if self.failed_dl else ""
                skip = f" \u00b7 {self.skipped} skipped, not tracked" if self.skipped else ""
                rows.append(f"{icon} Download  {self.bar()}{cur}{fail}{skip}")
        if self.phase == "file" and not self.finished:
            rows.append("\U0001f5c2\ufe0f Filing into the library\u2026")
        for l in self.filed[-6:]:
            rows.append("\u2003" + l)
        if self.finished:
            rows.append({0: "\u2705 **Done.**",
                         2: "\u26a0\ufe0f **Done \u2014 some books are HELD** (see the lines above)."}
                        .get(self.rc, f"\u274c **Failed** (rc={self.rc}) \u2014 see autobuy-bat.log"))
        return "\n".join(rows)[:1900]


class _LiveEditor:
    """Throttled edits: at most one PATCH per interval, only when the text changed."""
    def __init__(self, edit_url, interval=2.5):
        self.url = edit_url
        self.interval = interval
        self._last_t = 0.0
        self._last_text = ""

    def push(self, text, force=False):
        if not self.url or text == self._last_text:
            return
        now = time.monotonic()
        if not force and (now - self._last_t) < self.interval:
            return
        if _discord_live_edit(self.url, text):
            self._last_t = now
            self._last_text = text


# ---- live Discord progress for a purchase run ------------------------------
# The outcome alone wasn't enough (29 Sep 2026: a purchase stopped mid-way and
# the only trace was the dashboard card). One message per run, edited in place
# as the extension reports: each step's result, what it is trying right now,
# then the outcome. A failure also gets a Retry button (bot) of its own.
BUY_STEP_TEXT = {"approve": "Approved", "retry": "Retry", "borrow": "Kindle Unlimited borrow",
                 "borrow-ajax": "Borrow (fallback)", "borrow-verify": "Borrow confirmed",
                 "buy-click": "Pressed Buy", "payment": "Payment method", "review": "Review page",
                 "checkout": "Checkout", "confirm": "Order confirmation", "return": "Kindle Unlimited return",
                 "offer": "Audiobook offer"}
BUY_RUN_STATES = ("probing", "awaiting_approval", "approved", "running")
_BUY_OUTCOME = {
    "done": "✅ Bought — Update Audiobooks runs next.",
    "done_unreturned": "✅ Bought — but return the KU borrow by hand.",
    "failed": "❌ Stopped — nothing was bought.",
    "uncertain": "⚠️ A Buy click happened but I couldn't see a confirmation — check your Amazon orders before retrying.",
    "retry_later": "⏳ Amazon refused the checkout — I'll try again automatically.",
    "dry_done": "\U0001f9ea Dry run finished — nothing was bought.",
}
_buy_live = {}


def _buy_step_text(name):
    if name.startswith("offer on "):
        return "Audiobook offer on the " + name[len("offer on "):]
    return BUY_STEP_TEXT.get(name, name)


def _buy_live_render(job):
    price = job.get("price")
    ptxt = f"${price:.2f}" if isinstance(price, (int, float)) else "price ?"
    lines = [f"\U0001f6d2 **{job.get('series', '')} — book {job.get('n')}** · {ptxt}"
             + (" · dry run" if job.get("dry_run") else "")]
    since = job.get("run_started") or ""
    for st in job.get("steps") or []:
        if (st.get("at") or "") < since:
            continue
        name, detail = str(st.get("step") or ""), str(st.get("detail") or "")
        mark = "✅" if st.get("ok") else "❌"
        if name.startswith("offer on ") and detail.startswith("not here"):
            mark = "➖"                   # looked, not on this page: the search moves on
        lines.append(f"{mark} {_buy_step_text(name)} — {detail[:150]}")
    status = job.get("status")
    if status in BUY_RUN_STATES:
        if job.get("now"):
            lines.append(f"⏳ now: {str(job['now'])[:160]}")
    else:
        out = _BUY_OUTCOME.get(status, f"Finished: {status}.")
        if status in ("failed", "uncertain") and job.get("note"):
            out += f"\n({str(job['note'])[:200]})"
        lines.append(out)
    return "\n".join(lines)[:1900]


class _BuyLive:
    """One live Discord message per purchase run. Network happens on its own
    thread; callers (holding _lock) only drop the latest text in."""
    IDLE_LIMIT_S = 3 * 3600

    def __init__(self, text):
        self.text, self.final, self.ev = text, False, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def set(self, text, final=False):
        self.text = text
        self.final = self.final or final
        self.ev.set()

    def _run(self):
        ed = _LiveEditor(_discord_live_start(self.text))
        shown, idle = self.text, 0
        while True:
            if not self.ev.wait(timeout=60):
                idle += 60
                if idle >= self.IDLE_LIMIT_S:
                    return                    # an abandoned run: stop watching
                continue
            self.ev.clear()
            idle = 0
            t = self.text
            if t != shown:
                ed.push(t, force=True)
                shown = t
            if self.final and self.text == shown:
                return
            time.sleep(1.2)                   # pace the edits; anything newer is picked up next loop


def _buy_live_begin(job):
    """A purchase run (dry or real) was handed to the extension."""
    job["run_started"] = _buy_now()
    job["now"] = "handed to the browser extension — waiting for it to pick the job up"
    _buy_live[job["id"]] = _BuyLive(_buy_live_render(job))


def _buy_live_update(job):
    lv = _buy_live.get(job.get("id"))
    if not lv:
        return
    final = job.get("status") not in BUY_RUN_STATES
    if final:
        job.pop("now", None)
    lv.set(_buy_live_render(job), final=final)
    if final:
        _buy_live.pop(job["id"], None)
        if job.get("status") == "failed":
            head = (f"❌ {job.get('series', '')} — book {job.get('n')}: the purchase stopped "
                    f"({str(job.get('note') or '')[:160]}). Nothing was bought.")
            _notify_with_button(head + " Retry once it's sorted:",
                                [("Retry", f"rr:retry:{job['id']}")],
                                fallback=head + " Retry it from the dashboard's purchase card (or `!retry`).")



def _bat_path():
    p = (get_settings().get("buy_bat") or "").strip()
    if p and os.path.isfile(p):
        return p
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for c in (os.path.join(base, "Update Audiobooks.bat"),
              os.path.join(os.path.dirname(base), "Update Audiobooks.bat")):
        if os.path.isfile(c):
            return c
    return None


LIBATION_MB_PER_MIN = 0.91      # measured: Riftside 4 1202 min -> 1093 MB, Loopbreaker 1 1313 min -> 1194 MB
_libation_len_cache = {}


def _libation_length_minutes(asin):
    if asin in _libation_len_cache:
        return _libation_len_cache[asin]
    d = _libation_files_dir()
    mins = None
    if d:
        try:
            con = sqlite3.connect(_libation_ro_uri(d), uri=True, timeout=2)
            try:
                row = con.execute("select LengthInMinutes from Books where AudibleProductId = ?", (asin,)).fetchone()
            finally:
                con.close()
            mins = int(row[0]) if row and row[0] else None
        except Exception:       # noqa: BLE001
            mins = None
    _libation_len_cache[asin] = mins
    return mins


def _libation_download_pct(asin):
    """How far Libation's in-flight download of a book is, 0-99, or None.
    LibationCli prints nothing between 'Begin' and 'Completed'; the growing
    file in its DownloadsInProgress folder, against the book's length, is the
    only progress there is (the encrypted download is ~0.91 MB per minute)."""
    if not asin:
        return None
    temp = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Temp")
    size = None
    try:
        for name in os.listdir(temp):
            if not name.lower().startswith("libation"):
                continue
            d = os.path.join(temp, name, "DownloadsInProgress")
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.upper().startswith(asin.upper() + "."):
                    size = max(size or 0, os.path.getsize(os.path.join(d, f)))
    except OSError:
        return None
    if size is None:
        return None
    mins = _libation_length_minutes(asin)
    if not mins:
        return None
    return int(min(99, 100 * size / (mins * LIBATION_MB_PER_MIN * 1048576)))


def _run_bat_once(bat, on_output=None, on_tick=None):
    """Run Update Audiobooks.bat --auto hidden; returns (rc, tail of its output).
    on_output, when given, receives each new chunk of the bat's output while it
    runs (drives the live Discord progress message); on_tick runs every couple
    of seconds regardless of output (a download prints nothing while it runs)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logf = os.path.join(data_dir(), "autobuy-bat.log")
    with open(logf, "ab") as f:
        f.write(f"\n=== {_buy_now()} {bat} --auto ===\n".encode("utf-8", "replace"))
        f.flush()
        ofs = f.tell()
        p = subprocess.Popen(["cmd.exe", "/c", bat, "--auto"], stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.STDOUT, creationflags=flags, cwd=os.path.dirname(bat))

        def _drain():
            nonlocal ofs
            if not on_output:
                return
            try:
                with open(logf, "rb") as rf:
                    rf.seek(ofs)
                    chunk = rf.read()
                if chunk:
                    ofs += len(chunk)
                    on_output(chunk.decode("utf-8", "replace"))
            except OSError:
                pass

        deadline = time.monotonic() + 3600
        while True:
            try:
                rc = p.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                _drain()
                if on_tick:
                    try:
                        on_tick()
                    except Exception:   # noqa: BLE001 - progress sampling must never stop the run
                        pass
                if time.monotonic() > deadline:
                    p.kill()
                    rc = -1
                    break
        _drain()
    try:
        with open(logf, "rb") as f:
            f.seek(max(0, os.path.getsize(logf) - 1500))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        tail = ""
    return rc, tail


def _run_update_audiobooks(job=None, delay=None):
    """After a confirmed purchase (or on demand): wait for Audible to list it,
    run the bat, re-run a couple of times if Libation didn't see the title yet."""
    def _go():
        time.sleep(BUY_BAT_DELAY_S if delay is None else delay)
        bat = _bat_path()
        if not bat:
            add_log("auto-buy: Update Audiobooks.bat not found — set its path in Settings")
            _discord_notify("\u26a0\ufe0f Update Audiobooks.bat not found — set its path in Settings.")
            return
        asin = job.get("audio_asin") if job else None
        prog = _BatProgress()
        editor = _LiveEditor(_discord_live_start(prog.render()))

        def _on_output(chunk):
            prog.feed(chunk)
            editor.push(prog.render())

        def _on_tick():
            # the book in flight: sample Libation's temp file for a per-book bar
            if prog.phase == "download" and prog.current_asin and not prog.finished:
                pct = _libation_download_pct(prog.current_asin)
                if pct is not None and pct != prog.book_pct:
                    prog.book_pct = pct
                    editor.push(prog.render())

        downloaded = None
        for attempt in range(1, BUY_BAT_RETRIES + 1):
            prog.attempt = attempt
            rc, tail = _run_bat_once(bat, on_output=_on_output, on_tick=_on_tick)
            # DOWNLOADED, not merely in the library: Audible's licence service
            # can fail right after a purchase (29 Sep 2026) while the book
            # already sits in Libation's list
            got = (_libation_match(asin, job.get("series"), job.get("n"),
                                   srs=_find(job.get("series_id")) if job.get("series_id") else None)
                   if job else None)
            if job and got is not None and not got["downloaded"]:
                # the picker's own word: the ONE book it took for THIS series, now in
                # Libation. Two picks for the series and it can't tell which was bought.
                ours = [pid for pid, why in prog.picked_ids.items()
                        if _series_key(why.split(" (")[0]) == _series_key(job.get("series"))]
                if len(ours) == 1:
                    m = _libation_match(ours[0])
                    if m and m["downloaded"]:
                        got = m
            downloaded = None if got is None else got["downloaded"]
            held = " (some books HELD — see the bat log)" if rc == 2 else ""
            said = {True: "yes", False: "not yet", None: "can't tell"}[downloaded]
            with _lock:
                if job:
                    _job_step(job, "bat", rc in (0, 2) and downloaded is not False,
                              f"attempt {attempt}: rc={rc}{held}; downloaded: {said}")
                save_state()
            add_log(f"Update Audiobooks ran (rc={rc}){held}" +
                    (f"; {job.get('series', '')} {job.get('n')} downloaded: {said}" if job else ""))
            if downloaded or downloaded is None or not job:
                break
            time.sleep(BUY_BAT_RETRY_GAP_S)
        prog.finished = True
        prog.rc = rc
        _own_cache["at"] = 0.0        # new files on disk: recount ownership soon
        if rc in (0, 2):
            try:
                threading.Thread(target=_abs_trigger_scan, daemon=True).start()
            except Exception:
                pass
        if job:
            if downloaded is False:
                line = "❌ bought, but Libation couldn't download it — see below"
            elif rc in (0, 2):
                line = "🗂️ filed into the library — complete ✔"
            else:
                line = "❌ download/filing failed — see autobuy-bat.log"
            _release_card_line(job.get("series_id"), job.get("n"), line, replace_last=True)
            if downloaded is False:
                head = (f"⚠️ {job.get('series', '')} — book {job.get('n')} is bought, but Libation couldn't "
                        f"download it after {BUY_BAT_RETRIES} tries (usually Audible's licence service right after "
                        f"a purchase).")
                _notify_with_button(head + " Try the download again:",
                                    [("Download again", f"rr:download:{job['id']}")],
                                    fallback=head + " Send `!download` later, or hit Liberate in Libation.")
        if editor.url:
            editor.push(prog.render(), force=True)      # the live message becomes the summary
        else:
            filed = [ln.strip() for ln in tail.splitlines() if ln.startswith(("FILED", "NEW", "ADOPT", "HOLD"))][-4:]
            _discord_notify("\U0001f4e6 Update Audiobooks finished (rc=%s).%s" % (rc, ("\n" + "\n".join(filed)) if filed else ""))
    threading.Thread(target=_go, daemon=True).start()


# ---- periodic
