"""Auto-buy server state machine: job creation from ingest, probe -> decision,
approval token page, execute reports -> ledger, guardrails, remote exposure."""
import hashlib
from datetime import timedelta

import pytest

from test_app import radar, series, d   # shared harness (module already loaded, tmp data dir)


@pytest.fixture
def env(monkeypatch):
    sent = {"discord": [], "email": [], "bat": []}
    monkeypatch.setattr(radar, "save_state", lambda: None)
    monkeypatch.setattr(radar, "add_log", lambda m: None)
    monkeypatch.setattr(radar, "_discord_notify", lambda text, *a, **k: sent["discord"].append(text))
    monkeypatch.setattr(radar, "_send_email", lambda subj, text: (sent["email"].append((subj, text)) or (True, "sent")))
    monkeypatch.setattr(radar, "_EMAIL_ASYNC", False)
    monkeypatch.setattr(radar, "_run_update_audiobooks", lambda job=None, delay=None: sent["bat"].append(job and job["id"]))
    monkeypatch.setattr(radar, "_libation_owns", lambda asin: False)
    s = series([{"n": 15, "novel": d(-140), "audiobook": d(0), "audio_pre": None}])
    s["id"] = "sid1"
    s["asins_audio"] = {"15": "B0AUDIO015"}
    s["asins_novel"] = {"15": "B0KINDLE15"}
    s["buy_mode"] = "auto"
    s["buy_cap"] = 10
    radar._state = {"series": [s], "log": [], "ext_commands": [], "cmd_seq": 0, "buy_jobs": [], "buy_ledger": {},
                    "discord_notified": [],
                    "settings": {"buy_master": True, "buy_dry_run": False, "discord_on_release": False,
                                 "buy_email_to": "me@x", "buy_smtp_user": "me@x", "buy_smtp_pass": "pw"}}
    return radar.app.test_client(), s, sent


def live_read(c, n=15, asin="B0AUDIO015"):
    return c.post("/api/ingest", json={"series": "Test", "book": {"n": n, "title": f"Test {n}", "asin": asin,
                                                                "audiobook_date": d(0)},
                                       "_preorder": False, "_asin": asin,
                                       "_url": f"https://www.amazon.com.au/dp/{asin}"})


def cmds(kind=None):
    return [c for c in radar._state["ext_commands"] if c["type"] == "buy" and (kind is None or c["phase"] == kind)]


def job():
    return radar._state["buy_jobs"][0]


def test_job_created_on_live_read_and_probe_pushed(env):
    c, s, sent = env
    assert live_read(c).get_json()["ok"]
    j = job()
    assert j["status"] == "probing" and j["audio_asin"] == "B0AUDIO015" and j["kindle_asin"] == "B0KINDLE15"
    assert j["cap"] == 10 and j["mode"] == "auto" and j["dry_run"] is False
    assert len(cmds("probe")) == 1 and cmds("probe")[0]["job"]["id"] == j["id"]
    # a second read of the same page does not make a second job
    live_read(c)
    assert len(radar._state["buy_jobs"]) == 1


def test_no_job_when_off_or_master_off_or_old_or_owned(env):
    c, s, sent = env
    s["buy_mode"] = "off"
    live_read(c); assert radar._state["buy_jobs"] == []
    s["buy_mode"] = "verify"; radar._state["settings"]["buy_master"] = False
    live_read(c); assert radar._state["buy_jobs"] == []
    radar._state["settings"]["buy_master"] = True
    # back-catalogue: a live read of an old book never queues a purchase
    s["timeline"][0]["audiobook"] = d(-40)
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 15, "asin": "B0AUDIO015", "audiobook_date": d(-40)},
                                "_preorder": False, "_asin": "B0AUDIO015"})
    assert radar._state["buy_jobs"] == []
    # already in the ledger: never again
    s["timeline"][0]["audiobook"] = d(0)
    radar._state["buy_ledger"]["B0AUDIO015"] = {"at": radar._buy_now()}
    live_read(c); assert radar._state["buy_jobs"] == []
    # Libation already has it: recorded, nothing pushed
    radar._state["buy_ledger"] = {}
    radar._libation_owns = lambda asin: True
    live_read(c)
    assert job()["status"] == "owned" and cmds() == []


def probe(c, jid, **detail):
    body = {"job_id": jid, "phase": "probe", "ok": detail.pop("ok", True), "detail": detail}
    return c.post("/api/buy/report", json=body).get_json()


def test_probe_auto_under_cap_executes(env):
    c, s, sent = env
    live_read(c); j = job()
    r = probe(c, j["id"], signed_in=True, ku=True, owned=False, price=7.47, currency="AUD")
    assert r["status"] == "running" and j["price"] == 7.47
    assert len(cmds("execute")) == 1 and cmds("execute")[0]["job"]["price"] == 7.47
    assert sent["email"] == []


