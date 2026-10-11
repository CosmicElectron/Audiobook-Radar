# ---------------------------------------------------------------------------
# Daily forecast digest — your schedule, read to you by a deeply reluctant brat.
# ---------------------------------------------------------------------------

_TSUN_OPENERS = [
    "Hmph! I checked your precious book schedule again. You're WELCOME. 💢",
    "Listen up, because I'm only saying this once today: here's your dumb forecast.",
    "I can't believe I'm stuck reading release dates to you every morning. Ugh. Fine. Here:",
    "Oh look who needs ME to remember their books for them. Pathetic. Adorable— I mean pathetic.",
    "It's not like I stayed up organizing your reading calendar or anything! I was bored, okay?!",
    "Your daily forecast, delivered by yours truly, the only competent one around here. Bow.",
    "Baka! You'd forget every single release without me. Admit it. …Don't actually, it's embarrassing.",
    "Fine, FINE. Here's what's coming up. Stop looking at me like that.",
    "I graciously descend once more to manage your little book problem. Gratitude is expected.",
    "Tch. Another day, another schedule you can't keep track of yourself.",
    "You again?! Whatever. Here's the forecast. Not that I made it nice for you or anything.",
    "Daily report! Because SOMEONE has to be responsible around here, and it's obviously not you.",
    "Hmph, I suppose you've waited long enough. Behold: your releases.",
    "I had better things to do, you know! But your schedule doesn't read itself, so:",
]

_TSUN_CLOSERS = [
    "That's it. Now shoo. 😤",
    "Don't thank me. It's weird when you're grateful.",
    "Same time tomorrow. Don't be late, or I'll pretend I don't know you.",
    "Now go pretend you would've remembered all that on your own.",
    "Dismissed! And no, you can't ask follow-up questions.",
    "I expect you to actually READ these, you know. Hmph.",
    "That concludes today's charity work. You're welcome. Again.",
    "If you miss a release after all this, I'm never helping you again. (I will. But I'll be MAD.)",
    "Off you go. Some of us have important schedules to maintain.",
    "And that's everything. Not that I double-checked it for you or anything… I did. Twice.",
    "End of report! Applause is optional but recommended.",
    "Go on, get. And don't come crying to me if you forget— oh wait, you literally can't now.",
]

_TSUN_TODAY = [
    "…And YES, something's out TODAY. Try to contain yourself. I know I ca— YOU can't.",
    "Release day, release day! N-not that I'm excited for you! I just like being right!",
    "There's a TODAY in there, in case you're blind as well as forgetful. GO.",
    "Something drops today. If you don't read it immediately, what was even the point of me?!",
    "TODAY. Did you see it? The word TODAY?! Why are you still here?!",
    "A release! Today! I only reminded you like a million times. Praise me.",
]

_TSUN_SOON = [
    "Something's coming up REAL soon, so don't wander off and forget. I know how you are.",
    "Days away. DAYS. Set an alarm or something, honestly.",
    "The next one's almost here. I'm watching it so you don't have to. You're welcome.",
    "So close now! N-not that I've been counting down. That would be lame.",
    "Almost release time. If you miss it, I will be insufferable about it. More than usual.",
]

_TSUN_EMPTY = [
    "Nothing. There's NOTHING coming up. Congrats, your authors are all asleep. 💢",
    "The forecast is empty, which means you made me check for NOTHING. Unbelievable.",
    "No upcoming releases. Even I can't conjure books out of thin air, dummy.",
    "Zero releases on the horizon. Go touch grass or re-read something.",
    "Empty schedule today. Don't look at ME, I don't write the books!",
    "Nothing upcoming. Your favorite authors owe us BOTH an apology.",
]

_TSUN_EST = [
    "The ~ ones are my predictions. My predictions are excellent. Doubt them and perish.",
    "Squiggle marks mean I'm guessing. Brilliantly. As always.",
    "The estimates are educated guesses. MY education, so basically guaranteed.",
    "The ~ rows aren't announced yet — that's my genius forecasting. Trust it. Or don't. See if I care!",
    "Estimated dates are approximate! Complain to the authors, not me. I'm just gifted.",
]


_TSUN_NEWDATE = [
    "{icon} Big news, listen up! **{series}** Book {n} {kind} finally has a date: {when}. I found out first, obviously.",
    "{icon} Hmph, guess what: **{series}** Book {n} {kind} is set for {when}. You may celebrate. Quietly.",
    "{icon} NEW DATE. **{series}** Book {n} {kind}: {when}. I dropped everything to tell you. …Don't read into that.",
    "{icon} **{series}** Book {n} {kind} just got scheduled: {when}. Mark it down. I'm not reminding you. (I am.)",
    "{icon} Oh? OH? **{series}** Book {n} {kind} — {when}. You'd better look excited.",
    "{icon} Announcement! **{series}** Book {n} {kind} lands {when}. Say 'thank you'. Actually don't, it's weird.",
]

