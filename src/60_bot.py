# ---------------------------------------------------------------------------
# Discord bot: two-way control. The webhook talks AT you; with a bot token in
# Settings the radar also LISTENS - !buy, !approve, !status etc. from any
# device with Discord. Commands run in-process against the same state and
# guardrails as the dashboard. The gateway runs in its own thread; without a
# token nothing starts.
# ---------------------------------------------------------------------------
_bot_state = {"running": False, "err": ""}
_bot_channel = {"webhook": None, "id": None}


def _bot_channel_id():
    """The channel the webhook posts into - the bot posts there too, so buttons
    land next to the rest of the radar's messages. A GET on the webhook URL
    answers with its channel id; cached per webhook."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return None
    if _bot_channel["webhook"] == wh and _bot_channel["id"]:
        return _bot_channel["id"]
    try:
        with urlopen(Request(wh, headers={"User-Agent": APP_NAME}), timeout=10) as r:
            cid = int(json.loads(r.read().decode("utf-8", "replace")).get("channel_id"))
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord bot: couldn't look up the webhook's channel ({_no_secrets(str(e))})")
        return None
    _bot_channel.update(webhook=wh, id=cid)
    return cid


async def _bot_send(client, channel_id, text, buttons=()):
    import discord
    ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    view = None
    if buttons:
        view = discord.ui.View(timeout=None)
        for label, cid in buttons:
            view.add_item(discord.ui.Button(label=label, custom_id=cid, style=discord.ButtonStyle.primary))
    await ch.send(text[:1900], view=view)


def _bot_post(text, buttons=()):
    """Post as the BOT - a webhook message can't carry a button anyone can
    click. Blocking: call from a worker thread, never from the bot's own loop.
    True when it was sent."""
    import asyncio
    client, loop = _bot_state.get("client"), _bot_state.get("loop")
    if not (_bot_state.get("running") and client and loop):
        return False
    ch_id = _bot_channel_id()
    if not ch_id:
        return False
    try:
        asyncio.run_coroutine_threadsafe(_bot_send(client, ch_id, text, buttons), loop).result(timeout=20)
        return True
    except Exception as e:      # noqa: BLE001
        add_log(f"Discord bot post failed: {_no_secrets(str(e))}")
        return False


def _notify_with_button(text, buttons, fallback):
    """The message with its buttons via the bot, or the plain fallback text via
    the webhook when the bot isn't available. Fire-and-forget."""
    def _go():
        try:
            if not _bot_post(text, buttons):
                _discord_notify(fallback)
        except Exception as e:  # noqa: BLE001
            add_log(f"Discord notify error: {_no_secrets(str(e))}")
    threading.Thread(target=_go, daemon=True).start()


