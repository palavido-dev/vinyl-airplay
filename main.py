#!/usr/bin/env python3
"""
Vinyl AirPlay Streamer: Web-controlled backend
16-bit / 44.1kHz lossless PCM with live bass/treble EQ + record recognition
"""

import asyncio
import json
import math
import os
import random
import shutil
import threading
import time
import traceback
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

import numpy as np
import pyatv
import sounddevice as sd
import uvicorn
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pyatv.interface import MediaMetadata
from pyatv.storage.file_storage import FileStorage

import catalog as cat
import player as plr
import recorder as rec
from app_state import broadcast, spawn_bg, state
from audio_mp3 import LiveMP3Broadcaster
from audio_streams import (
    AsyncAudioStream,
    BrowserAudioStream,
    LocalOutputStream,
    _browser_streams,
    make_callback,
    run_device_stream,
    wav_header,
)
from config import TEMPLATES, save_settings
from device_helpers import _get_bluetooth_devices, _get_local_outputs
from recognition import _art_jpeg, _art_url, _make_on_match, _make_on_unknown
from routes_bluetooth import router as bluetooth_router
from routes_catalog import router as catalog_router
from routes_catalog_stats import router as catalog_stats_router
from routes_eq import router as eq_router
from routes_export import router as export_router
from routes_settings import router as settings_router
from routes_system import router as system_router
from transports_bluetooth import BluetoothManager

# ── Audio Config ──────────────────────────────────────────────────────────────

SAMPLE_RATE      = 44100
CHANNELS         = 2    # processing/output channels (stereo)
CAPTURE_CHANNELS_MAX = 2  # stereo capture: HiFiBerry DAC2 ADC Pro returns silence
                          # if opened with >2 channels (empty TDM slots). App only
                          # ever processes L+R anyway, so 2 is correct for any device.

def _capture_channels(device_index=None) -> int:
    """Return the number of input channels to use for a given device.
    Uses the lesser of CAPTURE_CHANNELS_MAX and the device's actual max."""
    try:
        info = sd.query_devices(device_index, kind='input')
        return min(CAPTURE_CHANNELS_MAX, int(info['max_input_channels']))
    except Exception:
        return 2  # safe stereo fallback
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


# ── Main Stream Coordinator ───────────────────────────────────────────────────

# ── Auto-Stream Watcher ───────────────────────────────────────────────────────

async def _auto_stream_watcher():
    """
    Poll Scarlett RMS while idle; auto-start stream when record plays.

    Opens the InputStream only when NOT streaming, and closes it the moment
    streaming starts: this prevents ALSA 'device busy' errors when run_stream
    opens its own InputStream.
    """
    RMS_THRESHOLD = 0.008
    SUSTAIN_SECS  = 2.0
    POLL_SECS     = 1.0     # longer interval: open/close device each cycle
    COOLDOWN_SECS = 15.0
    POLL_FRAMES   = int(44100 * POLL_SECS)

    print("[auto-stream] Watcher started")
    sustained = 0.0
    cooldown  = 0.0

    try:
        while True:
            await asyncio.sleep(POLL_SECS)

            # While streaming, just count down cooldown: don't touch the device
            if state.is_streaming:
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            # Also skip while listen mode or album recording has the device open
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            # Skip while catalog playback is active (or starting up)
            if state.player_task or (state.player and state.player.state != "stopped"):
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            if cooldown > 0:
                cooldown = max(0.0, cooldown - POLL_SECS)
                continue

            # Open device, read one chunk, close immediately: never holds it open
            # Re-check right before open (race condition: listen/album may have started)
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown = COOLDOWN_SECS
                continue
            audio_idx = state.settings.get("audio_device_index")
            try:
                with sd.InputStream(device=audio_idx, samplerate=44100,
                                    channels=_capture_channels(audio_idx), dtype="float32",
                                    blocksize=POLL_FRAMES) as stream:
                    data, _ = stream.read(POLL_FRAMES)
                rms = float(np.sqrt(np.mean(data[:, :min(2, data.shape[1])] ** 2)))
            except Exception as e:
                # Suppress noisy errors when something else has the device
                if not (state.listen_task or state.is_streaming
                        or (state.album_recorder and state.album_recorder.is_active)):
                    print(f"[auto-stream] Read error: {e}")
                await asyncio.sleep(5.0)
                continue

            if rms >= RMS_THRESHOLD:
                sustained += POLL_SECS
                if sustained >= SUSTAIN_SECS:
                    sustained = 0.0
                    if time.monotonic() < state.manual_stop_until:
                        print("[auto-stream] Suppressed: manual stop cooldown active")
                        continue
                    dev = state.settings.get("auto_stream_device")
                    if not dev:
                        print("[auto-stream] Audio detected but no default device set in Settings")
                        continue
                    volume = state.settings.get("volume", 80)
                    aidx   = state.settings.get("audio_device_index")
                    print(f"[auto-stream] Starting stream to {dev.get('name')} (RMS={rms:.4f})")
                    await broadcast("auto_stream_starting", {
                        "device":  dev.get("name"),
                        "message": f"Auto-stream: starting to {dev.get('name')}…"
                    })
                    state.stream_task = asyncio.create_task(
                        run_stream([dev], aidx, volume)
                    )
                    cooldown = COOLDOWN_SECS
            else:
                sustained = 0.0

    except asyncio.CancelledError:
        print("[auto-stream] Watcher stopped")
    except Exception as e:
        print(f"[auto-stream] Watcher error: {type(e).__name__}: {e}")


