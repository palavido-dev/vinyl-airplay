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
SILENCE_RATIO     = 0.50            # silence = RMS drops below this fraction of signal level
                                    # 0.50 = must be ~6dB quieter than the music
                                    # Vinyl groove noise is typically 10-20dB below music,
                                    # so this reliably catches inter-track gaps on any pressing
                                    # (0.40 gave only 7% headroom, 0.45 gave 10%, both too tight —
                                    # noise at 0.013 vs threshold 0.014 caused constant resets.
                                    # 0.50 gives ~19% headroom: noise 0.013 vs threshold 0.016)
SILENCE_RATIO_MIN = 0.006           # absolute floor — never treat above this as silence
SIGNAL_ADAPT_RATE = 0.002           # EMA rate for signal level tracker (slow — ~500 chunks)
SILENCE_MIN_SECS  = 1.5             # silence must last this long to split track
                                    # reduced from 2.0 — some albums have short inter-track gaps
END_OF_SIDE_SECS  = 20.0            # silence this long = end of side — auto-flush final track
                                    # _split_track trims to silence_start+pad so no long silence
                                    # is appended to the file
SILENCE_PAD_SECS  = 0.5            # keep this much silence at end of track (natural fade)
MIN_TRACK_SECS    = 15              # ignore tracks shorter than this (needle drop, interludes)
STARTUP_AUDIO_SECS = 2.0            # sustained audio required before silence detection begins
STARTUP_MIN_RMS    = 0.015          # minimum RMS to count as music for startup gate
                                    # vinyl groove noise is ~0.006-0.012, music is typically 0.02+
                                    # 0.015 avoids opening the gate on run-in groove noise


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
        self._smoothed_rms  = 0.0   # EMA-smoothed RMS for silence decisions (avoids noise spikes)

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

        # Expected duration of the current track (from catalog metadata or session estimate).
        # When > 0, suppresses track splits until a percentage of expected has elapsed.
        self.expected_track_secs = 0.0
        self.expected_is_estimate = False  # True = session median (use 60%), False = Discogs (use 45%)

        # Number of tracks remaining on this side (including current).
        # When > 0, uses a longer end-of-side threshold to avoid false triggers
        # on records with long inter-track gaps.
        self.remaining_tracks = 0

    def start(self, auto_split: bool = True):
        with self._lock:
            self._chunks        = []
            self._total_bytes   = 0
            self._active        = True
            self._auto_split    = auto_split
            self._silence_secs  = 0.0
            self._silence_start_byte = 0
            self._smoothed_rms  = 0.0
            self._sustained_audio_secs = 0.0
            self._audio_seen           = False
            self._signal_level         = 0.03
            self._silence_log_countdown = 0
            self._end_of_side_fired    = False
            self.expected_track_secs   = 0.0
            self.expected_is_estimate  = False
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
            if rms >= STARTUP_MIN_RMS:
                self._sustained_audio_secs += chunk_secs
                if self._sustained_audio_secs >= STARTUP_AUDIO_SECS:
                    self._audio_seen = True
                    # Seed signal level and smoothed RMS from the startup burst
                    self._signal_level = rms
                    self._smoothed_rms = rms
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
        # Smooth the RMS with an EMA to prevent vinyl surface noise spikes from
        # resetting the silence counter. A single 0.014 spike among 0.010 chunks
        # won't break silence detection when the AVERAGE is clearly below threshold.
        SMOOTH_ALPHA = 0.08  # ~12 chunk window (~1.2s) — heavy smoothing absorbs noise spikes
        if self._smoothed_rms == 0.0:
            self._smoothed_rms = rms  # seed on first call
        else:
            self._smoothed_rms += SMOOTH_ALPHA * (rms - self._smoothed_rms)

        # Compute dynamic threshold from current signal level estimate.
        # When we know expected track duration and are past it, boost the ratio
        # so vinyl surface noise in between-track gaps reliably registers as silence.
        # Only boost for Discogs durations (precise) — session estimates are too rough
        # and boosting on an inaccurate estimate can trap quiet passages.
        effective_ratio = SILENCE_RATIO  # 0.50 baseline

        if self.expected_track_secs > 0 and not self.expected_is_estimate:
            with self._lock:
                accumulated_secs = self._total_bytes / (SAMPLE_RATE * CHANNELS * 2)
            overdue_pct = accumulated_secs / self.expected_track_secs
            if overdue_pct >= 0.80:
                # Ramp ratio from 0.50 → 0.63 between 80% and 120% of expected
                boost = min(1.0, (overdue_pct - 0.80) / 0.40)  # 0→1 over range
                effective_ratio = SILENCE_RATIO + boost * 0.13   # 0.50 → 0.63

        silence_threshold = max(SILENCE_RATIO_MIN, self._signal_level * effective_ratio)

        # Hysteresis: once silence is accumulating, require smoothed RMS to clearly
        # exceed the threshold before resetting. Without this, smoothed RMS hovering
        # right at the threshold boundary oscillates in/out and resets silence_secs.
        # Entry: smoothed < threshold
        # Exit:  smoothed >= threshold * 1.1 (10% above — must be clearly music, not noise)
        # With SILENCE_RATIO=0.50 and typical signal=0.032:
        #   entry threshold = 0.016, exit = 0.0176
        #   vinyl noise smooth ~0.013 → stays in silence (well below both)
        #   quiet music smooth ~0.018 → exits silence (above 0.0176)
        in_silence = self._silence_secs > 0
        if in_silence:
            exit_threshold = silence_threshold * 1.1
            is_silent = self._smoothed_rms < exit_threshold
        else:
            is_silent = self._smoothed_rms < silence_threshold

        if is_silent:
            self._silence_secs += chunk_secs
            if self._silence_start_byte == 0:
                with self._lock:
                    self._silence_start_byte = self._total_bytes - len(pcm_chunk)
            # Periodic diagnostic log so we can see gap RMS in journalctl
            self._silence_log_countdown -= 1
            if self._silence_log_countdown <= 0:
                ratio_info = f"  ratio={effective_ratio:.2f}" if effective_ratio > SILENCE_RATIO else ""
                print(f"[recorder] Gap: RMS={rms:.5f}  smooth={self._smoothed_rms:.5f}"
                      f"  threshold={silence_threshold:.5f}"
                      f"  signal={self._signal_level:.5f}  silence={self._silence_secs:.1f}s{ratio_info}")
                self._silence_log_countdown = 20  # log every ~20 chunks
            # End-of-side detection: long silence = needle lifted / run-out groove
            # Use longer threshold when we know more tracks remain on this side,
            # because some records have very long inter-track gaps (20s+).
            # Real end-of-side (needle lift) produces indefinite silence, so
            # waiting longer is fine — leading/trailing trim removes dead air.
            eos_threshold = END_OF_SIDE_SECS
            if self.remaining_tracks > 1:
                eos_threshold = max(END_OF_SIDE_SECS, 90.0)
            if (not self._end_of_side_fired
                    and self._silence_secs >= eos_threshold):
                # Before firing end-of-side, check if accumulated audio makes sense.
                # If we have expected duration data and haven't reached the minimum,
                # this is likely a long quiet passage, not the actual end of side.
                if self.expected_track_secs > 0 and self.remaining_tracks > 1:
                    with self._lock:
                        accumulated_secs = self._silence_start_byte / (SAMPLE_RATE * CHANNELS * 2)
                    min_pct = 0.75 if self.expected_is_estimate else 0.45
                    min_secs = self.expected_track_secs * min_pct
                    if accumulated_secs < min_secs:
                        print(f"[recorder] End-of-side suppressed: {accumulated_secs:.0f}s captured"
                              f" < {min_secs:.0f}s — not a real end-of-side, resetting silence")
                        self._silence_secs = 0.0
                        self._silence_start_byte = 0
                        return  # skip end-of-side, keep recording
                self._end_of_side_fired = True
                print(f"[recorder] End-of-side detected ({self._silence_secs:.1f}s silence, "
                      f"threshold={eos_threshold:.0f}s, remaining={self.remaining_tracks})"
                      f" — flushing final track (trimmed to music end)")
                self._split_track()          # trims silence, hands off final track
                self._audio_seen = False     # re-arm startup gate for next side
                if self._on_end_of_side:
                    self._on_end_of_side()
        else:
            # Update signal level EMA while music is playing (use raw RMS, not smoothed)
            self._signal_level += SIGNAL_ADAPT_RATE * (rms - self._signal_level)
            self._silence_log_countdown = 0  # reset so next gap logs immediately
            self._end_of_side_fired = False  # reset if audio returns (e.g. between sides)
            if self._silence_secs >= SILENCE_MIN_SECS:
                # Check if accumulated audio meets expected track duration before splitting.
                # If we know the track should be ~3 min, don't split after 45 seconds of audio
                # just because there was a quiet passage.
                with self._lock:
                    accumulated_secs = self._silence_start_byte / (SAMPLE_RATE * CHANNELS * 2)

                if self.expected_track_secs > 0:
                    # Use a higher threshold for session estimates (less precise)
                    # Discogs: 45% — tight, because the data is exact
                    # Session estimate: 75% — aggressive, because it's a median of
                    # learned tracks and we'd rather merge a short gap than split
                    # a quiet passage. Still safe for short-track albums since the
                    # median will be proportionally shorter.
                    if self.expected_is_estimate:
                        min_pct = 0.75
                        source = "session est"
                    else:
                        min_pct = 0.45
                        source = "expected"
                    min_secs = self.expected_track_secs * min_pct
                    if accumulated_secs < min_secs:
                        print(f"[recorder] Suppressed split: {accumulated_secs:.0f}s captured"
                              f" < {min_secs:.0f}s ({min_pct:.0%} of {source} {self.expected_track_secs:.0f}s)"
                              f" — treating as quiet passage")
                        self._silence_secs       = 0.0
                        self._silence_start_byte = 0
                        return
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


