# ---------------------------------------------------------------------------
# iCalendar feed — subscribe from any calendar app (phone included, via the
# tunnel). REAL releases only, both kinds, as all-day events — a calendar entry
# reads as a commitment, so estimates stay on the dashboard/digest instead.
# ---------------------------------------------------------------------------

def _ics_escape(s):
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_fold(line):
    """RFC 5545 §3.1 line folding: content lines SHOULD stay within 75 octets;
    continuation lines start with a single space. Folds on UTF-8 byte length
    without ever splitting inside a multi-byte character (emoji in summaries)."""
    if len(line.encode("utf-8")) <= 75:
        return [line]
    out, cur, cur_len = [], [], 0
    limit = 75
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_len + b > limit:
            out.append("".join(cur))
            cur, cur_len = [" ", ch], 1 + b
        else:
            cur.append(ch)
            cur_len += b
    if cur:
        out.append("".join(cur))
    return out


def build_ics():
    rows = [r for r in upcoming_of_kind("novel", True) + upcoming_of_kind("audiobook", True)
            if not r["estimate"]]
    rows.sort(key=lambda r: (r["date"], r["kind"]))
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Release Radar//local//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Release Radar",
        "X-WR-CALDESC:Upcoming novel & audiobook releases",
    ]
    for r in rows:
        d = parse_date(r["date"])
        if not d:
            continue
        icon = "🎧" if r["kind"] == "audiobook" else "📖"
        summary = f"{icon} {r['title']} — {r['kind']}"
        desc_bits = [r["series"]]
        if r.get("author"):
            # scraped bylines sometimes keep their own "by " prefix
            desc_bits.append("by " + re.sub(r"^by\s+", "", r["author"], flags=re.I))
        # book_n is part of the key: one series can release two same-kind books on
        # one day, and calendar clients dedupe by UID — a collision loses an event
        uid = f"{r['series_id']}-{r['date']}-{r['kind']}-b{r.get('book_n')}@release-radar"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(' · '.join(desc_bits))}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fl for ln in lines for fl in _ics_fold(ln)) + "\r\n"


