# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def pick_port(preferred):
    import socket
    for p in range(preferred, preferred + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return preferred


def _make_tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (21, 19, 31, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), outline=(231, 177, 76, 255), width=4)
    d.ellipse((22, 22, 42, 42), outline=(74, 64, 103, 255), width=3)
    d.ellipse((28, 28, 36, 36), fill=(231, 177, 76, 255))
    return img


def run_in_tray(port, url):
    """Run the server with a system-tray icon (Open / Quit). Returns False if a
    tray can't be created, so the caller can fall back to a plain server."""
    if os.environ.get("RADAR_NO_TRAY") == "1":
        return False
    try:
        import pystray
    except Exception as e:
        log.info("Tray unavailable (%s); running without it.", e)
        return False

    def serve():
        try:
            app.run(host=HOST, port=port, threaded=True, use_reloader=False)
        except Exception as e:
            log.exception("server thread crashed: %s", e)
            os._exit(1)

    threading.Thread(target=serve, daemon=True).start()
    # No auto-open on startup. If a dashboard tab already exists (e.g. your pinned
    # one, even when Firefox has it idle/unloaded), the extension leaves it alone;
    # it only opens a tab when there genuinely isn't one. The tray "Open" below is
    # the explicit way to bring the dashboard up.

    def on_open(icon, item):
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def on_quit(icon, item):
        try:
            icon.stop()
        finally:
            os._exit(0)

    try:
        global _tray_icon
        icon = pystray.Icon(
            "ReleaseRadar", _make_tray_image(), "Release Radar",
            menu=pystray.Menu(
                pystray.MenuItem("Open Release Radar", on_open, default=True),
                pystray.MenuItem("Quit", on_quit),
            ),
        )
        _tray_icon = icon
        icon.run()  # blocks until Quit
        return True
    except Exception as e:
        log.info("Could not start tray icon (%s); running without it.", e)
        return False


def _radar_already_on(port):
    """True when another Release Radar instance already answers on this port."""
    try:
        req = Request(f"http://127.0.0.1:{port}/api/instance",
                      headers={"User-Agent": APP_NAME})
        with urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}").get("app") == "release-radar"
    except Exception:
        return False


def main():
    # SINGLE INSTANCE. Two copies sharing one data.json is corruption waiting to
    # happen (each holds its own in-memory state and saves over the other's) and
    # every timed job — the Discord digest included — fires once per copy. This
    # happened: an instance from 11 Aug sat on port 8766 next to the real one and
    # duplicated the digest daily. A second launch now just fronts the first.
    preferred = int(os.environ.get("RADAR_PORT", DEFAULT_PORT))
    if _radar_already_on(preferred):
        print(f"{APP_NAME} is already running at http://127.0.0.1:{preferred}/ — opening it.")
        try:
            webbrowser.open(f"http://127.0.0.1:{preferred}/")
        except Exception:
            pass
        return

    load_state()
    _ensure_line_files()
    _start_daily_digest()
    _reattach_orphaned_bat_run()
    _start_discord_bot()
    port = pick_port(preferred)
    url = f"http://127.0.0.1:{port}/"
    add_log(f"{APP_NAME} started on {url}")

    print(f"{APP_NAME} running at {url}")
    print(f"RSS feed:    {url}feed.xml")
    print(f"New-tab view: {url}board")
    print("Quit from the system-tray icon, the dashboard's Quit button, or close this window.")
    print("Your data lives in:")
    print("   " + DATA_FILE)

    # Prefer a tray icon (so the no-console exe is quittable); fall back to a
    # plain server that you quit from the dashboard or by closing the window.
    if not run_in_tray(port, url):
        app.run(host=HOST, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
