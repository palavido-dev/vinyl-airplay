#!/usr/bin/env python3
"""Vinyl AirPlay: Bluetooth device routes (scan/pair/connect/disconnect/remove/codec).

Thin HTTP layer over BluetoothManager; all device logic lives in
transports_bluetooth. Shares the global AppState via app_state.
"""

import asyncio

from fastapi import APIRouter

from app_state import state
from config import save_settings
from transports_bluetooth import BluetoothManager

router = APIRouter()


@router.get("/api/bluetooth/scan")
async def bluetooth_scan():
    """Scan for nearby Bluetooth audio devices."""
    devices = await state.bluetooth_manager.scan(timeout=12)
    return {"ok": True, "devices": devices}


@router.post("/api/bluetooth/{device_id}/pair")
async def bluetooth_pair(device_id: str):
    """Pair and trust a Bluetooth device."""
    address = device_id.replace("bt:", "", 1)
    result = await state.bluetooth_manager.pair(address)
    if result.get("ok"):
        # Refresh paired devices list
        state.available_bt_devices = await state.bluetooth_manager.get_paired_devices()
    return result


@router.post("/api/bluetooth/{device_id}/connect")
async def bluetooth_connect(device_id: str):
    """Connect to a paired Bluetooth device."""
    address = device_id.replace("bt:", "", 1)
    result = await state.bluetooth_manager.connect(address)
    if result.get("ok"):
        state.available_bt_devices = await state.bluetooth_manager.get_paired_devices()
    return result


@router.post("/api/bluetooth/{device_id}/disconnect")
async def bluetooth_disconnect(device_id: str):
    """Disconnect from a Bluetooth device."""
    address = device_id.replace("bt:", "", 1)
    result = await state.bluetooth_manager.disconnect(address)
    state.available_bt_devices = await state.bluetooth_manager.get_paired_devices()
    return result


@router.post("/api/bluetooth/{device_id}/remove")
async def bluetooth_remove(device_id: str):
    """Unpair and remove a Bluetooth device."""
    address = device_id.replace("bt:", "", 1)
    result = await state.bluetooth_manager.remove(address)
    # Remove from cached list
    state.available_bt_devices = [d for d in state.available_bt_devices
                                  if d["address"] != address]
    # Clean up from hidden/names settings
    state.settings.get("hidden_devices", [])
    if device_id in state.settings.get("hidden_devices", []):
        state.settings["hidden_devices"].remove(device_id)
        save_settings(state.settings)
    state.settings.get("device_names", {}).pop(device_id, None)
    return result


@router.get("/api/bluetooth/codec")
async def bluetooth_codec():
    """Get active Bluetooth codec info."""
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, BluetoothManager.get_bt_codec_info)
    return info
