"""Which Libation books may Update Audiobooks download? Only ones in a series
Release Radar tracks - the house rule (29 Sep 2026), after a run fetched and
filed two unrelated books alongside the one it had just bought.

Prints the Audible product IDs to download on ONE line (stdout, for the bat's
`for /f`), and a human summary on stderr. Prints nothing when there is nothing
to download or when it can't tell - downloading nothing is the safe failure.

A Libation book counts as a tracked series' book when any ONE of these holds:
  1. Audible's series name is the tracked series' name ("The Unfallen Series"
     and "The Unfallen" are the same name). Asked about book n, Audible's
     own number must agree (a "1-3" box set covers books 1, 2 and 3).
  2. its product ID is one the radar has recorded for that series
  3. Audible's record has no series, but the title starts with the tracked
     name as whole words ("Riftside 4: ...", never "Riftsiders"; asked about
     book 4, never "Riftside 4.5" or "Riftside 4-6")
  4. its AUTHOR is the tracked series' author and its title is one the radar
     knows for that series. Every Arcane Academy book is titled "Spell
     Breaker", and the one released on 2 Oct 2026 arrived with no series on
     Audible's record, so rules 1-3 all missed it (7 Oct 2026).
There is deliberately NO date-only rule: a tracked author's other series can
arrive series-less within a day of a tracked release, and an untracked
download is the worst outcome.
Rules 1, 3 and 4 never take a book whose author disagrees with the series'
author when both are known (another author's "Chrysalis" is not ours), and
rules 3-4 never a book Audible itself files under this series with a
different number. A box set / omnibus / Publisher's Pack is only ever
taken by its NAME (rules 1 and 3, the picker), never by an author guess, and
never counts as "book n".
Asked about ONE book (the app's "is book n in Libation?", which decides
whether the auto-buy skips a purchase as already owned), a shared title is
not enough on its own: the release date has to agree with book n's, and a
sibling released the same day with the same title makes the answer unknowable,
so it is "not owned" (the store page still refuses a double purchase).
Never by the Amazon ASIN alone: Audible's product ID for a book is not the
listing ASIN the radar knows (Riftside 4: B0HJ9CQDHW vs B0HJ9DX4S2).

The app imports this module for its own Libation check (_libation_match), so
the two share one idea of "a tracked book"; the app's per-book question is
deliberately the stricter one.

    python pick_downloads.py [--libation DIR] [--data FILE]
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys

LIBERATED = 1          # Libation's LiberatedStatus.Liberated; anything else still needs downloading
ROLE_AUTHOR = 1        # Libation's BookContributor.Role: 1 author, 2 narrator, 3 publisher
DATE_SLACK_DAYS = 1    # Audible's publish timestamp is UTC; the radar's dates are store-local days
GENERIC_WORDS = {"series", "saga", "book", "books", "novel", "trilogy", "cycle", "chronicles",
                 "adventure", "litrpg", "progression", "fantasy", "the"}
OMNIBUS = re.compile(r"\b(?:(?:books?|vol(?:ume)?s?\.?)\s*\d+\s*(?:-|–|to|&|and|through)\s*\d+|box(?:ed)?\s*set"
                     r"|omnibus|anthology|collection|bundle|complete\s+series|publisher'?s\s+pack)\b", re.I)


def _s(v):
    return v if isinstance(v, str) else ""


def norm(s):
    s = re.sub(r"[^a-z0-9]+", " ", _s(s).lower()).strip()
    s = re.sub(r"^(?:the|a|an) ", "", s)
    s = re.sub(r" (?:series|saga|trilogy|cycle)$", "", s)
    return s.strip()


def name_key(s):
    """A person's name, normalised. Not norm(): that drops a leading article,
    and "A.P. Gore" is not "P. Gore"."""
    return re.sub(r"[^a-z0-9]+", " ", _s(s).lower()).strip()


def title_keys(title):
    """The ways a title can be spelled across Amazon and Audible, normalised:
    the whole thing without parentheticals ("(Arcane Academy Book 5)") and the
    part before the subtitle colon. Very short keys are dropped ("RE: ...")."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", _s(title))
    keys = {norm(t), norm(t.split(":", 1)[0])}
    return {k for k in keys if len(k) >= 4}


def author_keys(author):
    """'by A.P. Gore' / 'K. Georgiades aka PathOfPen' / 'TheFirstDefier, see all'
    -> every name in it, normalised. Libation stores an author exactly as
    Audible prints it, which can be the same 'X aka Y' string, so both sides
    go through this."""
    s = re.sub(r"^\s*by\s+", "", _s(author), flags=re.I)
    s = re.sub(r",?\s*see all\s*$", "", s, flags=re.I)
    parts = re.split(r"\s*(?:,|&|/|\baka\b|\band\b|\bwith\b)\s*", s, flags=re.I)
    return {name_key(p) for p in parts if len(name_key(p)) >= 3}


def _num(n):
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(n)


def _order_range(order):
    """Libation's SeriesBook.Order is free text: '4', '4.5', '1-3' (a box set),
    '' -> (low, high) or None when it says nothing."""
    s = _s(order) if isinstance(order, str) else ("" if order is None else str(order))
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*", s)
    if not m:
        return None
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return (lo, hi) if lo <= hi else None


