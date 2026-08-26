"""tidy_library: plan on real-shaped folders, apply, undo -> identical tree."""
import os

import tidy_library as tl
from test_file_audiobooks import mk   # noqa: F401  (helper)


def tree(root):
    out = []
    for cur, dirs, files in os.walk(root):
        for d in dirs:
            out.append(os.path.relpath(os.path.join(cur, d), root))
        for f in files:
            out.append(os.path.relpath(os.path.join(cur, f), root))
    return sorted(out)


def build(root):
    d = str(root)
    mk(d, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 11", files=("The Primal Hunter 11 - Book 11.m4b",))
    mk(d, "Zogarth", "The Primal Hunter", "The Primal Hunter 12 - Book 12", files=("The Primal Hunter 12 - Book 12.m4b",))
    mk(d, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 1", files=("The Primal Hunter - Book 1.m4b",))
    mk(d, "TurtleMe", "The Beginning After the End", "Apotheosis - Book 12", files=("Apotheosis - Book 12.m4b",))
    mk(d, "TurtleMe", "The Beginning After The End Series", "Providence - Book 11", files=("Providence - Book 11.m4b",))
    mk(d, "TurtleMe", "The Beginning After The End Series", "Beckoning Fates, Horizon's Edge - Book 3-4",
       files=("Beckoning Fates, Horizon's Edge - Book 3-4.m4b",))
    mk(d, "TurtleMe", "The Beginning After The End Series", files=("desktop.ini",))
    mk(d, "Will Wight", "Cradle", "1 Unsouled Cradle", files=("1 Unsouled Cradle.m4b",))
    mk(d, "Will Wight", "Cradle", "10 Reaper Cradle", files=("01.mp3", "02.mp3"))       # chapter files: folder only
    mk(d, "Matt Dinniman", "Dungeon Crawler Carl", "This Inevitable Ruin - Book 7",
       files=("This Inevitable Ruin꞉ Dungeon Crawler Carl, Book 7 [B0DK282SYV].m4b",))
    mk(d, "Craig Alanson", "Ascendant", "Transcendent - Book 2", files=("Transcendent - Book 2.m4b",))
    mk(d, "EmergencyComplaints", "Ascendant", "Ascendant 2 - Book 2", files=("Ascendant 2 - Book 2.m4b",))
    mk(d, "Shawn Wilson", "Ultimate Level 1", "New Dreams", files=("New Dreams.m4b",))
    mk(d, "!Smart AudioBook Player Statistics", files=(".nomedia",))
    return d


def test_plan_apply_undo_roundtrip(tmp_path):
    d = build(tmp_path / "AudioBooks")
    before = tree(d)
    plan = tl.build_plan(d, db_path=str(tmp_path / "no-such.db"))   # no Libation: numbers from names only
    why = {os.path.basename(s["src"]): os.path.basename(s["dst"] or "") for s in plan["steps"]}
    full = {os.path.basename(s["src"]): (s["dst"] or "") for s in plan["steps"]}
    # expectations
    assert why["The Primal Hunter 12 - Book 12"] == "The Primal Hunter - Book 12"
    assert why["The Primal Hunter 11 - Book 11.m4b"] == "The Primal Hunter - Book 11.m4b"
    assert full["Providence - Book 11"].endswith(os.path.join("The Beginning After the End", "Providence - Book 11"))
    assert why["1 Unsouled Cradle"] == "Unsouled - Book 1"
    assert why["10 Reaper Cradle"] == "Reaper - Book 10"
    assert "01.mp3" not in why                                        # chapter files untouched
    assert why["This Inevitable Ruin꞉ Dungeon Crawler Carl, Book 7 [B0DK282SYV].m4b"] == "This Inevitable Ruin - Book 7.m4b"
    assert "Transcendent - Book 2" not in why                         # titled book kept
    assert why["Ascendant 2 - Book 2"] == "Ascendant - Book 2"
    notes = {os.path.basename(n["path"]): n["why"] for n in plan["notes"]}
    # the omnibus is MOVED with the merge but never renamed (already in schema)
    assert not any(s["op"] == "rename" and os.path.basename(s["src"]) == "Beckoning Fates, Horizon's Edge - Book 3-4"
                   for s in plan["steps"])
    assert "Beckoning Fates, Horizon's Edge - Book 3-4" not in notes
    assert "no book number" in notes["New Dreams"]
    assert plan["stats"]["series_merged"] == 1

    done, failed = tl.apply_plan(plan, log=lambda *a: None)
    assert not failed
    after = tree(d)
    assert os.path.isfile(os.path.join(d, "Zogarth", "The Primal Hunter", "The Primal Hunter - Book 12",
                                       "The Primal Hunter - Book 12.m4b"))
    assert not os.path.exists(os.path.join(d, "TurtleMe", "The Beginning After The End Series"))
    assert os.path.isfile(os.path.join(d, "TurtleMe", "The Beginning After the End", "Providence - Book 11",
                                       "Providence - Book 11.m4b"))
    assert os.path.isfile(os.path.join(d, "Will Wight", "Cradle", "Reaper - Book 10", "01.mp3"))
    assert after != before

    assert tl.undo(done, log=lambda *a: None) == []
    restored = tree(d)
    # the emptied duplicate folder is recreated but its desktop.ini is gone — the only allowed diff
    assert sorted(set(before) - set(restored)) == [os.path.join("TurtleMe", "The Beginning After The End Series", "desktop.ini")]
    assert not (set(restored) - set(before))


def test_plan_is_idempotent(tmp_path):
    d = build(tmp_path / "AudioBooks")
    plan = tl.build_plan(d, db_path="nope.db")
    tl.apply_plan(plan, log=lambda *a: None)
    again = tl.build_plan(d, db_path="nope.db")
    assert again["steps"] == []