def handle_bot_button(custom_id, who="discord"):
    """A button press ('rr:<action>:<arg>') -> reply text. Pure dispatch, like
    the ! commands, so it is testable without Discord."""
    parts = (custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == "rr" and parts[1] == "retry":
        with _lock:
            job = _buy_job_by_id(parts[2])
            if not job:
                return "that purchase job no longer exists."
            if job.get("status") not in BUY_RETRYABLE:
                return f"nothing to retry — {job.get('series', '?')} #{job.get('n')} is {job.get('status')} now."
            # the job record travels to the phone view: a fixed label there,
            # the Discord user only in the file log
            log.info("retry of %s pressed on Discord by %s", job.get("id"), who)
            _buy_retry(job, who="Discord")
            save_state()
        return (f"\U0001f501 checking Amazon again for {job.get('series', '?')} #{job.get('n')} — "
                "if you're signed in, it carries on by itself.")
    if len(parts) == 3 and parts[0] == "rr" and parts[1] == "download":
        with _lock:
            job = _buy_job_by_id(parts[2])
        if not _bat_path():
            return "Update Audiobooks.bat not found — set its path in Settings."
        _run_update_audiobooks(job, delay=0)
        return "\U0001f4e6 running Update Audiobooks again (tracked series only) — progress appears here."
    return "that button isn't one of mine."


async def _bot_announce_parked(client):
    """Jobs parked for a sign-in without a button yet (posted while the bot was
    offline, or before buttons existed) get one when the bot connects."""
    import asyncio
    with _lock:
        jobs = [j for j in _state.get("buy_jobs", [])
                if j.get("status") == "needs_signin" and not j.get("signin_button_at")]
    if not jobs:
        return
    ch_id = await asyncio.get_running_loop().run_in_executor(None, _bot_channel_id)
    if not ch_id:
        return
    for j in jobs:
        await _bot_send(client, ch_id,
                        f"\U0001f511 Still waiting on an Amazon sign-in to buy {j.get('series', '?')} — book {j.get('n')}. "
                        "Sign in on the PC, then hit the button.",
                        [("I've signed in — retry now", f"rr:retry:{j['id']}")])
        with _lock:
            j["signin_button_at"] = _buy_now()
    with _lock:
        save_state()


def _bot_invite_url():
    """The OAuth invite link for the bot - the application id is public and is
    simply the base64 first segment of the token. Being online (gateway) and
    being IN a server are different things; the server owner must open this
    link once. Permissions: view channels + send messages + read history."""
    tok = (get_settings().get("bot_token") or "").strip()
    if not tok:
        return ""
    try:
        seg = tok.split(".")[0]
        app_id = base64.b64decode(seg + "=" * (-len(seg) % 4)).decode("ascii")
        if not app_id.isdigit():
            return ""
        return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions=68608"
    except Exception:      # noqa: BLE001
        return ""


def _bot_match_series(frag):
    """Series whose name contains the fragment (normalised). Returns (series|None, err)."""
    f = _norm(frag or "")
    if not f:
        return None, "which series? e.g. `!buy primal hunter 15`"
    with _lock:
        hits = [x for x in _state.get("series", []) if f in _norm(x.get("name", ""))]
    if not hits:
        return None, f"no tracked series matches '{frag}'"
    if len(hits) > 1:
        exact = [x for x in hits if _norm(x.get("name", "")) == f]
        if len(exact) == 1:
            return exact[0], None
        return None, "that matches " + ", ".join(sorted(x.get("name", "?") for x in hits)[:5]) + " - be more specific"
    return hits[0], None


def _bot_find_job(frag, statuses):
    with _lock:
        jobs = [j for j in _state.get("buy_jobs", []) if j.get("status") in statuses]
        if frag:
            f = _norm(frag)
            jobs = [j for j in jobs if f in _norm(j.get("series", "")) or f == str(j.get("n"))]
    if not jobs:
        return None, ("nothing is waiting" + (f" that matches '{frag}'" if frag else ""))
    if len(jobs) > 1:
        return None, ("more than one matches: "
                      + "; ".join(f"{j.get('series','?')} #{j.get('n')} ({j.get('status')})" for j in jobs[:5]))
    return jobs[0], None


def _bot_fmt_job(j):
    price = f"${j['price']:.2f}" if isinstance(j.get("price"), (int, float)) else "price ?"
    return f"{j.get('series','?')} #{j.get('n')} \u00b7 {price} \u00b7 {j.get('status')}"


def handle_bot_command(text):
    """'!command args' -> reply string, or None when the message isn't ours.
    Pure dispatch over the same internals the dashboard uses."""
    t = (text or "").strip()
    if not t.startswith("!"):
        return None
    parts = t[1:].split(None, 1)
    cmd = (parts[0] if parts else "").lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("help", "commands"):
        return ("**Release Radar commands**\n"
                "`!status` - radar health + what's next\n"
                "`!jobs` - purchase jobs\n"
                "`!buy <series> [book#]` - price-check a book's audiobook (asks before buying)\n"
                "`!approve [series]` / `!deny [series]` - decide a waiting purchase\n"
                "`!retry [series]` - re-check a stopped purchase now (e.g. after signing back in to Amazon)\n"
                "`!download` - run Update Audiobooks now\n"
                "`!research <series>` - AI web-research on the next book\n"
                "`!holds` - books the filer couldn't place")

    if cmd == "status":
        with _lock:
            n_series = len(_state.get("series", []))
            jobs = _state.get("buy_jobs", [])
        ext_s = int((datetime.now() - _ext_last_poll).total_seconds())
        ext = ("checking in" if ext_s < 120 else f"SILENT for {ext_s // 3600}h {ext_s % 3600 // 60}m")
        up = [r for r in upcoming_of_kind("audiobook", False)[:3]]
        nxt = "\n".join(f"\u2022 {r.get('series','?')} #{r.get('book_n','?')} \u2014 {r.get('date')}" for r in up) or "\u2022 nothing scheduled"
        waiting = [j for j in jobs if j.get("status") == "awaiting_approval"]
        return (f"**Release Radar** \u00b7 {n_series} series \u00b7 extension {ext}\n"
                f"Next audiobooks:\n{nxt}\n"
                + (f"\u23f3 {len(waiting)} purchase(s) waiting for approval \u2014 `!approve`" if waiting else "No purchases waiting."))

    if cmd == "jobs":
        with _lock:
            jobs = list(reversed(_state.get("buy_jobs", [])))[:8]
        if not jobs:
            return "no purchase jobs."
        return "\n".join("\u2022 " + _bot_fmt_job(j) for j in jobs)

    if cmd == "approve":
        job, err = _bot_find_job(arg, ("awaiting_approval",))
        if err:
            return err
        with _lock:
            job["dry_run"] = False
            _buy_approve(job, who="discord")
            save_state()
        return f"\u2705 approved: {_bot_fmt_job(job)} \u2014 buying now."

    if cmd == "retry":
        job, err = _bot_find_job(arg, BUY_RETRYABLE)
        if err:
            return err
        return handle_bot_button(f"rr:retry:{job['id']}", who="!retry")

    if cmd == "deny":
        job, err = _bot_find_job(arg, ("awaiting_approval", "retry_later", "probing", "waiting_offer"))
        if err:
            return err
        with _lock:
            _buy_deny(job, who="discord")
            save_state()
        return f"\u274c denied: {job.get('series','?')} #{job.get('n')}."

    if cmd == "buy":
        m = re.match(r"^(.*?)(?:\s+(\d+))?$", arg)
        frag, n_txt = (m.group(1) or "").strip(), m.group(2)
        srs, err = _bot_match_series(frag)
        if err:
            return err
        if n_txt:
            n = int(n_txt)
        else:
            with _lock:
                ns = [e for e in srs.get("timeline", []) if _entry_n(e) is not None and e.get("audiobook")]
            if not ns:
                return f"{srs.get('name','?')}: no audiobook recorded yet - give a book number"
            n = max(_entry_n(e) for e in ns)
        ok, msg = _buy_for_book(srs["id"], n, source="Discord")
        return (f"\U0001f6d2 {srs.get('name','?')} #{n}: {msg}" if ok else f"can't: {msg}")

    if cmd == "download":
        if not _bat_path():
            return "Update Audiobooks.bat not found - set its path in Settings."
        _run_update_audiobooks(None, delay=0)
        return "\U0001f4e6 running Update Audiobooks - progress appears here."

    if cmd == "research":
        srs, err = _bot_match_series(arg)
        if err:
            return err
        if not _ai_conf():
            return "no AI key set (Settings \u2192 Integrations)."
        threading.Thread(target=_ai_research_series, args=(srs["id"],), kwargs={"manual": True}, daemon=True).start()
        return f"\U0001f50e researching {srs.get('name','?')} - the result lands on its card and in the log."

    if cmd == "holds":
        held = _load_holds()
        if not held:
            return "no held books."
        return "\n".join(f"\u2022 {h.get('book','?')} \u2014 {h.get('reason','')[:120]}" for h in held[:8]) \
               + "\nresolve them in Settings \u2192 Held books."

    return f"unknown command `!{cmd}` - try `!help`"


def _start_discord_bot():
    tok = (get_settings().get("bot_token") or "").strip()
    if not tok:
        return

    def _run():
        try:
            import asyncio
            import discord
        except ImportError:
            _bot_state["err"] = "discord.py is not installed"
            add_log("Discord bot: discord.py is not installed (pip install discord.py)")
            return
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            _bot_state.update(running=True, err="", client=client, loop=asyncio.get_running_loop())
            add_log(f"Discord bot online as {client.user}")
            try:
                await _bot_announce_parked(client)
            except Exception as e:      # noqa: BLE001
                add_log(f"Discord bot: couldn't post retry buttons ({_no_secrets(str(e))})")

        @client.event
        async def on_interaction(interaction):
            # button presses; handled here rather than by View callbacks so a
            # button still works after the app restarts
            cid = (getattr(interaction, "data", None) or {}).get("custom_id") or ""
            if not cid.startswith("rr:"):
                return
            try:
                reply = await asyncio.get_running_loop().run_in_executor(
                    None, handle_bot_button, cid, str(interaction.user))
            except Exception as e:      # noqa: BLE001
                reply = f"error: {e}"
            try:
                base = interaction.message.content if interaction.message else ""
                await interaction.response.edit_message(content=(base + f"\n→ {reply}")[:1990], view=None)
            except Exception:           # noqa: BLE001
                if not interaction.response.is_done():
                    await interaction.response.send_message(reply[:1900])

        @client.event
        async def on_message(message):
            if message.author.bot:
                return
            try:
                reply = handle_bot_command(message.content)
            except Exception as e:      # noqa: BLE001
                reply = f"error: {e}"
            if reply:
                for i in range(0, len(reply), 1900):
                    await message.channel.send(reply[i:i + 1900])

        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            client.run(tok, log_handler=None)
        except Exception as e:          # noqa: BLE001
            hint = " - enable the MESSAGE CONTENT intent in the Developer Portal (Bot page)" \
                   if "intent" in str(e).lower() else ""
            _bot_state.update(running=False, err=str(e))
            add_log(f"Discord bot stopped: {e}{hint}")

    threading.Thread(target=_run, daemon=True).start()



def _find_orphaned_bat_run(update_log, bat_log, max_age_h=2):
    """An Update Audiobooks run the app launched but whose watcher thread died
    (exe swapped mid-run, 25 Aug 2026: the 22:54 run finished at 23:01 into
    silence). Returns the byte offset of the run's header in bat_log, or None.

    In flight = the bat's own log ends with a 'started' line and no DONE/ERROR
    after it, AND the app-side bat_log has a run header newer than the last
    completed run (manual console runs never write bat_log - not ours to watch)."""
    try:
        with open(update_log, encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read().splitlines() if l.strip()]
    except OSError:
        return None
    started_at = None
    for l in lines:
        if " Update Audiobooks started" in l:
            started_at = l
        elif started_at and re.search(r"\b(DONE|ERROR)\b", l):
            started_at = None
    if not started_at:
        return None
    m = re.search(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})", started_at)
    if m:
        dd, mo, yy, hh, mi, ss = (int(x) for x in m.groups())
        try:
            if (datetime.now() - datetime(yy, mo, dd, hh, mi, ss)) > timedelta(hours=max_age_h):
                return None                    # stale: the run died long ago
        except ValueError:
            pass
    try:
        with open(bat_log, "rb") as f:
            data = f.read()
    except OSError:
        return None
    i = data.rfind(b"\n=== ")
    if i < 0:
        i = 0 if data.startswith(b"=== ") else -1
    return (i + 1 if i > 0 else i) if i >= 0 else None


