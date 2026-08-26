# ---------------------------------------------------------------------------
# Gap maths
# ---------------------------------------------------------------------------

def gap_days_list(series):
    """Novel→audio gaps in CHRONOLOGICAL order (oldest first). The recency
    weighting downstream keys off list position, and hand-edited timelines can
    be stored in any row order — sort here, like novel_gap_days_list does."""
    pairs = []
    for pair in series.get("history", []):
        n = parse_date(pair.get("novel"))
        a = parse_date(pair.get("audiobook"))
        if n and a:
            pairs.append((n, (a - n).days))
    pairs.sort(key=lambda p: p[0])
    return [g for _, g in pairs]


def avg_gap_days(series):
    gaps = gap_days_list(series)
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps))


def _robust_gaps(gaps):
    """(kept_gaps, dropped_count): the gap list with wild outliers removed.

    One freak gap (an audiobook delayed a year, a pandemic-era novel) shouldn't
    wreck every future guess. With 4+ datapoints, gaps outside the Tukey fences
    (1.5×IQR beyond the quartiles) are dropped; fewer than 4 and there's no way
    to call anything an outlier, so everything is kept."""
    if not gaps:
        return [], 0
    if len(gaps) < 4:
        return list(gaps), 0
    # inclusive: with the 4-8 datapoints a series realistically has, exclusive
    # interpolation lets the extreme value drag its own fence out of reach
    q1, _, q3 = statistics.quantiles(gaps, n=4, method="inclusive")
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [g for g in gaps if lo <= g <= hi] or list(gaps)
    return kept, len(gaps) - len(kept)


def predicted_gap_days(gaps):
    """Days to project forward: recency-weighted mean of the outlier-filtered
    gaps, or None. Newest gap weighs ~2x a gap three books back (0.75^age) —
    cadences drift, and the recent books know the current publisher/narrator
    situation better than the launch-era ones."""
    kept, _ = _robust_gaps(gaps)
    if not kept:
        return None
    ws = [0.75 ** (len(kept) - 1 - i) for i in range(len(kept))]
    return round(sum(g * w for g, w in zip(kept, ws)) / sum(ws))


def gap_label(days):
    if days is None:
        return None
    d = abs(days)
    if d < 14:
        return f"{days}d"
    if d < 90:
        return f"~{round(days / 7)} wks"
    return f"~{round(days / 30.4)} mo"


def audio_gap_days_list(series):
    """PER-VOLUME spacing (days) between audiobook releases, chronological. This
    is the series' audio rhythm, which is what actually paces a backlog.

    Normalized by how many volumes separate the two books: when a publisher
    announces book 9's audio while 4-8 are still unannounced, the raw date gap
    spans six volumes, and counting it as one 'gap' would read as a dramatically
    slower cadence instead of the same pace across more books."""
    pts = [(a, _entry_n(e)) for e in series.get("timeline", [])
           for a in [parse_date(e.get("audiobook"))] if a]
    pts.sort(key=lambda p: p[0])
    out = []
    for i in range(1, len(pts)):
        (d0, n0), (d1, n1) = pts[i - 1], pts[i]
        days = (d1 - d0).days
        if days <= 0:
            continue
        steps = (n1 - n0) if (n0 is not None and n1 is not None and n1 > n0) else 1
        out.append(round(days / steps))
    return out


def _audio_cadence(series):
    """How far apart this series' audiobooks land, or None.

    Prefers the audio's own measured rhythm, but only once there are two gaps to
    average — a single gap is usually the launch-era delay (first audiobook lags,
    then the line settles), so with less than that the novel cadence is the better
    proxy for how fast the publisher moves."""
    gaps = audio_gap_days_list(series)
    if len(gaps) >= 2:
        return predicted_gap_days(gaps)
    return predicted_gap_days(novel_gap_days_list(series))


AUDIO_MIN_SEPARATION = 21     # no series ships two of its audiobooks days apart
AUDIO_LAG_TIGHT = 21          # spread under this = a dependable novel→audio lag


def _safe_add_days(d, days):
    """d + days, or None if that runs off the end of the calendar (a hand-typed
    '9999-12-31' must not 500 the whole dashboard)."""
    if d is None or days is None:
        return None
    try:
        return d + timedelta(days=days)
    except (OverflowError, OSError, ValueError):
        return None