# ── FLAC Encoding ─────────────────────────────────────────────────────────────

ALBUM_AUDIO_DIR = Path(__file__).parent / "album_audio"


def encode_flac(pcm: bytes, output_path: Path, metadata: dict = {}) -> bool:
    """Encode PCM audio to FLAC using ffmpeg. Returns True on success."""
    wav_bytes = _pcm_to_wav(pcm)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_wav = f.name

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_wav,
            "-c:a", "flac",
            "-compression_level", "5",  # good balance of speed vs size
        ]

        # Add metadata tags
        if metadata.get("title"):
            cmd += ["-metadata", f"TITLE={metadata['title']}"]
        if metadata.get("artist"):
            cmd += ["-metadata", f"ARTIST={metadata['artist']}"]
        if metadata.get("album"):
            cmd += ["-metadata", f"ALBUM={metadata['album']}"]
        if metadata.get("year"):
            cmd += ["-metadata", f"DATE={metadata['year']}"]
        if metadata.get("genre"):
            cmd += ["-metadata", f"GENRE={metadata['genre']}"]
        if metadata.get("disc"):
            cmd += ["-metadata", f"DISC={metadata['disc']}"]

        cmd.append(str(output_path))

        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            print(f"[recorder] ffmpeg FLAC error: {result.stderr.decode()[:300]}")
            return False
        return True

    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[recorder] encode_flac failed: {e}")
        return False
    finally:
        os.unlink(tmp_wav)


