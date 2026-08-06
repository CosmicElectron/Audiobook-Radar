"""Tests for Release Radar's pure logic: gap maths, predictions, next-release
resolution, forecast rows, timeline cleaning, ICS/digest builders, backups.

Run:  python -m pytest test_app.py -q     (from this folder)

No test touches the real %APPDATA% data.json — everything runs on synthetic
series dicts, and the backup test is pointed at a temp directory.
"""
import importlib.util
import json
import os
from datetime import date, timedelta

import pytest

_spec = importlib.util.spec_from_file_location(
    "radar", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"))
radar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radar)

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