def _order_covers(order, n):
    r = _order_range(order)
    try:
        f = float(n)
    except (TypeError, ValueError):
        return False
    return r is not None and r[0] <= f <= r[1]


def _same_n(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _day(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _near(a, b):
    return bool(a) and bool(b) and abs((a - b).days) <= DATE_SLACK_DAYS


def _words(key):
    return {w for w in key.split() if len(w) >= 4 and w not in GENERIC_WORDS}


def ro_uri(db_path):
    """A read-only SQLite URI for a Windows path (as_uri() percent-encodes a
    '#' or '%' in the path, which would otherwise cut the URI short and drop
    mode=ro)."""
    return pathlib.Path(db_path).absolute().as_uri() + "?mode=ro"


def series_entry(s, n=None):
    """One of Release Radar's series records as the matcher sees it. With n,
    only that book's title, audiobook date and product ID count (the app asks
    "is book n in Libation?"); without, the whole series (the picker)."""
    s = s if isinstance(s, dict) else {}
    titles, dates, asins, others = set(), set(), set(), []
    asins_audio = s.get("asins_audio") if isinstance(s.get("asins_audio"), dict) else {}
    for e in s.get("timeline") or []:
        if not isinstance(e, dict):
            continue
        if n is not None and not _same_n(e.get("n"), n):
            if e.get("audiobook"):                      # a sibling: its date and title, to spot an unknowable twin
                others.append((str(e["audiobook"])[:10], title_keys(e.get("title"))))
            continue
        titles |= title_keys(e.get("title"))
        if e.get("audiobook"):
            dates.add(str(e["audiobook"])[:10])
    if n is None:
        titles |= title_keys(s.get("novel_title"))
        for h in s.get("history") or []:
            if isinstance(h, dict) and h.get("audiobook"):
                dates.add(str(h["audiobook"])[:10])
        if s.get("audiobook_date"):
            dates.add(str(s["audiobook_date"])[:10])
        asins = {v for v in asins_audio.values() if isinstance(v, str) and v}
    else:
        if _same_n(s.get("novel_n"), n):
            titles |= title_keys(s.get("novel_title"))
        if isinstance(asins_audio.get(_num(n)), str) and asins_audio.get(_num(n)):
            asins.add(asins_audio[_num(n)])
    return {"name": _s(s.get("name")), "key": norm(s.get("name")), "n": n,
            "authors": author_keys(s.get("author")), "titles": titles, "dates": dates,
            "latest": max(dates) if dates else None, "asins": asins, "others": others}


def match_book(book, index):
    """(tracked series' name, why) when the book belongs to one, else None.
    book: one of library_books()'s dicts; index: [series_entry(...)]."""
    for s in index:                                                     # 1. Audible's series name (+ number)
        if s["authors"] and book["authors"] and not (s["authors"] & book["authors"]):
            continue                                                    # another author's series of the same name
        for sname in book["series"]:
            if s["key"] and norm(sname) == s["key"] and (s["n"] is None or _order_covers(book["orders"].get(sname), s["n"])):
                return s["name"], s["name"]
    for s in index:                                                     # 2. a product ID the radar recorded
        if book["asin"] in s["asins"]:
            return s["name"], "matched by ID"
    t = norm(book["title"])
    tkeys = title_keys(book["title"])
    pub = _day(book.get("published"))
    omnibus = bool(OMNIBUS.search(_s(book.get("title"))))
    for s in index:
        # Audible files it under this very series with ANOTHER number: that is the last word
        if s["n"] is not None and any(norm(x) == s["key"] and _order_range(book["orders"].get(x)) is not None
                                      and not _order_covers(book["orders"].get(x), s["n"]) for x in book["series"]):
            continue
        same_author = bool(s["authors"] & book["authors"])
        if s["authors"] and book["authors"] and not same_author:
            continue                                                    # another author's book, whatever it is called
        if omnibus and s["n"] is not None:
            continue                                                    # a box set is never "book n" (rule 1's range covers it)
        if not book["series"] and len(s["key"]) >= 5:                   # 3. no series on the record: title starts with the name
            if s["n"] is None:
                head, tail = s["key"], r"(?: |$)"                        # whole words: "Riftside 4", never "Riftsiders"
            else:
                head, tail = f"{s['key']} {_num(s['n'])}", r"(?: (?!\d)|$)"   # ...and no "4.5" / "4-6" after the number
            if re.match(re.escape(head) + tail, t):
                return s["name"], s["name"]
        if not same_author or omnibus:
            continue
        same_day = any(_near(pub, _day(d)) for d in s["dates"])
        if tkeys & s["titles"]:                                         # 4. the author's book, under a title the radar knows
            foreign = bool(book["series"]) and not any(norm(x) == s["key"] for x in book["series"])
            # Audible names a series differently ("Stormweaver Series" for "Warformed: Stormweaver"):
            # one name inside the other, or a release the radar saw that day
            kin = (not foreign) or same_day or any(
                _words(norm(x)) and (norm(x) in s["key"] or s["key"] in norm(x)) for x in book["series"])
            twin = s["n"] is not None and any(_near(pub, _day(d)) and (not tk or tk & tkeys) for d, tk in s["others"])
            if kin and (s["n"] is None or (same_day and not twin)):
                return s["name"], f"{s['name']} (by author + title)"
        # No date-only rule: a tracked author's OTHER series (Shawn Wilson's Ultimate
        # Level beside Loopbreaker) can land series-less within a day of a tracked
        # release, and an untracked download is the worst outcome. A tracked book the
        # radar has no title for is listed as skipped in the Discord message instead.
    return None


def default_libation_dir():
    for c in (os.path.join(os.path.expanduser("~"), "Libation"),
              os.path.join(os.environ.get("APPDATA", ""), "Libation"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Libation")):
        if c and os.path.isfile(os.path.join(c, "LibationContext.db")):
            return c
    return None


def default_data_file():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ReleaseRadar", "data.json")


def load_state(data_file):
    with open(data_file, encoding="utf-8") as f:
        return json.load(f)


def tracked_index(data_file, state=None):
    """[series_entry] for every series Release Radar tracks."""
    state = state if state is not None else load_state(data_file)
    return [series_entry(s) for s in state.get("series") or [] if isinstance(s, dict) and s.get("name")]


def settings_libation_dir(state):
    """The app's own "Libation files" setting, when it names a real Libation folder."""
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    d = settings.get("buy_libation_files")
    return d if isinstance(d, str) and d and os.path.isfile(os.path.join(d, "LibationContext.db")) else None


def library_books(libation_dir, pending_only=True):
    """Libation's books as the matcher sees them:
    [{asin, title, series: [names], orders: {name: order}, authors: {normalised},
      published: 'YYYY-MM-DD' or None, status}], pending_only -> not yet downloaded."""
    db = os.path.join(libation_dir, "LibationContext.db")
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=5)
    try:
        rows = con.execute(
            """select b.BookId, b.AudibleProductId, b.Title, b.DatePublished, coalesce(u.BookStatus, 0), s.Name, sb."Order"
                 from Books b
                 left join UserDefinedItem u on u.BookId = b.BookId
                 left join SeriesBook sb on sb.BookId = b.BookId
                 left join Series s on s.SeriesId = sb.SeriesId""").fetchall()
        try:
            auth = con.execute(
                """select bc.BookId, c.Name from BookContributor bc
                     join Contributors c on c.ContributorId = bc.ContributorId
                    where bc.Role = ?""", (ROLE_AUTHOR,)).fetchall()
        except sqlite3.Error:        # an older Libation schema: the author rules just don't fire
            auth = []
    finally:
        con.close()
    authors = {}
    for bid, name in auth:
        keys = author_keys(name) | ({name_key(name)} if len(name_key(name)) >= 3 else set())
        if keys:
            authors.setdefault(bid, set()).update(keys)
    books = {}
    for bid, asin, title, published, status, sname, order in rows:
        if not asin or (pending_only and status == LIBERATED):
            continue
        b = books.setdefault(asin, {"asin": asin, "title": title or "?", "series": [], "orders": {},
                                    "authors": authors.get(bid, set()), "status": status,
                                    "published": str(published)[:10] if published else None})
        if sname and sname not in b["series"]:
            b["series"].append(sname)
            b["orders"][sname] = order
    return list(books.values())


def pending_books(libation_dir):
    return library_books(libation_dir, pending_only=True)


def pick(books, index):
    """Split pending books into (download, skip) by the tracked series."""
    take, skip = [], []
    for b in books:
        hit = match_book(b, index)
        if hit:
            take.append((b["asin"], b["title"], hit[1]))
        else:
            skip.append((b["asin"], b["title"], ", ".join(b["series"]) or "no series"))
    return take, skip


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--libation", default=None)
    ap.add_argument("--data", default=None)
    a = ap.parse_args(argv)
    say = lambda m: print(m, file=sys.stderr)
    data = a.data or default_data_file()
    try:
        state = load_state(data)
        lib = a.libation or settings_libation_dir(state) or default_libation_dir()
        if not lib:
            raise FileNotFoundError("Libation's LibationContext.db was not found")
        index = tracked_index(data, state)
        if not index:
            raise ValueError("Release Radar has no tracked series")
        take, skip = pick(pending_books(lib), index)
    except Exception as e:      # noqa: BLE001 - any doubt means download nothing
        say(f" Can't tell which books are tracked ({e}) - downloading nothing.")
        return 1
    for asin, title, why in take:
        say(f" Download (tracked): {title} [{asin}] - {why}")
    if skip:
        say(" Not downloading (not in a tracked series): "
            + "; ".join(f"{t} ({s})" for _, t, s in skip[:12]) + (" ..." if len(skip) > 12 else ""))
    if not take:
        say(" Nothing new from the series Release Radar tracks.")
        return 0
    print(" ".join(asin for asin, _, _ in take))
    return 0


if __name__ == "__main__":
    sys.exit(main())
