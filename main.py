#!/usr/bin/env python3
"""
Vinyl AirPlay Streamer: Web-controlled backend
16-bit / 44.1kHz lossless PCM with live bass/treble EQ + record recognition
"""

import asyncio
import json
import os
import random
import shutil
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

import pyatv
import sounddevice as sd
import uvicorn
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pyatv.storage.file_storage import FileStorage

import audio_gain
import catalog as cat
import player as plr
import recorder as rec
from app_state import broadcast, spawn_bg, state, ws_heartbeat
from audio_mp3 import LiveMP3Broadcaster
from audio_streams import (
    BrowserMP3Stream,
    _browser_streams,
)
from config import TEMPLATES, save_settings
from device_helpers import _capture_channels, _get_bluetooth_devices, _get_local_outputs
from learn_engine import LearnSession
from player_engine import _build_side_entry, _run_playback, _run_playback_queue, _stop_playback
from recognition import _art_url
from recording_engine import (
    _encode_and_save_album_side,
    _start_stall_watchdog,
    _stop_stall_watchdog,
)
from routes_bluetooth import router as bluetooth_router
from routes_catalog import router as catalog_router
from routes_catalog_stats import router as catalog_stats_router
from routes_eq import router as eq_router
from routes_export import router as export_router
from routes_settings import router as settings_router
from routes_system import router as system_router
from streaming import (
    _auto_stream_watcher,
    _ensure_audio_active,
    _restart_auto_stream_watcher,
    _start_listen_mode,
    _stop_listen_mode,
    run_stream,
)
from transports_bluetooth import BluetoothManager

# ── Audio Config ──────────────────────────────────────────────────────────────

SAMPLE_RATE      = 44100
CHANNELS         = 2    # processing/output channels (stereo)

BITS          = 16
BLOCK_SIZE    = 8192   # larger blocks = fewer callbacks = less overflow on Pi.
                       # The live MP3 broadcaster accumulates leftover bytes
                       # across put() calls, so this can be any size; the
                       # encoder always gets aligned MP3 frames downstream.
INPUT_LATENCY = 0.5    # seconds: large ALSA buffer absorbs USB timing jitter
                       # (Scarlett 2i2 4th Gen triggers retire_capture_urb warnings
                       # on the Pi's USB host controller; a bigger buffer keeps the
                       # stream alive through those hiccups)
READ_SIZE     = 8192
MAX_CHUNKS    = 500


# ── Bluetooth Manager ─────────────────────────────────────────────────────────

# BluetoothManager lives in transports_bluetooth.py; built with the shared state.
state.bluetooth_manager = BluetoothManager(state)


# ── App Lifespan ──────────────────────────────────────────────────────────────

_lifespan_initialized = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _lifespan_initialized
    if _lifespan_initialized:
        # Second uvicorn instance (HTTPS) shares the same app object.
        # Skip audio/DB init so we don't double-open the capture device.
        yield
        return
    _lifespan_initialized = True
    cat.init_db(state.settings)
    devices = sd.query_devices()
    state.audio_devices = [
        {"index": i, "name": d["name"], "max_input_channels": d["max_input_channels"]}
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]
    audio_idx = state.settings.get("audio_device_index")
    ch = _capture_channels(audio_idx)
    print(f"[audio] Input device={audio_idx}, capture_channels={ch}")
    # Auto-set analog capture gain on known ADCs (e.g. HiFiBerry) so a quiet
    # line/phono input still triggers auto-record (issue #40).
    try:
        if state.settings.get("adc_auto_gain_enabled", True):
            _gidx, _gprof = audio_gain.detect_adc()
            if _gidx is not None:
                _gain = state.settings.get("adc_gain_db", audio_gain.DEFAULT_GAIN_DB)
                _ok, _msg = audio_gain.apply_gain(_gain, _gidx, _gprof)
                print(f"[adc-gain] {_msg}" if _ok else f"[adc-gain] Failed: {_msg}")
    except Exception as e:
        print(f"[adc-gain] Error: {e}")
    if state.settings.get("auto_stream_enabled"):
        state.auto_stream_task = asyncio.create_task(_auto_stream_watcher())
        print("[auto-stream] Watcher started on boot")
    state.live_mp3.configure(
        state.settings.get("http_stream_enabled", False),
        state.settings.get("http_stream_bitrate_kbps", 256),
    )
    loop = asyncio.get_event_loop()
    state.loop = loop
    state.heartbeat_task = asyncio.create_task(ws_heartbeat())

    # Load saved AirPlay credentials so streaming can authenticate without
    # asking the user to re-pair every time. pyatv 0.17 only attaches creds
    # to conf objects when a storage is explicitly passed to scan().
    try:
        for _creds_path in ("/home/listen/.pyatv.conf", "/root/.pyatv.conf"):
            if os.path.exists(_creds_path):
                state.atv_storage = FileStorage(_creds_path, loop)
                await state.atv_storage.load()
                print(f"[pyatv] Loaded credential storage from {_creds_path}")
                break
        else:
            # Fall back to default location, creating an empty storage if needed
            state.atv_storage = FileStorage.default_storage(loop)
            await state.atv_storage.load()
    except Exception as e:
        print(f"[pyatv] Could not load credential storage: {e}")
        state.atv_storage = None

    yield
    if state.stop_event:
        state.stop_event.set()
    if state.listen_stop_event:
        state.listen_stop_event.set()
    if state.player:
        state.player.stop()
        state.player = None
    if state.player_task:
        state.player_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.player_task
    if state.auto_stream_task and not state.auto_stream_task.done():
        state.auto_stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.auto_stream_task
    if state.heartbeat_task:
        state.heartbeat_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.heartbeat_task
    if state.stream_task:
        state.stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.stream_task
    state.live_mp3.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(bluetooth_router)
app.include_router(catalog_router)
app.include_router(catalog_stats_router)
app.include_router(eq_router)
app.include_router(export_router)
app.include_router(settings_router)
app.include_router(system_router)


# ── Artwork Serving ───────────────────────────────────────────────────────────

@app.get("/artwork/{filename}")
async def serve_artwork(filename: str):
    path = cat.ARTWORK_DIR / filename
    if path.exists():
        return FileResponse(str(path))
    return HTMLResponse("", 404)


# ── Core Routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    resp = TEMPLATES.TemplateResponse(request, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/manifest.json")
async def manifest():
    """Serve PWA manifest."""
    manifest_path = Path("templates/manifest.json")
    if manifest_path.exists():
        return FileResponse(manifest_path, media_type="application/manifest+json")
    return {"error": "Manifest not found"}


@app.get("/service-worker.js")
async def service_worker():
    """Serve service worker for PWA offline support."""
    sw_path = Path("templates/service-worker.js")
    if sw_path.exists():
        resp = FileResponse(sw_path, media_type="application/javascript")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    return {"error": "Service worker not found"}


@app.get("/api/scan")
async def scan_devices():
    # Scan WITHOUT a protocol filter so every service (RAOP, AirPlay,
    # Companion) is populated on each conf. Pass storage= so saved
    # credentials are attached to the conf services. Together these
    # let us compute needs_pairing correctly per service.
    if state.atv_storage is not None:
        await state.atv_storage.load()
    found = await pyatv.scan(
        asyncio.get_event_loop(),
        timeout=7,
        storage=state.atv_storage,
    )
    hidden = set(state.settings.get("hidden_devices", []))
    custom_names = state.settings.get("device_names", {})
    state.available_devices = []
    # Only the audio protocols matter for our use case. Companion is for
    # remote control and is irrelevant to streaming audio, so don't gate
    # "paired" on it even when it reports Mandatory.
    AUDIO_PROTOS = (pyatv.Protocol.RAOP, pyatv.Protocol.AirPlay)
    for d in found:
        # A device needs pairing if RAOP (what we stream with) has
        # Mandatory pairing and we don't already have credentials.
        raop = d.get_service(pyatv.Protocol.RAOP)
        needs_pair = bool(
            raop and str(getattr(raop, "pairing", "")).endswith("Mandatory")
            and not raop.credentials
        )
        # "paired" is true if every Mandatory *audio* protocol on this
        # device has credentials. tvOS often requires both RAOP and
        # AirPlay; HomePods often require neither.
        audio_paired = True
        for proto in AUDIO_PROTOS:
            svc = d.get_service(proto)
            if (
                svc
                and str(getattr(svc, "pairing", "")).endswith("Mandatory")
                and not svc.credentials
            ):
                audio_paired = False
                break
        state.available_devices.append({
            "id":       d.identifier,
            "name":     d.name,
            "custom_name": custom_names.get(d.identifier),
            "address":  str(d.address),
            "hidden":   d.identifier in hidden,
            "needs_pairing": needs_pair,
            "paired":   audio_paired,
        })
    return {"devices": state.available_devices + _get_local_outputs()}


@app.post("/api/devices/{device_id}/pair/start")
async def pair_start(device_id: str, body: dict | None = None):
    """
    Begin pairing with a device. Returns whether a PIN is needed.
    If the device shows a PIN on screen, the client should prompt the user to
    enter it and call /pair/pin. If no PIN is needed, pairing completes immediately.
    Pairs RAOP protocol first (required for audio), then AirPlay.
    """
    if body is None:
        body = {}
    protocol_name = body.get("protocol", "raop")
    proto_map = {
        "raop":    pyatv.Protocol.RAOP,
        "airplay": pyatv.Protocol.AirPlay,
    }
    protocol = proto_map.get(protocol_name, pyatv.Protocol.RAOP)

    loop = asyncio.get_event_loop()
    # No protocol filter: we need RAOP / AirPlay / Companion service
    # objects on the conf so pyatv.pair can pick the right one.
    if state.atv_storage is not None:
        await state.atv_storage.load()
    found = await pyatv.scan(
        loop, timeout=7, identifier=device_id, storage=state.atv_storage,
    )
    if not found:
        return {"ok": False, "error": "Device not found on network"}
    conf = found[0]

    try:
        pairing = await pyatv.pair(conf, protocol, loop, storage=state.atv_storage)
        await pairing.begin()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    state.pairing_sessions[device_id] = {
        "pairing":  pairing,
        "conf":     conf,
        "protocol": protocol_name,
    }

    return {
        "ok":               True,
        "device_provides_pin": pairing.device_provides_pin,
        "needs_pin":        True,  # always wait for explicit /pin call to finish
    }


