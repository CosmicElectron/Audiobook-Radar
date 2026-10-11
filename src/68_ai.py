# ---- AI series research --------------------------------------------------
# For dormant/overdue series: ask an LLM with WEB SEARCH what's actually going
# on (author announced a date? series concluded? publisher dropped it?) and pin
# the answer to the card instead of guessing from cadence maths alone.
# Provider + key live in Settings (write-only). Raw HTTP on purpose: the exe
# bundles no SDKs, and four providers share one tiny call shape.
AI_INTEL_TTL_DAYS = 14          # re-research a series at most this often
AI_DAILY_MAX = 3                # automatic research calls per day (cost bound)
AI_TIMEOUT_S = 120
AI_AUTH_CACHE_S = 3600          # a CLI sign-in check stays trusted this long
AI_EFFORT = "max"               # a few calls a night; thoroughness is the whole point
AI_FAIL_NIGHTS_ALERT = 2        # whole batches failing this many nights running -> a person

# One headless CLI at a time. Three parallel runs raced to refresh the same
# OAuth token on 11 Sep 2026; the refresh rotates the token, the losers hit the
# server with a stale one and the CLI signed itself out for good - four silent
# nights of "OAuth session expired and could not be refreshed".
_ai_cli_lock = threading.Lock()
_ai_cli_auth = {"ok": None, "at": 0.0}
_ai_worker_busy = False
_ai_worker_since = 0.0
AI_HUNG_S = 1800                # a batch older than this is stuck: say so (the CLI timeouts should make it impossible)

AI_AUTH_WHAT = ("AI research is down: the Claude Code CLI on this PC is signed out, "
                "so the nightly series research can't run.")
AI_AUTH_DETAIL = ("On the PC, open a terminal and run `claude auth login` — or run `claude setup-token` "
                  "and paste the token into Settings → Integrations → AI key. Research resumes by itself.")
AI_AUTH_HINT = ("the Claude Code CLI is signed out on this PC — run `claude auth login` in a terminal "
                "(or paste a `claude setup-token` token in Settings), then try again")
# the same failure class for the API providers is a bad key, not a sign-out
AI_KEY_WHAT = "AI research is down: the {prov} API rejected the key."
AI_KEY_DETAIL = "Paste a valid key in Settings → Integrations → AI key."
AI_KEY_HINT = "the {prov} API rejected the key — paste a valid one in Settings → Integrations, then try again"
_SHIM_EXT = (".cmd", ".bat", ".ps1")


