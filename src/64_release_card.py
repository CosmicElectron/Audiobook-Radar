# ---- living release card --------------------------------------------------
# One Discord message per audiobook release that keeps its whole story:
# detected -> approval -> bought -> downloaded -> filed. Appended lines edit
# the same message instead of posting new ones.
RELEASE_CARD_KEEP_DAYS = 7


def _release_card_create(sid, n, text, embed=None):
    url = _discord_live_start(text, embeds=[embed] if embed else None)
    if not url:
        return False
    with _lock:
        _state.setdefault("release_live", {})[f"{sid}:{n}"] = {
            "url": url, "base": text, "lines": [], "at": _buy_now()}
        save_state()
    return True


def _release_card_line(sid, n, line, replace_last=False):
    """Append one progress line to the release card (no-op if none exists)."""
    if sid is None or n is None:
        return False
    with _lock:
        rec = _state.get("release_live", {}).get(f"{sid}:{n}")
        if not rec:
            return False
        lines = rec.setdefault("lines", [])
        if replace_last and lines:
            lines[-1] = line
        elif not lines or lines[-1] != line:
            lines.append(line)
        rec["lines"] = lines[-12:]
        text = (rec["base"] + "\n" + "\n".join("\u2003" + l for l in rec["lines"]))[:1900]
        url = rec["url"]
        save_state()
    threading.Thread(target=_discord_live_edit, args=(url, text), daemon=True).start()
    return True


def _release_card_prune():
    with _lock:
        live = _state.get("release_live", {})
        cutoff = datetime.now() - timedelta(days=RELEASE_CARD_KEEP_DAYS)
        stale = []
        for k, rec in live.items():
            try:
                if datetime.fromisoformat(rec.get("at")) < cutoff:
                    stale.append(k)
            except (TypeError, ValueError):
                stale.append(k)
        for k in stale:
            del live[k]