@app.post("/api/devices/{device_id}/pair/pin")
async def pair_pin(device_id: str, body: dict | None = None):
    """Submit PIN and finish pairing. Then save credentials to .pyatv.conf."""
    if body is None:
        body = {}
    session = state.pairing_sessions.get(device_id)
    if not session:
        return {"ok": False, "error": "No active pairing session: call /pair/start first"}

    pin = body.get("pin", "")
    pairing = session["pairing"]
    conf    = session["conf"]

    try:
        if pin:
            pairing.pin(int(pin))
        await pairing.finish()
    except Exception as e:
        state.pairing_sessions.pop(device_id, None)
        return {"ok": False, "error": f"Pairing failed: {e}"}

    if not pairing.has_paired:
        state.pairing_sessions.pop(device_id, None)
        return {"ok": False, "error": "Pairing did not succeed: wrong PIN?"}

    state.pairing_sessions.pop(device_id, None)

    # pyatv.pair was given state.atv_storage, so the newly issued
    # credentials are already attached to the in-memory storage. All
    # that's left is to flush them to disk.
    if state.atv_storage is not None:
        try:
            await state.atv_storage.save()
            print("[pair] Credentials saved")
        except Exception as e:
            print(f"[pair] Could not save credential storage: {e}")

    protocol_name = session["protocol"]
    # Check if more protocols need pairing
    conf.get_service(pyatv.Protocol.RAOP)
    airplay = conf.get_service(pyatv.Protocol.AirPlay)
    remaining = []
    airplay_mandatory = (
        airplay
        and str(getattr(airplay, "pairing", "")).endswith("Mandatory")
        and not airplay.credentials
    )
    if protocol_name == "raop" and airplay_mandatory:
        remaining.append("airplay")

    return {
        "ok":       True,
        "paired":   True,
        "remaining_protocols": remaining,
        "message":  f"Paired successfully via {protocol_name.upper()}"
                    + (f": also pair: {', '.join(remaining).upper()}" if remaining else ""),
    }


@app.post("/api/devices/{device_id}/pair/cancel")
async def pair_cancel(device_id: str):
    """Cancel an in-progress pairing session."""
    session = state.pairing_sessions.pop(device_id, None)
    if session:
        with suppress(Exception):
            await session["pairing"].finish()
    return {"ok": True}


@app.post("/api/devices/{device_id}/hide")
async def toggle_device_hidden(device_id: str, body: dict | None = None):
    """Toggle a device's hidden state. Persists to settings.json."""
    if body is None:
        body = {}
    hidden = set(state.settings.get("hidden_devices", []))
    hide = body.get("hidden", True)
    if hide:
        hidden.add(device_id)
    else:
        hidden.discard(device_id)
    state.settings["hidden_devices"] = list(hidden)
    save_settings(state.settings)
    # Update available_devices list in memory too
    for d in state.available_devices:
        if d["id"] == device_id:
            d["hidden"] = hide
    return {"ok": True, "hidden": hide}


@app.get("/api/devices")
async def get_cached_devices():
    """Return cached devices from last scan (instant). Use /api/scan to refresh."""
    custom_names = state.settings.get("device_names", {})
    for d in state.available_devices:
        d["custom_name"] = custom_names.get(d["id"])
    devices = state.available_devices + _get_local_outputs() + _get_bluetooth_devices()
    # Add browser device
    devices.insert(0, {
        "id": "browser",
        "type": "browser",
        "name": "This Device",
        "custom_name": custom_names.get("browser"),
        "paired": True,
        "hidden": "browser" in state.settings.get("hidden_devices", []),
    })
    return {"devices": devices}


@app.post("/api/devices/{device_id}/rename")
async def rename_device(device_id: str, body: dict | None = None):
    """Set or clear a custom display name for a device."""
    if body is None:
        body = {}
    custom_name = body.get("name", "").strip()
    if "device_names" not in state.settings:
        state.settings["device_names"] = {}
    if custom_name:
        state.settings["device_names"][device_id] = custom_name
    else:
        state.settings["device_names"].pop(device_id, None)
    save_settings(state.settings)
    for d in state.available_devices:
        if d["id"] == device_id:
            d["custom_name"] = custom_name or None
    return {"ok": True, "device_id": device_id, "custom_name": custom_name or None}


@app.get("/api/audio-devices")
async def audio_devices():
    return {"devices": state.audio_devices,
            "current_index": state.settings.get("audio_device_index")}


# ── Browser Stream Routes ─────────────────────────────────────────────────────

@app.get("/live.mp3")
async def stream_live_mp3():
    """Persistent MP3 URL for live turntable audio (silence when no input)."""
    if not state.settings.get("http_stream_enabled", False):
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "HTTP live streaming is disabled in Settings"},
        )

    client_id = state.live_mp3.register_client()
    if client_id is None:
        return JSONResponse(
            status_code=503,
            content={"ok": False,
                     "error": f"Listener limit reached ({state.live_mp3.MAX_CLIENTS} max)"},
        )
    await broadcast("live_listeners", {"count": state.live_mp3.listener_count()})

    async def generate():
        try:
            while True:
                chunk = state.live_mp3.get_chunk(client_id)
                if chunk is None:
                    break
                if chunk:
                    yield chunk
                else:
                    await asyncio.sleep(0.02)
        finally:
            state.live_mp3.unregister_client(client_id)
            with suppress(Exception):
                await broadcast("live_listeners", {"count": state.live_mp3.listener_count()})

    # Prevent seeking by disabling Range requests and setting headers
    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Accept-Ranges": "none",  # Prevent HTTP clients from seeking
            "X-Content-Duration": "live",  # Optional: mark as live
        },
    )

@app.post("/api/stream/create")
async def create_browser_stream():
    """Create a new browser audio stream and return its stream_id.

    Uses a per-session MP3 encoder so the browser can play it from an <audio>
    element (keeps playing when Safari is backgrounded / locked on iOS, #54).
    """
    bitrate = state.settings.get("http_stream_bitrate_kbps", 256)
    stream = BrowserMP3Stream(bitrate_kbps=bitrate)
    _browser_streams[stream.stream_id] = stream
    print(f"[browser-stream] Created MP3 stream {stream.stream_id}")
    return {"ok": True, "stream_id": stream.stream_id}