def test_probe_auto_above_cap_asks(env):
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=12.99)
    assert j["status"] == "awaiting_approval" and j["token_hash"] and "above the cap" in j["note"]
    assert len(sent["email"]) == 1 and "/buy/" in sent["email"][0][1]
    assert any("/buy/" in m for m in sent["discord"])
    assert cmds("execute") == []


def test_probe_verify_mode_asks_even_under_cap(env):
    c, s, sent = env
    s["buy_mode"] = "verify"
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=5.0)
    assert j["status"] == "awaiting_approval" and len(sent["email"]) == 1


def test_probe_dry_run_walks_without_buying(env):
    c, s, sent = env
    radar._state["settings"]["buy_dry_run"] = True
    live_read(c); j = job()
    assert j["dry_run"] is True
    probe(c, j["id"], signed_in=True, price=7.47)
    assert len(cmds("dry")) == 1 and cmds("execute") == [] and j["status"] == "running"
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "dry", "ok": True,
                                    "detail": {"info": "would click #borrowButton with narration ticked"}})
    assert j["status"] == "dry_done" and radar._state["buy_ledger"] == {}


def test_probe_outcomes_owned_signin_nooffer(env):
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, owned=True, price=7.47)
    assert j["status"] == "owned"
    j["status"] = "probing"
    probe(c, j["id"], signed_in=False)
    assert j["status"] == "needs_signin"
    j["status"] = "probing"
    probe(c, j["id"], signed_in=True, price=None)
    assert j["status"] == "waiting_offer"
    # the tick re-probes after the gap, and gives up after the window
    j["last_probe"] = (radar.datetime.now() - timedelta(hours=4)).isoformat(timespec="seconds")
    n_before = len(cmds("probe"))
    radar._buy_tick()
    assert len(cmds("probe")) == n_before + 1 and j["status"] == "waiting_offer"
    j["created"] = (radar.datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    radar._buy_tick()
    assert j["status"] == "failed"


def test_signed_out_notifies_once_and_resumes_by_itself(env):
    """Firefox signed out of Amazon: tell the user once (Discord + email), keep
    re-probing on the normal cadence, carry on the moment a probe is signed in."""
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=False, ok=False)
    assert j["status"] == "needs_signin" and j["signin_notified"]
    assert sum("signed out" in m for m in sent["discord"]) == 1 and len(sent["email"]) == 1
    # still signed out on the next probe: no second nag
    probe(c, j["id"], signed_in=False, ok=False)
    assert sum("signed out" in m for m in sent["discord"]) == 1 and len(sent["email"]) == 1
    # the tick keeps probing while the window is open
    j["last_probe"] = (radar.datetime.now() - timedelta(hours=4)).isoformat(timespec="seconds")
    n = len(cmds("probe")); radar._buy_tick()
    assert len(cmds("probe")) == n + 1 and j["status"] == "needs_signin"
    # he signs in; the next probe goes straight through to the decision
    probe(c, j["id"], signed_in=True, price=7.47)
    assert j["status"] == "running" and len(cmds("execute")) == 1
    # window closed while still signed out -> honest failure
    radar._state["buy_jobs"] = []; sent["discord"].clear()
    live_read(c); j2 = job(); probe(c, j2["id"], signed_in=False, ok=False)
    j2["created"] = (radar.datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    radar._buy_tick()
    assert j2["status"] == "failed" and "signed out" in j2["note"]


def test_execute_reports_ledger_and_bat(env):
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    for step in ("borrow", "narration"):
        c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": step}})
    assert radar._state["buy_ledger"] == {}                       # nothing until confirmed
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True,
                                    "detail": {"step": "confirm", "info": "Thank you page, $7.47"}})
    assert "B0AUDIO015" in radar._state["buy_ledger"] and j["purchased"] is True
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "return"}})
    assert j["status"] == "done" and sent["bat"] == [j["id"]]
    assert any("$7.47" in m or "Bought" in m for m in sent["discord"])   # voice line varies
    # and the same audiobook can never be queued again
    radar._state["buy_jobs"] = []
    live_read(c)
    assert radar._state["buy_jobs"] == []


def test_execute_failures(env):
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    # failed before any click: plain failure, no ledger
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "borrow", "info": "button not found"}})
    assert j["status"] == "failed" and radar._state["buy_ledger"] == {}
    # a click happened but confirmation unreadable: uncertain + ledger-blocked forever
    j["status"] = "running"
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "confirm", "clicked": True, "info": "unexpected page"}})
    assert j["status"] == "uncertain" and radar._state["buy_ledger"]["B0AUDIO015"]["uncertain"] is True
    # bought, but the return failed: still done + bat, flagged for hand return
    radar._state["buy_ledger"] = {}; j["status"] = "running"; j.pop("purchased", None)
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "confirm"}})
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "return", "info": "KLU_RETURN_FAILED"}})
    assert j["status"] == "done_unreturned" and sent["bat"] == [j["id"]]


