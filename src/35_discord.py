# ---------------------------------------------------------------------------
# Discord notifications (via a channel webhook the user pastes into settings)
# ---------------------------------------------------------------------------

KIND_COLOR = {"novel": 0x9D8CFF, "audiobook": 0xE7B14C,   # dashboard violet / amber
              "both": 0xC29BA6}                           # day-and-date blend


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
        return True, ""
    except HTTPError as e:
        if embeds:
            try:
                _send({"content": ((fallback_text or text) or "")[:1900]})
                return True, ""
            except Exception:
                pass
        return False, f"HTTP {e.code}"
    except URLError as e:
        return False, str(getattr(e, "reason", e))
    except Exception as e:
        return False, str(e)


def _discord_notify(text, embeds=None):
    """Fire-and-forget Discord message (no-op if no webhook configured)."""
    wh = (get_settings().get("discord_webhook") or "").strip()
    if not wh:
        return

    def _go():
        ok, err = _discord_post(wh, text, embeds)
        if not ok:
            add_log(f"Discord notify failed: {err}")

    threading.Thread(target=_go, daemon=True).start()


def _book_embed(series, n, cover):
    """Small embed that puts the book's cover art beside a ping (None if no art)."""
    if not cover:
        return None
    title = f"{series} — Book {n}" if n is not None else (series or "")
    return {"title": title[:256], "thumbnail": {"url": cover}, "color": KIND_COLOR["audiobook"]}


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
    """Discord lines for dates that were just added or changed (today or later only,
    so backfilling a series' old catalogue doesn't spam)."""
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
            msgs.append(_fmt_line("date_changed", icon=icon[kind], series=name, n=n,
                                  kind=kind, when=when, was=_discord_ts(old, 'D')))
        else:
            msgs.append(_fmt_line("new_date", icon=icon[kind], series=name, n=n,
                                  kind=kind, when=when))
    return msgs


def _release_live_message(name, n, kind):
    icon = {"novel": "📖", "audiobook": "🎧"}.get(kind, "📚")
    return _fmt_line("out_now", icon=icon, series=name, n=n, kind=kind)

# Release pings are driven by the page itself: the extension re-reads a releasing
# novel hourly on its release day, and the moment the store page flips from
# pre-order to buyable, the ingest above sends "out now". No clock-based guessing.