@app.get("/api/stream/{stream_id}")
async def stream_audio(stream_id: str):
    """Stream the session's live MP3 to the browser <audio> element."""
    stream = _browser_streams.get(stream_id)
    if not stream:
        return JSONResponse({"error": "Stream not found"}, status_code=404)

    async def generate():
        chunks_sent = 0
        empty_polls = 0
        max_empty = 1000  # ~10s of no data before giving up
        try:
            while True:
                chunk = stream.get_chunk()
                if chunk is None:
                    break  # stopped and drained
                if chunk:
                    empty_polls = 0
                    chunks_sent += 1
                    yield chunk
                else:
                    empty_polls += 1
                    if empty_polls > max_empty:
                        print(f"[browser-stream] Timeout waiting for data on {stream_id}")
                        break
                    await asyncio.sleep(0.01)
        except GeneratorExit:
            print(f"[browser-stream] Client disconnected from {stream_id} after {chunks_sent} chunks")
        except Exception as e:
            print(f"[browser-stream] Error in stream {stream_id}: {e}")
        finally:
            with suppress(Exception):
                stream.stop()
            _browser_streams.pop(stream_id, None)
            print(f"[browser-stream] Closed stream {stream_id}")

    return StreamingResponse(
        generate(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache, no-store", "Accept-Ranges": "none"},
    )


@app.get("/api/status")
async def get_status():
    bass, treble, volume = state.eq.values
    storage_dir = cat.get_audio_storage_dir(state.settings)
    try:
        usage = shutil.disk_usage(str(storage_dir))
        storage_info = {
            "path": str(storage_dir),
            "free_gb": round(usage.free / (1024**3), 1),
            "total_gb": round(usage.total / (1024**3), 1),
        }
    except Exception:
        storage_info = {"path": str(storage_dir), "free_gb": 0, "total_gb": 0}
    ar = state.album_recorder
    rec_info = None
    if ar and ar.is_active:
        rec_info = {"album_id": ar.album_id, "side": ar.side}
    return {
        "streaming":        state.is_streaming,
        "active_devices":   state.active_devices,
        "settings":         state.settings,
        "audio_devices":    state.audio_devices,
        "eq":               {"bass": bass, "treble": treble, "volume": volume,
                             "bands": state.eq.band_values,
                             "preset": state.settings.get("eq_preset", "")},
        "now_playing":      state.now_playing,
        "player":           state.player.get_status() if state.player else {"state": "stopped"},
        "storage":          storage_info,
        "album_recording":  rec_info,
        "input_level":      state.rec_level,
        "live_listeners":   state.live_mp3.listener_count(),
    }


@app.post("/api/start")
async def start_stream(body: dict):
    if state.is_streaming:
        return {"ok": False, "error": "Already streaming"}
    targets   = body.get("devices", [])
    volume    = body.get("volume",   state.settings.get("volume", 80))
    audio_idx = body.get("audio_device_index", state.settings.get("audio_device_index"))
    state.settings.update({"saved_devices": targets, "volume": volume,
                            "audio_device_index": audio_idx})
    save_settings(state.settings)
    state.stream_task = asyncio.create_task(run_stream(targets, audio_idx, volume))
    return {"ok": True}


@app.post("/api/stop")
async def stop_stream():
    if state.stop_event:
        state.stop_event.set()
    if state.stream_task:
        state.stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.stream_task
        state.stream_task = None
    # Also stop listen mode, unless an album recording still needs the capture
    if state.listen_task and not (state.album_recorder and state.album_recorder.is_active):
        _stop_listen_mode()
    # Suppress auto-stream for 60s after manual stop so it doesn't immediately restart
    state.manual_stop_until = time.monotonic() + 60.0
    # Force-reset state in case scan failed before setting is_streaming=False
    if state.is_streaming:
        state.is_streaming    = False
        state.active_devices  = []
        state.stop_event      = None
        state.airplay_metadata = None
        await broadcast("status",     {"streaming": False, "message": "Stopped"})
        await broadcast("now_playing", {"track_title": None})
    return {"ok": True}


@app.post("/api/settings")
async def update_settings(body: dict):
    if "auto_stream_enabled" in body:
        state.settings["auto_stream_enabled"] = bool(body["auto_stream_enabled"])
        save_settings(state.settings)
        spawn_bg(_restart_auto_stream_watcher())
    if body.get("auto_stream_device"):
        state.settings["auto_stream_device"] = body["auto_stream_device"]
        save_settings(state.settings)
    if "discogs_token" in body:
        state.settings["discogs_token"] = str(body["discogs_token"])
        save_settings(state.settings)
    if "discogs_username" in body:
        state.settings["discogs_username"] = str(body["discogs_username"]).strip()
        save_settings(state.settings)
    if "crossfade_secs" in body:
        cf = max(0, min(2.0, float(body["crossfade_secs"])))
        state.settings["crossfade_secs"] = cf
        if state.player:
            state.player.set_crossfade(cf)
        save_settings(state.settings)
    if "http_stream_enabled" in body:
        state.settings["http_stream_enabled"] = bool(body["http_stream_enabled"])
    if "http_stream_bitrate_kbps" in body:
        state.settings["http_stream_bitrate_kbps"] = LiveMP3Broadcaster.sanitize_bitrate(
            body["http_stream_bitrate_kbps"]
        )
    if "app_name" in body:
        state.settings["app_name"] = str(body["app_name"])[:40]
    if "theme" in body:
        state.settings["theme"] = str(body["theme"])
    if "audio_device_index" in body:
        v = body["audio_device_index"]
        state.settings["audio_device_index"] = None if v in (None, "", "null") else int(v)
    if "rec_play_audio" in body:
        state.settings["rec_play_audio"] = bool(body["rec_play_audio"])
    if "eq_auto_load" in body:
        state.settings["eq_auto_load"] = bool(body["eq_auto_load"])
    if "audio_detect_threshold" in body:
        with suppress(ValueError, TypeError):
            state.settings["audio_detect_threshold"] = max(0.001, min(0.05, float(body["audio_detect_threshold"])))
    if "adc_auto_gain_enabled" in body:
        state.settings["adc_auto_gain_enabled"] = bool(body["adc_auto_gain_enabled"])
    if "adc_gain_db" in body:
        with suppress(ValueError, TypeError):
            state.settings["adc_gain_db"] = float(body["adc_gain_db"])
        # Apply immediately so the user hears the change without a restart
        if state.settings.get("adc_auto_gain_enabled", True):
            _ok, _msg = audio_gain.apply_gain(state.settings["adc_gain_db"])
            print(f"[adc-gain] {_msg}" if _ok else f"[adc-gain] Failed: {_msg}")
    state.live_mp3.configure(
        state.settings.get("http_stream_enabled", False),
        state.settings.get("http_stream_bitrate_kbps", 256),
    )
    save_settings(state.settings)
    return {"ok": True}


@app.get("/api/audio/gain")
async def get_audio_gain():
    """ADC detection + current analog capture gain, for the Settings UI."""
    st = audio_gain.status()
    st["auto_gain_enabled"] = state.settings.get("adc_auto_gain_enabled", True)
    st["configured_db"] = state.settings.get("adc_gain_db", audio_gain.DEFAULT_GAIN_DB)
    return st


# ── Catalog Routes ────────────────────────────────────────────────────────────


@app.post("/api/catalog/{album_id}/learn")
async def learn_album(album_id: int):
    """
    Fingerprint the currently buffered audio and associate it with this album.
    Call while the record is playing. Works even when not streaming to AirPlay :
    as long as the service is running and audio is coming in.
    """
    if not state.is_streaming:
        return {"ok": False, "error": "Not currently streaming: start streaming first, then try again"}

    if not state.fp_buffer.ready():
        return {"ok": False, "error": "Not enough audio buffered yet: wait 30 seconds and try again"}

    wav = state.fp_buffer.get_wav()
    if not wav:
        return {"ok": False, "error": "Audio buffer empty"}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: cat.fingerprint_wav(wav))
    if not result:
        return {"ok": False, "error": "Fingerprinting failed: is audio coming through the Scarlett?"}

    raw_ints, _compressed, duration = result
    ok = await loop.run_in_executor(
        None, lambda: cat.save_fingerprint_for_album(album_id, raw_ints, duration)
    )
    if not ok:
        return {"ok": False, "error": "Failed to save fingerprint: does this album have any tracks?"}

    # Enable auto-learn so subsequent tracks get learned automatically
    if state.recogniser:
        state.recogniser.set_auto_learn_album(album_id)

    # Broadcast now playing immediately
    db_track = cat.get_album_tracks(album_id)
    albums   = cat.get_all_albums()
    album    = next((a for a in albums if a["id"] == album_id), None)
    if album and db_track:
        track = db_track[0]
        now   = {
            "track_id":     track["id"],
            "track_title":  track["title"],
            "track_artist": track.get("artist") or album["artist"],
            "album_id":     album_id,
            "album_title":  album["title"],
            "album_artist": album["artist"],
            "year":         album.get("year"),
            "artwork_path":      album.get("artwork_path"),
            "user_artwork_path": album.get("user_artwork_path"),
        }
        state.now_playing = now
        cat.log_play(track["id"], album_id)
        await broadcast("now_playing", {**now, "artwork_url": _art_url(now)})

    # Count remaining unlearned tracks
    all_tracks = cat.get_album_tracks(album_id)
    db = cat.get_db()
    unlearned = sum(
        1 for t in all_tracks
        if not db.execute("SELECT 1 FROM fingerprints WHERE track_id = ?", (t["id"],)).fetchone()
    )
    db.close()

    if unlearned == 0:
        msg = "All tracks learned! This album will be fully recognized on future plays."
    else:
        msg = f"Learned one track. {unlearned} track(s) still need learning: press again as each new track plays."

    return {"ok": True, "message": msg}

@app.delete("/api/catalog/{album_id}/fingerprints")
async def clear_album_fingerprints(album_id: int):
    """Clear all learned fingerprints for every track in an album."""
    deleted = cat.clear_album_fingerprints(album_id)
    return {"ok": True, "deleted": deleted,
            "message": f"Cleared {deleted} fingerprints: album is unlearned"}


@app.delete("/api/catalog/track/{track_id}/fingerprints")
async def clear_track_fingerprints(track_id: int):
    """Clear all learned fingerprints for a single track."""
    deleted = cat.clear_track_fingerprints(track_id)
    return {"ok": True, "deleted": deleted,
            "message": f"Cleared {deleted} fingerprints for this track"}


@app.post("/api/catalog/track/{track_id}/re-fingerprint")
async def re_fingerprint_track(track_id: int):
    """Re-fingerprint a single track from its side's recorded FLAC audio."""
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, cat.fingerprint_track_from_flac, track_id)
    if rows is None:
        return {"ok": False, "error": "Could not re-fingerprint. Check that the track "
                "has timestamps and a FLAC recording exists for its side."}
    return {"ok": True, "rows": rows,
            "message": f"Re-fingerprinted from FLAC: {rows} windows saved"}


@app.post("/api/catalog/{album_id}/re-fingerprint")
async def re_fingerprint_album(album_id: int, body: dict | None = None):
    """Re-fingerprint all tracks on an album from recorded FLAC audio.
    If body contains force=true, re-fingerprints even tracks that already
    have fingerprints. Otherwise only processes unlearned tracks."""
    if body is None:
        body = {}
    force = body.get("force", False)
    tracks = cat.get_album_tracks(album_id)
    loop = asyncio.get_event_loop()
    db = cat.get_db()
    try:
        fp_track_ids = {
            r["track_id"] for r in db.execute(
                "SELECT DISTINCT track_id FROM fingerprints "
                "WHERE track_id IN (SELECT id FROM tracks WHERE album_id = ?)",
                (album_id,)
            ).fetchall()
        }
    finally:
        db.close()

    done = 0
    skipped = 0
    for t in tracks:
        if t.get("start_secs") is None or t.get("end_secs") is None:
            skipped += 1
            continue
        if not force and t["id"] in fp_track_ids:
            skipped += 1
            continue
        rows = await loop.run_in_executor(None, cat.fingerprint_track_from_flac, t["id"])
        if rows is not None:
            done += 1
    return {"ok": True, "fingerprinted": done, "skipped": skipped,
            "message": f"Re-fingerprinted {done} track(s) from FLAC"}


@app.post("/api/catalog/{album_id}/reorder")
async def reorder_tracks(album_id: int, body: dict):
    """Save new track order. body: { track_ids: [id, id, ...] in desired order }"""
    track_ids = body.get("track_ids", [])
    if not track_ids:
        return {"ok": False, "error": "No track IDs provided"}
    ok = cat.reorder_album_tracks(track_ids)
    return {"ok": ok}


@app.post("/api/catalog/{album_id}/reassign-sides")
async def reassign_sides(album_id: int, body: dict):
    """Reassign tracks to sides and renumber. body: { tracks: [{id, side}, ...] }"""
    tracks = body.get("tracks", [])
    if not tracks:
        return {"ok": False, "error": "No track assignments provided"}
    ok = cat.reassign_tracks_to_sides(tracks)
    return {"ok": ok}


@app.delete("/api/catalog/{album_id}")
async def delete_album_route(album_id: int):
    # Use soft-delete for undo support
    success = cat.soft_delete_album(album_id)
    if not success:
        return JSONResponse({"ok": False, "error": "Album not found"}, status_code=404)
    return {"ok": True}


@app.post("/api/catalog/{album_id}/favorite")
async def toggle_album_favorite(album_id: int):
    new_state = cat.toggle_favorite(album_id)
    return {"ok": True, "favorite": new_state}


@app.get("/api/playlists")
async def get_playlists():
    """List all saved playlists."""
    return {"ok": True, "playlists": cat.get_playlists()}


@app.post("/api/playlists")
async def save_playlist(body: dict):
    """Save a playlist. body: { name: str, album_ids: list[int] }"""
    name = body.get("name", "").strip()
    entries = body.get("entries")  # side-level: [{a:int, s:str}, ...]
    album_ids = body.get("album_ids", [])
    if not name:
        return {"ok": False, "error": "Playlist name required"}
    pid = cat.save_playlist_entries(name, entries) if entries is not None else cat.save_playlist(name, album_ids)
    return {"ok": True, "id": pid}


