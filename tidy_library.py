"""One-off library tidy: bring pre-schema folders in AudioBooks into line with
the schema, WITHOUT re-titling anything you already named.

Schema (per book):   <Author>\\<Series>\\<Title> - Book <n>\\<Title> - Book <n>.m4b
  * <Title> is the book's short title AS IT IS IN YOUR FOLDER NAME, cleaned:
    taglines ("- A LitRPG Adventure"), "{narrator}" tags, "[ASIN]", a
    trailing series name, look-alike colons. When the title is only the
    series name plus its number ("The Primal Hunter 12") the number is
    redundant with "Book 12" and is dropped: "The Primal Hunter - Book 12" -
    the form most of the hand-fixed folders already use.
  * The single audio file inside a book folder is named like the folder.
  * One series folder per series per author: duplicates such as
    "The Beginning After the End" + "The Beginning After The End Series"
    are merged into the canonical one (the one matching Libation's series
    name, else the one without a "Series" suffix, else the fuller one).

When the folder has no number, the files inside are asked - metadata.json
(Audiobookshelf), then the m4b's own tags, then a .nfo - and only then
Libation's database (matched by series AND author, so the three different
"Ascendant" series cannot cross-talk). Nothing ever overrides your titles -
Audible's own titles are often messier than yours.

Left alone, always: author folders, multi-file (chapter) books' files,
omnibus/range folders ("Book 3-4", "Books 1-5"), extra files (covers,
book info.txt, metadata.json), anything that cannot be numbered.

Three modes - nothing changes on disk until --apply:
  python tidy_library.py --plan  [--dst DIR] [--out plan.json]   (default)
      writes a human-readable report + plan.json (every rename, in order)
  python tidy_library.py --apply plan.json
      performs the renames, writing undo-<stamp>.json next to the plan
  python tidy_library.py --undo undo-<stamp>.json
      reverses an applied plan, last rename first
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

import file_audiobooks as fa   # shares norm(), AUDIO_EXT, IGNORE_*, schema naming

DEFAULT_DST = fa.DEFAULT_DST
LIBATION_DB = r"C:\Users\<user>\Libation\LibationContext.db"


# ------------------------------------------------------------------ Libation
def load_libation(db_path: str = LIBATION_DB) -> list[dict]:
    """[{title, subtitle, series, order, authors}] for every book Libation knows."""
    if not os.path.exists(db_path):
        return []
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = c.execute("""
            select b.Title, b.Subtitle, s.Name, sb."Order",
              (select group_concat(c2.Name, ', ') from BookContributor bc
                 join Contributors c2 on c2.ContributorId = bc.ContributorId
                where bc.BookId = b.BookId and bc.Role = 1)
            from Books b
            left join SeriesBook sb on sb.BookId = b.BookId
            left join Series s on s.SeriesId = sb.SeriesId""").fetchall()
        c.close()
    except sqlite3.Error:
        return []
    return [{"title": t or "", "subtitle": sub or "", "series": se or "",
             "order": str(o or "").strip(), "authors": a or ""}
            for t, sub, se, o, a in rows]


def libation_number(known: list[dict], author: str, series: str, title_part: str) -> str | None:
    """Book number from Libation for (author folder, series folder, title), or None."""
    ns, nt = fa.norm(series), fa.norm(title_part)
    hits = []
    for b in known:
        if fa.norm(b["series"]) != ns or not b["order"] or "-" in b["order"]:
            continue
        if fa.author_level(author, b["authors"]) < 1:
            continue
        # plain titles (no series-reduction) so "Loop" and "Loop 2" stay distinct
        bt = fa.norm(fa.short_title(b["title"], None))
        bs = fa.norm(fa.short_title(b["subtitle"], None)) if b["subtitle"] else ""
        if nt and (nt == bt or nt == bs):
            hits.append(b["order"])
    hits = sorted(set(hits))
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ planning
def _audio_files(p: str) -> list[str]:
    try:
        return sorted(f for f in os.listdir(p)
                      if os.path.splitext(f)[1].lower() in fa.AUDIO_EXT)
    except OSError:
        return []


def _canonical_series(dirs: list[fa.SeriesDir], lib_series_names: set[str]) -> fa.SeriesDir:
    def key(s: fa.SeriesDir):
        exact_lib = s.series.lower() in lib_series_names
        suffixed = s.series.lower().rstrip().endswith((" series", " saga"))
        return (not exact_lib, suffixed, -len(s.books), len(s.series))
    return sorted(dirs, key=key)[0]


def build_plan(dst: str = DEFAULT_DST, db_path: str = LIBATION_DB) -> dict:
    authors, lib = fa.scan_library(dst)
    known = load_libation(db_path)
    lib_series_names = {b["series"].lower() for b in known if b["series"]}

    steps: list[dict] = []           # executed in order; each {op, src, dst, why}
    notes: list[dict] = []           # things left alone, with reasons
    stats = {"series_merged": 0, "folders": 0, "files": 0, "unchanged": 0, "left": 0}

    # --- 1. duplicate series folders under the same author -> merge
    by_author: dict[str, dict[str, list[fa.SeriesDir]]] = {}
    for s in lib:
        by_author.setdefault(s.author, {}).setdefault(fa.norm(s.series), []).append(s)
    merged_into: dict[str, fa.SeriesDir] = {}        # dup series path -> canonical
    for groups in by_author.values():
        for dirs in groups.values():
            if len(dirs) < 2:
                continue
            canon = _canonical_series(dirs, lib_series_names)
            for d in dirs:
                if d is canon:
                    continue
                clash = [b for b in d.books if b.lower() in {x.lower() for x in canon.books}]
                if clash:
                    notes.append({"path": d.path, "why": f"not merged into '{canon.series}': "
                                  f"both have {clash}"})
                    continue
                for b in d.books:
                    steps.append({"op": "move", "src": os.path.join(d.path, b),
                                  "dst": os.path.join(canon.path, b),
                                  "why": f"merge duplicate series folder '{d.series}' -> '{canon.series}'"})
                steps.append({"op": "rmdir", "src": d.path, "dst": None,
                              "why": "remove emptied duplicate series folder"})
                merged_into[d.path] = canon
                canon.books.extend(d.books)
                stats["series_merged"] += 1

    # --- 2. per book folder: expected folder + file name
    for s in lib:
        if s.path in merged_into:
            continue                                  # handled under its canonical series
        series_path, series_name = s.path, s.series
        taken = {b.lower() for b in s.books}
        planned: set[str] = set()
        for book in sorted(set(s.books)):
            # after step 1, the folder may be arriving from a merged duplicate
            src_dir = os.path.join(series_path, book)
            orig_dir = src_dir if os.path.isdir(src_dir) else next(
                (os.path.join(d, book) for d, c in merged_into.items()
                 if c is s and os.path.isdir(os.path.join(d, book))), src_dir)
            title_part, n = fa.parse_folder(book)
            why_n = ""
            if n is None:
                # the files inside usually know: metadata.json / m4b tags / .nfo
                meta = fa.book_metadata(orig_dir)
                if meta and meta.get("n"):
                    ms = meta.get("series") or ""
                    # only trust it when it names this series (or names none)
                    if not ms or fa.series_ratio(ms, series_name) >= 0.8:
                        n = meta["n"]
                        why_n = f" (number from {meta['source']})"
            if n is None:
                n = libation_number(known, s.author, series_name, title_part)
                if n:
                    why_n = " (number from Libation)"
            if n is None:
                notes.append({"path": src_dir, "why": "no book number found - left alone"})
                stats["left"] += 1
                continue
            if "-" in n and why_n:
                # a range we only learned from the files: the folder name has no
                # clean title to build from, so don't guess
                notes.append({"path": src_dir, "why": "omnibus / range - left alone"})
                stats["left"] += 1
                continue
            expected = fa.schema_book_name(f"{title_part} - Book {n}" if title_part else f"- Book {n}",
                                           series_name)
            if not expected:
                notes.append({"path": src_dir, "why": "could not build a schema name - left alone"})
                stats["left"] += 1
                continue
            el = expected.lower()
            if el != book.lower() and (el in taken or el in planned):
                notes.append({"path": src_dir, "why": f"would collide with '{expected}' - left alone"})
                stats["left"] += 1
                continue
            planned.add(el)
            new_dir = os.path.join(series_path, expected)
            changed = False
            if expected != book:
                steps.append({"op": "rename", "src": src_dir, "dst": new_dir,
                              "why": "folder -> '<Title> - Book n'" + why_n})
                stats["folders"] += 1
                changed = True
            files = _audio_files(orig_dir)
            if len(files) == 1:
                ext = os.path.splitext(files[0])[1].lower()
                want = expected + ext
                if files[0] != want:
                    steps.append({"op": "rename", "src": os.path.join(new_dir, files[0]),
                                  "dst": os.path.join(new_dir, want),
                                  "why": "file -> same name as its folder"})
                    stats["files"] += 1
                    changed = True
            if not changed:
                stats["unchanged"] += 1
    return {"dst": dst, "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "stats": stats, "steps": steps, "notes": notes}


# ------------------------------------------------------------------- apply/undo
def _do(op: str, src: str, dst: str | None) -> None:
    if op in ("rename", "move"):
        if not os.path.exists(src):
            raise FileNotFoundError(src)
        if dst is None:
            raise ValueError("missing dst")
        same = os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src))
        if os.path.exists(dst) and not same:
            raise FileExistsError(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if same and src != dst:
            tmp = dst + ".__case__"                 # case-only rename on Windows
            os.rename(src, tmp)
            os.rename(tmp, dst)
        else:
            os.rename(src, dst)
    elif op == "rmdir":
        left = [f for f in os.listdir(src) if f.lower() not in fa.IGNORE_FILES]
        if left:
            raise OSError(f"not empty: {src} -> {left[:3]}")
        for f in os.listdir(src):
            os.remove(os.path.join(src, f))
        os.rmdir(src)
    elif op == "mkdir":
        os.makedirs(src, exist_ok=True)
    else:
        raise ValueError(op)


def apply_plan(plan: dict, log=print) -> tuple[list[dict], list[dict]]:
    done, failed = [], []
    for st in plan["steps"]:
        try:
            _do(st["op"], st["src"], st.get("dst"))
            done.append(st)
            log(f"OK    {st['op']:<6} {st['src']}\n      -> {st.get('dst') or ''}")
        except Exception as e:   # noqa: BLE001 - keep going, report
            failed.append({**st, "error": str(e)})
            log(f"FAIL  {st['op']:<6} {st['src']}\n      !! {e}")
    return done, failed


def undo(done: list[dict], log=print) -> list[dict]:
    failed = []
    for st in reversed(done):
        try:
            if st["op"] in ("rename", "move"):
                _do("rename", st["dst"], st["src"])
            elif st["op"] == "rmdir":
                _do("mkdir", st["src"], None)
            log(f"UNDO  {st['op']:<6} {st.get('dst') or st['src']}\n      -> {st['src']}")
        except Exception as e:   # noqa: BLE001
            failed.append({**st, "error": str(e)})
            log(f"FAIL  undo {st['op']} {st.get('dst')}: {e}")
    return failed


# ------------------------------------------------------------------- report
def report(plan: dict) -> str:
    st = plan["stats"]
    out = [f"Library: {plan['dst']}", f"Planned {plan['created']}", "",
           f"  duplicate series folders to merge : {st['series_merged']}",
           f"  book folders to rename            : {st['folders']}",
           f"  audio files to rename             : {st['files']}",
           f"  already correct                   : {st['unchanged']}",
           f"  left alone (see end)              : {st['left'] + (len(plan['notes']) - st['left'])}", ""]
    cur = None
    for s in plan["steps"]:
        if s["op"] == "rmdir":
            series = os.path.dirname(s["src"])
        elif s["why"].startswith("file"):
            series = os.path.dirname(os.path.dirname(s["src"]))
        else:
            series = os.path.dirname(s["src"])
        rel = os.path.relpath(series, plan["dst"])
        if rel != cur:
            out.append(f"\n[{rel}]")
            cur = rel
        a = os.path.basename(s["src"])
        b = os.path.basename(s["dst"]) if s.get("dst") else ""
        if s["op"] == "move":
            b = os.path.relpath(s["dst"], plan["dst"])
        if s["why"].startswith("file"):
            out.append(f"    file   {a}\n        -> {b}")
        elif b:
            out.append(f"  {s['op']:<6} {a}\n      -> {b}   ({s['why']})")
        else:
            out.append(f"  {s['op']:<6} {a}   ({s['why']})")
    if plan["notes"]:
        out.append("\nLeft alone:")
        for n in plan["notes"]:
            out.append(f"  {os.path.relpath(n['path'], plan['dst'])}\n      {n['why']}")
    return "\n".join(out)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="tidy the AudioBooks library into the schema")
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--db", default=LIBATION_DB)
    ap.add_argument("--plan", action="store_true", help="write the plan (default)")
    ap.add_argument("--out", default=None, help="plan json path (default: next to this script)")
    ap.add_argument("--apply", metavar="PLAN_JSON")
    ap.add_argument("--undo", metavar="UNDO_JSON")
    a = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if a.undo:
        data = json.load(open(a.undo, encoding="utf-8"))
        failed = undo(data["done"])
        print(f"\nundone {len(data['done']) - len(failed)} of {len(data['done'])} steps"
              + (f"; {len(failed)} failed" if failed else ""))
        return 1 if failed else 0
    if a.apply:
        plan = json.load(open(a.apply, encoding="utf-8"))
        done, failed = apply_plan(plan)
        undo_path = os.path.join(os.path.dirname(os.path.abspath(a.apply)), f"undo-{stamp}.json")
        json.dump({"plan": a.apply, "applied": stamp, "done": done, "failed": failed},
                  open(undo_path, "w", encoding="utf-8"), indent=1)
        print(f"\napplied {len(done)} steps, {len(failed)} failed. Undo file: {undo_path}")
        return 1 if failed else 0
    plan = build_plan(a.dst, a.db)
    out = a.out or os.path.join(here, f"tidy-plan-{stamp}.json")
    json.dump(plan, open(out, "w", encoding="utf-8"), indent=1)
    txt = report(plan)
    with open(os.path.splitext(out)[0] + ".txt", "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    print(f"\nPlan written: {out}\nNothing has been changed. Apply with:\n"
          f"  python tidy_library.py --apply \"{out}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