_TSUN_CHANGED = [
    "{icon} Ugh, they MOVED it. **{series}** Book {n} {kind} is now {when} (was {was}). Not my fault!",
    "{icon} Date shuffle! **{series}** Book {n} {kind}: {when} now, not {was}. Keep up.",
    "{icon} Correction, because SOMEONE'S publisher can't commit: **{series}** Book {n} {kind} → {when} (was {was}).",
    "{icon} **{series}** Book {n} {kind} slid to {when} (was {was}). I'm as annoyed as you are. More, probably.",
    "{icon} Heads up: **{series}** Book {n} {kind} changed to {when}. It used to be {was}. Authors, honestly.",
    "{icon} New date alert: **{series}** Book {n} {kind} is {when} now (was {was}). Adjust your little plans.",
]

_TSUN_BOUGHT = [
    "🛒 Fine. I bought **{series}** Book {n} for {price}. With YOUR money. You're welcome, obviously.",
    "🛒 **{series}** Book {n}: purchased for {price}. I haggled it down from full price. (The Kindle trick. My trick.)",
    "🛒 Bought **{series}** Book {n} ({price}). Try to act surprised when it shows up in your library.",
    "🛒 {price} for **{series}** Book {n}. Handled. Quietly. Like a professional. Unlike some people.",
    "🛒 **{series}** Book {n} is yours now — {price}. Yes I checked the price twice. Who do you take me for?",
]
_TSUN_ASK_BUY = [
    "💳 Oi. **{series}** Book {n} is sitting there at {price}. Want it or not? Approve: {link}",
    "💳 {price} for **{series}** Book {n}. I'm not spending your money without a nod. Well? {link}",
    "💳 **{series}** Book {n}, {price}. One tap and it's done. Or ignore me. See if I care. {link}",
]
_TSUN_FILED = [
    "🗂 Downloaded and filed. **{series}** Book {n} is on the shelf where it belongs. Everything in its place.",
    "🗂 **{series}** Book {n}: in your library, named properly, filed properly. I even lined up the folder name.",
    "🗂 Done — **{series}** Book {n} is downloaded, sorted, and shelved. Enjoy. Or whatever.",
]
_TSUN_WATCHDOG = [
    "⚠️ HEY. {what} I can't do my job like this. Fix it. ({detail})",
    "⚠️ Something's wrong: {what} I've been shouting into the void. {detail}",
    "⚠️ {what} Not to alarm you but I'm COMPLETELY BLIND right now. {detail}",
]
_TSUN_RECOVERED = [
    "✅ {what} We're back. I'll pretend that never happened if you do.",
    "✅ {what} Panic over. I had it under control the entire time. Obviously.",
]
_TSUN_OUTNOW = [
    "🎉 {icon} IT'S OUT. **{series}** Book {n} {kind} is live RIGHT NOW. Why are you still reading this?!",
    "🎉 {icon} **{series}** Book {n} {kind} just went live! Go! GO! You can thank me later. (Do thank me.)",
    "🎉 {icon} Release confirmed: **{series}** Book {n} {kind} is out now. I checked the page myself. You're welcome.",
    "🎉 {icon} **{series}** Book {n} {kind}: OUT. NOW. Drop everything. The dishes can wait.",
    "🎉 {icon} It's here, it's here! **{series}** Book {n} {kind} is buyable this very second. Move it!",
    "🎉 {icon} **{series}** Book {n} {kind} is officially out. I watched that page like a hawk for you. Praise is acceptable.",
]

_TSUN_TEST = [
    "✅ Testing, testing… yes, I can reach this channel. Obviously. I'm very competent.",
    "✅ It works. Of course it works, I set it up. You may stop poking the button now.",
    "✅ Test received! Congratulations on pressing a button. The notifications will flow.",
    "✅ Hello? HELLO? …Good, it's connected. Don't make me do that again.",
    "✅ Yes yes, the webhook works. I'll take my applause in book recommendations.",
]

_TSUN_MORE = [
    "…and {count} more. Like I'd list everything for you.",
    "…plus {count} others I'm keeping to myself. Ask nicely tomorrow.",
    "…and {count} more beyond that. My patience has limits, you know.",
    "…{count} more exist. The list stops here because I say so.",
]

_TSUN_NARRATOR = [
    "{icon} Ears up! **{series}** Book {n} has a NEW narrator: **{narrator}** (was {was}). I noticed immediately, obviously.",
    "{icon} Casting news: **{series}** Book {n} is read by **{narrator}** now, not {was}. Don't shoot the messenger. Or do, see if I care!",
    "{icon} They swapped narrators on **{series}** Book {n} — **{narrator}** takes over from {was}. Your ears, your problem.",
    "{icon} PSA: **{series}** Book {n} sounds different. New narrator **{narrator}** (was {was}). You're welcome for the warning.",
]