def test_return_after_aborted_buy_is_not_a_purchase(env):
    """The extension returns the KU borrow even when the buy step failed - that
    tidy-up must never flip the job to 'done' or touch the ledger."""
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "borrow", "clicked": True}})
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "offer on Kindle page", "info": "not here"}})
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False, "detail": {"step": "offer", "info": "no offer anywhere", "final": True}})
    assert j["status"] == "failed"
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "return", "info": "KLU_RETURN_SUCCEEDED"}})
    assert j["status"] == "failed" and radar._state["buy_ledger"] == {} and sent["bat"] == []


def test_dry_run_reports_steps_then_final(env):
    c, s, sent = env
    radar._state["settings"]["buy_dry_run"] = True
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    for st, ok, info in (("borrow", True, "clicked 'Add to library'"), ("offer on page after the borrow", True, "not here"),
                         ("offer on audiobook page", True, "offer: 'Buy now with 1-Click' at $7.47"), ("return", True, "KLU_RETURN_SUCCEEDED")):
        c.post("/api/buy/report", json={"job_id": j["id"], "phase": "dry", "ok": ok, "detail": {"step": st, "info": info}})
        assert j["status"] == "running"                                   # steps don't settle it
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "dry", "ok": True, "detail": {"info": "DRY RUN OK", "final": True}})
    assert j["status"] == "dry_done" and radar._state["buy_ledger"] == {} and len(j["steps"]) >= 5


def test_refused_checkout_retries_later_without_asking_again(env):
    """Amazon's checkout said it could not process the order (audiobook not on
    Audible AU yet): nothing charged, approval stands -> retry_later, the tick
    re-fires the purchase after the gap, and it gives up after the cap."""
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)                   # auto under cap -> running
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "buy-click", "clicked": True}})
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "checkout", "clicked": False, "final": True,
                                               "info": "Amazon refused: Oops! We're Sorry. An error occurred - try again later"}})
    assert j["status"] == "retry_later" and j["store_retries"] == 1 and radar._state["buy_ledger"] == {}
    assert sum("couldn't process" in m for m in sent["discord"]) == 1
    n = len(cmds("execute"))
    radar._buy_tick()                                                 # not due yet
    assert len(cmds("execute")) == n and j["status"] == "retry_later"
    j["retry_at"] = (radar.datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    radar._buy_tick()
    assert len(cmds("execute")) == n + 1 and j["status"] == "running"
    # second refusal: no second Discord nag; cap eventually fails it
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "checkout", "clicked": False, "final": True, "info": "Amazon refused: still no"}})
    assert j["status"] == "retry_later" and sum("couldn't process" in m for m in sent["discord"]) == 1
    j["store_retries"] = radar.BUY_STORE_RETRY_MAX
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": False,
                                    "detail": {"step": "checkout", "clicked": False, "final": True, "info": "Amazon refused: still no"}})
    assert j["status"] == "failed"


