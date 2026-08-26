# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------

def build_rss(base_url):
    mode, est = _upcoming_settings()
    items = upcoming_of_kind(mode, est)[:FEED_ITEM_COUNT]
    kind_word = "novel" if mode == "novel" else "audiobook"
    now = format_datetime(datetime.now().astimezone())
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>{html.escape(APP_NAME)} - Next {FEED_ITEM_COUNT} releases</title>",
        f"<link>{html.escape(base_url)}</link>",
        f"<description>Upcoming {kind_word} releases you are tracking.</description>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in items:
        d = parse_date(it["date"])
        pub = format_datetime(datetime(d.year, d.month, d.day).astimezone()) if d else now
        kind = "Audiobook" if it["kind"] == "audiobook" else "Novel"
        est = " (estimated)" if it["estimate"] else ""
        title = f"{it['title']} - {kind} - {it['date']}{est}"
        desc = (
            f"{html.escape(it['series'])}"
            + (f" by {html.escape(it['author'])}" if it['author'] else "")
            + f". {kind} releasing {it['date']}{est}. "
            + f"{it['countdown_days']} day(s) to go."
        )
        guid = f"{it['series_id']}-{it['date']}-{it['kind']}-b{it.get('book_n')}"
        parts += [
            "<item>",
            f"<title>{html.escape(title)}</title>",
            f"<description>{desc}</description>",
            f"<pubDate>{pub}</pubDate>",
            f'<guid isPermaLink="false">{html.escape(guid)}</guid>',
            "</item>",
        ]
    parts.append("</channel></rss>")
    return "\n".join(parts)


