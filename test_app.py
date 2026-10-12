"""Tests for Release Radar's pure logic: gap maths, predictions, next-release
resolution, forecast rows, timeline cleaning, ICS/digest builders, backups.

Run:  python -m pytest test_app.py -q     (from this folder)

No test touches the real %APPDATA% data.json — everything runs on synthetic
series dicts, and the backup test is pointed at a temp directory.
"""
import json
import os
from datetime import date, datetime, timedelta

import pytest

from conftest import radar   # one loaded app module for every test file; conftest guards DATA_FILE

TODAY = date.today()


def d(days_from_today):
    return (TODAY + timedelta(days=days_from_today)).isoformat()


def series(timeline, status="none", **kw):
    """Build a series dict the way ingest would leave it (headline fields mirror
    the latest numbered book)."""
    tl = sorted(timeline, key=lambda e: (e.get("novel") or e.get("audiobook") or "", e.get("n") or 0))
    s = {"id": "t1", "name": "Test", "audiobook_status": status, "timeline": tl,
         "history": radar._history_from_timeline(tl)}
    ns = [e for e in tl if e.get("n") is not None]
    if ns:
        last = max(ns, key=lambda e: e["n"])
        s["novel_n"] = last["n"]
        s["novel_date"] = last.get("novel")
        s["audiobook_date"] = last.get("audiobook")
        ad = radar.parse_date(last.get("audiobook"))
        if status == "auto":
            s["audiobook_status"] = ("released" if (ad and ad <= TODAY)
                                     else ("announced" if ad else "none"))
    s.update(kw)
    return s


# ---------------------------------------------------------------- gap maths

def test_parse_date_formats():
    assert radar.parse_date("2026-08-25") == date(2026, 8, 25)
    assert radar.parse_date("Aug 25, 2026") == date(2026, 8, 25)
    assert radar.parse_date("25 August 2026") == date(2026, 8, 25)
    assert radar.parse_date("") is None and radar.parse_date(None) is None


def test_robust_gaps_keeps_small_sets():
    kept, dropped = radar._robust_gaps([0, 209])
    assert kept == [0, 209] and dropped == 0


def test_robust_gaps_drops_outlier():
    gaps = [30, 32, 31, 29, 578]          # one freak gap
    kept, dropped = radar._robust_gaps(gaps)
    assert 578 not in kept and dropped == 1 and len(kept) == 4


def test_predicted_gap_days_recency_weighting():
    # newest gap should pull the estimate towards itself vs the plain mean
    est = radar.predicted_gap_days([10, 10, 100])
    assert est > (10 + 10 + 100) / 3


def test_confidence_uses_filtered_set():
    c = radar._estimate_confidence([30, 32, 31, 29, 578])
    assert c["outliers"] == 1 and c["n"] == 4 and c["level"] == "high"


# ----------------------------------------------------- prediction + horizon

def make_skill_hunter():
    """The real-world shape: book1 day-and-date, book2 audio pre-order in the
    future, books 3-4 novels out with no audio."""
    return series([
        {"n": 1, "novel": d(-280), "audiobook": d(-280)},
        {"n": 2, "novel": d(-190), "audiobook": d(+20)},
        {"n": 3, "novel": d(-98),  "audiobook": None},
        {"n": 4, "novel": d(-7),   "audiobook": None},
    ])


def test_estimate_never_lands_just_after_a_confirmed_date():
    """Regression: book 2's audio confirmed at +20d used to leave book 3 estimated
    at +21d — the novel-anchored guess cleared the 'must be later' bar by one day.
    The queue model paces it by the series' cadence instead."""
    s = make_skill_hunter()
    f = radar._audio_forecast(s)
    assert f["n"] == 3 and f["queued"] and f["after_n"] == 2
    real = TODAY + timedelta(days=20)
    assert (f["date"] - real).days >= radar.AUDIO_MIN_SEPARATION
    # and it is paced by the cadence, not merely nudged past the confirmed date
    assert (f["date"] - real).days >= radar._audio_cadence(s) - 1


def test_estimate_survives_for_later_book():
    # books 1-2 out (gap 30d), book 3's audio has a real date next week,
    # book 4's novel is out with no audio -> its ~30d forecast lands after
    # the horizon and must survive
    s = series([
        {"n": 1, "novel": d(-200), "audiobook": d(-170)},
        {"n": 2, "novel": d(-100), "audiobook": d(-70)},
        {"n": 3, "novel": d(-40),  "audiobook": d(+7)},
        {"n": 4, "novel": d(-5),   "audiobook": None},
    ])
    pred, anchor = radar._predicted_audio_pair(s)
    assert pred is not None and pred > TODAY + timedelta(days=7)
    assert anchor == radar.parse_date(d(-5))


def test_estimate_queues_behind_a_confirmed_date_not_suppressed():
    # the undated book's production-only guess lands before a confirmed date for
    # another book; audiobooks release in order, so it must be pushed BEYOND that
    # date (by roughly the series' cadence), never deleted and never a day after
    s = series([
        {"n": 1, "novel": d(-200), "audiobook": d(-190)},
        {"n": 2, "novel": d(-100), "audiobook": d(-90)},
        {"n": 4, "novel": d(-60),  "audiobook": d(+90)},   # confirmed, far out
        {"n": 5, "novel": d(-30),  "audiobook": None},     # guess would be ~now
    ])
    pred, _ = radar._predicted_audio_pair(s)
    assert pred is not None
    assert (pred - (TODAY + timedelta(days=90))).days >= radar.AUDIO_MIN_SEPARATION


def _backlog_series(**kw):
    """Erratic novel→audio lag (so the queue governs) with a real audio rhythm."""
    return series([
        {"n": 1, "novel": d(-400), "audiobook": d(-370)},   # lag 30
        {"n": 2, "novel": d(-300), "audiobook": d(-240)},   # lag 60
        {"n": 3, "novel": d(-200), "audiobook": d(-80)},    # lag 120
        {"n": 4, "novel": d(-100), "audiobook": None},
        {"n": 5, "novel": d(-50),  "audiobook": None},
    ], **kw)


def test_queue_stacks_successive_books():
    """A backlog forecasts as a draining queue, not everything at once."""
    s = _backlog_series()
    assert not radar._audio_lag_is_reliable(s)          # erratic lag -> queue rules
    sched = radar._audio_schedule(s)
    assert [r["n"] for r in sched] == [4, 5]
    spacing = (sched[1]["date"] - sched[0]["date"]).days
    assert spacing >= radar.AUDIO_MIN_SEPARATION
    assert sched[0]["queued"] and sched[0]["after_n"] == 3
    assert sched[1]["queued"] and sched[1]["after_n"] == 4


def test_confirmed_novel_date_beats_the_queue_on_day_and_date_series():
    """Regression: a same-day series' audio→audio gaps are just its novel cadence,
    so letting the queue win pushed the audiobook PAST its own confirmed novel."""
    s = series([
        {"n": 1, "novel": d(-300), "audiobook": d(-300)},
        {"n": 2, "novel": d(-240), "audiobook": d(-240)},
        {"n": 3, "novel": d(-180), "audiobook": d(-180)},
        {"n": 4, "novel": d(-60),  "audiobook": d(-60)},    # one long gap: cadence > next gap
        {"n": 5, "novel": d(+30),  "audiobook": None},      # confirmed pre-order
    ])
    assert radar._audio_lag_is_reliable(s)
    f = radar._audio_forecast(s)
    assert f["n"] == 5 and not f["queued"]
    assert f["date"] == TODAY + timedelta(days=30)          # the novel's own date
    assert radar._audio_estimate(s)[1] == "likely same day as the novel"


def test_later_books_announcement_does_not_push_an_earlier_one_back():
    """The queue anchors on EARLIER books only — publishers routinely announce the
    newest volume's audio while a backlog volume is still unannounced."""
    s2 = _backlog_series()
    s2["timeline"].append({"n": 9, "novel": d(-5), "audiobook": d(+300)})
    s2["timeline"].sort(key=lambda e: e["n"])
    f = radar._audio_forecast(s2)
    assert f["n"] == 4 and f["after_n"] == 3      # anchored on book 3, not book 9
    # the announcement may refine the cadence, but book 4 must stay AHEAD of the
    # later book it precedes — never shoved a year past it
    assert f["date"] < TODAY + timedelta(days=300)


def test_audio_cadence_normalizes_across_skipped_volumes():
    """A date gap spanning several unannounced volumes is not a slower cadence."""
    consecutive = series([{"n": 1, "novel": d(-400), "audiobook": d(-400)},
                          {"n": 2, "novel": d(-300), "audiobook": d(-300)}])
    skipped = series([{"n": 1, "novel": d(-400), "audiobook": d(-400)},
                      {"n": 5, "novel": d(-300), "audiobook": d(-300)}])
    assert radar.audio_gap_days_list(consecutive) == [100]
    assert radar.audio_gap_days_list(skipped) == [25]      # 100 days over 4 volumes


def test_unnumbered_entry_cannot_hijack_the_forecast():
    s = series([
        {"n": 1, "novel": d(-300), "audiobook": d(-270)},
        {"n": 2, "novel": d(-200), "audiobook": d(-100)},
        {"n": None, "novel": d(-150), "audiobook": None},   # a side story
        {"n": 3, "novel": d(-50),  "audiobook": None},
    ])
    f = radar._audio_forecast(s)
    assert f is not None and f["n"] == 3          # never the unidentifiable row


def test_far_future_audio_date_does_not_crash_the_schedule():
    s = series([{"n": 1, "novel": d(-300), "audiobook": d(-200)},
                {"n": 2, "novel": d(-100), "audiobook": "9999-12-31"},
                {"n": 3, "novel": d(-50),  "audiobook": None}])
    radar._audio_schedule(s)                      # must not raise OverflowError
    radar.series_view(s)


def test_audio_cadence_prefers_audio_rhythm_then_novel_rhythm():
    two_audio_gaps = series([
        {"n": 1, "novel": d(-400), "audiobook": d(-400)},
        {"n": 2, "novel": d(-300), "audiobook": d(-300)},
        {"n": 3, "novel": d(-200), "audiobook": d(-200)},
    ])
    assert radar.audio_gap_days_list(two_audio_gaps) == [100, 100]
    assert radar._audio_cadence(two_audio_gaps) == 100
    # a single audio gap is usually the launch-era delay — fall back to the
    # novel cadence rather than projecting that one-off forever
    one_audio_gap = series([
        {"n": 1, "novel": d(-400), "audiobook": d(-400)},
        {"n": 2, "novel": d(-300), "audiobook": d(-10)},    # 290d outlier gap
        {"n": 3, "novel": d(-200), "audiobook": None},
    ])
    assert len(radar.audio_gap_days_list(one_audio_gap)) == 1
    assert radar._audio_cadence(one_audio_gap) == 100        # the novel rhythm


def test_overdue_book_stops_the_queue_and_is_reported_separately():
    """A book that missed its window can't be jumped: nothing behind it is
    predictable until it lands, so the queue stops and it surfaces as overdue."""
    s = series([
        {"n": 1, "novel": d(-500), "audiobook": d(-470)},
        {"n": 2, "novel": d(-400), "audiobook": d(-370)},
        {"n": 3, "novel": d(-300), "audiobook": None},      # long overdue
        {"n": 4, "novel": d(-20),  "audiobook": None},
    ])
    sched = radar._audio_schedule(s)
    assert [r["n"] for r in sched] == [3]                   # stops dead at book 3
    head = radar._audio_forecast(s)
    assert head["n"] == 3 and head["date"] < TODAY          # the card says "overdue"
    assert radar._audio_next_forecast(s) is None            # book 4 is unknowable
    assert radar._audio_estimate(s) is None
    _with_state([s])
    assert not [r for r in radar.upcoming_of_kind("audiobook", True) if r["estimate"]]
    od = radar.overdue_audiobooks()
    assert len(od) == 1
    assert od[0]["book_n"] == 3 and od[0]["series"] == "Test"
    assert od[0]["days_late"] == (TODAY - head["date"]).days > 0


def test_overdue_list_is_sorted_newest_overdue_first():
    a = series([{"n": 1, "novel": d(-500), "audiobook": d(-490)},
                {"n": 2, "novel": d(-400), "audiobook": d(-390)},
                {"n": 3, "novel": d(-300), "audiobook": None}], id="a")      # very late
    b = series([{"n": 1, "novel": d(-500), "audiobook": d(-490)},
                {"n": 2, "novel": d(-400), "audiobook": d(-390)},
                {"n": 3, "novel": d(-40),  "audiobook": None}], name="B", id="b")
    _with_state([a, b])
    od = radar.overdue_audiobooks()
    assert len(od) == 2
    # the one that just slipped leads; the long-late one sinks to the bottom
    assert od[0]["days_late"] < od[1]["days_late"]
    assert od[0]["series"] == "B"


def test_card_and_schedule_agree_on_confidence():
    """One prediction, one confidence badge — the card chip and the schedule row
    must be computed from the same gap list."""
    for s in (_backlog_series(), make_skill_hunter()):
        _with_state([s])
        v = radar.series_view(s)
        rows = [r for r in radar.upcoming_of_kind("audiobook", True) if r["estimate"]]
        if not rows or not v["predicted_confidence"]:
            continue
        assert rows[0]["confidence"] == v["predicted_confidence"]