@app.post("/api/playlists/{playlist_id}/add")
async def add_to_playlist(playlist_id: int, body: dict):
    """Add an album to a playlist. body: { album_id: int }"""
    album_id = body.get("album_id")
    if not album_id:
        return {"ok": False, "error": "album_id required"}
    ok = cat.add_album_to_playlist(playlist_id, int(album_id))
    return {"ok": ok}


@app.post("/api/playlists/{playlist_id}/reorder")
async def reorder_playlist(playlist_id: int, body: dict):
    """Move an album within a playlist. body: { from: int, to: int }"""
    from_idx = body.get("from")
    to_idx = body.get("to")
    if from_idx is None or to_idx is None:
        return {"ok": False, "error": "from and to indices required"}
    ok = cat.reorder_playlist(playlist_id, int(from_idx), int(to_idx))
    return {"ok": ok}


@app.post("/api/playlists/{playlist_id}/remove")
async def remove_from_playlist(playlist_id: int, body: dict):
    """Remove from playlist. body: { album_id: int } or { entry_idx: int }"""
    entry_idx = body.get("entry_idx")
    if entry_idx is not None:
        ok = cat.remove_playlist_entry(playlist_id, int(entry_idx))
        return {"ok": ok}
    album_id = body.get("album_id")
    if not album_id:
        return {"ok": False, "error": "album_id or entry_idx required"}
    ok = cat.remove_album_from_playlist(playlist_id, int(album_id))
    return {"ok": ok}


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int):
    """Delete a playlist."""
    cat.delete_playlist(playlist_id)
    return {"ok": True}


@app.put("/api/playlists/{playlist_id}/rename")
async def rename_playlist(playlist_id: int, body: dict):
    """Rename a playlist. body: { name: string }"""
    new_name = body.get("name", "").strip()
    if not new_name:
        return {"ok": False, "error": "Name cannot be empty"}
    cat.rename_playlist(playlist_id, new_name)
    return {"ok": True}


# -- Smart Playlists ----------------------------------------------------------

@app.get("/api/smart-playlists")
async def get_smart_playlists():
    playlists = cat.get_smart_playlists()
    return {"ok": True, "playlists": playlists}


@app.post("/api/smart-playlists")
async def create_smart_playlist(body: dict):
    name = body.get("name", "").strip()
    rules = body.get("rules", [])
    if not name:
        return {"ok": False, "error": "Name is required"}
    pid = cat.create_smart_playlist(name, rules)
    return {"ok": True, "id": pid}


@app.get("/api/smart-playlists/{playlist_id}/albums")
async def get_smart_playlist_albums(playlist_id: int):
    albums = cat.get_smart_playlist_albums(playlist_id)
    return {"ok": True, "albums": albums}


@app.post("/api/smart-playlists/{playlist_id}/play")
async def play_smart_playlist(playlist_id: int, body: dict):
    """Resolve a smart playlist and queue all matching albums for playback."""
    albums = cat.get_smart_playlist_albums(playlist_id)
    if not albums:
        return {"ok": False, "error": "No matching albums"}

    body.get("devices", [])
    if not state.player and not state.is_streaming:
        return {"ok": False, "error": "No output device active"}

    # Build queue from resolved albums (sides with audio)
    queue_items = []
    for album in albums:
        audio_sides = cat.get_album_audio(album["id"])
        for audio in audio_sides:
            queue_items.append({
                "album_id": album["id"],
                "side": audio.get("side", "A"),
                "title": album.get("title", ""),
                "artist": album.get("artist", ""),
            })

    if not queue_items:
        return {"ok": False, "error": "No recorded audio in matching albums"}

    if state.player:
        for item in queue_items:
            state.player.add_to_queue(item["album_id"], item["side"])

    return {"ok": True, "queued": len(queue_items)}


@app.put("/api/smart-playlists/{playlist_id}")
async def update_smart_playlist(playlist_id: int, body: dict):
    name = body.get("name")
    rules = body.get("rules")
    cat.update_smart_playlist(playlist_id, name=name, rules=rules)
    return {"ok": True}


@app.delete("/api/smart-playlists/{playlist_id}")
async def delete_smart_playlist(playlist_id: int):
    cat.delete_smart_playlist(playlist_id)
    return {"ok": True}


# ── Song Playlists ──────────────────────────────────────────────────────────

@app.get("/api/song-playlists")
async def get_song_playlists():
    return {"ok": True, "playlists": cat.get_song_playlists()}


@app.get("/api/song-playlists/{playlist_id}")
async def get_song_playlist(playlist_id: int):
    pl = cat.get_song_playlist(playlist_id)
    if not pl:
        return {"ok": False, "error": "Not found"}
    return {"ok": True, "playlist": pl}


@app.post("/api/song-playlists")
async def create_song_playlist(body: dict):
    name = body.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "Name required"}
    track_ids = body.get("track_ids", [])
    pid = cat.create_song_playlist(name, track_ids)
    return {"ok": True, "id": pid}


@app.put("/api/song-playlists/{playlist_id}")
async def update_song_playlist(playlist_id: int, body: dict):
    name = body.get("name")
    track_ids = body.get("track_ids")
    cat.update_song_playlist(playlist_id, name=name, track_ids=track_ids)
    return {"ok": True}


@app.post("/api/song-playlists/{playlist_id}/add")
async def add_to_song_playlist(playlist_id: int, body: dict):
    track_id = body.get("track_id")
    if not track_id:
        return {"ok": False, "error": "track_id required"}
    ok = cat.add_track_to_song_playlist(playlist_id, int(track_id))
    return {"ok": ok}


@app.post("/api/song-playlists/{playlist_id}/remove")
async def remove_from_song_playlist(playlist_id: int, body: dict):
    index = body.get("index")
    if index is None:
        return {"ok": False, "error": "index required"}
    ok = cat.remove_track_from_song_playlist(playlist_id, int(index))
    return {"ok": ok}


@app.delete("/api/song-playlists/{playlist_id}")
async def delete_song_playlist(playlist_id: int):
    cat.delete_song_playlist(playlist_id)
    return {"ok": True}


@app.post("/api/song-playlists/{playlist_id}/play")
async def play_song_playlist(playlist_id: int, body: dict):
    """Play a song playlist by queuing individual tracks."""
    pl = cat.get_song_playlist(playlist_id)
    if not pl or not pl.get("tracks"):
        return {"ok": False, "error": "Empty playlist or not found"}

    targets = body.get("devices") or state.settings.get("saved_devices", [])
    if not targets:
        return {"ok": False, "error": "No output devices selected"}

    # Group tracks by album for efficient playback
    # For now, start playback with the first track's album, at that track
    first_track = pl["tracks"][0]
    album_id = first_track["album_id"]
    track_id = first_track["id"]

    # Stop any active playback
    if state.is_streaming and state.stop_event:
        state.stop_event.set()
    await _stop_playback()

    volume = state.settings.get("volume", 80)
    state.player_task = asyncio.create_task(
        _run_playback(album_id, targets, volume, start_track_id=track_id)
    )

    # Queue remaining tracks from different albums
    # (will be picked up once player is active)
    return {"ok": True, "playing": first_track["title"]}


@app.put("/api/catalog/{album_id}/position")
async def update_playback_position(album_id: int, body: dict):
    """Save playback position for resume. body: { side_idx: string, secs: float }"""
    side_idx = body.get("side_idx")
    secs = body.get("secs", 0.0)
    cat.update_playback_position(album_id, side_idx, float(secs))
    return {"ok": True}


@app.post("/api/player/queue/insert-next")
async def player_queue_insert_next(body: dict):
    """Insert album sides right after currently playing track in queue."""
    if not state.player:
        return {"ok": False, "error": "No player active"}

    album_id = body.get("album_id")
    if not album_id:
        return {"ok": False, "error": "album_id required"}

    album_info = cat.get_album(album_id)
    if not album_info:
        return {"ok": False, "error": "Album not found"}

    # Get all sides for this album from tracks
    all_sides = set()
    for track in cat.get_album_tracks(album_id):
        if track.get("side"):
            all_sides.add(track["side"])

    sides = sorted(all_sides)
    entries = []
    for side in sides:
        entry = _build_side_entry(album_id, side, album_info)
        if entry:
            entries.append(entry)

    if not entries:
        return {"ok": False, "error": "No playable sides found"}

    # Insert after current position
    current_idx = state.player.current_index if state.player else 0
    insert_pos = current_idx + 1

    for i, entry in enumerate(entries):
        state.player.queue.insert(insert_pos + i, entry)

    return {"ok": True, "inserted": len(entries)}


@app.put("/api/catalog/{album_id}/rating")
async def update_album_rating(album_id: int, body: dict):
    """Set star rating (0-5) for an album. body: { rating: int }"""
    rating = int(body.get("rating", 0))
    cat.update_album_rating(album_id, rating)
    return {"ok": True, "rating": rating}


@app.put("/api/catalog/{album_id}/notes")
async def update_album_notes(album_id: int, body: dict):
    """Update the notes field for an album. body: { notes: string }"""
    notes = body.get("notes", "")
    cat.update_album_notes(album_id, notes)
    return {"ok": True}


async def _apply_eq_dict(eq: dict):
    """Apply an EQ preset (bass/treble/bands/volume) to the live EQ, persist it
    as the current settings, and broadcast so open UIs update their sliders."""
    bass   = float(eq.get("bass", 0))
    treble = float(eq.get("treble", 0))
    bands  = [float(b) for b in (eq.get("bands") or [0, 0, 0, 0, 0])][:5]
    state.eq.set_eq(bass, treble)
    state.eq.set_bands(bands)
    state.settings["bass"] = bass
    state.settings["treble"] = treble
    state.settings["eq_bands"] = state.eq.band_values
    if eq.get("volume") is not None:
        vol = int(eq["volume"])
        state.eq.set_volume(vol)
        state.settings["volume"] = vol
    save_settings(state.settings)
    await broadcast("eq_update", {"eq": {"bass": bass, "treble": treble,
                                         "bands": state.eq.band_values,
                                         "volume": state.eq.values[2]}})


