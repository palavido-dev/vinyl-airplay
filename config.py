#!/usr/bin/env python3
"""Vinyl AirPlay: paths, persisted settings, and the shared Jinja2 template env.

Leaf module (stdlib + Jinja2 only) so both main.py and the app-state / router
modules can import it without any circular dependency back into main.
"""

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

SETTINGS_FILE = Path("settings.json")
TEMPLATES     = Jinja2Templates(directory="templates")


def load_settings() -> dict:
    defaults = {
        "saved_devices": [],
        "volume": 80,
        "audio_device_index": None,
        "bass": 0,
        "treble": 0,
        "discogs_token": "",
        "hidden_devices": [],
        "auto_stream_enabled": False,
        "auto_stream_device": None,
        "device_names": {},
        "audio_storage_path": "",
        "device_volumes": {},
        "http_stream_enabled": False,
        "http_stream_bitrate_kbps": 256,
    }
    if SETTINGS_FILE.exists():
        s = json.loads(SETTINGS_FILE.read_text())
        for k, v in defaults.items():
            s.setdefault(k, v)
        return s
    return defaults


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))
