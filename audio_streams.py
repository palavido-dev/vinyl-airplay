#!/usr/bin/env python3
"""Vinyl AirPlay: real-time audio output sinks + the capture callback.

The live audio path: WAV framing, the AirPlay/local/browser output sinks, and
make_callback (the per-buffer hot loop that feeds fingerprinting, recording, EQ,
and every output sink). Audio constants are local (matching audio_eq/audio_mp3).
Shares AppState via app_state.
"""

import asyncio
import collections
import struct
import subprocess
import threading
import time
import traceback
import uuid
from contextlib import suppress

import numpy as np
import pyatv

from app_state import broadcast, state

SAMPLE_RATE = 44100
CHANNELS    = 2
BITS        = 16
MAX_CHUNKS  = 500
READ_SIZE   = 8192


# ── WAV Header ────────────────────────────────────────────────────────────────

def wav_header() -> bytes:
    byte_rate = SAMPLE_RATE * CHANNELS * (BITS // 8)
    block_align = CHANNELS * (BITS // 8)
    data_size = 0x7FFFFFFF
    h  = struct.pack('<4sI4s', b'RIFF', data_size + 36, b'WAVE')
    h += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, CHANNELS, SAMPLE_RATE, byte_rate, block_align, BITS)
    h += struct.pack('<4sI', b'data', data_size)
    return h


# ── Async Audio Stream ────────────────────────────────────────────────────────

class AsyncAudioStream:
    def __init__(self):
        self._deque = collections.deque()
        self._event = threading.Event()
        self._buf = wav_header()
        self._stop = threading.Event()

    def put(self, chunk):
        if not self._stop.is_set():
            if len(self._deque) < MAX_CHUNKS:
                self._deque.append(chunk)
            self._event.set()

    def stop(self):
        self._stop.set()
        self._event.set()

    def readable(self):
        return True

    def seekable(self):
        return False

    async def read(self, size=READ_SIZE):
        loop = asyncio.get_event_loop()
        while len(self._buf) < size:
            if self._stop.is_set() and not self._deque:
                break
            if not self._deque:
                self._event.clear()
                if not self._deque and not self._stop.is_set():
                    await loop.run_in_executor(None, lambda: self._event.wait(timeout=0.5))
            while self._deque:
                self._buf += self._deque.popleft()
        out = self._buf[:size]
        self._buf = self._buf[size:]
        return out


# ── Per-Device Stream Thread ──────────────────────────────────────────────────

def run_device_stream(conf, audio_stream, volume, done_callback):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def _stream():
        max_retries=3
        retry_count=0
        base_delay=1.0

        while retry_count <= max_retries:
            try:
                print(f"[airplay] Connecting to {conf.name} ({conf.address})…")
                atv = await pyatv.connect(conf, loop)
                try:
                    print(f"[airplay] Connected: setting volume {volume}")
                    await atv.audio.set_volume(volume)
                    print(f"[airplay] Streaming to {conf.name}")
                    try:
                        await atv.stream.stream_file(audio_stream)
                        print(f"[airplay] stream_file returned normally for {conf.name}")
                    except Exception as sf_err:
                        print(f"[airplay] stream_file EXCEPTION for {conf.name}: "
                              f"{type(sf_err).__name__}: {sf_err}")
                        traceback.print_exc()
                        raise
                finally:
                    atv.close()
                break
            except Exception as e:
                retry_count += 1
                if retry_count <= max_retries:
                    delay = base_delay * (2 ** (retry_count - 1))
                    print(
                        f"[airplay] Connection lost to {conf.name}, "
                        f"retrying in {delay}s (attempt {retry_count}/{max_retries})"
                    )
                    if state.loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            broadcast("reconnecting", {"device": conf.name, "attempt": retry_count}),
                            state.loop,
                        )
                    await asyncio.sleep(delay)
                else:
                    print(f"[airplay] Final ERROR for {conf.name}: {type(e).__name__}: {e}")
                    raise

    err = None
    try:
        loop.run_until_complete(_stream())
    except Exception as e:
        print(f"[airplay] ERROR for {conf.name}: {type(e).__name__}: {e}")
        err = e
    finally:
        loop.close()
    done_callback(conf.name, err)