def _ai_cli_exe():
    """The real claude executable. npm installs a claude.cmd shim on Windows;
    running that goes through cmd.exe, which ends the command line at the first
    newline of the (multi-line) prompt - every flag and the JSON instructions
    were silently dropped for weeks (the 7 Sep 2026 'permission' apology).
    Resolve the binary the shim points at; if that fails, hand the shim back so
    the UI still says 'configured' and _ai_call can explain the refusal."""
    import shutil as _sh
    exe = _sh.which("claude")
    if not exe or os.path.splitext(exe)[1].lower() not in _SHIM_EXT:
        return exe
    for cand in (os.path.join(os.path.dirname(exe), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
                 os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")):
        if os.path.isfile(cand):
            return cand
    return exe


def _ai_env(conf):
    """Child environment for the CLI: ours minus every Claude Code / Anthropic
    session variable. Started from a Claude Code session, the app would pass
    CLAUDECODE / CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH etc. down, and the CLI then
    hands its OAuth refresh to a 'host' that doesn't exist; an ANTHROPIC_API_KEY
    would silently bill the API instead of the subscription. A configured
    long-lived token goes in explicitly."""
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith(("CLAUDE", "ANTHROPIC")) or k.upper() in ("TRACEPARENT", "TRACESTATE"))}
    if conf.get("token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = conf["token"]
    return env

AI_DEFAULT_MODELS = {
    "claude-code": "opus",         # a few calls a night, and the answer's quality is the whole point
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
        exe = _ai_cli_exe()
        if not exe:
            return None
        model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS["claude-code"]
        # the key field doubles as an optional long-lived token (`claude setup-token`)
        return {"provider": prov, "key": "", "token": key, "model": model, "exe": exe}
    if not key:
        return None
    model = (st.get("ai_model") or "").strip() or AI_DEFAULT_MODELS.get(prov, "")
    return {"provider": prov, "key": key, "model": model}


def _ai_http(url, payload, headers):
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json", "User-Agent": APP_NAME, **headers})
    with urlopen(req, timeout=AI_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _ai_error_kind(msg):
    """auth = a person must sign the CLI in again; limit = wait; other."""
    m = (msg or "").lower()
    if re.search(r"authenticat|oauth|logged in|log in|login|sign in|signed in|credential|\b401\b|\b403\b", m):
        return "auth"
    if re.search(r"usage limit|rate limit|\b429\b|overloaded|too many requests|quota", m):
        return "limit"
    return "other"


def _ai_cli_logged_in(force=False):
    """Cheap pre-flight (no model call): is the Claude Code CLI signed in?
    True/False, or None when it can't tell (old CLI, timeout)."""
    conf = _ai_conf()
    if not conf or conf["provider"] != "claude-code":
        return None
    if conf.get("token"):
        # A long-lived token bypasses the CLI's own session, and `auth status`
        # can't vouch for it - only a real call can. Unknown, not "signed in":
        # otherwise a bad token would be cleared as fixed five minutes after
        # every failure.
        return None
    now = time.time()
    if not force and _ai_cli_auth["at"] and now - _ai_cli_auth["at"] < AI_AUTH_CACHE_S:
        return _ai_cli_auth["ok"]
    if not _ai_cli_lock.acquire(timeout=5):
        return None                     # a research call is running: can't tell, don't wait
    ok = None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([conf["exe"], "auth", "status", "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, cwd=data_dir(), creationflags=flags, stdin=subprocess.DEVNULL,
                           env=_ai_env(conf))
        ok = bool(json.loads(r.stdout or "{}").get("loggedIn"))   # exit code is 1 when signed out
    except Exception:                                             # noqa: BLE001
        ok = None
    finally:
        _ai_cli_lock.release()
    _ai_cli_auth.update({"ok": ok, "at": now})
    return ok


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
        # The tool set is pinned to web search/fetch (no shell, files or MCP
        # servers), which is what makes skipping the permission prompts safe:
        # in print mode an unapproved tool is silently denied and the model
        # then answers with a "permission hasn't come through" apology instead
        # of research (7 Sep 2026). One CLI at a time - see _ai_cli_lock.
        if os.path.splitext(conf["exe"])[1].lower() in _SHIM_EXT:
            raise RuntimeError("only the npm claude.cmd shim was found and it mangles multi-line "
                               "prompts — reinstall the Claude Code CLI")
        env = _ai_env(conf)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with _ai_cli_lock:
            r = subprocess.run(
                [conf["exe"], "-p", prompt, "--model", model,
                 "--tools", "WebSearch", "WebFetch",
                 "--allowedTools", "WebSearch", "WebFetch",
                 "--permission-mode", "bypassPermissions", "--allow-dangerously-skip-permissions",
                 "--strict-mcp-config", "--effort", AI_EFFORT],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=max(AI_TIMEOUT_S, 240), cwd=data_dir(), creationflags=flags,
                stdin=subprocess.DEVNULL, env=env)
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


def _ai_prompt_for(srs, missed=None):
    tl = sorted([e for e in srs.get("timeline", []) if _entry_n(e) is not None],
                key=lambda e: _entry_n(e))
    hist = "; ".join(f"book {_entry_n(e)}: novel {e.get('novel') or '?'}"
                     + (f", audio {e.get('audiobook')}" if e.get("audiobook") else "")
                     for e in tl[-5:])
    focus = ""
    if missed:
        focus = (f"Based on the author's usual pace, book {(srs.get('novel_n') or 0) + 1} was expected around "
                 f"{missed.isoformat()} and has not appeared - find out why and when it is now expected.\n")
    # the specific gaps in the data are the questions - "research this series"
    # came back as a restatement of the Amazon listings we already had
    questions = []
    novels = [e for e in tl if parse_date(e.get("novel"))]
    if novels:
        last = novels[-1]
        if parse_date(last["novel"]) <= today():
            questions.append(f"novel {_entry_n(last) + 1}: nothing known - when?")
        else:
            questions.append(f"anything announced beyond book {_entry_n(last)} (due {last['novel']})?")
    if any(e.get("audiobook") for e in tl):
        undated = [str(_entry_n(e)) for e in tl if e.get("novel") and not e.get("audiobook")]
        if undated:
            questions.append(f"audiobook{'s' if len(undated) > 1 else ''} for book{'s' if len(undated) > 1 else ''} "
                             f"{', '.join(undated)}: no date known - what has the author or the studio said?")
    if questions:
        focus += "Open questions, in order of importance: " + " / ".join(questions) + "\n"
    author = re.sub(r"^by\s+", "", srs.get("author") or "", flags=re.I) or "unknown author"
    name = srs.get("name", "")
    searches = (f'"{author} {name} audiobook", "{name} book {(_entry_n(novels[-1]) + 1) if novels else "next"} release", '
                f'"site:reddit.com {name} audiobook", "site:royalroad.com {name} author note", "{author} patreon {name}"')
    # links the user keeps in the series notes (its Royal Road page, Patreon, a
    # Discord announcement mirror) are the author's own places: read them first
    links = re.findall(r"https?://\S+", srs.get("notes") or "")
    if links:
        focus += "Read these pages FIRST, they are the author's own: " + " ".join(links[:4]) + "\n"
    # what the browser extension harvested from Royal Road (the research tool
    # itself gets a 403 there): the author's announcements, notes and replies
    rr = srs.get("rr_notes") or {}
    if rr.get("items"):
        lines, size = [], 0
        for it in sorted(rr["items"], key=lambda i: i.get("date") or "", reverse=True):
            line = f"[{(it.get('date') or '')[:10]} · {it.get('chapter', '')[:60]} · {it.get('kind', '')}] {it.get('text', '')[:700]}"
            if size + len(line) > 3500:
                break
            lines.append(line)
            size += len(line)
        focus += ("The author's own words on Royal Road, collected by the browser (newest first) - timing stated "
                  "here is authoritative, quote it in 'said':\n" + "\n".join(lines) + "\n")
    # Schedule-only brief. The first version asked for "1-2 factual sentences"
    # and got trivia back (where the series is serialised, what it is about);
    # the user wants what he'd find himself: the author's own words on WHEN -
    # "book 10 in November, then 11-13 back to back" - with the source.
    return (
        f"You research release dates for ONE book series. Find what the AUTHOR or PUBLISHER has said about "
        f"WHEN the next books come out - novels and audiobooks. Nothing else matters: no plot, no trivia "
        f"(where it is serialised, Kindle Unlimited, awards, narrators), nothing about books already out.\n"
        f"Series: '{name}' by {author}. "
        f"Already known - do NOT repeat any of it, a pre-order listed here is not news: "
        f"{hist or 'nothing recorded'}. Today is {today().isoformat()}.\n{focus}"
        f"Run at least four different searches before answering, such as: {searches}. "
        "Then READ pages, don't stop at search snippets: on Royal Road open the fiction's newest chapters - "
        "the author's note and the author's own replies in the chapter COMMENTS are where 'book 10 is with "
        "the narrator, out in November' gets said. Also the author's Patreon posts, their Discord or Reddit "
        "posts (r/litrpg, r/ProgressionFantasy), their newsletter or website, and studio/publisher announcements "
        "(Aethon, Podium, Soundbooth, Tantor). An Amazon or Audible listing alone is not an answer. "
        "Prefer the author's own words and note WHEN they said it. A plan like 'book 10 in November, then "
        "11 to 13 back to back' is exactly what is wanted - give each book its own entry, with the month. "
        "If all you find is progress (drafting, chapters going up on Patreon), report that as the summary "
        "with its month and give no dated entries. Found nothing beyond what is already known? Then "
        "releases is [] and the summary is exactly \"no release news found\" - never fill the space with "
        "the series' history or where it is published.\n\n"
        "Answer with ONLY this JSON, no other text, no padding:\n"
        '{"status": "ongoing|completed|hiatus|cancelled|unknown",\n'
        ' "releases": [{"n": <book number>, "kind": "novel|audiobook", '
        '"expected": "<YYYY-MM-DD, or YYYY-MM, or \\"early 2027\\" / \\"Q1 2027\\" / \\"spring 2027\\">", '
        '"said": "<their words, max 15 words>", "source": "<who, where, when - e.g. author on Patreon, Sep 2026>"}],\n'
        ' "royalroad": "<the fiction\'s Royal Road URL if you came across it, else null>",\n'
        ' "summary": "<ONE sentence, max 25 words, release timing only; exactly \\"no release news found\\" if there is none>",\n'
        ' "confidence": "high|medium|low"}'
    )


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PART_DAY = {"early": 5, "mid": 15, "late": 25}
_SEASON_MONTH = {"spring": 4, "summer": 7, "autumn": 10, "fall": 10, "winter": 1}


def _intel_parse_when(text):
    """An author's 'when' as (ISO date, precision) or None. Vague forms land on
    a representative day - the 15th of a month, the middle of a quarter or
    season, mid-year - and the precision says how much to trust the day."""
    t = re.sub(r"[,.]", " ", (text or "").strip().lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t or t in ("null", "none", "tba", "tbd", "tbc", "unknown", "unannounced"):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return (t, "day") if parse_date(t) else None
    m = re.fullmatch(r"(\d{4})-(\d{2})", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{m.group(2)}-15", "month"
    if re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", t):
        return None                                   # ISO-shaped but not a date (2026-13)
    m = re.search(r"\b(?:(early|mid|late)[- ])?([a-z]+) (\d{4})\b", t)
    if m and m.group(2)[:3] in _MONTHS and (len(m.group(2)) <= 3 or m.group(2).startswith(("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "sept"))):
        return f"{m.group(3)}-{_MONTHS[m.group(2)[:3]]:02d}-{_PART_DAY.get(m.group(1), 15):02d}", "month"
    m = re.search(r"\bq([1-4]) (\d{4})\b", t)
    if m:
        return f"{m.group(2)}-{int(m.group(1)) * 3 - 1:02d}-15", "quarter"
    m = re.search(r"\bh([12]) (\d{4})\b", t)
    if m:
        return (f"{m.group(2)}-03-31", "half") if m.group(1) == "1" else (f"{m.group(2)}-09-30", "half")
    m = re.search(r"\b(spring|summer|autumn|fall|winter) (?:of )?(\d{4})\b", t)
    if m:
        return f"{m.group(2)}-{_SEASON_MONTH[m.group(1)]:02d}-15", "season"
    m = re.search(r"\b(?:(early|mid|late)[- ])?(\d{4})\b", t)
    if m:
        day = {"early": "02-15", "mid": "06-30", "late": "11-15"}.get(m.group(1), "07-01")
        return f"{m.group(2)}-{day}", "year"
    return None


def _intel_apply(srs, intel):
    """Store each author-stated date on the series ('kind:n' -> record) so the
    estimates use it. A book that already has a real date is left alone.
    Returns the entries applied, for the log."""
    now = _buy_now()
    tl = srs.get("timeline") or []
    out = srs.setdefault("intel_dates", {})
    applied = []
    rr = (intel.get("royalroad") or "").strip()
    if rr.startswith("https://www.royalroad.com/fiction/") and not srs.get("rr_url"):
        srs["rr_url"] = rr                           # next time the browser reads the author's notes first
    for r in intel.get("releases") or []:
        n, kind = r.get("n"), r.get("kind")
        if n is None or kind not in ("novel", "audiobook"):
            continue
        when = _intel_parse_when(r.get("expected"))
        if not when:
            continue
        if any(_entry_n(e) == n and e.get(kind) for e in tl):
            continue                                  # the store already dated it
        out[f"{kind}:{n}"] = {"date": when[0], "precision": when[1], "expected": r.get("expected") or "",
                              "said": r.get("said") or "", "source": r.get("source") or "", "at": now}
        applied.append(f"{kind} {n} ~{r.get('expected')}")
    return applied


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

    def _s(v, cap):
        return "" if v is None or str(v).strip().lower() in ("", "null", "none") else str(v).strip()[:cap]

    releases = []
    for r in (d.get("releases") or []) if isinstance(d.get("releases"), list) else []:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("n"))
        except (TypeError, ValueError):
            n = None
        kind = _s(r.get("kind"), 12).lower()
        releases.append({"n": n, "kind": "audiobook" if kind.startswith("audio") else ("novel" if kind.startswith("novel") else kind),
                         "expected": _s(r.get("expected"), 60), "said": _s(r.get("said"), 160), "source": _s(r.get("source"), 120)})
    return {"status": str(d.get("status", "unknown"))[:12],
            "releases": releases,
            "royalroad": _s(d.get("royalroad"), 200),
            "summary": _s(d.get("summary"), 200),
            "confidence": str(d.get("confidence", ""))[:8]}


def _ai_intel_line(intel):
    """One line for the activity log: the dated entries, else the summary."""
    rel = [f"{r['kind']} {r['n']} ~{r['expected']}" + (f" ({r['source']})" if r.get("source") else "")
           for r in intel.get("releases") or [] if r.get("expected")]
    main = "; ".join(rel) or intel.get("summary") or ""
    if not main or main.lower().startswith("no release news"):
        main = "no release news found"
    st = intel.get("status")
    return f"series {st}: {main}" if st in ("completed", "hiatus", "cancelled") else main


def _ai_research_series(sid, manual=False, why=None):
    """Run one research pass for a series; stores the result on the series and
    pings Discord when the picture changed - always, when the pass was
    triggered by a missed estimate (why="overdue"). Returns (ok, message)."""
    if not _ai_conf():
        return False, "no AI key configured (Settings \u2192 Integrations)"
    with _lock:
        srs = _find(sid)
        if not srs:
            return False, "unknown series"
        missed = predicted_novel_date(srs) if why == "overdue" else None
        n_next = (srs.get("novel_n") or 0) + 1
        prompt = _ai_prompt_for(srs, missed=missed)
        old = (srs.get("intel") or {}).get("status")
        name = srs.get("name", "")
    try:
        raw = _ai_call(prompt)
    except Exception as e:      # noqa: BLE001 - HTTP/auth/parse all end up here
        add_log(f"AI research failed for {name}: {e}")
        if not manual:
            _ai_mark_tried(sid)             # a hand-triggered try spent no budget: keep tonight's slot
        kind = _ai_error_kind(str(e))
        if kind == "auth":
            prov = (_ai_conf() or {}).get("provider") or "claude-code"
            if prov == "claude-code":
                _ai_cli_auth.update({"ok": False, "at": time.time()})
                _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
                return False, AI_AUTH_HINT
            _problem("ai_key", what=AI_KEY_WHAT.format(prov=prov), detail=AI_KEY_DETAIL)
            return False, AI_KEY_HINT.format(prov=prov)
        if kind == "limit":
            return False, "the AI provider's usage limit is hit right now — try again later"
        return False, f"research failed: {e}"
    intel = _ai_parse(raw)
    if not intel:
        add_log(f"AI research for {name}: unparseable answer ({(raw or '')[:120]!r})")
        if not manual:
            _ai_mark_tried(sid)
        return False, "the model's answer wasn't parseable"
    intel["at"] = _buy_now()
    # Nothing is posted anywhere: an author-stated date becomes the card's
    # estimate (with its source on hover) and the schedule, hero and digest
    # pick it up from there - the user reads dates, not research prose.
    with _lock:
        srs = _find(sid)
        if srs is not None:
            srs["intel"] = intel
            applied = _intel_apply(srs, intel)
            save_state()
        else:
            applied = []
    add_log(f"AI research: {name} -> " + ("; ".join(applied) if applied else _ai_intel_line(intel)))
    _ai_cli_auth.update({"ok": True, "at": time.time()})
    _problem_clear("ai_auth", what="AI research is working again.")
    _problem_clear("ai_key", what="AI research is working again.")
    _problem_clear("ai_failing", what="AI research is producing answers again.")
    return True, intel["status"]


# ---- the author's Royal Road notes come first --------------------------------
# Royal Road refuses the research tool (403) but not the browser, so the
# extension collects the author's announcements, notes and comment replies and
# the research reads them. Research for a series with a known Royal Road page
# waits (briefly) for a fresh harvest.
RR_NOTES_FRESH_H = 20
RR_WAIT_S = 600


def _rr_url(srs):
    u = (srs.get("rr_url") or "").strip()
    if not u:
        m = re.search(r"https?://(?:www\.)?royalroad\.com/fiction/\d+[^\s]*", srs.get("notes") or "")
        u = m.group(0) if m else ""
    return u if "royalroad.com/fiction/" in u else ""


def _rr_notes_fresh(srs):
    try:
        return datetime.now() - datetime.fromisoformat((srs.get("rr_notes") or {}).get("at") or "") < timedelta(hours=RR_NOTES_FRESH_H)
    except (TypeError, ValueError):
        return False


def _rr_request(srs):
    """Ask the extension for the author's Royal Road notes when they are stale.
    True while the research should wait for them. Caller holds _lock."""
    if not _rr_url(srs) or _rr_notes_fresh(srs):
        return False
    if datetime.now() - _ext_last_poll > timedelta(minutes=10):
        return False                                  # nobody to fetch them: research without
    try:
        asked = datetime.fromisoformat(srs.get("rr_requested_at") or "")
    except (TypeError, ValueError):
        asked = None
    if asked and datetime.now() - asked < timedelta(seconds=RR_WAIT_S):
        return True                                   # on its way
    if asked and datetime.now() - asked < timedelta(hours=24):
        return False                                  # asked, nothing came: don't hold research up again today
    srs["rr_requested_at"] = _buy_now()
    _push_command(type="rrnotes", sid=srs["id"], url=_rr_url(srs))
    add_log(f"asked the extension for {srs.get('name', '')}'s Royal Road notes before researching")
    return True


def _ai_mark_tried(sid):
    """A series that failed today leaves the automatic batch for the day, so the
    budget isn't spent re-running one broken call every five minutes."""
    with _lock:
        srs = _find(sid)
        if srs is not None:
            srs["ai_tried"] = today().isoformat()


def _novel_overdue(srs):
    """The next NOVEL is overdue: the forecast the card would show has passed
    and nothing is pre-ordered. Only this earns an automatic research call
    (the user's rule) - a missing audiobook or a merely quiet series does not;
    the manual Check button stays available for those."""
    pred = predicted_novel_date(srs)
    if not pred or pred >= today():
        return False
    return not any((parse_date(e.get("novel")) or date.min) >= today() for e in srs.get("timeline") or [])


def _ai_candidates():
    """[(sid, urgent)] for every series with an overdue novel, urgent first.

    Urgent = the estimate was missed since the series was last researched (or
    it never was): that is researched the night the estimate passes, cap or no
    cap - the trigger is the estimate itself, nothing else. The rest are
    periodic re-checks of long-overdue series, every AI_INTEL_TTL_DAYS, within
    the daily cap. Most-overdue first within each group."""
    out = []
    cutoff = datetime.now() - timedelta(days=AI_INTEL_TTL_DAYS)
    with _lock:
        for srs in _state.get("series", []):
            if srs.get("ai_tried") == today().isoformat() or not _novel_overdue(srs):
                continue
            pred = predicted_novel_date(srs)
            try:
                at = datetime.fromisoformat((srs.get("intel") or {}).get("at", ""))
            except (TypeError, ValueError):
                at = None
            urgent = at is None or at.date() <= pred      # nothing researched since the miss
            if not urgent and at > cutoff:
                continue                                   # re-check not due yet
            out.append((0 if urgent else 1, pred, srs["id"], urgent))
    out.sort()
    return [(sid, urgent) for _, _, sid, urgent in out]


def _ai_note_night(results):
    """Two consecutive all-failed batches (parse errors, timeouts, a bad model
    name...) are worth a person; one bad night isn't. Sign-out is reported on
    its own and mustn't be double-counted here."""
    if not results:
        return
    d = today().isoformat()
    with _lock:
        if any(results):
            _state["ai_fail_nights"] = 0
        elif _state.get("ai_fail_day") != d:          # several batches in a day count once
            _state["ai_fail_nights"] = int(_state.get("ai_fail_nights") or 0) + 1
        _state["ai_fail_day"] = d
        n = _state["ai_fail_nights"]
        signed_out = "ai_auth" in (_state.get("problems") or {})
        save_state()
    if n == 0:
        _problem_clear("ai_failing", what="AI research is producing answers again.")
    elif n >= AI_FAIL_NIGHTS_ALERT and not signed_out:
        _problem("ai_failing", what=f"AI research has failed {n} nights running.",
                 detail="The dashboard's activity log has the error; Settings → Integrations has the provider and model.")


def _ai_tick():
    global _ai_worker_busy, _ai_worker_since
    conf = _ai_conf()
    # problems that describe a configuration the user has since walked away
    # from must not outlive it (the banner would prescribe the wrong fix)
    if not conf:
        for key in ("ai_auth", "ai_key", "ai_failing"):
            _problem_clear(key, notify=False)
        return
    _problem_clear("ai_key" if conf["provider"] == "claude-code" else "ai_auth", notify=False)
    if _ai_worker_busy:
        if time.time() - _ai_worker_since > AI_HUNG_S:
            _problem("ai_hung", what="An AI research call has been stuck for over half an hour.",
                     detail="Quit Release Radar from the tray and start it again to free it.")
        return
    _problem_clear("ai_hung", notify=False)   # no worker: whatever was stuck is gone (restart included)
    d = today().isoformat()
    if _ai_day["date"] != d:
        _ai_day.update({"date": d, "used": 0})
    cands = _ai_candidates()
    urgent = [sid for sid, u in cands if u]
    rechecks = [sid for sid, u in cands if not u][: max(0, AI_DAILY_MAX - _ai_day["used"])]
    sids = urgent + rechecks
    with _lock:
        signed_out_open = "ai_auth" in (_state.get("problems") or {})
    if conf["provider"] == "claude-code" and (sids or signed_out_open):
        # The (cached, cheap) sign-in check runs whenever there is work or an
        # open sign-out problem, so research resumes - and the banner clears -
        # within the hour of a re-login instead of waiting for tomorrow's batch.
        signed = _ai_cli_logged_in()
        if signed is False:
            _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
            return                              # the day's budget is kept for when it works
        if signed:
            _problem_clear("ai_auth", what="The Claude Code CLI is signed in again — AI research is back.")
    if not sids:
        return
    # a series with a Royal Road page gets its author's notes harvested first;
    # it joins a later tick once they have arrived (or the wait ran out)
    with _lock:
        waiting = {sid for sid in sids if (srs := _find(sid)) is not None and _rr_request(srs)}
        if waiting:
            save_state()
    urgent = [sid for sid in urgent if sid not in waiting]
    rechecks = [sid for sid in rechecks if sid not in waiting]
    sids = urgent + rechecks
    if not sids:
        return
    _ai_day["used"] += len(rechecks)            # a just-missed estimate is researched cap or no cap
    _ai_worker_busy, _ai_worker_since = True, time.time()

    def _run():
        global _ai_worker_busy
        results = []
        try:
            for sid in sids:                    # one at a time, never in parallel
                results.append(_ai_research_series(sid, why="overdue" if sid in urgent else None)[0])
        except Exception as e:                  # noqa: BLE001 - a state write can fail; never die silently
            add_log(f"AI research batch error: {e}")
        finally:
            _ai_worker_busy = False
        try:
            _problem_clear("ai_hung", what="The stuck AI research call has finished.")
            _ai_note_night(results)
        except Exception as e:                  # noqa: BLE001
            add_log(f"AI research bookkeeping error: {e}")

    threading.Thread(target=_run, daemon=True).start()


_ai_running = set()


@app.post("/api/series/<sid>/research")
def api_series_research(sid):
    """Kick one research pass off in the background and answer at once - the
    call takes 1-3 minutes (web search) and the Check popup must not hang."""
    conf = _ai_conf()
    if not conf:
        return jsonify({"ok": False, "message": "no AI configured (Settings \u2192 Integrations)"}), 400
    if conf["provider"] == "claude-code" and _ai_cli_logged_in(force=True) is False:
        # a two-minute wait for a certain failure helps nobody: say so now
        _problem("ai_auth", what=AI_AUTH_WHAT, detail=AI_AUTH_DETAIL)
        return jsonify({"ok": False, "message": AI_AUTH_HINT}), 400
    with _lock:
        if not _find(sid):
            return jsonify({"ok": False, "message": "unknown series"}), 404
        if sid in _ai_running:
            return jsonify({"ok": True, "message": "already researching \u2014 give it a couple of minutes"})
        _ai_running.add(sid)

    with _lock:
        srs = _find(sid)
        wait = bool(srs and _rr_request(srs))
        if wait:
            save_state()

    def _go():
        try:
            if wait:
                for _ in range(RR_WAIT_S // 5):      # the harvest takes a minute or two
                    time.sleep(5)
                    with _lock:
                        s2 = _find(sid)
                        if s2 is None or _rr_notes_fresh(s2):
                            break
            _ai_research_series(sid, manual=True)
        finally:
            _ai_running.discard(sid)

    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"ok": True, "message": ("AI check started \u2014 reading the author's Royal Road notes first; "
                                            "the card's estimate updates in a few minutes." if wait else
                                            "AI check started \u2014 the card's estimate updates in a couple of minutes.")})



def _buy_tick():
    with _lock:
        now = datetime.now()
        changed = False
        for job in _state.get("buy_jobs", []):
            st = job.get("status")
            if st in ("waiting_offer", "needs_signin"):
                try:
                    # a Retry restarts the probe window (window_start)
                    created = datetime.fromisoformat(job.get("window_start") or job.get("created"))
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
                    _buy_live_begin(job)
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


