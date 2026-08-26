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
def _libation_files_dir():
    cands = [get_settings().get("buy_libation_files") or "",
             os.path.join(os.path.expanduser("~"), "Libation"),
             os.path.join(os.environ.get("APPDATA", ""), "Libation"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Libation")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "LibationContext.db")):
            return c
    return None


def _libation_owns(asin):
    """True/False from Libation's library, None when it can't be checked."""
    d = _libation_files_dir()
    if not d or not asin:
        return None
    try:
        con = sqlite3.connect(f"file:{os.path.join(d, 'LibationContext.db')}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("select 1 from Books where AudibleProductId = ? limit 1", (asin,)).fetchone()
        finally:
            con.close()
        return bool(row)
    except Exception:
        return None


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
    owned = _libation_owns(audio_asin)
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
    msg = (f"\U0001f511 Firefox is signed out of Amazon, so I can't buy {job.get('series', '')} — book {job.get('n')}. "
           f"Sign in again on the PC (any Amazon page); I'll keep checking for {BUY_PROBE_DAYS} days and carry on by myself.")
    _discord_notify(msg)
    _send_email_async(job["id"], f"Release Radar: sign in to Amazon to buy {job.get('title', '')}", msg)


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
        add_log(f"Discord live message failed: {e}")
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
        self.current = ""
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
            self.total = max(self.total, self.new)
        m = re.match(r"DownloadDecryptBook Begin:\s*\S+\s*\[[A-Z0-9]+\]\s*(.+)", l)
        if m:
            self.begun += 1
            self.total = max(self.total, self.begun)
            self.current = m.group(1).strip()
        if re.match(r"DownloadDecryptBook Completed:", l):
            self.done_dl += 1
        if re.match(r"(FILED|NEW|ADOPT|HOLD)\b", l) and not re.match(r"New:", l):
            self.filed.append(l[:120])

    def bar(self):
        if not self.total:
            return ""
        done = min(self.done_dl, self.total)
        f = int(self.BAR_W * done / self.total)
        return "\u25b0" * f + "\u25b1" * (self.BAR_W - f) + f"  {done}/{self.total}"

    def render(self):
        rows = ["\U0001f4e6 **Update Audiobooks**" + (f" (attempt {self.attempt})" if self.attempt > 1 else "")]
        if self.processed is not None:
            rows.append(f"\u2705 Scan: {self.processed} titles, {self.new or 0} new")
        elif self.phase == "starting":
            rows.append("\u23f3 starting\u2026")
        else:
            rows.append("\U0001f50e Scanning the Audible library\u2026"
                        + (f" ({self.accounts} accounts)" if self.accounts else ""))
        if self.processed is not None:
            if self.total == 0:
                rows.append("\U0001f4a4 Nothing new to download")
            else:
                downloading = self.phase == "download" and not self.finished and self.done_dl < self.total
                icon = "\u2b07\ufe0f" if downloading else "\u2705"
                cur = f" \u00b7 *{self.current}*" if downloading and self.current else ""
                rows.append(f"{icon} Download  {self.bar()}{cur}")
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


def _run_bat_once(bat, on_output=None):
    """Run Update Audiobooks.bat --auto hidden; returns (rc, tail of its output).
    on_output, when given, receives each new chunk of the bat's output while it
    runs (drives the live Discord progress message)."""
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

        for attempt in range(1, BUY_BAT_RETRIES + 1):
            prog.attempt = attempt
            rc, tail = _run_bat_once(bat, on_output=_on_output)
            owned = _libation_owns(asin) if asin else None
            held = " (some books HELD — see the bat log)" if rc == 2 else ""
            with _lock:
                if job:
                    _job_step(job, "bat", rc in (0, 2), f"attempt {attempt}: rc={rc}{held}; Libation has it: {owned}")
                save_state()
            add_log(f"Update Audiobooks ran (rc={rc}){held}; Libation has {asin or 'n/a'}: {owned}")
            if owned or owned is None or not job:
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
            _release_card_line(job.get("series_id"), job.get("n"),
                               "🗂️ filed into the library — complete ✔" if rc in (0, 2)
                               else "❌ download/filing failed — see autobuy-bat.log", replace_last=True)
        if editor.url:
            editor.push(prog.render(), force=True)      # the live message becomes the summary
        else:
            filed = [ln.strip() for ln in tail.splitlines() if ln.startswith(("FILED", "NEW", "ADOPT", "HOLD"))][-4:]
            _discord_notify("\U0001f4e6 Update Audiobooks finished (rc=%s).%s" % (rc, ("\n" + "\n".join(filed)) if filed else ""))
    threading.Thread(target=_go, daemon=True).start()


# ---- periodic
