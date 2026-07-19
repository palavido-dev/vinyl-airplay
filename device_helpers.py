#!/usr/bin/env python3
"""Vinyl AirPlay: device enumeration helpers.

Local ALSA output/input discovery and the cached-Bluetooth-devices accessor,
shared by the device routes, stream coordinator, and player engine. Shares
AppState via app_state.

Audio devices are addressed by their STABLE ALSA card id (the string in
/proc/asound/cards, e.g. "sndrpihifiberry"), never by the kernel card index,
because the index is not deterministic across reboots on the Pi (the HiFiBerry
and the two vc4-hdmi cards enumerate in a non-fixed order). Selecting a device
therefore stores the card id, and the live sounddevice/ALSA index is resolved
from it only at the moment a stream is opened.
"""

import glob
import re

import sounddevice as sd

from app_state import state

CAPTURE_CHANNELS_MAX = 2  # stereo capture: HiFiBerry DAC2 ADC Pro returns silence
                          # if opened with >2 channels (empty TDM slots). App only
                          # ever processes L+R anyway, so 2 is correct for any device.


# ── ALSA card enumeration (stable, name-based) ────────────────────────────────

def _read_sound_cards() -> list[tuple[int, str, str]]:
    """Return [(index, card_id, description)] parsed from /proc/asound/cards."""
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


def _card_index_for_id(card_id: str) -> int | None:
    """Current kernel index of the card with this stable id, or None."""
    for idx, cid, _ in _read_sound_cards():
        if cid == card_id:
            return idx
    return None


def _card_has(idx: int, kind: str) -> bool:
    """kind 'p' = has a playback PCM, 'c' = has a capture PCM."""
    return bool(glob.glob(f"/proc/asound/card{idx}/pcm*{kind}"))


def _card_description(card_id: str, fallback: str = "") -> str:
    for _, cid, name in _read_sound_cards():
        if cid == card_id:
            return name
    return fallback


# ── Capture (input) device resolution ─────────────────────────────────────────

def _capture_selection() -> str | int | None:
    """The stored capture selection: a card id string (preferred), a legacy int
    sounddevice index, or None for the ALSA default."""
    card = state.settings.get("audio_device_card")
    if card:
        return str(card)
    idx = state.settings.get("audio_device_index")
    return idx if idx not in (None, "", "null") else None


def _capture_device_index(selection=None) -> int | None:
    """Resolve the capture selection to a live sounddevice input index.

    Accepts a card id string, a legacy int index, or None. Returns an int index
    to hand to sd.InputStream, or None to let PortAudio use its default input.
    """
    if selection is None:
        selection = _capture_selection()
    if selection is None:
        return None
    # Legacy: an integer sounddevice index was stored.
    if isinstance(selection, int) or (isinstance(selection, str) and selection.isdigit()):
        return int(selection)
    # Preferred: a stable card id. Find the current sounddevice input device
    # that belongs to that card (its name carries "(hw:<index>,<dev>)").
    cidx = _card_index_for_id(str(selection))
    if cidx is None:
        return None
    needle = f"hw:{cidx},"
    try:
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and needle in d["name"]:
                return i
    except Exception:
        return None
    return None


def _list_capture_devices() -> list[dict]:
    """Capture-capable ALSA cards, addressed by stable card id, for the UI."""
    custom_names = state.settings.get("device_names", {})
    out = []
    for idx, cid, name in _read_sound_cards():
        if not _card_has(idx, "c"):
            continue
        out.append({
            "card_id": cid,
            "name": custom_names.get(f"in:{cid}") or name,
            "raw_name": name,
        })
    return out


def _capture_channels(device_index=None) -> int:
    """Return the number of input channels to use for a given device.
    Uses the lesser of CAPTURE_CHANNELS_MAX and the device's actual max."""
    try:
        info = sd.query_devices(device_index, kind='input')
        return min(CAPTURE_CHANNELS_MAX, int(info['max_input_channels']))
    except Exception:
        return 2  # safe stereo fallback


# ── Local output devices (playback) ───────────────────────────────────────────

def _get_local_outputs():
    """List local ALSA playback devices, one per playback-capable card.

    Each device is addressed by its stable card id (id "local:<card_id>", ALSA
    device "plughw:CARD=<card_id>,DEV=0"), so selection and routing survive a
    card reorder without pointing at the wrong hardware.
    """
    custom_names = state.settings.get("device_names", {})
    hidden = set(state.settings.get("hidden_devices", []))
    outputs = []
    try:
        for idx, cid, name in _read_sound_cards():
            if not _card_has(idx, "p"):
                continue
            dev_id = f"local:{cid}"
            outputs.append({
                "id": dev_id,
                "name": name,
                "custom_name": custom_names.get(dev_id),
                "address": "local",
                "hidden": dev_id in hidden,
                "needs_pairing": False,
                "paired": True,
                "type": "local",
                "card_id": cid,
                "alsa_device": f"plughw:CARD={cid},DEV=0",
            })
    except Exception as e:
        print(f"[local-out] Error listing outputs: {e}")
    return outputs


def _get_bluetooth_devices():
    """Return cached paired Bluetooth devices for the device list."""
    return [d for d in state.available_bt_devices if d.get("paired")]
