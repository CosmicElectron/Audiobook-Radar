"""Embedded-metadata readers: metadata.json (Audiobookshelf), m4b ilst tags, .nfo."""
import json
import os
import struct

import file_audiobooks as fa
import tidy_library as tl


def atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), name) + payload


def data_atom(text: str, kind: int = 1) -> bytes:
    return atom(b"data", struct.pack(">II", kind, 0) + text.encode("utf-8"))


def freeform(name: str, value: str) -> bytes:
    return atom(b"----", atom(b"mean", b"\0\0\0\0com.apple.iTunes")
                + atom(b"name", b"\0\0\0\0" + name.encode()) + data_atom(value))


def make_m4b(path, title, album=None, series=None, part=None, big_mdat=False):
    ilst = atom(b"\xa9nam", data_atom(title))
    if album:
        ilst += atom(b"\xa9alb", data_atom(album))
    if series:
        ilst += freeform("SERIES", series)
    if part:
        ilst += freeform("PART", part)
    meta = atom(b"meta", b"\0\0\0\0" + atom(b"hdlr", b"\0" * 24) + atom(b"ilst", ilst))
    moov = atom(b"moov", atom(b"mvhd", b"\0" * 100) + atom(b"udta", meta))
    ftyp = atom(b"ftyp", b"M4B \0\0\0\0isomiso2")
    mdat = atom(b"mdat", b"\0" * (70000 if big_mdat else 16))
    with open(path, "wb") as f:
        f.write(ftyp + (mdat + moov if big_mdat else moov + mdat))   # moov at end or start


def test_m4b_tags_standard_and_freeform(tmp_path):
    p = tmp_path / "x.m4b"
    make_m4b(p, "The Primal Hunter 14", album="The Primal Hunter 14 (Unabridged)",
             series="The Primal Hunter", part="14", big_mdat=True)
    t = fa._m4b_tags(str(p))
    assert t["title"] == "The Primal Hunter 14" and t["series"] == "The Primal Hunter" and t["part"] == "14"
    m = fa._m4b_meta(str(tmp_path))
    assert (m["series"], m["n"], m["source"]) == ("The Primal Hunter", "14", "m4b tags")


def test_m4b_number_from_title(tmp_path):
    make_m4b(tmp_path / "y.m4b", "Shattered Boundaries: Ultimate Level 1, Book 3")
    assert fa._m4b_meta(str(tmp_path))["n"] == "3"


def test_m4b_garbage_is_harmless(tmp_path):
    (tmp_path / "z.m4b").write_bytes(b"not an mp4 at all" * 10)
    assert fa._m4b_tags(str(tmp_path / "z.m4b")) == {}
    assert fa._m4b_meta(str(tmp_path)) is None


def test_metadata_json_forms(tmp_path):
    d = tmp_path / "b"; d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"title": "New Dreams", "subtitle": "Ultimate Level 1, Book 4",
                                                "series": ["Ultimate Level 1 #4"]}), encoding="utf-8")
    m = fa._json_meta(str(d))
    assert (m["series"], m["n"], m["title"]) == ("Ultimate Level 1", "4", "New Dreams")
    (d / "metadata.json").write_text(json.dumps({"title": "T", "series": [{"name": "S", "sequence": "8.5"}]}))
    assert fa._json_meta(str(d))["n"] == "8.5"
    (d / "metadata.json").write_text(json.dumps({"title": "Pack", "series": ["Mage Errant #1-2"]}))
    assert fa._json_meta(str(d))["n"] == "1-2"
    (d / "metadata.json").write_text(json.dumps({"title": "Ultimate Truths", "series": ["Ultimate Level 1"]}))
    assert fa._json_meta(str(d))["n"] is None
    (d / "metadata.json").write_text("{not json")
    assert fa._json_meta(str(d)) is None


def test_nfo_forms(tmp_path):
    d = tmp_path / "b"; d.mkdir()
    (d / "x.nfo").write_text("General Information\n Title:   Blessed Time 2: Coda: A LitRPG Adventure\n"
                             " Series:  Blessed Time, Book 2\n Author: C\n", encoding="utf-8")
    m = fa._nfo_meta(str(d))
    assert m["n"] == "2" and m["series"] == "Blessed Time, Book 2"
    (d / "x.nfo").write_bytes(b"\xe2\x96\x88" * 50 + b"\n scene release \n Solo Leveling, Vol. 4 - Solo Leveling, Book 4\n")
    assert fa._nfo_meta(str(d))["n"] == "4"


def test_book_metadata_order(tmp_path):
    d = tmp_path / "b"; d.mkdir()
    make_m4b(d / "a.m4b", "Title: Series, Book 7")
    (d / "a.nfo").write_text("Title: Something, Book 9")
    assert fa.book_metadata(str(d))["n"] == "7"            # m4b beats nfo
    (d / "metadata.json").write_text(json.dumps({"title": "T", "series": ["Series #3"]}))
    assert fa.book_metadata(str(d))["n"] == "3"            # json beats both
    (d / "metadata.json").write_text(json.dumps({"title": "T", "series": ["Series"]}))   # json without a number
    assert fa.book_metadata(str(d))["n"] == "7"            # falls through to the m4b


def test_tidy_uses_embedded_numbers_but_not_for_other_series(tmp_path):
    lib = tmp_path / "AudioBooks"
    d = lib / "Shawn Wilson" / "Ultimate Level 1" / "New Dreams"; d.mkdir(parents=True)
    make_m4b(d / "New Dreams Ultimate Level 1, Book 4.m4b", "New Dreams: Ultimate Level 1, Book 4")
    (d / "metadata.json").write_text(json.dumps({"title": "New Dreams", "series": ["Ultimate Level 1 #4"]}))
    other = lib / "Someone" / "Their Series" / "Loose Title"; other.mkdir(parents=True)
    (other / "metadata.json").write_text(json.dumps({"title": "Loose Title", "series": ["A Different Series #2"]}))
    make_m4b(other / "x.m4b", "Loose Title")
    plan = tl.build_plan(str(lib), db_path="nope.db")
    steps = {os.path.basename(s["src"]): s for s in plan["steps"]}
    assert os.path.basename(steps["New Dreams"]["dst"]) == "New Dreams - Book 4"
    assert "metadata.json" in steps["New Dreams"]["why"]
    assert os.path.basename(steps["New Dreams Ultimate Level 1, Book 4.m4b"]["dst"]) == "New Dreams - Book 4.m4b"
    assert "Loose Title" not in steps                       # number belongs to another series: ignored
    assert any("Loose Title" in n["path"] for n in plan["notes"])
