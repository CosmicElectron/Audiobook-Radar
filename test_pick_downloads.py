"""pick_downloads.py: Update Audiobooks downloads only books in a series
Release Radar tracks (29 Sep 2026: a post-purchase run also fetched and filed
two unrelated books). Built on a small copy of Libation's real schema."""
import json
import sqlite3

import pytest

import pick_downloads as pd


def libation(tmp_path, books):
    """books: (asin, title, series or None, order, status) tuples, or dicts with
    those keys plus authors=[names] and published='YYYY-MM-DD HH:MM:SS'
    -> a Libation files dir holding a LibationContext.db shaped like the real one."""
    d = tmp_path / "Libation"
    d.mkdir(parents=True)
    con = sqlite3.connect(str(d / "LibationContext.db"))
    con.executescript("""
        create table Books (BookId integer primary key, AudibleProductId text, Title text, DatePublished text);
        create table UserDefinedItem (BookId integer, BookStatus integer);
        create table Series (SeriesId integer primary key, AudibleSeriesId text, Name text);
        create table SeriesBook (SeriesId integer, BookId integer, "Order" text);
        create table Contributors (ContributorId integer primary key, AudibleContributorId text, Name text);
        create table BookContributor (BookId integer, ContributorId integer, Role integer, "Order" integer);""")
    series_ids, people = {}, {}
    for i, b in enumerate(books, 1):
        if not isinstance(b, dict):
            b = dict(zip(("asin", "title", "series", "order", "status"), b))
        con.execute("insert into Books values (?, ?, ?, ?)", (i, b["asin"], b["title"], b.get("published")))
        con.execute("insert into UserDefinedItem values (?, ?)", (i, b.get("status", 0)))
        if b.get("series"):
            sid = series_ids.setdefault(b["series"], len(series_ids) + 1)
            con.execute("insert or ignore into Series values (?, ?, ?)", (sid, f"S{sid}", b["series"]))
            con.execute("insert into SeriesBook values (?, ?, ?)", (sid, i, b.get("order")))
        for role, names in ((pd.ROLE_AUTHOR, b.get("authors") or []), (2, b.get("narrators") or [])):
            for k, name in enumerate(names):
                cid = people.setdefault(name, len(people) + 1)
                con.execute("insert or ignore into Contributors values (?, ?, ?)", (cid, f"C{cid}", name))
                con.execute("insert into BookContributor values (?, ?, ?, ?)", (i, cid, role, k))
    con.commit()
    con.close()
    return str(d)


def radar_data(tmp_path, names, asins=None, series=None, settings=None):
    """names -> bare tracked series; series -> full Release Radar series records."""
    recs = [{"name": n, "asins_audio": (asins or {}).get(n, {})} for n in names] + list(series or [])
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"series": recs, "settings": settings or {}}), encoding="utf-8")
    return str(p)


def run(lib, data):
    """main() -> (stdout ids, stderr text)"""
    import io, contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = pd.main(["--libation", lib, "--data", data])
    return rc, out.getvalue().split(), err.getvalue()


BOOKS = [
    ("B0HJ9CQDHW", "Riftside 4", "Riftside", "4", 0),                     # tracked, pending
    ("B0FGY99X92", "The Unfallen", "The Unfallen Series", "1", 0),         # not tracked
    ("B0G7LX1VMD", "Princess of the Void", "Princess of the Void", "1", 0),
    ("B0RIFT0003", "Riftside 3", "Riftside", "3", 1),                     # tracked, already downloaded
    ("B0PHUNTER1", "The Primal Hunter 16", "The Primal Hunter Series", "16", 0),   # "Series" suffix differs
    ("B0NOSERIES", "Defiance of the Fall 17: A LitRPG", None, None, 0),  # no series on Audible's record
]