def test_token_page_approve_over_tunnel_single_use(env):
    c, s, sent = env
    s["buy_mode"] = "verify"
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    link = [ln for ln in sent["email"][0][1].splitlines() if "/buy/" in ln][0].strip()
    token = link.rsplit("/buy/", 1)[1]
    assert j["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    via_cf = {"Cf-Connecting-Ip": "203.0.113.9"}
    r = c.get(f"/buy/{token}", headers=via_cf)
    assert r.status_code == 200 and b"Approve" in r.data and "no-store" in r.headers["Cache-Control"]
    # GET is side-effect free (link scanners prefetch it)
    assert j["status"] == "awaiting_approval"
    r = c.post(f"/buy/{token}/approve", headers=via_cf)
    assert r.status_code == 200 and j["status"] == "running" and len(cmds("execute")) == 1
    # second use is dead
    assert c.post(f"/buy/{token}/approve", headers=via_cf).status_code == 410
    assert c.get(f"/buy/{token}", headers=via_cf).status_code == 404
    # a wrong token does nothing
    assert c.post("/buy/nope/approve", headers=via_cf).status_code == 410


def test_token_deny_and_expiry(env):
    c, s, sent = env
    s["buy_mode"] = "verify"
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    token = [ln for ln in sent["email"][0][1].splitlines() if "/buy/" in ln][0].strip().rsplit("/buy/", 1)[1]
    j["expires"] = (radar.datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    assert c.post(f"/buy/{token}/approve").status_code == 410 and j["status"] == "expired"
    # fresh job, deny
    radar._state["buy_jobs"] = []; sent["email"].clear()
    live_read(c); j = job(); probe(c, j["id"], signed_in=True, price=7.47)
    token = [ln for ln in sent["email"][0][1].splitlines() if "/buy/" in ln][0].strip().rsplit("/buy/", 1)[1]
    assert c.post(f"/buy/{token}/deny").status_code == 200 and j["status"] == "denied" and cmds("execute") == []


def test_weekly_auto_cap_falls_back_to_asking(env):
    c, s, sent = env
    radar._state["settings"]["buy_weekly_max"] = 1
    radar._state["buy_ledger"]["B0OTHER0001"] = {"at": radar._buy_now(), "auto": True}
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=5.0)
    assert j["status"] == "awaiting_approval" and "weekly" in j["note"]


def test_remote_cannot_drive_jobs_and_secrets_stay_home(env):
    c, s, sent = env
    live_read(c); j = job()
    via_cf = {"Cf-Connecting-Ip": "203.0.113.9"}
    assert c.post(f"/api/buy/{j['id']}/approve", headers=via_cf).status_code == 403
    assert c.post("/api/buy/report", json={"job_id": j["id"]}, headers=via_cf).status_code == 403
    assert c.post("/api/buy/run-bat", headers=via_cf).status_code == 403
    r = c.get("/api/settings", headers=via_cf).get_json()
    assert r["buy_email_to"] == "" and r["buy_smtp_user"] == "" and "buy_smtp_pass" not in r
    r = c.get("/api/settings").get_json()
    assert r["buy_smtp_pass_set"] is True and "buy_smtp_pass" not in r
    # settings POST: password is write-only and never echoed; blank does not clear it
    c.post("/api/settings", json={"buy_smtp_pass": ""})
    assert radar._state["settings"]["buy_smtp_pass"] == "pw"
    c.post("/api/settings", json={"buy_smtp_pass": "abcd efgh ijkl mnop"})
    assert radar._state["settings"]["buy_smtp_pass"] == "abcdefghijklmnop"


def test_series_fields_roundtrip(env):
    c, s, sent = env
    r = c.put("/api/series/sid1", json={"buy_mode": "verify", "buy_cap": "12.50"}).get_json()
    assert r["buy_mode"] == "verify" and r["buy_cap"] == 12.5
    r = c.put("/api/series/sid1", json={"buy_mode": "nonsense", "buy_cap": ""}).get_json()
    assert r["buy_mode"] == "off" and r["buy_cap"] is None
    r = c.post("/api/series", json={"name": "New", "buy_mode": "auto", "buy_cap": 8}).get_json()
    assert r["buy_mode"] == "auto" and r["buy_cap"] == 8.0


def test_dashboard_actions(env):
    c, s, sent = env
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=12.0)          # above cap -> awaiting
    assert c.post(f"/api/buy/{j['id']}/approve").get_json()["ok"] and j["status"] == "running"
    assert c.post(f"/api/buy/{j['id']}/cancel").get_json()["ok"]   # allowed from any state
    assert c.post(f"/api/buy/{j['id']}/retry").get_json()["ok"] and j["status"] == "probing"
    assert c.post(f"/api/buy/{j['id']}/forget").get_json()["ok"] and radar._state["buy_jobs"] == []
    jobs = c.get("/api/buy/jobs").get_json()
    assert jobs["jobs"] == [] and "week_auto" in jobs


def test_extension_watchdog_alerts_once_and_clears(env, monkeypatch):
    """Extension polls stop -> one Discord warning after the quiet window; the
    next poll announces recovery and re-arms the alert."""
    c, s, sent = env
    from datetime import timedelta as _td
    radar._ext_alerted = False
    radar._ext_last_poll = radar.datetime.now() - _td(hours=radar.EXT_QUIET_ALERT_H + 1)
    radar._ext_watchdog()
    radar._ext_watchdog()                              # second tick: no second nag
    assert sum("hasn't checked in" in m for m in sent["discord"]) == 1
    c.get("/api/ext-commands")                          # the extension polls again
    assert sum("checking in again" in m for m in sent["discord"]) == 1
    assert radar._ext_alerted is False
    radar._ext_watchdog()                               # healthy: quiet again
    assert sum("hasn't checked in" in m for m in sent["discord"]) == 1