async def _restart_auto_stream_watcher():
    if state.auto_stream_task and not state.auto_stream_task.done():
        state.auto_stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.auto_stream_task
        state.auto_stream_task = None
    if state.settings.get("auto_stream_enabled"):
        state.auto_stream_task = asyncio.create_task(_auto_stream_watcher())
        print("[auto-stream] Watcher (re)started")
    else:
        print("[auto-stream] Disabled")



async def run_stream(targets, audio_device_index, volume):
    try:
        await _run_stream_inner(targets, audio_device_index, volume)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[airplay] run_stream crashed: {e}")
        traceback.print_exc()
        await broadcast("error", {"message": f"Stream error: {e}"})
        state.is_streaming = False
        state.active_devices = []
        state.stop_event = None
        state.airplay_metadata = None
        await broadcast("status", {"streaming": False, "message": "Stopped (error)"})


async def _run_stream_inner(targets, audio_device_index, volume):
    state.stop_event     = asyncio.Event()
    main_loop            = asyncio.get_event_loop()

    # Separate local vs Bluetooth vs AirPlay targets
    local_targets      = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets  = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    airplay_targets    = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:"))]

    # A2DP supports only one device at a time
    if len(bluetooth_targets) > 1:
        print("[bluetooth] Multiple BT devices selected; using first only")
        bluetooth_targets = bluetooth_targets[:1]

    # Create a shared mutable MediaMetadata object: passed once to stream_file
    # and updated in-place via on_match() whenever a new track is identified
    np = state.now_playing or {}
    state.airplay_metadata = MediaMetadata(
        title  = np.get("track_title"),
        artist = np.get("track_artist") or np.get("album_artist"),
        album  = np.get("album_title"),
        artwork= _art_jpeg(np) if np else None,
    )

    # Set up AirPlay devices (if any). storage= attaches saved
    # credentials to the conf services so pyatv.connect doesn't have
    # to re-pair every time.
    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs      = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    # Set up local output streams
    local_streams = []
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            print(f"[local-out] No ALSA device for {lt.get('id')}, skipping")
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[local-out] Failed to open {alsa_dev}: {e}")

    # Set up Bluetooth output streams
    bt_streams = []
    for bt in bluetooth_targets:
        address = bt.get("address") or bt.get("id", "").replace("bt:", "")
        if not address:
            print(f"[bluetooth] No address for {bt.get('id')}, skipping")
            continue
        alsa_dev = f"bluealsa:DEV={address},PROFILE=a2dp"
        try:
            bts = LocalOutputStream(alsa_dev)
            bts.start()
            bt_streams.append(bts)
            print(f"[bluetooth] Opened stream to {address}")
        except Exception as e:
            print(f"[bluetooth] Failed to open {address}: {e}")

    http_only = False
    if not confs and not local_streams and not bt_streams:
        if state.settings.get("http_stream_enabled"):
            http_only = True
            print("[http-stream] No playback targets selected; running capture for /live.mp3 only")
        else:
            await broadcast("error", {"message": "No paired devices found on network"})
            state.stop_event=None
            return

    # Only mark streaming=True now that we have confirmed devices
    state.is_streaming   = True
    state.active_devices = [d["name"] for d in targets] if targets else ["HTTP MP3"]
    status_message = (
        "Streaming (HTTP MP3 live URL active)"
        if http_only
        else f"Streaming to {len(confs) + len(local_streams) + len(bt_streams)} device(s)"
    )
    await broadcast("status", {
        "streaming": True, "devices": state.active_devices,
        "message": status_message
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    active_count  = len(confs)
    threads_done  = asyncio.Event()
    if active_count == 0:
        threads_done.set()  # No AirPlay threads: mark done immediately

    def on_device_done(name, err):
        nonlocal active_count
        if err:
            asyncio.run_coroutine_threadsafe(
                broadcast("error", {"message": f"{name}: {err}"}), main_loop
            )
        active_count -= 1
        if active_count <= 0:
            main_loop.call_soon_threadsafe(threads_done.set)

    for conf in confs:
        threading.Thread(
            target=run_device_stream,
            args=(conf, audio_streams[conf.identifier], volume, on_device_done),
            daemon=True
        ).start()

    # Init recorder buffer
    def _on_track_ready(pcm, duration):
        """Called by RecordingBuffer when silence gap detected: new track starting."""
        # Always reset recogniser on track boundary, even when not recording
        if state.recogniser and not (state.learn_session and state.learn_session.active):
            state.recogniser.reset_match()

    def _on_level(rms):
        state.rec_level = rms
        # Broadcast real-time input level to all WebSocket clients using main_loop
        try:
            db = 20 * math.log10(max(rms, 1e-8))
            asyncio.run_coroutine_threadsafe(
                broadcast("level", {"db": db, "rms": rms}),
                main_loop
            )
        except Exception:
            pass

    def _on_audio_detected():
        """Fires when startup gate opens: needle dropped, new side starting."""
        # Reset recogniser so it starts fresh for the first track
        if state.recogniser and not (state.learn_session and state.learn_session.active):
            state.recogniser.reset_match()
        if state.learn_session and state.learn_session.active:
            s = state.learn_session
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_audio_detected", {
                    "learned":     s.learned,
                    "track_count": s.track_count,
                    "next_track":  s.next_track_name(),
                }),
                main_loop
            )

    def _on_end_of_side():
        """Fires after END_OF_SIDE_SECS of silence: final track flushed, gate re-armed."""
        # Auto-finalize album recording if active
        if state.album_recorder and state.album_recorder.is_active:
            asyncio.run_coroutine_threadsafe(
                _auto_finalize_album_side(), main_loop)

        if state.learn_session and state.learn_session.active:
            s = state.learn_session
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_end_of_side", {
                    "learned":     s.learned,
                    "track_count": s.track_count,
                    "message":     "End of side detected: last track saved. "
                                   + ("Flip the record and press Continue."
                                      if s.pending_tracks else "All tracks learned!"),
                }),
                main_loop
            )

    state.rec_buffer = rec.RecordingBuffer(
        on_track_ready     = _on_track_ready,
        on_level_update    = _on_level,
        on_audio_detected  = _on_audio_detected,
        on_end_of_side     = _on_end_of_side,
        auto_split         = True,
    )

    # Start recogniser
    state.fp_buffer.clear()
    state.recogniser = cat.Recogniser(
        buffer           = state.fp_buffer,
        on_match         = _make_on_match(main_loop),
        on_unknown       = _make_on_unknown(main_loop),
    )
    state.recogniser.start()

    callback = make_callback(list(audio_streams.values()) + local_streams + bt_streams, state.eq, state.fp_buffer)

    try:
        with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                            channels=_capture_channels(audio_device_index), dtype="float32",
                            blocksize=BLOCK_SIZE, latency=INPUT_LATENCY,
                            callback=callback):
            stop_task    = asyncio.create_task(state.stop_event.wait())
            if confs:
                threads_task = asyncio.create_task(threads_done.wait())
                done, pending = await asyncio.wait(
                    [stop_task, threads_task], return_when=asyncio.FIRST_COMPLETED
                )
            else:
                # Local-only: just wait for stop
                await stop_task
                pending = set()
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if state.recogniser:
            state.recogniser.set_auto_learn_album(None)
            state.recogniser.stop()
            state.recogniser = None
        if state.rec_buffer and state.rec_buffer.is_active:
            state.rec_buffer.stop()
        state.rec_buffer = None
        state.now_playing = None
        for s in audio_streams.values():
            s.stop()
        for lo in local_streams:
            lo.stop()
        for bts in bt_streams:
            bts.stop()
        state.is_streaming = False
        state.active_devices = []
        state.stop_event = None
        await broadcast("status",      {"streaming": False, "message": "Stopped"})
        await broadcast("now_playing", {"track_title": None})


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
    if state.settings.get("auto_stream_enabled"):
        state.auto_stream_task = asyncio.create_task(_auto_stream_watcher())
        print("[auto-stream] Watcher started on boot")
    state.live_mp3.configure(
        state.settings.get("http_stream_enabled", False),
        state.settings.get("http_stream_bitrate_kbps", 256),
    )
    loop = asyncio.get_event_loop()
    state.loop = loop

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
    """Create a new browser audio stream and return its stream_id."""
    stream = BrowserAudioStream()
    _browser_streams[stream.stream_id] = stream
    print(f"[browser-stream] Created stream {stream.stream_id}")
    return {"ok": True, "stream_id": stream.stream_id}


