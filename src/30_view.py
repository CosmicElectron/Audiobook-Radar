# ---------------------------------------------------------------------------
# Computed view for the UI / feed
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    "released": "Audiobook out",
    "announced": "Announced",
    "none": "No audiobook yet",
    "unknown": "Unknown",
}


def series_view(s):
    avg = avg_gap_days(s)
    pred = predicted_audiobook_date(s)
    n_avg = avg_novel_gap_days(s)
    n_pred = predicted_novel_date(s)
    audio_fc = _audio_forecast(s)
    audio_next = _audio_next_forecast(s)
    audio_conf = _estimate_confidence(_audio_confidence_gaps(s, audio_fc))
    novel_conf = _estimate_confidence(novel_gap_days_list(s))
    nxt = next_release(s)
    status = s.get("audiobook_status", "unknown")
    # Derived display status: the raw status mirrors only the LATEST book, but a
    # real still-upcoming audiobook for an earlier book is an announcement the
    # card should wear — pill + counts say "Audio pre-order" instead of "No
    # audiobook yet". The raw status is kept untouched (edit modal, ingest).
    t = today()
    # An audiobook for ANY book is "pre-order" only while it is genuinely ahead of
    # us. A date that is today-or-earlier is out — and on release day itself the
    # store's own confirmation (audio_pre == False, set when the page reads as
    # buyable) must win over the calendar, otherwise the pill still says
    # "pre-order" on a book the Audiobook column is already calling OUT NOW.
    def _pending(e):
        d = parse_date(e.get("audiobook"))
        if not d:
            return False
        if e.get("audio_pre") is False:      # confirmed buyable by the store
            return False
        if d > t:
            return True                      # strictly ahead of today
        # dated today and not yet confirmed: still a pre-order until the store
        # says otherwise (US-midnight releases land mid-afternoon here)
        return d == t and e.get("audio_pre") is True
    has_future_audio = any(_pending(e) for e in s.get("timeline", []))
    if status in ("none", "unknown") and has_future_audio:
        status_class, status_label = "announced", "Audio pre-order"
    else:
        status_class, status_label = status, STATUS_LABELS.get(status, "Unknown")
    # Narrator: the most recent book that names one speaks for the series.
    narrator = None
    for e in sorted(s.get("timeline", []), key=lambda e: _entry_n(e) or 0):
        # cleaned on the way out too, so credits captured before the extension
        # learned to strip page furniture still display properly
        cleaned = clean_narrator(e.get("narrator"))
        if cleaned:
            narrator = cleaned
    view = {
        "id": s["id"],
        "name": s.get("name", ""),
        "author": s.get("author", ""),
        "url": s.get("url", ""),
        "novel_title": s.get("novel_title", ""),
        "novel_date": s.get("novel_date"),
        "audiobook_date": s.get("audiobook_date"),
        "audiobook_status": status,
        "status_class": status_class,
        "status_label": status_label,
        "narrator": narrator,
        "cover_url": s.get("cover_url", ""),
        "history": s.get("history", []),
        "timeline": s.get("timeline", []),
        "novel_n": s.get("novel_n"),
        "store_host": s.get("store_host", ""),
        "asins": s.get("asins", {}) or {},
        "asins_novel": s.get("asins_novel", {}) or {},
        "asins_audio": s.get("asins_audio", {}) or {},
        "buy_mode": s.get("buy_mode") or "off",
        "buy_cap": s.get("buy_cap"),
        "audio_own": _ownership_for(s.get("id")),
        "listening": _abs_listening_for(s.get("name", "")),
        "intel": s.get("intel"),
        "edited": bool(s.get("edited")),
        "notes": s.get("notes", ""),
        "last_fetched": s.get("last_fetched"),
        "last_change": s.get("last_change"),
        "last_change_at": s.get("last_change_at"),
        "fetch_error": s.get("fetch_error"),
        "scraped_title": s.get("scraped_title"),
        "avg_gap_days": avg,
        "avg_gap_label": gap_label(avg),
        "history_count": len(gap_days_list(s)),
        "predicted_date": iso(pred),
        "predicted_book_n": audio_fc.get("n") if audio_fc else None,
        "predicted_queued": bool(audio_fc and audio_fc.get("queued")),
        "predicted_after_n": audio_fc.get("after_n") if audio_fc else None,
        "predicted_confidence": pred and audio_conf,
        # the soonest projection still ahead — same as predicted_* unless the
        # first missing book is overdue, in which case the card shows both
        "next_est_date": iso(audio_next["date"]) if audio_next else None,
        "next_est_book_n": audio_next.get("n") if audio_next else None,
        "avg_novel_gap_label": gap_label(n_avg),
        "novel_history_count": len(novel_gap_days_list(s)),
        "predicted_novel_date": iso(n_pred),
        "predicted_novel_confidence": n_pred and novel_conf,
        # who said so, when an estimate is the author's own date (hover asterisk)
        "predicted_novel_source": predicted_novel_source(s),
        "predicted_audio_source": (audio_fc or {}).get("source"),
        "next_est_source": (audio_next or {}).get("source"),
        "next_date": None,
        "next_kind": None,
        "next_estimate": False,
        "countdown_days": None,
    }
    if nxt:
        nd, kind, est = nxt
        view["next_date"] = iso(nd)
        view["next_kind"] = kind
        view["next_estimate"] = est
        view["countdown_days"] = (nd - today()).days
    return view