# ── Local Audio Output ────────────────────────────────────────────────────────

class LocalOutputStream:
    """Plays PCM to a local ALSA device via aplay subprocess.
    Same put()/stop() interface as AsyncAudioStream.
    Uses aplay pipe instead of sounddevice to avoid buffer underrun clicks."""

    def __init__(self, alsa_device, samplerate=SAMPLE_RATE, channels=CHANNELS):
        self._alsa_device = alsa_device  # e.g. "plughw:2,0" or "touchscreen"
        self._samplerate = samplerate
        self._channels = channels
        self._proc = None
        self._retry_count = 0
        self._max_retries = 1

    def start(self):
        # -B 500000: a 500ms ALSA buffer. The default is small enough that CPU
        # or IO spikes (recording encode, SD-card writes) cause audible
        # underruns on some sinks, reported as choppy HDMI audio in #57. Music
        # playback tolerates the extra latency.
        self._proc = subprocess.Popen(
            ["aplay", "-D", self._alsa_device, "-f", "S16_LE",
             "-r", str(self._samplerate), "-c", str(self._channels),
             "-B", "500000", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Pre-fill with ~30ms of silence so the ALSA device settles
        # before real audio arrives (prevents open-device pop)
        try:
            silence = b'\x00' * (int(self._samplerate * 0.03) * self._channels * 2)
            self._proc.stdin.write(silence)
        except Exception:
            pass
        print(f"[local-out] Opened aplay pipe to {self._alsa_device}")

    def put(self, pcm_bytes):
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(pcm_bytes)
            except (BrokenPipeError, OSError) as e:
                print(f"[local-out] Write error: {e}")
                if self._retry_count<self._max_retries:
                    self._retry_count+=1
                    print(f"[local-out] Attempting restart ({self._retry_count}/{self._max_retries})")
                    time.sleep(2)
                    try:
                        self.stop()
                        self.start()
                        self._proc.stdin.write(pcm_bytes)
                    except Exception as restart_err:
                        print(f"[local-out] Restart failed: {restart_err}")

    def stop(self):
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
            self._proc = None
            print("[local-out] Closed")


# ── Browser Audio Stream ──────────────────────────────────────────────────────

_browser_streams = {}

class BrowserAudioStream:
    """Buffer PCM chunks for browser playback via HTTP streaming."""

    def __init__(self):
        self.stream_id = uuid.uuid4().hex
        self._deque = collections.deque(maxlen=300)
        self._stop = threading.Event()

    def put(self, pcm_bytes):
        if not self._stop.is_set():
            self._deque.append(pcm_bytes)

    def stop(self):
        self._stop.set()

    def is_stopped(self):
        return self._stop.is_set()


class BrowserMP3Stream:
    """Per-session PCM->MP3 encoder for "This Device" playback.

    Same put()/stop()/is_stopped() sink interface as BrowserAudioStream, but
    pipes the player's PCM through ffmpeg to MP3 and buffers the encoded bytes.
    A browser can play the result from an <audio> element, which (unlike the
    Web Audio path or a raw streaming WAV) keeps playing when Safari is
    backgrounded or the screen is locked on iOS (issue #54).
    """

    # If nothing is ever fed within this window of creation (playback aborted,
    # user navigated away before connecting), the encoder self-terminates so an
    # idle ffmpeg does not leak.
    STARTUP_GRACE_SECS = 30.0

    def __init__(self, bitrate_kbps: int = 256):
        self.stream_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._fed = False
        # Per-client MP3 buffers. iOS Safari opens several connections to a
        # media element (a metadata probe, the playback stream, range probes),
        # so each connection gets its own read cursor. A single shared buffer
        # would let those connections steal each other's chunks and stutter.
        self._clients = {}
        self._clients_lock = threading.Lock()
        self._client_seq = 0
        self._proc = None
        try:
            self._proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
                 "-i", "pipe:0",
                 "-acodec", "libmp3lame", "-b:a", f"{int(bitrate_kbps)}k",
                 "-f", "mp3", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except Exception as e:
            print(f"[browser-mp3] Failed to start encoder: {e}")
            self._proc = None
            return
        self._reader = threading.Thread(target=self._read_loop,
                                        name="browser-mp3-read", daemon=True)
        self._reader.start()
        threading.Thread(target=self._grace_watchdog, daemon=True).start()

    def _grace_watchdog(self):
        if self._stop.wait(self.STARTUP_GRACE_SECS):
            return  # already stopped
        if not self._fed:
            print(f"[browser-mp3] Unused stream {self.stream_id[:8]}, self-stopping")
            self.stop()

    def _read_loop(self):
        proc = self._proc
        if not proc or not proc.stdout:
            return
        out = proc.stdout
        try:
            while not self._stop.is_set():
                data = out.read(4096)
                if not data:
                    break
                with self._clients_lock:
                    for q in self._clients.values():
                        q.append(data)
        except Exception:
            pass

    def put(self, pcm_bytes):
        proc = self._proc
        if self._stop.is_set() or not proc or not proc.stdin:
            return
        self._fed = True
        with suppress(BrokenPipeError, OSError):
            proc.stdin.write(pcm_bytes)

    def register_client(self) -> int:
        """Register an HTTP consumer; returns a client id with its own buffer."""
        with self._clients_lock:
            self._client_seq += 1
            cid = self._client_seq
            self._clients[cid] = collections.deque(maxlen=4000)
            return cid

    def unregister_client(self, client_id: int):
        """Drop one consumer. Does NOT stop the encoder: other connections (and
        the player) may still need it. The encoder is stopped by the player on
        playback end and by the startup watchdog if never used."""
        with self._clients_lock:
            self._clients.pop(client_id, None)

    def get_chunk(self, client_id: int):
        """Next MP3 chunk for a client: b"" if none yet, None once stopped."""
        with self._clients_lock:
            q = self._clients.get(client_id)
            if q is not None and q:
                return q.popleft()
        return None if self._stop.is_set() else b""

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc:
            with suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                with suppress(Exception):
                    proc.kill()
        print("[browser-mp3] Encoder stopped")

    def is_stopped(self):
        return self._stop.is_set()


# ── Audio Callback ────────────────────────────────────────────────────────────

_last_overflow_log = 0.0

def make_callback(streams, eq, fp_buffer):
    def callback(indata, frames, cb_time, status):
        global _last_overflow_log
        if status:
            now = time.monotonic()
            if now - _last_overflow_log > 30.0:
                print(f"[audio] {status}")
                _last_overflow_log = now
        # Scarlett 2i2 4th Gen captures 4 channels: use first 2 (L+R inputs)
        audio_in = np.ascontiguousarray(indata[:, :CHANNELS]) if indata.shape[1] > CHANNELS else indata
        # Compute RMS once from float32 data: avoids the expensive
        # int16->float32 round-trip that rec_buffer and album_recorder
        # were each doing independently on every callback.
        rms = float(np.sqrt(np.mean(audio_in ** 2)))
        # Feed the input-gain calibration meter (issue #67) with the raw
        # pre-EQ block peak. Local ref: the field can be cleared mid-callback.
        cal = state.gain_cal
        if cal is not None:
            cal.feed(float(np.max(np.abs(audio_in))))
        # Feed fingerprint buffer BEFORE EQ/volume: raw signal gives best results
        raw_pcm = (audio_in * 32767).astype(np.int16).tobytes()
        fp_buffer.put(raw_pcm)
        # Feed recorder with raw pre-EQ audio (preserves dynamics)
        # Always call put(): silence detection runs inside regardless of is_active,
        # so inter-track gaps trigger recogniser reset even when not recording
        if state.rec_buffer:
            state.rec_buffer.put(raw_pcm, rms=rms)
        # Feed album recorder (full-side capture) with raw pre-EQ audio
        if state.album_recorder and state.album_recorder.is_active:
            state.album_recorder.put(raw_pcm, rms=rms)
        # Apply EQ + volume for the actual stream output
        audio = eq.process(audio_in)
        pcm   = (audio * 32767).astype(np.int16).tobytes()
        for s in streams:
            s.put(pcm)
        state.live_mp3.put(pcm)
    return callback