def test_digest_audio_outlook_uses_the_queue_schedule():
    """The novel row's '🎧 audio expected ~' must quote the same schedule as the
    audiobook row, not a novel-date-only guess the queue model has superseded."""
    s = _backlog_series()
    s["timeline"].append({"n": 6, "novel": d(+15), "audiobook": None})
    s["novel_date"], s["novel_n"] = d(+15), 6
    _with_state([s])
    rows = radar._merged_upcoming_rows()
    novel_row = next(r for r in rows if r["kind"] == "novel" and r["book_n"] == 6)
    kind, dt = novel_row["audio_follow"]
    proj = next(x for x in radar._audio_schedule_usable(s) if x["n"] == 6)
    assert kind == "est" and dt == proj["date"].isoformat()


def test_announced_status_suppresses_estimate():
    s = series([{"n": 1, "novel": d(-100), "audiobook": d(-70)},
                {"n": 2, "novel": d(-10), "audiobook": d(+30)}], status="announced")
    assert radar.predicted_audiobook_date(s) is None


# ------------------------------------------------------------- next release

def test_next_release_finds_earlier_book_preorder():
    s = make_skill_hunter()
    nxt = radar.next_release(s)
    assert nxt is not None
    dt, kind, est = nxt
    assert dt == TODAY + timedelta(days=20) and kind == "audiobook" and est is False


def test_next_release_series_level_only():
    s = {"id": "x", "name": "Manual", "audiobook_status": "announced",
         "audiobook_date": d(+15), "novel_date": d(-30), "timeline": [], "history": []}
    assert radar.next_release(s) == (TODAY + timedelta(days=15), "audiobook", False)


def test_next_audiobook_prefers_real_over_estimate():
    s = make_skill_hunter()
    nxt = radar.next_audiobook(s, include_estimates=True)
    assert nxt[0] == TODAY + timedelta(days=20) and nxt[1] is False


# ------------------------------------------------------------ forecast rows

def _with_state(slist):
    radar._state = {"series": slist, "log": [], "settings": {}}


def test_upcoming_real_and_later_estimate_coexist():
    s = series([
        {"n": 1, "novel": d(-200), "audiobook": d(-170)},
        {"n": 2, "novel": d(-100), "audiobook": d(-70)},
        {"n": 3, "novel": d(-40),  "audiobook": d(+7)},
        {"n": 4, "novel": d(-5),   "audiobook": None},
    ])
    _with_state([s])
    rows = radar.upcoming_of_kind("audiobook", True)
    real = [r for r in rows if not r["estimate"]]
    est = [r for r in rows if r["estimate"]]
    assert len(real) == 1 and real[0]["book_n"] == 3
    assert len(est) == 1 and est[0]["book_n"] == 4
    assert est[0]["date"] > real[0]["date"]


def test_predicted_novel_labeled_as_next_book():
    s = series([
        {"n": 1, "novel": d(-300), "audiobook": d(-300)},
        {"n": 2, "novel": d(-200), "audiobook": d(-200)},
        {"n": 3, "novel": d(-100), "audiobook": d(-100)},
    ])
    _with_state([s])
    rows = radar.upcoming_of_kind("novel", True)
    est = [r for r in rows if r["estimate"]]
    assert len(est) == 1
    assert est[0]["book_n"] == 4
    assert est[0]["title"] == "Test 4"


def test_estimate_row_skipped_when_book_has_real_date():
    s = make_skill_hunter()
    _with_state([s])
    rows = radar.upcoming_of_kind("audiobook", True)
    ns = [r["book_n"] for r in rows if not r["estimate"]]
    assert ns == [2]
    for r in rows:
        if r["estimate"]:
            assert r["book_n"] not in ns


# ----------------------------------------------------------- derived status

def test_status_class_reflects_earlier_preorder():
    v = radar.series_view(make_skill_hunter())
    assert v["audiobook_status"] == "none"          # raw stays raw
    assert v["status_class"] == "announced"
    assert v["status_label"] == "Audio pre-order"


def test_pill_is_not_preorder_on_release_day_once_confirmed():
    """Primal Hunter 19 Aug: book 15's audio dated TODAY and read as buyable,
    tracked book 17 is a novel pre-order. The pill said 'Audio pre-order' while
    the Audiobook column said OUT NOW. Today-and-confirmed is released."""
    s = series([{"n": 15, "novel": d(-140), "audiobook": d(0), "audio_pre": False},
                {"n": 17, "novel": d(+90), "audiobook": None, "novel_pre": True}])
    v = radar.series_view(s)
    assert v["status_label"] != "Audio pre-order"
    assert v["status_class"] != "announced"
    # ...but while the store still calls it a pre-order today, it IS one
    s2 = series([{"n": 15, "novel": d(-140), "audiobook": d(0), "audio_pre": True},
                 {"n": 17, "novel": d(+90), "audiobook": None}])
    assert radar.series_view(s2)["status_label"] == "Audio pre-order"
    # and a genuinely future date is a pre-order regardless
    s3 = series([{"n": 15, "novel": d(-140), "audiobook": d(+6)},
                 {"n": 17, "novel": d(+90), "audiobook": None}])
    assert radar.series_view(s3)["status_label"] == "Audio pre-order"


def test_status_class_plain_waiting():
    s = series([{"n": 1, "novel": d(-100), "audiobook": d(-70)},
                {"n": 2, "novel": d(-10), "audiobook": None}])
    v = radar.series_view(s)
    assert v["status_class"] == "none" and v["status_label"] == "No audiobook yet"


def test_clean_narrator_strips_page_furniture():
    assert radar.clean_narrator("Christian J. Gilliland, see all") == "Christian J. Gilliland"
    assert radar.clean_narrator("Jane Doe (Narrator)") == "Jane Doe"
    assert radar.clean_narrator("A Reader, B Reader, see more") == "A Reader, B Reader"
    assert radar.clean_narrator("  Spaced   Name  ") == "Spaced Name"
    assert radar.clean_narrator("") is None and radar.clean_narrator(None) is None


def test_stored_narrator_is_cleaned_on_display():
    """Credits captured before the extension learned to strip furniture still
    render properly — no data migration needed."""
    s = series([{"n": 1, "novel": d(-100), "audiobook": d(-70),
                 "narrator": "Christian J. Gilliland, see all"}])
    assert radar.series_view(s)["narrator"] == "Christian J. Gilliland"


def test_narrator_comes_from_latest_named_book():
    s = series([{"n": 1, "novel": d(-100), "audiobook": d(-70), "narrator": "A Reader"},
                {"n": 2, "novel": d(-10), "audiobook": d(-5), "narrator": "B Voice"},
                {"n": 3, "novel": d(-1), "audiobook": None}])
    assert radar.series_view(s)["narrator"] == "B Voice"


# --------------------------------------------------------- timeline cleaning

def test_clean_timeline_normalizes_dates():
    out = radar._clean_timeline([{"n": 2, "novel": "Jan 28, 2026", "audiobook": "25 August 2026"}])
    assert out == [{"n": 2, "novel": "2026-01-28", "audiobook": "2026-08-25"}]


def test_clean_timeline_preserves_aux_keys():
    existing = [{"n": 2, "novel": "2026-01-28", "audiobook": "2026-08-25",
                 "title": "Book Two", "narrator": "B Voice", "audio_pre": True, "cover": "http://c/2.jpg"}]
    out = radar._clean_timeline([{"n": 2, "novel": "2026-01-28", "audiobook": "2026-09-01"}], existing)
    assert out[0]["title"] == "Book Two" and out[0]["narrator"] == "B Voice"
    assert out[0]["cover"] == "http://c/2.jpg" and out[0]["audio_pre"] is True
    assert out[0]["audiobook"] == "2026-09-01"     # the edit still wins


def test_gap_list_is_chronological_regardless_of_row_order():
    tl = [{"n": 2, "novel": d(-100), "audiobook": d(-70)},
          {"n": 1, "novel": d(-300), "audiobook": d(-150)},   # oldest, biggest gap
          {"n": 3, "novel": d(-30), "audiobook": d(-20)}]
    a = series(tl)
    b = series(list(reversed(tl)))
    a["history"] = radar._history_from_timeline(tl)            # deliberately unsorted
    b["history"] = radar._history_from_timeline(list(reversed(tl)))
    assert radar.gap_days_list(a) == radar.gap_days_list(b) == [150, 30, 10]


def test_clean_timeline_aux_follows_novel_date_on_renumber():
    existing = [{"n": 1, "novel": "2025-01-01", "audiobook": "2025-02-01", "title": "One"},
                {"n": 2, "novel": "2025-06-01", "audiobook": None,
                 "title": "Beta Rising", "narrator": "John Smith"}]
    # user fixes an off-by-one: old book 2 becomes book 3, real book 2 inserted
    out = radar._clean_timeline([
        {"n": 1, "novel": "2025-01-01", "audiobook": "2025-02-01"},
        {"n": 2, "novel": "2025-03-01", "audiobook": None},
        {"n": 3, "novel": "2025-06-01", "audiobook": None},
    ], existing)
    rows = {e["n"]: e for e in out}
    assert rows[3]["title"] == "Beta Rising" and rows[3]["narrator"] == "John Smith"
    assert "title" not in rows[2]          # the inserted book inherits nothing
    assert [e["n"] for e in out] == [1, 2, 3]   # chronological output


def test_novel_estimate_ignores_audio_only_higher_entry():
    s = series([
        {"n": 1, "novel": d(-900), "audiobook": d(-900)},
        {"n": 2, "novel": d(-600), "audiobook": d(-600)},
        {"n": 3, "novel": d(-300), "audiobook": d(-300)},
        {"n": 4, "novel": d(-10), "audiobook": None},
        {"n": 5, "novel": None, "audiobook": d(+40)},   # audio pre-order seen first
    ])
    s["novel_date"] = d(-10)                # ingest leaves novel_date at book 4
    s["novel_n"] = 5
    _with_state([s])
    rows = [r for r in radar.upcoming_of_kind("novel", True) if r["estimate"]]
    assert len(rows) == 1
    # the projection fills book 5's novel slot — the label must say 5, not 6
    assert rows[0]["book_n"] == 5 and rows[0]["title"] == "Test 5"


# ------------------------------------------------------------- ICS / digest

def test_ics_real_releases_only():
    s = make_skill_hunter()
    _with_state([s])
    ics = radar.build_ics()
    assert ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR")
    real_day = (TODAY + timedelta(days=20)).strftime("%Y%m%d")
    assert f"DTSTART;VALUE=DATE:{real_day}" in ics
    # the series has an active estimate (book 3), but estimates never reach the
    # calendar — a calendar entry reads as a commitment
    assert radar.predicted_audiobook_date(s) is not None
    assert ics.count("BEGIN:VEVENT") == 1
    # RFC 5545: every physical line fits in 75 octets
    for ln in ics.split("\r\n"):
        assert len(ln.encode("utf-8")) <= 75, f"unfolded line: {ln!r}"


def test_ics_fold_preserves_content():
    long = "SUMMARY:" + "🎧" * 40 + " end"
    folded = radar._ics_fold(long)
    assert all(len(x.encode("utf-8")) <= 75 for x in folded)
    assert folded[0] + "".join(x[1:] for x in folded[1:]) == long


def test_ics_uids_unique_for_same_day_same_kind():
    s = series([{"n": 1, "novel": d(-400), "audiobook": d(+14)},
                {"n": 2, "novel": d(-300), "audiobook": d(+14)}])
    _with_state([s])
    ics = radar.build_ics()
    uids = [ln for ln in ics.splitlines() if ln.startswith("UID:")]
    assert len(uids) == len(set(uids)) and len(uids) >= 2


def test_digest_merges_day_and_date_pair():
    s = series([{"n": 1, "novel": d(-300), "audiobook": d(-300)},
                {"n": 2, "novel": d(+10), "audiobook": d(+10)}])   # day-and-date pre-order
    s["audiobook_status"] = "announced"
    _with_state([s])
    rows = radar._merged_upcoming_rows()
    pair = [r for r in rows if r.get("book_n") == 2]
    assert len(pair) == 1 and pair[0]["kind"] == "both"
    text = radar._daily_digest_text()
    assert "novel + audiobook" in text
    assert text.count("**Test**") == 1
    content, embeds = radar._daily_digest_payload()
    both = [e for e in embeds if "novel + audiobook" in e.get("title", "")]
    assert len(both) == 1 and both[0]["title"].startswith("📖🎧")


def test_digest_does_not_merge_estimate_with_real():
    # real novel + same-day audio ESTIMATE must stay two rows (the ~ matters)
    s = series([{"n": 1, "novel": d(-200), "audiobook": d(-200)},
                {"n": 2, "novel": d(-100), "audiobook": d(-100)},
                {"n": 3, "novel": d(+30), "audiobook": None}])
    _with_state([s])
    rows = radar._merged_upcoming_rows()
    assert all(r["kind"] != "both" for r in rows)
    kinds = {(r["kind"], r["estimate"]) for r in rows if r["date"] == d(+30)}
    assert ("novel", False) in kinds          # the real pre-order row survives


def test_novel_rows_carry_audio_outlook():
    # book 3 novel pre-order with a real (different-day) audio date -> real note;
    # a second series with gap history but no announced audio -> estimate note
    a = series([{"n": 1, "novel": d(-200), "audiobook": d(-200)},
                {"n": 3, "novel": d(+10), "audiobook": d(+40)}])
    a["audiobook_status"] = "announced"
    b = series([{"n": 1, "novel": d(-300), "audiobook": d(-270)},
                {"n": 2, "novel": d(-100), "audiobook": d(-70)},
                {"n": 3, "novel": d(+15), "audiobook": None}], name="Other", id="t2")
    _with_state([a, b])
    rows = radar._merged_upcoming_rows()
    ra = next(r for r in rows if r["series_id"] == a["id"] and r["kind"] == "novel")
    rb = next(r for r in rows if r["series_id"] == b["id"] and r["kind"] == "novel")
    assert ra["audio_follow"][0] == "real" and ra["audio_follow"][1] == d(+40)
    assert rb["audio_follow"][0] == "est"
    assert "audiobook" in radar._audio_follow_text(ra)
    assert "expected ~" in radar._audio_follow_text(rb)