def next_audiobook(series, include_estimates):
    """Next AUDIOBOOK event for a series: (date, is_estimate, note) or None.
    Announced/released audiobooks always count. Predicted audiobooks count only
    when estimates are on — for day-and-date series the prediction lands on (or
    near) the novel date, which is exactly the 'likely simultaneous' case. Plain
    novels are intentionally excluded so this list stays audiobook-focused."""
    t = today()
    real = [d for d in (parse_date(e.get("audiobook")) for e in series.get("timeline", []))
            if d and d >= t]
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    if status in ("announced", "released") and ad and ad >= t:
        real.append(ad)
    if real:
        return min(real), False, None
    if include_estimates:
        est = _audio_estimate(series)
        if est:
            return est[0], True, est[1]
    return None


def _audio_estimate(series):
    """(predicted_date, note) for the audiobook forecast, or None. Independent of
    any real upcoming audiobook — callers decide precedence (the forecast lists
    show both: real rows for dated books, this row for the next undated one)."""
    f = _audio_next_forecast(series)
    if not f:
        return None
    pred = f["date"]
    if f.get("source"):
        return pred, _intel_note(f["source"])
    if f["queued"]:
        # the audio queue, not the novel, is what's holding this one back — say so
        after = f" book {f['after_n']}'s" if f.get("after_n") is not None else " the previous"
        return pred, f"next in the audio queue after{after}"
    nd = f["novel"] or parse_date(series.get("novel_date"))
    gap = (pred - nd).days if nd else None
    if gap is None:
        note = "estimated from past releases"
    elif gap <= 3:
        note = "likely same day as the novel"
    elif gap < 21:
        note = f"~{gap} days after the novel"
    else:
        note = f"~{round(gap / 7)} weeks after the novel"
    return pred, note


def _intel_note(src):
    """The estimate's basis when it is the author's own word."""
    said = (src.get("said") or src.get("expected") or "").strip()
    return f"author: {said}" if said else "author's stated timing"


def _novel_estimate(series):
    """(predicted_date, note) for the next-novel forecast, or None."""
    pred = predicted_novel_date(series)
    if not pred or pred < today():
        return None
    src = predicted_novel_source(series)
    if src:
        return pred, _intel_note(src)
    avg = predicted_gap_days(novel_gap_days_list(series))
    if avg is None:
        note = "estimated from past releases"
    elif avg < 21:
        note = f"~{avg} days after the last book"
    elif avg < 90:
        note = f"~{round(avg / 7)} weeks after the last book"
    else:
        note = f"~{round(avg / 30.4)} months after the last book"
    return pred, note


