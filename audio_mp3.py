#!/usr/bin/env python3
"""Vinyl AirPlay: live PCM to MP3 broadcaster for the /live.mp3 HTTP stream.

Extracted from main.py. Encodes captured PCM to MP3 once via ffmpeg and fans
the bytes out to any number of HTTP clients. Audio constants are kept local
(matching audio_eq.py / player.py / recorder.py) so this module has no import
back into main.
"""

import collections
import subprocess
import threading
import time
from contextlib import suppress
from typing import ClassVar

SAMPLE_RATE = 44100
CHANNELS    = 2
BITS        = 16


class LiveMP3Broadcaster:
    """Encodes live PCM to MP3 once and fans out bytes to any number of clients."""

    ALLOWED_BITRATES: ClassVar[set[int]] = {128, 192, 256, 320}
    # Soft cap on simultaneous listeners. Encoding is shared, so the cost per
    # listener is only bandwidth, but a runaway client loop shouldn't be able
    # to open unbounded response streams.
    MAX_CLIENTS = 10
    # Tune chunk size for ffmpeg and smoother streaming (use 1152 samples per MP3 frame for 44.1kHz stereo)
    # 1152 samples * 2 channels * 2 bytes/sample = 4608 bytes
    PCM_CHUNK_BYTES = 4608
    PCM_CHUNK_SECS = PCM_CHUNK_BYTES / (SAMPLE_RATE * CHANNELS * (BITS // 8))

    def __init__(self):
        self.enabled = False
        self.bitrate_kbps = 256
        self._proc = None
        self._running = threading.Event()
        self._start_lock = threading.Lock()
        self._input_lock = threading.Lock()
        # 16384 chunks ~= ~7 minutes at PCM_CHUNK_SECS. Generous buffer
        # for transient capture/encode stalls.
        self._input_chunks = collections.deque(maxlen=16384)
        # Leftover bytes from previous put() call that didn't align to a
        # full PCM_CHUNK_BYTES boundary. Saved so the next put() can
        # complete a full chunk instead of the feed loop padding with
        # silence (which is audibly choppy).
        self._partial_input = b""
        self._clients_lock = threading.Lock()
        self._clients = {}
        self._client_seq = 0
        self._feeder_thread = None
        self._reader_thread = None

    @classmethod
    def sanitize_bitrate(cls, value) -> int:
        try:
            v = int(value)
        except Exception:
            return 256
        return v if v in cls.ALLOWED_BITRATES else 256

    def is_running(self) -> bool:
        return self._running.is_set() and self._proc is not None

    def configure(self, enabled: bool, bitrate_kbps: int):
        bitrate_kbps = self.sanitize_bitrate(bitrate_kbps)
        should_restart = self.is_running() and self.bitrate_kbps != bitrate_kbps
        self.enabled = bool(enabled)
        self.bitrate_kbps = bitrate_kbps

        if not self.enabled:
            self.stop()
            return
        # Encoder lifecycle is tied to listeners: ffmpeg runs only while at
        # least one client is connected, instead of encoding silence around
        # the clock whenever the option is enabled.
        if self.listener_count() > 0 and (should_restart or not self.is_running()):
            self.start(force=should_restart)

    def listener_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def start(self, force: bool = False):
        """Start the encoder. Serialized by _start_lock (register_client on
        the event loop and put() on the audio callback thread can race);
        no-op when already running unless force=True (bitrate change)."""
        with self._start_lock:
            if self.is_running() and not force:
                return
            # Tear down any previous encoder without flipping enabled. start()
            # may be called from configure() right after the user toggles the
            # stream on, and we don't want our own cleanup to immediately
            # disable us again.
            self._teardown_encoder()
            # Pre-fill input with silence so a client that connects while the
            # capture stream is idle still gets a playable (silent) stream
            # immediately instead of a stalled response.
            silence = b"\x00" * self.PCM_CHUNK_BYTES
            with self._input_lock:
                for _ in range(128):
                    self._input_chunks.append(silence)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "-i",
                "pipe:0",
                "-acodec",
                "libmp3lame",
                "-b:a",
                f"{self.bitrate_kbps}k",
                "-f",
                "mp3",
                "pipe:1",
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except Exception as e:
                self._proc = None
                print(f"[http-stream] Failed to start ffmpeg encoder: {e}")
                return

            self._running.set()
            self._feeder_thread = threading.Thread(target=self._feed_loop, name="http-mp3-feed", daemon=True)
            self._reader_thread = threading.Thread(target=self._read_loop, name="http-mp3-read", daemon=True)
            self._feeder_thread.start()
            self._reader_thread.start()
            print(f"[http-stream] Encoder started at {self.bitrate_kbps} kbps")

    def stop(self):
        # Public stop: hard stop. Sets enabled=False so put() will
        # early-return instead of auto-restarting the encoder via its
        # auto-recovery path. Without this, an audio callback that fires
        # during shutdown will re-spawn ffmpeg and block systemd from
        # terminating the service. configure() resets enabled=True when
        # the user turns the stream back on in Settings.
        self.enabled = False
        self._teardown_encoder()

    def _teardown_encoder(self):
        # Internal cleanup: tears down ffmpeg and drains queues without
        # touching `enabled`. Used by both start() (clean slate before
        # relaunch) and stop() (hard shutdown).
        self._running.clear()
        with self._input_lock:
            self._input_chunks.clear()
            self._partial_input = b""

        proc = self._proc
        self._proc = None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:
            with suppress(Exception):
                proc.kill()
        print("[http-stream] Encoder stopped")

    def put(self, pcm_bytes: bytes):
        if not self.enabled or not pcm_bytes:
            return
        if not self.is_running():
            # Auto-recovery from a dead ffmpeg, but only while someone is
            # actually listening: with no clients the encoder stays down
            if self.listener_count() == 0:
                return
            self.start()
            if not self.is_running():
                return
        with self._input_lock:
            # Combine any leftover bytes from the previous call so chunks
            # always align to PCM_CHUNK_BYTES (one MP3 frame at 44.1k
            # stereo s16). Without this, capture blocks that aren't a
            # multiple of PCM_CHUNK_BYTES leave a partial chunk in the
            # queue, which the feed loop then pads with silence: that
            # silence injection is exactly what makes /live.mp3 sound
            # choppy when BLOCK_SIZE != 4608.
            data = self._partial_input + pcm_bytes
            chunk = self.PCM_CHUNK_BYTES
            full_count = len(data) // chunk
            for i in range(full_count):
                self._input_chunks.append(data[i * chunk:(i + 1) * chunk])
            self._partial_input = data[full_count * chunk:]

    def register_client(self) -> int | None:
        """Register a listener. Returns None when the soft cap is reached.
        The first listener starts the encoder."""
        with self._clients_lock:
            if len(self._clients) >= self.MAX_CLIENTS:
                return None
            self._client_seq += 1
            cid = self._client_seq
            # Further increase per-client buffer size (e.g., 8192 chunks)
            self._clients[cid] = collections.deque(maxlen=8192)
        if self.enabled and not self.is_running():
            self.start()
        return cid

    def unregister_client(self, client_id: int):
        with self._clients_lock:
            self._clients.pop(client_id, None)
            remaining = len(self._clients)
        # Last listener gone: stop encoding (keep enabled so the next
        # listener starts it again)
        if remaining == 0:
            self._teardown_encoder()

    def get_chunk(self, client_id: int) -> bytes | None:
        with self._clients_lock:
            q = self._clients.get(client_id)
            if q is None:
                return None
            if q:
                return q.popleft()
            return b""

    def _broadcast(self, data: bytes):
        if not data:
            return
        with self._clients_lock:
            for q in self._clients.values():
                q.append(data)

    def _feed_loop(self):
        silence = b"\x00" * self.PCM_CHUNK_BYTES
        # Pre-buffer: wait until at least 2 seconds of audio is available
        prebuffer_chunks = int(2.0 / self.PCM_CHUNK_SECS)
        while len(self._input_chunks) < prebuffer_chunks and self._running.is_set():
            time.sleep(self.PCM_CHUNK_SECS / 2)

        next_tick = time.monotonic()
        while self._running.is_set():
            proc = self._proc
            if not proc or proc.poll() is not None:
                break

            with self._input_lock:
                pcm = self._input_chunks.popleft() if self._input_chunks else None
            # Always send a full chunk of silence if no PCM is available
            if pcm is None or len(pcm) == 0:
                pcm = silence
            elif len(pcm) < self.PCM_CHUNK_BYTES:
                pcm = pcm + (b"\x00" * (self.PCM_CHUNK_BYTES - len(pcm)))

            try:
                proc.stdin.write(pcm)
            except Exception:
                break

            # Log underruns every 5 seconds

            # Sleep exactly PCM_CHUNK_SECS per chunk for real-time pacing
            next_tick += self.PCM_CHUNK_SECS
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

        self._running.clear()

    def _read_loop(self):
        while self._running.is_set():
            proc = self._proc
            if not proc or proc.poll() is not None:
                break
            try:
                data = proc.stdout.read(4096)
            except Exception:
                break
            if not data:
                time.sleep(0.01)
                continue
            self._broadcast(data)
        self._running.clear()
