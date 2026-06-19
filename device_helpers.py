#!/usr/bin/env python3
"""Vinyl AirPlay: device enumeration helpers.

Local ALSA output discovery and the cached-Bluetooth-devices accessor, shared by
the device routes, stream coordinator, and player engine. Shares AppState via app_state.
"""

import sounddevice as sd

from app_state import state

CAPTURE_CHANNELS_MAX = 2  # stereo capture: HiFiBerry DAC2 ADC Pro returns silence
                          # if opened with >2 channels (empty TDM slots). App only
                          # ever processes L+R anyway, so 2 is correct for any device.


def _capture_channels(device_index=None) -> int:
    """Return the number of input channels to use for a given device.
    Uses the lesser of CAPTURE_CHANNELS_MAX and the devices actual max."""
    try:
        info = sd.query_devices(device_index, kind='input')
        return min(CAPTURE_CHANNELS_MAX, int(info['max_input_channels']))
    except Exception:
        return 2  # safe stereo fallback


def _get_local_outputs():
    """List local audio output devices (ALSA software devices that actually work)."""
    custom_names = state.settings.get("device_names", {})
    hidden = set(state.settings.get("hidden_devices", []))
    outputs = []
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_output_channels"] < 2:
                continue
            name = d["name"]
            dev_id = f"local:{i}"
            n = name.lower()
            # Determine ALSA device string for aplay
            alsa_device = None
            if n.startswith(('front', 'default', 'sysdefault', 'touchscreen')):
                # Named ALSA device: use the short name (before comma)
                alsa_device = name.split(",")[0].strip()
            if not alsa_device:
                continue
            outputs.append({
                "id": dev_id,
                "name": name,
                "custom_name": custom_names.get(dev_id),
                "address": "local",
                "hidden": dev_id in hidden,
                "needs_pairing": False,
                "paired": True,
                "type": "local",
                "hw_index": i,
                "alsa_device": alsa_device,
            })
    except Exception as e:
        print(f"[local-out] Error listing outputs: {e}")
    # Prefer named devices (touchscreen) over generic ones (default, sysdefault)
    if len(outputs) > 1:
        named = [o for o in outputs if not o["name"].lower().startswith(("default", "sysdefault"))]
        if named:
            outputs = named
    return outputs


def _get_bluetooth_devices():
    """Return cached paired Bluetooth devices for the device list."""
    return [d for d in state.available_bt_devices if d.get("paired")]