def test_downloads_only_pending_books_of_tracked_series(tmp_path, capsys):
    lib = libation(tmp_path, BOOKS)
    data = radar_data(tmp_path, ["Riftside", "The Primal Hunter", "Defiance of the Fall"])
    assert pd.main(["--libation", lib, "--data", data]) == 0
    out, err = capsys.readouterr()
    assert out.split() == ["B0HJ9CQDHW", "B0PHUNTER1", "B0NOSERIES"]
    assert "The Unfallen" in err and "Princess of the Void" in err       # named as skipped
    assert "B0RIFT0003" not in out                                        # already downloaded


def test_nothing_tracked_pending_prints_nothing(tmp_path, capsys):
    lib = libation(tmp_path, BOOKS)
    data = radar_data(tmp_path, ["Chrysalis"])
    assert pd.main(["--libation", lib, "--data", data]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "" and "Nothing new" in err


def test_when_in_doubt_download_nothing(tmp_path, capsys):
    lib = libation(tmp_path, BOOKS)
    assert pd.main(["--libation", lib, "--data", str(tmp_path / "missing.json")]) == 1
    assert capsys.readouterr().out.strip() == ""
    assert pd.main(["--libation", str(tmp_path / "nope"), "--data", radar_data(tmp_path, ["Riftside"])]) == 1
    assert capsys.readouterr().out.strip() == ""
    assert pd.main(["--libation", lib, "--data", radar_data(tmp_path, [])]) == 1   # tracks nothing
    assert capsys.readouterr().out.strip() == ""


def test_a_known_audible_id_counts_even_under_another_series_name(tmp_path, capsys):
    lib = libation(tmp_path, [("B0ODDNAME1", "Book 5", "Completely Different Name", "5", 0)])
    data = radar_data(tmp_path, ["Riftside"], asins={"Riftside": {"5": "B0ODDNAME1"}})
    pd.main(["--libation", lib, "--data", data])
    assert capsys.readouterr().out.split() == ["B0ODDNAME1"]


@pytest.mark.parametrize("a,b", [("The Unfallen Series", "The Unfallen"), ("RE: Maelstrom", "RE_ Maelstrom"),
                                 ("1% Lifesteal", "1 Lifesteal"), ("The Primal Hunter", "Primal Hunter")])
def test_series_names_normalise_alike(a, b):
    assert pd.norm(a) == pd.norm(b)


def test_the_picker_honours_the_apps_libation_files_setting(tmp_path):
    """The app can be pointed at a Libation folder in Settings; the bat's
    picker reads the same data.json, so it looks there too."""
    lib = libation(tmp_path / "elsewhere", [("B0HJ9CQDHW", "Riftside 4", "Riftside", "4", 0)])
    data = radar_data(tmp_path, ["Riftside"], settings={"buy_libation_files": lib})
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        assert pd.main(["--data", data]) == 0
    assert out.getvalue().split() == ["B0HJ9CQDHW"]


# ---------------------------------------------------------------- the author's own book (7 Oct 2026)
# Arcane Academy book 5's audiobook came out on 2 Oct 2026 titled "Spell
# Breaker" (every book in that series is), with NO series on Audible's record
# yet, under Audible's own product ID. All three rules missed it and the run
# refused a book the user tracks.

ARCANE = {
    "name": "Arcane Academy", "author": "by A.P. Gore", "novel_n": 7,
    "novel_title": "Spell Breaker: A Progression Fantasy Adventure (Arcane Academy Book 7)",
    "asins_audio": {"1": "B0FHJ5T7N4", "5": "B0HLTXRTJB"},                  # Amazon listing ASINs, not Audible's
    "timeline": [
        {"n": 1, "novel": "2025-04-30", "audiobook": "2025-07-14"},
        {"n": 4, "novel": "2026-01-31", "audiobook": "2026-05-12"},
        {"n": 5, "novel": "2026-04-30", "audiobook": "2026-10-02",
         "title": "Spell Breaker: A Progression Fantasy Adventure (Arcane Academy Book 5)"},
        {"n": 6, "novel": "2026-09-21", "audiobook": None,
         "title": "Spell Breaker: A Progression Fantasy Adventure (Arcane Academy Book 6)"},
    ],
}
SPELL_BREAKER_5 = {"asin": "B0HLV5JPC9", "title": "Spell Breaker", "series": None, "order": None, "status": 0,
                   "authors": ["A.P. Gore", "Patricia Jones"], "narrators": ["Matt Hicks"],
                   "published": "2026-10-02 20:03:00"}


def untitled(series):
    return dict(series, novel_title=None, timeline=[dict(e, title=None) for e in series["timeline"]])


def test_a_series_less_book_is_known_by_its_author_and_title(tmp_path):
    lib = libation(tmp_path, [SPELL_BREAKER_5])
    rc, ids, err = run(lib, radar_data(tmp_path, [], series=[ARCANE]))
    assert rc == 0 and ids == ["B0HLV5JPC9"]
    assert "Download (tracked): Spell Breaker [B0HLV5JPC9] - Arcane Academy (by author + title)" in err


def test_a_release_date_alone_never_takes_a_book(tmp_path):
    """Shawn Wilson's Ultimate Level can land series-less a day after a
    Loopbreaker release: the author + date are not enough. A tracked book the
    radar has no title for is skipped (and named in the Discord message)."""
    data = radar_data(tmp_path, [], series=[untitled(ARCANE)])
    rc, ids, err = run(libation(tmp_path, [SPELL_BREAKER_5]), data)
    assert ids == [] and "Spell Breaker (no series)" in err
    # the same day, the same author, a different series' title: never
    rc, ids, _ = run(libation(tmp_path / "b", [dict(SPELL_BREAKER_5, title="Ultimate Level 1: New Freedoms")]),
                     radar_data(tmp_path / "b", [], series=[ARCANE]))
    assert ids == []


def test_another_authors_series_of_the_same_name_is_not_ours(tmp_path):
    lib = libation(tmp_path, [
        {"asin": "B0OTHERAA1", "title": "The Sorting Stone", "series": "The Arcane Academy Series", "order": "1", "status": 0, "authors": ["Jane Romance"]},
        {"asin": "B0OURSAA05", "title": "Spell Breaker", "series": "Arcane Academy", "order": "5", "status": 0, "authors": ["A.P. Gore"]},
        {"asin": "B0NOAUTHOR", "title": "Spell Breaker", "series": "Arcane Academy", "order": "6", "status": 0},   # no author on record: the name decides
    ])
    rc, ids, _ = run(lib, radar_data(tmp_path, [], series=[ARCANE]))
    assert ids == ["B0OURSAA05", "B0NOAUTHOR"]


def test_the_authors_other_work_and_a_namesake_title_are_not_taken(tmp_path):
    lib = libation(tmp_path, [
        dict(SPELL_BREAKER_5, asin="B0OTHERWRK", title="Spell Weaver", published="2026-10-20 00:00:00"),   # same author, unknown title & date
        dict(SPELL_BREAKER_5, asin="B0NAMESAKE", authors=["Blake Charlton"]),                                # same title, other author
        dict(SPELL_BREAKER_5, asin="B0SPINOFF1", title="Spell Weaver", series="Arcane Academy Tales",
             order="1", published="2026-10-02 20:03:00", authors=["A.P. Gore"]),                              # a spin-off series, same day
        dict(SPELL_BREAKER_5, asin="B0FARAWAY1", series="Dragon Riders", order="2",
             published="2026-10-20 00:00:00", authors=["A.P. Gore"]),                                         # reused title, unrelated series, another day
    ])
    rc, ids, err = run(lib, radar_data(tmp_path, [], series=[ARCANE]))
    assert rc == 0 and ids == [] and "Nothing new" in err
    assert "Spell Weaver (no series)" in err and "B0NAMESAKE" not in ids


def test_box_sets_and_publishers_packs_never_pass_as_a_book(tmp_path):
    lib = libation(tmp_path, [
        dict(SPELL_BREAKER_5, asin="B0BOXSET01", title="Spell Breaker: Books 1-3"),
        dict(SPELL_BREAKER_5, asin="B0PUBPACK1", title="Arcane Academy: Publisher's Pack"),
        dict(SPELL_BREAKER_5, asin="B0OMNIBUS1", title="Spell Breaker Omnibus"),
    ])
    rc, ids, _ = run(lib, radar_data(tmp_path, [], series=[ARCANE]))
    assert ids == ["B0PUBPACK1"]                     # the series' own pack, by its NAME; never by an author guess
    books = {b["asin"]: b for b in pd.library_books(lib)}
    assert all(pd.match_book(b, [pd.series_entry(ARCANE, n=5)]) is None for b in books.values())   # and never "book 5"


def test_libations_author_strings_are_split_like_the_radars(tmp_path):
    """Audible prints 'K. Georgiades aka PathOfPen' as ONE author and Libation
    stores it so; the radar shows the same string. Both sides split."""
    lone = {"name": "The Lone Wanderer", "author": "by K. Georgiades aka PathOfPen",
            "timeline": [{"n": 6, "audiobook": "2026-10-01", "title": "The Lone Wanderer 6: A LitRPG Adventure"}]}
    lib = libation(tmp_path, [{"asin": "B0LONE0006", "title": "The Lone Wanderer 6", "series": None, "order": None,
                               "status": 0, "authors": ["K. Georgiades aka PathOfPen"], "published": "2026-10-01 12:00:00"}])
    rc, ids, _ = run(lib, radar_data(tmp_path, [], series=[lone]))
    assert ids == ["B0LONE0006"]
    books = pd.library_books(lib)
    assert books[0]["authors"] >= {"k georgiades", "pathofpen", "k georgiades aka pathofpen"}
    assert pd.match_book(books[0], [pd.series_entry(dict(lone, author="PathOfPen"), n=6)]) is not None


def test_a_book_with_no_author_on_the_radar_side_only_matches_by_series_rules(tmp_path):
    rc, ids, _ = run(libation(tmp_path, [SPELL_BREAKER_5]), radar_data(tmp_path, [], series=[dict(ARCANE, author="")]))
    assert ids == []


def test_a_foreign_series_name_needs_kinship(tmp_path):
    """Audible files Fire and Song under 'Stormweaver Series'; the radar calls
    the series 'Warformed: Stormweaver'. Same author, known title, one name
    inside the other: taken. An unrelated series name on the record: not."""
    storm = {"name": "Warformed: Stormweaver", "author": "Bryce O'Connor",
             "novel_title": "Fire and Song (Warformed: Stormweaver Book 2)", "timeline": []}
    lib = libation(tmp_path, [
        {"asin": "B0CC3335TK", "title": "Fire and Song", "series": "Stormweaver Series", "order": "2", "status": 0,
         "authors": ["Bryce O'Connor"], "published": "2023-10-31 00:00:00"},
        {"asin": "B0ELSEWHER", "title": "Fire and Song", "series": "Campfire Tales", "order": "1", "status": 0,
         "authors": ["Bryce O'Connor"], "published": "2023-10-31 00:00:00"},
    ])
    rc, ids, err = run(lib, radar_data(tmp_path, [], series=[storm]))
    assert ids == ["B0CC3335TK"] and "Warformed: Stormweaver (by author + title)" in err


@pytest.mark.parametrize("author,keys", [
    ("by A.P. Gore", {"a p gore"}),
    ("K. Georgiades aka PathOfPen", {"k georgiades", "pathofpen"}),
    ("TheFirstDefier, see all", {"thefirstdefier"}),
    ("Bryce O'Connor", {"bryce o connor"}),
    ("A. F. Kay", {"a f kay"}),
    ("A.P. Gore and Patricia Jones", {"a p gore", "patricia jones"}),
    ("", set()),
    (None, set()),
    (["not", "a", "string"], set()),
])
def test_author_spellings_normalise_to_libations(author, keys):
    assert pd.author_keys(author) == keys


@pytest.mark.parametrize("title,keys", [
    ("Spell Breaker: A Progression Fantasy Adventure (Arcane Academy Book 5)", {"spell breaker", "spell breaker a progression fantasy adventure"}),
    ("Spell Breaker", {"spell breaker"}),
    ("Loopbreaker: Book 1: A Time Loop LitRPG", {"loopbreaker", "loopbreaker book 1 a time loop litrpg"}),
    ("RE: Maelstrom", {"re maelstrom"}),                              # the 2-letter head is dropped
    (None, set()),
    (42, set()),
])
def test_title_keys(title, keys):
    assert pd.title_keys(title) == keys


@pytest.mark.parametrize("order,n,covers", [
    ("4", 4, True), ("4", "4", True), (4, 4.0, True), ("4.5", 4, False), ("1-3", 2, True), ("1-3", 4, False),
    ("1 - 3", 3, True), ("", 1, False), (None, 1, False), ("-1", 1, False), ("abc", 1, False),
])
def test_audibles_free_text_order(order, n, covers):
    assert pd._order_covers(order, n) is covers


# ---------------------------------------------------------------- asked about ONE book (the app's question)

def test_match_book_for_one_numbered_book_is_what_the_app_asks(tmp_path):
    """The app asks 'is book n in Libation?' - only that book's title, date and
    ID count, and a bare 'Riftside 3' must not pass for book 4."""
    lib = libation(tmp_path, [SPELL_BREAKER_5,
                              ("B0RIFT0003", "Riftside 3", None, None, 1),
                              ("B0RIFT0004", "Riftside 4: A LitRPG Fantasy Adventure", None, None, 0)])
    books = {b["asin"]: b for b in pd.library_books(lib, pending_only=False)}
    assert pd.match_book(books["B0HLV5JPC9"], [pd.series_entry(ARCANE, n=5)]) == ("Arcane Academy", "Arcane Academy (by author + title)")
    assert pd.match_book(books["B0HLV5JPC9"], [pd.series_entry(ARCANE, n=6)]) is None            # book 6's title is the same, its date isn't out
    assert pd.match_book(books["B0HLV5JPC9"], [pd.series_entry(ARCANE, n=4)]) is None
    rift = {"name": "Riftside", "author": "by Cassius Lange"}
    assert pd.match_book(books["B0RIFT0004"], [pd.series_entry(rift, n=4)]) == ("Riftside", "Riftside")
    assert pd.match_book(books["B0RIFT0003"], [pd.series_entry(rift, n=4)]) is None
    assert pd.match_book(books["B0RIFT0003"], [pd.series_entry(rift, n=3)]) == ("Riftside", "Riftside")
    assert books["B0RIFT0003"]["status"] == 1 and books["B0HLV5JPC9"]["authors"] == {"a p gore", "patricia jones"}


def test_audibles_own_number_beats_a_shared_title(tmp_path):
    """Book 5 on the shelf, filed by Audible as Arcane Academy #5, must never
    pass for book 6 - not even when 6 comes out the same day."""
    twin_day = dict(ARCANE, timeline=[dict(e, audiobook="2026-10-02") if e["n"] == 6 else e for e in ARCANE["timeline"]])
    lib = libation(tmp_path, [dict(SPELL_BREAKER_5, series="Arcane Academy", order="5")])
    book = pd.library_books(lib)[0]
    assert pd.match_book(book, [pd.series_entry(twin_day, n=5)]) == ("Arcane Academy", "Arcane Academy")
    assert pd.match_book(book, [pd.series_entry(twin_day, n=6)]) is None
    assert pd.match_book(book, [pd.series_entry(twin_day)]) is not None       # the picker still knows the series


def test_same_day_twins_with_the_same_title_are_unknowable_so_not_owned(tmp_path):
    twin_day = dict(ARCANE, timeline=[dict(e, audiobook="2026-10-02") if e["n"] == 6 else e for e in ARCANE["timeline"]])
    book = pd.library_books(libation(tmp_path, [SPELL_BREAKER_5]))[0]               # no series on the record
    assert pd.match_book(book, [pd.series_entry(twin_day, n=6)]) is None
    assert pd.match_book(book, [pd.series_entry(twin_day, n=5)]) is None             # can't tell 5 from 6 either
    assert pd.match_book(book, [pd.series_entry(twin_day)]) is not None              # the picker: a tracked book, fine


def test_a_numbered_prefix_stops_at_the_number(tmp_path):
    rift = {"name": "Riftside", "author": "by Cassius Lange"}
    lib = libation(tmp_path, [
        {"asin": "B0RIFT0045", "title": "Riftside 4.5: Interlude", "series": None, "order": None, "status": 0, "authors": ["Cassius Lange"]},
        {"asin": "B0RIFT0406", "title": "Riftside 4-6 Box Set", "series": None, "order": None, "status": 0, "authors": ["Cassius Lange"]},
        {"asin": "B0RIFT0004", "title": "Riftside 4: A LitRPG", "series": None, "order": None, "status": 0, "authors": ["Cassius Lange"]},
        {"asin": "B0RIFTSIDR", "title": "Riftsiders", "series": None, "order": None, "status": 0, "authors": ["Someone Else"]},
        {"asin": "B0CHRYSAL1", "title": "Chrysalis: A Novel", "series": None, "order": None, "status": 0, "authors": ["Someone Else"]},
        {"asin": "B0CHRYSAL2", "title": "Chrysalis 7: Antvance", "series": None, "order": None, "status": 0, "authors": ["RinoZ"]},
    ])
    books = {b["asin"]: b for b in pd.library_books(lib)}
    assert pd.match_book(books["B0RIFT0045"], [pd.series_entry(rift, n=4)]) is None
    assert pd.match_book(books["B0RIFT0406"], [pd.series_entry(rift, n=4)]) is None
    assert pd.match_book(books["B0RIFT0004"], [pd.series_entry(rift, n=4)]) == ("Riftside", "Riftside")
    rc, ids, _ = run(lib, radar_data(tmp_path, [], series=[rift, {"name": "Chrysalis", "author": "by RinoZ"}]))
    # the picker: Riftside's novella and box set ARE Riftside; "Riftsiders" and another author's "Chrysalis" are not ours
    assert ids == ["B0RIFT0045", "B0RIFT0406", "B0RIFT0004", "B0CHRYSAL2"]


def test_a_range_order_covers_its_books(tmp_path):
    lib = libation(tmp_path, [("B0RIFTBOX1", "Riftside: Books 1-3", "Riftside", "1-3", 1)])
    book = pd.library_books(lib, pending_only=False)[0]
    rift = {"name": "Riftside"}
    assert pd.match_book(book, [pd.series_entry(rift, n=2)]) == ("Riftside", "Riftside")
    assert pd.match_book(book, [pd.series_entry(rift, n=4)]) is None


def test_malformed_radar_records_do_not_crash_the_matcher():
    bad = {"name": "X", "author": ["list"], "timeline": [{"n": 1, "title": 7, "audiobook": 3}, "junk"], "asins_audio": ["list"],
           "history": "nope", "novel_title": {"d": 1}}
    e = pd.series_entry(bad)
    assert e["authors"] == set() and e["titles"] == set() and e["asins"] == set()
    assert pd.series_entry(bad, n=1)["dates"] == {"3"} and pd.series_entry("junk")["name"] == ""