def test_digest_payload_shape():
    s = make_skill_hunter()
    s["cover_url"] = "http://c/x.jpg"
    _with_state([s])
    content, embeds = radar._daily_digest_payload()
    assert isinstance(content, str) and content
    assert embeds, "expected release embeds"
    assert any(e.get("thumbnail", {}).get("url") == "http://c/x.jpg" for e in embeds)
    assert "description" in embeds[-1]              # closing-remarks embed


def test_book_embed_requires_cover():
    assert radar._book_embed("X", 1, None) is None
    e = radar._book_embed("X", 1, "http://c/1.jpg")
    assert e["thumbnail"]["url"] == "http://c/1.jpg"


def _backfill_series():
    s = series([
        {"n": 1, "novel": d(-300), "audiobook": d(-270), "cover": "http://c/1.jpg", "narrator": "A"},
        {"n": 2, "novel": d(-200), "audiobook": d(-170)},          # missing cover + narrator
        {"n": 3, "novel": d(-100), "audiobook": None},             # missing cover, no audio
    ])
    s["store_host"] = "www.amazon.com.au"
    s["asins_novel"] = {"2": "BNOVEL0002", "3": "BNOVEL0003"}
    s["asins_audio"] = {"1": "BAUDIO0001", "2": "BAUDIO0002"}
    return s


def _wire_backfill(monkeypatch, pushed):
    monkeypatch.setattr(radar, "_push_command", lambda **c: pushed.append(c) or "1")
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)


def _age_backfill_clock(hours=None):
    from datetime import datetime as _dt
    hours = (radar.COVERFILL_EVERY_HOURS + 1) if hours is None else hours
    radar._state["coverfill_last"] = (_dt.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def test_cover_backfill_waits_a_full_day_between_rounds():
    """Cover art is cosmetic — a round per day, not several, so the extension
    isn't re-reading Amazon pages on the user's behalf all day long."""
    assert radar.COVERFILL_EVERY_HOURS >= 24


def test_cover_backfill_queue(monkeypatch):
    pushed = []
    _wire_backfill(monkeypatch, pushed)
    radar._state = {"series": [_backfill_series()], "log": [], "settings": {}}
    radar._maybe_queue_cover_backfill()
    assert len(pushed) == 1
    urls = pushed[0]["urls"]
    assert "https://www.amazon.com.au/dp/BNOVEL0002" in urls      # book 2 cover
    assert "https://www.amazon.com.au/dp/BNOVEL0003" in urls      # book 3 cover
    assert "https://www.amazon.com.au/dp/BAUDIO0002" in urls      # book 2 narrator
    assert "https://www.amazon.com.au/dp/BAUDIO0001" not in urls  # book 1 complete
    assert len(urls) == 3
    # interval guard: an immediate second call queues nothing
    radar._maybe_queue_cover_backfill()
    assert len(pushed) == 1


def test_cover_backfill_no_stacking(monkeypatch):
    pushed = []
    _wire_backfill(monkeypatch, pushed)
    radar._state = {"series": [_backfill_series()], "log": [], "settings": {},
                    "ext_commands": [{"id": "9", "type": "coverfill", "urls": ["x"]}]}
    radar._maybe_queue_cover_backfill()
    assert pushed == []          # a pending list means no new push, ever


def test_cover_backfill_tries_and_starvation(monkeypatch):
    pushed = []
    _wire_backfill(monkeypatch, pushed)
    monkeypatch.setattr(radar, "COVERFILL_CAP", 2)
    radar._state = {"series": [_backfill_series()], "log": [], "settings": {}}
    radar._maybe_queue_cover_backfill()
    first = pushed[0]["urls"]
    assert len(first) == 2
    # nothing got filled; next round must reach the third (never-tried) URL
    # instead of hammering the first two again
    _age_backfill_clock()
    radar._maybe_queue_cover_backfill()
    second = pushed[1]["urls"]
    assert len(second) == 1 and second[0] not in first
    # all three now tried once and inside the retry backoff -> nothing to queue
    _age_backfill_clock()
    radar._maybe_queue_cover_backfill()
    assert len(pushed) == 2
    # exhausted pages (max tries) never come back even after the backoff
    for u, e in radar._state["coverfill_tried"].items():
        e["n"] = radar.COVERFILL_MAX_TRIES
        e["t"] = d(-30) + "T00:00:00"
    _age_backfill_clock()
    radar._maybe_queue_cover_backfill()
    assert len(pushed) == 2
    # ...but once a field actually fills, its tracking entry is pruned
    radar._state["series"][0]["timeline"][1]["cover"] = "http://c/2.jpg"
    _age_backfill_clock()
    radar._maybe_queue_cover_backfill()
    assert "https://www.amazon.com.au/dp/BNOVEL0002" not in radar._state["coverfill_tried"]


def test_ingest_backfill_fills_but_never_overwrites(monkeypatch):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    s = series([{"n": 2, "novel": d(-200), "audiobook": d(-170), "title": "Test 2"}])
    s["last_change"] = None
    radar._state = {"series": [s], "log": [], "settings": {}}
    client = radar.app.test_client()
    body = {"series": "Test", "book": {"n": 2, "title": "Test 2", "audiobook_date": d(-100),
                                       "narrator": "New Voice", "cover": "http://c/a.jpg"},
            "_backfill": True}
    r = client.post("/api/ingest", json=body)
    assert r.get_json()["ok"]
    e = s["timeline"][0]
    assert e["audiobook"] == d(-170)          # existing date NOT overwritten
    assert e["narrator"] == "New Voice"       # aux data still collected
    assert e.get("cover") is None or e["cover"] == "http://c/a.jpg"
    # the same read WITHOUT the flag is Amazon-authoritative as before
    body.pop("_backfill")
    client.post("/api/ingest", json=body)
    assert e["audiobook"] == d(-100)


def test_ingest_noop_does_not_churn_last_change(monkeypatch):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "_discord_notify", lambda *a, **k: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    s = series([{"n": 2, "novel": d(-200), "audiobook": d(-170), "title": "Test 2"}],
               status="auto")
    s["novel_title"] = "Test 2"
    s["last_change"] = "old line"
    s["last_change_at"] = "2026-01-01T00:00:00"
    radar._state = {"series": [s], "log": [], "settings": {}}
    client = radar.app.test_client()
    body = {"series": "Test", "book": {"n": 2, "title": "Test 2",
                                       "audiobook_date": d(-170)}}   # nothing new
    client.post("/api/ingest", json=body)
    assert s["last_change"] == "old line" and s["last_change_at"] == "2026-01-01T00:00:00"
    # a real change still stamps it
    body["book"]["audiobook_date"] = d(-100)
    client.post("/api/ingest", json=body)
    assert s["last_change"] == "Updated from Amazon: book 2"


def _img_bytes(colorful):
    import io
    from PIL import Image
    import random as _r
    _r.seed(7)
    im = Image.new("RGB", (64, 96), (40, 42, 44))
    px = im.load()
    if colorful:
        for x in range(64):
            for y in range(96):
                px[x, y] = (_r.randint(0, 255), _r.randint(0, 120), _r.randint(60, 255))
    else:
        for x in range(10, 54):          # grey "text" band, like the real cards
            for y in range(40, 56):
                px[x, y] = (230, 230, 230)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def test_placeholder_detector():
    assert radar._cover_bytes_look_placeholder(_img_bytes(colorful=False)) is True
    assert radar._cover_bytes_look_placeholder(_img_bytes(colorful=True)) is False
    assert radar._cover_bytes_look_placeholder(b"not an image") is False


def test_placeholder_cover_repair(monkeypatch):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    ph = "http://c/placeholder.jpg"
    real = "http://c/book4.jpg"
    monkeypatch.setattr(radar, "_cover_is_placeholder", lambda u: u == ph)
    s = series([{"n": 4, "novel": d(-100), "audiobook": None, "cover": real},
                {"n": 5, "novel": d(+60), "audiobook": None, "cover": ph}])
    s["cover_url"] = ph
    radar._state = {"series": [s], "log": [], "settings": {}}
    radar._maybe_fix_placeholder_covers()
    assert s["cover_url"] == real
    # daily guard: no rework the same day even if state changes
    s["cover_url"] = ph
    radar._maybe_fix_placeholder_covers()
    assert s["cover_url"] == ph


def test_dashboard_script_parses():
    """The dashboard's JS is inlined in a Python string, so a stray quote breaks
    the whole page while app.py still compiles and every other test passes. Parse
    it for real (skipped when node isn't installed)."""
    import re
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    blocks = re.findall(r"<script>(.*?)</script>", radar.PAGE_HTML, re.S)
    assert blocks, "no inline script found in PAGE_HTML"
    for i, src in enumerate(blocks):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_check_{i}.js")
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            assert r.returncode == 0, f"dashboard script #{i} failed to parse:\n{r.stderr}"
        finally:
            if os.path.exists(p):
                os.remove(p)


def test_a_foreign_website_cannot_read_secrets_or_change_anything(monkeypatch):
    """A page you happen to visit must not be able to read the Discord webhook
    or wipe/kill the app. Firefox does not block private-network requests, so
    blanket `Access-Control-Allow-Origin: *` made this live."""
    # This test drives /api/ingest, which persists. Without these stubs it writes
    # its synthetic state straight over the real %APPDATA% data.json — which is
    # exactly what happened once. Never let a test touch the live data file.
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "_backup_state", lambda: None)
    monkeypatch.setattr(radar, "_discord_notify", lambda *a, **k: None)
    radar._state = {"series": [], "log": [], "ext_commands": [], "cmd_seq": 0,
                    "settings": {"discord_webhook": "https://discord.com/api/webhooks/SECRET"}}
    c = radar.app.test_client()
    evil = {"Origin": "https://evil.example"}

    r = c.get("/api/settings", headers=evil)
    assert r.headers.get("Access-Control-Allow-Origin") is None      # unreadable
    for path in ("/api/series/clear", "/api/quit", "/api/settings"):
        assert c.post(path, headers=evil, json={}).status_code == 403

    # ...while the extension, the dashboard and embeddable read-only surfaces work
    ext = {"Origin": "moz-extension://abc"}
    assert c.get("/api/ext-commands", headers=ext).headers["Access-Control-Allow-Origin"] == "moz-extension://abc"
    local = {"Origin": "http://127.0.0.1:8765"}
    assert c.post("/api/settings", headers=local, json={"discord_daily": True}).status_code == 200
    for path in ("/api/state", "/feed.xml", "/calendar.ics", "/board"):
        assert c.get(path, headers=evil).headers.get("Access-Control-Allow-Origin") == "*"
    # and a request with no Origin at all (the extension's background fetch)
    assert c.post("/api/ingest", json={"series": "S", "book": {"n": 1, "novel_date": d(+5)}}).status_code == 200


def test_tunnel_requests_are_read_only_and_never_see_the_webhook(monkeypatch):
    """The Cloudflare tunnel that serves the phone has no login in front of it.
    Found live: GET /api/settings over the hostname returned the Discord webhook
    (and Cloudflare cached it for 4 h); POST /api/quit etc. were reachable.
    Remote = Cf-* headers present, or a Host that isn't loopback."""
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    radar._state = {"series": [], "log": [], "ext_commands": [], "cmd_seq": 0,
                    "settings": {"discord_webhook": "https://discord.com/api/webhooks/SECRET"}}
    c = radar.app.test_client()
    via_cf = {"Cf-Connecting-Ip": "203.0.113.9", "Cf-Ray": "abc"}
    # reads still work but the credential is masked and nothing is cacheable
    r = c.get("/api/settings", headers=via_cf)
    assert r.status_code == 200
    j = r.get_json()
    assert j["discord_webhook"] == "" and j["discord_webhook_set"] is True and j["remote_readonly"] is True
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert "no-store" in c.get("/", headers=via_cf).headers.get("Cache-Control", "")
    assert c.get("/api/state", headers=via_cf).status_code == 200
    # every write is refused over the tunnel
    for path in ("/api/series/clear", "/api/quit", "/api/settings", "/api/ext/crawl",
                 "/api/fetch-url", "/api/discord/digest-now"):
        assert c.post(path, headers=via_cf, json={}).status_code == 403, path
    # a non-loopback Host with no Cf headers counts as remote too
    r = c.get("/api/settings", headers={"Host": "radar.example.org"})
    assert r.get_json()["discord_webhook"] == ""
    assert c.post("/api/settings", headers={"Host": "radar.example.org"}, json={}).status_code == 403
    # ...while the machine itself is unaffected
    r = c.get("/api/settings", headers={"Host": "127.0.0.1:8765"})
    assert r.get_json()["discord_webhook"].endswith("/SECRET") and r.get_json()["remote_readonly"] is False
    assert c.post("/api/settings", headers={"Host": "127.0.0.1:8765"}, json={"discord_daily": True}).status_code == 200
    assert c.post("/api/settings", headers={"Host": "localhost:8765"}, json={}).status_code == 200


def test_instance_endpoint_identifies_the_app():
    """A second launch decides whether to exit by asking /api/instance — the
    marker must be present and unmistakable."""
    radar._state = {"series": [], "log": [], "settings": {}, "ext_commands": [], "cmd_seq": 0}
    r = radar.app.test_client().get("/api/instance")
    assert r.status_code == 200
    body = r.get_json()
    assert body["app"] == "release-radar" and isinstance(body["pid"], int)


