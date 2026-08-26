# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

DATE_INPUT_FORMATS = [
    "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
    "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%B %Y", "%b %Y",
]


def parse_date(value):
    """Return a date from a flexible string, or None."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # ISO with time
    try:
        return datetime.fromisoformat(s.replace("Z", "")).date()
    except Exception:
        pass
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def iso(d):
    return d.isoformat() if isinstance(d, date) else (d or None)


def today():
    return date.today()