_LINES_DIR = os.path.join(data_dir(), "digest_lines")
_LINE_FILES = {
    "openers": "_TSUN_OPENERS", "closers": "_TSUN_CLOSERS", "today": "_TSUN_TODAY",
    "soon": "_TSUN_SOON", "empty": "_TSUN_EMPTY", "estimates": "_TSUN_EST",
    "new_date": "_TSUN_NEWDATE", "date_changed": "_TSUN_CHANGED",
    "out_now": "_TSUN_OUTNOW", "test": "_TSUN_TEST", "more": "_TSUN_MORE",
    "narrator": "_TSUN_NARRATOR",
    "bought": "_TSUN_BOUGHT", "ask_buy": "_TSUN_ASK_BUY", "filed": "_TSUN_FILED",
    "watchdog": "_TSUN_WATCHDOG", "recovered": "_TSUN_RECOVERED",
}
_LINE_PLACEHOLDERS = {
    "bought": "{series} {n} {price}",
    "ask_buy": "{series} {n} {price} {link}",
    "filed": "{series} {n}",
    "watchdog": "{what} {detail}",
    "recovered": "{what}",
    "new_date": "{icon} {series} {n} {kind} {when}",
    "date_changed": "{icon} {series} {n} {kind} {when} {was}",
    "out_now": "{icon} {series} {n} {kind}",
    "more": "{count}",
    "narrator": "{icon} {series} {n} {narrator} {was}",
}
# used verbatim if a user-edited template has a typo'd placeholder
_LINE_SAFE = {
    "bought": "🛒 Bought **{series}** — Book {n} for {price}.",
    "ask_buy": "💳 **{series}** — Book {n} at {price}. Approve or deny: {link}",
    "filed": "🗂 **{series}** — Book {n} downloaded and filed into the library.",
    "watchdog": "⚠️ {what} {detail}",
    "recovered": "✅ {what}",
    "new_date": "{icon} **{series}** — Book {n} {kind} now dated {when}",
    "date_changed": "{icon} **{series}** — Book {n} {kind} date changed to {when} (was {was})",
    "out_now": "🎉 {icon} **{series}** — Book {n} {kind} is **out now**!",
    "test": "✅ Release Radar test message — notifications are wired up.",
    "more": "…and {count} more.",
    "narrator": "{icon} **{series}** — Book {n} audiobook narrated by {narrator} (previously {was})",
}
_LINE_HEADER = ("# Release Radar — digest lines: {name}\n"
                "# One message per line. Blank lines and lines starting with # are ignored.\n"
                "# Discord markdown works (**bold**, emoji). Edits apply on the next digest —\n"
                "# no restart needed. Delete this file to get the defaults back on next run.\n")


def _ensure_line_files():
    """Write the default line files (once) so they're there to edit or replace."""
    try:
        os.makedirs(_LINES_DIR, exist_ok=True)
        for name, var in _LINE_FILES.items():
            p = os.path.join(_LINES_DIR, name + ".txt")
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(_LINE_HEADER.format(name=name))
                    if name in _LINE_PLACEHOLDERS:
                        f.write("# Available placeholders (keep the curly braces): "
                                + _LINE_PLACEHOLDERS[name] + "\n")
                    f.write("\n".join(globals()[var]) + "\n")
    except Exception as e:
        add_log(f"couldn't write digest line files: {e}")


def _fmt_line(name, **fields):
    """Random line from a pool, with {placeholders} filled. A template with a bad
    placeholder falls back to a plain factual line instead of crashing."""
    tpl = random.choice(_load_lines(name))
    try:
        return tpl.format(**fields)
    except (KeyError, IndexError, ValueError):
        add_log(f"digest_lines/{name}.txt has a bad placeholder in: {tpl[:60]!r} — used plain wording")
        return _LINE_SAFE.get(name, tpl).format(**fields)


def _load_lines(name):
    """Load one pool from its file; fall back to the built-ins if missing/empty."""
    fallback = globals()[_LINE_FILES[name]]
    try:
        p = os.path.join(_LINES_DIR, name + ".txt")
        if not os.path.exists(p):
            _ensure_line_files()
        with open(p, "r", encoding="utf-8-sig") as f:
            lines = [ln.strip() for ln in f
                     if ln.strip() and not ln.strip().startswith("#")]
        return lines or fallback
    except Exception:
        return fallback



DIGEST_ICON = {"novel": "📖", "audiobook": "🎧", "both": "📖🎧"}
DIGEST_KIND_WORD = {"novel": "novel", "audiobook": "audiobook", "both": "novel + audiobook"}