def _ingest_client(monkeypatch, series_list):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    sent = []
    monkeypatch.setattr(radar, "_discord_notify", lambda text, *a, **k: sent.append(text))
    radar._state = {"series": series_list, "log": [], "settings": {"discord_on_release": True},
                    "ext_commands": [], "cmd_seq": 0, "discord_notified": []}
    return radar.app.test_client(), sent


def test_stale_preorder_correction_is_not_a_release(monkeypatch):
    """The pre-v1.11 detector marked every released Kindle page as a pre-order.
    The first full crawl with the fixed detector flipped those stale states all
    at once and pinged 'out now' for novels released months ago (20 Aug 2026,
    ~15 bogus Discord messages). A flip only counts as a release when it happens
    around the release date — and a stale correction must not record a bogus
    release-hour flip time either."""
    s = series([{"n": 4, "novel": d(-186), "novel_pre": True}])   # released in February, stale pre=True
    c, sent = _ingest_client(monkeypatch, [s])
    r = c.post("/api/ingest", json={
        "series": "Test", "book": {"n": 4, "title": "Test 4", "novel_date": d(-186)},
        "_preorder": False, "_url": "https://www.amazon.com.au/dp/B0OLDBOOK04"})
    assert r.get_json()["ok"]
    ent = s["timeline"][0]
    assert sent == []                              # no 'out now' for a months-old book
    assert ent.get("novel_pre") is False           # ...but the state is corrected
    assert "novel_live_at" not in ent              # ...and no fake flip time is learned
    # a genuine flip on release day still pings and still records the hour
    s2 = series([{"n": 5, "novel": d(0), "novel_pre": True}])
    c2, sent2 = _ingest_client(monkeypatch, [s2])
    c2.post("/api/ingest", json={"series": "Test", "book": {"n": 5, "title": "Test 5", "novel_date": d(0)},
                                 "_preorder": False, "_url": "https://www.amazon.com.au/dp/B0NEWBOOK05"})
    assert len(sent2) == 1 and s2["timeline"][0].get("novel_live_at")


def test_release_ping_fires_when_first_buyable_read_lands_a_day_late(monkeypatch):
    """The Primal Hunter case: an audiobook dated yesterday whose pre-order state
    was never captured (detector couldn't read audio buy boxes), first read as
    buyable the day AFTER release. Same-day-only logic confirmed the release
    and said nothing. It must ping."""
    s = series([{"n": 15, "novel": d(-140), "audiobook": d(-1), "audio_pre": None}])
    s["asins_audio"] = {"15": "B0AUDIO015"}
    c, sent = _ingest_client(monkeypatch, [s])
    r = c.post("/api/ingest", json={
        "series": "Test", "book": {"n": 15, "title": "Test 15", "asin": "B0AUDIO015",
                                   "audiobook_date": d(-1)},
        "_preorder": False, "_asin": "B0AUDIO015", "_url": "https://www.amazon.com.au/dp/B0AUDIO015"})
    assert r.get_json()["ok"]
    assert len(sent) == 1 and "book 15" in sent[0].lower()   # wording is random; the ping is what matters
    assert any("release:t1:15:audiobook" in k for k in radar._state["discord_notified"])
    # and only once — a second identical read must not repeat it
    c.post("/api/ingest", json={
        "series": "Test", "book": {"n": 15, "title": "Test 15", "asin": "B0AUDIO015",
                                   "audiobook_date": d(-1)},
        "_preorder": False, "_asin": "B0AUDIO015", "_url": "https://www.amazon.com.au/dp/B0AUDIO015"})
    assert len(sent) == 1


def test_release_ping_stays_silent_for_old_catalogue(monkeypatch):
    """Backfilling a book released months ago reads as buyable too — that must
    NOT produce an 'out now' ping. The grace window is a few days, not forever."""
    s = series([{"n": 3, "novel": d(-400), "audiobook": d(-200), "audio_pre": None}])
    s["asins_audio"] = {"3": "B0AUDIO003"}
    c, sent = _ingest_client(monkeypatch, [s])
    c.post("/api/ingest", json={
        "series": "Test", "book": {"n": 3, "title": "Test 3", "asin": "B0AUDIO003",
                                   "audiobook_date": d(-200)},
        "_preorder": False, "_asin": "B0AUDIO003", "_url": "https://www.amazon.com.au/dp/B0AUDIO003"})
    assert sent == []


# ------------------------------------------------------------------ backups

def test_daily_backup_snapshot_and_prune(tmp_path, monkeypatch):
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"v": 1}), encoding="utf-8")
    bdir = tmp_path / "backups"
    monkeypatch.setattr(radar, "DATA_FILE", str(data))
    monkeypatch.setattr(radar, "BACKUP_DIR", str(bdir))
    bdir.mkdir()
    for i in range(20, 0, -1):        # 20 fake old snapshots
        (bdir / f"data-{(TODAY - timedelta(days=i)).isoformat()}.json").write_text("{}")
    radar._daily_backup()
    files = sorted(os.listdir(bdir))
    assert f"data-{TODAY.isoformat()}.json" in files
    assert len(files) == radar.BACKUP_KEEP
    radar._daily_backup()             # second call same day: no-op
    assert len(os.listdir(bdir)) == radar.BACKUP_KEEP


def test_dashboard_is_composed_from_static_files():
    """Phase-1 split: the served page is composed from static/dashboard.* and
    must contain no leftover markers; the sources must exist next to app.py
    (and inside the exe via --add-data \"static;static\")."""
    assert "__RR_CSS__" not in radar.PAGE_HTML and "__RR_JS__" not in radar.PAGE_HTML
    assert "<style>" in radar.PAGE_HTML and "<script>" in radar.PAGE_HTML
    for name in ("dashboard.html", "dashboard.css", "dashboard.js"):
        assert os.path.isfile(os.path.join(radar._STATIC_DIR, name)), name
    assert radar.PAGE_HTML == radar._load_page_html()


def test_app_py_matches_its_parts():
    """app.py is generated from src/ — direct edits to app.py (or a forgotten
    compose) must fail loudly, not ship silently."""
    import compose_app
    assert compose_app.compose_text() == open(compose_app.APP, encoding="utf-8", newline="").read()
    assert len(compose_app.parts()) >= 20


# ---------------------------------------------------------------- problems: alert once, nag daily, all-clear
# Background: the nightly AI research failed four nights running in Sep 2026 and
# the only trace was the dashboard's activity log. Anything a person must fix
# now goes through _problem(): one Discord ping, a daily reminder, an all-clear.

def _quiet(monkeypatch):
    sent = []
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "_discord_notify", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    radar._state = {"series": [], "log": [], "settings": {}, "ext_commands": [], "cmd_seq": 0}
    return sent


class _SyncThread:
    """Stand-in for threading.Thread that runs the target inline (and counts starts)."""
    starts = 0

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.t, self.a, self.k = target, args, kwargs or {}

    def start(self):
        _SyncThread.starts += 1
        self.t(*self.a, **self.k)


def test_problem_alerts_once_then_daily_then_clears(monkeypatch):
    sent = _quiet(monkeypatch)
    assert radar._problem("x", what="Thing broke.", detail="Fix it.") is True
    assert len(sent) == 1 and "Thing broke." in sent[0] and "Fix it." in sent[0]
    assert radar._problem("x", what="Thing broke.") is False           # same day: silent
    assert len(sent) == 1 and radar._state["problems"]["x"]["count"] == 2
    radar._state["problems"]["x"]["last_alert"] = (datetime.now() - timedelta(hours=25)).isoformat()
    assert radar._problem("x", what="Thing still broke.") is True      # daily reminder
    assert len(sent) == 2
    assert radar._problems_list()[0]["key"] == "x"
    assert radar._problem_clear("x", what="Thing works.") is True
    assert "Thing works." in sent[2] and "x" not in radar._state["problems"]
    assert radar._problem_clear("x") is False and len(sent) == 3       # clearing twice is silent
    assert radar.build_state_payload()["problems"] == []


def test_problem_survives_restart_without_realerting(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._problem("x", what="Thing broke.")
    saved = json.loads(json.dumps(radar._state))                       # what data.json would hold
    radar._state = saved                                               # "restart"
    assert radar._problem("x", what="Thing broke.") is False and len(sent) == 1


def test_problem_notify_false_is_dashboard_only(monkeypatch):
    sent = _quiet(monkeypatch)
    assert radar._problem("discord", what="Discord is down.", notify=False) is True
    assert sent == [] and radar._problems_list()[0]["what"] == "Discord is down."


def test_digest_failure_is_a_problem_and_clears_on_success(monkeypatch):
    sent = _quiet(monkeypatch)
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    monkeypatch.setattr(radar, "_discord_live_start", lambda text, embeds=None: None)   # webhook set, but no network here
    radar._state["settings"] = {"discord_daily": True, "discord_daily_hour": 0, "discord_digest_days": 1,
                                "discord_webhook": "https://discord.com/api/webhooks/x"}
    monkeypatch.setattr(radar, "_send_digest", lambda wh: (False, "HTTP 404"))
    radar._maybe_send_daily_digest()
    assert "digest" in radar._state["problems"] and any("didn't send" in t for t in sent)
    radar._state["discord_daily_last"] = None
    monkeypatch.setattr(radar, "_send_digest", lambda wh: (True, ""))
    radar._maybe_send_daily_digest()
    assert "digest" not in radar._state["problems"] and any("sending again" in t for t in sent)


# ---------------------------------------------------------------- read watchdog: polling but blind

def test_read_watchdog_nudges_then_alerts_then_clears(monkeypatch):
    """Sep 2026: three days of healthy 20-second polls and not one page read (the
    extension's worker lock was stuck). Self-heal with a forced refresh first;
    only if that yields nothing, tell Discord; all-clear when reads resume."""
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    radar._state["last_ingest"] = (datetime.now() - timedelta(hours=40)).isoformat(timespec="seconds")
    radar._ext_last_poll = datetime.now()
    radar._read_nudged_at = None
    radar._read_watchdog()
    assert [c["type"] for c in radar._state["ext_commands"]] == ["refresh"]
    assert sent == []
    radar._read_watchdog()                                             # no stacking, no alert yet
    assert len(radar._state["ext_commands"]) == 1 and sent == []
    radar._read_nudged_at = datetime.now() - timedelta(hours=7)
    radar._read_watchdog()
    assert len(sent) == 1 and "hasn't read" in sent[0] and "no_reads" in radar._state["problems"]
    radar._read_watchdog()                                             # still quiet: no second ping today
    assert len(sent) == 1
    radar._state["last_ingest"] = datetime.now().isoformat(timespec="seconds")
    radar._read_watchdog()
    assert "no_reads" not in radar._state["problems"] and len(sent) == 2 and "again" in sent[1]
    assert radar._read_nudged_at is None


def test_read_watchdog_leaves_a_dead_extension_to_the_poll_watchdog(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    radar._state["last_ingest"] = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    radar._ext_last_poll = datetime.now() - timedelta(hours=1)
    radar._read_nudged_at = None
    radar._read_watchdog()
    assert radar._state["ext_commands"] == [] and sent == []
    radar._ext_last_poll = datetime.now()


def test_read_watchdog_needs_something_to_compare_against(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._ext_last_poll = datetime.now()
    radar._read_nudged_at = None
    radar._read_watchdog()                                             # no series
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    radar._read_watchdog()                                             # never ingested
    assert radar._state["ext_commands"] == [] and sent == []


# ---------------------------------------------------------------- AI research: sign-out is a problem, not a log line

def _claude_code(monkeypatch, token=""):
    monkeypatch.setattr(radar, "_ai_conf", lambda: {"provider": "claude-code", "key": "", "token": token,
                                                     "model": "sonnet", "exe": "claude"})


def test_ai_error_kinds():
    assert radar._ai_error_kind("claude CLI failed (1): Failed to authenticate: OAuth session expired") == "auth"
    assert radar._ai_error_kind("HTTP Error 401: Unauthorized") == "auth"
    assert radar._ai_error_kind("HTTP Error 429: Too Many Requests") == "limit"
    assert radar._ai_error_kind("You've hit your usage limit") == "limit"
    assert radar._ai_error_kind("timed out after 240 seconds") == "other"


def test_ai_research_signed_out_cli_raises_one_problem(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    _claude_code(monkeypatch)

    def boom(prompt):
        raise RuntimeError("claude CLI failed (1): Failed to authenticate: OAuth session expired and could not be refreshed")
    monkeypatch.setattr(radar, "_ai_call", boom)
    ok, msg = radar._ai_research_series("s1")
    assert not ok and "claude auth login" in msg
    assert "ai_auth" in radar._state["problems"] and len(sent) == 1 and "signed out" in sent[0]
    radar._ai_research_series("s1")                                    # same failure, same day: one ping
    assert len(sent) == 1
    # a successful answer later clears it with an all-clear
    monkeypatch.setattr(radar, "_ai_call", lambda p: '{"status": "ongoing", "summary": "s", "confidence": "high"}')
    ok, _ = radar._ai_research_series("s1")
    assert ok and "ai_auth" not in radar._state["problems"] and len(sent) == 2


def test_ai_tick_skips_the_batch_while_signed_out_then_runs_it_serially(monkeypatch):
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [("s1", False), ("s2", False), ("s3", False)])
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: False)
    ran, busy_seen = [], []
    monkeypatch.setattr(radar, "_ai_research_series",
                        lambda sid, manual=False, why=None: (ran.append(sid) or busy_seen.append(radar._ai_worker_busy) or True, "ok"))
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    radar._ai_day.update({"date": None, "used": 0})
    radar._ai_worker_busy = False
    _SyncThread.starts = 0
    radar._ai_tick()
    radar._ai_tick()
    assert ran == [] and len(sent) == 1 and "signed out" in sent[0]
    assert radar._ai_day["used"] == 0                                  # budget kept for when it works
    assert "ai_auth" in radar._state["problems"] and _SyncThread.starts == 0
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: True)
    radar._ai_tick()
    assert ran == ["s1", "s2", "s3"] and _SyncThread.starts == 1       # ONE worker, in order (not a thread per series)
    assert busy_seen == [True, True, True]
    assert "ai_auth" not in radar._state["problems"] and len(sent) == 2 and "signed in again" in sent[1]
    assert radar._ai_day["used"] == 3 and radar._ai_worker_busy is False
    radar._ai_tick()                                                   # budget spent for today
    assert ran == ["s1", "s2", "s3"]


def test_ai_tick_two_failed_nights_are_a_problem(monkeypatch):
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [("s1", False)])
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: True)
    monkeypatch.setattr(radar, "_ai_research_series", lambda sid, manual=False, why=None: (False, "research failed: timed out"))
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    radar._ai_worker_busy = False
    radar._ai_day.update({"date": None, "used": 0})
    monkeypatch.setattr(radar, "today", lambda: date(2000, 1, 1))
    radar._ai_tick()
    assert sent == [] and radar._state["ai_fail_nights"] == 1          # one bad night: quiet
    radar._ai_tick()                                                   # a second batch the same day...
    assert radar._state["ai_fail_nights"] == 1                         # ...counts once, not as a second night
    monkeypatch.setattr(radar, "today", lambda: date(2000, 1, 2))
    radar._ai_tick()
    assert radar._state["ai_fail_nights"] == 2 and len(sent) == 1 and "2 nights" in sent[0]
    monkeypatch.setattr(radar, "_ai_research_series", lambda sid, manual=False, why=None: (True, "ongoing"))
    monkeypatch.setattr(radar, "today", lambda: date(2000, 1, 3))
    radar._ai_tick()
    assert radar._state["ai_fail_nights"] == 0 and "ai_failing" not in radar._state["problems"]


