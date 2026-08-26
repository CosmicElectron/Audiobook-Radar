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

import pytest

_spec = importlib.util.spec_from_file_location(
    "radar", os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"))
radar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radar)


@pytest.fixture(autouse=True)
def never_touch_the_real_data_file(tmp_path, monkeypatch):
    monkeypatch.setattr(radar, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(radar, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(radar, "_LINES_DIR", str(tmp_path / "digest_lines"))