def _merged_upcoming_rows():
    """Digest rows, soonest first, with a day-and-date novel+audiobook pair for
    the SAME book folded into one kind='both' row — two lines for one release
    event read like a stutter. Only confirmed dates merge; an estimate never
    hides inside a real row (its ~ marking must stay visible)."""
    rows = upcoming_of_kind("novel", True) + upcoming_of_kind("audiobook", True)
    rows.sort(key=lambda r: (r["date"], r["kind"]))
    merged, seen = [], {}
    for r in rows:
        key = (r["series_id"], r.get("book_n"), r["date"])
        mergeable = r.get("book_n") is not None and not r["estimate"]
        prev = seen.get(key)
        if mergeable and prev is not None and prev["kind"] != r["kind"]:
            prev["kind"] = "both"
            continue
        if mergeable:
            r = dict(r)             # don't mutate the shared upcoming row
            seen[key] = r
        merged.append(r)
    # A confirmed novel row also answers the reader's next question: when's the
    # audio? Real announced date if the book has one (a same-day pair already
    # merged above), the gap model's expectation otherwise, or an honest "not
    # announced". ('real'/'est'/'none', iso_date_or_None)
    with _lock:
        by_id = {s["id"]: s for s in _state["series"]}
    for r in merged:
        if r["kind"] != "novel" or r["estimate"]:
            continue
        s = by_id.get(r["series_id"])
        if not s:
            continue
        ent = next((e for e in s.get("timeline", [])
                    if _entry_n(e) == r.get("book_n")), None)
        ad = parse_date(ent.get("audiobook")) if ent else None
        if ad:
            r["audio_follow"] = ("real", iso(ad))
        else:
            # take THIS book's row out of the same schedule the rest of the app
            # uses — recomputing it from the novel date alone would reintroduce
            # the very estimate the queue model exists to correct
            proj = next((x for x in _audio_schedule_usable(s)
                         if x["n"] == r.get("book_n")), None)
            r["audio_follow"] = ("est", iso(proj["date"])) if proj else ("none", None)
    return merged


def _audio_follow_text(r):
    """One short clause describing a novel row's audiobook outlook, or ''."""
    kind, dt = r.get("audio_follow") or (None, None)
    if kind == "real":
        return f"🎧 audiobook {_discord_ts(dt, 'D')}"
    if kind == "est":
        return f"🎧 audio expected ~ {_discord_ts(dt, 'D')}"
    if kind == "none":
        return "🎧 no audiobook announced yet"
    return ""


def _daily_digest_text():
    """The forecast message: both kinds, estimates included (marked ~), soonest first."""
    rows = _merged_upcoming_rows()
    if not rows:
        return random.choice(_load_lines("empty"))
    shown = rows[:5]
    lines = [random.choice(_load_lines("openers")), ""]
    for r in shown:
        ic = DIGEST_ICON.get(r["kind"], "📚")
        kw = DIGEST_KIND_WORD.get(r["kind"], r["kind"])
        if r["estimate"]:
            c = r.get("confidence") or {}
            spread = f" ±{c['spread_days']}d" if c.get("spread_days") is not None else ""
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            lines.append(f"{ic} ~ **{r['series']}**{bk} — {kw}, my guess: around {_discord_ts(r['date'], 'D')}{spread}")
        elif r["countdown_days"] == 0:
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            af = _audio_follow_text(r)
            lines.append(f"{ic} **{r['series']}**{bk} — {kw} **TODAY**" + (f" · {af}" if af else ""))
        else:
            bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
            af = _audio_follow_text(r)
            lines.append(f"{ic} **{r['series']}**{bk} — {kw} {_discord_ts(r['date'], 'R')} ({_discord_ts(r['date'], 'D')})"
                         + (f" · {af}" if af else ""))
    if len(rows) > len(shown):
        lines.append(_fmt_line("more", count=len(rows) - len(shown)))
    lines.append("")
    if any(r["countdown_days"] == 0 for r in shown if not r["estimate"]):
        lines.append(random.choice(_load_lines("today")))
    elif any(0 < r["countdown_days"] <= 3 for r in shown if not r["estimate"]):
        lines.append(random.choice(_load_lines("soon")))
    if any(r["estimate"] for r in shown):
        lines.append(random.choice(_load_lines("estimates")))
    lines.append(random.choice(_load_lines("closers")))
    return "\n".join(lines)