@app.post("/api/catalog/{album_id}/eq")
async def save_album_eq(album_id: int):
    """Save the current live EQ (bass/treble/bands/volume) as this album's preset."""
    bass, treble, volume = state.eq.values
    cat.set_album_eq(album_id, {
        "bass": bass, "treble": treble,
        "bands": state.eq.band_values, "volume": volume,
    })
    return {"ok": True, "eq": {"bass": bass, "treble": treble,
                               "bands": state.eq.band_values, "volume": volume}}


@app.delete("/api/catalog/{album_id}/eq")
async def delete_album_eq(album_id: int):
    """Remove this album's saved EQ preset."""
    cat.clear_album_eq(album_id)
    return {"ok": True}


# ── Feature 1: Inline Metadata Editing ────────────────────────────────────

@app.put("/api/catalog/{album_id}/metadata")
async def update_album_metadata(album_id: int, body: Annotated[dict, Body()]):
    """Update album metadata fields. body: {title?, artist?, year?, genre?, label?}"""
    success = cat.update_album_metadata(album_id, body)
    if not success:
        return JSONResponse({"ok": False, "error": "Album not found"}, status_code=404)
    return {"ok": True}


# ── Maintenance: Rebuild Fingerprints ─────────────────────────────────────

async def _rebuild_fingerprints_worker():
    """Walk every playable track, re-extract its PCM slice from the side
    FLAC, and replace its fingerprint rows.

    Runs entirely off the event loop via run_in_executor; we just poke
    progress state and broadcast on each track's completion. The hot
    work (ffmpeg decode + fpcalc + DB write) is CPU/IO bound but cheap
    enough on a Pi 5 to do all of it serially without thread-pool churn.
    """
    loop = asyncio.get_event_loop()
    tracks = await loop.run_in_executor(None, cat.get_all_playable_tracks)
    total = len(tracks)

    state.rebuild_fp.update({
        "in_progress":  True,
        "total":        total,
        "done":         0,
        "ok":           0,
        "failed":       0,
        "current":      None,
        "started_at":   time.time(),
        "finished_at":  None,
        "last_error":   None,
    })

    # Pre-run backup so a bad rebuild is reversible. Filename is timestamped
    # so repeated rebuilds don't clobber each other.
    backup_path = (
        Path(__file__).parent / f"fingerprints_backup_{int(time.time())}.json"
    )
    try:
        await loop.run_in_executor(
            None, cat.backup_fingerprints_to, str(backup_path)
        )
        state.rebuild_fp["backup_path"] = str(backup_path)
    except Exception as e:
        state.rebuild_fp["last_error"] = f"Backup failed: {e}"
        print(f"[rebuild-fp] Backup failed: {e}")

    await broadcast("rebuild_fingerprints_progress", dict(state.rebuild_fp))

    for t in tracks:
        state.rebuild_fp["current"] = {
            "track_id": t["track_id"],
            "title":    t["track_title"],
            "album":    t["album_title"],
        }
        try:
            result = await loop.run_in_executor(
                None,
                cat.rebuild_track_fingerprints,
                t["track_id"],
                t["audio_path"],
                float(t["start_secs"]),
                float(t["end_secs"]),
            )
        except Exception as e:
            result = {"ok": False, "rows": 0, "error": str(e)}

        if result["ok"]:
            state.rebuild_fp["ok"] += 1
        else:
            state.rebuild_fp["failed"] += 1
            state.rebuild_fp["last_error"] = (
                f"{t['album_title']}: {t['track_title']}: {result['error']}"
            )
        state.rebuild_fp["done"] += 1

        # Broadcast every 5 tracks (or always on the last) to keep the WS
        # quiet while still feeling responsive on a ~1000-track library.
        if state.rebuild_fp["done"] % 5 == 0 or state.rebuild_fp["done"] == total:
            await broadcast("rebuild_fingerprints_progress", dict(state.rebuild_fp))

    state.rebuild_fp["in_progress"] = False
    state.rebuild_fp["current"]     = None
    state.rebuild_fp["finished_at"] = time.time()
    await broadcast("rebuild_fingerprints_progress", dict(state.rebuild_fp))
    print(
        f"[rebuild-fp] Done: {state.rebuild_fp['ok']} ok / "
        f"{state.rebuild_fp['failed']} failed out of {total}"
    )


@app.post("/api/maintenance/rebuild-fingerprints")
async def rebuild_fingerprints_start():
    """Kick off the full-library fingerprint rebuild. Returns immediately
    with a 202-ish payload; progress is broadcast over WS and pollable
    via the status endpoint."""
    if state.rebuild_fp["in_progress"]:
        return {"ok": False, "error": "Rebuild already in progress",
                "progress": dict(state.rebuild_fp)}
    spawn_bg(_rebuild_fingerprints_worker())
    return {"ok": True, "started": True}


@app.get("/api/maintenance/rebuild-fingerprints/status")
async def rebuild_fingerprints_status():
    return {"ok": True, "progress": dict(state.rebuild_fp)}


# ── Feature 2: Duplicate Detection ────────────────────────────────────────


# ── Feature 4: Player Status Restoration ──────────────────────────────────

@app.get("/api/player/status")
async def get_player_status():
    """Get current player status (for page reload restoration)."""
    if not state.player:
        return {"state": "stopped"}
    status = state.player.get_status()
    return status


# ── Feature 6: Soft Delete Support ───────────────────────────────────────

@app.post("/api/catalog/{album_id}/soft-delete")
async def soft_delete_album_route(album_id: int):
    """Soft-delete an album (can be restored)."""
    success = cat.soft_delete_album(album_id)
    if not success:
        return JSONResponse({"ok": False, "error": "Album not found"}, status_code=404)
    return {"ok": True}


@app.post("/api/catalog/{album_id}/restore")
async def restore_album_route(album_id: int):
    """Restore a soft-deleted album."""
    success = cat.restore_album(album_id)
    if not success:
        return JSONResponse({"ok": False, "error": "Album not found"}, status_code=404)
    return {"ok": True}


# ── Library Export ────────────────────────────────────────────────────────

@app.get("/api/export/catalog")
async def export_catalog():
    """Download the SQLite catalog database file."""
    db_path = cat.DB_PATH
    if not db_path.exists():
        return {"ok": False, "error": "Catalog database not found"}
    return FileResponse(
        db_path,
        media_type="application/octet-stream",
        filename=f"vinyl-catalog-{Path.cwd().name}.db"
    )


@app.get("/api/export/manifest")
async def export_manifest():
    """Download a JSON manifest listing all albums, tracks, and audio file paths."""
    manifest = cat.get_export_manifest(state.settings)
    return {
        "ok": True,
        "audio_directory": manifest["audio_directory"],
        "albums": manifest["albums"],
        "total_flac_files": manifest["total_flac_files"],
        "total_size_bytes": manifest["total_size_bytes"],
        "catalog_db_path": manifest["catalog_db_path"],
    }


@app.get("/api/now-playing")
async def now_playing():
    if not state.now_playing:
        return {"track_title": None}
    return {**state.now_playing, "artwork_url": _art_url(state.now_playing)}

# ── Album Recording (Full-Side Capture) ──────────────────────────────────────


@app.post("/api/album-recording/start")
async def album_recording_start(body: dict):
    """
    Start recording a full album side to FLAC.
    body: { album_id: int, side: str ("A", "B", etc.) }
    Requires streaming or listen mode to be active.
    """
    album_id = body.get("album_id")
    side = body.get("side", "A").upper()

    if not album_id:
        return {"ok": False, "error": "album_id required"}

    if state.album_recorder and state.album_recorder.is_active:
        return {"ok": False, "error": "Album recording already in progress: stop it first"}

    # Give the recording its own reference on the shared capture (attaches to
    # the running capture when streaming): stopping the stream mid-recording
    # then leaves the recording running
    if not state.listen_task:
        await _start_listen_mode()
        await asyncio.sleep(0.5)

    # Get album info for metadata
    albums = cat.get_all_albums()
    album = next((a for a in albums if a["id"] == album_id), None)
    if not album:
        return {"ok": False, "error": f"Album {album_id} not found"}

    album_info = {
        "artist": album["artist"],
        "title":  album["title"],
        "year":   album.get("year"),
        "genre":  album.get("genre"),
    }

    # Clear stale track timestamps from any previous recording of this side
    cat.reset_side_track_timestamps(album_id, side)

    # Create the album recorder
    audio_dir = cat.get_audio_storage_dir(state.settings)
    state.album_recorder = rec.AlbumRecorder(album_id, side, album_info,
                                             audio_dir=audio_dir,
                                             gate_threshold=state.settings.get("audio_detect_threshold", 0.006))

    # Notify UI when audio is first detected
    _loop = asyncio.get_event_loop()
    _aid, _side = album_id, side
    def _on_album_audio_detected():
        asyncio.run_coroutine_threadsafe(
            broadcast("album_recording_status", {
                "recording": True, "album_id": _aid, "side": _side,
                "message": f"\u23fa Recording Side {_side} \u2014 audio detected",
            }), _loop)
    state.album_recorder.on_audio_detected = _on_album_audio_detected

    # Get the tracks for this side so we can track progress
    all_tracks = cat.get_album_tracks(album_id)
    side_tracks = [t for t in all_tracks if (t.get("side") or "A") == side]

    # Mark first track
    if side_tracks:
        state.album_recorder.mark_first_track(side_tracks[0]["id"])

    # Tell the recording buffer how many tracks remain so it uses a longer
    # end-of-side threshold and doesn't stop after the first track
    print(f"[album-rec] rec_buffer={'YES' if state.rec_buffer else 'NO'}, "
          f"side_tracks={len(side_tracks)}, "
          f"durations={[t.get('duration_secs') for t in side_tracks]}")
    if state.rec_buffer:
        state.rec_buffer.remaining_tracks = len(side_tracks)
        # Pass expected track durations for time-based fallback splitting
        # (handles albums with seamless transitions where silence detection fails)
        expected_durs = [t.get("duration_secs", 0) or 0 for t in side_tracks]
        state.rec_buffer.set_expected_durations(expected_durs)

    # Also start a learn session so fingerprints get learned automatically
    # (reuses existing learn infrastructure)
    if not state.learn_session and state.rec_buffer and _ensure_audio_active():
        loop = asyncio.get_event_loop()
        session = LearnSession(album_id, len(side_tracks), loop, side=side)
        if session.pending_tracks:
            state.learn_session = session
            if state.recogniser:
                state.recogniser.set_learning_mode(True)

            def _on_learn_track_ready(pcm, dur):
                if state.learn_session and state.learn_session.active:
                    state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
                # Also mark track boundary in album recorder
                if state.album_recorder and state.album_recorder.is_active:
                    next_id = state.learn_session.next_track_id() if state.learn_session else None
                    state.album_recorder.mark_track_boundary(next_id)
                    # Notify UI of track boundary with completed track name
                    # tc includes the new boundary for the upcoming track, so
                    # the just-completed track is at tc-2 (tc-1 is the next one)
                    tc = state.album_recorder.track_count if state.album_recorder else 0
                    track_name = side_tracks[tc-2]["title"] if tc >= 2 and (tc-2) < len(side_tracks) else None
                    asyncio.run_coroutine_threadsafe(
                        broadcast("album_recording_status", {
                            "recording": True, "album_id": _aid, "side": _side,
                            "message": f"\u23fa Recording Side {_side}: {tc-1} track(s) learned",
                            "track_name": track_name,
                        }), _loop)

            state.rec_buffer._on_track_ready = _on_learn_track_ready
            state.rec_buffer.start(auto_split=True)
            _start_stall_watchdog()

    await broadcast("album_recording_status", {
        "recording": True,
        "album_id": album_id,
        "side": side,
        "album_name": f"{album['artist']}: {album['title']}",
        "side_tracks": len(side_tracks),
        "message": f"Recording Side {side}: drop the needle when ready",
    })

    return {
        "ok": True,
        "album_id": album_id,
        "side": side,
        "side_tracks": len(side_tracks),
    }