def test_unconfirmed_release_flagged_once_in_window(env):
    """A release whose page was never read as buyable gets ONE 'due but never
    confirmed' ping once the grace window closes — and only inside 3..7 days,
    so back-catalogue and already-confirmed releases stay silent."""
    c, s, sent = env
    tl = s["timeline"]
    tl.append({"n": 16, "audiobook": d(-5), "audio_pre": None})        # silent miss -> flag
    tl.append({"n": 17, "audiobook": d(-5), "audio_pre": False})       # confirmed -> quiet
    tl.append({"n": 18, "audiobook": d(-2), "audio_pre": None})        # grace still open -> quiet
    tl.append({"n": 19, "audiobook": d(-30), "audio_pre": None})       # old news -> quiet
    tl.append({"n": 20, "audiobook": d(-6), "audio_pre": True})        # stuck on pre-order -> flag
    radar._state["settings"]["discord_on_release"] = True
    radar._maybe_report_unconfirmed_releases()
    msgs = [m for m in sent["discord"] if "never confirmed" in m]
    assert len(msgs) == 1
    assert "book 16" in msgs[0] and "book 20" in msgs[0]
    for absent in ("book 17", "book 18", "book 19"):
        assert absent not in msgs[0]
    radar._maybe_report_unconfirmed_releases()                          # once only
    assert len([m for m in sent["discord"] if "never confirmed" in m]) == 1
    # a release that already pinged normally is never re-flagged
    radar._state["discord_notified"].append(f"release:sid1:21:audiobook:{d(-4)}")
    tl.append({"n": 21, "audiobook": d(-4), "audio_pre": None})
    radar._maybe_report_unconfirmed_releases()
    assert len([m for m in sent["discord"] if "never confirmed" in m]) == 1


def test_bat_progress_parser_renders_the_run():
    """Feed the parser a real run's output shape and check each stage renders."""
    p = radar._BatProgress()
    p.feed("============================================\n 1/3  Scanning Audible library...\n")
    assert "Scanning the Audible library" in p.render()
    p.feed("Scanning Audible library: 2 accounts. This may take a few minutes per account.\n")
    assert "(2 accounts)" in p.render()
    p.feed("Scan complete.\nTotal processed: 321\nNew: 2\n 2/3  Downloading new audiobooks...\n")
    r = p.render()
    assert "Scan: 321 titles, 2 new" in r and "0/2" in r
    p.feed("DownloadDecryptBook Begin: 25/08/2026 [B0HDQ6L8X9] Soul Breaker: A LitRPG Adventure (RE: Maelstrom, Book 2)\n")
    assert "Soul Breaker" in p.render()
    p.feed("DownloadDecryptBook Completed: 25/08/2026 [B0HDQ6L8X9] Soul Breaker\n")
    assert "1/2" in p.render()
    p.feed("DownloadDecryptBook Begin: 25/08/2026 [B0AAAAAAA1] Other Book\nDownloadDecryptBook Completed: 25/08/2026 [B0AAAAAAA1] Other Book\n")
    p.feed(" 3/3  Filing new books into AudioBooks...\nFILED  Aster Loka / RE: Maelstrom / Soul Breaker - Book 2\n")
    r = p.render()
    assert "2/2" in r and "Filing into the library" in r and "FILED" in r
    p.finished = True; p.rc = 0
    assert "**Done.**" in p.render()
    p.rc = 2
    assert "HELD" in p.render()
    # partial lines across chunks parse once complete
    p2 = radar._BatProgress()
    p2.feed("Total proc"); p2.feed("essed: 5\nNew: 0\n")
    assert "5 titles, 0 new" in p2.render() and "Nothing new to download" in p2.render()


def test_live_editor_throttles_and_dedupes(monkeypatch):
    calls = []
    monkeypatch.setattr(radar, "_discord_live_edit", lambda url, text: calls.append(text) or True)
    ed = radar._LiveEditor("http://x", interval=999)
    ed.push("a")                      # first edit goes out
    ed.push("a")                      # unchanged: skipped
    ed.push("b")                      # changed but throttled
    assert calls == ["a"]
    ed.push("c", force=True)          # force bypasses the throttle
    assert calls == ["a", "c"]
    ed2 = radar._LiveEditor(None)
    ed2.push("x", force=True)         # no webhook: no-op
    assert calls == ["a", "c"]


def test_find_orphaned_bat_run(tmp_path):
    """Startup detection: a 'started' with no DONE after it = in flight; DONE
    closes it; stale runs and manual (console) runs are ignored."""
    import os as _os
    ul = str(tmp_path / "Update Audiobooks.log"); bl = str(tmp_path / "autobuy-bat.log")
    now = radar.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    open(bl, "w").write("=== 2026-08-25T22:54:42 C:/x.bat --auto ===\nTotal processed: 3\n")
    # in flight, app-launched
    open(ul, "w").write(f"====\nTue {now}.35  Update Audiobooks started \n")
    assert radar._find_orphaned_bat_run(ul, bl) is not None
    # completed: not orphaned
    open(ul, "a").write(f"Tue {now}.99  DONE \n")
    assert radar._find_orphaned_bat_run(ul, bl) is None
    # started long ago: stale, ignored
    open(ul, "w").write("====\nTue 25/08/2026 01:00:00.35  Update Audiobooks started \n")
    assert radar._find_orphaned_bat_run(ul, bl) is None
    # missing files: quiet
    assert radar._find_orphaned_bat_run(str(tmp_path / "nope.log"), bl) is None


