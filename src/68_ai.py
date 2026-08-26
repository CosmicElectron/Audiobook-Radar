# ---- AI series research --------------------------------------------------
# For dormant/overdue series: ask an LLM with WEB SEARCH what's actually going
# on (author announced a date? series concluded? publisher dropped it?) and pin
# the answer to the card instead of guessing from cadence maths alone.
# Provider + key live in Settings (write-only). Raw HTTP on purpose: the exe
# bundles no SDKs, and four providers share one tiny call shape.
AI_INTEL_TTL_DAYS = 14          # re-research a series at most this often
AI_DAILY_MAX = 3                # automatic research calls per day (cost bound)
AI_TIMEOUT_S = 120

AI_DEFAULT_MODELS = {
    "claude-code": "sonnet",       # headless Claude Code: plan-usage friendly default
    "anthropic": "claude-opus-5",
    "openrouter": "perplexity/sonar",
    "openai": "gpt-5-mini",
    "grok": "grok-3-mini",
}

_ai_day = {"date": None, "used": 0}


def _ai_conf():
    st = get_settings()
    key = (st.get("ai_api_key") or "").strip()
    prov = st.get("ai_provider") or "openrouter"
    if prov == "claude-code":
        # runs the local Claude Code CLI under someone's own subscription login -
        # no API key involved; configured iff the CLI is installed
        import shutil as _sh
        exe = _sh.which("claude")
        if not exe:
            return None
        model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS["claude-code"]
        return {"provider": prov, "key": "", "model": model, "exe": exe}
    if not key:
        return None
    model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS.get(prov, "")
    return {"provider": prov, "key": key, "model": model}


def _ai_http(url, payload, headers):
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json", "User-Agent": APP_NAME, **headers})
    with urlopen(req, timeout=AI_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ai_call(prompt):
    """One web-search-capable completion; returns the model's text or raises."""
    conf = _ai_conf()
    if not conf:
        raise RuntimeError("no AI key configured")
    prov, key, model = conf["provider"], conf["key"], conf["model"]
    if prov == "claude-code":
        # Headless Claude Code with web search, billed to the subscription the
        # CLI is logged into (usage limits apply; a maxed-out window errors).
        # Run from data_dir so it never picks up a project's CLAUDE.md context.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            [conf["exe"], "-p", prompt, "--model", model,
             "--tools", "WebSearch", "WebFetch",
             "--allowedTools", "WebSearch", "WebFetch"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(AI_TIMEOUT_S, 240), cwd=data_dir(), creationflags=flags,
            stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"claude CLI failed ({r.returncode}): "
                               f"{(r.stderr or r.stdout or '').strip()[:200] or 'no output'}")
        out = (r.stdout or "").strip()
        if not out:
            raise RuntimeError("claude CLI returned nothing")
        return out
    if prov == "anthropic":
        d = _ai_http("https://api.anthropic.com/v1/messages",
                     {"model": model, "max_tokens": 2048,
                      "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}],
                      "messages": [{"role": "user", "content": prompt}]},
                     {"x-api-key": key, "anthropic-version": "2023-06-01"})
        if d.get("stop_reason") == "refusal":
            raise RuntimeError("model declined the request")
        return "\n".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    if prov == "openai":
        d = _ai_http("https://api.openai.com/v1/responses",
                     {"model": model, "tools": [{"type": "web_search"}], "input": prompt},
                     {"Authorization": f"Bearer {key}"})
        out = []
        for item in d.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        out.append(c.get("text", ""))
        return "\n".join(out)
    if prov == "grok":
        d = _ai_http("https://api.x.ai/v1/chat/completions",
                     {"model": model, "search_parameters": {"mode": "auto"},
                      "messages": [{"role": "user", "content": prompt}]},
                     {"Authorization": f"Bearer {key}"})
        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    # openrouter (default): sonar-style models search on their own
    d = _ai_http("https://openrouter.ai/api/v1/chat/completions",
                 {"model": model, "messages": [{"role": "user", "content": prompt}]},
                 {"Authorization": f"Bearer {key}"})
    return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def _ai_prompt_for(srs):
    tl = sorted([e for e in srs.get("timeline", []) if _entry_n(e) is not None],
                key=lambda e: _entry_n(e))
    hist = "; ".join(f"book {_entry_n(e)}: novel {e.get('novel') or '?'}"
                     + (f", audio {e.get('audiobook')}" if e.get("audiobook") else "")
                     for e in tl[-5:])
    return (
        f"Research the book series '{srs.get('name', '')}' by {srs.get('author') or 'unknown author'} "
        f"(LitRPG/progression fantasy, sold on Amazon/Audible/Royal Road).\n"
        f"Known release history (may be incomplete): {hist or 'none recorded'}.\n"
        f"Today is {today().isoformat()}.\n\n"
        "Search the web for CURRENT information: has the author announced the next book or its audiobook, "
        "is the series complete, on hiatus, or cancelled? Author newsletters, Patreon, Reddit "
        "(r/litrpg, r/ProgressionFantasy), publisher (Aethon) announcements and Amazon pre-order pages are good sources.\n\n"
        "Answer with ONLY a JSON object, no other text:\n"
        '{"status": "ongoing|completed|hiatus|cancelled|unknown",\n'
        ' "next_book": {"n": <number or null>, "kind": "novel|audiobook|null", "expected": "<date or timeframe or null>"},\n'
        ' "summary": "<1-2 factual sentences, cite where the information came from>",\n'
        ' "confidence": "high|medium|low"}'
    )