@app.post("/api/album-recording/flip")
async def album_recording_flip(body: dict):
    """
    Finish current side and start recording the next side.
    body: { side: str ("B", "C", etc.) }
    """
    if not state.album_recorder:
        return {"ok": False, "error": "No album recording in progress"}

    album_id = state.album_recorder.album_id
    already_finalized = not state.album_recorder.is_active
    loop = asyncio.get_event_loop()
    _stop_stall_watchdog()

    # Stop learn session for this side
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()

    # Encode current side in background (skip if auto-finalize already did it)
    if not already_finalized:
        ar = state.album_recorder

        async def _finish_and_start_next():
            path, duration, boundaries = await loop.run_in_executor(None, ar.finish)
            if path:
                file_size = path.stat().st_size
                cat.save_album_audio(album_id, ar.side, str(path), duration, file_size)

                for b in boundaries:
                    if b["track_id"] and b["end_secs"] is not None:
                        cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

                cat.correct_side_boundaries(album_id, ar.side, duration)

                all_tracks = cat.get_album_tracks(album_id)
                album_sides = sorted({t.get("side") or "A" for t in all_tracks})
                idx = album_sides.index(ar.side) if ar.side in album_sides else -1
                hn = idx >= 0 and idx < len(album_sides) - 1
                ns = album_sides[idx + 1] if hn else None
                await broadcast("album_recording_side_saved", {
                    "album_id": album_id,
                    "side": ar.side,
                    "duration_secs": round(duration, 1),
                    "size_mb": round(file_size / (1024 * 1024), 1),
                    "tracks_captured": len(boundaries),
                    "has_next_side": hn,
                    "next_side": ns,
                })

        spawn_bg(_finish_and_start_next())

    # Start new side
    new_side = body.get("side", "B").upper()
    albums = cat.get_all_albums()
    album = next((a for a in albums if a["id"] == album_id), None)
    if not album:
        return {"ok": False, "error": "Album not found"}

    album_info = {
        "artist": album["artist"],
        "title":  album["title"],
        "year":   album.get("year"),
        "genre":  album.get("genre"),
    }

    audio_dir = cat.get_audio_storage_dir(state.settings)
    state.album_recorder = rec.AlbumRecorder(album_id, new_side, album_info,
                                             audio_dir=audio_dir,
                                             gate_threshold=state.settings.get("audio_detect_threshold", 0.006))

    # Notify UI when audio is first detected on new side
    _loop2 = asyncio.get_event_loop()
    _aid2, _side2 = album_id, new_side
    def _on_album_audio_detected_flip():
        asyncio.run_coroutine_threadsafe(
            broadcast("album_recording_status", {
                "recording": True, "album_id": _aid2, "side": _side2,
                "message": f"\u23fa Recording Side {_side2} \u2014 audio detected",
            }), _loop2)
    state.album_recorder.on_audio_detected = _on_album_audio_detected_flip

    # Get tracks for new side
    all_tracks = cat.get_album_tracks(album_id)
    side_tracks = [t for t in all_tracks if (t.get("side") or "A") == new_side]

    if side_tracks:
        state.album_recorder.mark_first_track(side_tracks[0]["id"])

    # Tell the recording buffer how many tracks remain on new side
    if state.rec_buffer:
        state.rec_buffer.remaining_tracks = len(side_tracks)
        expected_durs = [t.get("duration_secs", 0) or 0 for t in side_tracks]
        state.rec_buffer.set_expected_durations(expected_durs)

    # Restart learn session for new side
    if state.rec_buffer and (state.is_streaming or state.listen_task):
        session = LearnSession(album_id, len(side_tracks), _loop2, side=new_side)
        if session.pending_tracks:
            state.learn_session = session
            if state.recogniser:
                state.recogniser.set_learning_mode(True)

            def _on_learn_track_ready(pcm, dur):
                if state.learn_session and state.learn_session.active:
                    state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
                if state.album_recorder and state.album_recorder.is_active:
                    next_id = state.learn_session.next_track_id() if state.learn_session else None
                    state.album_recorder.mark_track_boundary(next_id)
                    # tc includes the new boundary for the upcoming track, so
                    # the just-completed track is at tc-2 (tc-1 is the next one)
                    tc = state.album_recorder.track_count if state.album_recorder else 0
                    track_name = side_tracks[tc-2]["title"] if tc >= 2 and (tc-2) < len(side_tracks) else None
                    asyncio.run_coroutine_threadsafe(
                        broadcast("album_recording_status", {
                            "recording": True, "album_id": _aid2, "side": _side2,
                            "message": f"\u23fa Recording Side {_side2}: {tc-1} track(s) learned",
                            "track_name": track_name,
                        }), _loop2)

            state.rec_buffer._on_track_ready = _on_learn_track_ready
            state.rec_buffer.start(auto_split=True)
            _start_stall_watchdog()

    await broadcast("album_recording_status", {
        "recording": True,
        "album_id": album_id,
        "side": new_side,
        "album_name": f"{album['artist']}: {album['title']}",
        "side_tracks": len(side_tracks),
        "message": f"Recording Side {new_side}: flip the record and drop the needle",
    })

    return {"ok": True, "side": new_side, "side_tracks": len(side_tracks)}


@app.post("/api/album-recording/stop")
async def album_recording_stop():
    """Stop the current album recording, encode to FLAC, and save."""
    if not state.album_recorder:
        return {"ok": False, "error": "No album recording in progress"}

    ar = state.album_recorder
    already_finalized = not ar.is_active
    state.album_recorder = None
    album_id = ar.album_id

    # If auto-finalize already saved the FLAC, just clean up
    if already_finalized:
        if state.learn_session:
            state.learn_session.active = False
            state.learn_session = None
        if state.recogniser:
            state.recogniser.set_learning_mode(False)
        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "message": "Recording stopped",
        })
        return {"ok": True, "already_finalized": True}
    _stop_stall_watchdog()

    # Stop learn session
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()

    await broadcast("album_recording_status", {
        "recording": False,
        "album_id": album_id,
        "side": ar.side,
        "message": f"Stop pressed: encoding Side {ar.side} in background...",
    })

    spawn_bg(_encode_and_save_album_side(ar))

    return {
        "ok": True,
        "side": ar.side,
        "encoding_in_background": True,
        "message": f"Encoding Side {ar.side} is running on the server",
    }


@app.get("/api/album-recording/status")
async def album_recording_status():
    """Get current album recording status.
    Returns recording state so the UI can sync after a WebSocket reconnect."""
    if not state.album_recorder:
        return {
            "recording": False,
            "awaiting_flip": False,
            "encoding": state.album_encoding,
        }
    ar = state.album_recorder
    if not ar.is_active:
        # Recorder exists but inactive = side was auto-finalized, awaiting flip
        all_tracks = cat.get_album_tracks(ar.album_id)
        album_sides = sorted({t.get("side") or "A" for t in all_tracks})
        current_idx = album_sides.index(ar.side) if ar.side in album_sides else -1
        has_next = current_idx >= 0 and current_idx < len(album_sides) - 1
        next_side = album_sides[current_idx + 1] if has_next else None
        return {
            "recording": False,
            "awaiting_flip": has_next,
            "album_id": ar.album_id,
            "side": ar.side,
            "next_side": next_side,
        }
    return {
        "recording": True,
        "awaiting_flip": False,
        "album_id": ar.album_id,
        "side": ar.side,
        "elapsed_secs": round(ar.elapsed_secs, 1),
        "encoding": state.album_encoding,
        "tracks_captured": ar.track_count,
    }


@app.post("/api/album-recording/cancel")
async def album_recording_cancel():
    """Cancel the current album recording without saving."""
    _stop_stall_watchdog()
    if state.album_recorder:
        state.album_recorder.cancel()
        state.album_recorder = None
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()
    await broadcast("album_recording_status", {
        "recording": False,
        "message": "Album recording cancelled",
    })
    return {"ok": True}


# ── Catalog Playback (Player) ────────────────────────────────────────────────