def _book_label(s, e, kind):
    """'Chrysalis 10' style label for one timeline entry."""
    n = _entry_n(e)
    base = (e.get("title") or "").strip()
    if not base:
        base = s.get("novel_title", "") if n == s.get("novel_n") else ""
    if not base:
        base = s.get("name", "")
        return f"{base} {n}" if n is not None else base
    return base


def upcoming_of_kind(kind, include_estimates):
    """Upcoming releases of one kind ('novel' or 'audiobook'), soonest first.

    ONE ROW PER BOOK, not per series: a series can have several things pending at
    once (an announced audiobook for book 5 AND a pre-ordered novel for book 6),
    and every one of them deserves its own row. Real dates come from the timeline;
    at most one estimate per series is appended (the soonest predicted book that
    isn't already covered by a real date).
    """
    rows = []
    t = today()
    with _lock:
        for s in _state["series"]:
            seen = set()
            for e in s.get("timeline", []):
                d = parse_date(e.get(kind))
                if not d or d < t:
                    continue
                n = _entry_n(e)
                seen.add(n)
                rows.append({
                    "series_id": s["id"],
                    "series": s.get("name", ""),
                    "title": _book_label(s, e, kind),
                    "book_n": n,
                    "author": s.get("author", ""),
                    "date": iso(d),
                    "kind": kind,
                    "estimate": False,
                    "note": None,
                    "confidence": None,
                    "countdown_days": (d - t).days,
                    "cover": e.get("cover") or s.get("cover_url") or None,
                    # release-day support: is it confirmed live, and at what
                    # local hour is it expected to go live if not yet
                    "live": (e.get("audio_pre" if kind == "audiobook" else "novel_pre") is False),
                    "live_at": e.get("audio_live_at" if kind == "audiobook" else "novel_live_at"),
                    "expected_hour": expected_release_hour(s, kind)[0],
                    "expected_hour_basis": expected_release_hour(s, kind)[1],
                })
            if not include_estimates:
                continue
            est_pair = _novel_estimate(s) if kind == "novel" else _audio_estimate(s)
            if not est_pair:
                continue
            nd, note = est_pair
            if nd < t:
                continue                      # stale guess; real dates came from the timeline
            ent = None
            n = None
            fc = None
            source = predicted_novel_source(s) if kind == "novel" else None
            if kind == "novel":
                # The predicted novel doesn't exist as a timeline entry yet — it's
                # the book AFTER the latest known NOVEL (the estimate anchors on
                # novel_date, so an audio-only pre-order entry with a higher n
                # must not shift the label). Never borrow the current book's title.
                maxn = max((_entry_n(e) for e in s.get("timeline", [])
                            if _entry_n(e) is not None and e.get("novel")), default=None)
                if maxn is None:
                    maxn = s.get("novel_n")
                n = (maxn + 1) if maxn is not None else None
            else:
                # The forecast names the exact book it projected, so take the
                # number straight from the same row that produced the date.
                fc = _audio_next_forecast(s)
                n = fc.get("n") if fc else None
                source = fc.get("source") if fc else None
                ent = next((e for e in s.get("timeline", []) if _entry_n(e) == n), None) if n is not None else None
            if n is not None and n in seen:
                continue                      # this book already has a real date listed
            if ent:
                title = _book_label(s, ent, kind)
            elif kind == "novel":
                # generic label — the released latest book's exact title must not
                # appear on a row for a book that doesn't exist yet
                title = (f"{s.get('name', '')} {n}".strip() if n is not None
                         else s.get("name", ""))
            else:
                title = s.get("novel_title") or s.get("name", "")
            # same gap list the card's chip uses, so one prediction never carries
            # two different confidence badges across surfaces
            gaps = (novel_gap_days_list(s) if kind == "novel"
                    else _audio_confidence_gaps(s, fc))
            rows.append({
                "series_id": s["id"],
                "series": s.get("name", ""),
                "title": title,
                "book_n": n,
                "author": s.get("author", ""),
                "date": iso(nd),
                "kind": kind,
                "estimate": True,
                "note": note,
                # an author-stated date needs no confidence chip: the asterisk says who said it
                "confidence": None if source else _estimate_confidence(gaps),
                "source": source,
                "countdown_days": (nd - t).days,
                "cover": (ent.get("cover") if ent else None) or s.get("cover_url") or None,
            })
    rows.sort(key=lambda r: (r["date"], r["estimate"]))
    return rows


