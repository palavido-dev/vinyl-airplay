#!/usr/bin/env python3
"""
Vinyl AirPlay — MP3 Recorder
Captures audio while streaming, detects track boundaries via silence,
encodes to 320kbps MP3, and embeds ID3 tags + album art.

File naming: Artist - Album - TrackNum - Title.mp3
"""

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE       = 44100
CHANNELS          = 2
BITRATE           = "320"           # kbps
RECORDINGS_DIR    = Path(__file__).parent / "recordings"

# Silence detection
# Adaptive silence detection
# Rather than a fixed RMS threshold (which varies by pressing), we measure the
# actual playing level and call something "silent" when it drops to a fraction of that.
SILENCE_RATIO     = 0.40            # silence = RMS drops below this fraction of signal level
                                    # 0.40 = must be 8dB quieter than the music
                                    # Vinyl groove noise is typically 10-20dB below music,
                                    # so this reliably catches inter-track gaps on any pressing
SILENCE_RATIO_MIN = 0.006           # absolute floor — never treat above this as silence
SIGNAL_ADAPT_RATE = 0.002           # EMA rate for signal level tracker (slow — ~500 chunks)
SILENCE_MIN_SECS  = 1.5             # silence must last this long to split track
                                    # reduced from 2.0 — some albums have short inter-track gaps
END_OF_SIDE_SECS  = 8.0             # silence this long = end of side — auto-flush final track
                                    # _split_track trims to silence_start+pad so no long silence
                                    # is appended to the file
SILENCE_PAD_SECS  = 0.5            # keep this much silence at end of track (natural fade)
MIN_TRACK_SECS    = 15              # ignore tracks shorter than this (needle drop, interludes)
STARTUP_AUDIO_SECS = 2.0            # sustained audio required before silence detection begins


# ── Recording Buffer ──────────────────────────────────────────────────────────