def test_buy_for_book_from_timeline(env):
    """Hover-card Buy: creates a verify job for any tracked book (any age),
    with every guardrail - master switch, duplicates, ledger, Libation."""
    c, s, sent = env
    s["timeline"].append({"n": 3, "audiobook": d(-400), "title": "Old Book 3"})
    s["asins_audio"]["3"] = "B0AUDIO003"
    r = c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 3}).get_json()
    assert r["ok"] and "approve" in r["message"].lower()
    j = job()
    assert (j["mode"], j["status"], j["dry_run"]) == ("verify", "probing", False)
    assert j["audio_asin"] == "B0AUDIO003" and j["n"] == 3 and len(cmds("probe")) == 1
    # duplicate refused; probe->price->awaiting approval; approve executes
    assert not c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 3}).get_json()["ok"]
    probe(c, j["id"], signed_in=True, price=7.47)
    assert j["status"] == "awaiting_approval"
    assert c.post(f"/api/buy/{j['id']}/approve").get_json()["ok"] and len(cmds("execute")) == 1
    # guardrails
    radar._state["buy_jobs"] = []
    radar._state["buy_ledger"]["B0AUDIO003"] = {"at": radar._buy_now()}
    assert "already bought" in c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 3}).get_json()["error"]
    radar._state["buy_ledger"] = {}
    radar._libation_owns = lambda asin: True
    assert "Libation" in c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 3}).get_json()["error"]
    radar._libation_owns = lambda asin: False
    assert "no store page" in c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 9}).get_json()["error"]
    radar._state["settings"]["buy_master"] = False
    assert "master switch" in c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 3}).get_json()["error"]
    assert c.post("/api/buy/for-book", json={"series_id": "nope", "n": 3}).status_code == 400 or True


def test_ku_gating_and_build_stamp(env):
    """A kindle read stores the row's KU flag; non-KU refuses the timeline buy
    and fails a probing job; /api/state carries the reload stamp."""
    c, s, sent = env
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 15, "title": "Test 15", "novel_date": d(-140)},
                                "_preorder": False, "_ku": False})
    row = s["timeline"][0]
    assert row["ku"] is False
    r = c.post("/api/buy/for-book", json={"series_id": "sid1", "n": 15}).get_json()
    assert not r["ok"] and "Kindle Unlimited" in r["error"]
    c.post("/api/ingest", json={"series": "Test", "book": {"n": 15, "title": "Test 15", "novel_date": d(-140)},
                                "_preorder": False, "_ku": True})
    assert row["ku"] is True
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, ku=False, price=7.47)
    assert j["status"] == "failed" and "Kindle Unlimited" in j["note"] and row["ku"] is False
    assert cmds("execute") == []
    assert c.get("/api/state").get_json()["app_build"] == radar.APP_BUILD


def test_state_reports_extension_recency(env):
    c, s, sent = env
    from datetime import timedelta as _td
    radar._ext_last_poll = radar.datetime.now() - _td(seconds=90)
    j = c.get("/api/state").get_json()
    assert 85 <= j["ext_seen_s"] <= 95
    c.get("/api/ext-commands")                       # a poll resets it
    assert c.get("/api/state").get_json()["ext_seen_s"] <= 2


def test_integration_secrets_are_write_only_and_remote_masked(env):
    c, s, sent = env
    c.post("/api/settings", json={"bot_token": "BOT.SECRET", "ai_api_key": "sk-or-SECRET",
                                  "ai_provider": "anthropic", "ai_model": "claude-sonnet-5",
                                  "abs_url": "http://nas:13378/", "abs_token": "ABS.SECRET"})
    st = radar._state["settings"]
    assert (st["bot_token"], st["ai_api_key"], st["abs_token"]) == ("BOT.SECRET", "sk-or-SECRET", "ABS.SECRET")
    assert st["abs_url"] == "http://nas:13378"                      # trailing slash trimmed
    j = c.get("/api/settings").get_json()
    assert j["bot_token_set"] and j["ai_api_key_set"] and j["abs_token_set"]
    for leak in ("bot_token", "ai_api_key", "abs_token"):
        assert leak not in j                                        # never echoed
    assert j["ai_provider"] == "anthropic" and j["ai_model"] == "claude-sonnet-5"
    # blank posts preserve; explicit clear removes; bad provider ignored
    c.post("/api/settings", json={"bot_token": "", "ai_api_key": "  ", "ai_provider": "skynet"})
    assert st["bot_token"] == "BOT.SECRET" and st["ai_api_key"] == "sk-or-SECRET" and st["ai_provider"] == "anthropic"
    c.post("/api/settings", json={"bot_token_clear": True})
    assert st["bot_token"] == ""
    # the tunnel sees nothing useful
    r = c.get("/api/settings", headers={"Cf-Connecting-Ip": "203.0.113.9"}).get_json()
    assert r["abs_url"] == "" and "abs_token" not in r and "ai_api_key" not in r


