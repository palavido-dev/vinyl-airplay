#!/usr/bin/env python3
"""Vinyl AirPlay: automatic capture (ADC) gain for known sound cards.

Some capture ADCs (notably the HiFiBerry DAC+ ADC Pro) ship with their analog
PGA gain at 0 dB, which leaves a line or phono input too quiet for auto-record
to trigger (issue #40). On startup we detect a known card by its ALSA name and
set a configured analog gain via amixer, with the value exposed in Settings so
the user keeps control and can dial in headroom for their turntable.
"""

import re
import subprocess

# Known capture ADCs, matched (substring, case-insensitive) against the ALSA
# card id/name in /proc/asound/cards. "controls" are the amixer simple-control
# names for the analog capture PGA, applied together for L+R.
KNOWN_ADCS = [
    {
        "match": "hifiberry_dacplusadcpro",
        "label": "HiFiBerry DAC+ ADC Pro",
        "controls": ["PGA Gain Left", "PGA Gain Right"],
        "min_db": -12.0,
        "max_db": 40.0,
    },
    {
        "match": "hifiberry_dacplusadc",
        "label": "HiFiBerry DAC+ ADC",
        "controls": ["PGA Gain Left", "PGA Gain Right"],
        "min_db": -12.0,
        "max_db": 40.0,
    },
]

# Conservative default: a useful lift for quiet line/phono inputs while leaving
# headroom before clipping on loud pressings. Users tune it in Settings.
DEFAULT_GAIN_DB = 6.0
GAIN_STEP_DB = 0.5


def _read_cards() -> list[tuple[int, str, str]]:
    """Return [(index, id, name)] parsed from /proc/asound/cards."""
    cards = []
    try:
        with open("/proc/asound/cards") as f:
            text = f.read()
    except OSError:
        return cards
    # e.g. " 2 [sndrpihifiberry]: HifiberryDacpAd - snd_rpi_hifiberry_dacplusadcpro"
    for m in re.finditer(r"^\s*(\d+)\s+\[([^\]]+)\]:\s*(.+)$", text, re.M):
        cards.append((int(m.group(1)), m.group(2).strip(), m.group(3).strip()))
    return cards


def detect_adc() -> tuple[int | None, dict | None]:
    """Return (card_index, profile) for the first known capture ADC, else (None, None)."""
    for idx, cid, name in _read_cards():
        hay = f"{cid} {name}".lower()
        for prof in KNOWN_ADCS:
            if prof["match"] in hay:
                return idx, prof
    return None, None


def _clamp_snap(gain_db: float, prof: dict) -> float:
    g = max(prof["min_db"], min(prof["max_db"], float(gain_db)))
    return round(g / GAIN_STEP_DB) * GAIN_STEP_DB


def apply_gain(gain_db: float, card_index: int | None = None,
               profile: dict | None = None) -> tuple[bool, str]:
    """Set the analog capture gain on a known ADC. Returns (ok, message)."""
    if card_index is None or profile is None:
        card_index, profile = detect_adc()
    if card_index is None:
        return False, "No known capture ADC detected"
    g = _clamp_snap(gain_db, profile)
    val = f"{g:.1f}dB"
    errs = []
    for ctrl in profile["controls"]:
        try:
            subprocess.run(
                ["amixer", "-c", str(card_index), "sset", ctrl, val],
                check=True, capture_output=True, text=True, timeout=5,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            errs.append(f"{ctrl}: {e}")
    if errs:
        return False, "; ".join(errs)
    return True, f"{profile['label']} capture gain set to {val}"


def read_gain(card_index: int | None = None,
              profile: dict | None = None) -> float | None:
    """Return the current gain in dB from the first control, or None."""
    if card_index is None or profile is None:
        card_index, profile = detect_adc()
    if card_index is None:
        return None
    try:
        out = subprocess.run(
            ["amixer", "-c", str(card_index), "sget", profile["controls"][0]],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"Item0:\s*'(-?[\d.]+)dB'", out)
    return float(m.group(1)) if m else None


def status() -> dict:
    """Detection + current gain, for the Settings UI."""
    idx, prof = detect_adc()
    if idx is None:
        return {"detected": False}
    return {
        "detected": True,
        "label": prof["label"],
        "card_index": idx,
        "current_db": read_gain(idx, prof),
        "min_db": prof["min_db"],
        "max_db": prof["max_db"],
        "step_db": GAIN_STEP_DB,
        "default_db": DEFAULT_GAIN_DB,
    }