@app.post("/api/player/play")
async def player_play(body: dict):
    """
    Start catalog playback through AirPlay devices.
    body: { album_id: int, devices?: [{id, name}], track_id?: int }
    If devices not specified, uses last-used saved devices.
    """
    album_id = body.get("album_id")
    if not album_id:
        return {"ok": False, "error": "album_id required"}

    # Check album has recorded audio
    audio_files = cat.get_album_audio(album_id)
    if not audio_files:
        return {"ok": False, "error": "No recorded audio for this album. Record it first."}

    # Get target devices
    targets = body.get("devices") or state.settings.get("saved_devices", [])
    if not targets:
        return {"ok": False, "error": "No AirPlay devices selected"}

    volume = body.get("volume", state.settings.get("volume", 80))
    track_id = body.get("track_id")
    resume_position = body.get("resume_position_secs")

    # Auto-load this album's saved EQ preset before playback starts (#53)
    if state.settings.get("eq_auto_load"):
        _album_eq = cat.get_album_eq(album_id)
        if _album_eq:
            await _apply_eq_dict(_album_eq)

    # Stop any active vinyl streaming first
    if state.is_streaming:
        if state.stop_event:
            state.stop_event.set()
        if state.stream_task:
            state.stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.stream_task
            state.stream_task = None

    # Stop any active listen mode
    _stop_listen_mode()
    await asyncio.sleep(0.3)  # let audio device release

    # Stop any existing playback
    await _stop_playback()

    # Start playback
    state.player_task = asyncio.create_task(
        _run_playback(album_id, targets, volume, start_track_id=track_id,
                      resume_position_secs=resume_position)
    )
    return {"ok": True}


@app.post("/api/player/play-queue")
async def player_play_queue(body: dict):
    """
    Queue multiple albums/sides for sequential playback.
    body: { album_ids: [int], entries?: [{a:int,s:str}], devices?: [{id, name}], volume?: int }
    If 'entries' is provided, uses side-level ordering. Otherwise falls back to album_ids.
    """
    entries = body.get("entries")
    album_ids = body.get("album_ids", [])
    if not entries and not album_ids:
        return {"ok": False, "error": "album_ids or entries required"}

    targets = body.get("devices") or state.settings.get("saved_devices", [])
    if not targets:
        return {"ok": False, "error": "No AirPlay devices selected"}

    volume = body.get("volume", state.settings.get("volume", 80))

    # Cache album info lookups
    albums = cat.get_all_albums()
    albums_by_id = {a["id"]: a for a in albums}

    combined_playlist = []

    if entries:
        # Side-level entries: [{a: album_id, s: side}, ...]
        for entry in entries:
            aid = entry.get("a")
            side = entry.get("s")
            if not aid or not side:
                continue
            album_info = albums_by_id.get(aid)
            if not album_info:
                continue
            pe = _build_side_entry(aid, side, album_info)
            if pe:
                combined_playlist.append(pe)
    else:
        # Legacy: album_ids -- expand all sides per album
        for aid in album_ids:
            album_info = albums_by_id.get(aid)
            if not album_info:
                continue
            audio_files = cat.get_album_audio(aid)
            for af in audio_files:
                pe = _build_side_entry(aid, af["side"], album_info)
                if pe:
                    combined_playlist.append(pe)

    if not combined_playlist:
        return {"ok": False, "error": "No recorded audio found for any of the selected albums"}

    # Stop streaming / listen / existing playback
    if state.is_streaming:
        if state.stop_event:
            state.stop_event.set()
        if state.stream_task:
            state.stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.stream_task
            state.stream_task = None
    _stop_listen_mode()
    await asyncio.sleep(0.3)
    await _stop_playback()

    # Start combined playback using first album as the "base" info
    first_aid = album_ids[0]
    first_info = next((a for a in cat.get_all_albums() if a["id"] == first_aid), {})

    state.player_task = asyncio.create_task(
        _run_playback_queue(first_aid, first_info, combined_playlist, targets, volume)
    )
    return {"ok": True, "queued": len(combined_playlist)}


@app.post("/api/player/play-shuffle-tracks")
async def player_play_shuffle_tracks(body: dict):
    """
    Play every track in the catalog in random order, one track at a time.

    This is distinct from the existing album-shuffle: instead of shuffling
    whole albums and playing them through, we treat each track as its own
    playlist entry (using PlaylistEntry.source_offset_secs /
    source_duration_secs to bound playback to just that track's slice of
    its side FLAC), then shuffle the list.

    body: { devices?: [{id, name}], volume?: int, limit?: int }
    """
    targets = body.get("devices") or state.settings.get("saved_devices", [])
    if not targets:
        return {"ok": False, "error": "No AirPlay devices selected"}
    volume = body.get("volume", state.settings.get("volume", 80))

    tracks = cat.get_all_playable_tracks()
    if not tracks:
        return {"ok": False, "error": "No recorded tracks available"}

    random.shuffle(tracks)
    limit = body.get("limit")
    if isinstance(limit, int) and limit > 0:
        tracks = tracks[:limit]

    combined_playlist = []
    for t in tracks:
        start = float(t["start_secs"])
        end   = float(t["end_secs"])
        dur   = max(0.0, end - start)
        if dur <= 0:
            continue
        # Single-track entry: tracks list has just this one track, with
        # entry-local timing (0 -> dur). Player will start ffmpeg with
        # -ss start -t dur so only this track's audio is decoded.
        track_obj = {
            "id":           t["track_id"],
            "title":        t["track_title"],
            "artist":       t.get("album_artist"),
            "track_number": t.get("track_number"),
            "start_secs":   0.0,
            "end_secs":     dur,
        }
        combined_playlist.append(plr.PlaylistEntry(
            audio_path=t["audio_path"],
            side=t.get("side") or "",
            duration_secs=dur,
            tracks=[track_obj],
            album_id=t["album_id"],
            album_title=t["album_title"],
            album_artist=t["album_artist"],
            artwork_path=t.get("user_artwork_path") or t.get("artwork_path"),
            source_offset_secs=start,
            source_duration_secs=dur,
        ))

    if not combined_playlist:
        return {"ok": False, "error": "No playable tracks (all missing boundaries)"}

    # Stop streaming / listen / existing playback (same pattern as play-queue)
    if state.is_streaming:
        if state.stop_event:
            state.stop_event.set()
        if state.stream_task:
            state.stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.stream_task
            state.stream_task = None
    _stop_listen_mode()
    await asyncio.sleep(0.3)
    await _stop_playback()

    first = combined_playlist[0]
    base_info = {
        "id":     first.album_id,
        "title":  first.album_title,
        "artist": first.album_artist,
    }
    state.player_task = asyncio.create_task(
        _run_playback_queue(first.album_id, base_info, combined_playlist, targets, volume)
    )
    return {"ok": True, "queued": len(combined_playlist)}


@app.post("/api/player/pause")
async def player_pause():
    if state.player:
        state.player.toggle_pause()
        return {"ok": True, "state": state.player.state}
    return {"ok": False, "error": "Not playing"}


@app.post("/api/player/stop")
async def player_stop():
    await _stop_playback()
    return {"ok": True}


@app.post("/api/player/next")
async def player_next():
    if state.player:
        state.player.next_track()
        return {"ok": True}
    return {"ok": False, "error": "Not playing"}


@app.post("/api/player/prev")
async def player_prev():
    if state.player:
        state.player.prev_track()
        return {"ok": True}
    return {"ok": False, "error": "Not playing"}


@app.post("/api/player/repeat")
async def player_repeat():
    """Cycle repeat mode: off → album → track → off."""
    if state.player:
        mode = state.player.cycle_repeat()
        return {"ok": True, "repeat_mode": mode}
    return {"ok": False, "error": "Not playing"}


@app.post("/api/player/seek")
async def player_seek(body: dict):
    """Seek to position or track. body: { position_secs?: float, track_id?: int }"""
    if not state.player:
        return {"ok": False, "error": "Not playing"}
    if "track_id" in body:
        state.player.seek_to_track(body["track_id"])
    elif "position_secs" in body:
        state.player.seek_to(float(body["position_secs"]))
    else:
        return {"ok": False, "error": "Provide position_secs or track_id"}
    return {"ok": True}


@app.get("/api/player/queue")
async def player_queue():
    """Get current queue (playlist) with current index."""
    if not state.player:
        return {"ok": True, "queue": [], "current_index": -1}

    queue = []
    for i, entry in enumerate(state.player.playlist):
        tracks = []
        for t in entry.tracks:
            dur = None
            if t.get("end_secs") is not None and t.get("start_secs") is not None:
                dur = t["end_secs"] - t["start_secs"]
            tracks.append({
                "id": t.get("id"),
                "title": t.get("title", ""),
                "track_number": t.get("track_number"),
                "duration_secs": dur,
            })
        queue.append({
            "index": i,
            "album_id": entry.album_id,
            "album_title": entry.album_title or "",
            "album_artist": entry.album_artist or "",
            "side": entry.side,
            "artwork": entry.artwork_path or "",
            "tracks": tracks,
        })

    current = state.player._side_idx if hasattr(state.player, '_side_idx') else -1
    current_track_idx = state.player._current_track_idx if hasattr(state.player, '_current_track_idx') else -1
    return {"ok": True, "queue": queue, "current_index": current, "current_track_idx": current_track_idx}