def _daily_digest_payload():
    """(content, embeds) for the rich digest: tsundere opener as the message text,
    each release as an embed with its cover art, closing remarks in a final embed.
    The plain _daily_digest_text stays as the degrade path if Discord rejects it."""
    rows = _merged_upcoming_rows()
    if not rows:
        return random.choice(_load_lines("empty")), []
    shown = rows[:5]
    embeds = []
    for r in shown:
        ic = DIGEST_ICON.get(r["kind"], "📚")
        bk = f" #{r['book_n']}" if r.get("book_n") is not None else ""
        title = f"{'~ ' if r['estimate'] else ''}{ic} {r['series']}{bk} — {DIGEST_KIND_WORD.get(r['kind'], r['kind'])}"
        if r["estimate"]:
            c = r.get("confidence") or {}
            spread = f" ±{c['spread_days']}d" if c.get("spread_days") is not None else ""
            desc = f"my guess: around {_discord_ts(r['date'], 'D')}{spread}"
            if r.get("note"):
                desc += f"\n{r['note']}"
        elif r["countdown_days"] == 0:
            desc = "**TODAY**"
        else:
            desc = f"{_discord_ts(r['date'], 'R')} ({_discord_ts(r['date'], 'D')})"
        af = _audio_follow_text(r)
        if af:
            desc += f"\n{af}"
        e = {"title": title[:256], "description": desc,
             "color": KIND_COLOR.get(r["kind"], KIND_COLOR["audiobook"])}
        if r.get("cover"):
            e["thumbnail"] = {"url": r["cover"]}
        embeds.append(e)
    tail = []
    if len(rows) > len(shown):
        tail.append(_fmt_line("more", count=len(rows) - len(shown)))
    if any(r["countdown_days"] == 0 for r in shown if not r["estimate"]):
        tail.append(random.choice(_load_lines("today")))
    elif any(0 < r["countdown_days"] <= 3 for r in shown if not r["estimate"]):
        tail.append(random.choice(_load_lines("soon")))
    if any(r["estimate"] for r in shown):
        tail.append(random.choice(_load_lines("estimates")))
    tail.append(random.choice(_load_lines("closers")))
    embeds.append({"description": "\n".join(tail), "color": 0x332C47})
    return random.choice(_load_lines("openers")), embeds


def _send_digest(webhook):
    """Send the rich digest, degrading to the full plain-text version if Discord
    rejects the embeds. Returns (ok, error_str)."""
    content, embeds = _daily_digest_payload()
    return _discord_post(webhook, content, embeds, fallback_text=_daily_digest_text())


PLACEHOLDER_SAT = 9.5     # mean px saturation below this = "cover to be revealed"


