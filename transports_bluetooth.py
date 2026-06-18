#!/usr/bin/env python3
"""Vinyl AirPlay: Bluetooth device discovery/pairing/connection via bluetoothctl.

Extracted from main.py. The AppState is injected at construction so this module
does not import main (avoids a circular import); it only reads settings and
writes the discovered-device cache through that handle.
"""

import asyncio
import subprocess
from typing import ClassVar


class BluetoothManager:
    """Manage Bluetooth device discovery, pairing, and connection via bluetoothctl."""

    def __init__(self, state):
        self._scanning = False
        self._state = state

    @staticmethod
    def _run_ctl(*args, timeout=10) -> str:
        """Run a bluetoothctl command and return stdout."""
        try:
            result = subprocess.run(
                ['bluetoothctl', *list(args)],
                capture_output=True, text=True, timeout=timeout
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            print(f"[bluetooth] bluetoothctl error: {e}")
            return ""

    @staticmethod
    def _parse_device_line(line: str) -> tuple[str, str]:
        """Parse 'Device XX:XX:XX:XX:XX:XX Name' → (address, name)."""
        parts = line.strip().split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            return parts[1], parts[2]
        elif len(parts) == 2 and parts[0] == "Device":
            return parts[1], parts[1]  # no name, use address
        return "", ""

    # A2DP-related Bluetooth UUIDs that indicate audio capability
    AUDIO_UUIDS: ClassVar[set[str]] = {
        "0000110a",  # Audio Source
        "0000110b",  # Audio Sink
        "0000110c",  # A/V Remote Control Target
        "0000110d",  # Advanced Audio Distribution
        "0000110e",  # A/V Remote Control
    }
    AUDIO_ICONS: ClassVar[set[str]] = {"audio-card", "audio-headphones", "audio-headset", "audio-speakers"}

    @staticmethod
    def _parse_info(address: str) -> dict:
        """Get detailed info about a device from 'bluetoothctl info'."""
        output = BluetoothManager._run_ctl("info", address)
        if not output or "Missing device address" in output:
            return None  # ephemeral BLE device, no info available
        info = {"address": address, "name": address, "paired": False,
                "trusted": False, "connected": False, "icon": "", "uuids": []}
        for line in output.splitlines():
            line = line.strip()
            if line.startswith(('Name:', 'Alias:')):
                info["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Paired:"):
                info["paired"] = "yes" in line.lower()
            elif line.startswith("Trusted:"):
                info["trusted"] = "yes" in line.lower()
            elif line.startswith("Connected:"):
                info["connected"] = "yes" in line.lower()
            elif line.startswith("Icon:"):
                info["icon"] = line.split(":", 1)[1].strip()
            elif (
                line.startswith("UUID:") and "(" in line and ")" in line
            ):
                # Extract UUID hex from parenthesized value
                uuid = line.split("(")[1].split(")")[0].strip()
                info["uuids"].append(uuid)
        return info

    async def scan(self, timeout=12) -> list[dict]:
        """Discover nearby Bluetooth devices. Returns list of device dicts."""
        loop = asyncio.get_event_loop()

        # Enable the adapter and make it discoverable
        await loop.run_in_executor(None, lambda: self._run_ctl("power", "on"))

        # Start scanning in background
        await loop.run_in_executor(
            None, lambda: self._run_ctl("--timeout", str(timeout), "scan", "on",
                                        timeout=timeout + 5)
        )

        # Get all known devices (includes paired + newly discovered)
        output = await loop.run_in_executor(None, lambda: self._run_ctl("devices"))
        devices = []
        custom_names = self._state.settings.get("device_names", {})
        hidden = set(self._state.settings.get("hidden_devices", []))

        for line in output.splitlines():
            address, name = self._parse_device_line(line)
            if not address:
                continue

            info = await loop.run_in_executor(
                None, lambda addr=address: self._parse_info(addr)
            )

            # Skip ephemeral BLE devices with no info
            if info is None:
                continue

            # Only include devices that look like audio devices:
            # must have an audio icon OR at least one A2DP-related UUID
            icon = info.get("icon", "")
            uuids = {u[:8] for u in info.get("uuids", [])}
            has_audio_icon = icon in self.AUDIO_ICONS
            has_audio_uuid = bool(uuids & self.AUDIO_UUIDS)
            if not has_audio_icon and not has_audio_uuid:
                continue

            dev_id = f"bt:{address}"
            devices.append({
                "id": dev_id,
                "name": info.get("name", name),
                "custom_name": custom_names.get(dev_id),
                "address": address,
                "paired": info.get("paired", False),
                "trusted": info.get("trusted", False),
                "connected": info.get("connected", False),
                "hidden": dev_id in hidden,
                "needs_pairing": not info.get("paired", False),
                "type": "bluetooth",
            })

        self._state.available_bt_devices = devices
        return devices

    async def pair(self, address: str) -> dict:
        """Pair and trust a Bluetooth device."""
        loop = asyncio.get_event_loop()

        # Pair
        output = await loop.run_in_executor(
            None, lambda: self._run_ctl("pair", address, timeout=30)
        )
        if "Failed" in output and "AlreadyExists" not in output:
            return {"ok": False, "error": f"Pairing failed: {output}"}

        # Trust (auto-reconnect)
        await loop.run_in_executor(None, lambda: self._run_ctl("trust", address))

        return {"ok": True, "message": "Paired and trusted"}

    async def connect(self, address: str) -> dict:
        """Connect to a paired Bluetooth device."""
        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(
            None, lambda: self._run_ctl("connect", address, timeout=15)
        )
        if "Failed" in output:
            return {"ok": False, "error": f"Connection failed: {output}"}

        # Verify connection
        info = await loop.run_in_executor(
            None, lambda: self._parse_info(address)
        )
        if info and info.get("connected"):
            return {"ok": True, "message": f"Connected to {info.get('name', address)}"}
        return {"ok": False, "error": "Connection attempt finished but device not connected"}

    async def disconnect(self, address: str) -> dict:
        """Disconnect from a Bluetooth device."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._run_ctl("disconnect", address)
        )
        return {"ok": True, "message": "Disconnected"}

    async def remove(self, address: str) -> dict:
        """Unpair and remove a Bluetooth device."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._run_ctl("remove", address)
        )
        return {"ok": True, "message": "Device removed"}

    async def get_paired_devices(self) -> list[dict]:
        """Get list of paired Bluetooth devices with current status."""
        loop = asyncio.get_event_loop()
        output = await loop.run_in_executor(
            None, lambda: self._run_ctl("devices", "Paired")
        )
        custom_names = self._state.settings.get("device_names", {})
        hidden = set(self._state.settings.get("hidden_devices", []))
        devices = []

        for line in output.splitlines():
            address, name = self._parse_device_line(line)
            if not address:
                continue
            info = await loop.run_in_executor(
                None, lambda addr=address: self._parse_info(addr)
            )
            if info is None:
                continue
            dev_id = f"bt:{address}"
            devices.append({
                "id": dev_id,
                "name": info.get("name", name),
                "custom_name": custom_names.get(dev_id),
                "address": address,
                "paired": True,
                "trusted": info.get("trusted", False),
                "connected": info.get("connected", False),
                "hidden": dev_id in hidden,
                "needs_pairing": False,
                "type": "bluetooth",
            })

        return devices

    @staticmethod
    def get_bt_codec_info() -> dict:
        """Get active Bluetooth codec info from bluealsa-cli."""
        try:
            pcms = subprocess.run(
                ["bluealsa-cli", "list-pcms"],
                capture_output=True, text=True, timeout=3
            ).stdout.strip()
            if not pcms:
                return {"codec": None, "pcms": []}
            result = {"pcms": [], "codec": None}
            for pcm_path in pcms.splitlines():
                pcm_path = pcm_path.strip()
                if not pcm_path:
                    continue
                try:
                    info = subprocess.run(
                        ["bluealsa-cli", "info", pcm_path],
                        capture_output=True, text=True, timeout=3
                    ).stdout
                    codec = None
                    for line in info.splitlines():
                        if "Codec" in line:
                            codec = line.split(":", 1)[-1].strip()
                            break
                    result["pcms"].append({"path": pcm_path, "codec": codec})
                    if codec and not result["codec"]:
                        result["codec"] = codec
                except Exception:
                    pass
            return result
        except Exception as e:
            print(f"[bluetooth] Failed to get codec info: {e}")
            return {"codec": None, "pcms": []}