@app.get("/api/stream/{stream_id}")
async def stream_audio(stream_id: str):
    """Stream PCM audio as WAV to browser."""
    stream = _browser_streams.get(stream_id)
    if not stream:
        return JSONResponse({"error": "Stream not found"}, status_code=404)

    async def generate():
        chunks_sent = 0
        try:
            # Send WAV header first
            yield wav_header()
            print(f"[browser-stream] Sent WAV header for {stream_id}")
            empty_polls = 0
            max_empty = 500  # 5 seconds of empty polls before giving up
            # Stream chunks from buffer
            while True:
                if stream._deque:
                    chunk = stream._deque.popleft()
                    empty_polls = 0
                    chunks_sent += 1
                    yield chunk
                elif stream.is_stopped():
                    # Drain remaining
                    while stream._deque:
                        yield stream._deque.popleft()
                        chunks_sent += 1
                    print(f"[browser-stream] Stream {stream_id} stopped by player after {chunks_sent} chunks")
                    break
                else:
                    empty_polls += 1
                    if empty_polls > max_empty:
                        print(f"[browser-stream] Timeout waiting for data on {stream_id} after {chunks_sent} chunks")
                        break
                    await asyncio.sleep(0.01)
        except GeneratorExit:
            print(f"[browser-stream] Client disconnected from {stream_id} after {chunks_sent} chunks")
        except Exception as e:
            print(f"[browser-stream] Error in stream {stream_id}: {e}")
        finally:
            _browser_streams.pop(stream_id, None)
            print(f"[browser-stream] Closed stream {stream_id}")

    return StreamingResponse(generate(), media_type="audio/wav")


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
    state.live_mp3.configure(
        state.settings.get("http_stream_enabled", False),
        state.settings.get("http_stream_bitrate_kbps", 256),
    )
    save_settings(state.settings)
    return {"ok": True}


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

async def _auto_finalize_album_side():
    """Called when RecordingBuffer detects end-of-side silence during album recording.
    Encodes the current side to FLAC and notifies the UI."""
    try:
        await _auto_finalize_album_side_inner()
    except Exception as e:
        print(f"[auto-finalize] ERROR: {e}")
        traceback.print_exc()
        await broadcast("album_recording_status", {
            "recording": False,
            "error": True,
            "message": f"Auto-finalize failed: {e}",
        })