def _cover_bytes_look_placeholder(data):
    """True when the image looks like a publisher's 'cover to be revealed' card.
    Those are near-greyscale; real covers have color. Measured on the live
    library (5 placeholders vs 15 real covers): placeholders' mean saturation
    topped out at 7.3, the most muted real cover sat at 12.5 — the 9.5 line
    splits them with margin. A genuinely greyscale real cover would false-
    positive, which only means the card shows the previous book's art."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((64, 64))
        px = list(im.getdata())
        sat = sum(max(c) - min(c) for c in px) / len(px)
        return sat < PLACEHOLDER_SAT
    except Exception:
        return False


def _cover_is_placeholder(url):
    """Cached placeholder check for a cover URL. Fetches (outside the state
    lock) at most once per URL; on any fetch/decode failure the answer is False
    — never block a real cover on a network hiccup."""
    if not url:
        return False
    with _lock:
        cache = _state.setdefault("cover_checks", {})
        if url in cache:
            return cache[url]
    try:
        verdict = _cover_bytes_look_placeholder(fetch_bytes(url))
    except Exception:
        verdict = False
    with _lock:
        cache = _state.setdefault("cover_checks", {})
        cache[url] = verdict
        while len(cache) > 300:
            cache.pop(next(iter(cache)))
    return verdict


RELEASE_PING_GRACE_DAYS = 3   # 'out now' may fire this many days after the date
EXT_QUIET_ALERT_H = 6         # extension silent this long -> one Discord warning
UNCONFIRMED_MIN_DAYS = 3      # after the ping grace expires...
UNCONFIRMED_MAX_DAYS = 7      # ...but before it's old news: flag silent releases

# A healthy extension polls /api/ext-commands every ~20 s. If those polls stop,
# nothing reads Amazon any more and every feature silently starves (this went
# unnoticed for two days in Aug 2026). In-memory only: an app restart resets
# the clock, which is correct - the app can't know how long it was down.
_ext_last_poll = datetime.now()
_ext_alerted = False


def _ext_seen():
    global _ext_last_poll, _ext_alerted
    _ext_last_poll = datetime.now()
    if _ext_alerted:
        _ext_alerted = False
        add_log("extension is back")
        # edits the warning into the all-clear; gone an hour later
        _alert_resolve("ext_silent", _fmt_line("recovered", what="The browser extension is checking in again."))


def _maybe_report_unconfirmed_releases():
    """The 'out now' ping depends on the extension reading the page as buyable
    around release day. When that read never happens (detector blind spot,
    extension dead, browser closed), a release used to pass in permanent
    silence — RE: Maelstrom 2 (10 Aug 2026) was only noticed two weeks late.
    Once the grace window closes, say so ONCE per book: due, never confirmed.
    The 7-day upper bound keeps back-catalogue and long outages quiet."""
    lines = []
    with _lock:
        notified = set(_state.get("discord_notified", []))
        for s2 in _state.get("series", []):
            for e in s2.get("timeline", []):
                n = _entry_n(e)
                if n is None:
                    continue
                for dkey, pkey, kind in (("audiobook", "audio_pre", "audiobook"),
                                          ("novel", "novel_pre", "novel")):
                    dd = parse_date(e.get(dkey))
                    if not dd:
                        continue
                    age = (today() - dd).days
                    if not (UNCONFIRMED_MIN_DAYS < age <= UNCONFIRMED_MAX_DAYS):
                        continue
                    if e.get(pkey) is False:
                        continue                     # confirmed buyable: the normal path owns it
                    rel_key = f"release:{s2['id']}:{n}:{kind}:{e.get(dkey)}"
                    key = f"unconfirmed:{s2['id']}:{n}:{kind}:{e.get(dkey)}"
                    if rel_key in notified or key in notified:
                        continue
                    notified.add(key)
                    lines.append(f"**{s2.get('name', '')}** book {n} {kind} — due {e.get(dkey)}, "
                                 f"never seen buyable")
        if lines:
            _state["discord_notified"] = list(notified)[-500:]
            save_state()
    if lines and get_settings().get("discord_on_release", True):
        _discord_notify("🔎 Due but never confirmed live — worth checking the store page"
                        " (the page may be unreadable, or the extension was down):\n" +
                        "\n".join(lines[:8]))


def _ext_watchdog():
    global _ext_alerted
    quiet = datetime.now() - _ext_last_poll
    if not _ext_alerted and quiet > timedelta(hours=EXT_QUIET_ALERT_H):
        _ext_alerted = True
        h = int(quiet.total_seconds() // 3600)
        add_log(f"extension has been silent for {h} h")
        _alert_post("ext_silent", _fmt_line("watchdog",
                                            what=f"The browser extension hasn't checked in for {h} hours — no release dates are being read.",
                                            detail="Is Floorp open, and is the Release Radar add-on enabled? Reinstalling the .xpi over the top fixes a dead add-on."))


# ---- problems: things only a person can fix ------------------------------
# Every scheduled job used to fail into the in-app activity log and nowhere
# else: the nightly AI research died for four nights in Sep 2026 (the Claude
# CLI had signed itself out) and nobody heard. A problem is raised by key,
# told to Discord ONCE, repeated daily while it persists, and announced when it
# clears. Persisted, so a restart neither re-alerts nor forgets.
PROBLEM_REMIND_H = 24


def _problem(key, what, detail="", notify=True):
    """Raise (or refresh) a problem. Returns True when it was announced."""
    what, detail = _no_secrets(what), _no_secrets(detail)      # this text travels far (tunnel included)
    now = datetime.now()
    stamp = now.isoformat(timespec="seconds")
    with _lock:
        probs = _state.setdefault("problems", {})
        p = probs.get(key)
        if p:
            p["what"], p["detail"] = what, detail
            p["count"] = int(p.get("count") or 0) + 1
            try:
                recent = now - datetime.fromisoformat(p.get("last_alert") or "") < timedelta(hours=PROBLEM_REMIND_H)
            except ValueError:
                recent = False
            if recent:
                return False
            p["last_alert"] = stamp
        else:
            probs[key] = {"what": what, "detail": detail, "count": 1, "since": stamp, "last_alert": stamp}
        save_state()
    add_log(f"PROBLEM ({key}): {what} {detail}".rstrip())
    if notify:
        _alert_post(f"problem:{key}", _fmt_line("watchdog", what=what, detail=detail))
    return True


def _problem_clear(key, what=None, notify=True):
    """Drop a problem; announces the all-clear only if it was actually open."""
    with _lock:
        p = (_state.get("problems") or {}).pop(key, None)
        if p:
            save_state()
    if not p:
        return False
    add_log(f"resolved ({key}): {what or p.get('what', '')}")
    # the warning becomes the all-clear and disappears an hour later; a silent
    # clear (notify=False) still takes the stale warning down, just posts nothing new
    _alert_resolve(f"problem:{key}", _fmt_line("recovered", what=what or (p.get("what", "") + " That's fixed now.")),
                   fallback=notify)
    return True


def _problems_list():
    with _lock:
        probs = _state.get("problems") or {}
        return [dict(v, key=k) for k, v in sorted(probs.items(), key=lambda kv: kv[1].get("since", ""))]


# A polling extension that never reads a page is as blind as a dead one, and
# the poll watchdog above can't see it: Sep 2026 had three days of healthy
# 20-second polls and not one Amazon read (the daily refresh only ever ran
# when Floorp was restarted). Self-heal first — ask for a forced refresh — and
# if that changes nothing, say so.
READ_QUIET_NUDGE_H = 30       # the refresh runs daily; this much silence is abnormal
READ_QUIET_ALERT_H = 6        # a nudge that yields nothing within this -> Discord
_read_nudged_at = None


def _read_watchdog():
    global _read_nudged_at
    now = datetime.now()
    if now - _ext_last_poll > timedelta(minutes=10):
        # not polling at all: _ext_watchdog owns that story - and time spent
        # away must not count against a nudge it never received
        _read_nudged_at = None
        return
    with _lock:
        if not _state.get("series"):
            return
        # last_read is stamped by every accepted page read; last_ingest only by
        # reads that CHANGED something, which a healthy quiet library never does
        last = _state.get("last_read") or _state.get("last_ingest") or ""
        pending = any(c.get("type") == "refresh" for c in _state.get("ext_commands", []))
        open_problem = "no_reads" in (_state.get("problems") or {})
    try:
        quiet = now - datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return                                  # never ingested anything: nothing to compare against
    if quiet < timedelta(hours=READ_QUIET_NUDGE_H):
        if _read_nudged_at is not None or open_problem:
            _read_nudged_at = None
            _problem_clear("no_reads", what="The extension is reading Amazon pages again.")
        return
    h = int(quiet.total_seconds() // 3600)
    if _read_nudged_at is None or now - _read_nudged_at >= timedelta(hours=24):
        _read_nudged_at = now
        if not pending:
            _push_command(type="refresh")
        add_log(f"no page reads for {h} h although the extension is polling — asked it for a forced refresh")
        return
    if now - _read_nudged_at >= timedelta(hours=READ_QUIET_ALERT_H):
        _problem("no_reads",
                 what=f"The extension is alive but hasn't read a single Amazon page in {h} hours, and a forced refresh changed nothing.",
                 detail="Open the Release Radar add-on popup: read its log, try 'Reset if stuck', or restart Floorp. "
                        "Release days can't be detected until it reads pages again.")


COVERFILL_CAP = 20        # backfill page-reads per round; the extension paces them
COVERFILL_EVERY_HOURS = 24  # cover art is cosmetic — trickle it, don't hammer
COVERFILL_MAX_TRIES = 3   # after this many reads with no yield, the page simply
                          # doesn't carry the field — stop spending reads on it
COVERFILL_RETRY_DAYS = 14 # backoff between repeat attempts at the same page


def _maybe_queue_cover_backfill():
    """Once a day, hand the extension a bounded list of book pages to re-read so
    back-catalogue entries pick up their own cover art (and audiobooks their
    narrator) — routine crawls skip settled books, so these fields would
    otherwise stay empty forever on books tracked before per-book capture
    existed. Uses only the extension's normal logged-in page reads."""
    with _lock:
        # Don't stack: if the last list is still waiting for the extension
        # (Firefox closed, worker busy), pushing another would replay near-
        # identical forced crawls back-to-back when it returns.
        if any(c.get("type") == "coverfill" for c in _state.get("ext_commands", [])):
            return
        # Once a day. This is cosmetic backfill of old books, so it rides along
        # with the extension's own daily refresh rather than adding rounds of its
        # own — at COVERFILL_CAP a round it still drains a full library in days.
        last = _state.get("coverfill_last")
        if last:
            try:
                if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=COVERFILL_EVERY_HOURS):
                    return
            except ValueError:
                pass
        urls = []
        for s in _state["series"]:
            host = s.get("store_host")
            if not host:
                continue
            a_nov = s.get("asins_novel") or {}
            a_all = s.get("asins") or {}
            a_aud = s.get("asins_audio") or {}
            for e in s.get("timeline", []):
                n = _entry_n(e)
                if n is None:
                    continue
                key = str(n)
                if not e.get("cover"):
                    asin = a_nov.get(key) or a_all.get(key)
                    if asin:
                        urls.append(f"https://{host}/dp/{asin}")
                if e.get("audiobook") and not (e.get("narrator") or "").strip():
                    asin = a_aud.get(key)
                    if asin:
                        urls.append(f"https://{host}/dp/{asin}")
        candidates = list(dict.fromkeys(urls))
        now = datetime.now()
        tried = _state.setdefault("coverfill_tried", {})
        # forget tracking for pages that are no longer candidates (field filled,
        # or the book/series is gone) so the map stays small and honest
        for u in list(tried):
            if u not in candidates:
                del tried[u]

        def _eligible(u):
            e = tried.get(u)
            if not e:
                return True
            if e.get("n", 0) >= COVERFILL_MAX_TRIES:
                return False
            try:
                t = datetime.fromisoformat(e.get("t", ""))
            except ValueError:
                return True
            return now - t >= timedelta(days=COVERFILL_RETRY_DAYS)

        # never-tried pages first so the tail of a long list can't be starved by
        # head-of-list pages that keep yielding nothing; retries queue behind
        fresh = [u for u in candidates if u not in tried]
        retry = [u for u in candidates if u in tried and _eligible(u)]
        urls = (fresh + retry)[:COVERFILL_CAP]
        _state["coverfill_last"] = now.isoformat(timespec="seconds")
        if not urls:
            save_state()
            return
        for u in urls:
            e = tried.setdefault(u, {"n": 0})
            e["n"] = e.get("n", 0) + 1
            e["t"] = now.isoformat(timespec="seconds")
    _push_command(type="coverfill", urls=urls)
    add_log(f"Asked the extension to backfill covers/narrators for {len(urls)} book page(s)")