def make_album_audio_filename(artist: str, album: str, side: str) -> str:
    """Build filename for a full-side album recording."""
    def san(s: str) -> str:
        if not s:
            return "Unknown"
        for ch in r'\/:*?"<>|':
            s = s.replace(ch, "-")
        return s.strip(" .")[:60]

    return f"{san(artist)} - {san(album)} - Side {side}.flac"


# ── Album Recorder (Full-Side Capture) ───────────────────────────────────────

class AlbumRecorder:
    """
    Captures a full album side as one continuous FLAC file while the existing
    RecordingBuffer handles track-level splitting for fingerprinting.

    Usage:
        recorder = AlbumRecorder(album_id, side, album_info)
        # Feed PCM from audio callback:
        recorder.put(pcm_chunk)
        # When track boundary detected by RecordingBuffer:
        recorder.mark_track_boundary(track_id)
        # When end-of-side detected or user stops:
        path, duration = recorder.finish()
    """

    def __init__(self, album_id: int, side: str, album_info: dict):
        self._lock = threading.Lock()
        self.album_id = album_id
        self.side = side
        self.album_info = album_info  # {artist, title, year, genre, ...}

        self._chunks: list[bytes] = []
        self._total_bytes = 0
        self._active = True

        # Track boundary tracking
        self._track_boundaries: list[dict] = []  # [{track_id, start_byte, start_secs}]
        self._current_track_start_byte = 0

        # Startup gate — same idea as RecordingBuffer: don't count silence
        self._audio_started = False
        self.on_audio_detected = None  # callback when first audio arrives

        ALBUM_AUDIO_DIR.mkdir(exist_ok=True)
        print(f"[album-rec] Started: {album_info.get('artist')} - "
              f"{album_info.get('title')} Side {side}")

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def elapsed_secs(self) -> float:
        with self._lock:
            return self._total_bytes / (SAMPLE_RATE * CHANNELS * 2)

    @property
    def track_count(self) -> int:
        return len(self._track_boundaries)

    def put(self, pcm_chunk: bytes):
        """Called from audio callback with each block of int16 stereo PCM."""
        if not self._active:
            return

        # Detect first audio to mark start
        if not self._audio_started:
            samples = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2)))
            if rms >= 0.006:  # same as SILENCE_RATIO_MIN
                self._audio_started = True
                print("[album-rec] Audio detected — recording")
                if self.on_audio_detected:
                    try: self.on_audio_detected()
                    except Exception: pass
            else:
                return  # skip pre-needle silence

        with self._lock:
            self._chunks.append(pcm_chunk)
            self._total_bytes += len(pcm_chunk)

    def mark_track_boundary(self, track_id: int = None):
        """
        Called when RecordingBuffer detects a track split (silence gap).
        Records the timestamp of the boundary within the full-side audio.
        """
        with self._lock:
            boundary_byte = self._total_bytes
            boundary_secs = boundary_byte / (SAMPLE_RATE * CHANNELS * 2)

            # Close out the previous track boundary
            if self._track_boundaries:
                prev = self._track_boundaries[-1]
                prev["end_byte"] = boundary_byte
                prev["end_secs"] = boundary_secs

            # Start new track
            self._track_boundaries.append({
                "track_id":   track_id,
                "start_byte": boundary_byte,
                "start_secs": boundary_secs,
                "end_byte":   None,
                "end_secs":   None,
            })

            print(f"[album-rec] Track boundary at {boundary_secs:.1f}s "
                  f"(track {len(self._track_boundaries)}, id={track_id})")

    def mark_first_track(self, track_id: int = None):
        """
        Mark the start of the first track (called when audio is first detected).
        """
        with self._lock:
            if not self._track_boundaries:
                self._track_boundaries.append({
                    "track_id":   track_id,
                    "start_byte": 0,
                    "start_secs": 0.0,
                    "end_byte":   None,
                    "end_secs":   None,
                })
                print(f"[album-rec] First track started (id={track_id})")

    def finish(self) -> tuple[Optional[Path], float, list[dict]]:
        """
        Finalize the recording: encode to FLAC, return path + duration + boundaries.
        Returns (file_path, duration_secs, track_boundaries) or (None, 0, []).
        """
        with self._lock:
            self._active = False
            if not self._chunks:
                print("[album-rec] Nothing recorded — no audio received")
                return None, 0.0, []

            pcm = b"".join(self._chunks)
            self._chunks = []

            # Close out the last track boundary
            total_secs = len(pcm) / (SAMPLE_RATE * CHANNELS * 2)
            if self._track_boundaries:
                last = self._track_boundaries[-1]
                if last["end_secs"] is None:
                    last["end_byte"] = len(pcm)
                    last["end_secs"] = total_secs

            boundaries = list(self._track_boundaries)

        # ── Trim leading silence (before needle audio) ────────────────
        TRIM_THRESHOLD = 0.008          # RMS below this = silence
        TRIM_BLOCK     = SAMPLE_RATE * CHANNELS * 2  # 1-second blocks (16-bit stereo)
        FADE_TAIL      = int(0.1 * SAMPLE_RATE * CHANNELS * 2)  # 0.1s buffer

        lead_pos = 0
        pcm_len = len(pcm)
        while lead_pos + TRIM_BLOCK <= pcm_len:
            block = np.frombuffer(pcm[lead_pos:lead_pos + TRIM_BLOCK], dtype=np.int16)
            rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
            if rms >= TRIM_THRESHOLD:
                # Found audio — back up a small buffer so we don't clip the attack
                lead_pos = max(0, lead_pos - FADE_TAIL)
                lead_pos = lead_pos - (lead_pos % 4)  # frame-align
                break
            lead_pos += TRIM_BLOCK
        else:
            lead_pos = 0  # don't trim if everything is quiet

        if lead_pos > 0:
            lead_trimmed_secs = lead_pos / (SAMPLE_RATE * CHANNELS * 2)
            pcm = pcm[lead_pos:]
            print(f"[album-rec] Trimmed {lead_trimmed_secs:.1f}s leading silence")

            # Shift all track boundaries back by the trimmed amount
            for b in boundaries:
                b["start_byte"] = max(0, b["start_byte"] - lead_pos)
                if b["end_byte"] is not None:
                    b["end_byte"] = max(0, b["end_byte"] - lead_pos)
                b["start_secs"] = max(0.0, b["start_secs"] - lead_trimmed_secs)
                if b["end_secs"] is not None:
                    b["end_secs"] = max(0.0, b["end_secs"] - lead_trimmed_secs)

        # ── Trim trailing silence ──────────────────────────────────────

        original_len = len(pcm)
        trim_pos = original_len
        # Walk backwards in 1-second blocks
        while trim_pos > TRIM_BLOCK:
            block_start = trim_pos - TRIM_BLOCK
            block = np.frombuffer(pcm[block_start:trim_pos], dtype=np.int16)
            rms = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2)))
            if rms >= TRIM_THRESHOLD:
                # This block has audio — keep everything up to here + fade tail
                trim_pos = min(trim_pos + FADE_TAIL, original_len)
                # Align to frame boundary (2 channels × 2 bytes = 4 bytes per frame)
                trim_pos = trim_pos - (trim_pos % 4)
                break
            trim_pos = block_start
        else:
            trim_pos = original_len  # don't trim if everything is quiet (shouldn't happen)

        if trim_pos < original_len:
            trimmed_secs = (original_len - trim_pos) / (SAMPLE_RATE * CHANNELS * 2)
            pcm = pcm[:trim_pos]
            print(f"[album-rec] Trimmed {trimmed_secs:.1f}s trailing silence")

            # Update last track boundary to match trimmed length
            new_total = len(pcm) / (SAMPLE_RATE * CHANNELS * 2)
            if boundaries and boundaries[-1]["end_secs"] is not None:
                boundaries[-1]["end_secs"] = new_total
                boundaries[-1]["end_byte"] = len(pcm)
        # ──────────────────────────────────────────────────────────────

        duration = _pcm_duration(pcm)
        if duration < 30:  # less than 30 seconds — probably not a real side
            print(f"[album-rec] Recording too short ({duration:.1f}s) — discarding")
            return None, 0.0, []

        # Build filename and encode
        filename = make_album_audio_filename(
            self.album_info.get("artist", "Unknown"),
            self.album_info.get("title", "Unknown Album"),
            self.side,
        )
        output_path = ALBUM_AUDIO_DIR / filename

        # Avoid overwriting
        counter = 1
        base = output_path.stem
        while output_path.exists():
            output_path = ALBUM_AUDIO_DIR / f"{base} ({counter}).flac"
            counter += 1

        metadata = {
            "title":  f"{self.album_info.get('title', 'Unknown')} - Side {self.side}",
            "artist": self.album_info.get("artist", "Unknown"),
            "album":  self.album_info.get("title", "Unknown Album"),
            "year":   self.album_info.get("year", ""),
            "genre":  self.album_info.get("genre", ""),
            "disc":   self.side,
        }

        print(f"[album-rec] Encoding FLAC: {output_path.name} ({duration:.0f}s)")

        if not encode_flac(pcm, output_path, metadata):
            print("[album-rec] FLAC encoding failed!")
            return None, 0.0, []

        if not output_path.exists():
            print("[album-rec] Output file missing after encode")
            return None, 0.0, []

        size_mb = output_path.stat().st_size / (1024 * 1024)
        print(f"[album-rec] ✓ Saved {output_path.name} ({duration:.0f}s, {size_mb:.1f} MB)")

        return output_path, duration, boundaries

    def cancel(self):
        """Discard the recording without saving."""
        with self._lock:
            self._active = False
            self._chunks = []
            self._total_bytes = 0
        print("[album-rec] Recording cancelled")


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