def test_ai_call_claude_code_runs_one_at_a_time_with_token_and_pinned_tools(monkeypatch):
    _claude_code(monkeypatch, token="sk-ant-oat01-LONGLIVED")
    # what a child would inherit when the app was started from a Claude Code session
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-NOPE")
    calls = []

    class R:
        returncode, stdout, stderr = 0, '{"status": "ongoing"}', ""

    def fake_run(cmd, **kw):
        assert radar._ai_cli_lock.locked()                             # serialised
        calls.append((cmd, kw))
        return R()
    monkeypatch.setattr(radar.subprocess, "run", fake_run)
    assert radar._ai_call("prompt\nline two") == '{"status": "ongoing"}'
    cmd, kw = calls[0]
    env = kw["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-LONGLIVED"
    assert not [k for k in env if k.startswith(("CLAUDE", "ANTHROPIC")) and k != "CLAUDE_CODE_OAUTH_TOKEN"]
    assert cmd[:3] == ["claude", "-p", "prompt\nline two"] and "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--tools") + 1:cmd.index("--tools") + 3] == ["WebSearch", "WebFetch"]
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions" and "--allow-dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--effort") + 1] == "max"                                   # thinking turned all the way up
    assert kw["stdin"] is radar.subprocess.DEVNULL and not radar._ai_cli_lock.locked()


def test_ai_cli_logged_in_reads_auth_status(monkeypatch):
    _claude_code(monkeypatch)

    class R:
        returncode, stdout, stderr = 1, '{"loggedIn": false, "authMethod": "none"}', ""
    monkeypatch.setattr(radar.subprocess, "run", lambda *a, **k: R())
    radar._ai_cli_auth.update({"ok": None, "at": 0.0})
    assert radar._ai_cli_logged_in() is False
    R.stdout = '{"loggedIn": true}'
    assert radar._ai_cli_logged_in() is False                          # cached
    assert radar._ai_cli_logged_in(force=True) is True
    _claude_code(monkeypatch, token="sk-ant-oat01-x")
    monkeypatch.setattr(radar.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no CLI call")))
    assert radar._ai_cli_logged_in(force=True) is None                 # a token can't be vouched for without a real call


def test_ai_tick_never_clears_a_token_failure_on_faith(monkeypatch):
    """With a setup-token pasted, the pre-flight used to answer 'signed in'
    unconditionally, so a bad token's alert was cleared five minutes later."""
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch, token="sk-ant-oat01-BAD")
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    radar._state["problems"] = {"ai_auth": {"what": "x", "detail": "", "count": 1,
                                            "since": "2026-01-01T00:00:00", "last_alert": "2026-01-01T00:00:00"}}
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [])
    radar._ai_worker_busy = False
    radar._ai_tick()
    assert "ai_auth" in radar._state["problems"] and sent == []


def test_ai_hung_problem_clears_when_the_worker_finishes_or_after_a_restart(monkeypatch):
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [])
    radar._ai_worker_busy, radar._ai_worker_since = True, 0.0             # "stuck" since the epoch
    radar._ai_tick()
    assert "ai_hung" in radar._state["problems"] and len(sent) == 1
    radar._ai_worker_busy = False                                        # finished (or the app restarted)
    radar._ai_tick()
    assert "ai_hung" not in radar._state["problems"]


def test_deconfiguring_ai_retires_its_problems(monkeypatch):
    sent = _quiet(monkeypatch)
    for key in ("ai_auth", "ai_key", "ai_failing"):
        radar._problem(key, what="old", notify=False)
    monkeypatch.setattr(radar, "_ai_conf", lambda: None)
    radar._ai_worker_busy = False
    radar._ai_tick()
    assert not any(k in radar._state["problems"] for k in ("ai_auth", "ai_key", "ai_failing")) and sent == []
    # switching to an API provider retires the CLI-specific one (its fix text would be wrong)
    radar._problem("ai_auth", what="old", notify=False)
    monkeypatch.setattr(radar, "_ai_conf", lambda: {"provider": "openrouter", "key": "k", "model": "m"})
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [])
    radar._ai_tick()
    assert "ai_auth" not in radar._state["problems"]


def test_changing_the_ai_config_retires_its_problems_and_cache(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(radar, "_backup_state", lambda: None)
    radar._state["settings"].update({"ai_provider": "claude-code", "ai_api_key": ""})
    radar._problem("ai_auth", what="old", notify=False)
    radar._ai_cli_auth.update({"ok": False, "at": 1.0})
    radar.app.test_client().post("/api/settings", json={"ai_provider": "openrouter", "ai_api_key": "sk-or-new"})
    assert "ai_auth" not in radar._state["problems"] and radar._ai_cli_auth["ok"] is None


def test_api_provider_rejected_key_is_not_a_cli_signout(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    monkeypatch.setattr(radar, "_ai_conf", lambda: {"provider": "openrouter", "key": "k", "model": "m"})

    def boom(prompt):
        raise RuntimeError("HTTP Error 401: Unauthorized")
    monkeypatch.setattr(radar, "_ai_call", boom)
    ok, msg = radar._ai_research_series("s1")
    assert not ok and "key" in msg and "claude auth login" not in msg
    assert "ai_key" in radar._state["problems"] and "ai_auth" not in radar._state["problems"]
    assert "openrouter" in sent[0] and "signed out" not in sent[0]


def test_manual_research_failure_keeps_the_nightly_slot(monkeypatch):
    _quiet(monkeypatch)
    radar._state["series"] = [_overdue_novel_series()]
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_call", lambda p: (_ for _ in ()).throw(RuntimeError("timed out")))
    radar._ai_research_series("s1", manual=True)
    assert radar._ai_candidates() == [("s1", True)]                    # a hand-triggered try spent no budget
    radar._ai_research_series("s1")
    assert radar._ai_candidates() == []                                # the automatic one does


def test_invalid_webhook_text_is_never_echoed():
    ok, err = radar._discord_post("MTIz-SECRET-bot-token-value", "hi")       # a mis-pasted bot token
    assert not ok and "SECRET" not in err and "https://discord.com/api/webhooks/" in err
    ok, err = radar._discord_post("https://discord.com/api/webhooks/1/SECRET PART2", "hi")   # embedded space
    assert not ok and "PART2" not in err and "SECRET" not in err


def test_good_post_or_new_webhook_ends_a_failure_streak(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(radar, "_backup_state", lambda: None)
    radar._discord_fail["n"] = 5
    radar._state["settings"]["discord_webhook"] = "https://discord.com/api/webhooks/1/old"
    radar.app.test_client().post("/api/settings", json={"discord_webhook": "https://discord.com/api/webhooks/1/new"})
    assert radar._discord_fail["n"] == 0
    radar._discord_fail["n"] = 5
    monkeypatch.setattr(radar, "urlopen", lambda req, timeout=10: __import__("io").BytesIO(b""))
    assert radar._discord_post("https://discord.com/api/webhooks/1/x", "hi") == (True, "")
    assert radar._discord_fail["n"] == 0


def test_manual_research_refuses_fast_when_signed_out(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: False)
    r = radar.app.test_client().post("/api/series/s1/research", json={})
    assert r.status_code == 400 and "claude auth login" in r.get_json()["message"]
    assert "ai_auth" in radar._state["problems"] and len(sent) == 1


def test_ai_cli_exe_sees_through_the_npm_shim(tmp_path, monkeypatch):
    """npm's claude.cmd runs through cmd.exe, which cuts the command line at the
    prompt's first newline: for weeks the model got a one-line prompt with no
    flags and no JSON instructions. The real binary sits next to the shim."""
    import shutil
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\r\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(shim))
    assert radar._ai_cli_exe() == str(shim)                            # nothing better known: hand it back...
    monkeypatch.setattr(radar, "_ai_conf", lambda: {"provider": "claude-code", "key": "", "token": "",
                                                     "model": "sonnet", "exe": str(shim)})
    with pytest.raises(RuntimeError, match="shim"):
        radar._ai_call("prompt")                                       # ...but never run it
    real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"MZ")
    assert radar._ai_cli_exe() == str(real)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert radar._ai_cli_exe() is None


def _overdue_novel_series(**kw):
    """Two novels 200 days apart, the last 300 days ago: the next one was due
    ~100 days ago and nothing is pre-ordered."""
    return series([{"n": 1, "novel": d(-500)}, {"n": 2, "novel": d(-300)}], id="s1", **kw)


def test_ai_failed_series_leaves_the_batch_for_the_day(monkeypatch):
    _quiet(monkeypatch)
    radar._state["series"] = [_overdue_novel_series()]
    assert radar._ai_candidates() == [("s1", True)]
    radar._ai_mark_tried("s1")
    assert radar._ai_candidates() == []


def test_ai_candidates_are_overdue_novels_only():
    """Rule: research only when a NOVEL is overdue. An overdue or missing
    audiobook, a series that is merely quiet, or a next novel already on
    pre-order is not a reason to spend a research call."""
    radar._state = {"series": [], "log": [], "settings": {}}
    ok = _overdue_novel_series()
    audio_only = series([{"n": 1, "novel": d(-500)}, {"n": 2, "novel": d(-30), "audiobook": None}], id="a")   # novel fresh, audio missing
    audio_only["audio_overdue"] = True
    single_old = series([{"n": 1, "novel": d(-400)}], id="q")                                             # quiet, no gap to judge by
    preordered = series([{"n": 1, "novel": d(-500)}, {"n": 2, "novel": d(-300)}, {"n": 3, "novel": d(+40), "novel_pre": True}], id="p")
    radar._state["series"] = [ok, audio_only, single_old, preordered]
    assert radar._ai_candidates() == [("s1", True)]


def test_ai_tick_clears_a_stale_signout_even_with_nothing_to_research(monkeypatch):
    """The banner said 'signed out' for as long as there was nothing to research,
    because the sign-in check only ran in front of a batch."""
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    radar._state["problems"] = {"ai_auth": {"what": "x", "detail": "", "count": 1,
                                            "since": "2026-01-01T00:00:00", "last_alert": "2026-01-01T00:00:00"}}
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [])
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: True)
    radar._ai_worker_busy = False
    radar._ai_tick()
    assert "ai_auth" not in radar._state["problems"] and len(sent) == 1 and "signed in again" in sent[0]


def test_ingest_stamps_a_read_even_when_nothing_changed(monkeypatch):
    """last_ingest is a 'something changed' stamp; a healthy quiet library never
    moves it. The read watchdog must trust page READS, or it cries wolf weekly."""
    s = series([{"n": 2, "novel": d(-200), "audiobook": d(-170), "title": "Test 2"}], status="auto")
    s["novel_title"] = "Test 2"
    c, _ = _ingest_client(monkeypatch, [s])
    radar._state["last_ingest"] = "2020-01-01T00:00:00"
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 2, "title": "Test 2", "audiobook_date": d(-170)}})
    assert radar._state["last_ingest"] == "2020-01-01T00:00:00"      # unchanged data: untouched
    assert radar._state["last_read"][:10] == TODAY.isoformat()       # but the read counts
    radar._ext_last_poll = datetime.now()
    radar._read_nudged_at = None
    radar._read_watchdog()
    assert radar._state["ext_commands"] == []


