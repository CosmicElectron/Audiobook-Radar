"""File freshly liberated audiobooks into the AudioBooks library - into the
series folder you ALREADY have, even when Libation spells the author or
series differently.

Called by "Update Audiobooks.bat" (step 3) in place of a blind robocopy.
Also importable: ``file_all(src, dst)`` returns a list of Result rows.

Layout (both sides):  <Author>\\<Series>\\<Title> - Book <n>\\<files>
Libation's author string and series string drift per book ("Shirtaloon" vs
"Shirtaloon, Travis Deverell"; "The Beginning After the End" vs
"... The End Series"), which is how a second folder for an existing series
appears. This script matches on normalised names, author-aware (the same
series name exists under several unrelated authors - e.g. three different
"Ascendant"s), and refuses to guess when it is genuinely ambiguous: those
books stay in the Libation folder with a printed reason, and the next run
retries them.

Rules (in priority order):
  1. Never overwrite. If the library already has that book folder (same
     number / same name) the new copy is HELD, not merged.
  2. Exact series match (normalised) + compatible author -> file there.
  3. Exact series match, unique in the library, distinctive (2+ words) but
     author looks different -> file there, flagged as "author differs".
  4. Close series match (>= 0.90 similarity) + same primary author -> file.
  5. Otherwise it is a new series: create it under the EXISTING author
     folder when the author is known (so authors stay consolidated), else a
     new author folder - i.e. exactly what robocopy used to do.
Nothing is deleted except now-empty directories left behind in the source.

Usage:
  python file_audiobooks.py [--src DIR] [--dst DIR] [--dry-run] [--quiet]
Exit code: 0 all filed (or nothing to do), 2 something was HELD, 1 error.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field

DEFAULT_SRC = r"T:\Movies and TV shows\Books\Libation Audiobooks"
DEFAULT_DST = r"T:\Movies and TV shows\Books\AudioBooks"

# Taught corrections ("this author string means that author folder"), written by
# the Release Radar dashboard when a HOLD is resolved. Keys are norm()ed.
ALIASES_PATH = os.path.join(os.environ.get("APPDATA", ""), "ReleaseRadar", "filer-aliases.json")
REPORT_PATH = os.path.join(os.environ.get("APPDATA", ""), "ReleaseRadar", "filer-report.json")


def load_aliases(path: str = None):
    import json as _json
    try:
        with open(path or ALIASES_PATH, encoding="utf-8") as f:
            d = _json.load(f)
        return {"authors": dict(d.get("authors") or {}), "series": dict(d.get("series") or {})}
    except (OSError, ValueError):
        return {"authors": {}, "series": {}}


def save_alias(kind: str, key: str, value: str, path: str = None) -> None:
    import json as _json
    p = path or ALIASES_PATH
    d = load_aliases(p)
    d.setdefault(kind, {})[norm(key)] = value
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        _json.dump(d, f, indent=1)

AUDIO_EXT = {".m4b", ".m4a", ".mp3", ".aax", ".aaxc", ".flac", ".ogg", ".opus"}
IGNORE_DIRS = {"!Smart AudioBook Player Statistics", "$RECYCLE.BIN",
               "System Volume Information", ".stfolder", "@eaDir"}
IGNORE_FILES = {"desktop.ini", "thumbs.db", ".ds_store", ".nomedia"}

BOOK_RE = re.compile(r"^(?P<title>.*?)\s*-\s*Book\s*(?P<n>\d+(?:\.\d+)?)?\s*$", re.I)
SERIES_SUFFIXES = ("series", "saga", "a litrpg adventure", "a litrpg saga",
                   "a litrpg series", "light novel", "the complete series")


# ----------------------------------------------------------------- normalise
def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def norm(s: str) -> str:
    """Lower-case, ascii, punctuation -> space, drop leading article and
    trailing 'series'-type words. 'The Beginning After The End Series' ->
    'beginning after the end'."""
    s = _ascii(s or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)            # "(light novel)"
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(the|a|an) ", "", s)
    changed = True
    while changed:
        changed = False
        for suf in SERIES_SUFFIXES:
            if s.endswith(" " + suf):
                s = s[: -len(suf) - 1].rstrip()
                changed = True
    return re.sub(r"\s+", " ", s).strip()


def author_parts(a: str) -> list[str]:
    """'Shirtaloon, Travis Deverell' -> ['shirtaloon', 'travis deverell']."""
    parts = re.split(r",|&| and |;|/", _ascii(a or "").lower())
    out = []
    for p in parts:
        p = re.sub(r"[^a-z0-9]+", " ", p).strip()
        p = re.sub(r"\b(aka|a k a)\b.*$", "", p).strip()  # 'K. Georgiades aka PathOfPen'
        if p:
            out.append(p)
    return out


def author_level(a: str, b: str) -> int:
    """3 identical, 2 same primary author / one author set contains the
    other, 1 primary authors very similar, 0 unrelated."""
    pa, pb = author_parts(a), author_parts(b)
    if not pa or not pb:
        return 0
    if pa == pb:
        return 3
    if pa[0] == pb[0] or set(pa) <= set(pb) or set(pb) <= set(pa):
        return 2
    if difflib.SequenceMatcher(None, pa[0], pb[0]).ratio() >= 0.88:
        return 1
    # initials vs full name: 'j m clarke' vs 'jm clarke'
    if pa[0].replace(" ", "") == pb[0].replace(" ", ""):
        return 2
    return 0


def series_ratio(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


# ------------------------------------------------------------ schema naming
# Book folder schema: "<Title> - Book <n>".  When the title is only the series
# name plus its number ("The Primal Hunter 12") the number is redundant with
# "Book 12" and is dropped -> "The Primal Hunter - Book 12".  Real titles that
# merely contain a number ("Hell Difficulty Tutorial 6", "Magic 2.0") keep it.
NUM_RE = re.compile(
    r"""^(?P<title>.*?)\s*(?:[-\u2013\u2014,:]\s*|\(\s*|\s+)?(?:Book|Books|Bk\.?|Vol\.?|Volume|Part)\s*"""
    r"""(?P<n>\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*\)?\s*(?P<tail>.*)$""", re.I)
TAGLINE_RE = re.compile(
    r"(?:\s*[-:\u2013\u2014]\s*(?:an?\s+)?|\s+an?\s+)(?:deck[- ]building\s+)?"
    r"(?:litrpg|gamelit|progression fantasy|cultivation|epic fantasy|"
    r"dungeon core|isekai|sci-fi|science fiction|fantasy)[^,]*$", re.I)
BRACKET_ASIN_RE = re.compile(r"\s*\[(?:B0[0-9A-Z]{8}|\d{10})\]\s*", re.I)
CURLY_RE = re.compile(r"\s*\{[^}]*\}\s*")                  # "{Erin Rieman}" tags
SERIES_IN_PAREN_RE = re.compile(r"\s*\([^)]*\bbook\s*\d[^)]*\)\s*", re.I)
LOOKALIKE_COLONS = "\u2236\ua789\uff1a\u0589"               # chars people use instead of ':'
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*' + LOOKALIKE_COLONS + ']')


def short_title(title: str, series: str | None) -> str:
    """Libation-ish <title short> plus the redundancy rule above."""
    t = (title or "").strip()
    for ch in LOOKALIKE_COLONS:
        t = t.replace(ch, ":")
    t = t.split(":", 1)[0].strip()                 # "Title: Subtitle" -> "Title"
    t = BRACKET_ASIN_RE.sub(" ", t)
    t = CURLY_RE.sub(" ", t)
    t = SERIES_IN_PAREN_RE.sub(" ", t)
    t = TAGLINE_RE.sub("", t).strip()               # "X - A LitRPG Adventure" -> "X"
    t = re.sub(r"\s+", " ", t).strip(" -\u2013\u2014,")
    if series:
        ns = norm(series)
        # when the title IS the series, use the series name minus any
        # trailing "(light novel)"-style qualifier the folder carries
        series_display = re.sub(r"\s*\([^)]*\)\s*$", "", series).strip() or series
        # trailing " - <series>" / ", <series>" (old template put the series
        # in the title): "Becoming a God Made Easy - Theos" -> "Becoming a God Made Easy"
        parts = re.split(r"\s*[-\u2013\u2014,]\s+", t)
        while len(parts) > 1 and norm(parts[-1]) == ns:
            parts.pop()
            t = " - ".join(parts) if len(parts) > 1 else parts[0]
        # trailing series words with no separator: "Unsouled Cradle" -> "Unsouled"
        words = t.split()
        k = len(series.split())
        if len(words) > k and norm(" ".join(words[-k:])) == ns:
            t = " ".join(words[:-k])
        m = re.match(r"^(?P<base>.*?)\s*[-:]?\s*(?P<n>\d+(?:\.\d+)?)$", t)
        if m:
            nb = norm(m.group("base"))
            # exact, or a near-typo of the series name ("All the Skill 2")
            if nb == ns or (nb and difflib.SequenceMatcher(None, nb, ns).ratio() >= 0.92):
                return series_display                # "The Primal Hunter 12" -> series name
        if norm(t) == ns:
            return series_display                    # same words, other casing/article
    return t


LEADING_NUM_RE = re.compile(r"^(?P<n>\d{1,2}(?:\.\d+)?)\s*[-.)]?\s+(?P<title>[^\d].*)$")


def parse_folder(name: str) -> tuple[str, str | None]:
    """'Whatever - Book 12' -> ('Whatever', '12'); '1 Unsouled Cradle' ->
    ('Unsouled Cradle', '1'); unnumbered -> (name, None)."""
    m = NUM_RE.match(name)
    if not m or not m.group("n"):
        m2 = LEADING_NUM_RE.match(name.strip())
        if m2:
            return m2.group("title").strip(), m2.group("n").lstrip("0") or "0"
        bare = re.sub(r"\s*[-,:\u2013\u2014]?\s*(?:Book|Vol\.?|Volume)\s*$", "", name.strip(), flags=re.I)
        return bare.strip() or name.strip(), None
    n = re.sub(r"\s*-\s*", "-", m.group("n"))
    n = re.sub(r"\b0+(\d)", r"\1", n)               # 08 -> 8 (keeps 8.5, 3-4)
    title = m.group("title").strip()
    tail = (m.group("tail") or "").strip(" -\u2013\u2014")
    if not title and tail:                           # "Volume 08 - In Omnia Paratus {x}"
        title = tail
    return title, n


def schema_book_name(folder: str, series: str | None) -> str | None:
    """Schema folder name for a book folder, or None when it can't be numbered."""
    title_part, n = parse_folder(folder)
    if n is None:
        return None
    title = short_title(title_part, series) if title_part else (series or "")
    if not title:
        title = series or title_part
    return ILLEGAL_RE.sub("", f"{title} - Book {n}").strip(" .")


# ---------------------------------------------------------- embedded metadata
# When a folder name carries no book number, the files inside usually do:
#   metadata.json  (Audiobookshelf)  "series": ["Ultimate Level 1 #4"], "subtitle"
#   the .m4b tags  (iTunes-style)    title "X: Series, Book 3"  or  SERIES + PART
#   a .nfo file    (text)            "Title: Blessed Time 2: Coda ..."
NUM_IN_TEXT_RE = re.compile(
    r"(?:Book|Books|Bk\.?|Vol\.?|Volume|Part|#)\s*(?P<n>\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)(?!\d)", re.I)


def _num_from_text(text: str) -> str | None:
    """'Ultimate Level 1, Book 4' -> '4'; 'Series #8.5' -> '8.5'; 'Book 1-2' -> '1-2'."""
    if not text:
        return None
    m = NUM_IN_TEXT_RE.search(text)
    if not m:
        return None
    n = re.sub(r"\s*-\s*", "-", m.group("n"))
    return re.sub(r"\b0+(\d)", r"\1", n)


def _json_meta(book_dir: str) -> dict | None:
    p = os.path.join(book_dir, "metadata.json")
    if not os.path.isfile(p):
        return None
    try:
        import json
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    out = {"source": "metadata.json", "title": d.get("title") or "", "series": "", "n": None}
    ser = d.get("series")
    if isinstance(ser, list) and ser:
        first = ser[0]
        if isinstance(first, dict):                     # newer ABS: {"name":..,"sequence":..}
            out["series"] = first.get("name") or ""
            out["n"] = str(first.get("sequence") or "").strip() or None
        else:                                           # "Name #4"
            m = re.match(r"^(?P<name>.*?)\s*#\s*(?P<n>\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?)\s*$", str(first))
            if m:
                out["series"], out["n"] = m.group("name").strip(), m.group("n")
            else:
                out["series"] = str(first)
    elif isinstance(d.get("seriesName"), str):
        out["series"] = d["seriesName"]
        out["n"] = str(d.get("sequence") or "").strip() or None
    if not out["n"]:
        out["n"] = _num_from_text(d.get("subtitle") or "") or _num_from_text(d.get("title") or "")
    return out


def _mp4_atoms(fh, start: int, end: int):
    """Yield (name, payload_start, payload_end) for the atoms in [start, end)."""
    import struct
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            return
        size, name = struct.unpack(">I4s", head)
        hdr = 8
        if size == 1:                                   # 64-bit size
            big = fh.read(8)
            if len(big) < 8:
                return
            size = struct.unpack(">Q", big)[0]
            hdr = 16
        elif size == 0:                                 # to end of file
            size = end - pos
        if size < hdr:
            return
        yield name, pos + hdr, min(pos + size, end)
        pos += size


def _m4b_tags(path: str) -> dict[str, str]:
    """Read iTunes-style tags (ilst) from an .m4b/.m4a without any library.
    Returns lowercase keys: title, album, artist, and freeform names such as
    series/part, plus mvn/mvi if present."""
    tags: dict[str, str] = {}
    std = {b"\xa9nam": "title", b"\xa9alb": "album", b"\xa9ART": "artist", b"aART": "album_artist",
           b"\xa9mvn": "movement", b"\xa9mvi": "movement_index", b"tvsh": "show", b"tvsn": "season",
           b"tves": "episode", b"\xa9grp": "grouping", b"soal": "sort_album", b"sonm": "sort_title"}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            def find(parent_start, parent_end, name):
                for nm, a, b in _mp4_atoms(fh, parent_start, parent_end):
                    if nm == name:
                        return a, b
                return None
            moov = find(0, size, b"moov")
            if not moov:
                return tags
            udta = find(*moov, b"udta")
            if not udta:
                return tags
            meta = find(*udta, b"meta")
            if not meta:
                return tags
            ilst = find(meta[0] + 4, meta[1], b"ilst")     # meta has a 4-byte version/flags
            if not ilst:
                return tags
            for nm, a, b in _mp4_atoms(fh, *ilst):
                def data_text(s0, s1):
                    for dn, da, db in _mp4_atoms(fh, s0, s1):
                        if dn == b"data" and db - da > 8:
                            fh.seek(da)
                            kind = int.from_bytes(fh.read(4), "big") & 0xFFFFFF
                            fh.read(4)
                            raw = fh.read(db - da - 8)
                            if kind in (1, 0):                 # UTF-8 text / implicit
                                return raw.decode("utf-8", "replace").strip("\x00 ")
                            if kind == 21 and len(raw) <= 8:   # integer
                                return str(int.from_bytes(raw, "big"))
                            if kind == 0 and len(raw) >= 4:
                                return str(int.from_bytes(raw[2:4], "big"))
                    return None
                if nm == b"----":
                    name = None
                    for cn, ca, cb in _mp4_atoms(fh, a, b):
                        if cn == b"name":
                            fh.seek(ca + 4)
                            name = fh.read(cb - ca - 4).decode("utf-8", "replace").strip("\x00 ").lower()
                    val = data_text(a, b)
                    if name and val:
                        tags.setdefault(name, val)
                elif nm in std:
                    val = data_text(a, b)
                    if val:
                        tags.setdefault(std[nm], val)
                elif nm in (b"trkn", b"disk"):
                    for dn, da, db in _mp4_atoms(fh, a, b):
                        if dn == b"data" and db - da >= 12:
                            fh.seek(da + 8)
                            raw = fh.read(db - da - 8)
                            if len(raw) >= 4:
                                tags.setdefault("track" if nm == b"trkn" else "disc",
                                                str(int.from_bytes(raw[2:4], "big")))
    except (OSError, ValueError):
        pass
    return tags


def _m4b_meta(book_dir: str) -> dict | None:
    files = sorted(f for f in os.listdir(book_dir) if os.path.splitext(f)[1].lower() in (".m4b", ".m4a"))
    if not files:
        return None
    t = _m4b_tags(os.path.join(book_dir, files[0]))
    if not t:
        return None
    out = {"source": "m4b tags", "title": t.get("title", ""), "series": "", "n": None}
    ser = t.get("series") or t.get("movement") or t.get("show") or ""
    part = t.get("part") or t.get("series-part") or t.get("movement_index") or t.get("episode")
    out["series"] = ser
    if part and re.match(r"^\d+(?:\.\d+)?(?:-\d+)?$", str(part).strip()):
        out["n"] = str(part).strip()
    if not out["n"]:
        out["n"] = (_num_from_text(t.get("title", "")) or _num_from_text(t.get("album", ""))
                    or _num_from_text(t.get("grouping", "")))
    return out


def _nfo_meta(book_dir: str) -> dict | None:
    nfos = sorted(f for f in os.listdir(book_dir) if f.lower().endswith(".nfo"))
    if not nfos:
        return None
    try:
        with open(os.path.join(book_dir, nfos[0]), "r", encoding="utf-8", errors="replace") as f:
            text = f.read(20000)
    except OSError:
        return None
    out = {"source": ".nfo", "title": "", "series": "", "n": None}
    for line in text.splitlines():
        m = re.match(r"^\s*(Title|Series|Book|Album)\s*[:=]\s*(.+?)\s*$", line, re.I)
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if key == "title" and not out["title"]:
                out["title"] = val
            if key == "series" and not out["series"]:
                out["series"] = val
            n = _num_from_text(val)
            if n and not out["n"]:
                out["n"] = n
    if not out["n"]:
        # XML-ish nfo: <set><name>Series</name><index>3</index></set> or plain text
        m = re.search(r"<(?:index|season|set_index)>\s*(\d+(?:\.\d+)?)\s*<", text, re.I)
        out["n"] = m.group(1) if m else _num_from_text(os.path.splitext(nfos[0])[0]) or _num_from_text(text)
    return out


def book_metadata(book_dir: str) -> dict | None:
    """Best embedded metadata for a book folder: {source, title, series, n} or None.
    Order: metadata.json, then the m4b tags, then a .nfo - first one with a number."""
    found = None
    for reader in (_json_meta, _m4b_meta, _nfo_meta):
        try:
            m = reader(book_dir)
        except Exception:      # noqa: BLE001 - a bad file must never stop the run
            m = None
        if m and m.get("n"):
            return m
        found = found or m
    return found



# ------------------------------------------------------------------- library
@dataclass
class SeriesDir:
    author: str
    series: str
    path: str
    books: list[str] = field(default_factory=list)   # book folder names


def _subdirs(p: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(p)
                      if os.path.isdir(os.path.join(p, d)) and d not in IGNORE_DIRS
                      and not d.startswith("."))
    except OSError:
        return []


def _has_audio(p: str) -> bool:
    try:
        return any(os.path.splitext(f)[1].lower() in AUDIO_EXT for f in os.listdir(p))
    except OSError:
        return False


def scan_library(dst: str) -> tuple[list[str], list[SeriesDir]]:
    """Return (author folder names, series dirs) of the library."""
    authors = _subdirs(dst)
    series: list[SeriesDir] = []
    for a in authors:
        ap = os.path.join(dst, a)
        for s in _subdirs(ap):
            sp = os.path.join(ap, s)
            series.append(SeriesDir(a, s, sp, _subdirs(sp)))
    return authors, series


def book_key(name: str) -> tuple[str, str]:
    """('title-ish', 'n') used to detect 'already have this book'."""
    m = BOOK_RE.match(name)
    if m and m.group("n"):
        return (norm(m.group("title")), m.group("n").lstrip("0") or "0")
    return (norm(name), "")


# ------------------------------------------------------------------ matching
@dataclass
class Decision:
    action: str          # FILED | NEW | HELD
    target: str | None   # destination path of the book folder (if any)
    reason: str
    matched: SeriesDir | None = None


def choose_series(author: str, series: str, authors: list[str],
                  lib: list[SeriesDir]) -> tuple[SeriesDir | None, str | None, str]:
    """Pick the existing series folder for (author, series).
    Returns (series_dir | None, author_folder | None, reason)."""
    exact = [s for s in lib if norm(s.series) == norm(series)]
    # 2. exact series + compatible author (best author level, then most books)
    compat = [(author_level(author, s.author), len(s.books), s) for s in exact]
    compat = [t for t in compat if t[0] >= 1]
    if compat:
        compat.sort(key=lambda t: (-t[0], -t[1], len(t[2].series)))
        best = compat[0]
        tied = [t for t in compat if t[0] == best[0] and t[2].author != best[2].author]
        if tied:
            names = sorted({t[2].author for t in compat if t[0] == best[0]})
            return None, None, f"ambiguous: '{series}' exists under {names}"
        return best[2], best[2].author, "series match" + (
            "" if best[0] >= 2 else f" (author differs: '{best[2].author}' vs '{author}')")
    # 3. exact series, unique, distinctive, author unrelated
    if exact:
        if len(exact) == 1 and len(norm(series).split()) >= 2:
            s = exact[0]
            return s, s.author, f"series match, author differs ('{s.author}' vs '{author}')"
        names = sorted({s.author for s in exact})
        return None, None, f"ambiguous: '{series}' exists under {names} - none match author '{author}'"
    # 4. close series name + same primary author
    close = []
    for s in lib:
        r = series_ratio(series, s.series)
        if r >= 0.90 and author_level(author, s.author) >= 2:
            close.append((r, len(s.books), s))
    if close:
        close.sort(key=lambda t: (-t[0], -t[1]))
        if len(close) > 1 and close[0][0] == close[1][0] and close[0][2].path != close[1][2].path:
            return None, None, f"ambiguous: '{series}' resembles {[c[2].series for c in close[:3]]}"
        s = close[0][2]
        return s, s.author, f"series near-match '{s.series}' ({close[0][0]:.2f})"
    # 5. new series - keep the author consolidated if we know them
    best_author, best_lvl = None, 0
    for a in authors:
        lvl = author_level(author, a)
        if lvl > best_lvl or (lvl == best_lvl and lvl and a == author):
            best_author, best_lvl = a, lvl
    if best_lvl >= 2:
        return None, best_author, f"new series under existing author '{best_author}'"
    return None, None, "new series, new author"


def decide(author: str, series: str | None, book: str, dst: str,
           authors: list[str], lib: list[SeriesDir]) -> Decision:
    if series is None:
        # book folder directly under the author (no series metadata)
        best_author, best_lvl = None, 0
        for a in authors:
            lvl = author_level(author, a)
            if lvl > best_lvl:
                best_author, best_lvl = a, lvl
        afold = best_author if best_lvl >= 2 else author
        target = os.path.join(dst, afold, book)
        if os.path.exists(target):
            return Decision("HELD", target, "already in library (no series)")
        return Decision("NEW" if best_lvl < 2 else "FILED", target,
                        "no series metadata; under author folder")
    sdir, afold, reason = choose_series(author, series, authors, lib)
    if sdir is None and afold is None and reason.startswith("ambiguous"):
        return Decision("HELD", None, reason)
    if sdir is not None:
        target = os.path.join(sdir.path, book)
        bk = book_key(book)
        for existing in sdir.books:
            ek = book_key(existing)
            # same folder name, or the same book NUMBER inside the same
            # series folder ("The Primal Hunter 14 - Book 14" vs
            # "The Primal Hunter - Book 14"), or the same title stem
            same = (existing.lower() == book.lower()
                    or (bk[1] and ek[1] and bk[1] == ek[1])
                    or (bk[0] and bk[0] == ek[0] and bk[1] == ek[1]))
            if same:
                return Decision("HELD", os.path.join(sdir.path, existing),
                                f"already in library as '{existing}'")
        if os.path.exists(target):
            return Decision("HELD", target, "destination exists")
        return Decision("FILED", target, reason, sdir)
    # new series
    target = os.path.join(dst, afold or author, series, book)
    if os.path.exists(target):
        return Decision("HELD", target, "destination exists")
    return Decision("NEW", target, reason)


# ------------------------------------------------------------------- walking
@dataclass
class Result:
    author: str
    series: str | None
    book: str
    src: str
    action: str
    target: str | None
    reason: str

    def line(self) -> str:
        where = f"{self.author} / {self.series + ' / ' if self.series else ''}{self.book}"
        if self.action == "HELD":
            return f"HOLD   {where}\n       -> {self.reason}"
        arrow = self.target or ""
        return f"{self.action:<6} {where}\n       -> {arrow}  ({self.reason})"


def find_book_dirs(src: str) -> list[tuple[str, str | None, str, str]]:
    """Yield (author, series|None, book, path) for every folder in src that
    directly contains audio files. Handles Author\\Series\\Book,
    Author\\Book and loose Book folders at the root."""
    out = []
    for a in _subdirs(src):
        ap = os.path.join(src, a)
        if _has_audio(ap):                       # Book folder straight at root
            out.append((a, None, a, ap))
            continue
        for s in _subdirs(ap):
            sp = os.path.join(ap, s)
            if _has_audio(sp):                   # Author\Book (no series)
                out.append((a, None, s, sp))
                continue
            for b in _subdirs(sp):
                bp = os.path.join(sp, b)
                if _has_audio(bp):
                    out.append((a, s, b, bp))
                else:
                    # one level deeper (chapter-split sub-folders) - treat the
                    # book folder as the unit anyway if it has audio below
                    if any(_has_audio(os.path.join(bp, d)) for d in _subdirs(bp)):
                        out.append((a, s, b, bp))
    return out


def _move(srcp: str, dstp: str) -> None:
    os.makedirs(os.path.dirname(dstp), exist_ok=True)
    if os.path.exists(dstp):
        raise FileExistsError(dstp)
    shutil.move(srcp, dstp)


def _rename_single_audio(book_dir: str) -> None:
    """Schema: the one audio file in a book folder is named like the folder."""
    try:
        files = [f for f in os.listdir(book_dir)
                 if os.path.splitext(f)[1].lower() in AUDIO_EXT]
    except OSError:
        return
    if len(files) != 1:
        return
    want = os.path.basename(book_dir) + os.path.splitext(files[0])[1].lower()
    if files[0] != want and not os.path.exists(os.path.join(book_dir, want)):
        os.rename(os.path.join(book_dir, files[0]), os.path.join(book_dir, want))


def _prune_empty(root: str) -> int:
    """Remove empty directories under root (not root itself). Ignores
    desktop.ini/Thumbs.db leftovers so an 'empty' folder really goes."""
    removed = 0
    for cur, dirs, files in os.walk(root, topdown=False):
        if os.path.abspath(cur) == os.path.abspath(root):
            continue
        real = [f for f in files if f.lower() not in IGNORE_FILES]
        if not real and not _subdirs(cur):
            try:
                for f in files:
                    os.remove(os.path.join(cur, f))
                os.rmdir(cur)
                removed += 1
            except OSError:
                pass
    return removed


def _loose_audio_files(src: str) -> list[str]:
    try:
        return sorted(f for f in os.listdir(src)
                      if os.path.isfile(os.path.join(src, f)) and os.path.splitext(f)[1].lower() in AUDIO_EXT)
    except OSError:
        return []


def place_loose_file(path: str, authors: list[str], lib: list[SeriesDir]) -> tuple[str, str, str] | str:
    """Work out (author folder, series folder name, schema book name) for a lone
    audio file dropped into the Libation folder (e.g. 'ThePrimalHunter15.m4b'
    from elsewhere), from its tags + the library. Returns a reason string when
    it can't be placed safely."""
    t = _m4b_tags(path) if os.path.splitext(path)[1].lower() in (".m4b", ".m4a") else {}
    title = t.get("title") or os.path.splitext(os.path.basename(path))[0]
    artist = t.get("artist") or t.get("album_artist") or ""
    series_tag = t.get("series") or t.get("movement") or ""
    part = t.get("part") or t.get("series-part") or t.get("movement_index") or ""
    short = short_title(title, None)
    ns = norm(short)
    # 1. an explicit series tag + number
    cands: list[tuple[SeriesDir, str]] = []
    if series_tag and part and re.match(r"^\d+(?:\.\d+)?$", str(part).strip()):
        for sd in lib:
            if norm(sd.series) == norm(series_tag) and (not artist or author_level(artist, sd.author) >= 1):
                cands.append((sd, str(part).strip()))
    # 2. title is "<series> <n>" for a series this author already has
    if not cands:
        for sd in lib:
            if artist and author_level(artist, sd.author) < 1:
                continue
            nser = norm(sd.series)
            if not nser or not ns.startswith(nser):
                continue
            rest = ns[len(nser):].strip()
            m = re.fullmatch(r"(\d+(?:\.\d+)?)", rest) or (re.fullmatch(r"(?:book|vol(?:ume)?|part)\s*(\d+(?:\.\d+)?)", rest))
            if m:
                cands.append((sd, m.group(1)))
            elif not rest and part:
                cands.append((sd, str(part).strip()))
    # 3. "<title>: <series>, Book N" shapes: number in the title/album text + series match
    if not cands:
        n_txt = _num_from_text(title) or _num_from_text(t.get("album") or "")
        if n_txt:
            for sd in lib:
                if artist and author_level(artist, sd.author) < 1:
                    continue
                if norm(sd.series) and (norm(sd.series) in norm(title) or norm(sd.series) in norm(t.get("album") or "")):
                    cands.append((sd, n_txt))
    # de-dup by series path
    seen, uniq = set(), []
    for sd, n in cands:
        if sd.path not in seen:
            seen.add(sd.path); uniq.append((sd, n))
    if len(uniq) != 1:
        if not uniq:
            return (f"can't tell which series this is (title '{short}', artist '{artist or '?'}') - "
                    f"put it in an Author\\Series\\Book folder yourself")
        return f"ambiguous: could be {[x[0].series for x in uniq][:4]}"
    sd, n = uniq[0]
    if "-" in n:
        return "omnibus/range number - place it by hand"
    if any(book_key(b)[1] == n.lstrip('0') for b in sd.books):
        return f"'{sd.series}' already has Book {n}"
    book = schema_book_name(f"{short} - Book {n}", sd.series) or f"{sd.series} - Book {n}"
    return (sd.author, sd.series, book)


def _adopt_loose_files(src: str, authors: list[str], lib: list[SeriesDir], dry_run: bool, log) -> list[Result]:
    """Move lone audio files at the Libation root into Author\\Series\\Book folders
    (named to the schema) so the normal filing picks them up. Unplaceable files
    are HELD in place with a reason."""
    out: list[Result] = []
    for f in _loose_audio_files(src):
        p = os.path.join(src, f)
        r = place_loose_file(p, authors, lib)
        if isinstance(r, str):
            res = Result("?", None, f, p, "HELD", None, r)
            out.append(res); log(res.line()); continue
        author, series, book = r
        bdir = os.path.join(src, author, series, book)
        target = os.path.join(bdir, book + os.path.splitext(f)[1].lower())
        if not dry_run:
            try:
                os.makedirs(bdir, exist_ok=True)
                os.rename(p, target)
            except OSError as e:
                res = Result(author, series, book, p, "HELD", target, f"could not move into a book folder: {e}")
                out.append(res); log(res.line()); continue
        log(f"ADOPT  {f}\n       -> {author} / {series} / {book}  (from its tags)")
        if dry_run:
            out.append(Result(author, series, book, p, "FILED", target, "loose file placed from its tags (dry run)"))
    return out


def file_all(src: str = DEFAULT_SRC, dst: str = DEFAULT_DST,
             dry_run: bool = False, log=print, aliases=None) -> list[Result]:
    if not os.path.isdir(dst):
        raise FileNotFoundError(f"library folder not reachable: {dst}")
    os.makedirs(src, exist_ok=True)
    aliases = aliases if aliases is not None else load_aliases()
    authors, lib = scan_library(dst)
    results: list[Result] = []
    results.extend(_adopt_loose_files(src, authors, lib, dry_run, log))
    for author, series, book, path in find_book_dirs(src):
        # taught corrections first: a resolved HOLD never comes back
        author = aliases["authors"].get(norm(author), author)
        if series is not None:
            series = aliases["series"].get(norm(series), series)
        # The folder shape is Libation's template filled from its own database;
        # when Audible's library record carried no series (Arcane Academy 5,
        # 7 Oct 2026: "A.P. Gore, Patricia Jones\Spell Breaker - Book"), the
        # files inside still know: metadata.json said "Arcane Academy #5".
        meta_note = ""
        if series is None or parse_folder(book)[1] is None:
            meta = book_metadata(path)
            if meta and meta.get("series") and series is None:
                series = aliases["series"].get(norm(meta["series"]), meta["series"])
                meta_note = f"series from {meta['source']}"
            if meta and meta.get("n") and parse_folder(book)[1] is None:
                title_part, _ = parse_folder(book)
                book = ILLEGAL_RE.sub("", f"{title_part or series or meta.get('title') or book} - Book {meta['n']}").strip(" .")
                meta_note = (meta_note + ", " if meta_note else "") + f"number from {meta['source']}"
        d = decide(author, series, book, dst, authors, lib)
        if meta_note and d.action != "HELD":
            d.reason += f"; {meta_note}"
        if d.action != "HELD":
            # apply the schema name on arrival, so Libation's
            # "Series 12 - Book 12" lands as "Series - Book 12"
            dest_series = os.path.basename(os.path.dirname(d.target)) if series else None
            want = schema_book_name(book, dest_series)
            if want and want != book:
                d.target = os.path.join(os.path.dirname(d.target), want)
                if os.path.exists(d.target):
                    d = Decision("HELD", d.target, f"already in library as '{want}'")
                else:
                    d.reason += f"; renamed to '{want}'"
        if d.action != "HELD" and not dry_run:
            try:
                _move(path, d.target)
                _rename_single_audio(d.target)
                # keep the in-memory library current for the next book
                if d.matched is not None:
                    d.matched.books.append(os.path.basename(d.target))
                elif series is not None:
                    afold = os.path.basename(os.path.dirname(os.path.dirname(d.target)))
                    if afold not in authors:
                        authors.append(afold)
                    sp = os.path.dirname(d.target)
                    sd = next((s for s in lib if s.path == sp), None)
                    if sd is None:
                        lib.append(SeriesDir(afold, series, sp, [os.path.basename(d.target)]))
                    else:
                        sd.books.append(os.path.basename(d.target))
            except (OSError, FileExistsError) as e:
                d = Decision("HELD", d.target, f"move failed: {e}")
        r = Result(author, series, book, path, d.action, d.target, d.reason)
        results.append(r)
        log(r.line())
    if not dry_run:
        _prune_empty(src)
    return results


def main(argv=None) -> int:
    # cmd.exe consoles use a legacy code page; never die on a stray glyph
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--dry-run", action="store_true", help="decide, don't move")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    log = (lambda *x: None) if a.quiet else print
    try:
        results = file_all(a.src, a.dst, dry_run=a.dry_run, log=log)
    except FileNotFoundError as e:
        print(f"ERROR  {e}")
        return 1
    # machine-readable report for the Release Radar dashboard (held books get
    # a resolver UI there); best-effort, never fatal
    try:
        import json as _json
        from datetime import datetime as _dt
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            _json.dump({"at": _dt.now().isoformat(timespec="seconds"),
                        "results": [{"author": r.author, "series": r.series, "book": r.book,
                                     "src": r.src, "action": r.action, "target": r.target,
                                     "reason": r.reason} for r in results]}, f, indent=1)
    except Exception:
        pass
    n = {k: sum(1 for r in results if r.action == k) for k in ("FILED", "NEW", "HELD")}
    if not results:
        print("Nothing new in the Libation folder.")
    else:
        print(f"\nFiled into existing series: {n['FILED']} | new series/authors: "
              f"{n['NEW']} | held for you to look at: {n['HELD']}"
              + ("   [DRY RUN - nothing moved]" if a.dry_run else ""))
        if n["HELD"]:
            print(f"Held books stay in: {a.src}\n(Each one above says why. Move it by hand, "
                  "or fix the folder name and run again.)")
    return 2 if n["HELD"] else 0


if __name__ == "__main__":
    sys.exit(main())