def _audio_lag_is_reliable(series):
    """True when this series ships audio at a consistent remove from the novel
    (day-and-date, or a steady lag). Then the NOVEL date — especially a confirmed
    pre-order — is the better predictor and the queue must not override it: for a
    same-day series the 'queue' is just a noisy restatement of the novel cadence,
    and letting it win pushes the audiobook past its own novel."""
    c = _estimate_confidence(gap_days_list(series))
    return bool(c and c["n"] >= 2 and c.get("spread_days") is not None
                and c["spread_days"] <= AUDIO_LAG_TIGHT)


def _audio_schedule(series):
    """Projected audiobook dates for every book still missing one, in book order:
    [{n, date, novel, queued, after_n}, ...].

    Two things bind an audiobook's release, and the later one wins:
      production — it can't arrive sooner after its novel than the usual lag
      queue      — a series' audiobooks come out in order, spaced by their cadence
    Each projection becomes the anchor for the next, so a backlog (several novels
    out, audio still catching up) forecasts as a queue draining at the series'
    real pace instead of everything landing at once. A CONFIRMED date — including
    a pre-order for an earlier book — is simply the strongest possible anchor:
    it's where the queue currently stands, which is why an announcement makes the
    following books' estimates sharper rather than deleting them."""
    gap_model = predicted_gap_days(gap_days_list(series))
    cadence = _audio_cadence(series)
    lag_reliable = _audio_lag_is_reliable(series)
    # Numbered books only: an unnumbered row can't be identified on a card or in
    # the schedule, and sorting it to the front would let it hijack the forecast.
    entries = sorted((e for e in series.get("timeline", []) if _entry_n(e) is not None),
                     key=lambda e: (_entry_n(e), e.get("novel") or ""))
    out = []
    last_audio, last_n = None, None
    for e in entries:
        a = parse_date(e.get("audiobook"))
        if a:
            # walk the queue forward book by book, so the anchor is always an
            # EARLIER book — a later volume's announcement must never push an
            # earlier, still-missing audiobook behind it
            if last_audio is None or a > last_audio:
                last_audio, last_n = a, _entry_n(e)
            continue
        nd = parse_date(e.get("novel"))
        prod = _safe_add_days(nd, gap_model) if nd is not None else None
        queue = None
        if last_audio is not None:
            if lag_reliable:
                # trust the lag; the queue only enforces ordering here
                queue = _safe_add_days(last_audio, 1)
            else:
                step = max(cadence if cadence is not None else 0, AUDIO_MIN_SEPARATION)
                queue = _safe_add_days(last_audio, step)
        if prod is None and queue is None:
            continue
        if prod is None:
            est, queued = queue, True
        elif queue is None:
            est, queued = prod, False
        else:
            est, queued = (queue, True) if queue > prod else (prod, False)
        out.append({"n": _entry_n(e), "date": est, "novel": nd,
                    "queued": queued, "after_n": last_n})
        if est < today():
            # This book's window has passed with nothing announced, and the next
            # volume cannot possibly come out before it — so how late this one
            # ends up running is exactly how little we know about the rest. The
            # honest answer for everything behind it is "no idea", not a number
            # anchored on a date that already failed. It surfaces as overdue.
            break
        last_audio, last_n = est, _entry_n(e)
    return out


def _audio_confidence_gaps(series, fc):
    """The gap list that actually produced this forecast, so the confidence chip
    describes the model that made the call rather than a model that didn't."""
    if fc and fc.get("queued"):
        ag = audio_gap_days_list(series)
        if len(ag) >= 2:
            return ag
        return novel_gap_days_list(series)     # cadence fell back to the novel rhythm
    return gap_days_list(series)


def _audio_schedule_usable(series):
    """The projected schedule, or [] when this series shouldn't be forecast at all
    (its tracked book's audio is already announced/released, or the series has
    visibly stopped producing audiobooks)."""
    status = series.get("audiobook_status", "unknown")
    if status in ("released", "announced"):
        return []
    sched = _audio_schedule(series)
    if not sched:
        return []
    # Don't keep projecting a pattern the series itself has broken: count novels
    # whose audiobook is well overdue by the novel→audio model (novel + gap + 60d
    # grace, still nothing). When the contradictions match or outnumber the real
    # gap datapoints, that premise is dead — e.g. one simultaneous release followed
    # by three novels with no audio at all should NOT predict a same-day audiobook.
    # Only applies when the forecast actually rests on that model: a queue-paced
    # one is built from the audio's own (slower) rhythm, where a backlog of
    # audio-less novels is exactly what the model already expects.
    if not sched[0]["queued"]:
        gaps = gap_days_list(series)
        avg = predicted_gap_days(gaps)
        if avg is not None and gaps:
            t = today()
            overdue = 0
            for e in series.get("timeline", []):
                n_d = parse_date(e.get("novel"))
                if n_d and n_d < t and not e.get("audiobook") and t > n_d + timedelta(days=avg + 60):
                    overdue += 1
            if overdue >= len(gaps):
                return []
    return sched


