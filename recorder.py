#!/usr/bin/env python3
"""
Vinyl AirPlay — Audio Recorder
Detects track boundaries via silence gaps, captures full album sides
as FLAC files with track boundary timestamps.
"""

import io
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
SIGNAL_DECAY_RATE = 0.0004          # decay rate when below threshold — 5× slower than adapt
                                    # prevents signal level from getting stuck high on dynamic albums
SILENCE_FORGIVE_SECS = 0.3          # ignore above-threshold blips shorter than this during a gap
                                    # vinyl pops/crackles shouldn't reset the silence counter
SILENCE_MIN_SECS  = 1.5             # silence must last this long to split track
                                    # reduced from 2.0 — some albums have short inter-track gaps
EARLY_SPLIT_RATIO  = 0.75           # suppress silence splits until 75% of expected duration elapsed
                                    # lets time-based fallback handle albums with long vinyl gaps
END_OF_SIDE_SECS  = 20.0            # silence this long = end of side — auto-flush final track
                                    # _split_track trims to silence_start+pad so no long silence
                                    # is appended to the file
SILENCE_PAD_SECS  = 0.5            # keep this much silence at end of track (natural fade)
MIN_TRACK_SECS    = 15              # ignore tracks shorter than this (needle drop, interludes)
STARTUP_AUDIO_SECS = 2.0            # sustained audio required before silence detection begins
DURATION_SPLIT_TOLERANCE = 10.0     # seconds past expected track duration before forcing a split
                                    # fallback for albums with seamless transitions (no silence gaps)


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
        # Forgiveness window: brief above-threshold blips don't reset silence counter
        self._above_thresh_secs = 0.0      # how long audio has been above threshold

        # Duration-based fallback for seamless albums (no silence between tracks)
        self._expected_durations: list[float] = []   # per-track expected durations (seconds)
        self._duration_track_idx = 0                 # which expected track we're on
        self._track_elapsed_secs = 0.0               # seconds since last split

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
            self._above_thresh_secs    = 0.0
            self._duration_track_idx   = 0
            self._track_elapsed_secs   = 0.0
            # Keep _expected_durations — set externally before start()
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

    def set_expected_durations(self, durations: list[float]):
        """Set expected track durations (from Discogs) for time-based fallback splitting."""
        self._expected_durations = [d for d in durations if d > 0]
        if self._expected_durations:
            print(f"[recorder] Expected track durations set: "
                  f"{[f'{d:.0f}s' for d in self._expected_durations]}")

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
            self._above_thresh_secs = 0.0  # reset above-threshold counter
            if self._silence_start_byte == 0:
                with self._lock:
                    self._silence_start_byte = self._total_bytes - len(pcm_chunk)
            # Slowly decay signal level even during silence — prevents threshold
            # from getting stuck high on dynamic albums where quiet music sits
            # below an inflated threshold from earlier loud passages.
            self._signal_level -= SIGNAL_DECAY_RATE * self._signal_level
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
            self._above_thresh_secs += chunk_secs
            # Forgiveness: brief above-threshold blips (vinyl pops, crackles) during
            # a gap shouldn't reset the silence counter. Require sustained audio
            # to confirm the gap is actually over.
            if self._above_thresh_secs >= SILENCE_FORGIVE_SECS:
                # Genuine audio returned — finalize any pending silence
                self._silence_log_countdown = 0  # reset so next gap logs immediately
                self._end_of_side_fired = False  # reset if audio returns
                # Determine whether silence-based splitting is allowed.
                # When we have expected durations and we're well short of the
                # expected track length, suppress silence-based splits entirely
                # and let the time-based fallback handle it instead. This
                # prevents false splits from long inter-track gaps on vinyl.
                allow_silence_split = True
                min_silence = SILENCE_MIN_SECS
                if (self._expected_durations
                        and self._duration_track_idx < len(self._expected_durations)):
                    expected = self._expected_durations[self._duration_track_idx]
                    if expected > 0 and self._track_elapsed_secs < expected * EARLY_SPLIT_RATIO:
                        allow_silence_split = False  # too early — let time-based fallback handle it
                if allow_silence_split and self._silence_secs >= min_silence:
                    # Sustained silence ended — split track
                    self._split_track()
                self._silence_secs       = 0.0
                self._silence_start_byte = 0
            # else: blip is too short, keep accumulating silence

        # ── Duration-based fallback for seamless albums ──────────────────
        # If we know the expected track durations (from Discogs) and silence
        # detection hasn't triggered a split, force one after the expected
        # duration + tolerance. This handles albums where tracks blend
        # into each other with no silence gaps.
        if (self._expected_durations
                and self._audio_seen
                and self._duration_track_idx < len(self._expected_durations)):
            self._track_elapsed_secs += chunk_secs
            expected = self._expected_durations[self._duration_track_idx]
            if expected > 0 and self._track_elapsed_secs >= expected + DURATION_SPLIT_TOLERANCE:
                print(f"[recorder] Time-based split: {self._track_elapsed_secs:.1f}s "
                      f"elapsed (expected ~{expected:.0f}s + {DURATION_SPLIT_TOLERANCE:.0f}s tolerance)")
                # Cut at the current position (no silence to trim to)
                with self._lock:
                    self._silence_start_byte = self._total_bytes
                self._silence_secs = 0.0
                self._split_track()

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
        if self.remaining_tracks > 0:
            self.remaining_tracks -= 1
        # Reset duration-based tracking after a real split
        self._track_elapsed_secs = 0.0
        if self._expected_durations and self._duration_track_idx < len(self._expected_durations):
            self._duration_track_idx += 1
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


# ── FLAC Encoding ─────────────────────────────────────────────────────────────

DEFAULT_AUDIO_DIR = Path(__file__).parent / "album_audio"


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

    def __init__(self, album_id: int, side: str, album_info: dict,
                 audio_dir: Path = None):
        self._lock = threading.Lock()
        self.album_id = album_id
        self.side = side
        self.album_info = album_info  # {artist, title, year, genre, ...}
        self._audio_dir = (audio_dir or DEFAULT_AUDIO_DIR).resolve()

        self._chunks: list[bytes] = []
        self._total_bytes = 0
        self._active = True

        # Track boundary tracking
        self._track_boundaries: list[dict] = []  # [{track_id, start_byte, start_secs}]
        self._current_track_start_byte = 0

        # Startup gate — same idea as RecordingBuffer: don't count silence
        self._audio_started = False
        self.on_audio_detected = None  # callback when first audio arrives

        self._audio_dir.mkdir(parents=True, exist_ok=True)
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
        output_path = self._audio_dir / filename

        # Avoid overwriting
        counter = 1
        base = output_path.stem
        while output_path.exists():
            output_path = self._audio_dir / f"{base} ({counter}).flac"
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