async def _auto_finalize_album_side_inner():
    ar = state.album_recorder
    if not ar or not ar.is_active:
        return

    album_id = ar.album_id
    side = ar.side
    # Keep album_recorder around (but inactive after finish()) so the
    # flip endpoint can read album_id from it.  The audio callback
    # already checks is_active before calling put(), so no PCM is fed.
    _stop_stall_watchdog()

    # Stop learn session for this side
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)

    await broadcast("album_recording_status", {
        "recording": False,
        "album_id": album_id,
        "side": side,
        "message": f"End of Side {side} detected: encoding FLAC…",
    })

    loop = asyncio.get_event_loop()
    path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

    if path:
        file_size = path.stat().st_size
        cat.save_album_audio(album_id, side, str(path), duration, file_size)

        for b in boundaries:
            if b["track_id"] and b["end_secs"] is not None:
                cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

        # Sanity-check boundaries against catalog durations and correct if needed
        cat.correct_side_boundaries(album_id, side, duration)

        # Check if album has more sides to record
        all_tracks = cat.get_album_tracks(album_id)
        album_sides = sorted({t.get("side") or "A" for t in all_tracks})
        current_idx = album_sides.index(side) if side in album_sides else -1
        has_next_side = current_idx >= 0 and current_idx < len(album_sides) - 1
        next_side = album_sides[current_idx + 1] if has_next_side else None

        await broadcast("album_recording_side_saved", {
            "album_id": album_id,
            "side": side,
            "duration_secs": round(duration, 1),
            "size_mb": round(file_size / (1024 * 1024), 1),
            "tracks_captured": len(boundaries),
            "has_next_side": has_next_side,
            "next_side": next_side,
        })

        if has_next_side:
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "message": f"Side {side} saved: {duration:.0f}s, "
                           f"{file_size / (1024*1024):.1f} MB. Flip and record Side {next_side} when ready.",
            })
        else:
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "message": f"Side {side} saved: {duration:.0f}s, "
                           f"{file_size / (1024*1024):.1f} MB. All sides complete!",
            })
            # Last side done: clean up recorder and stop audio stream/recogniser
            state.album_recorder = None
            if state.recogniser:
                state.recogniser.stop()
                state.recogniser = None
            if state.rec_buffer:
                state.rec_buffer.stop()
                state.rec_buffer = None
            await stop_stream()

        print(f"[album-rec] Auto-finalized Side {side}: {duration:.0f}s, "
              f"{file_size / (1024*1024):.1f} MB"
              f"{' (last side)' if not has_next_side else ''}")
    else:
        await broadcast("album_recording_status", {
            "recording": False,
            "message": "Side too short or encoding failed",
        })


async def _encode_and_save_album_side(ar: rec.AlbumRecorder):
    """Finalize a recorded side and persist it in the background."""
    album_id = ar.album_id
    side = ar.side
    loop = asyncio.get_event_loop()

    state.album_encoding.update({
        "in_progress": True,
        "album_id": album_id,
        "side": side,
        "started_at": time.time(),
        "finished_at": None,
        "ok": None,
        "message": f"Encoding Side {side}...",
    })

    try:
        path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

        if not path:
            msg = "Recording too short or encoding failed"
            state.album_encoding.update({
                "in_progress": False,
                "finished_at": time.time(),
                "ok": False,
                "message": msg,
            })
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "error": True,
                "message": msg,
            })
            return

        file_size = path.stat().st_size
        cat.save_album_audio(album_id, side, str(path), duration, file_size)

        for b in boundaries:
            if b["track_id"] and b["end_secs"] is not None:
                cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

        cat.correct_side_boundaries(album_id, side, duration)

        all_tracks = cat.get_album_tracks(album_id)
        album_sides = sorted({t.get("side") or "A" for t in all_tracks})
        current_idx = album_sides.index(side) if side in album_sides else -1
        has_next_side = current_idx >= 0 and current_idx < len(album_sides) - 1
        next_side = album_sides[current_idx + 1] if has_next_side else None

        done_msg = f"Side {side} saved: {duration:.0f}s, {file_size / (1024*1024):.1f} MB"
        state.album_encoding.update({
            "in_progress": False,
            "finished_at": time.time(),
            "ok": True,
            "message": done_msg,
        })

        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "side": side,
            "message": done_msg,
        })
        await broadcast("album_recording_side_saved", {
            "album_id": album_id,
            "side": side,
            "duration_secs": round(duration, 1),
            "size_mb": round(file_size / (1024 * 1024), 1),
            "tracks_captured": len(boundaries),
            "has_next_side": has_next_side,
            "next_side": next_side,
        })
    except Exception as e:
        state.album_encoding.update({
            "in_progress": False,
            "finished_at": time.time(),
            "ok": False,
            "message": f"Encoding failed: {e}",
        })
        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "side": side,
            "error": True,
            "message": f"Encoding failed: {e}",
        })


async def _stream_stall_watchdog():
    """Periodically checks whether the audio stream has stopped delivering data.
    USB audio interfaces can silently die after an input overflow (ALSA/URB errors).
    When detected, flushes accumulated audio and finalizes the recording so the
    user isn't left staring at a frozen timer."""
    POLL_INTERVAL = 3.0  # seconds between checks
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            rb = state.rec_buffer
            if not rb or not rb.is_active:
                continue
            if not rb.stream_stalled:
                continue

            print("[watchdog] Audio stream stall detected: no data for "
                  f"{rec.STREAM_STALL_SECS:.0f}s. Flushing recording.")

            # Force-flush any accumulated audio as the final track
            if rb._total_bytes > 0:
                rb._silence_start_byte = rb._total_bytes
                rb._silence_secs = 0.0
                rb._split_track()

            # Fire end-of-side to finalize album recording
            if rb._on_end_of_side:
                rb._on_end_of_side()

            # Notify UI about the error
            album_id = state.album_recorder.album_id if state.album_recorder else None
            side = state.album_recorder.side if state.album_recorder else None
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "error": True,
                "message": "Audio stream lost (USB/hardware glitch). "
                           "Recording saved with what was captured. "
                           "Try recording this side again.",
            })
            break  # watchdog's job is done for this recording session
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[watchdog] Error: {e}")