DEFAULT_RELEASE_HOUR = 14.0    # Amazon US midnight Eastern == 2pm AWST; the
                               # global fallback until a series has its own data


def _flip_hours(series_list, kind=None):
    """Local clock hours (float) at which releases were first seen buyable."""
    out = []
    for s in series_list:
        for e in s.get("timeline", []):
            for key in (("audio_live_at",) if kind == "audiobook" else
                        ("novel_live_at",) if kind == "novel" else
                        ("audio_live_at", "novel_live_at")):
                v = e.get(key)
                if not v:
                    continue
                try:
                    t = datetime.fromisoformat(v)
                    out.append(t.hour + t.minute / 60.0)
                except ValueError:
                    pass
    return out


def _circular_mean_hour(hours):
    """Average of clock hours that wraps correctly at midnight."""
    import math
    if not hours:
        return None
    x = sum(math.cos(h / 24.0 * 2 * math.pi) for h in hours)
    y = sum(math.sin(h / 24.0 * 2 * math.pi) for h in hours)
    ang = math.atan2(y, x)
    if ang < 0:
        ang += 2 * math.pi
    return round(ang / (2 * math.pi) * 24.0, 2)


def expected_release_hour(series, kind=None):
    """(hour, basis): the local hour a release is expected to go live.
    basis = 'series' (this series' own confirmed flips), 'global' (every
    series' flips), or 'default' (nothing learned yet)."""
    own = _flip_hours([series], kind)
    if own:
        return _circular_mean_hour(own), "series"
    with _lock:
        everyone = _flip_hours(_state.get("series", []), kind)
    if everyone:
        return _circular_mean_hour(everyone), "global"
    return DEFAULT_RELEASE_HOUR, "default"


def overdue_audiobooks():
    """Books whose projected audiobook window has passed with nothing announced,
    most-overdue first. These are deliberately kept OUT of the upcoming schedule —
    a date that already failed isn't a forecast — and reported on their own so a
    late audiobook is visible rather than silently distorting the queue."""
    t = today()
    rows = []
    with _lock:
        for s in _state["series"]:
            for r in _audio_schedule_usable(s):
                if r["date"] >= t:
                    break            # book order: everything after this is ahead
                ent = next((e for e in s.get("timeline", [])
                            if _entry_n(e) == r["n"]), None)
                rows.append({
                    "series_id": s["id"],
                    "series": s.get("name", ""),
                    "title": _book_label(s, ent, "audiobook") if ent else s.get("name", ""),
                    "book_n": r["n"],
                    "expected": iso(r["date"]),
                    "days_late": (t - r["date"]).days,
                    "novel_date": iso(r["novel"]) if r.get("novel") else None,
                    "cover": (ent.get("cover") if ent else None) or s.get("cover_url") or None,
                })
    # newest overdue first: the one that just slipped is the live news, while a
    # book that's been late for months is old information sitting at the bottom
    rows.sort(key=lambda r: r["days_late"])
    return rows


def _upcoming_settings():
    """(mode, include_estimates) for the active upcoming view."""
    s = get_settings()
    mode = s.get("upcoming_mode", "audiobook")
    if mode not in ("audiobook", "novel"):
        mode = "audiobook"
    est = bool(s.get("show_estimates_novel", True) if mode == "novel"
               else s.get("show_estimates", True))
    return mode, est