class RecordingBuffer:
    """
    Receives raw PCM chunks from the audio callback.
    Detects silence gaps and either:
      - auto-splits into tracks (auto mode)
      - records one continuous chunk until stop() called (manual mode)
    Thread-safe: put() from audio thread, everything else from main thread.
    """

    def __init__(self,
                 on_track_ready,          # callback(pcm_bytes, duration_secs)
                 on_level_update,         # callback(rms_float) — for UI meter
                 on_audio_detected=None,  # callback() — fired once when startup gate opens
                 on_end_of_side=None,     # callback() — fired when end-of-side silence detected
                 auto_split: bool = True):
        self._lock            = threading.Lock()
        self._on_track_ready     = on_track_ready
        self._on_level_update    = on_level_update
        self._on_audio_detected  = on_audio_detected
        self._on_end_of_side     = on_end_of_side
        self._auto_split         = auto_split

        self._chunks: list[bytes] = []
        self._total_bytes   = 0
        self._active        = False

        # Silence detection state
        self._silence_secs  = 0.0
        self._last_rms      = 0.0
        self._block_secs    = 1024 / SAMPLE_RATE  # seconds per callback block (approx)

        # Track where silence started so we can trim it from the end
        self._silence_start_byte = 0

        # Startup gate: don't act on silence until we've seen sustained audio first.
        self._sustained_audio_secs = 0.0   # how long we've heard audio above threshold
        self._audio_seen           = False  # True once startup gate is cleared
        self._end_of_side_fired    = False  # prevent double-firing end-of-side flush

        # Adaptive signal level: exponential moving average of RMS while music is playing.
        # Silence threshold = _signal_level * SILENCE_RATIO.
        # Adapts automatically to any pressing's loudness.
        self._signal_level = 0.03          # initial estimate; refined once audio starts
        self._silence_log_countdown = 0    # rate-limit diagnostic prints

    def start(self, auto_split: bool = True):
        with self._lock:
            self._chunks        = []
            self._total_bytes   = 0
            self._active        = True
            self._auto_split    = auto_split
            self._silence_secs  = 0.0
            self._silence_start_byte = 0
            self._sustained_audio_secs = 0.0
            self._audio_seen           = False
            self._signal_level         = 0.03
            self._silence_log_countdown = 0
            self._end_of_side_fired    = False
        print(f"[recorder] Recording started (auto_split={auto_split})")

    def stop(self) -> Optional[bytes]:
        """Stop recording and return the accumulated PCM, or None if too short."""
        with self._lock:
            if not self._active:
                return None
            self._active = False
            pcm = b"".join(self._chunks)
            self._chunks = []
            self._total_bytes = 0

        duration = _pcm_duration(pcm)
        if duration < MIN_TRACK_SECS:
            print(f"[recorder] Track too short ({duration:.1f}s) — discarding")
            return None

        print(f"[recorder] Recording stopped — {duration:.1f}s captured")
        return pcm

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def elapsed_secs(self) -> float:
        with self._lock:
            return _pcm_duration(b"x" * self._total_bytes)

    def put(self, pcm_chunk: bytes):
        """Called from audio callback with each block of int16 stereo PCM.

        Silence detection and level monitoring always run regardless of whether
        recording is active — this allows inter-track gap detection (for recogniser
        reset) even during normal non-recording streaming.
        Only chunk accumulation is gated behind _active.
        """
        # Calculate RMS first — needed for both recording and silence detection
        samples = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        rms     = float(np.sqrt(np.mean(samples ** 2)))

        with self._lock:
            if self._active:
                self._chunks.append(pcm_chunk)
                self._total_bytes += len(pcm_chunk)
            self._last_rms = rms

        # Level update (outside lock to avoid blocking audio thread)
        self._on_level_update(rms)

        if not self._auto_split:
            return

        # Startup gate: accumulate sustained audio before enabling silence detection.
        # Once we've heard STARTUP_AUDIO_SECS of continuous audio, the gate opens
        # and normal split logic takes over.
        chunk_secs = len(pcm_chunk) / (SAMPLE_RATE * CHANNELS * 2)

        if not self._audio_seen:
            if rms >= SILENCE_RATIO_MIN:
                self._sustained_audio_secs += chunk_secs
                if self._sustained_audio_secs >= STARTUP_AUDIO_SECS:
                    self._audio_seen = True
                    # Seed signal level from the startup burst so threshold is
                    # calibrated before the first track even ends
                    self._signal_level = rms
                    thresh = max(SILENCE_RATIO_MIN, rms * SILENCE_RATIO)
                    print(f"[recorder] Audio detected — silence detection active"
                          f"  signal={rms:.5f}  silence_threshold={thresh:.5f}")
                    if self._on_audio_detected:
                        self._on_audio_detected()
            else:
                # Reset sustained counter if audio drops before gate opens
                self._sustained_audio_secs = 0.0
            return  # don't do split logic until gate is open

        # Adaptive silence detection (gate is open)
        # Compute dynamic threshold from current signal level estimate
        silence_threshold = max(SILENCE_RATIO_MIN, self._signal_level * SILENCE_RATIO)

        if rms < silence_threshold:
            self._silence_secs += chunk_secs
            if self._silence_start_byte == 0:
                with self._lock:
                    self._silence_start_byte = self._total_bytes - len(pcm_chunk)
            # Periodic diagnostic log so we can see gap RMS in journalctl
            self._silence_log_countdown -= 1
            if self._silence_log_countdown <= 0:
                print(f"[recorder] Gap: RMS={rms:.5f}  threshold={silence_threshold:.5f}"
                      f"  signal={self._signal_level:.5f}  silence={self._silence_secs:.1f}s")
                self._silence_log_countdown = 20  # log every ~20 chunks
            # End-of-side detection: long silence = needle lifted / run-out groove
            if (not self._end_of_side_fired
                    and self._silence_secs >= END_OF_SIDE_SECS):
                self._end_of_side_fired = True
                print(f"[recorder] End-of-side detected ({self._silence_secs:.1f}s silence)"
                      f" — flushing final track (trimmed to music end)")
                self._split_track()          # trims silence, hands off final track
                self._audio_seen = False     # re-arm startup gate for next side
                if self._on_end_of_side:
                    self._on_end_of_side()
        else:
            # Update signal level EMA while music is playing
            self._signal_level += SIGNAL_ADAPT_RATE * (rms - self._signal_level)
            self._silence_log_countdown = 0  # reset so next gap logs immediately
            self._end_of_side_fired = False  # reset if audio returns (e.g. between sides)
            if self._silence_secs >= SILENCE_MIN_SECS:
                # Sustained silence ended — split track
                self._split_track()
            self._silence_secs       = 0.0
            self._silence_start_byte = 0

    def _split_track(self):
        """Called when silence gap detected — extract the completed track."""
        with self._lock:
            pcm = b"".join(self._chunks)
            # Trim to silence start + pad (keep natural fade)
            pad_bytes = int(SILENCE_PAD_SECS * SAMPLE_RATE * CHANNELS * 2)
            cut_at    = self._silence_start_byte + pad_bytes
            track_pcm = pcm[:cut_at]
            # Keep audio after silence for next track
            self._chunks      = [pcm[cut_at:]]
            self._total_bytes = len(pcm[cut_at:])
            self._silence_secs       = 0.0
            self._silence_start_byte = 0

        duration = _pcm_duration(track_pcm)
        if duration < MIN_TRACK_SECS:
            print(f"[recorder] Gap detected ({duration:.1f}s PCM) — notifying track boundary")
            # Still notify for recogniser reset even if not recording
            self._on_track_ready(None, 0.0)
            return

        print(f"[recorder] Auto-split: track ready ({duration:.1f}s)")
        self._on_track_ready(track_pcm, duration)