def _start_stall_watchdog():
    """Start the audio stream stall watchdog (idempotent)."""
    if state.stall_watchdog_task and not state.stall_watchdog_task.done():
        return  # already running
    state.stall_watchdog_task = asyncio.ensure_future(_stream_stall_watchdog())


def _stop_stall_watchdog():
    """Cancel the stall watchdog."""
    if state.stall_watchdog_task and not state.stall_watchdog_task.done():
        state.stall_watchdog_task.cancel()
    state.stall_watchdog_task = None


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

    # Auto-start audio capture if not already running
    if not _ensure_audio_active():
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
                                             audio_dir=audio_dir)

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
                                             audio_dir=audio_dir)

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

async def _stop_playback():
    """Stop any active catalog playback and clean up AirPlay connections."""
    if state.player:
        state.player.stop()
        state.player = None
    if state.player_task:
        state.player_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.player_task
        state.player_task = None


async def _run_playback(album_id: int, targets: list[dict], volume: int,
                        start_track_id: int | None = None,
                        resume_position_secs: float | None = None):
    """
    Connect to AirPlay devices and run catalog playback.
    Similar to _run_stream_inner but feeds from FLAC files instead of sounddevice.
    """
    main_loop = asyncio.get_event_loop()

    # Build playlist from album_audio records + track timestamps
    audio_files = cat.get_album_audio(album_id)
    if not audio_files:
        await broadcast("error", {"message": "No recorded audio for this album"})
        return

    albums = cat.get_all_albums()
    album_info = next((a for a in albums if a["id"] == album_id), None)
    if not album_info:
        await broadcast("error", {"message": "Album not found"})
        return

    all_tracks = cat.get_album_tracks(album_id)

    playlist = []
    for af in audio_files:
        side = af["side"]
        # Get tracks for this side with timestamps
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

        # Normalize timestamps: shift so first track starts at 0
        # (DB stores offsets from stream start, not FLAC file start)
        if side_tracks:
            first_start = min(
                (t["start_secs"] for t in side_tracks if t["start_secs"] is not None),
                default=0,
            )
            if first_start > 5.0:  # only shift if offset is significant
                print(f"[player] Side {side}: normalizing timestamps, "
                      f"shifting by -{first_start:.1f}s")
                for t in side_tracks:
                    if t["start_secs"] is not None:
                        t["start_secs"] -= first_start
                    if t["end_secs"] is not None:
                        t["end_secs"] -= first_start
        print(f"[player] Side {side}: {len(side_tracks)} tracks, "
              f"audio={af['file_path']}")
        if side_tracks:
            print(f"[player]   first track: {side_tracks[0]['title']} "
                  f"start={side_tracks[0].get('start_secs')} "
                  f"end={side_tracks[0].get('end_secs')}")
        playlist.append(plr.PlaylistEntry(
            audio_path    = af["file_path"],
            side          = side,
            duration_secs = af.get("duration_secs") or 0,
            tracks        = side_tracks,
            album_id      = album_id,
            album_title   = album_info["title"],
            album_artist  = album_info["artist"],
            artwork_path  = album_info.get("user_artwork_path") or album_info.get("artwork_path"),
        ))

    if not playlist:
        await broadcast("error", {"message": "No audio files found"})
        return

    # Create AirPlay metadata object for Now Playing display on devices
    state.airplay_metadata = MediaMetadata(
        title=None, artist=None, album=album_info.get("title"), artwork=None,
    )

    # Separate local / Bluetooth / browser / AirPlay targets
    local_targets     = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    browser_targets   = [t for t in targets if str(t.get("id", "")).startswith("browser:")]
    airplay_targets   = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:", "browser:"))]
    if len(bluetooth_targets) > 1:
        bluetooth_targets = bluetooth_targets[:1]

    # Scan and connect to AirPlay devices. storage= so saved creds attach.
    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs      = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    if not confs and not local_targets and not bluetooth_targets and not browser_targets:
        await broadcast(
            "error", {"message": "No paired devices found on network"}
        )
        state.airplay_metadata = None
        return

    n_devices = (
        len(confs) + len(local_targets)
        + len(bluetooth_targets) + len(browser_targets)
    )
    await broadcast("player_status", {
        "state": "loading",
        "album_id": album_id,
        "message": f"Connecting to {n_devices} device(s)…",
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    local_streams = []

    # Set up local output streams
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            print(f"[player] No ALSA device for {lt.get('id')}, skipping")
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[player] Failed to open local device {alsa_dev}: {e}")

    # Set up Bluetooth output streams
    bt_streams = []
    for bt in bluetooth_targets:
        address = bt.get("address") or bt.get("id", "").replace("bt:", "")
        if not address:
            continue
        alsa_dev = f"bluealsa:DEV={address},PROFILE=a2dp"
        try:
            bts = LocalOutputStream(alsa_dev)
            bts.start()
            bt_streams.append(bts)
        except Exception as e:
            print(f"[player] Failed to open BT device {address}: {e}")

    # Set up browser output streams
    browser_streams = []
    for br in browser_targets:
        stream_id = br.get("id", "").replace("browser:", "")
        if stream_id and stream_id in _browser_streams:
            browser_streams.append(_browser_streams[stream_id])
            print(f"[player] Added browser stream {stream_id}")


    # Always include HTTP MP3 stream if enabled
    all_streams = list(audio_streams.values()) + local_streams + bt_streams + browser_streams
    if state.settings.get("http_stream_enabled", False):
        all_streams.append(state.live_mp3)

    if not all_streams:
        await broadcast("error", {"message": "No output devices available"})
        state.airplay_metadata = None
        return

    active_count  = len(confs)
    threads_done  = asyncio.Event()
    if active_count == 0:
        threads_done.set()  # No AirPlay threads to wait for

    def on_device_done(name, err):
        nonlocal active_count
        if err:
            asyncio.run_coroutine_threadsafe(
                broadcast("error", {"message": f"{name}: {err}"}), main_loop
            )
        active_count -= 1
        if active_count <= 0:
            main_loop.call_soon_threadsafe(threads_done.set)

    for conf in confs:
        threading.Thread(
            target=run_device_stream,
            args=(conf, audio_streams[conf.identifier], volume, on_device_done),
            daemon=True,
        ).start()

    # Player callbacks
    def on_track_change(track_info):
        state.now_playing = track_info
        if state.airplay_metadata is not None:
            state.airplay_metadata.title   = track_info.get("track_title")
            state.airplay_metadata.artist  = (
                track_info.get("track_artist") or track_info.get("album_artist")
            )
            state.airplay_metadata.album   = track_info.get("album_title")
            state.airplay_metadata.artwork = _art_jpeg(track_info)
        asyncio.run_coroutine_threadsafe(broadcast("now_playing", {
            "track_title":  track_info.get("track_title"),
            "track_artist": track_info.get("track_artist"),
            "album_title":  track_info.get("album_title"),
            "album_artist": track_info.get("album_artist"),
            "year":         track_info.get("year"),
            "album_id":     track_info.get("album_id"),
            "track_id":     track_info.get("track_id"),
            "artwork_url":  _art_url(track_info),
            "source":       "player",
        }), main_loop)
        # Log the play
        if track_info.get("track_id") and track_info.get("album_id"):
            cat.log_play(track_info["track_id"], track_info["album_id"])

    def on_status_change(status):
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", status), main_loop
        )

    def on_finished():
        state.now_playing = None
        asyncio.run_coroutine_threadsafe(
            broadcast("now_playing", {"track_title": None}), main_loop
        )
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", {"state": "stopped", "album_id": album_id}),
            main_loop,
        )

    # Create and start the player
    player = plr.Player(
        eq              = state.eq,
        streams         = all_streams,
        on_track_change = on_track_change,
        on_status_change= on_status_change,
        on_finished     = on_finished,
    )
    state.player = player
    player.set_crossfade(state.settings.get("crossfade_secs", 0))

    player.play(album_id, album_info, playlist, start_track_id=start_track_id)

    # If resuming mid-track, seek to exact position
    if resume_position_secs is not None and resume_position_secs > 0:
        player.seek_to(resume_position_secs)
        print(f"[player] Resuming at {resume_position_secs:.1f}s")

    try:
        # Wait for either: player finishes, devices disconnect, or external stop
        while player.state != "stopped":
            if threads_done.is_set() and not local_streams and not bt_streams and not browser_streams:
                # All AirPlay devices disconnected and no local/BT/browser fallback
                player.stop()
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        player.stop()
    finally:
        for s in audio_streams.values():
            s.stop()
        for s in local_streams:
            s.stop()
        for s in bt_streams:
            s.stop()
        for s in browser_streams:
            s.stop()
        state.player = None
        state.player_task = None
        state.now_playing = None
        state.airplay_metadata = None
        await broadcast("player_status", {"state": "stopped"})
        await broadcast("now_playing", {"track_title": None})


