#!/usr/bin/env python3
"""
Vinyl AirPlay Streamer — Web-controlled backend
16-bit / 44.1kHz lossless PCM with live bass/treble EQ + record recognition
"""

import asyncio
import collections
import json
import math
import struct
import threading
import concurrent.futures
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
import pyatv
from pyatv.interface import MediaMetadata
import sounddevice as sd
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

import catalog as cat
import recorder as rec

# ── Audio Config ──────────────────────────────────────────────────────────────

SAMPLE_RATE      = 44100
CHANNELS         = 2    # processing/output channels (stereo)
CAPTURE_CHANNELS = 4    # Scarlett 2i2 4th Gen presents as 4-ch to ALSA
BITS          = 16
BLOCK_SIZE    = 4096
READ_SIZE     = 8192
MAX_CHUNKS    = 500

# ── Paths & Settings ──────────────────────────────────────────────────────────

SETTINGS_FILE = Path("settings.json")
TEMPLATES     = Jinja2Templates(directory="templates")


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        s = json.loads(SETTINGS_FILE.read_text())
        # Backward-compat: older versions stored this as 'audd_key'
        if 'acoustid_key' not in s and 'audd_key' in s:
            s['acoustid_key'] = s.get('audd_key')
        return s
    return {"saved_devices": [], "volume": 80, "audio_device_index": None,
            "bass": 0, "treble": 0, "acoustid_key": "", "acoustid_enabled": False, "discogs_token": "", "hidden_devices": [], "auto_stream_enabled": False, "auto_stream_device": None}


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ── Biquad Shelf EQ ───────────────────────────────────────────────────────────

def _shelf_coeffs(freq, gain_db, shelf_type, fs=SAMPLE_RATE, Q=0.707):
    A      = 10 ** (gain_db / 40.0)
    w0     = 2 * math.pi * freq / fs
    alpha  = math.sin(w0) / (2 * Q)
    cos_w0 = math.cos(w0)
    if shelf_type == 'low':
        b0 =    A*((A+1)-(A-1)*cos_w0+2*math.sqrt(A)*alpha)
        b1 =  2*A*((A-1)-(A+1)*cos_w0)
        b2 =    A*((A+1)-(A-1)*cos_w0-2*math.sqrt(A)*alpha)
        a0 =      (A+1) +(A-1)*cos_w0+2*math.sqrt(A)*alpha
        a1 =  -2 *((A-1)+(A+1)*cos_w0)
        a2 =      (A+1) +(A-1)*cos_w0-2*math.sqrt(A)*alpha
    else:
        b0 =    A*((A+1)+(A-1)*cos_w0+2*math.sqrt(A)*alpha)
        b1 = -2*A*((A-1)+(A+1)*cos_w0)
        b2 =    A*((A+1)+(A-1)*cos_w0-2*math.sqrt(A)*alpha)
        a0 =      (A+1) -(A-1)*cos_w0+2*math.sqrt(A)*alpha
        a1 =   2 *((A-1)-(A+1)*cos_w0)
        a2 =      (A+1) -(A-1)*cos_w0-2*math.sqrt(A)*alpha
    b = np.array([b0,b1,b2], dtype=np.float64)/a0
    a = np.array([a0,a1,a2], dtype=np.float64)/a0
    return b, a


def _apply_biquad(x, b, a, z):
    out = np.empty_like(x)
    b0,b1,b2 = b; a1,a2 = a[1],a[2]
    for c in range(x.shape[1]):
        z0,z1 = z[0,c],z[1,c]
        for i in range(x.shape[0]):
            s=x[i,c]; y=b0*s+z0
            z0=b1*s-a1*y+z1; z1=b2*s-a2*y; out[i,c]=y
        z[0,c],z[1,c]=z0,z1
    return out


class EQ:
    BASS_FREQ=250; TREBLE_FREQ=8000

    def __init__(self, bass_db=0.0, treble_db=0.0, volume=80):
        self._lock=threading.Lock()
        self._bass_db=bass_db; self._treble_db=treble_db
        self._volume=int(np.clip(volume,0,100))
        self._z_bass=np.zeros((2,CHANNELS)); self._z_treble=np.zeros((2,CHANNELS))
        self._update_coeffs()

    def _update_coeffs(self):
        self._b_bass,self._a_bass=_shelf_coeffs(self.BASS_FREQ,self._bass_db,'low')
        self._b_treble,self._a_treble=_shelf_coeffs(self.TREBLE_FREQ,self._treble_db,'high')

    def set_eq(self, bass_db, treble_db):
        with self._lock:
            if bass_db!=self._bass_db or treble_db!=self._treble_db:
                self._bass_db=float(np.clip(bass_db,-12,12))
                self._treble_db=float(np.clip(treble_db,-12,12))
                self._update_coeffs()
                self._z_bass=np.zeros((2,CHANNELS)); self._z_treble=np.zeros((2,CHANNELS))

    def set_volume(self, volume):
        with self._lock:
            self._volume=int(np.clip(volume,0,100))

    @property
    def values(self):
        return self._bass_db, self._treble_db, self._volume

    def process(self, x):
        with self._lock:
            gain=self._volume/100.0
            flat=(self._bass_db==0.0 and self._treble_db==0.0)
            if gain==1.0 and flat: return x
            x64=x.astype(np.float64)*gain
            if not flat:
                x64=_apply_biquad(x64,self._b_bass,self._a_bass,self._z_bass)
                x64=_apply_biquad(x64,self._b_treble,self._a_treble,self._z_treble)
            return np.clip(x64,-1.0,1.0).astype(np.float32)


# ── Global State ──────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.settings            = load_settings()
        self.is_streaming        = False
        self.active_devices      = []
        self.available_devices   = []
        self.audio_devices       = []
        self.stream_task: Optional[asyncio.Task] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.ws_clients          = []
        self.eq = EQ(
            bass_db   = self.settings.get("bass",   0),
            treble_db = self.settings.get("treble", 0),
            volume    = self.settings.get("volume", 80),
        )
        self.fp_buffer              = cat.FingerprintBuffer()
        self.recogniser: Optional[cat.Recogniser] = None
        self.now_playing: Optional[dict] = None
        self.rec_buffer: Optional[rec.RecordingBuffer] = None
        self.rec_level: float = 0.0          # current RMS for UI meter
        self.learn_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fpcalc")
        self.auto_stream_task: Optional[asyncio.Task] = None
        self.manual_stop_until: float = 0.0  # monotonic — auto-stream suppressed after manual stop
        self.pairing_sessions: dict = {}  # device_id → active pyatv pairing object
        self.listen_task: Optional[asyncio.Task] = None  # audio-only (no AirPlay) task
        self.rec_pending: list = []          # finished recordings awaiting save
        self.rec_album_id: Optional[int] = None  # manually chosen album for tagging
        self.learn_session: Optional["LearnSession"] = None
        self.album_recorder: Optional[rec.AlbumRecorder] = None  # full-side capture
        self.airplay_metadata = None  # MediaMetadata for now-playing display


state = AppState()