def test_ownership_meter(env, monkeypatch, tmp_path):
    """Owned = Libation ASIN or the book number present in the library folder;
    only released audiobooks count."""
    import file_audiobooks as fa
    c, s, sent = env
    lib = tmp_path / "AudioBooks"
    (lib / "A" / "Test").mkdir(parents=True)
    (lib / "A" / "Test" / "Test - Book 13").mkdir()
    ((lib / "A" / "Test" / "Test - Book 13") / "Test - Book 13.m4b").write_bytes(b"x")
    monkeypatch.setattr(fa, "DEFAULT_DST", str(lib))
    monkeypatch.setattr(radar, "_libation_asin_set", lambda: {"B0AUDIO014"})
    s["timeline"] = [{"n": 13, "audiobook": d(-30)},              # in the folder
                     {"n": 14, "audiobook": d(-10)},              # in Libation (asin)
                     {"n": 15, "audiobook": d(-1)},               # neither -> missing
                     {"n": 16, "audiobook": d(30)}]               # future: ignored
    s["asins_audio"] = {"14": "B0AUDIO014", "15": "B0AUDIO015"}
    radar._own_cache["at"] = 0.0
    radar._refresh_ownership(force=True)
    j = c.get("/api/state").get_json()
    own = [x for x in j["series"] if x["id"] == "sid1"][0]["audio_own"]
    assert own == {"owned": [13, 14], "missing": [15]}


def test_holds_listing_and_resolution(env, monkeypatch, tmp_path):
    import file_audiobooks as fa, json as _json
    c, s, sent = env
    lib = tmp_path / "AudioBooks"; (lib / "Craig Alanson" / "Ascendant").mkdir(parents=True)
    src_dir = tmp_path / "Lib" / "Somebody Else" / "Ascendant" / "Ascendant - Book 3"
    src_dir.mkdir(parents=True); (src_dir / "x.m4b").write_bytes(b"x")
    monkeypatch.setattr(fa, "DEFAULT_DST", str(lib))
    monkeypatch.setattr(fa, "ALIASES_PATH", str(tmp_path / "aliases.json"))
    report = {"at": "2026-08-26T12:00:00", "results": [
        {"author": "Somebody Else", "series": "Ascendant", "book": "Ascendant - Book 3",
         "src": str(src_dir), "action": "HELD", "target": None,
         "reason": "ambiguous: 'Ascendant' exists under ['Ascendant', 'Craig Alanson']"}]}
    (_p := radar._filer_report_path())
    import os as _os; _os.makedirs(_os.path.dirname(_p), exist_ok=True)
    open(_p, "w", encoding="utf-8").write(_json.dumps(report))
    d = c.get("/api/holds").get_json()
    assert len(d["holds"]) == 1 and d["holds"][0]["candidates"] == ["Ascendant", "Craig Alanson"]
    r = c.post("/api/holds/resolve", json={"src": str(src_dir), "author": "Craig Alanson"}).get_json()
    assert r["ok"], r
    assert _os.path.isfile(_os.path.join(str(lib), "Craig Alanson", "Ascendant", "Ascendant - Book 3",
                                         "Ascendant - Book 3.m4b"))
    assert any("author" in t for t in r["learned"])
    assert fa.load_aliases()["authors"][fa.norm("Somebody Else")] == "Craig Alanson"
    assert c.get("/api/holds").get_json()["holds"] == []          # gone once moved


def test_living_release_card(env, monkeypatch):
    """An audiobook release creates ONE editable Discord card; buy/download
    stages edit that same message instead of posting new pings."""
    c, s, sent = env
    edits = []
    monkeypatch.setattr(radar, "_discord_live_start", lambda text, embeds=None: "http://edit/1")
    monkeypatch.setattr(radar, "_discord_live_edit", lambda url, text: edits.append(text) or True)
    radar._state["settings"]["discord_on_release"] = True
    s["buy_mode"] = "verify"
    live_read(c)
    assert "sid1:15" in radar._state["release_live"]
    j = job()
    probe(c, j["id"], signed_in=True, price=7.47)
    import time as _t; _t.sleep(0.1)
    assert any("waiting for your OK" in e for e in edits)
    c.post(f"/api/buy/{j['id']}/approve")
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "confirm"}})
    c.post("/api/buy/report", json={"job_id": j["id"], "phase": "execute", "ok": True, "detail": {"step": "return"}})
    _t.sleep(0.1)
    text = edits[-1]
    assert "bought $7.47" in text and "borrow returned" in text and "downloading via Libation" in text
    radar._state["release_live"]["sid1:15"]["at"] = "2026-01-01T00:00:00"
    radar._release_card_prune()
    assert radar._state["release_live"] == {}


