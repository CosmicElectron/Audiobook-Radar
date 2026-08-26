# ---------------------------------------------------------------------------
# Front-end (inlined so the exe is a single self-contained file)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard page. The real, editable sources live in static/dashboard.html/
# .css/.js (lintable, diffable); they are composed here into the same single
# self-contained page this app has always served. PyInstaller bundles the
# folder via:  --add-data "static;static"  (see build.bat).
# ---------------------------------------------------------------------------
_STATIC_DIR = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "static")


def _load_page_html():
    def rd(name):
        # universal newlines: the old r\"\"\"-literal went through Python's source
        # parser, which normalised CRLF - the served page must stay identical
        with open(os.path.join(_STATIC_DIR, name), encoding="utf-8") as f:
            return f.read()
    return (rd("dashboard.html")
            .replace("__RR_CSS__", rd("dashboard.css"))
            .replace("__RR_JS__", rd("dashboard.js")))


PAGE_HTML = _load_page_html()