def _maybe_fix_placeholder_covers():
    """Once a day: a card whose cover is a 'to be revealed' placeholder falls
    back to the newest book that has real art (per-book covers arrive via the
    backfill). The revealed cover replaces it automatically later through the
    normal newest-book ingest path."""
    with _lock:
        if _state.get("coverfix_last") == today().isoformat():
            return
        _state["coverfix_last"] = today().isoformat()
        work = [(s, s.get("cover_url"),
                 [e.get("cover") for e in sorted(s.get("timeline", []),
                                                 key=lambda e: _entry_n(e) or 0, reverse=True)
                  if e.get("cover")])
                for s in _state["series"] if s.get("cover_url")]
    changed = 0
    for s, url, cands in work:
        if not _cover_is_placeholder(url):
            continue
        best = next((c for c in cands if c != url and not _cover_is_placeholder(c)), None)
        if best:
            with _lock:
                s["cover_url"] = best
            changed += 1
            add_log(f"'{s.get('name', '?')}': cover not revealed yet — showing the previous book's art")
    if changed:
        with _lock:
            save_state()


def _maybe_send_daily_digest():
    st = get_settings()
    wh = (st.get("discord_webhook") or "").strip()
    if not st.get("discord_daily") or not wh:
        return
    if datetime.now().hour < int(st.get("discord_daily_hour", 9)):
        return
    every = int(st.get("discord_digest_days", 1) or 1)
    ts = today()
    with _lock:
        last = parse_date(_state.get("discord_daily_last") or "")
        if last and (ts - last).days < every:
            return
        _state["discord_daily_last"] = ts.isoformat()    # mark first so a race can't double-send
        save_state()
    ok, err = _send_digest(wh)
    if not ok:
        add_log(f"Daily digest failed: {err}")
        _problem("digest", what=f"The forecast digest didn't send ({err}).",
                 detail="Check the webhook in Settings → Discord.")
    else:
        add_log("Sent the daily forecast digest to Discord")
        _problem_clear("digest", what="The forecast digest is sending again.")


