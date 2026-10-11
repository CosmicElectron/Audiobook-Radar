"""
Release Radar - a local audiobook release tracker.

Runs a small local web server, opens a dashboard in your browser, checks your
tracked series once a day for new audiobook announcements/releases, predicts a
likely audiobook window from the historical novel->audiobook gap, and publishes
an RSS feed of your next three releases.

Run it:        python app.py
Build an exe:  see build.bat (produces dist/ReleaseRadar.exe)

The dashboard lives at:   http://127.0.0.1:8765/
The RSS feed lives at:     http://127.0.0.1:8765/feed.xml
"""

import os
import re
import sys
import json
import time
import uuid
import random
import html
import statistics
import threading
import base64
import subprocess
import sqlite3
import smtplib
import hashlib
import secrets
from email.message import EmailMessage
import logging
import webbrowser
from datetime import datetime, date, timedelta
from email.utils import format_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from http.client import InvalidURL

from flask import Flask, request, jsonify, Response

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:  # pragma: no cover - bs4 is in requirements, but degrade anyway
    HAVE_BS4 = False

APP_NAME = "Release Radar"
DEFAULT_PORT = 8765
# Bind address. Default 127.0.0.1 (localhost only — safest). Set RADAR_HOST=0.0.0.0
# only if a Cloudflare tunnel / reverse proxy on a DIFFERENT machine must reach this
# app over the LAN. Doing so exposes an unauthenticated control panel on your
# network, so pair it with auth in front (e.g. Cloudflare Access).
HOST = os.environ.get("RADAR_HOST", "127.0.0.1")
FEED_ITEM_COUNT = 3            # "next three releases", per the brief
BOARD_ITEM_COUNT = 6          # how many to show on the new-tab board view
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