def _audio_forecast(series):
    """The FIRST book still missing an audiobook, as the schedule dict, or None.

    First in book order, not first still-in-the-future: audiobooks come out in
    order, so the earliest undated book is genuinely the next one due. When its
    projection has already passed, that book is overdue and the card says so —
    quietly skipping to a later book would hide a late audiobook behind a
    confident-looking date months out."""
    sched = _audio_schedule_usable(series)
    return sched[0] if sched else None


def _audio_next_forecast(series):
    """The soonest projection still ahead of us, or None.

    Distinct from _audio_forecast, which reports the first missing book even when
    that book is overdue. This one answers 'what audiobook is there to look
    forward to', so one late book can't erase the series from the schedule, feed,
    calendar and digest."""
    t = today()
    for r in _audio_schedule_usable(series):
        if r["date"] >= t:
            return r
    return None


def _predicted_audio_pair(series):
    """(predicted_date, the novel date it was predicted from) or (None, None)."""
    f = _audio_forecast(series)
    return (f["date"], f["novel"]) if f else (None, None)


def predicted_audiobook_date(series):
    """Only meaningful when there is no announcement yet."""
    return _predicted_audio_pair(series)[0]


def next_release(series):
    """The next dated event for this series: (date, kind, is_estimate) or None.

    Real dates are gathered from EVERY timeline entry, not just the series-level
    headline fields — those mirror only the latest book, so an announced audiobook
    for an earlier book (status still "none") would otherwise be invisible and an
    estimate would show in its place."""
    t = today()
    real = []
    for e in series.get("timeline", []):
        for key, kind in (("novel", "novel"), ("audiobook", "audiobook")):
            d = parse_date(e.get(key))
            if d and d >= t:
                real.append((d, kind))
    status = series.get("audiobook_status", "unknown")
    ad = parse_date(series.get("audiobook_date"))
    if status in ("announced", "released") and ad and ad >= t:
        real.append((ad, "audiobook"))
    nd = parse_date(series.get("novel_date"))
    if nd and nd >= t:
        real.append((nd, "novel"))
    if real:
        d, kind = min(real)
        return d, kind, False
    pred = predicted_audiobook_date(series)
    if pred and pred >= t:
        return pred, "audiobook", True
    return None


def novel_gap_days_list(series):
    """Gaps (days) between consecutive NOVEL releases, taken from the timeline."""
    dates = []
    for e in sorted(series.get("timeline", []), key=lambda e: _entry_n(e) or 0):
        nd = parse_date(e.get("novel"))
        if nd:
            dates.append(nd)
    out = []
    for i in range(1, len(dates)):
        d = (dates[i] - dates[i - 1]).days
        if d > 0:
            out.append(d)
    return out


def avg_novel_gap_days(series):
    gaps = novel_gap_days_list(series)
    if not gaps:
        return None
    return round(sum(gaps) / len(gaps))


def predicted_novel_date(series):
    """Predict the NEXT novel, but only when the latest known novel is already out
    (if the latest novel is still upcoming, that pre-order *is* the next novel)."""
    nd = parse_date(series.get("novel_date"))
    if nd is None or nd >= today():
        return None
    avg = predicted_gap_days(novel_gap_days_list(series))
    if avg is None:
        return None
    return nd + timedelta(days=avg)


def _estimate_confidence(gaps):
    """How much to trust a gap-based projection, from the number and consistency of
    the past gaps it averages. Returns {level, n, spread_days, outliers} or None.
    'spread_days' is the typical deviation of those gaps — roughly how far off the
    guess could be. Judged on the same outlier-filtered set the prediction uses
    ('outliers' = how many wild gaps were ignored), so one freak delay doesn't
    tank the confidence of an otherwise clockwork series."""
    kept, dropped = _robust_gaps(gaps)
    n = len(kept)
    if n == 0:
        return None
    if n == 1:
        return {"level": "low", "n": 1, "spread_days": None, "outliers": dropped}
    mean = sum(kept) / n
    sd = (sum((g - mean) ** 2 for g in kept) / n) ** 0.5
    sd_i = int(round(sd))
    if n >= 3 and sd <= 10:
        level = "high"      # several releases, all landing within ~10 days of the average
    elif sd <= 21:
        level = "medium"    # within ~3 weeks
    else:
        level = "low"       # scattered enough that the date is a rough guess
    return {"level": level, "n": n, "spread_days": sd_i, "outliers": dropped}