def _reattach_orphaned_bat_run():
    """Called at startup: if a bat run is in flight with no watcher, adopt it -
    feed its output so far into a fresh live progress message and post the
    summary when it finishes."""
    bat = _bat_path()
    if not bat:
        return
    update_log = os.path.join(os.path.dirname(bat), "Update Audiobooks.log")
    bat_log = os.path.join(data_dir(), "autobuy-bat.log")
    ofs = _find_orphaned_bat_run(update_log, bat_log)
    if ofs is None:
        return
    add_log("adopting an Update Audiobooks run that was in flight when the app restarted")

    def _watch():
        prog = _BatProgress()
        pos = ofs
        editor = None
        deadline = time.monotonic() + 2 * 3600
        while time.monotonic() < deadline:
            try:
                with open(bat_log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                if chunk:
                    pos += len(chunk)
                    prog.feed(chunk.decode("utf-8", "replace"))
                    if editor is None:
                        editor = _LiveEditor(_discord_live_start(prog.render()))
                    editor.push(prog.render())
            except OSError:
                pass
            try:
                with open(update_log, encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-400:]
                done = None
                for l in tail.splitlines():
                    if " Update Audiobooks started" in l:
                        done = None
                    elif re.search(r"\b(DONE|ERROR)\b", l):
                        done = l
                if done:
                    prog.finished = True
                    prog.rc = 1 if "ERROR" in done else (2 if "held" in done.lower() else 0)
                    if editor is None:
                        editor = _LiveEditor(_discord_live_start(prog.render()))
                    editor.push(prog.render(), force=True)
                    return
            except OSError:
                pass
            time.sleep(5)

    threading.Thread(target=_watch, daemon=True).start()



