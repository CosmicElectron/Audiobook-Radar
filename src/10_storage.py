# ---------------------------------------------------------------------------
# Paths / storage
# ---------------------------------------------------------------------------

def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "ReleaseRadar")
    else:
        d = os.path.join(os.path.expanduser("~"), ".release-radar")
    os.makedirs(d, exist_ok=True)
    return d

DATA_FILE = os.path.join(data_dir(), "data.json")
LOG_FILE = os.path.join(data_dir(), "radar.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("radar")

_lock = threading.RLock()
_state = {"version": 1, "last_check": None, "series": [], "log": [], "settings": {}}


def default_state():
    return {"version": 1, "last_check": None, "series": [], "log": [], "ext_commands": [], "cmd_seq": 0,
            "settings": {}}


def load_state():
    global _state
    with _lock:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    _state = json.load(f)
                _state.setdefault("series", [])
                _state.setdefault("log", [])
                _state.setdefault("last_check", None)
                _state.setdefault("ext_commands", [])
                _state.setdefault("cmd_seq", 0)
                _state.setdefault("settings", {})
                _state.setdefault("buy_jobs", [])
                _state.setdefault("buy_ledger", {})
                _clean_stored_narrators()
            except Exception as e:
                log.exception("Could not read data file: %s", e)
                _state = default_state()
        else:
            _state = default_state()
            save_state()


def get_settings():
    with _lock:
        s = _state.setdefault("settings", {})
        s.setdefault("show_estimates", True)          # audiobook estimates
        s.setdefault("show_estimates_novel", True)    # novel estimates
        s.setdefault("upcoming_mode", "audiobook")    # which kind the schedule shows
        s.setdefault("discord_webhook", "")           # Discord channel webhook URL
        s.setdefault("discord_on_new", True)          # ping when a new/changed date arrives
        s.setdefault("discord_on_release", True)          # ping when a release goes live
        s.setdefault("discord_daily", False)              # daily forecast digest
        s.setdefault("discord_daily_hour", 9)             # 24h local hour to send it
        s.setdefault("discord_digest_days", 1)            # 1=daily, 2/3, 7=weekly
        # auto-buy (KU borrow -> companion audiobook -> return borrow)
        s.setdefault("buy_master", False)        # master switch: nothing buys while off
        s.setdefault("buy_dry_run", True)        # walk the page, click nothing, report
        s.setdefault("buy_cap_default", BUY_DEFAULT_CAP)
        s.setdefault("buy_weekly_max", BUY_WEEKLY_MAX)
        s.setdefault("buy_email_to", "")         # where approval requests go
        s.setdefault("buy_smtp_user", "")        # Gmail address that sends them
        s.setdefault("buy_smtp_pass", "")        # its App Password (not the login password)
        s.setdefault("buy_smtp_host", "smtp.gmail.com")
        s.setdefault("buy_public_base", "")      # https://your-tunnel-host — for approve links on the phone
        s.setdefault("buy_bat", "")              # Update Audiobooks.bat (auto-found when blank)
        s.setdefault("buy_libation_files", "")   # Libation files dir (auto-found when blank)
        s.setdefault("buy_tab_mode", "foreground")  # foreground | window (unfocused) | background
        # integrations (secrets are write-only: stored here, never echoed back)
        s.setdefault("bot_token", "")            # Discord bot - two-way control
        s.setdefault("ai_provider", "openrouter")  # openrouter | anthropic | openai | grok
        s.setdefault("ai_api_key", "")           # AI series research (web search)
        s.setdefault("ai_model", "")             # blank = provider default
        s.setdefault("abs_url", "")              # Audiobookshelf server, e.g. http://nas:13378
        s.setdefault("abs_token", "")            # ABS API token
        return dict(s)


# auto-buy tunables
BUY_MODES = ("off", "verify", "auto")
BUY_DEFAULT_CAP = 10.0          # above this price, "auto" behaves like "verify"
BUY_TOKEN_TTL_H = 48            # an approval link is good for two days
BUY_PROBE_MAX = 12              # re-check a release-day page this many times...
BUY_PROBE_DAYS = 4              # ...over this many days, waiting for the offer to appear
BUY_PROBE_GAP_H = 3             # ...at most once every few hours
BUY_WEEKLY_MAX = 3              # hard ceiling on AUTOMATIC purchases per 7 days
BUY_BAT_DELAY_S = 150           # give Audible's library a moment to list the title
BUY_BAT_RETRIES = 3             # re-run the bat if Libation didn't see it yet
BUY_BAT_RETRY_GAP_S = 600
BUY_STORE_RETRY_H = 3           # Amazon refused the checkout (title not live on Audible AU yet): try again in...
BUY_STORE_RETRY_MAX = 16        # ...at most this many times (~2 days)

BACKUP_DIR = os.path.join(data_dir(), "backups")
BACKUP_KEEP = 14           # rolling daily snapshots — two weeks of undo


def _daily_backup():
    """Snapshot data.json into backups/ once per day, before the day's first
    write, keeping the newest BACKUP_KEEP. Hand edits stay recoverable even when
    no bulk operation ever wrote a .bak."""
    try:
        if not os.path.exists(DATA_FILE):
            return
        dest = os.path.join(BACKUP_DIR, "data-%s.json" % today().isoformat())
        if os.path.exists(dest):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        with open(DATA_FILE, "rb") as f:
            blob = f.read()
        with open(dest, "wb") as g:
            g.write(blob)
        old = sorted(x for x in os.listdir(BACKUP_DIR)
                     if x.startswith("data-") and x.endswith(".json"))
        for x in old[:-BACKUP_KEEP]:
            os.remove(os.path.join(BACKUP_DIR, x))
    except Exception as e:
        log.info("daily backup failed: %s", e)


def save_state():
    with _lock:
        _daily_backup()
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, DATA_FILE)


def add_log(msg):
    with _lock:
        _state["log"].insert(0, {"time": datetime.now().isoformat(timespec="seconds"), "msg": msg})
        _state["log"] = _state["log"][:60]
    log.info(msg)