async def _run_playback_queue(album_id: int, album_info: dict,
                              playlist: list, targets: list[dict], volume: int):
    """
    Run playback from a pre-built playlist (used by play-queue for multi-album).
    Reuses the same device-connection and player logic as _run_playback.
    """
    main_loop = asyncio.get_event_loop()

    if not playlist:
        await broadcast("error", {"message": "No audio files found"})
        return

    state.airplay_metadata = MediaMetadata(
        title=None, artist=None, album=album_info.get("title"), artwork=None,
    )

    local_targets      = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets  = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    browser_targets    = [t for t in targets if str(t.get("id", "")).startswith("browser:")]
    airplay_targets    = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:", "browser:"))]

    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    if not confs and not local_targets and not bluetooth_targets and not browser_targets:
        await broadcast(
            "error", {"message": "No paired devices found on network"}
        )
        state.airplay_metadata = None
        return

    n_albums = len({e.album_id for e in playlist if e.album_id})
    await broadcast("player_status", {
        "state": "loading",
        "album_id": album_id,
        "message": f"Connecting: {n_albums} album(s) queued…",
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    local_streams = []
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[player] Failed to open local device {alsa_dev}: {e}")

    bt_streams = []
    if bluetooth_targets:
        bt_target = bluetooth_targets[0]  # A2DP: max one BT device
        bt_addr = str(bt_target.get("id", "")).replace("bt:", "")
        if bt_addr:
            try:
                bt_lo = LocalOutputStream(f"bluealsa:DEV={bt_addr},PROFILE=a2dp")
                bt_lo.start()
                bt_streams.append(bt_lo)
                print(f"[player-playlist] Bluetooth stream started → {bt_addr}")
            except Exception as e:
                print(f"[player-playlist] Failed to open BT device {bt_addr}: {e}")

    browser_streams = []
    for br in browser_targets:
        stream_id = br.get("id", "").replace("browser:", "")
        if stream_id and stream_id in _browser_streams:
            browser_streams.append(_browser_streams[stream_id])
            print(f"[player-playlist] Added browser stream {stream_id}")

    all_streams = list(audio_streams.values()) + local_streams + bt_streams + browser_streams
    if not all_streams:
        await broadcast("error", {"message": "No output devices available"})
        state.airplay_metadata = None
        return

    active_count = len(confs)
    threads_done = asyncio.Event()
    if active_count == 0:
        threads_done.set()

    def on_device_done(name, err):
        nonlocal active_count
        if err:
            asyncio.run_coroutine_threadsafe(
                broadcast("error", {"message": f"{name}: {err}"}), main_loop)
        active_count -= 1
        if active_count <= 0:
            main_loop.call_soon_threadsafe(threads_done.set)

    for conf in confs:
        threading.Thread(
            target=run_device_stream,
            args=(conf, audio_streams[conf.identifier], volume, on_device_done),
            daemon=True,
        ).start()

    def on_track_change(track_info):
        state.now_playing = track_info
        if state.airplay_metadata is not None:
            state.airplay_metadata.title = track_info.get("track_title")
            state.airplay_metadata.artist = (
                track_info.get("track_artist") or track_info.get("album_artist"))
            state.airplay_metadata.album = track_info.get("album_title")
            state.airplay_metadata.artwork = _art_jpeg(track_info)
        asyncio.run_coroutine_threadsafe(broadcast("now_playing", {
            "track_title":  track_info.get("track_title"),
            "track_artist": track_info.get("track_artist"),
            "album_title":  track_info.get("album_title"),
            "album_artist": track_info.get("album_artist"),
            "year":         track_info.get("year"),
            "album_id":     track_info.get("album_id"),
            "track_id":     track_info.get("track_id"),
            "artwork_url":  _art_url(track_info),
            "source":       "player",
        }), main_loop)
        if track_info.get("track_id") and track_info.get("album_id"):
            cat.log_play(track_info["track_id"], track_info["album_id"])

    def on_status_change(status):
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", status), main_loop)

    def on_finished():
        state.now_playing = None
        asyncio.run_coroutine_threadsafe(
            broadcast("now_playing", {"track_title": None}), main_loop)
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", {"state": "stopped"}), main_loop)

    player = plr.Player(
        eq=state.eq, streams=all_streams,
        on_track_change=on_track_change,
        on_status_change=on_status_change,
        on_finished=on_finished,
    )
    state.player = player
    player.set_crossfade(state.settings.get("crossfade_secs", 0))
    player.play(album_id, album_info, playlist)

    try:
        while player.state != "stopped":
            if threads_done.is_set() and not local_streams and not bt_streams and not browser_streams:
                player.stop()
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        player.stop()
    finally:
        for s in audio_streams.values():
            s.stop()
        for s in local_streams:
            s.stop()
        for s in bt_streams:
            s.stop()
        for s in browser_streams:
            s.stop()
        state.player = None
        state.player_task = None
        state.now_playing = None
        state.airplay_metadata = None
        await broadcast("player_status", {"state": "stopped"})
        await broadcast("now_playing", {"track_title": None})


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


def _build_side_entry(aid: int, side: str, album_info: dict) -> plr.PlaylistEntry | None:
    """Build a PlaylistEntry for one specific side of an album."""
    audio_files = cat.get_album_audio(aid)
    af = next((f for f in audio_files if f["side"] == side), None)
    if not af:
        return None
    all_tracks = cat.get_album_tracks(aid)
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
    return plr.PlaylistEntry(
        audio_path=af["file_path"],
        side=side,
        duration_secs=af.get("duration_secs"),
        tracks=side_tracks,
        album_id=aid,
        album_title=album_info["title"],
        album_artist=album_info["artist"],
        artwork_path=album_info.get("user_artwork_path") or album_info.get("artwork_path"),
    )


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


# ── Learn Session ─────────────────────────────────────────────────────────────

class LearnSession:
    """
    Orchestrates hands-off fingerprint learning for a full album side.

    Flow:
      1. User picks album + how many tracks to learn
      2. Silence detection automatically captures each track
      3. Each captured track is fingerprinted and sliced into windows
      4. Tracks assigned sequentially to album's unlearned track list
      5. When count reached → broadcasts 'learn_paused' so UI can ask
         "Flip record / next side?" or "Done"
    """

    def __init__(self, album_id: int, track_count: int, loop, side: str | None = None):
        self.album_id    = album_id
        self.track_count = track_count   # how many tracks to learn this session
        self.learned     = 0             # tracks learned so far this session
        self.active      = True
        self._loop       = loop

        # Get the ordered list of unlearned tracks for this album,
        # filtered to the current side if specified (so recording Side A
        # doesn't accidentally learn Side B tracks).
        all_tracks = cat.get_album_tracks(album_id)
        if side:
            all_tracks = [t for t in all_tracks if (t.get("side") or "A") == side]
        db = cat.get_db()
        self.pending_tracks = [
            t for t in all_tracks
            if not db.execute(
                "SELECT 1 FROM fingerprints WHERE track_id = ?", (t["id"],)
            ).fetchone()
        ]
        db.close()
        print(f"[learn] Session started: album {album_id} side {side or 'all'}, "
              f"{track_count} tracks to learn, "
              f"{len(self.pending_tracks)} unlearned tracks available")

    def next_track_id(self) -> int | None:
        """Return the next unlearned track id, or None if all done."""
        if self.pending_tracks:
            return self.pending_tracks[0]["id"]
        return None

    def next_track_name(self) -> str:
        if self.pending_tracks:
            t = self.pending_tracks[0]
            return f"{t.get('side','')}{t.get('track_number','')}: {t['title']}"
        return "Unknown"

    def on_track_captured(self, pcm: bytes):
        """Called when a complete track's PCM is ready. Fingerprints and saves it."""
        if pcm is None:
            return

        import io
        import wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(pcm)
        wav = buf.getvalue()

        result = cat.fingerprint_wav(wav)
        if not result:
            print("[learn] Fingerprinting failed for captured track: skipping")
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_update", {
                    "learned": self.learned,
                    "track_count": self.track_count,
                    "status": "warning",
                    "message": "Fingerprinting failed: was audio too quiet? Skipping track.",
                }),
                self._loop
            )
            return

        raw_ints, _compressed, duration = result
        track_id = self.next_track_id()

        if track_id is None:
            print("[learn] No more unlearned tracks: stopping session")
            self.active = False
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_done", {"learned": self.learned, "message": "All tracks already learned!"}),
                self._loop
            )
            return

        rows = cat.save_track_fingerprints(track_id, raw_ints, duration)

        # Capture the name of the track we just learned BEFORE advancing the pointer
        just_learned_name = self.next_track_name()

        self.pending_tracks.pop(0)
        self.learned += 1

        # Notify UI that fingerprint was saved: triggers track list refresh
        # (separate from the track boundary notification which fires before FP is saved)
        if state.album_recorder or self.album_id:
            asyncio.run_coroutine_threadsafe(
                broadcast("album_recording_status", {
                    "recording": True,
                    "album_id": self.album_id,
                    "message": f"\u23fa Learned {just_learned_name}",
                }),
                self._loop
            )

        track_name = self.next_track_name() if self.pending_tracks else ":"
        print(f"[learn] ✓ Track learned ({self.learned}/{self.track_count}): "
              f"{rows} fingerprint windows saved")

        if self.learned >= self.track_count:
            # Session target reached: pause for user confirmation
            self.active = False
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_paused", {
                    "learned": self.learned,
                    "track_count": self.track_count,
                    "remaining_in_album": len(self.pending_tracks),
                    "message": (
                        f"Learned {self.learned} tracks. "
                        + (
                            "Flip the record or swap to the next."
                            if self.pending_tracks
                            else "All tracks learned!"
                        )
                    ),
                }),
                self._loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_update", {
                    "learned":       self.learned,
                    "track_count":   self.track_count,
                    "learned_track": just_learned_name,
                    "next_track":    track_name,
                    "message":       f"Learned track {self.learned} of {self.track_count}. Listening for next…",
                }),
                self._loop
            )


