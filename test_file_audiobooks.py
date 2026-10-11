"""Tests for file_audiobooks.py — built from real shapes in a real library."""
import os

import pytest

import file_audiobooks as fa


def mk(root, *parts, files=("book.m4b",)):
    p = os.path.join(root, *parts)
    os.makedirs(p, exist_ok=True)
    for f in files:
        with open(os.path.join(p, f), "wb") as fh:
            fh.write(b"x")
    return p


@pytest.fixture
def lib(tmp_path):
    dst = tmp_path / "AudioBooks"
    src = tmp_path / "Libation Audiobooks"
    dst.mkdir(); src.mkdir()
    d = str(dst)
    # real shapes
    mk(d, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 14")
    mk(d, "Zogarth", "The Primal Hunter", "The Primal Hunter 11 - Book 11")
    mk(d, "Shirtaloon, Travis Deverell", "He Who Fights with Monsters", "He Who Fights with Monsters - Book 12")
    mk(d, "TurtleMe", "The Beginning After the End", "The Beginning After the End - Book 1")
    mk(d, "TurtleMe", "The Beginning After the End", "The Beginning After the End - Book 2")
    mk(d, "TurtleMe", "The Beginning After The End Series", "The Beginning After the End - Book 9")
    mk(d, "Ascendant", "Ascendant", "Ascendant - Book 1")
    mk(d, "Craig Alanson", "Ascendant", "Deceptions - Book 1")
    mk(d, "EmergencyComplaints", "Ascendant", "Ascendant - A Progression Fantasy - Book 1")
    mk(d, "K. Georgiades aka PathOfPen", "The Lone Wanderer", "Lord of the Fungal Spire - Book 5")
    mk(d, "Matt Dinniman", "Dungeon Crawler Carl", "Dungeon Crawler Carl - Book 1")
    mk(d, "!Smart AudioBook Player Statistics", files=(".nomedia",))
    return str(src), d


def run(src, dst, dry=False):
    out = []
    res = fa.file_all(src, dst, dry_run=dry, log=out.append)
    return res, "\n".join(out)


def test_nothing_to_do(lib):
    src, dst = lib
    res, _ = run(src, dst)
    assert res == []


def test_exact_series_same_author(lib):
    src, dst = lib
    mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 15")
    res, _ = run(src, dst)
    assert [r.action for r in res] == ["FILED"]
    assert os.path.isfile(os.path.join(dst, "Zogarth", "The Primal Hunter",
                                       "The Primal Hunter - Book 15", "The Primal Hunter - Book 15.m4b"))
    assert not os.path.exists(os.path.join(src, "Zogarth"))   # pruned


def test_author_subset_and_case(lib):
    """Libation lists only 'Shirtaloon'; series cased differently."""
    src, dst = lib
    mk(src, "Shirtaloon", "He Who Fights With Monsters", "He Who Fights with Monsters - Book 13")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert res[0].target == os.path.join(dst, "Shirtaloon, Travis Deverell",
                                         "He Who Fights with Monsters",
                                         "He Who Fights with Monsters - Book 13")


def test_series_suffix_goes_to_fuller_folder(lib):
    """'... Series' normalises to the canonical folder; picks the one with more books."""
    src, dst = lib
    mk(src, "TurtleMe", "The Beginning After The End Series", "The Beginning After the End - Book 10")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert res[0].target.startswith(os.path.join(dst, "TurtleMe", "The Beginning After the End" + os.sep))


def test_series_name_collision_is_author_resolved(lib):
    src, dst = lib
    mk(src, "Craig Alanson", "Ascendant", "Transcendent - Book 2")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert os.path.join("Craig Alanson", "Ascendant") in res[0].target


def test_series_name_collision_unknown_author_is_held(lib):
    src, dst = lib
    mk(src, "Somebody Else", "Ascendant", "Ascendant - Book 3")
    res, text = run(src, dst)
    assert res[0].action == "HELD"
    assert "ambiguous" in res[0].reason
    assert os.path.isdir(os.path.join(src, "Somebody Else", "Ascendant", "Ascendant - Book 3"))
    assert "HOLD" in text


def test_distinctive_unique_series_with_odd_author_files_with_flag(lib):
    """Audible sometimes credits a translator/co-author only."""
    src, dst = lib
    mk(src, "Totally Different Name", "Dungeon Crawler Carl", "Carl's Doomsday Scenario - Book 2")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert "author differs" in res[0].reason
    assert os.path.join("Matt Dinniman", "Dungeon Crawler Carl") in res[0].target


def test_aka_author(lib):
    src, dst = lib
    mk(src, "K. Georgiades", "The Lone Wanderer", "Clash of the Seventh Circle - Book 6")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert os.path.join("K. Georgiades aka PathOfPen", "The Lone Wanderer") in res[0].target


def test_never_overwrite_existing_book(lib):
    src, dst = lib
    mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter 14 - Book 14")   # same book, new name
    res, _ = run(src, dst)
    assert res[0].action == "HELD"
    assert "already in library" in res[0].reason
    # original untouched, new copy still in src
    assert os.path.isfile(os.path.join(dst, "Zogarth", "The Primal Hunter",
                                       "The Primal Hunter - Book 14", "book.m4b"))
    assert os.path.isfile(os.path.join(src, "Zogarth", "The Primal Hunter",
                                       "The Primal Hunter 14 - Book 14", "book.m4b"))


def test_new_series_known_author_consolidates(lib):
    src, dst = lib
    mk(src, "Shirtaloon", "Brand New Thing", "Brand New Thing - Book 1")
    res, _ = run(src, dst)
    assert res[0].action == "NEW"
    assert res[0].target == os.path.join(dst, "Shirtaloon, Travis Deverell", "Brand New Thing",
                                         "Brand New Thing - Book 1")


def test_new_series_new_author(lib):
    src, dst = lib
    mk(src, "Fresh Author", "Fresh Series", "Fresh Series - Book 1")
    res, _ = run(src, dst)
    assert res[0].action == "NEW"
    assert os.path.isfile(os.path.join(dst, "Fresh Author", "Fresh Series", "Fresh Series - Book 1",
                                       "Fresh Series - Book 1.m4b"))   # file named like its folder


def test_no_series_metadata(lib):
    src, dst = lib
    mk(src, "Zogarth", "Standalone Thing - Book")
    res, _ = run(src, dst)
    assert res[0].action in ("FILED", "NEW")
    assert res[0].target == os.path.join(dst, "Zogarth", "Standalone Thing - Book")
    assert os.path.isfile(os.path.join(res[0].target, "Standalone Thing - Book.m4b"))


def test_two_new_books_same_new_series_share_folder(lib):
    src, dst = lib
    mk(src, "Fresh Author", "Fresh Series", "Fresh Series - Book 1")
    mk(src, "Fresh Author", "Fresh Series", "Fresh Series - Book 2")
    res, _ = run(src, dst)
    assert sorted(r.action for r in res) == ["FILED", "NEW"]   # 2nd sees the folder made by the 1st
    assert len(os.listdir(os.path.join(dst, "Fresh Author", "Fresh Series"))) == 2


def test_dry_run_moves_nothing(lib):
    src, dst = lib
    p = mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 15")
    res, text = run(src, dst, dry=True)
    assert res[0].action == "FILED"
    assert os.path.isdir(p)
    assert not os.path.exists(os.path.join(dst, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 15"))


def test_missing_library_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        fa.file_all(str(tmp_path / "src"), str(tmp_path / "nope"))


def test_cli_exit_codes(lib, capsys):
    src, dst = lib
    assert fa.main(["--src", src, "--dst", dst]) == 0
    mk(src, "Somebody Else", "Ascendant", "Ascendant - Book 3")
    assert fa.main(["--src", src, "--dst", dst]) == 2
    out = capsys.readouterr().out
    assert "held for you" in out and "Held books stay in" in out


@pytest.mark.parametrize("a,b", [
    ("The Beginning After The End Series", "The Beginning After the End"),
    ("He Who Fights With Monsters", "He Who Fights with Monsters"),
    ("The Saga of Tanya the Evil (light novel)", "Saga of Tanya the Evil"),
    ("Dungeon Crawler Carl Series", "Dungeon Crawler Carl"),
])
def test_norm_equivalences(a, b):
    assert fa.norm(a) == fa.norm(b)


def test_norm_keeps_distinct():
    assert fa.norm("Ascendant") != fa.norm("Ascendant 2")
    assert fa.norm("Primal Hunter") == fa.norm("The Primal Hunter")


@pytest.mark.parametrize("a,b,lvl", [
    ("Shirtaloon", "Shirtaloon, Travis Deverell", 2),
    ("K. Georgiades", "K. Georgiades aka PathOfPen", 3),   # aka-part stripped
    ("J.M. Clarke", "J. M. Clarke", 3),                       # punctuation-insensitive
    ("Zogarth", "Zogarth", 3),
    ("Matt Dinniman", "Craig Alanson", 0),
])
def test_author_levels(a, b, lvl):
    assert fa.author_level(a, b) == lvl


def test_schema_applied_on_arrival(lib):
    """Libation's 'Series 15 - Book 15' lands as 'Series - Book 15', file renamed to match;
    a real title with a number keeps it."""
    src, dst = lib
    mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter 15 - Book 15", files=("The Primal Hunter 15 - Book 15.m4b",))
    mk(src, "Cerim", "Hell Difficulty", "Hell Difficulty Tutorial 6 - Book 6", files=("x.m4b",))
    res, _ = run(src, dst)
    by = {r.book: r for r in res}
    ph = by["The Primal Hunter 15 - Book 15"]
    assert ph.target == os.path.join(dst, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 15")
    assert os.path.isfile(os.path.join(ph.target, "The Primal Hunter - Book 15.m4b"))
    assert "renamed" in ph.reason
    hd = by["Hell Difficulty Tutorial 6 - Book 6"]
    assert hd.target.endswith(os.path.join("Hell Difficulty", "Hell Difficulty Tutorial 6 - Book 6"))
    assert os.path.isfile(os.path.join(hd.target, "Hell Difficulty Tutorial 6 - Book 6.m4b"))


def test_schema_rename_respects_existing_book(lib):
    """'The Primal Hunter 14 - Book 14' arriving when 'The Primal Hunter - Book 14' exists -> HELD."""
    src, dst = lib
    mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter 14 - Book 14")
    res, _ = run(src, dst)
    assert res[0].action == "HELD"


@pytest.mark.parametrize("folder,series,want", [
    ("The Primal Hunter 12 - Book 12", "The Primal Hunter", "The Primal Hunter - Book 12"),
    ("He Who Fights with Monsters 10 - Book 10", "He Who Fights with Monsters", "He Who Fights with Monsters - Book 10"),
    ("Nightmare Realm Summoner 2 - Book 2", "Nightmare Realm Summoner", "Nightmare Realm Summoner - Book 2"),
    ("The Primal Hunter 7 - A LitRPG Adventure - Book 7", "The Primal Hunter", "The Primal Hunter - Book 7"),
    ("Hell Difficulty Tutorial 6 - Book 6", "Hell Difficulty", "Hell Difficulty Tutorial 6 - Book 6"),
    ("Carl's Doomsday Scenario - Book 2", "Dungeon Crawler Carl", "Carl's Doomsday Scenario - Book 2"),
    ("Beware of chicken - Book2", "Beware of chicken", "Beware of chicken - Book 2"),
    ("The Beginning After the End - Book 8.5", "The Beginning After the End", "The Beginning After the End - Book 8.5"),
    ("Beckoning Fates, Horizon's Edge - Book 3-4", "The Beginning After the End", "Beckoning Fates, Horizon's Edge - Book 3-4"),
    ("Rise to Omniscience - Books 1-5", "Rise to Omniscience", "Rise to Omniscience - Book 1-5"),
    ("Volume 08 - In Omnia Paratus {Erin Rieman}", "The Saga of Tanya the Evil", "In Omnia Paratus - Book 8"),
    ("This Inevitable Ruin꞉ Dungeon Crawler Carl, Book 7 [B0DK282SYV]", "Dungeon Crawler Carl", "This Inevitable Ruin - Book 7"),
    ("Becoming a God Made Easy - Theos, Book 1", "Theos", "Becoming a God Made Easy - Book 1"),
    ("The Saga of Tanya the Evil, Vol. 3 - Book 3", "The Saga of Tanya the Evil", "The Saga of Tanya the Evil - Book 3"),
    ("Some Standalone", "Whatever", None),
])
def test_schema_book_name(folder, series, want):
    assert fa.schema_book_name(folder, series) == want



def _mk_m4b(path, title, artist, series=None, part=None):
    """A tiny real MP4 with iTunes tags (same builder as test_book_metadata)."""
    from test_book_metadata import make_m4b
    make_m4b(path, title, album=title, series=series, part=part)
    # make_m4b has no artist arg: append an artist atom by rewriting with the helper pieces
    import struct
    def atom(name, payload): return struct.pack(">I4s", 8 + len(payload), name) + payload
    def data_atom(text): return atom(b"data", struct.pack(">II", 1, 0) + text.encode("utf-8"))
    raw = open(path, "rb").read()
    # insert an (c)ART atom into the ilst by rebuilding: easier to just rebuild fully
    ilst = atom(b"\xa9nam", data_atom(title)) + atom(b"\xa9ART", data_atom(artist)) + atom(b"\xa9alb", data_atom(title))
    if series: ilst += atom(b"----", atom(b"mean", b"\0\0\0\0com.apple.iTunes") + atom(b"name", b"\0\0\0\0SERIES") + data_atom(series))
    if part: ilst += atom(b"----", atom(b"mean", b"\0\0\0\0com.apple.iTunes") + atom(b"name", b"\0\0\0\0PART") + data_atom(part))
    meta = atom(b"meta", b"\0\0\0\0" + atom(b"hdlr", b"\0" * 24) + atom(b"ilst", ilst))
    moov = atom(b"moov", atom(b"mvhd", b"\0" * 100) + atom(b"udta", meta))
    open(path, "wb").write(atom(b"ftyp", b"M4B \0\0\0\0isomiso2") + moov + atom(b"mdat", b"\0" * 16))


def test_loose_file_adopted_from_tags(lib):
    """'ThePrimalHunter15.m4b' dropped at the Libation root, tags say
    'The Primal Hunter 15: A LitRPG Adventure' by Zogarth -> filed as Book 15."""
    src, dst = lib
    _mk_m4b(os.path.join(src, "ThePrimalHunter15.m4b"), "The Primal Hunter 15: A LitRPG Adventure", "Zogarth")
    res, text = run(src, dst)
    assert [r.action for r in res] == ["FILED"]
    assert os.path.isfile(os.path.join(dst, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 15",
                                       "The Primal Hunter - Book 15.m4b"))
    assert "ADOPT" in text and not os.listdir(src)


def test_loose_file_with_series_tag_and_titled_book(lib):
    src, dst = lib
    _mk_m4b(os.path.join(src, "whatever.m4b"), "Clash of the Seventh Circle", "K. Georgiades", series="The Lone Wanderer", part="6")
    res, _ = run(src, dst)
    assert res[0].action == "FILED"
    assert res[0].target.endswith(os.path.join("The Lone Wanderer", "Clash of the Seventh Circle - Book 6"))


def test_loose_file_unplaceable_is_held(lib):
    src, dst = lib
    _mk_m4b(os.path.join(src, "mystery.m4b"), "Some Unknown Book", "Nobody Known")
    res, _ = run(src, dst)
    assert res[0].action == "HELD" and "can't tell which series" in res[0].reason
    assert os.path.isfile(os.path.join(src, "mystery.m4b"))
    # already in the library -> held too
    _mk_m4b(os.path.join(src, "dup.m4b"), "The Primal Hunter 14", "Zogarth")
    res, _ = run(src, dst)
    held = [r for r in res if r.book == "dup.m4b"]
    assert held and held[0].action == "HELD" and "already has Book 14" in held[0].reason


def test_aliases_teach_the_filer(lib, tmp_path):
    """A taught author alias reroutes what used to be a HOLD."""
    src, dst = lib
    aliases = {"authors": {fa.norm("Somebody Else"): "Craig Alanson"}, "series": {}}
    mk(src, "Somebody Else", "Ascendant", "Ascendant - Book 3")
    res, _ = run_with_aliases(src, dst, aliases)
    assert res[0].action == "FILED"
    assert os.path.join("Craig Alanson", "Ascendant") in res[0].target


def run_with_aliases(src, dst, aliases):
    out = []
    res = fa.file_all(src, dst, dry_run=False, log=out.append, aliases=aliases)
    return res, "\n".join(out)


def test_alias_save_and_load(tmp_path):
    p = str(tmp_path / "aliases.json")
    fa.save_alias("authors", "Somebody Else", "Craig Alanson", path=p)
    fa.save_alias("series", "Ascendant Saga", "Ascendant", path=p)
    d = fa.load_aliases(p)
    assert d["authors"][fa.norm("Somebody Else")] == "Craig Alanson"
    assert d["series"][fa.norm("Ascendant Saga")] == "Ascendant"
    assert fa.load_aliases(str(tmp_path / "missing.json")) == {"authors": {}, "series": {}}


# ---------------------------------------------------------------- the files know what the folder doesn't (7 Oct 2026)
# Arcane Academy 5 came out of Libation as "A.P. Gore, Patricia Jones\Spell
# Breaker - Book": Audible's library record carried no series, so Libation's
# folder template had nothing to fill in. Its metadata.json said
# "Arcane Academy #5" all along, and the filer never looked.

def test_embedded_metadata_names_the_series_and_number(lib):
    import json
    src, dst = lib
    mk(dst, "A.P. Gore, Patricia Jones", "Arcane Academy", "Spell Breaker - Book 4")
    p = mk(src, "A.P. Gore, Patricia Jones", "Spell Breaker - Book", files=("Spell Breaker - Book.m4b",))
    with open(os.path.join(p, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "Spell Breaker - Book", "subtitle": "A Progression Fantasy Adventure (Arcane Academy, Book 5)",
                   "authors": ["A.P. Gore", "Patricia Jones"], "series": ["Arcane Academy #5"], "asin": "B0HLV5JPC9"}, f)
    res, out = run(src, dst)
    assert res[0].action == "FILED"
    assert res[0].target == os.path.join(dst, "A.P. Gore, Patricia Jones", "Arcane Academy", "Spell Breaker - Book 5")
    assert os.path.isfile(os.path.join(res[0].target, "Spell Breaker - Book 5.m4b"))
    assert "series from metadata.json, number from metadata.json" in res[0].reason


def test_embedded_number_fills_an_unnumbered_series_folder(lib):
    import json
    src, dst = lib
    p = mk(src, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book", files=("The Primal Hunter - Book.m4b",))
    with open(os.path.join(p, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "The Primal Hunter", "series": ["The Primal Hunter #15"]}, f)
    res, _ = run(src, dst)
    assert res[0].action == "FILED" and res[0].target.endswith(os.path.join("The Primal Hunter", "The Primal Hunter - Book 15"))
    assert "number from metadata.json" in res[0].reason


def test_metadata_that_matches_a_book_already_there_is_held(lib):
    import json
    src, dst = lib
    mk(dst, "A.P. Gore, Patricia Jones", "Arcane Academy", "Spell Breaker - Book 4")
    p = mk(src, "A.P. Gore, Patricia Jones", "Spell Breaker - Book", files=("Spell Breaker - Book.m4b",))
    with open(os.path.join(p, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "Spell Breaker", "series": ["Arcane Academy #4"]}, f)
    res, _ = run(src, dst)
    assert res[0].action == "HELD" and "already in library" in res[0].reason
    assert os.path.isdir(p)                                               # nothing moved
