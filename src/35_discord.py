# ---------------------------------------------------------------------------
# Discord notifications (via a channel webhook the user pastes into settings)
# ---------------------------------------------------------------------------

KIND_COLOR = {"novel": 0x9D8CFF, "audiobook": 0xE7B14C,   # dashboard violet / amber
              "both": 0xC29BA6}                           # day-and-date blend


def _no_secrets(s):
    """Error text can echo the webhook URL (urllib: "unknown url type: '<the
    whole string>'"). It reaches the activity log, the dashboard and, via
    /api/state, the read-only tunnel - so it must never carry the token."""
    s = re.sub(r"https?://\S+", "<url>", s or "")
    s = re.sub(r"webhooks/\S+", "webhooks/<redacted>", s)
    return re.sub(r"'[^']*'", "'<redacted>'", s)[:200]      # urllib quotes the offending input verbatim


def _discord_post(webhook, text, embeds=None, fallback_text=None):
    """Blocking POST to a Discord webhook. Returns (ok, error_str). If an embed
    payload is rejected, retries once as plain text (fallback_text when given, so
    rich digests can degrade to their full text form) — a ping must never die
    because an embed was malformed."""
    def _send(payload):
        data = json.dumps(payload).encode("utf-8")
        req = Request(webhook, data=data,
                      headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
        with urlopen(req, timeout=10) as resp:
            resp.read()

    embeds = [e for e in (embeds or []) if e]
    payload = {"content": (text or "")[:1900]}
    if embeds:
        payload["embeds"] = embeds[:10]
    try:
        _send(payload)
    except HTTPError as e:
        if embeds:
            try:
                _send({"content": ((fallback_text or text) or "")[:1900]})
                with _lock:
                    _discord_fail["n"] = 0
                return True, ""
            except Exception:
                pass
        return False, f"HTTP {e.code}"
    except (ValueError, InvalidURL):
        # both echo the pasted string in their message: say nothing of it
        return False, "not a valid webhook URL — it must start with https://discord.com/api/webhooks/"
    except URLError as e:
        return False, _no_secrets(str(getattr(e, "reason", e)))
    except Exception as e:
        return False, _no_secrets(f"{type(e).__name__}: {e}")
    with _lock:
        _discord_fail["n"] = 0          # any good post, whoever sent it, ends a failure streak
    return True, ""


_discord_fail = {"n": 0}          # consecutive failed posts (any thread)
DISCORD_FAILS_TO_REPORT = 2       # one blip is weather; two in a row is a problem


def _discord_notify(text, embeds=None):
    """Fire-and-forget Discord message (no-op if no webhook configured)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return

    def _go():
        try:
            ok, err = _discord_post(wh, text, embeds)
            with _lock:
                _discord_fail["n"] = 0 if ok else _discord_fail["n"] + 1
                n = _discord_fail["n"]
            if not ok:
                add_log(f"Discord notify failed: {err}")
                if n >= DISCORD_FAILS_TO_REPORT:
                    # Discord can't be told that Discord is broken: the dashboard banner carries it
                    _problem("discord", what=f"Discord messages are failing ({err}).",
                             detail="Check the webhook in Settings → Discord — was it rotated or deleted?",
                             notify=False)
            else:
                _problem_clear("discord", what="Discord messages are going through again.")
        except Exception as e:                  # noqa: BLE001 - a state write can fail; never die silently
            add_log(f"Discord notify error: {e}")

    threading.Thread(target=_go, daemon=True).start()


def _book_embed(series, n, cover, kind="audiobook"):
    """Small embed that puts the book's cover art beside a ping (None if no art).
    The stripe colour says what the ping is about: violet for a novel, amber
    for an audiobook - the dashboard's own colours."""
    if not cover:
        return None
    title = f"{series} — Book {n}" if n is not None else (series or "")
    return {"title": title[:256], "thumbnail": {"url": cover}, "color": KIND_COLOR.get(kind, KIND_COLOR["audiobook"])}


# ---- alerts that clean up after themselves ---------------------------------
# A warning that later clears ("extension silent" -> "back", a problem -> fixed)
# used to leave two messages behind for good. The warning is now posted as an
# editable message; the all-clear EDITS it, and an hour later it is deleted, so
# a transient outage leaves no clutter. Persisted, so a restart still tidies up.
ALERT_TIDY_S = 3600
_alert_inflight = {}          # key -> Event, set once the post has stored its message URL


def _alert_post(key, text):
    """Post an editable alert under a key (several per key: daily reminders).
    Falls back to a plain message when there is nothing to edit later."""
    if not (get_settings().get("discord_webhook") or "").strip():
        _discord_notify(text)              # nothing to edit later; stays a plain (no-op) message
        return
    ev = threading.Event()
    _alert_inflight[key] = ev

    def _go():
        try:
            url = _discord_live_start(text)
            if url:
                with _lock:
                    _state.setdefault("alerts", {}).setdefault(key, []).append(url)
                    save_state()
            else:
                _discord_notify(text)
        except Exception as e:  # noqa: BLE001
            add_log(f"Discord alert error: {e}")
        finally:
            ev.set()
            _alert_inflight.pop(key, None)

    threading.Thread(target=_go, daemon=True).start()


def _alert_resolve(key, text, fallback=True):
    """Turn the alert into its all-clear and schedule its removal. With nothing
    to edit (older build, no webhook) the all-clear is a plain message, unless
    fallback is False (a silent clear: the warning still goes away)."""
    ev = _alert_inflight.get(key)
    if ev:
        ev.wait(timeout=8)                 # the alert may have been raised seconds ago
    with _lock:
        urls = (_state.get("alerts") or {}).pop(key, None) or []
        if urls:
            due = (datetime.now() + timedelta(seconds=ALERT_TIDY_S)).isoformat(timespec="seconds")
            _state.setdefault("discord_tidy", []).extend({"url": u, "at": due} for u in urls)
        save_state()
    if not urls:
        if fallback:
            _discord_notify(text)
        return False
    threading.Thread(target=_discord_live_edit, args=(urls[-1], text), daemon=True).start()
    return True


def _discord_delete(edit_url):
    """Delete a webhook message; 'already gone' counts as done."""
    try:
        with urlopen(Request(edit_url, method="DELETE", headers={"User-Agent": APP_NAME}), timeout=10) as r:
            r.read()
        return True
    except HTTPError as e:
        return e.code == 404
    except Exception:           # noqa: BLE001
        return False


def _discord_tidy_tick():
    """Delete resolved alerts whose hour is up; a failed delete is retried on
    later ticks, a few times."""
    now = datetime.now()
    with _lock:
        items = list(_state.get("discord_tidy") or [])
    if not items:
        return
    keep, changed = [], False
    for it in items:
        try:
            due = datetime.fromisoformat(it.get("at") or "")
        except ValueError:
            changed = True
            continue
        if due > now:
            keep.append(it)
            continue
        changed = True
        it["tries"] = int(it.get("tries") or 0) + 1
        if not _discord_delete(it["url"]) and it["tries"] < 3:
            keep.append(it)
    if changed:
        with _lock:
            _state["discord_tidy"] = keep
            save_state()


def _discord_ts(iso_str, style="D"):
    """Discord timestamp tag <t:unix:style> — renders in each viewer's own timezone.
    Styles: D = date, R = relative ("in 9 days"). Anchored at local noon so nearby
    timezones don't roll to the wrong day."""
    d = parse_date(iso_str)
    if not d:
        return iso_str or "?"
    ts = int(datetime(d.year, d.month, d.day, 12, 0).timestamp())
    return f"<t:{ts}:{style}>"


def _new_date_messages(s, n, old_novel, new_novel, old_audio, new_audio):
    """[(line, kind)] for dates that were just added or changed (today or later
    only, so backfilling a series' old catalogue doesn't spam)."""
    if not get_settings().get("discord_on_new", True):
        return []
    name = s.get("name", "")
    icon = {"novel": "📖", "audiobook": "🎧"}
    msgs = []
    for kind, old, new in (("novel", old_novel, new_novel), ("audiobook", old_audio, new_audio)):
        if not new or new == old:
            continue
        d = parse_date(new)
        if not d or d < today():
            continue
        when = f"**{_discord_ts(new, 'D')}** ({_discord_ts(new, 'R')})"
        if old:
            msgs.append((_fmt_line("date_changed", icon=icon[kind], series=name, n=n,
                                   kind=kind, when=when, was=_discord_ts(old, 'D')), kind))
        else:
            msgs.append((_fmt_line("new_date", icon=icon[kind], series=name, n=n,
                                   kind=kind, when=when), kind))
    return msgs


def _release_live_message(name, n, kind):
    icon = {"novel": "📖", "audiobook": "🎧"}.get(kind, "📚")
    return _fmt_line("out_now", icon=icon, series=name, n=n, kind=kind)

# Release pings are driven by the page itself: the extension re-reads a releasing
# novel hourly on its release day, and the moment the store page flips from
# pre-order to buyable, the ingest above sends "out now". No clock-based guessing.