# ── Learn Routes ──────────────────────────────────────────────────────────────

# ── Audio-only listen mode ────────────────────────────────────────────────────

async def _start_listen_mode():
    """Open sounddevice input without AirPlay streaming: for learning/recording only."""
    if state.is_streaming or state.listen_task:
        return  # already running
    audio_device_index = int(state.settings.get("audio_device_index") or 0)
    loop = asyncio.get_event_loop()

    def _on_track_ready(pcm, dur):
        if state.learn_session and state.learn_session.active:
            state.learn_executor.submit(state.learn_session.on_track_captured, pcm)

    def _on_level(rms):
        db = 20 * np.log10(rms + 1e-9)
        asyncio.run_coroutine_threadsafe(
            broadcast("level", {"rms": round(rms, 5), "db": round(db, 1)}), loop)

    def _on_audio_detected():
        asyncio.run_coroutine_threadsafe(broadcast("audio_detected", {}), loop)

    def _on_end_of_side():
        # Auto-finalize album recording if active
        if state.album_recorder and state.album_recorder.is_active:
            asyncio.run_coroutine_threadsafe(
                _auto_finalize_album_side(), loop)

        if state.learn_session:
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_end_of_side", {
                    "learned": state.learn_session.learned,
                    "track_count": state.learn_session.track_count,
                    "message": "End of side: flip record and press Continue.",
                }), loop)

    state.rec_buffer = rec.RecordingBuffer(
        on_track_ready    = _on_track_ready,
        on_level_update   = _on_level,
        on_audio_detected = _on_audio_detected,
        on_end_of_side    = _on_end_of_side,
        auto_split        = True,
    )
    state.fp_buffer.clear()
    state.recogniser = cat.Recogniser(
        buffer           = state.fp_buffer,
        on_match         = _make_on_match(loop),
        on_unknown       = _make_on_unknown(loop),
    )
    state.recogniser.start()
    stop_event = asyncio.Event()
    state.stop_event = stop_event

    async def _run():
        callback = make_callback({}, state.eq, state.fp_buffer)
        try:
            with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                                channels=_capture_channels(audio_device_index), dtype="float32",
                                blocksize=BLOCK_SIZE, latency=INPUT_LATENCY,
                                callback=callback):
                print("[listen] Audio-only mode started")
                await broadcast("status", {"streaming": False, "listening": True,
                                           "message": "Listening (no AirPlay)"})
                await stop_event.wait()
        except Exception as e:
            print(f"[listen] ERROR: {e}")
            traceback.print_exc()
        finally:
            if state.recogniser:
                state.recogniser.stop()
                state.recogniser = None
            if state.rec_buffer and state.rec_buffer.is_active:
                state.rec_buffer.stop()
            state.rec_buffer  = None
            state.stop_event  = None
            state.listen_task = None
            print("[listen] Audio-only mode stopped")
            await broadcast("status", {"streaming": False, "listening": False, "message": "Stopped"})

    state.listen_task = asyncio.create_task(_run())


def _stop_listen_mode():
    if state.stop_event and state.listen_task:
        state.stop_event.set()


def _ensure_audio_active() -> bool:
    return state.is_streaming or (state.listen_task is not None)


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
