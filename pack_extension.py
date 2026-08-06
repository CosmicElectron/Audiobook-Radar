"""Package release-radar-firefox/ into release-radar-firefox.xpi.

Uses Python's zipfile rather than PowerShell's Compress-Archive: plain deflate,
forward-slash paths, no directory entries and no extra fields, which is the
shape Firefox expects from an add-on archive. Run after editing the extension:

    python pack_extension.py
"""
import hashlib
import json
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "release-radar-firefox")
OUT = os.path.join(HERE, "release-radar-firefox.xpi")

# manifest.json first: Firefox reads it before anything else in the archive
ORDER_FIRST = ["manifest.json"]
SKIP = {".DS_Store", "Thumbs.db"}


def files():
    found = []
    for root, dirs, names in os.walk(SRC):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for n in sorted(names):
            if n in SKIP:
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, SRC).replace(os.sep, "/")
            found.append((rel, full))
    found.sort(key=lambda p: (p[0] not in ORDER_FIRST, p[0]))
    return found


def main():
    entries = files()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, full in entries:
            with open(full, "rb") as f:
                z.writestr(zipfile.ZipInfo(rel), f.read(), zipfile.ZIP_DEFLATED)

    # verify: every packed byte matches disk, and the manifest's icons resolve
    z = zipfile.ZipFile(OUT)
    bad = z.testzip()
    assert bad is None, f"corrupt entry: {bad}"
    names = set(z.namelist())
    for rel, full in entries:
        assert hashlib.sha256(z.read(rel)).digest() == \
            hashlib.sha256(open(full, "rb").read()).digest(), f"mismatch: {rel}"
    m = json.loads(z.read("manifest.json"))
    icons = list((m.get("icons") or {}).values()) + \
        list((m.get("browser_action", {}).get("default_icon") or {}).values())
    missing = [p for p in icons if p not in names]
    assert not missing, f"manifest references missing icons: {missing}"
    print(f"packed {len(entries)} files -> {os.path.basename(OUT)} "
          f"(v{m.get('version')}, {os.path.getsize(OUT)} bytes)")
    print("icons referenced by the manifest, all present:", ", ".join(icons))


if __name__ == "__main__":
    main()