# ── PCM Helpers ───────────────────────────────────────────────────────────────

def _pcm_duration(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * CHANNELS * 2)


def _pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


# ── MP3 Encoding ──────────────────────────────────────────────────────────────

def encode_mp3(pcm: bytes, output_path: Path) -> bool:
    """Encode PCM audio to MP3 at 320kbps using LAME."""
    wav_bytes = _pcm_to_wav(pcm)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_wav = f.name

    try:
        result = subprocess.run([
            "lame",
            "--silent",
            "-b", BITRATE,
            "--cbr",
            "-q", "0",          # highest quality algorithm
            "--id3v2-only",     # we'll write our own tags via mutagen
            tmp_wav,
            str(output_path),
        ], capture_output=True, timeout=60)

        if result.returncode != 0:
            print(f"[recorder] LAME error: {result.stderr.decode()[:200]}")
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[recorder] encode_mp3 failed: {e}")
        return False
    finally:
        os.unlink(tmp_wav)


# ── ID3 Tagging ───────────────────────────────────────────────────────────────

def tag_mp3(mp3_path: Path, metadata: dict) -> bool:
    """
    Write ID3v2.4 tags to an MP3 file using mutagen.
    metadata keys: title, artist, album, album_artist, year, track_number,
                   side, genre, artwork_path
    """
    try:
        from mutagen.id3 import (
            ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TCON,
            APIC, ID3NoHeaderError
        )

        try:
            tags = ID3(str(mp3_path))
        except ID3NoHeaderError:
            tags = ID3()

        title        = metadata.get("title", "Unknown Track")
        artist       = metadata.get("artist") or metadata.get("album_artist", "Unknown Artist")
        album_artist = metadata.get("album_artist", artist)
        album        = metadata.get("album", "Unknown Album")
        year         = str(metadata.get("year", "")) if metadata.get("year") else ""
        track_num    = metadata.get("track_number", "")
        genre        = metadata.get("genre", "")

        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags["TPE2"] = TPE2(encoding=3, text=album_artist)
        tags["TALB"] = TALB(encoding=3, text=album)
        if year:
            tags["TDRC"] = TDRC(encoding=3, text=year)
        if track_num:
            tags["TRCK"] = TRCK(encoding=3, text=str(track_num))
        if genre:
            tags["TCON"] = TCON(encoding=3, text=genre)

        # Embed artwork
        art_path = metadata.get("user_artwork_path") or metadata.get("artwork_path")
        if art_path and Path(art_path).exists():
            with open(art_path, "rb") as f:
                art_data = f.read()
            # Detect format
            mime = "image/jpeg" if art_path.lower().endswith(('.jpg', '.jpeg')) else "image/png"
            tags["APIC"] = APIC(
                encoding=3,
                mime=mime,
                type=3,     # Cover (front)
                desc="Cover",
                data=art_data,
            )

        tags.save(str(mp3_path), v2_version=4)
        return True

    except Exception as e:
        print(f"[recorder] tag_mp3 failed: {e}")
        return False