# ── WebSocket Broadcast ───────────────────────────────────────────────────────

async def broadcast(event: str, data: dict = {}):
    msg  = json.dumps({"event": event, **data})
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.remove(ws)


# ── WAV Header ────────────────────────────────────────────────────────────────

def wav_header() -> bytes:
    byte_rate=SAMPLE_RATE*CHANNELS*(BITS//8); block_align=CHANNELS*(BITS//8)
    data_size=0x7FFFFFFF
    h  = struct.pack('<4sI4s', b'RIFF', data_size+36, b'WAVE')
    h += struct.pack('<4sIHHIIHH', b'fmt ',16,1,CHANNELS,SAMPLE_RATE,byte_rate,block_align,BITS)
    h += struct.pack('<4sI', b'data', data_size)
    return h


# ── Async Audio Stream ────────────────────────────────────────────────────────

class AsyncAudioStream:
    def __init__(self):
        self._deque=collections.deque(); self._event=threading.Event()
        self._buf=wav_header(); self._stop=threading.Event()

    def put(self, chunk):
        if not self._stop.is_set():
            if len(self._deque)<MAX_CHUNKS: self._deque.append(chunk)
            self._event.set()

    def stop(self):
        self._stop.set(); self._event.set()

    def readable(self): return True
    def seekable(self): return False

    async def read(self, size=READ_SIZE):
        loop=asyncio.get_event_loop()
        while len(self._buf)<size:
            if self._stop.is_set() and not self._deque: break
            if not self._deque:
                self._event.clear()
                if not self._deque and not self._stop.is_set():
                    await loop.run_in_executor(None, lambda: self._event.wait(timeout=0.5))
            while self._deque: self._buf+=self._deque.popleft()
        out=self._buf[:size]; self._buf=self._buf[size:]; return out


# ── Per-Device Stream Thread ──────────────────────────────────────────────────

def run_device_stream(conf, audio_stream, volume, done_callback):
    loop=asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    async def _stream():
        print(f"[airplay] Connecting to {conf.name} ({conf.address})…")
        # Load credentials from whichever .pyatv.conf exists.
        # Service runs as root but pairing was done as listen user, so check both.
        atv=await pyatv.connect(conf, loop)
        try:
            print(f"[airplay] Connected — setting volume {volume}")
            await atv.audio.set_volume(volume)
            print(f"[airplay] Streaming to {conf.name}")
            import traceback
            try:
                await atv.stream.stream_file(audio_stream)
                print(f"[airplay] stream_file returned normally for {conf.name}")
            except Exception as sf_err:
                print(f"[airplay] stream_file EXCEPTION for {conf.name}: "
                      f"{type(sf_err).__name__}: {sf_err}")
                traceback.print_exc()
                raise
        finally: atv.close()
    err=None
    try: loop.run_until_complete(_stream())
    except Exception as e:
        print(f"[airplay] ERROR for {conf.name}: {type(e).__name__}: {e}")
        err=e
    finally: loop.close()
    done_callback(conf.name, err)


# ── Audio Callback ────────────────────────────────────────────────────────────

def make_callback(streams, eq, fp_buffer):
    def callback(indata, frames, time, status):
        if status: print(f"[audio] {status}")
        # Scarlett 2i2 4th Gen captures 4 channels — use first 2 (L+R inputs)
        audio_in = np.ascontiguousarray(indata[:, :CHANNELS]) if indata.shape[1] > CHANNELS else indata
        # Feed fingerprint buffer BEFORE EQ/volume — raw signal gives best results
        raw_pcm = (audio_in * 32767).astype(np.int16).tobytes()
        fp_buffer.put(raw_pcm)
        # Feed recorder with raw pre-EQ audio (preserves dynamics)
        # Always call put() — silence detection runs inside regardless of is_active,
        # so inter-track gaps trigger recogniser reset even when not recording
        if state.rec_buffer:
            state.rec_buffer.put(raw_pcm)
        # Feed album recorder (full-side capture) with raw pre-EQ audio
        if state.album_recorder and state.album_recorder.is_active:
            state.album_recorder.put(raw_pcm)
        # Apply EQ + volume for the actual stream output
        audio = eq.process(audio_in)
        pcm   = (audio*32767).astype(np.int16).tobytes()
        for s in streams: s.put(pcm)
    return callback


# ── Recognition Callbacks ─────────────────────────────────────────────────────

def _art_url(track: dict) -> Optional[str]:
    art = track.get("user_artwork_path") or track.get("artwork_path")
    return f"/artwork/{Path(art).name}" if art else None


def _art_jpeg(track: dict) -> Optional[bytes]:
    """Load artwork JPEG bytes from disk for AirPlay metadata."""
    art = track.get("user_artwork_path") or track.get("artwork_path")
    if not art:
        return None
    try:
        p = Path(art)
        if not p.is_absolute():
            p = cat.ARTWORK_DIR / p
        return p.read_bytes() if p.exists() else None
    except Exception:
        return None


def _make_on_match(loop):
    def on_match(track):
        state.now_playing = track
        # Update shared MediaMetadata in-place — RAOP sender picks up changes live
        if state.airplay_metadata is not None:
            state.airplay_metadata.title   = track.get("track_title")
            state.airplay_metadata.artist  = (
                track.get("track_artist") or track.get("album_artist")
            )
            state.airplay_metadata.album   = track.get("album_title")
            state.airplay_metadata.artwork = _art_jpeg(track)
        asyncio.run_coroutine_threadsafe(broadcast("now_playing", {
            "track_title":  track.get("track_title"),
            "track_artist": track.get("track_artist"),
            "album_title":  track.get("album_title"),
            "album_artist": track.get("album_artist"),
            "year":         track.get("year"),
            "side":         track.get("side"),
            "track_number": track.get("track_number"),
            "album_id":     track.get("album_id"),
            "track_id":     track.get("track_id"),
            "artwork_url":  _art_url(track),
        }), loop)
    return on_match


def _make_on_unknown(loop):
    def on_unknown():
        state.now_playing = None
        asyncio.run_coroutine_threadsafe(
            broadcast("now_playing", {"track_title": None}), loop
        )
    return on_unknown


# ── Main Stream Coordinator ───────────────────────────────────────────────────

# ── Auto-Stream Watcher ───────────────────────────────────────────────────────

async def _auto_stream_watcher():
    """
    Poll Scarlett RMS while idle; auto-start stream when record plays.

    Opens the InputStream only when NOT streaming, and closes it the moment
    streaming starts — this prevents ALSA 'device busy' errors when run_stream
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

            # While streaming, just count down cooldown — don't touch the device
            if state.is_streaming:
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            # Also skip while listen mode or album recording has the device open
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            if cooldown > 0:
                cooldown = max(0.0, cooldown - POLL_SECS)
                continue

            # Open device, read one chunk, close immediately — never holds it open
            # Re-check right before open (race condition: listen/album may have started)
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown = COOLDOWN_SECS
                continue
            audio_idx = state.settings.get("audio_device_index")
            try:
                with sd.InputStream(device=audio_idx, samplerate=44100,
                                    channels=CAPTURE_CHANNELS, dtype="float32",
                                    blocksize=POLL_FRAMES) as stream:
                    data, _ = stream.read(POLL_FRAMES)
                rms = float(np.sqrt(np.mean(data[:, :2] ** 2)))
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
                        print("[auto-stream] Suppressed — manual stop cooldown active")
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
        try: await state.auto_stream_task
        except (asyncio.CancelledError, Exception): pass
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
        import traceback
        print(f"[airplay] run_stream crashed: {e}")
        traceback.print_exc()
        await broadcast("error", {"message": f"Stream error: {e}"})
        state.is_streaming = False; state.active_devices = []
        state.stop_event = None; state.airplay_metadata = None
        await broadcast("status", {"streaming": False, "message": "Stopped (error)"})


async def _run_stream_inner(targets, audio_device_index, volume):
    state.stop_event     = asyncio.Event()
    main_loop            = asyncio.get_event_loop()

    # Create a shared mutable MediaMetadata object — passed once to stream_file
    # and updated in-place via on_match() whenever a new track is identified
    np = state.now_playing or {}
    state.airplay_metadata = MediaMetadata(
        title  = np.get("track_title"),
        artist = np.get("track_artist") or np.get("album_artist"),
        album  = np.get("album_title"),
        artwork= _art_jpeg(np) if np else None,
    )

    # Load atvremote saved credentials (~/.pyatv.conf) so Apple TV accepts our connection
    found      = await pyatv.scan(main_loop, timeout=7,
                                  protocol=pyatv.Protocol.AirPlay)
    id_to_conf = {d.identifier: d for d in found}
    confs      = [id_to_conf[t["id"]] for t in targets if t["id"] in id_to_conf]

    if not confs:
        await broadcast("error", {"message": "No paired devices found on network"})
        state.stop_event=None
        return

    # Only mark streaming=True now that we have confirmed devices
    state.is_streaming   = True
    state.active_devices = [d["name"] for d in targets]
    await broadcast("status", {
        "streaming": True, "devices": state.active_devices,
        "message": f"Streaming to {len(targets)} device(s)"
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    active_count  = len(confs)
    threads_done  = asyncio.Event()

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
        """Called by RecordingBuffer when silence gap detected — new track starting."""
        # Always reset recogniser on track boundary, even when not recording
        if state.recogniser and not (state.learn_session and state.learn_session.active):
            state.recogniser.reset_match()
        if pcm:
            asyncio.run_coroutine_threadsafe(
                _save_and_broadcast_recording(pcm), main_loop
            )

    def _on_level(rms):
        state.rec_level = rms

    def _on_audio_detected():
        """Fires when startup gate opens — needle dropped, new side starting."""
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
        """Fires after END_OF_SIDE_SECS of silence — final track flushed, gate re-armed."""
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
                    "message":     "End of side detected — last track saved. "
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
        api_key          = state.settings.get("acoustid_key") or state.settings.get("audd_key") or None,
        acoustid_enabled = state.settings.get("acoustid_enabled", False),
    )
    state.recogniser.start()

    callback = make_callback(list(audio_streams.values()), state.eq, state.fp_buffer)

    try:
        with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                            channels=CAPTURE_CHANNELS, dtype="float32",
                            blocksize=BLOCK_SIZE, callback=callback):
            stop_task    = asyncio.create_task(state.stop_event.wait())
            threads_task = asyncio.create_task(threads_done.wait())
            done, pending = await asyncio.wait(
                [stop_task, threads_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending: t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if state.recogniser:
            state.recogniser.set_auto_learn_album(None)
            state.recogniser.stop(); state.recogniser=None
        if state.rec_buffer and state.rec_buffer.is_active:
            # Flush any in-progress recording when stream stops
            pcm = state.rec_buffer.stop()
            if pcm:
                await _save_and_broadcast_recording(pcm)
        state.rec_buffer = None
        state.now_playing=None
        for s in audio_streams.values(): s.stop()
        state.is_streaming=False; state.active_devices=[]; state.stop_event=None
        await broadcast("status",      {"streaming": False, "message": "Stopped"})
        await broadcast("now_playing", {"track_title": None})


# ── App Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cat.init_db()
    devices = sd.query_devices()
    state.audio_devices = [
        {"index": i, "name": d["name"]}
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]
    if state.settings.get("auto_stream_enabled"):
        state.auto_stream_task = asyncio.create_task(_auto_stream_watcher())
        print("[auto-stream] Watcher started on boot")
    yield
    if state.stop_event: state.stop_event.set()
    if state.auto_stream_task and not state.auto_stream_task.done():
        state.auto_stream_task.cancel()
        try: await state.auto_stream_task
        except (asyncio.CancelledError, Exception): pass
    if state.stream_task:
        state.stream_task.cancel()
        try: await state.stream_task
        except (asyncio.CancelledError, Exception): pass


app = FastAPI(lifespan=lifespan)


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
    return TEMPLATES.TemplateResponse("index.html", {"request": request})


@app.get("/api/scan")
async def scan_devices():
    found = await pyatv.scan(asyncio.get_event_loop(), timeout=7,
                              protocol=pyatv.Protocol.AirPlay)
    hidden = set(state.settings.get("hidden_devices", []))
    # Load stored credentials to check which devices are already paired
    import os
    _creds_path = next((p for p in ["/root/.pyatv.conf", "/home/listen/.pyatv.conf"]
                        if os.path.exists(p)), None)
    _creds_data = {}
    if _creds_path:
        try:
            import json as _json
            _raw = _json.loads(open(_creds_path).read())
            # Build set of identifiers that have RAOP or AirPlay credentials
            for _entry in _raw if isinstance(_raw, list) else _raw.get("devices", []):
                _protos = _entry.get("protocols", {})
                _has = any(_protos.get(p, {}).get("credentials") for p in ("raop", "airplay"))
                if _has:
                    for _ident in _entry.get("identifiers", []):
                        _creds_data[_ident] = True
        except Exception:
            pass

    state.available_devices = []
    for d in found:
        raop    = d.get_service(pyatv.Protocol.RAOP)
        needs   = raop and str(getattr(raop, "pairing", "")).endswith("Mandatory")
        paired  = (not needs) or any(_creds_data.get(i) for i in [d.identifier] + list(d.all_identifiers))
        state.available_devices.append({
            "id":       d.identifier,
            "name":     d.name,
            "address":  str(d.address),
            "hidden":   d.identifier in hidden,
            "needs_pairing": bool(needs),
            "paired":   paired,
        })
    return {"devices": state.available_devices}


@app.post("/api/devices/{device_id}/pair/start")
async def pair_start(device_id: str, body: dict = {}):
    """
    Begin pairing with a device. Returns whether a PIN is needed.
    If the device shows a PIN on screen, the client should prompt the user to
    enter it and call /pair/pin. If no PIN is needed, pairing completes immediately.
    Pairs RAOP protocol first (required for audio), then AirPlay.
    """
    protocol_name = body.get("protocol", "raop")
    proto_map = {
        "raop":    pyatv.Protocol.RAOP,
        "airplay": pyatv.Protocol.AirPlay,
    }
    protocol = proto_map.get(protocol_name, pyatv.Protocol.RAOP)

    loop = asyncio.get_event_loop()
    found = await pyatv.scan(loop, timeout=7, identifier=device_id,
                              protocol=pyatv.Protocol.AirPlay)
    if not found:
        return {"ok": False, "error": "Device not found on network"}
    conf = found[0]

    try:
        pairing = await pyatv.pair(conf, protocol, loop)
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
async def pair_pin(device_id: str, body: dict = {}):
    """Submit PIN and finish pairing. Then save credentials to .pyatv.conf."""
    session = state.pairing_sessions.get(device_id)
    if not session:
        return {"ok": False, "error": "No active pairing session — call /pair/start first"}

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
        return {"ok": False, "error": "Pairing did not succeed — wrong PIN?"}

    state.pairing_sessions.pop(device_id, None)

    # Save credentials to both locations so they persist across restarts
    import json as _json, os
    for creds_path in ["/root/.pyatv.conf", "/home/listen/.pyatv.conf"]:
        try:
            from pyatv.storage.file_storage import FileStorage
            try:
                storage = FileStorage(creds_path, asyncio.get_event_loop())
            except TypeError:
                storage = FileStorage(creds_path)
            await storage.load()
            storage.save_device(conf)
            await storage.flush()
            print(f"[pair] Credentials saved to {creds_path}")
        except Exception as e:
            print(f"[pair] Could not save to {creds_path}: {e}")

    protocol_name = session["protocol"]
    # Check if more protocols need pairing
    raop    = conf.get_service(pyatv.Protocol.RAOP)
    airplay = conf.get_service(pyatv.Protocol.AirPlay)
    remaining = []
    if protocol_name == "raop" and airplay and str(getattr(airplay, "pairing", "")).endswith("Mandatory") and not airplay.credentials:
        remaining.append("airplay")

    return {
        "ok":       True,
        "paired":   True,
        "remaining_protocols": remaining,
        "message":  f"Paired successfully via {protocol_name.upper()}"
                    + (f" — also pair: {', '.join(remaining).upper()}" if remaining else ""),
    }


@app.post("/api/devices/{device_id}/pair/cancel")
async def pair_cancel(device_id: str):
    """Cancel an in-progress pairing session."""
    session = state.pairing_sessions.pop(device_id, None)
    if session:
        try:
            await session["pairing"].finish()
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/devices/{device_id}/hide")
async def toggle_device_hidden(device_id: str, body: dict = {}):
    """Toggle a device's hidden state. Persists to settings.json."""
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


@app.get("/api/audio-devices")
async def audio_devices():
    return {"devices": state.audio_devices}


@app.get("/api/status")
async def get_status():
    bass, treble, volume = state.eq.values
    return {
        "streaming":      state.is_streaming,
        "active_devices": state.active_devices,
        "settings":       state.settings,
        "audio_devices":  state.audio_devices,
        "eq":             {"bass": bass, "treble": treble, "volume": volume},
        "now_playing":    state.now_playing,
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
        try: await state.stream_task
        except (asyncio.CancelledError, Exception): pass
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


@app.post("/api/volume")
async def set_volume(body: dict):
    volume = int(body.get("volume", 80))
    state.eq.set_volume(volume)
    state.settings["volume"] = volume
    save_settings(state.settings)
    return {"ok": True, "volume": volume}


@app.post("/api/eq")
async def set_eq(body: dict):
    bass   = float(body.get("bass",   state.settings.get("bass",   0)))
    treble = float(body.get("treble", state.settings.get("treble", 0)))
    state.eq.set_eq(bass, treble)
    state.settings["bass"]=bass; state.settings["treble"]=treble
    save_settings(state.settings)
    return {"ok": True, "bass": bass, "treble": treble}


@app.post("/api/settings")
async def update_settings(body: dict):
    # Prefer 'acoustid_key' (new), but accept legacy 'audd_key' for older UIs.
    if "auto_stream_enabled" in body:
        state.settings["auto_stream_enabled"] = bool(body["auto_stream_enabled"])
        save_settings(state.settings)
        asyncio.create_task(_restart_auto_stream_watcher())
    if "auto_stream_device" in body:
        state.settings["auto_stream_device"] = body["auto_stream_device"]
        save_settings(state.settings)
    if "discogs_token" in body:
        state.settings["discogs_token"] = str(body["discogs_token"])
        _save_settings()

    if "acoustid_enabled" in body:
        state.settings["acoustid_enabled"] = bool(body["acoustid_enabled"])
        if state.recogniser:
            state.recogniser.set_acoustid_enabled(state.settings["acoustid_enabled"])
    if "acoustid_key" in body or "audd_key" in body:
        key = body.get("acoustid_key")
        if key is None:
            key = body.get("audd_key")
        state.settings["acoustid_key"] = key
        # Keep legacy field too so older code paths still work if present
        state.settings["audd_key"] = key
        if state.recogniser:
            state.recogniser.set_api_key(key)
    save_settings(state.settings)
    return {"ok": True}


# ── Catalog Routes ────────────────────────────────────────────────────────────

@app.get("/api/catalog")
async def get_catalog():
    return {"albums": cat.get_all_albums()}


@app.get("/api/catalog/history")
async def get_history():
    return {"plays": cat.get_recent_plays()}


@app.get("/api/catalog/{album_id}/tracks")
async def get_tracks(album_id: int):
    return {"tracks": cat.get_album_tracks(album_id)}


@app.post("/api/catalog/{album_id}/artwork")
async def upload_artwork(album_id: int, file: UploadFile = File(...)):
    data = await file.read()
    path = cat.save_user_artwork(data, album_id)
    if not path:
        return {"ok": False, "error": "Failed to save image"}
    cat.update_album_artwork(album_id, path, user=True)
    return {"ok": True, "artwork_url": f"/artwork/{Path(path).name}"}


@app.post("/api/catalog/manual")
async def manual_entry(body: dict):
    track = cat.save_manual_track(body)
    if not track:
        return {"ok": False, "error": "Failed to save"}
    return {"ok": True, "track": track}



@app.get("/api/catalog/search")
async def search_catalog(artist: str = "", album: str = ""):
    """Search MusicBrainz for releases matching artist + album name."""
    if not artist and not album:
        return {"releases": []}
    loop = asyncio.get_event_loop()
    releases = await loop.run_in_executor(
        None, lambda: cat.search_musicbrainz(artist, album)
    )
    return {"releases": releases}


@app.get("/api/catalog/search/discogs")
async def search_discogs(artist: str = "", album: str = ""):
    token = state.settings.get("discogs_token", "")
    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: cat.search_discogs(artist, album, token=token)
    )
    return {"releases": results}


@app.get("/api/catalog/release/discogs/{discogs_id}")
async def discogs_release(discogs_id: str):
    token = state.settings.get("discogs_token", "")
    data = await asyncio.get_event_loop().run_in_executor(
        None, lambda: cat.get_discogs_release(discogs_id, token=token)
    )
    return data


@app.get("/api/catalog/release/{mb_release_id}")
async def get_release(mb_release_id: str):
    """Fetch full track listing for a MusicBrainz release ID."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(
        None, lambda: cat.get_release_tracks(mb_release_id)
    )
    if not data:
        return {"ok": False, "error": "Release not found"}
    return {"ok": True, "release": data}


@app.post("/api/catalog/release")
async def save_release(body: dict):
    """Save a MusicBrainz release to the catalog."""
    release_data = body.get("release", {})
    if not release_data:
        return {"ok": False, "error": "No release data"}
    loop = asyncio.get_event_loop()
    album_id = await loop.run_in_executor(
        None, lambda: cat.save_release_to_catalog(release_data)
    )
    if album_id is None:
        return {"ok": False, "error": "Failed to save"}
    # Try to fetch artwork
    mb_id = release_data.get("mb_release_id")
    if mb_id:
        art = await loop.run_in_executor(
            None, lambda: cat.fetch_artwork(mb_id, album_id)
        )
        if art:
            cat.update_album_artwork(album_id, art, user=False)
    return {"ok": True, "album_id": album_id}


@app.post("/api/catalog/{album_id}/learn")
async def learn_album(album_id: int):
    """
    Fingerprint the currently buffered audio and associate it with this album.
    Call while the record is playing. Works even when not streaming to AirPlay —
    as long as the service is running and audio is coming in.
    """
    if not state.is_streaming:
        return {"ok": False, "error": "Not currently streaming — start streaming first, then try again"}

    if not state.fp_buffer.ready():
        return {"ok": False, "error": "Not enough audio buffered yet — wait 30 seconds and try again"}

    wav = state.fp_buffer.get_wav()
    if not wav:
        return {"ok": False, "error": "Audio buffer empty"}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: cat.fingerprint_wav(wav))
    if not result:
        return {"ok": False, "error": "Fingerprinting failed — is audio coming through the Scarlett?"}

    raw_ints, _compressed, duration = result
    ok = await loop.run_in_executor(
        None, lambda: cat.save_fingerprint_for_album(album_id, raw_ints, duration)
    )
    if not ok:
        return {"ok": False, "error": "Failed to save fingerprint — does this album have any tracks?"}

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
        msg = f"Learned one track. {unlearned} track(s) still need learning — press again as each new track plays."

    return {"ok": True, "message": msg}

@app.delete("/api/catalog/{album_id}/fingerprints")
async def clear_album_fingerprints(album_id: int):
    """Clear all learned fingerprints for every track in an album."""
    deleted = cat.clear_album_fingerprints(album_id)
    return {"ok": True, "deleted": deleted,
            "message": f"Cleared {deleted} fingerprints — album is unlearned"}


@app.delete("/api/catalog/track/{track_id}/fingerprints")
async def clear_track_fingerprints(track_id: int):
    """Clear all learned fingerprints for a single track."""
    deleted = cat.clear_track_fingerprints(track_id)
    return {"ok": True, "deleted": deleted,
            "message": f"Cleared {deleted} fingerprints for this track"}


@app.post("/api/catalog/fingerprints/rebuild-cache")
async def rebuild_fingerprint_cache():
    """Force a complete rebuild of the in-memory fingerprint cache."""
    cat.force_refresh_fingerprint_cache()
    return {"ok": True}


@app.post("/api/catalog/{album_id}/reorder")
async def reorder_tracks(album_id: int, body: dict):
    """Save new track order. body: { track_ids: [id, id, ...] in desired order }"""
    track_ids = body.get("track_ids", [])
    if not track_ids:
        return {"ok": False, "error": "No track IDs provided"}
    ok = cat.reorder_album_tracks(track_ids)
    return {"ok": ok}


@app.delete("/api/catalog/{album_id}")
async def delete_album_route(album_id: int):
    cat.delete_album(album_id)
    return {"ok": True}


@app.get("/api/test-acoustid")
async def test_acoustid():
    """Quick sanity-check: verifies the AcoustID client key is accepted."""
    import requests as req_lib
    key = (state.settings.get("acoustid_key") or state.settings.get("audd_key") or "").strip()
    if not key:
        return {"ok": False, "error": "No AcoustID client key saved — go to Settings and paste your key"}

    try:
        # We intentionally send a tiny/dummy fingerprint. If the key is invalid, AcoustID will
        # respond with an auth-style error. If the key is valid, we should *not* get an 'invalid key'
        # error (even though the lookup itself won't match anything).
        resp = req_lib.post(
            "https://api.acoustid.org/v2/lookup",
            data={"client": key, "meta": "recordingids", "duration": "1", "fingerprint": "AQAA", "format": "json"},
            timeout=8,
        )
        data = resp.json()
        if data.get("status") == "error":
            err = data.get("error")
            if isinstance(err, dict):
                err = json.dumps(err)
            msg = str(err or "").lower()
            if "invalid" in msg and ("key" in msg or "client" in msg):
                return {"ok": False, "error": "Invalid AcoustID client key"}
            return {"ok": True, "message": "AcoustID client key looks valid (server accepted the request) ✓"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/now-playing")
async def now_playing():
    if not state.now_playing:
        return {"track_title": None}
    return {**state.now_playing, "artwork_url": _art_url(state.now_playing)}



async def _save_and_broadcast_recording(pcm: bytes):
    """Encode PCM to MP3 using catalog metadata for the current track, broadcast when done."""
    np_data  = state.now_playing or {}

    # If user manually selected an album, use that album's metadata
    # (falls back to Now Playing, then to unknowns)
    if state.rec_album_id:
        albums = cat.get_all_albums()
        album  = next((a for a in albums if a["id"] == state.rec_album_id), None)
        if album:
            np_data = {
                "track_title":   np_data.get("track_title", "Unknown Track"),
                "track_artist":  album.get("artist", "Unknown Artist"),
                "album_artist":  album.get("artist", "Unknown Artist"),
                "album_title":   album.get("title", "Unknown Album"),
                "year":          album.get("year"),
                "track_number":  np_data.get("track_number", ""),
                "genre":         album.get("genre", ""),
                "artwork_path":      album.get("artwork_path"),
                "user_artwork_path": album.get("user_artwork_path"),
            }

    metadata = {
        "title":            np_data.get("track_title", "Unknown Track"),
        "artist":           np_data.get("track_artist") or np_data.get("album_artist", "Unknown Artist"),
        "album_artist":     np_data.get("album_artist", "Unknown Artist"),
        "album":            np_data.get("album_title", "Unknown Album"),
        "year":             np_data.get("year"),
        "track_number":     np_data.get("track_number", ""),
        "genre":            np_data.get("genre", ""),
        "artwork_path":     np_data.get("artwork_path"),
        "user_artwork_path":np_data.get("user_artwork_path"),
    }
    loop     = asyncio.get_event_loop()
    out_path = await loop.run_in_executor(None, lambda: rec.save_recording(pcm, metadata))
    if out_path:
        await broadcast("recording_saved", {
            "filename": out_path.name,
            "size_mb":  round(out_path.stat().st_size / (1024*1024), 1),
        })


# ── Recording Routes ─────────────────────────────────────────────────────────







@app.get("/api/recordings")
async def list_recordings():
    return {"recordings": rec.list_recordings()}

@app.get("/api/recordings/status")
async def recording_status():
    active  = bool(state.rec_buffer and state.rec_buffer.is_active)
    elapsed = state.rec_buffer.elapsed_secs if active and state.rec_buffer else 0
    return {
        "recording": active,
        "elapsed_secs": round(elapsed, 1),
        "level": round(state.rec_level, 4),
    }

@app.get("/api/recordings/zip/all")
async def download_all_recordings():
    """Download all recordings as a ZIP file."""
    import zipfile, io as _io
    files = list(rec.RECORDINGS_DIR.glob("*.mp3"))
    if not files:
        return HTMLResponse("No recordings", 404)
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for f in files:
            zf.write(f, f.name)
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=vinyl_recordings.zip"}
    )

@app.post("/api/recordings/start")
async def start_recording(body: dict = {}):
    # Auto-start audio capture if not already running
    if not _ensure_audio_active():
        await _start_listen_mode()
        await asyncio.sleep(0.5)  # let stream open
    if not state.rec_buffer:
        return {"ok": False, "error": "Audio device not ready — check input device in settings"}
    auto = body.get("auto_split", True)
    state.rec_album_id = body.get("album_id")  # None = use Now Playing
    state.rec_buffer.start(auto_split=auto)
    await broadcast("recording_status", {"recording": True, "auto_split": auto})
    album_name = None
    if state.rec_album_id:
        albums = cat.get_all_albums()
        album  = next((a for a in albums if a["id"] == state.rec_album_id), None)
        if album: album_name = f"{album['artist']} — {album['title']}"
    return {"ok": True, "tagging_as": album_name or "Now Playing (auto)"}

@app.post("/api/recordings/stop")
async def stop_recording():
    if not state.rec_buffer or not state.rec_buffer.is_active:
        return {"ok": False, "error": "Not recording"}
    pcm = state.rec_buffer.stop()
    state.rec_album_id = None
    await broadcast("recording_status", {"recording": False})
    if pcm:
        await _save_and_broadcast_recording(pcm)
        return {"ok": True, "message": "Track saved"}
    return {"ok": True, "message": "Track too short — discarded"}

@app.delete("/api/recordings/{filename}")
async def delete_recording(filename: str):
    ok = rec.delete_recording(filename)
    return {"ok": ok}

@app.get("/api/recordings/{filename}")
async def download_recording(filename: str):
    from fastapi.responses import FileResponse
    path = rec.RECORDINGS_DIR / Path(filename).name
    if not path.exists():
        return HTMLResponse("Not found", 404)
    return FileResponse(str(path), media_type="audio/mpeg",
                        filename=path.name)


# ── Album Recording (Full-Side Capture) ──────────────────────────────────────

def _drain_learn_session():
    """Safely stop the learn session, allowing any pending fingerprint work to finish.

    Sets state.learn_session = None immediately (prevents new submissions),
    then queues deactivation through the learn_executor so it runs AFTER
    any already-submitted on_track_captured calls complete.
    """
    session = state.learn_session
    state.learn_session = None          # block new submissions
    if session:
        # Queue deactivation AFTER pending fingerprints
        def _deactivate():
            session.active = False
            print("[learn] Session ended (drained)")
        if hasattr(state, 'learn_executor') and state.learn_executor:
            state.learn_executor.submit(_deactivate)
        else:
            session.active = False
    if state.recogniser:
        state.recogniser.set_learning_mode(False)


async def _auto_finalize_album_side():
    """Called when RecordingBuffer detects end-of-side silence during album recording.
    Encodes the current side to FLAC and notifies the UI."""
    ar = state.album_recorder
    if not ar or not ar.is_active:
        return

    album_id = ar.album_id
    side = ar.side
    state.album_recorder = None  # detach so no more PCM is fed

    # Stop learn session — drain executor so last track gets fingerprinted
    _drain_learn_session()

    await broadcast("album_recording_status", {
        "recording": False,
        "album_id": album_id,
        "side": side,
        "message": f"End of Side {side} detected — encoding FLAC…",
    })

    loop = asyncio.get_event_loop()
    path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

    if path:
        file_size = path.stat().st_size
        cat.save_album_audio(album_id, side, str(path), duration, file_size)

        for b in boundaries:
            if b["track_id"] and b["end_secs"] is not None:
                cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

        await broadcast("album_recording_side_saved", {
            "album_id": album_id,
            "side": side,
            "duration_secs": round(duration, 1),
            "size_mb": round(file_size / (1024 * 1024), 1),
            "tracks_captured": len(boundaries),
        })
        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "side": side,
            "message": f"✓ Side {side} saved — {duration:.0f}s, "
                       f"{file_size / (1024*1024):.1f} MB. Flip and record next side when ready.",
        })
        print(f"[album-rec] Auto-finalized Side {side}: {duration:.0f}s, "
              f"{file_size / (1024*1024):.1f} MB")
    else:
        await broadcast("album_recording_status", {
            "recording": False,
            "message": "Side too short or encoding failed",
        })


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
        return {"ok": False, "error": "Album recording already in progress — stop it first"}

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

    # Create the album recorder
    state.album_recorder = rec.AlbumRecorder(album_id, side, album_info)

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

    # Also start a learn session so fingerprints get learned automatically
    # (reuses existing learn infrastructure)
    if not state.learn_session and state.rec_buffer and _ensure_audio_active():
        loop = asyncio.get_event_loop()
        session = LearnSession(album_id, len(side_tracks), loop, also_record=False)
        if session.pending_tracks:
            state.learn_session = session
            session._rec_buffer = state.rec_buffer
            if state.recogniser:
                state.recogniser.set_learning_mode(True)

            def _on_learn_track_ready(pcm, dur):
                if state.learn_session and state.learn_session.active:
                    state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
                # Also mark track boundary in album recorder
                if state.album_recorder and state.album_recorder.is_active:
                    next_id = state.learn_session.next_track_id() if state.learn_session else None
                    state.album_recorder.mark_track_boundary(next_id)

            state.rec_buffer._on_track_ready = _on_learn_track_ready
            state.rec_buffer.expected_track_secs = session.next_track_expected_secs()
            state.rec_buffer.remaining_tracks = len(session.pending_tracks)
            state.rec_buffer.start(auto_split=True)

    await broadcast("album_recording_status", {
        "recording": True,
        "album_id": album_id,
        "side": side,
        "album_name": f"{album['artist']} — {album['title']}",
        "side_tracks": len(side_tracks),
        "message": f"Recording Side {side} — drop the needle when ready",
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
    if not state.album_recorder or not state.album_recorder.is_active:
        return {"ok": False, "error": "No album recording in progress"}

    # Finish current side
    album_id = state.album_recorder.album_id
    loop = asyncio.get_event_loop()

    # Stop learn session — drain executor so last track gets fingerprinted
    _drain_learn_session()
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()

    # Encode the current side in background
    ar = state.album_recorder
    state.album_recorder = None

    async def _finish_and_start_next():
        # Encode current side
        path, duration, boundaries = await loop.run_in_executor(None, ar.finish)
        if path:
            file_size = path.stat().st_size
            cat.save_album_audio(album_id, ar.side, str(path), duration, file_size)

            # Save track timestamps
            for b in boundaries:
                if b["track_id"] and b["end_secs"] is not None:
                    cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

            await broadcast("album_recording_side_saved", {
                "album_id": album_id,
                "side": ar.side,
                "duration_secs": round(duration, 1),
                "size_mb": round(file_size / (1024 * 1024), 1),
                "tracks_captured": len(boundaries),
            })

    asyncio.create_task(_finish_and_start_next())

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

    state.album_recorder = rec.AlbumRecorder(album_id, new_side, album_info)

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

    # Restart learn session for new side
    if state.rec_buffer and (state.is_streaming or state.listen_task):
        session = LearnSession(album_id, len(side_tracks), loop, also_record=False)
        if session.pending_tracks:
            state.learn_session = session
            session._rec_buffer = state.rec_buffer
            if state.recogniser:
                state.recogniser.set_learning_mode(True)

            def _on_learn_track_ready(pcm, dur):
                if state.learn_session and state.learn_session.active:
                    state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
                if state.album_recorder and state.album_recorder.is_active:
                    next_id = state.learn_session.next_track_id() if state.learn_session else None
                    state.album_recorder.mark_track_boundary(next_id)

            state.rec_buffer._on_track_ready = _on_learn_track_ready
            state.rec_buffer.expected_track_secs = session.next_track_expected_secs()
            state.rec_buffer.remaining_tracks = len(session.pending_tracks)
            state.rec_buffer.start(auto_split=True)

    await broadcast("album_recording_status", {
        "recording": True,
        "album_id": album_id,
        "side": new_side,
        "album_name": f"{album['artist']} — {album['title']}",
        "side_tracks": len(side_tracks),
        "message": f"Recording Side {new_side} — flip the record and drop the needle",
    })

    return {"ok": True, "side": new_side, "side_tracks": len(side_tracks)}


@app.post("/api/album-recording/stop")
async def album_recording_stop():
    """Stop the current album recording, encode to FLAC, and save."""
    if not state.album_recorder:
        return {"ok": False, "error": "No album recording in progress"}

    ar = state.album_recorder
    state.album_recorder = None
    album_id = ar.album_id

    # Stop learn session — drain executor so last track gets fingerprinted
    _drain_learn_session()
    if state.rec_buffer and state.rec_buffer.is_active:
        state.rec_buffer.stop()

    loop = asyncio.get_event_loop()
    path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

    if not path:
        await broadcast("album_recording_status", {
            "recording": False,
            "message": "Recording too short or encoding failed",
        })
        return {"ok": False, "error": "Recording too short or encoding failed"}

    file_size = path.stat().st_size
    cat.save_album_audio(album_id, ar.side, str(path), duration, file_size)

    # Save track timestamps
    for b in boundaries:
        if b["track_id"] and b["end_secs"] is not None:
            cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

    await broadcast("album_recording_status", {
        "recording": False,
        "album_id": album_id,
        "message": f"Side {ar.side} saved — {duration:.0f}s, "
                   f"{file_size / (1024*1024):.1f} MB",
    })
    await broadcast("album_recording_side_saved", {
        "album_id": album_id,
        "side": ar.side,
        "duration_secs": round(duration, 1),
        "size_mb": round(file_size / (1024 * 1024), 1),
        "tracks_captured": len(boundaries),
    })

    return {
        "ok": True,
        "side": ar.side,
        "duration_secs": round(duration, 1),
        "size_mb": round(file_size / (1024 * 1024), 1),
        "tracks_captured": len(boundaries),
        "file_path": str(path),
    }


@app.get("/api/album-recording/status")
async def album_recording_status():
    """Get current album recording status."""
    if not state.album_recorder or not state.album_recorder.is_active:
        return {"recording": False}
    ar = state.album_recorder
    return {
        "recording": True,
        "album_id": ar.album_id,
        "side": ar.side,
        "elapsed_secs": round(ar.elapsed_secs, 1),
        "tracks_captured": ar.track_count,
    }


@app.post("/api/album-recording/cancel")
async def album_recording_cancel():
    """Cancel the current album recording without saving."""
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


# ── Album Audio Serving ──────────────────────────────────────────────────────

@app.get("/api/album-audio/{album_id}")
async def get_album_audio(album_id: int):
    """List all recorded audio files for an album."""
    audio = cat.get_album_audio(album_id)
    return {"audio": audio}


@app.get("/api/album-audio/{album_id}/play/{audio_id}")
async def play_album_audio(album_id: int, audio_id: int, request: Request):
    """
    Serve a recorded album audio file (FLAC) for playback.
    Supports HTTP Range requests for seeking.
    """
    from starlette.responses import Response
    audio = cat.get_album_audio_by_id(audio_id)
    if not audio or audio["album_id"] != album_id:
        return HTMLResponse("Not found", 404)
    path = Path(audio["file_path"])
    if not path.exists():
        return HTMLResponse("File missing", 404)
    return FileResponse(str(path), media_type="audio/flac", filename=path.name)


@app.delete("/api/album-audio/{album_id}")
async def delete_album_audio_route(album_id: int):
    """Delete all recorded audio for an album."""
    count = cat.delete_album_audio(album_id)
    return {"ok": True, "deleted": count}


@app.delete("/api/album-audio/{album_id}/{audio_id}")
async def delete_album_audio_single(album_id: int, audio_id: int):
    """Delete a single recorded audio file (one side)."""
    ok = cat.delete_album_audio_by_id(audio_id)
    if not ok:
        return {"ok": False, "error": "Audio not found"}
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

    def __init__(self, album_id: int, track_count: int, loop,
                 also_record: bool = False):
        self.album_id    = album_id
        self.track_count = track_count   # how many tracks to learn this session
        self.learned     = 0             # tracks learned so far this session
        self.active      = True
        self.also_record = also_record   # also save each track as MP3
        self._loop       = loop
        self._rec_buffer = None          # set externally to update expected_track_secs

        # Get the ordered list of unlearned tracks for this album
        all_tracks = cat.get_album_tracks(album_id)
        db = cat.get_db()
        self.pending_tracks = [
            t for t in all_tracks
            if not db.execute(
                "SELECT 1 FROM fingerprints WHERE track_id = ?", (t["id"],)
            ).fetchone()
        ]
        db.close()
        print(f"[learn] Session started: album {album_id}, "
              f"{track_count} tracks to learn, "
              f"{len(self.pending_tracks)} unlearned tracks available")

    def next_track_id(self) -> Optional[int]:
        """Return the next unlearned track id, or None if all done."""
        if self.pending_tracks:
            return self.pending_tracks[0]["id"]
        return None

    def next_track_name(self) -> str:
        if self.pending_tracks:
            t = self.pending_tracks[0]
            return f"{t.get('side','')}{t.get('track_number','')} — {t['title']}"
        return "Unknown"

    def next_track_expected_secs(self) -> float:
        """Return the expected duration (from catalog metadata) of the next pending track, or 0."""
        if self.pending_tracks:
            return float(self.pending_tracks[0].get("duration_secs") or 0)
        return 0.0

    def on_track_captured(self, pcm: bytes):
        """Called when a complete track's PCM is ready. Fingerprints and saves it."""
        if not self.active:
            return

        import io, wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(pcm)
        wav = buf.getvalue()

        result = cat.fingerprint_wav(wav)
        if not result:
            print("[learn] Fingerprinting failed for captured track — skipping")
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_update", {
                    "learned": self.learned,
                    "track_count": self.track_count,
                    "status": "warning",
                    "message": "Fingerprinting failed — was audio too quiet? Skipping track.",
                }),
                self._loop
            )
            return

        raw_ints, _compressed, duration = result
        track_id = self.next_track_id()

        if track_id is None:
            print("[learn] No more unlearned tracks — stopping session")
            self.active = False
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_done", {"learned": self.learned, "message": "All tracks already learned!"}),
                self._loop
            )
            return

        rows = cat.save_track_fingerprints(track_id, raw_ints, duration)

        # Save the actual recorded duration to the track (backfills missing Discogs data)
        cat.update_track_duration(track_id, duration)

        # Capture the name of the track we just learned BEFORE advancing the pointer
        just_learned_name = self.next_track_name()

        # Optionally save as MP3
        if self.also_record:
            try:
                _db = cat.get_db()
                track_info = cat._get_track_full(_db, track_id) or {}
                _db.close()
                # _get_track_full does a JOIN so it returns all album fields directly
                # Map to the keys expected by make_filename / tag_mp3
                metadata = {
                    "title":             track_info.get("track_title", "Unknown Track"),
                    "artist":            track_info.get("track_artist") or track_info.get("album_artist", "Unknown"),
                    "album_artist":      track_info.get("album_artist", "Unknown Artist"),
                    "album":             track_info.get("album_title", "Unknown Album"),
                    "year":              track_info.get("year"),
                    "track_number":      track_info.get("track_number", ""),
                    "genre":             track_info.get("genre", ""),
                    "artwork_path":      track_info.get("artwork_path"),
                    "user_artwork_path": track_info.get("user_artwork_path"),
                }
                out_path = rec.save_recording(pcm, metadata)
                if out_path:
                    asyncio.run_coroutine_threadsafe(
                        broadcast("recording_saved", {
                            "filename": out_path.name,
                            "size_mb":  round(out_path.stat().st_size / (1024*1024), 1),
                        }),
                        self._loop
                    )
            except Exception as e:
                print(f"[learn] MP3 save failed: {e}")

        self.pending_tracks.pop(0)
        self.learned += 1

        # Update expected duration for the next track on the RecordingBuffer
        if self._rec_buffer:
            self._rec_buffer.expected_track_secs = self.next_track_expected_secs()
            self._rec_buffer.remaining_tracks = len(self.pending_tracks)

        track_name = self.next_track_name() if self.pending_tracks else "—"
        print(f"[learn] ✓ Track learned ({self.learned}/{self.track_count}): "
              f"{rows} fingerprint windows saved")

        if self.learned >= self.track_count:
            # Session target reached — pause for user confirmation
            self.active = False
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_paused", {
                    "learned": self.learned,
                    "track_count": self.track_count,
                    "remaining_in_album": len(self.pending_tracks),
                    "message": f"Learned {self.learned} tracks. "
                               f"{'Flip the record or swap to the next.' if self.pending_tracks else 'All tracks learned!'}"
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
    """Open sounddevice input without AirPlay streaming — for learning/recording only."""
    if state.is_streaming or state.listen_task:
        return  # already running
    audio_device_index = int(state.settings.get("audio_device", 0))
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
                    "message": "End of side — flip record and press Continue.",
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
        api_key          = state.settings.get("acoustid_key") or None,
        acoustid_enabled = state.settings.get("acoustid_enabled", False),
    )
    state.recogniser.start()
    stop_event = asyncio.Event()
    state.stop_event = stop_event

    async def _run():
        callback = make_callback({}, state.eq, state.fp_buffer)
        try:
            with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                                channels=CAPTURE_CHANNELS, dtype="float32",
                                blocksize=BLOCK_SIZE, callback=callback):
                print("[listen] Audio-only mode started")
                await broadcast("status", {"streaming": False, "listening": True,
                                           "message": "Listening (no AirPlay)"})
                await stop_event.wait()
        except Exception as e:
            print(f"[listen] ERROR: {e}")
            import traceback; traceback.print_exc()
        finally:
            if state.recogniser:
                state.recogniser.stop(); state.recogniser = None
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
    """Start a learn session — auto-starts audio capture if not already streaming."""
    """
    Start a learn session.
    body: { album_id: int, track_count: int }
    """
    if not state.is_streaming:
        return {"ok": False, "error": "Not streaming — start streaming first"}
    if not state.rec_buffer:
        return {"ok": False, "error": "Recorder not ready"}

    album_id    = body.get("album_id")
    track_count = body.get("track_count", 1)

    if not album_id:
        return {"ok": False, "error": "album_id required"}

    # Wire learn session as the track-ready callback
    loop = asyncio.get_event_loop()
    also_record = body.get("also_record", False)
    session = LearnSession(album_id, track_count, loop, also_record=also_record)

    if not session.pending_tracks:
        return {"ok": False, "error": "All tracks for this album are already learned!"}

    state.learn_session = session
    session._rec_buffer = state.rec_buffer

    if state.recogniser:
        state.recogniser.set_learning_mode(True)
    # Start capture buffer in auto-split mode
    # Override the on_track_ready callback to route to learn session
    def _on_learn_track_ready(pcm, dur):
        """Run fpcalc in background thread so it never blocks the audio callback."""
        if state.learn_session and state.learn_session.active:
            state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
    state.rec_buffer._on_track_ready = _on_learn_track_ready
    state.rec_buffer.expected_track_secs = session.next_track_expected_secs()
    state.rec_buffer.remaining_tracks = len(session.pending_tracks)
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
    body: { track_count: int }  — how many more tracks to do
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
    state.rec_buffer.expected_track_secs = session.next_track_expected_secs()
    state.rec_buffer.remaining_tracks = len(session.pending_tracks)
    state.rec_buffer.start(auto_split=True)  # restart capture

    first_track = session.next_track_name()
    await broadcast("learn_update", {
        "learned": session.learned,
        "track_count": session.learned + track_count,
        "next_track": first_track,
        "message": f"Continuing — waiting for audio… drop the needle when ready",
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
    if state.now_playing:
        await ws.send_text(json.dumps({
            "event": "now_playing",
            **state.now_playing,
            "artwork_url": _art_url(state.now_playing),
        }))
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect:
        if ws in state.ws_clients: state.ws_clients.remove(ws)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