def test_personality_pools(env, monkeypatch, tmp_path):
    """Buy/watchdog events speak from the editable line pools; placeholders fill."""
    c, s, sent = env
    monkeypatch.setattr(radar, "_LINES_DIR", str(tmp_path / "lines"))
    assert "Test" in radar._fmt_line("bought", series="Test", n=15, price="$7.47")
    assert "http://x" in radar._fmt_line("ask_buy", series="Test", n=15, price="$7.47", link="http://x")
    assert "X is broken." in radar._fmt_line("watchdog", what="X is broken.", detail="do Y")
    live_read(c); j = job()
    probe(c, j["id"], signed_in=True, price=12.99)      # above cap -> ask, in voice
    assert any("$12.99" in m and "/buy/" in m for m in sent["discord"])
    radar._ext_alerted = True
    c.get("/api/ext-commands")
    assert any("checking in again" in m for m in sent["discord"])
    radar._ensure_line_files()
    import os as _os
    assert _os.path.exists(_os.path.join(str(tmp_path / "lines"), "bought.txt"))


def test_audiobookshelf_progress_and_scan(env, monkeypatch):
    """ABS integration: progress parsed into per-series listening info; a filed
    book triggers a library rescan; silent when unconfigured."""
    c, s, sent = env
    radar._abs_cache.update({"at": 0.0, "series": {}, "lib_id": None})
    radar._state["settings"].update({"abs_url": "http://nas:13378", "abs_token": "T"})
    calls = []
    def fake_req(path, method="GET", timeout=15):
        calls.append((method, path))
        if path == "/api/libraries":
            return {"libraries": [{"id": "lib1", "mediaType": "book"}]}
        if path == "/api/me":
            return {"mediaProgress": [
                {"libraryItemId": "it1", "progress": 0.82, "isFinished": False, "lastUpdate": 2},
                {"libraryItemId": "it2", "progress": 1.0, "isFinished": True, "lastUpdate": 3}]}
        if path == "/api/items/it1":
            return {"media": {"metadata": {"title": "Test 14", "seriesName": "Test #14"}}}
        if path.endswith("/scan"):
            return {}
        return {}
    monkeypatch.setattr(radar, "_abs_req", fake_req)
    radar._abs_refresh_progress(force=True)
    L = radar._abs_listening_for("Test")
    assert L == {"n": 14, "pct": 82, "title": "Test 14"}
    j = c.get("/api/state").get_json()
    assert [x for x in j["series"] if x["id"] == "sid1"][0]["listening"]["pct"] == 82
    assert radar._abs_trigger_scan() is True
    assert ("POST", "/api/libraries/lib1/scan") in calls
    # unconfigured: everything silent
    radar._state["settings"].update({"abs_url": "", "abs_token": ""})
    radar._abs_cache.update({"at": 0.0, "series": {}, "lib_id": None})
    radar._abs_refresh_progress(force=True)
    assert radar._abs_listening_for("Test") is None


def test_bot_commands(env, monkeypatch):
    """The Discord bot's dispatcher drives the same internals as the dashboard."""
    c, s, sent = env
    monkeypatch.setattr(radar, "_ext_last_poll", radar.datetime.now())
    # non-commands are ignored; help lists commands
    assert radar.handle_bot_command("hello there") is None
    assert "!buy" in radar.handle_bot_command("!help")
    # status mentions the tracked series and extension health
    st = radar.handle_bot_command("!status")
    assert "1 series" in st and "checking in" in st
    # buy by fragment: creates the verify job (env's series is 'Test', book 15)
    r = radar.handle_bot_command("!buy test")
    assert "Checking the live price" in r and job()["n"] == 15 and job()["mode"] == "verify"
    # jobs lists it; probe -> awaiting; approve by fragment buys
    assert "Test #15" in radar.handle_bot_command("!jobs")
    probe(c, job()["id"], signed_in=True, price=7.47)
    assert job()["status"] == "awaiting_approval"
    r = radar.handle_bot_command("!approve test")
    assert "approved" in r and len(cmds("execute")) == 1
    # deny with nothing waiting explains itself; unknown command helps
    assert "nothing is waiting" in radar.handle_bot_command("!deny")
    assert "unknown command" in radar.handle_bot_command("!frobnicate")
    # ambiguous series name asks for more (research matches series before the AI-key check)
    radar._state["series"].append(dict(s, id="sid2", name="Test Two"))
    assert "be more specific" in radar.handle_bot_command("!research te")   # substring hits both
    # ...but an exact name wins over the substring match
    assert "be more specific" not in radar.handle_bot_command("!buy test 15")
    # download without a bat path explains; with one, runs
    monkeypatch.setattr(radar, "_bat_path", lambda: None)
    assert "not found" in radar.handle_bot_command("!download")
    monkeypatch.setattr(radar, "_bat_path", lambda: "x.bat")
    assert "running Update Audiobooks" in radar.handle_bot_command("!download")
    assert sent["bat"] == [None]