# ── File Naming ───────────────────────────────────────────────────────────────

def make_filename(metadata: dict) -> str:
    """
    Build filename: Artist - Album - TrackNum - Title.mp3
    Sanitizes all components for filesystem safety.
    """
    def san(s: str) -> str:
        if not s:
            return "Unknown"
        # Remove characters that are problematic on Windows/Mac/Linux
        for ch in r'\/:*?"<>|':
            s = s.replace(ch, "-")
        return s.strip(" .")[:60]

    artist   = san(metadata.get("album_artist") or metadata.get("artist", "Unknown"))
    album    = san(metadata.get("album", "Unknown Album"))
    track    = str(metadata.get("track_number", "")).zfill(2) if metadata.get("track_number") else "00"
    title    = san(metadata.get("title", "Unknown Track"))

    return f"{artist} - {album} - {track} - {title}.mp3"


# ── Main Save Function ────────────────────────────────────────────────────────

def save_recording(pcm: bytes, metadata: dict) -> Optional[Path]:
    """
    Encode PCM to MP3, tag it, and save to recordings/.
    Returns the output path on success, None on failure.
    metadata: same keys as tag_mp3 above.
    """
    RECORDINGS_DIR.mkdir(exist_ok=True)

    filename = make_filename(metadata)
    out_path = RECORDINGS_DIR / filename

    # Avoid overwriting — append counter if needed
    counter = 1
    while out_path.exists():
        stem    = out_path.stem
        out_path = RECORDINGS_DIR / f"{stem} ({counter}).mp3"
        counter += 1

    print(f"[recorder] Encoding → {out_path.name}")

    if not encode_mp3(pcm, out_path):
        print(f"[recorder] Encoding failed — check that lame is installed: sudo apt install lame")
        return None

    if not out_path.exists():
        print(f"[recorder] Output file missing after encode — lame may have failed silently")
        return None

    if not tag_mp3(out_path, metadata):
        print(f"[recorder] Tagging failed — MP3 saved without tags")

    size_mb  = out_path.stat().st_size / (1024 * 1024)
    duration = _pcm_duration(pcm)
    print(f"[recorder] Saved {out_path.name} ({duration:.0f}s, {size_mb:.1f} MB)")
    return out_path


# ── Recordings Catalog ────────────────────────────────────────────────────────

def list_recordings() -> list[dict]:
    """List all MP3 files in recordings/ with metadata."""
    RECORDINGS_DIR.mkdir(exist_ok=True)
    files = sorted(RECORDINGS_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        result.append({
            "filename": f.name,
            "size_mb":  round(stat.st_size / (1024 * 1024), 1),
            "modified": stat.st_mtime,
        })
    return result


def delete_recording(filename: str) -> bool:
    path = RECORDINGS_DIR / Path(filename).name  # prevent path traversal
    if path.exists() and path.suffix == ".mp3":
        path.unlink()
        return True
    return False
