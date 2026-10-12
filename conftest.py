"""Shared pytest plumbing for every test module in this folder.

Hard guarantee that no test can write the user's live library: a test that
exercises a persisting endpoint (/api/ingest, /api/settings, /api/buy/...) calls
save_state(), which writes DATA_FILE - the real %APPDATA%\\ReleaseRadar\\data.json.
One test that forgot to stub it once overwrote a 20-series library with its own
fixture. Every file path is pointed at a temp directory for the whole suite;
individual tests may still stub save_state, but they no longer have to remember.
"""
import importlib.util
import os
import subprocess
import sys

import pytest

# app.py is composed from src/ parts (see compose_app.py). Recompose before the
# module loads whenever a part is newer, so tests can never run stale code.
_HERE = os.path.dirname(os.path.abspath(__file__))
subprocess.run([sys.executable, os.path.join(_HERE, "compose_app.py")], check=True)

_spec = importlib.util.spec_from_file_location(
    "radar", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"))
radar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radar)


@pytest.fixture(autouse=True)
def never_touch_the_real_data_file(tmp_path, monkeypatch):
    monkeypatch.setattr(radar, "DATA_FILE", str(tmp_path / "data.json"))
    # the filer's machine-readable report feeds the dashboard's "Held books" and
    # the tunnel: a test run once left a pytest temp path in the live one
    try:
        import file_audiobooks as _fa
        monkeypatch.setattr(_fa, "REPORT_PATH", str(tmp_path / "filer-report.json"))
    except ImportError:
        pass
    monkeypatch.setattr(radar, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(radar, "_LINES_DIR", str(tmp_path / "digest_lines"))