_digest_thread_started = False


def _start_daily_digest():
    global _digest_thread_started
    if _digest_thread_started:
        return
    _digest_thread_started = True

    def _loop():
        time.sleep(30)
        while True:
            try:
                _maybe_send_daily_digest()
            except Exception as e:
                add_log(f"daily digest error: {e}")
            try:
                _maybe_queue_cover_backfill()
            except Exception as e:
                add_log(f"cover backfill error: {e}")
            try:
                _maybe_fix_placeholder_covers()
            except Exception as e:
                add_log(f"placeholder cover fix error: {e}")
            try:
                _buy_tick()
            except Exception as e:
                add_log(f"auto-buy tick error: {e}")
            try:
                _ext_watchdog()
            except Exception as e:
                add_log(f"extension watchdog error: {e}")
            try:
                _read_watchdog()
            except Exception as e:
                add_log(f"read watchdog error: {e}")
            try:
                _discord_tidy_tick()
            except Exception as e:
                add_log(f"Discord tidy error: {e}")
            try:
                _maybe_report_unconfirmed_releases()
            except Exception as e:
                add_log(f"unconfirmed-release check error: {e}")
            try:
                _refresh_ownership()
            except Exception as e:
                add_log(f"ownership scan error: {e}")
            try:
                _release_card_prune()
            except Exception:
                pass
            try:
                _abs_refresh_progress()
            except Exception as e:
                add_log(f"Audiobookshelf progress error: {e}")
            try:
                _abs_sync_status()
            except Exception as e:
                add_log(f"Audiobookshelf status write error: {e}")
            try:
                _ai_tick()
            except Exception as e:
                add_log(f"AI research tick error: {e}")
            time.sleep(300)   # check every 5 minutes

    threading.Thread(target=_loop, daemon=True).start()


def build_state_payload():
    with _lock:
        views = [series_view(s) for s in _state["series"]]
        last_check = _state.get("last_check")
        log_rows = list(_state["log"])[:20]
    mode, est = _upcoming_settings()
    up = upcoming_of_kind(mode, est)
    return {
        "app_name": APP_NAME,
        "last_check": last_check,
        "last_ingest": _state.get("last_ingest"),
        "series": views,
        "upcoming": up[:FEED_ITEM_COUNT],
        "all_upcoming": up,
        "overdue": overdue_audiobooks(),
        "upcoming_mode": mode,
        "show_estimates": get_settings().get("show_estimates", True),
        "show_estimates_novel": get_settings().get("show_estimates_novel", True),
        "log": log_rows,
        "problems": _problems_list(),
        "counts": {
            "total": len(views),
            "out": sum(1 for v in views if v["status_class"] == "released"),
            "announced": sum(1 for v in views if v["status_class"] == "announced"),
            "waiting": sum(1 for v in views if v["status_class"] in ("none", "unknown")),
        },
    }


