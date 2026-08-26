"""Rebuild app.py from the ordered parts in src/.

app.py is a GENERATED file - edit the src/ parts, then run:  python compose_app.py
(conftest.py composes automatically when a part is newer; build.bat composes
before PyInstaller. A guard test fails if app.py drifts from the parts.)

Why composition instead of real modules: the whole runtime stays ONE module, so
the test suite's monkeypatching of radar.<name> keeps working exactly as it
always has - while editing happens in files small enough to patch precisely.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
APP = os.path.join(ROOT, "app.py")

BANNER = ("# === GENERATED FILE - DO NOT EDIT ===\n"
          "# app.py is composed from the parts in src/ by compose_app.py.\n"
          "# Edit the src/ part, then run:  python compose_app.py\n")


def parts():
    return sorted(f for f in os.listdir(SRC) if f.endswith(".py"))


def compose_text():
    out = [BANNER]
    for name in parts():
        with open(os.path.join(SRC, name), encoding="utf-8", newline="") as f:
            out.append(f.read())
    return "".join(out)


def is_stale():
    if not os.path.exists(APP):
        return True
    app_m = os.path.getmtime(APP)
    return any(os.path.getmtime(os.path.join(SRC, n)) > app_m for n in parts())


def compose(check_only=False):
    text = compose_text()
    current = open(APP, encoding="utf-8", newline="").read() if os.path.exists(APP) else ""
    if text == current:
        return False
    if check_only:
        raise SystemExit("app.py is out of date - run: python compose_app.py")
    compile(text, APP, "exec")            # refuse to write a broken compose
    with open(APP, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return True


if __name__ == "__main__":
    changed = compose(check_only="--check" in sys.argv)
    print("app.py " + ("recomposed" if changed else "already up to date")
          + f" ({len(parts())} parts)")