def _ai_parse(text):
    m = re.search(r"\{(?:.|\n)*\}", text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict) or "status" not in d:
        return None
    return {"status": str(d.get("status", "unknown"))[:12],
            "next_book": d.get("next_book") if isinstance(d.get("next_book"), dict) else None,
            "summary": str(d.get("summary", ""))[:400],
            "confidence": str(d.get("confidence", ""))[:8]}


def _ai_research_series(sid, manual=False):
    """Run one research pass for a series; stores the result on the series and
    pings Discord when the picture changed. Returns (ok, message)."""
    if not _ai_conf():
        return False, "no AI key configured (Settings \u2192 Integrations)"
    with _lock:
        srs = _find(sid)
        if not srs:
            return False, "unknown series"
        prompt = _ai_prompt_for(srs)
        old = (srs.get("intel") or {}).get("status")
        name = srs.get("name", "")
    try:
        raw = _ai_call(prompt)
    except Exception as e:      # noqa: BLE001 - HTTP/auth/parse all end up here
        add_log(f"AI research failed for {name}: {e}")
        return False, f"research failed: {e}"
    intel = _ai_parse(raw)
    if not intel:
        add_log(f"AI research for {name}: unparseable answer ({(raw or '')[:120]!r})")
        return False, "the model's answer wasn't parseable"
    intel["at"] = _buy_now()
    with _lock:
        srs = _find(sid)
        if srs is not None:
            srs["intel"] = intel
            save_state()
    add_log(f"AI research: {name} -> {intel['status']} ({intel['confidence']})")
    nb = intel.get("next_book") or {}
    if (intel["status"] != (old or intel["status"]) or nb.get("expected")) and intel["status"] != "unknown":
        bits = f" Next: book {nb.get('n')} {nb.get('kind') or ''} ~{nb.get('expected')}." if nb.get("expected") else ""
        _discord_notify(f"\U0001f50e **{name}** research: {intel['status']}.{bits}\n{intel['summary']}")
    return True, intel["status"]


def _ai_candidates():
    """Series worth researching: overdue audio or dormant, intel stale/missing."""
    out = []
    cutoff = datetime.now() - timedelta(days=AI_INTEL_TTL_DAYS)
    with _lock:
        for srs in _state.get("series", []):
            intel = srs.get("intel") or {}
            try:
                fresh = datetime.fromisoformat(intel.get("at", "")) > cutoff
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                continue
            tl = srs.get("timeline") or []
            dates = [parse_date(e.get(k)) for e in tl for k in ("novel", "audiobook")]
            dates = [d for d in dates if d]
            newest = max(dates) if dates else None
            dormant = newest is not None and (today() - newest).days > 270
            overdue = bool(srs.get("audio_overdue")) or any(
                parse_date(e.get("novel")) and not e.get("audiobook")
                and (today() - parse_date(e.get("novel"))).days > 180 for e in tl)
            if dormant or overdue:
                out.append((newest or today(), srs["id"]))
    out.sort()
    return [sid for _, sid in out]


def _ai_tick():
    if not _ai_conf():
        return
    d = today().isoformat()
    if _ai_day["date"] != d:
        _ai_day.update({"date": d, "used": 0})
    if _ai_day["used"] >= AI_DAILY_MAX:
        return
    for sid in _ai_candidates()[: AI_DAILY_MAX - _ai_day["used"]]:
        _ai_day["used"] += 1
        threading.Thread(target=_ai_research_series, args=(sid,), daemon=True).start()


_ai_running = set()


@app.post("/api/series/<sid>/research")
def api_series_research(sid):
    """Kick one research pass off in the background and answer at once - the
    call takes 1-3 minutes (web search) and the Check popup must not hang."""
    if not _ai_conf():
        return jsonify({"ok": False, "message": "no AI configured (Settings \u2192 Integrations)"}), 400
    with _lock:
        if not _find(sid):
            return jsonify({"ok": False, "message": "unknown series"}), 404
        if sid in _ai_running:
            return jsonify({"ok": True, "message": "already researching \u2014 give it a couple of minutes"})
        _ai_running.add(sid)

    def _go():
        try:
            _ai_research_series(sid, manual=True)
        finally:
            _ai_running.discard(sid)

    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"ok": True, "message": "AI check started \u2014 the result lands on the card "
                                           "(and Discord if the picture changed) in a couple of minutes."})



def _buy_tick():
    with _lock:
        now = datetime.now()
        changed = False
        for job in _state.get("buy_jobs", []):
            st = job.get("status")
            if st in ("waiting_offer", "needs_signin"):
                try:
                    created = datetime.fromisoformat(job.get("created"))
                    last = datetime.fromisoformat(job.get("last_probe") or job.get("created"))
                except (TypeError, ValueError):
                    continue
                if (now - created).days >= BUY_PROBE_DAYS or int(job.get("probes") or 0) >= BUY_PROBE_MAX:
                    _job_set(job, "failed", "still signed out when the window closed — buy it by hand" if st == "needs_signin"
                             else "no companion offer appeared within the window")
                    changed = True
                elif (now - last) >= timedelta(hours=BUY_PROBE_GAP_H):
                    _buy_push_probe(job)
                    changed = True
            elif st == "retry_later":
                try:
                    due = datetime.fromisoformat(job.get("retry_at"))
                except (TypeError, ValueError):
                    due = now
                if now >= due:
                    _job_set(job, "running", f"retrying the purchase (try {int(job.get('store_retries') or 0) + 1})")
                    _push_command(type="buy", phase="execute", job=_job_cmd(job))
                    changed = True
            elif st == "awaiting_approval":
                try:
                    if datetime.fromisoformat(job.get("expires")) < now:
                        _job_set(job, "expired", "approval link expired")
                        changed = True
                except (TypeError, ValueError):
                    pass
        if changed:
            save_state()