@app.post("/api/player/queue/add")
async def player_queue_add(body: dict):
    """Add album to end of queue. body: { album_id: int }"""
    album_id = body.get("album_id")
    if not album_id:
        return {"ok": False, "error": "album_id required"}

    # Get album info and audio files
    audio_files = cat.get_album_audio(album_id)
    if not audio_files:
        return {"ok": False, "error": f"No recorded audio for album {album_id}"}

    albums = cat.get_all_albums()
    album_info = next((a for a in albums if a["id"] == album_id), None)
    if not album_info:
        return {"ok": False, "error": f"Album {album_id} not found"}

    all_tracks = cat.get_album_tracks(album_id)

    # Build playlist entries for this album's sides
    new_entries = []
    for af in audio_files:
        side = af["side"]
        side_tracks = [
            {
                "id":           t["id"],
                "title":        t["title"],
                "artist":       t.get("artist") or album_info["artist"],
                "track_number": t["track_number"],
                "start_secs":   t.get("start_secs"),
                "end_secs":     t.get("end_secs"),
            }
            for t in all_tracks
            if t["side"] == side
        ]
        if side_tracks:
            first_start = min(
                (t["start_secs"] for t in side_tracks if t["start_secs"] is not None),
                default=0,
            )
            if first_start > 5.0:
                for t in side_tracks:
                    if t["start_secs"] is not None:
                        t["start_secs"] -= first_start
                    if t["end_secs"] is not None:
                        t["end_secs"] -= first_start

        new_entries.append(plr.PlaylistEntry(
            audio_path=af["file_path"],
            side=side,
            duration_secs=af.get("duration_secs"),
            tracks=side_tracks,
            album_id=album_id,
            album_title=album_info["title"],
            album_artist=album_info["artist"],
            artwork_path=album_info.get("user_artwork_path") or album_info.get("artwork_path"),
        ))

    if not new_entries:
        return {"ok": False, "error": "Could not create playlist entries"}

    # If player doesn't exist or is stopped, start playback with just this album
    if not state.player or state.player.state == "stopped":
        await _stop_playback()
        targets = state.settings.get("saved_devices", [])
        if not targets:
            return {"ok": False, "error": "No AirPlay devices selected"}

        volume = state.settings.get("volume", 80)
        state.player_task = asyncio.create_task(
            _run_playback_queue(album_id, album_info, new_entries, targets, volume)
        )
        await broadcast("queue_updated", {"queue": []})
        return {"ok": True, "message": "Starting playback"}

    # Add to existing queue
    state.player.playlist.extend(new_entries)
    # Build queue info for broadcast
    queue_info = []
    for i, entry in enumerate(state.player.playlist):
        queue_info.append({
            "index": i,
            "album_id": entry.album_id,
            "album_title": entry.album_title or "",
            "album_artist": entry.album_artist or "",
            "side": entry.side,
            "artwork": entry.artwork_path or "",
        })
    await broadcast("queue_updated", {"queue": queue_info})
    return {"ok": True, "message": f"Added {len(new_entries)} side(s) to queue"}


@app.post("/api/player/queue/clear")
async def player_queue_clear():
    """Clear queue (stop after current album finishes)."""
    if not state.player:
        return {"ok": False, "error": "No playback active"}

    # Keep only the current album, clear the rest
    if state.player.playlist and hasattr(state.player, '_side_idx'):
        current_idx = state.player._side_idx
        # Find which album the current side belongs to
        if current_idx >= 0 and current_idx < len(state.player.playlist):
            current_album_id = state.player.playlist[current_idx].album_id
            # Keep only sides from current album
            state.player.playlist = [
                entry for entry in state.player.playlist
                if entry.album_id == current_album_id
            ]

    # Build queue info for broadcast
    queue_info = []
    for i, entry in enumerate(state.player.playlist):
        queue_info.append({
            "index": i,
            "album_id": entry.album_id,
            "album_title": entry.album_title or "",
            "album_artist": entry.album_artist or "",
            "side": entry.side,
            "artwork": entry.artwork_path or "",
        })
    await broadcast("queue_updated", {"queue": queue_info})
    return {"ok": True}


@app.post("/api/player/queue/remove")
async def player_queue_remove(body: dict):
    """Remove a single side from the queue by index. body: { index: int }"""
    if not state.player:
        return {"ok": False, "error": "No playback active"}

    idx = body.get("index")
    if idx is None or not isinstance(idx, int):
        return {"ok": False, "error": "index required (integer)"}

    pl = state.player.playlist
    cur = state.player._side_idx

    if idx < 0 or idx >= len(pl):
        return {"ok": False, "error": "Index out of range"}

    # Don't allow removing the currently playing side
    if idx == cur:
        return {"ok": False, "error": "Cannot remove the currently playing side"}

    pl.pop(idx)

    # Adjust current index if we removed something before it
    if idx < cur:
        state.player._side_idx -= 1

    # Kill pre-started next ffmpeg since playlist changed
    state.player._kill_next_ffmpeg()
    state.player._prestart_next_side()

    # Broadcast updated queue
    queue_info = []
    for i, entry in enumerate(state.player.playlist):
        queue_info.append({
            "index": i,
            "album_id": entry.album_id,
            "album_title": entry.album_title or "",
            "album_artist": entry.album_artist or "",
            "side": entry.side,
            "artwork": entry.artwork_path or "",
        })
    await broadcast("queue_updated", {"queue": queue_info})
    return {"ok": True}


@app.post("/api/player/queue/reorder")
async def player_queue_reorder(body: dict):
    """Move a queue item from one index to another. body: { from: int, to: int }"""
    if not state.player:
        return {"ok": False, "error": "No playback active"}

    from_idx = body.get("from")
    to_idx = body.get("to")
    if from_idx is None or to_idx is None:
        return {"ok": False, "error": "from and to required (integers)"}

    pl = state.player.playlist
    cur = state.player._side_idx

    if from_idx < 0 or from_idx >= len(pl) or to_idx < 0 or to_idx >= len(pl):
        return {"ok": False, "error": "Index out of range"}

    if from_idx == to_idx:
        return {"ok": True}

    # Move the entry
    entry = pl.pop(from_idx)
    pl.insert(to_idx, entry)

    # Recalculate current index - track where the playing side ended up
    if from_idx == cur:
        state.player._side_idx = to_idx
    elif from_idx < cur and to_idx >= cur:
        state.player._side_idx -= 1
    elif from_idx > cur and to_idx <= cur:
        state.player._side_idx += 1

    # Refresh gapless pre-start
    state.player._kill_next_ffmpeg()
    state.player._prestart_next_side()

    # Broadcast updated queue
    queue_info = []
    for i, entry in enumerate(state.player.playlist):
        queue_info.append({
            "index": i,
            "album_id": entry.album_id,
            "album_title": entry.album_title or "",
            "album_artist": entry.album_artist or "",
            "side": entry.side,
            "artwork": entry.artwork_path or "",
        })
    await broadcast("queue_updated", {"queue": queue_info})
    return {"ok": True}


# ── Learn Routes ──────────────────────────────────────────────────────────────


@app.post("/api/learn/start")
async def learn_start(body: dict):
    """Start a learn session: auto-starts audio capture if not already streaming."""
    """
    Start a learn session.
    body: { album_id: int, track_count: int }
    """
    if not state.is_streaming:
        return {"ok": False, "error": "Not streaming: start streaming first"}
    if not state.rec_buffer:
        return {"ok": False, "error": "Recorder not ready"}

    album_id    = body.get("album_id")
    track_count = body.get("track_count", 1)

    if not album_id:
        return {"ok": False, "error": "album_id required"}

    # Wire learn session as the track-ready callback
    loop = asyncio.get_event_loop()
    session = LearnSession(album_id, track_count, loop)

    if not session.pending_tracks:
        return {"ok": False, "error": "All tracks for this album are already learned!"}

    state.learn_session = session

    if state.recogniser:
        state.recogniser.set_learning_mode(True)
    # Start capture buffer in auto-split mode
    # Override the on_track_ready callback to route to learn session
    def _on_learn_track_ready(pcm, dur):
        """Run fpcalc in background thread so it never blocks the audio callback."""
        if state.learn_session and state.learn_session.active:
            state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
    state.rec_buffer._on_track_ready = _on_learn_track_ready
    state.rec_buffer.start(auto_split=True)

    first_track = session.next_track_name()
    await broadcast("learn_update", {
        "learned": 0,
        "track_count": track_count,
        "next_track": first_track,
        "message": f"Listening for track 1 of {track_count}: {first_track}",
    })
    return {"ok": True, "first_track": first_track, "track_count": track_count}


@app.post("/api/learn/continue")
async def learn_continue(body: dict):
    """
    Resume learning after a flip/swap.
    body: { track_count: int } : how many more tracks to do
    """
    if not state.is_streaming:
        return {"ok": False, "error": "Not streaming"}
    if not state.learn_session:
        return {"ok": False, "error": "No active learn session"}
    if not state.rec_buffer:
        return {"ok": False, "error": "Recorder not ready"}

    track_count = body.get("track_count", 1)
    session = state.learn_session
    session.track_count += track_count   # extend the session target
    session.active = True

    if state.recogniser:
        state.recogniser.set_learning_mode(True)
    state.rec_buffer.start(auto_split=True)  # restart capture

    first_track = session.next_track_name()
    await broadcast("learn_update", {
        "learned": session.learned,
        "track_count": session.learned + track_count,
        "next_track": first_track,
        "message": "Continuing: waiting for audio… drop the needle when ready",
    })
    return {"ok": True}


@app.post("/api/learn/stop")
async def learn_stop():
    """Cancel the current learn session."""
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()
    if state.listen_task:
        _stop_listen_mode()
    await broadcast("learn_done", {"learned": 0, "message": "Learn session cancelled"})
    return {"ok": True}


@app.get("/api/learn/status")
async def learn_status():
    s = state.learn_session
    if not s:
        return {"active": False}
    return {
        "active":            s.active,
        "album_id":          s.album_id,
        "learned":           s.learned,
        "track_count":       s.track_count,
        "remaining_in_album": len(s.pending_tracks),
        "next_track":        s.next_track_name() if s.pending_tracks else None,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_clients.append(ws)
    bass, treble, volume = state.eq.values
    await ws.send_text(json.dumps({
        "event": "status", "streaming": state.is_streaming,
        "devices": state.active_devices,
        "eq": {"bass": bass, "treble": treble, "volume": volume},
    }))
    if state.player:
        await ws.send_text(json.dumps({
            "event": "player_status", **state.player.get_status(),
        }))
    if state.now_playing:
        await ws.send_text(json.dumps({
            "event": "now_playing",
            **state.now_playing,
            "artwork_url": _art_url(state.now_playing),
        }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in state.ws_clients:
            state.ws_clients.remove(ws)


if __name__ == "__main__":
    cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    cert_file = os.path.join(cert_dir, "cert.pem")
    key_file = os.path.join(cert_dir, "key.pem")
    has_certs = os.path.exists(cert_file) and os.path.exists(key_file)
    if has_certs:
        print("[ssl] Certs found. HTTP on :8080 (kiosk), HTTPS on :8443 (mobile)")
        def run_https():
            uvicorn.run("main:app", host="0.0.0.0", port=8443, reload=False,
                        ssl_certfile=cert_file, ssl_keyfile=key_file,
                        log_level="warning")
        t = threading.Thread(target=run_https, daemon=True)
        t.start()
    else:
        print("[ssl] No certs found, running plain HTTP only")
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