def test_read_watchdog_forgets_the_nudge_while_the_extension_is_away(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": []}]
    radar._state["last_read"] = (datetime.now() - timedelta(hours=40)).isoformat(timespec="seconds")
    radar._ext_last_poll = datetime.now()
    radar._read_nudged_at = None
    radar._read_watchdog()                                             # nudge queued
    radar._ext_last_poll = datetime.now() - timedelta(hours=8)         # Floorp closed for the evening
    radar._read_watchdog()
    assert radar._read_nudged_at is None and sent == []
    radar._ext_last_poll = datetime.now()                              # back: the 6-h clock restarts
    radar._read_watchdog()
    assert sent == [] and radar._read_nudged_at is not None
    assert [c["type"] for c in radar._state["ext_commands"]] == ["refresh"]   # still just the one command


def test_discord_problem_needs_two_consecutive_failures(monkeypatch):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    radar._state = {"series": [], "log": [], "settings": {"discord_webhook": "https://discord.com/api/webhooks/1/x"}}
    radar._discord_fail["n"] = 0
    posts = []
    monkeypatch.setattr(radar, "_discord_post",
                        lambda wh, text, embeds=None, fallback_text=None: (posts.append(text) or (False, "HTTP 404")))
    radar._discord_notify("one")
    assert "discord" not in radar._state.get("problems", {})           # one blip is weather
    radar._discord_notify("two")
    assert "discord" in radar._state["problems"]
    monkeypatch.setattr(radar, "_discord_post",
                        lambda wh, text, embeds=None, fallback_text=None: (posts.append(text) or (True, "")))
    radar._discord_notify("three")
    assert "discord" not in radar._state["problems"] and any("going through again" in t for t in posts)


def test_discord_error_text_never_carries_the_webhook():
    ok, err = radar._discord_post("discord.com/api/webhooks/123/SECRETTOKEN", "hi")   # schemeless: urllib rejects it
    assert not ok and "SECRETTOKEN" not in err
    assert "SECRET" not in radar._no_secrets("see https://discord.com/api/webhooks/1/SECRET now")


def test_changing_the_webhook_clears_discord_problems(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(radar, "_backup_state", lambda: None)
    radar._state["settings"]["discord_webhook"] = "https://discord.com/api/webhooks/1/old"
    radar._problem("discord", what="down", notify=False)
    radar._problem("digest", what="down", notify=False)
    radar.app.test_client().post("/api/settings", json={"discord_webhook": "https://discord.com/api/webhooks/1/new"})
    assert "discord" not in radar._state["problems"] and "digest" not in radar._state["problems"]


def test_manual_digest_clears_the_digest_problem(monkeypatch):
    _quiet(monkeypatch)
    radar._state["settings"]["discord_webhook"] = "https://discord.com/api/webhooks/1/x"
    radar._problem("digest", what="down", notify=False)
    monkeypatch.setattr(radar, "_send_digest", lambda wh: (True, ""))
    r = radar.app.test_client().post("/api/discord/digest-now", json={})
    assert r.get_json()["ok"] and "digest" not in radar._state["problems"]


def test_problem_banner_can_actually_hide():
    css = open(os.path.join(radar._STATIC_DIR, "dashboard.css"), encoding="utf-8").read()
    assert ".problems[hidden]{display:none}" in css                   # author display:flex must not beat [hidden]


def test_a_missed_estimate_is_researched_that_night_cap_or_no_cap(monkeypatch):
    """Rule: 'the day it's overdue from the estimates, not some arbitrary
    amount'. A just-missed estimate is urgent and never queues behind the
    daily cap; long-overdue re-checks fill what's left of it."""
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: True)
    monkeypatch.setattr(radar, "_ai_candidates", lambda: [("u1", True), ("u2", True), ("r1", False), ("r2", False),
                                                          ("r3", False), ("r4", False)])
    ran = []
    monkeypatch.setattr(radar, "_ai_research_series", lambda sid, manual=False, why=None: (ran.append((sid, why)) or True, "ok"))
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    radar._ai_worker_busy = False
    radar._ai_day.update({"date": None, "used": 0})
    radar._ai_tick()
    assert ran == [("u1", "overdue"), ("u2", "overdue"), ("r1", None), ("r2", None), ("r3", None)]
    assert radar._ai_day["used"] == 3                                  # only the re-checks count


def test_urgency_follows_the_estimate_not_the_calendar(monkeypatch):
    _quiet(monkeypatch)
    s = _overdue_novel_series()                                        # estimate missed ~100 days ago
    pred = radar.predicted_novel_date(s)
    radar._state["series"] = [s]
    assert radar._ai_candidates() == [("s1", True)]                    # never researched: urgent
    s["intel"] = {"status": "ongoing", "at": (pred - timedelta(days=3)).isoformat() + "T01:00:00"}
    assert radar._ai_candidates() == [("s1", True)]                    # researched BEFORE the miss: still urgent
    s["intel"]["at"] = (pred + timedelta(days=1)).isoformat() + "T01:00:00"
    assert radar._ai_candidates() == [("s1", False)]                   # researched after the miss, 14+ days ago: re-check
    s["intel"]["at"] = datetime.now().isoformat(timespec="seconds")
    assert radar._ai_candidates() == []                                # fresh: nothing to do




def test_narrator_tail_strips_more_counts_and_lead_is_first_billed():
    assert radar.clean_narrator("Justin Thomas James, & 3 more") == "Justin Thomas James"
    assert radar.clean_narrator("Justin Thomas James and 2 more") == "Justin Thomas James"
    assert radar.lead_narrator("Justin Thomas James, Jessica Threet, Jeff Hays") == "Justin Thomas James"
    assert radar.lead_narrator("A Reader & B Reader, see all") == "A Reader"
    assert radar.lead_narrator(None) is None


def test_narrator_ping_ignores_page_furniture_and_extra_cast(monkeypatch):
    """Riftside, Sep 2026: books 1-3 were stored as 'Justin Thomas James, see all'
    (captured before the cleaner existed); book 4 read clean, and the same man
    was announced as a narrator change. Judge swaps on the cleaned lead only."""
    s = series([{"n": 3, "novel": d(-200), "audiobook": d(-100), "narrator": "Justin Thomas James, see all"}])
    c, sent = _ingest_client(monkeypatch, [s])
    body = {"series": "Test", "book": {"n": 4, "title": "Test 4", "audiobook_date": d(+12),
                                       "narrator": "Justin Thomas James, Jessica Threet, Jeff Hays"}}
    c.post("/api/ingest", json=body)
    assert not any("Justin Thomas James" in t for t in sent)          # same lead: not news
    body["book"].update({"n": 5, "audiobook_date": d(+40), "narrator": "Someone Else"})
    c.post("/api/ingest", json=body)
    assert any("Someone Else" in t and "Justin Thomas James" in t and "see all" not in t for t in sent)


def test_load_state_cleans_narrators_captured_before_the_cleaner(tmp_path, monkeypatch):
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"series": [{"id": "x", "name": "X", "timeline": [
        {"n": 1, "audiobook": "2025-01-01", "narrator": "A Reader, see all"}]}]}), encoding="utf-8")
    monkeypatch.setattr(radar, "DATA_FILE", str(p))
    radar.load_state()
    assert radar._state["series"][0]["timeline"][0]["narrator"] == "A Reader"


# ---------------------------------------------------------------- embeds: the stripe colour says novel or audiobook

def test_embed_stripe_colour_follows_the_kind():
    assert radar._book_embed("X", 1, "http://c/1.jpg", "novel")["color"] == radar.KIND_COLOR["novel"]
    assert radar._book_embed("X", 1, "http://c/1.jpg", "audiobook")["color"] == radar.KIND_COLOR["audiobook"]
    assert radar._book_embed("X", 1, "http://c/1.jpg")["color"] == radar.KIND_COLOR["audiobook"]


def test_novel_pings_are_violet_and_audiobook_pings_amber(monkeypatch):
    """Every ping embed used to be amber (the audiobook colour), novels included."""
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    got = []
    monkeypatch.setattr(radar, "_discord_notify", lambda text, embeds=None: got.append((text, embeds)))
    s = series([{"n": 2, "novel": d(-200), "audiobook": d(-170), "title": "Test 2"}], status="auto")
    s["cover_url"] = "http://c/s.jpg"
    radar._state = {"series": [s], "log": [], "settings": {"discord_on_new": True}, "ext_commands": [], "cmd_seq": 0,
                    "discord_notified": []}
    c = radar.app.test_client()
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 3, "title": "Test 3", "novel_date": d(30)}})
    assert got and "novel" in got[-1][0] and got[-1][1][0]["color"] == radar.KIND_COLOR["novel"]
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 3, "title": "Test 3", "audiobook_date": d(60)}})
    assert "audiobook" in got[-1][0] and got[-1][1][0]["color"] == radar.KIND_COLOR["audiobook"]


# ---------------------------------------------------------------- alerts tidy up after themselves
# Rule: when something disconnects and comes back, the "reconnected" message
# should be the old warning edited, and an hour later it should disappear.

def _alert_env(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._state["settings"]["discord_webhook"] = "https://discord.com/api/webhooks/1/x"
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    urls = iter(f"https://wh/messages/{i}" for i in range(1, 10))
    monkeypatch.setattr(radar, "_discord_live_start", lambda text, embeds=None: next(urls))
    edits, deletes = [], []
    monkeypatch.setattr(radar, "_discord_live_edit", lambda url, text: edits.append((url, text)) or True)
    monkeypatch.setattr(radar, "_discord_delete", lambda url: deletes.append(url) or True)
    return sent, edits, deletes


def test_alert_edits_itself_into_the_all_clear_then_disappears(monkeypatch):
    sent, edits, deletes = _alert_env(monkeypatch)
    radar._alert_post("ext_silent", "warning: silent")
    assert radar._state["alerts"]["ext_silent"] == ["https://wh/messages/1"] and sent == []
    assert radar._alert_resolve("ext_silent", "all clear: back") is True
    assert edits == [("https://wh/messages/1", "all clear: back")] and sent == []   # edited, nothing new posted
    assert "ext_silent" not in radar._state["alerts"]
    tidy = radar._state["discord_tidy"]
    assert len(tidy) == 1 and tidy[0]["url"] == "https://wh/messages/1"
    radar._discord_tidy_tick()
    assert deletes == []                                                             # not before its hour is up
    tidy[0]["at"] = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    radar._discord_tidy_tick()
    assert deletes == ["https://wh/messages/1"] and radar._state["discord_tidy"] == []


def test_alert_without_a_webhook_or_history_falls_back_to_plain_messages(monkeypatch):
    sent = _quiet(monkeypatch)
    radar._alert_post("k", "warn")
    assert sent == ["warn"]                                                          # no webhook: plain and immediate
    assert radar._alert_resolve("k", "fixed") is False and sent == ["warn", "fixed"]
    assert radar._alert_resolve("k", "fixed", fallback=False) is False and len(sent) == 2


def test_watchdog_and_problems_use_self_tidying_alerts(monkeypatch):
    sent, edits, deletes = _alert_env(monkeypatch)
    radar._ext_last_poll = datetime.now() - timedelta(hours=7)
    radar._ext_alerted = False
    radar._ext_watchdog()
    assert radar._state["alerts"]["ext_silent"] == ["https://wh/messages/1"]
    radar._ext_seen()
    assert edits[-1][0] == "https://wh/messages/1" and "checking in again" in edits[-1][1]
    radar._problem("x", what="Thing broke.")
    assert radar._state["alerts"]["problem:x"] == ["https://wh/messages/2"]
    radar._state["problems"]["x"]["last_alert"] = (datetime.now() - timedelta(hours=25)).isoformat()
    radar._problem("x", what="Thing still broke.")                                    # the daily reminder
    assert radar._state["alerts"]["problem:x"] == ["https://wh/messages/2", "https://wh/messages/3"]
    radar._problem_clear("x", what="Thing works.", notify=False)                      # a silent clear...
    assert edits[-1] == ("https://wh/messages/3", edits[-1][1]) and "Thing works." in edits[-1][1]
    assert len(radar._state["discord_tidy"]) == 3 and sent == []                     # ...still takes every message down


# ---------------------------------------------------------------- AI research: the author's plan, nothing else
# Feedback (7 Oct 2026): "way too wordy ... I wanted it to find info like 'November,
# then a bunch right after each other', not explain random trivia".





def test_research_brief_is_schedule_only():
    s = series([{"n": 9, "novel": d(-300)}], id="s1", author="RinoZ")
    p = radar._ai_prompt_for(s)
    assert "WHEN" in p and "Patreon" in p and '"releases"' in p and '"source"' in p and "no release news found" in p
    assert "no plot" in p and "trivia" in p



# ---------------------------------------------------------------- research feeds the estimates, says nothing
# Feedback (7 Oct 2026): it shouldn't tell me anything - update the timing logic
# when there's an update, with a hoverable asterisk for the sourcing.

def _month_ahead(days):
    """A vague 'Month YYYY' safely ahead of today, and the day it lands on (15th)."""
    d0 = TODAY + timedelta(days=days)
    return d0.strftime("%B %Y"), date(d0.year, d0.month, 15)


def test_intel_parse_when_understands_how_authors_say_when():
    f = radar._intel_parse_when
    assert f("2026-11-11") == ("2026-11-11", "day")
    assert f("2026-11") == ("2026-11-15", "month")
    assert f("November 2026") == ("2026-11-15", "month") == f("Nov 2026") == f("nov. 2026")
    assert f("late November 2026") == ("2026-11-25", "month") and f("early Jan 2027") == ("2027-01-05", "month")
    assert f("Q1 2027") == ("2027-02-15", "quarter") and f("H2 2027") == ("2027-09-30", "half")
    assert f("spring 2027") == ("2027-04-15", "season") and f("fall of 2027") == ("2027-10-15", "season")
    assert f("early 2027") == ("2027-02-15", "year") and f("2027") == ("2027-07-01", "year")
    assert f("sometime, no date") is None and f("TBA") is None and f(None) is None and f("2026-13") is None


def test_ai_parse_normalises_the_release_list():
    d = radar._ai_parse('''{"status": "ongoing", "releases": [
        {"n": "10", "kind": "Audiobook", "expected": "November 2026", "said": "10 in November, then 11-13 back to back", "source": "author on Patreon, Sep 2026"},
        {"n": 11, "kind": "audio", "expected": "2026-12"}, "junk", {"n": null, "kind": "novel", "expected": "2027"}],
        "summary": "x", "confidence": "high"}''')
    assert [(r["n"], r["kind"], r["expected"]) for r in d["releases"]] == [(10, "audiobook", "November 2026"), (11, "audiobook", "2026-12"), (None, "novel", "2027")]
    assert d["releases"][0]["source"] == "author on Patreon, Sep 2026"
    assert radar._ai_parse('{"status": "ongoing", "releases": [], "summary": "no release news found"}')["releases"] == []


def test_research_brief_asks_for_dated_entries_and_reads_royal_road_comments():
    s = series([{"n": 9, "novel": d(-300)}], id="s1", author="by RinoZ", notes="RR: https://www.royalroad.com/fiction/1234/chrysalis")
    p = radar._ai_prompt_for(s)
    assert '"releases"' in p and "COMMENTS" in p and "https://www.royalroad.com/fiction/1234/chrysalis" in p
    assert '"RinoZ Test audiobook"' in p and "by by" not in p               # the stored "by " prefix is stripped
    assert "novel 10: nothing known - when?" in p


def test_author_date_becomes_the_novel_estimate_with_its_source(monkeypatch):
    sent = _quiet(monkeypatch)
    s = _overdue_novel_series()                                            # gap maths says ~100 days ago
    radar._state["series"] = [s]
    _claude_code(monkeypatch)
    when, landing = _month_ahead(60)
    monkeypatch.setattr(radar, "_ai_call", lambda p: json.dumps({"status": "ongoing", "releases": [
        {"n": 3, "kind": "novel", "expected": when, "said": "book 3 in " + when.split()[0] + ", then 4 and 5 back to back",
         "source": "author on Patreon, Sep 2026"}], "summary": "x", "confidence": "high"}))
    ok, _ = radar._ai_research_series("s1", why="overdue")
    assert ok and sent == []                                               # nothing posted, anywhere
    assert radar.predicted_novel_date(s) == landing                        # the author's month replaces the gap maths
    assert not radar._novel_overdue(s)                                     # ...so it is no longer overdue
    src = radar.predicted_novel_source(s)
    assert src["source"] == "author on Patreon, Sep 2026" and src["precision"] == "month"
    v = radar.series_view(s)
    assert v["predicted_novel_date"] == landing.isoformat() and v["predicted_novel_source"]["said"].startswith("book 3 in")
    row = [r for r in radar.upcoming_of_kind("novel", True) if r["series_id"] == "s1"][0]
    assert row["estimate"] and row["date"] == landing.isoformat() and row["source"]["source"] == "author on Patreon, Sep 2026"
    assert row["note"].startswith("author:") and row["confidence"] is None


def test_author_date_anchors_the_audiobook_queue(monkeypatch):
    sent = _quiet(monkeypatch)
    # three novels, the first with an audiobook: books 2 and 3 are in the audio queue
    s = series([{"n": 1, "novel": d(-600), "audiobook": d(-500)}, {"n": 2, "novel": d(-400)}, {"n": 3, "novel": d(-200)}], id="s1")
    radar._state["series"] = [s]
    when, landing = _month_ahead(45)
    s["intel_dates"] = {"audiobook:2": {"date": landing.isoformat(), "precision": "month", "expected": when,
                                       "said": "2 and 3 are with the narrator, " + when, "source": "author on Royal Road comments", "at": "2026-10-07T00:00:00"}}
    fc = radar._audio_next_forecast(s)
    assert fc["n"] == 2 and fc["date"] == landing and fc["source"]["source"] == "author on Royal Road comments" and not fc["queued"]
    sched = radar._audio_schedule(s)
    assert sched[1]["n"] == 3 and sched[1]["date"] > landing             # book 3 queues behind the author's date
    v = radar.series_view(s)
    assert v["predicted_date"] == landing.isoformat() and v["predicted_audio_source"]["said"].startswith("2 and 3")
    row = [r for r in radar.upcoming_of_kind("audiobook", True) if r["series_id"] == "s1"][0]
    assert row["book_n"] == 2 and row["source"] and row["note"].startswith("author:")


def test_author_date_is_ignored_once_stale_or_superseded_by_a_real_date():
    s = _overdue_novel_series()
    s["intel_dates"] = {"novel:3": {"date": d(-30), "precision": "day", "said": "x", "source": "y", "at": d(-60)}}
    assert radar._intel_when(s, "novel", 3) is None                        # 30 days past: the gap maths is back
    assert radar._novel_overdue(s)
    s["intel_dates"]["novel:3"]["date"] = d(-5)
    assert radar._intel_when(s, "novel", 3) is not None                    # within the grace period: still the estimate
    s["timeline"].append({"n": 3, "novel": d(20)})                         # the store dated it: the intel is moot
    s["novel_date"] = d(20)
    assert radar.predicted_novel_date(s) is None                           # a real pre-order is the next novel
    applied = radar._intel_apply(s, {"releases": [{"n": 3, "kind": "novel", "expected": "2030-01-01", "said": "", "source": ""}]})
    assert applied == []                                                   # never overrides a real date


# ---------------------------------------------------------------- Royal Road notes: harvested by the browser, read first
# The research tool gets a 403 from Royal Road; the extension doesn't. The
# author's announcement chapters, notes and comment replies are where "the
# audiobooks land in November, then back to back" is said.

def test_rr_notes_endpoint_keeps_the_harvest_on_the_series(monkeypatch):
    _quiet(monkeypatch)
    radar._state["series"] = [{"id": "s1", "name": "S", "timeline": [], "rr_requested_at": "2026-10-07T10:00:00"}]
    body = {"sid": "s1", "url": "https://www.royalroad.com/fiction/22518/chrysalis", "author": "RinoZ",
            "items": [{"chapter": "Book 10 release", "date": "2026-09-20T01:00:00Z", "kind": "announcement", "text": "x" * 2000},
                      {"chapter": "Chapter 1877", "kind": "reply", "text": "   "}, "junk"]}
    r = radar.app.test_client().post("/api/rr-notes", json=body, headers={"Origin": "moz-extension://abc"})
    assert r.get_json() == {"ok": True, "items": 1}
    rr = radar._state["series"][0]["rr_notes"]
    assert rr["author"] == "RinoZ" and len(rr["items"][0]["text"]) == 1200 and rr["at"][:4] == str(TODAY.year)
    assert "rr_requested_at" not in radar._state["series"][0]
    assert radar.app.test_client().post("/api/rr-notes", json={"sid": "nope"}).status_code == 404


def test_research_brief_quotes_the_royal_road_harvest_as_authoritative():
    s = series([{"n": 9, "novel": d(-300)}], id="s1", author="RinoZ")
    s["rr_notes"] = {"at": "2026-10-07T12:00:00", "items": [
        {"chapter": "Chapter 1877", "date": "2026-10-07T03:00:00", "kind": "reply", "text": "November 11th, and the audiobooks for 8-10 come right after each other."},
        {"chapter": "Book 10 release", "date": "2026-09-20T01:00:00", "kind": "announcement", "text": "Book 10 is out on Amazon today!"}]}
    p = radar._ai_prompt_for(s)
    assert "authoritative" in p and "November 11th" in p
    assert p.index("November 11th") < p.index("Book 10 is out")                 # newest first


def test_rr_url_comes_from_the_research_or_the_notes():
    s = {"id": "s1", "name": "S", "timeline": [], "notes": "RR: https://www.royalroad.com/fiction/22518/chrysalis/ (daily)"}
    assert radar._rr_url(s) == "https://www.royalroad.com/fiction/22518/chrysalis/"
    assert radar._rr_url({"notes": "https://www.patreon.com/rinoz"}) == ""
    s2 = {"id": "s2", "name": "T", "timeline": []}
    radar._intel_apply(s2, {"releases": [], "royalroad": "https://www.royalroad.com/fiction/21188/the-primal-hunter"})
    assert s2["rr_url"] == "https://www.royalroad.com/fiction/21188/the-primal-hunter"
    radar._intel_apply(s2, {"releases": [], "royalroad": "https://evil.example/x"})
    assert s2["rr_url"] == "https://www.royalroad.com/fiction/21188/the-primal-hunter"   # never overwritten with junk


def test_research_waits_for_the_royal_road_harvest_then_runs(monkeypatch):
    sent = _quiet(monkeypatch)
    _claude_code(monkeypatch)
    s = _overdue_novel_series()
    s["rr_url"] = "https://www.royalroad.com/fiction/1/x"
    radar._state["series"] = [s]
    radar._ext_last_poll = datetime.now()
    monkeypatch.setattr(radar, "_ai_cli_logged_in", lambda force=False: True)
    ran = []
    monkeypatch.setattr(radar, "_ai_research_series", lambda sid, manual=False, why=None: (ran.append(sid) or True, "ok"))
    monkeypatch.setattr(radar.threading, "Thread", _SyncThread)
    radar._ai_worker_busy = False
    radar._ai_day.update({"date": None, "used": 0})
    radar._ai_tick()
    assert ran == [] and [c["type"] for c in radar._state["ext_commands"]] == ["rrnotes"]   # harvest first
    assert radar._state["ext_commands"][0]["sid"] == "s1" and s["rr_requested_at"]
    radar._ai_tick()
    assert ran == [] and len(radar._state["ext_commands"]) == 1                             # still waiting, no second ask
    radar.app.test_client().post("/api/rr-notes", json={"sid": "s1", "url": s["rr_url"], "items": []},
                                 headers={"Origin": "moz-extension://abc"})
    radar._ai_tick()
    assert ran == ["s1"]                                                                     # notes in: researched
    # a harvest that never arrives doesn't block the research for good
    s["rr_notes"]["at"] = "2020-01-01T00:00:00"
    s["rr_requested_at"] = (datetime.now() - timedelta(minutes=11)).isoformat(timespec="seconds")
    s.pop("ai_tried", None); ran.clear()
    radar._ai_tick()
    assert ran == ["s1"]
    # no extension polling: research right away, without the notes
    radar._ext_last_poll = datetime.now() - timedelta(hours=2)
    s.pop("rr_requested_at", None); ran.clear()
    radar._ai_tick()
    assert ran == ["s1"]
    radar._ext_last_poll = datetime.now()


# ---------------------------------------------------------------- Audiobookshelf status line (11 Oct 2026)
# One plain fact ABS doesn't have, no emoji, or nothing; the same words as the
# extension's overlay chip. Feedback: "no emojis please ... cluttered".

def test_abs_status_line_is_one_plain_fact_or_nothing():
    T = "2026-10-11"
    loop = {"predicted_date": "2026-10-01", "predicted_book_n": 2, "next_date": "2026-10-01", "next_kind": "audiobook",
            "next_estimate": True, "status_label": "No audiobook yet", "listening": {"n": 1, "pct": 82}}
    assert radar._abs_status_line(loop, T) == "Audio 2 overdue by 10 days"
    assert radar._abs_status_line({"predicted_date": "2026-10-10", "predicted_book_n": 2}, T) == "Audio 2 overdue by 1 day"
    assert radar._abs_status_line({"predicted_date": "2027-02-13", "predicted_book_n": 6, "next_date": "2027-02-13",
                                   "next_kind": "audiobook", "next_estimate": True}, T) == "Audio 6 est. 13 Feb 2027"
    assert radar._abs_status_line({"next_date": "2026-11-03", "next_kind": "audiobook", "next_estimate": False,
                                   "predicted_date": "2026-11-03", "predicted_book_n": 3}, T) == "Audiobook 3 Nov"
    assert radar._abs_status_line({"next_date": "2026-12-01", "next_kind": "novel", "next_estimate": False}, T) == "Novel 1 Dec"
    assert radar._abs_status_line({"next_date": "2026-10-14", "next_kind": "novel", "next_estimate": False,
                                   "predicted_date": "2027-02-13", "predicted_book_n": 8}, T) == "Audio 8 est. 13 Feb 2027"
    assert radar._abs_status_line({"next_est_date": "2027-01-20", "next_est_book_n": 3}, T) == "Audio 3 est. 20 Jan 2027"
    assert radar._abs_status_line({"status_label": "Audio pre-order", "listening": {"n": 1, "pct": 50}}, T) == ""
    for v in (loop, {"next_date": "2026-12-01", "next_kind": "novel", "next_estimate": False}):
        assert not any(ord(ch) > 0x2000 for ch in radar._abs_status_line(v, T))     # plain text only


# ---------------------------------------------------------------- the radar's line inside Audiobookshelf (11 Oct 2026)
# Written to ABS's own per-series description through its API; never over
# someone else's words; removed again when there is nothing to say.

def _abs_views():
    return [{"name": "Loopbreaker", "predicted_date": "2026-10-01", "predicted_book_n": 2},
            {"name": "Riftside", "next_date": "2026-11-03", "next_kind": "audiobook", "next_estimate": False},
            {"name": "Chrysalis", "status_label": "No audiobook yet"},            # nothing to say
            {"name": "Not In ABS", "predicted_date": "2026-10-01", "predicted_book_n": 9}]


def _abs_listing(**desc):
    base = {"s-loop": "Loopbreaker", "s-rift": "Riftside Series", "s-chry": "Chrysalis", "s-other": "Dual Class"}
    return [{"id": k, "name": v, "description": desc.get(k, "")} for k, v in base.items()]


def test_abs_status_plan_writes_refreshes_clears_and_respects_others():
    T = "2026-10-11"
    w, c, f = radar._abs_status_plan(_abs_views(), _abs_listing(), {}, T)
    assert w == [("s-loop", "Loopbreaker", "Audio 2 overdue by 10 days"), ("s-rift", "Riftside Series", "Audiobook 3 Nov")]
    assert c == [] and f == []
    # already there and unchanged: nothing to do
    written = {"s-loop": {"text": "Audio 2 overdue by 10 days"}, "s-rift": {"text": "Audiobook 3 Nov"}}
    listing = _abs_listing(**{"s-loop": "Audio 2 overdue by 10 days", "s-rift": "Audiobook 3 Nov"})
    assert radar._abs_status_plan(_abs_views(), listing, written, T) == ([], [], [])
    # a day later the line changes: refresh ours
    w, c, f = radar._abs_status_plan(_abs_views(), listing, written, "2026-10-12")
    assert w == [("s-loop", "Loopbreaker", "Audio 2 overdue by 11 days")]
    # nothing to say any more (the audiobook landed): the line we wrote is cleared, never a foreign one
    quiet = [dict(v, predicted_date=None) if v["name"] == "Loopbreaker" else v for v in _abs_views()]
    w, c, f = radar._abs_status_plan(quiet, listing, written, T)
    assert w == [] and c == [("s-loop", "Loopbreaker")]
    # someone else's words in the description: left alone, reported
    w, c, f = radar._abs_status_plan(_abs_views(), _abs_listing(**{"s-loop": "someone's own note"}), {}, T)
    assert ("s-loop", "Loopbreaker") in f and all(x[0] != "s-loop" for x in w)
    # a cleared description that we once wrote is not "cleared" twice
    assert radar._abs_status_plan(quiet, _abs_listing(), written, T)[1] == []
    # ABS names a series differently: one name inside the other still finds it, an ambiguous one doesn't
    storm = [{"name": "Warformed: Stormweaver", "predicted_date": "2027-01-05", "predicted_book_n": 3}]
    lst = [{"id": "s-st", "name": "Stormweaver Series", "description": ""}]
    assert radar._abs_status_plan(storm, lst, {}, T)[0] == [("s-st", "Stormweaver Series", "Audio 3 est. 5 Jan 2027")]
    lst2 = lst + [{"id": "s-wf", "name": "Warformed", "description": ""}]          # two candidates: no guess
    assert radar._abs_status_plan(storm, lst2, {}, T)[0] == []


def test_abs_sync_status_patches_the_series_and_remembers(monkeypatch):
    calls = []

    def fake_req(path, method="GET", timeout=15, payload=None):
        calls.append((method, path, payload))
        if path.startswith("/api/libraries/"):
            return {"results": [{"id": "s-loop", "name": "Loopbreaker", "description": ""}], "total": 1}
        return {}
    monkeypatch.setattr(radar, "_abs_req", fake_req)
    monkeypatch.setattr(radar, "_abs_conf", lambda: ("http://abs", "tok"))
    monkeypatch.setattr(radar, "_abs_library_id", lambda: "lib1")
    monkeypatch.setattr(radar, "series_view", lambda s: {"name": s["name"], "predicted_date": "2000-01-01", "predicted_book_n": 2})
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    radar._state["series"] = [{"id": "x", "name": "Loopbreaker"}]
    radar._state["settings"] = {}
    radar._state["abs_written"] = {}
    radar._abs_cache["sync_at"] = 0.0
    done = radar._abs_sync_status(force=True)
    assert done["written"] == 1 and done["failed"] == 0
    patches = [c for c in calls if c[0] == "PATCH"]
    assert patches == [("PATCH", "/api/series/s-loop", {"description": patches[0][2]["description"]})]
    assert patches[0][2]["description"].startswith("Audio 2 overdue by ")
    assert radar._state["abs_written"]["s-loop"]["text"] == patches[0][2]["description"]
    # the hourly throttle holds, force bypasses it
    assert radar._abs_sync_status() is None
    radar._state["settings"] = {"abs_write_status": False}
    assert radar._abs_sync_status(force=True) is None                       # switched off: never writes
    # switched off -> the lines come back out
    radar._abs_clear_status()
    assert calls[-1] == ("PATCH", "/api/series/s-loop", {"description": ""}) and radar._state["abs_written"] == {}


# ---------------------------------------------------------------- what the tunnel may say about the owner (12 Oct 2026)
# The read-only tunnel has no login in front of it. A reviewer found the
# machine's folder layout, a pytest temp path and the Discord user behind a
# button in what a stranger could read. Release dates are public; the owner is not.

REMOTE = {"X-Forwarded-For": "203.0.113.50", "Host": "radar.example.org"}


def test_public_text_strips_paths_local_urls_and_discord_identities():
    pt = radar._public_text
    assert pt(r"filed to T:\Books\AudioBooks\Author\Series - Book 5") == "filed to <path>"
    assert pt(r"bat at C:\Users\someone\Desktop\x\Update Audiobooks.bat not found") == "bat at <path>"   # spaces in paths: the rest of the clause goes too
    assert pt(r"NEW  Author / Series / Book 3 -> T:\Books\AudioBooks\Author\Series\Book 3  (new series)") == "NEW  Author / Series / Book 3 -> <path>(new series)"
    assert pt("Audiobookshelf base resolved: http://localhost:13378/audiobookshelf/config -> http://localhost:13378/audiobookshelf") == "Audiobookshelf base resolved: <local url> -> <local url>"
    assert pt("retry from Discord (somebody#1234)") == "retry from Discord"
    assert pt("Ingested from Amazon: Riftside book 4 (audiobook 2026-10-02)") == "Ingested from Amazon: Riftside book 4 (audiobook 2026-10-02)"
    assert pt(None) is None


def _remote_env(monkeypatch, tmp_path):
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    monkeypatch.setattr(radar, "_bat_path", lambda: r"C:\Users\someone\Desktop\Update Audiobooks.bat")
    monkeypatch.setattr(radar, "_libation_files_dir", lambda: r"C:\Users\someone\Libation")
    radar._state = {"series": [], "log": [{"at": "2026-10-12T10:00:00", "msg": r"filed to T:\Books\X - Book 1"},
                                          {"at": "2026-10-12T10:01:00", "msg": "Audiobookshelf base resolved: http://localhost:13378/a -> http://localhost:13378"}],
                    "ext_commands": [], "cmd_seq": 0, "buy_ledger": {}, "discord_notified": [], "problems": {},
                    "buy_jobs": [{"id": "j1", "series": "Riftside", "n": 4, "status": "done", "note": "bought",
                                  "steps": [{"at": "x", "step": "retry", "ok": True, "detail": "retry from Discord (somebody)"}],
                                  "token_hash": "abc"}],
                    "settings": {"buy_bat": r"C:\Users\someone\Desktop\Update Audiobooks.bat", "abs_url": "http://localhost:13378"}}
    return radar.app.test_client()


def test_remote_views_carry_no_machine_or_identity_details(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    s = c.get("/api/settings", headers=REMOTE).get_json()
    assert s["remote_readonly"] is True
    assert s["buy_bat"] == "" and s["buy_bat_found"] == "" and s["buy_libation_found"] == "" and s["abs_url"] == ""
    local = c.get("/api/settings").get_json()
    assert local["buy_bat_found"].endswith("Update Audiobooks.bat")                     # the PC still sees its own paths
    j = c.get("/api/buy/jobs", headers=REMOTE).get_json()
    assert j["bat"] == "" and j["libation"] == ""
    assert j["jobs"][0]["steps"][0]["detail"] == "retry from Discord" and j["jobs"][0]["token_hash"] is None
    assert c.get("/api/buy/jobs").get_json()["jobs"][0]["steps"][0]["detail"] == "retry from Discord (somebody)"
    st = c.get("/api/state", headers=REMOTE).get_json()
    msgs = [e["msg"] for e in st["log"]]
    assert msgs[0] == "filed to <path>" and "<local url>" in msgs[1] and "localhost" not in json.dumps(st["log"])
    assert "localhost" in json.dumps(c.get("/api/state").get_json()["log"])


def test_remote_holds_show_the_book_not_the_folder(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    held = tmp_path / "Libation Audiobooks" / "Somebody" / "Thing - Book 2"
    held.mkdir(parents=True)
    rep = tmp_path / "filer-report.json"
    rep.write_text(json.dumps({"results": [{"author": "Somebody", "series": None, "book": "Thing - Book 2", "src": str(held),
                                            "action": "HELD", "target": None, "reason": "ambiguous: 'Thing' exists under ['A', 'B']"}]}), encoding="utf-8")
    monkeypatch.setattr(radar, "_filer_report_path", lambda: str(rep))
    h = c.get("/api/holds", headers=REMOTE).get_json()["holds"]
    assert h[0]["src"] == "Thing - Book 2" and h[0]["candidates"] == ["A", "B"]
    assert c.get("/api/holds").get_json()["holds"][0]["src"] == str(held)


def test_tunnel_can_demand_a_cloudflare_access_login(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    assert c.get("/api/state", headers=REMOTE).status_code == 200                       # default: read-only, open
    radar._state["settings"]["tunnel_require_access"] = True
    assert c.get("/api/state", headers=REMOTE).status_code == 403
    assert c.get("/", headers=REMOTE).status_code == 403
    assert c.get("/api/state", headers={**REMOTE, "Cf-Access-Authenticated-User-Email": "owner@example.org"}).status_code == 200
    assert c.get("/api/state").status_code == 200                                       # the PC itself never needs it
    # the switch cannot be flipped from the tunnel side
    r = c.post("/api/settings", json={"tunnel_require_access": False}, headers={**REMOTE, "Cf-Access-Authenticated-User-Email": "owner@example.org"})
    assert r.status_code == 403 and radar._state["settings"]["tunnel_require_access"] is True


def test_public_text_second_pass_covers_ips_tags_webhooks_and_keeps_public_urls():
    pt = radar._public_text
    assert pt("Discord bot online as Alice-Chan#8496") == "Discord bot online as Discord"
    assert pt("refused a remote POST (from 203.0.113.7)") == "refused a remote POST (from <ip>)"
    assert pt("HTTP Error 400: https://discord.com/api/webhooks/123/abcTOKEN") == "HTTP Error 400: https://discord.com/api/webhooks/<redacted>"
    assert pt("Ingested from https://www.amazon.com.au/dp/B0HJ9DX4S2") == "Ingested from https://www.amazon.com.au/dp/B0HJ9DX4S2"
    assert pt("ABS at http://nas:13378/audiobookshelf and http://192.168.1.20:13378") == "ABS at <local url> and <local url>"
    assert pt("copied from " + "\\\\" + r"nas\books\Author\Book 1 (new)") == "copied from <path>(new)"     # UNC share
    assert pt("in /c/Users/someone/Desktop/x.bat today") == "in <path> today"


def test_remote_ext_commands_poll_is_inert(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    radar._state["ext_commands"] = [{"id": "c1", "kind": "crawl"}]
    seen = []
    monkeypatch.setattr(radar, "_ext_seen", lambda: seen.append(1))
    assert c.get("/api/ext-commands", headers=REMOTE).get_json() == {"commands": []} and seen == []
    assert c.get("/api/ext-commands").get_json()["commands"][0]["id"] == "c1" and seen == [1]
    assert c.get("/api/instance", headers=REMOTE).get_json()["pid"] == 0
    assert c.get("/api/instance").get_json()["pid"] > 0


def test_refused_requests_do_not_reach_the_public_activity_feed(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    logged = []
    monkeypatch.setattr(radar, "add_log", lambda m: logged.append(m))
    r = c.post("/api/series/clear", headers={**REMOTE, "Cf-Connecting-Ip": "203.0.113.77"})
    assert r.status_code == 403 and logged == []
    r = c.post("/api/quit", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and logged == []


def test_lan_devices_are_remote_too(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    r = c.get("/api/settings", environ_base={"REMOTE_ADDR": "192.168.1.55"}, headers={"Host": "127.0.0.1:8765"})
    assert r.get_json()["remote_readonly"] is True and r.get_json()["buy_bat_found"] == ""
    assert c.get("/api/settings", environ_base={"REMOTE_ADDR": "127.0.0.1"}).get_json()["remote_readonly"] is False


def test_buy_page_escapes_what_it_shows(monkeypatch, tmp_path):
    c = _remote_env(monkeypatch, tmp_path)
    tok = "t" * 32
    radar._state["buy_jobs"].append({"id": "j2", "series": "<img src=x onerror=alert(1)>", "n": 3, "title": "T & Co", "status": "awaiting_approval",
                                     "note": r"queued from C:\Users\someone\x", "cap": 10, "price": 7.5,
                                     "token_hash": radar._buy_token_hash(tok) if hasattr(radar, "_buy_token_hash") else None})
    monkeypatch.setattr(radar, "_buy_find_by_token", lambda t: radar._state["buy_jobs"][-1] if t == tok else None)
    html_out = c.get(f"/buy/{tok}", headers=REMOTE).get_data(as_text=True)
    assert "<img src=x" not in html_out and "&lt;img src=x onerror=alert(1)&gt;" in html_out
    assert "T &amp; Co" in html_out and "someone" not in html_out and "&lt;path&gt;" in html_out
