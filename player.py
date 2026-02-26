#!/usr/bin/env python3
"""
Vinyl AirPlay — Catalog Playback Engine

Decodes recorded album FLAC files and feeds PCM at real-time rate
into AirPlay streams (or any list of AsyncAudioStream-like objects).

Flow:
  1. Build playlist from album_audio records + track timestamps
  2. Decode FLAC → raw PCM via ffmpeg subprocess pipe
  3. Feed PCM chunks through EQ → AsyncAudioStreams at real-time rate
  4. Track position and fire Now Playing callbacks at track boundaries
  5. Support pause/resume/stop/seek/next/prev
"""

import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# ── Audio Constants (must match main.py) ─────────────────────────────────────

SAMPLE_RATE  = 44100
CHANNELS     = 2
BITS         = 16
BYTES_PER_SAMPLE = CHANNELS * (BITS // 8)      # 4 bytes per frame
BYTES_PER_SEC    = SAMPLE_RATE * BYTES_PER_SAMPLE  # 176,400 bytes/sec

# Feed chunks of this many frames at a time (~93ms per chunk)
CHUNK_FRAMES = 4096
CHUNK_BYTES  = CHUNK_FRAMES * BYTES_PER_SAMPLE  # 16,384 bytes
CHUNK_SECS   = CHUNK_FRAMES / SAMPLE_RATE       # ~0.0929s


# ── Playlist Entry ───────────────────────────────────────────────────────────

class PlaylistEntry:
    """One side of an album (one FLAC file)."""

    def __init__(self, audio_path: str, side: str, duration_secs: float,
                 tracks: list[dict]):
        self.audio_path    = audio_path
        self.side          = side
        self.duration_secs = duration_secs
        # tracks: list of {id, title, artist, start_secs, end_secs, track_number}
        # sorted by start_secs
        self.tracks = sorted(tracks, key=lambda t: t.get("start_secs") or 0)


# ── Player ───────────────────────────────────────────────────────────────────

class Player:
    """
    Decodes album FLAC files and feeds PCM to AirPlay streams.

    Callbacks:
      on_track_change(track_info: dict)  — fired when current track changes
      on_status_change(status: dict)     — fired on play/pause/stop/position updates
      on_finished()                      — fired when playlist ends
    """

    def __init__(self, eq, streams: list,
                 on_track_change: Optional[Callable] = None,
                 on_status_change: Optional[Callable] = None,
                 on_finished: Optional[Callable] = None):
        self.eq       = eq
        self.streams  = streams  # list of AsyncAudioStream objects

        self._on_track_change  = on_track_change or (lambda t: None)
        self._on_status_change = on_status_change or (lambda s: None)
        self._on_finished      = on_finished or (lambda: None)

        self.playlist: list[PlaylistEntry] = []
        self.album_id: Optional[int] = None
        self.album_info: Optional[dict] = None  # {title, artist, year, artwork_path, ...}

        self._side_idx     = 0        # current index into playlist
        self._position     = 0.0      # seconds into current side
        self._current_track_idx = -1  # index into current side's tracks list

        self._state        = "stopped"  # stopped | playing | paused
        self._lock         = threading.Lock()
        self._pause_event  = threading.Event()
        self._stop_event   = threading.Event()
        self._feed_thread: Optional[threading.Thread] = None
        self._ffmpeg: Optional[subprocess.Popen] = None

        self._pause_event.set()  # start unpaused

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def position_secs(self) -> float:
        return self._position

    @property
    def current_side(self) -> Optional[str]:
        if 0 <= self._side_idx < len(self.playlist):
            return self.playlist[self._side_idx].side
        return None

    @property
    def current_track(self) -> Optional[dict]:
        if 0 <= self._side_idx < len(self.playlist):
            entry = self.playlist[self._side_idx]
            if 0 <= self._current_track_idx < len(entry.tracks):
                return entry.tracks[self._current_track_idx]
        return None

    @property
    def duration_secs(self) -> float:
        """Duration of current side."""
        if 0 <= self._side_idx < len(self.playlist):
            return self.playlist[self._side_idx].duration_secs
        return 0.0

    def get_status(self) -> dict:
        track = self.current_track
        return {
            "state":         self._state,
            "album_id":      self.album_id,
            "side":          self.current_side,
            "position_secs": round(self._position, 1),
            "duration_secs": round(self.duration_secs, 1),
            "track_id":      track["id"] if track else None,
            "track_title":   track["title"] if track else None,
            "side_index":    self._side_idx,
            "side_count":    len(self.playlist),
        }

    # ── Playback Control ─────────────────────────────────────────────────────

    def play(self, album_id: int, album_info: dict,
             playlist: list[PlaylistEntry], start_track_id: Optional[int] = None):
        """Start playing an album. Stops any current playback first."""
        self.stop()

        self.album_id   = album_id
        self.album_info = album_info
        self.playlist   = playlist

        if not playlist:
            print("[player] Empty playlist — nothing to play")
            return

        # Find starting position
        self._side_idx = 0
        start_pos = 0.0

        if start_track_id:
            for si, entry in enumerate(playlist):
                for t in entry.tracks:
                    if t["id"] == start_track_id:
                        self._side_idx = si
                        start_pos = t.get("start_secs") or 0.0
                        break

        self._position = start_pos
        self._current_track_idx = -1
        self._stop_event.clear()
        self._pause_event.set()
        self._state = "playing"

        self._feed_thread = threading.Thread(
            target=self._feed_loop,
            name="player-feed",
            daemon=True,
        )
        self._feed_thread.start()

        self._on_status_change(self.get_status())

    def pause(self):
        if self._state == "playing":
            self._state = "paused"
            self._pause_event.clear()
            self._on_status_change(self.get_status())

    def resume(self):
        if self._state == "paused":
            self._state = "playing"
            self._pause_event.set()
            self._on_status_change(self.get_status())

    def toggle_pause(self):
        if self._state == "playing":
            self.pause()
        elif self._state == "paused":
            self.resume()

    def stop(self):
        if self._state == "stopped":
            return
        self._state = "stopped"
        self._stop_event.set()
        self._pause_event.set()  # unblock pause so thread can exit
        self._kill_ffmpeg()
        if self._feed_thread and self._feed_thread.is_alive():
            self._feed_thread.join(timeout=3)
        self._feed_thread = None
        self._position = 0
        self._current_track_idx = -1
        self._on_status_change(self.get_status())

    def seek_to(self, position_secs: float):
        """Seek to an absolute position within the current side."""
        if not self.playlist:
            return
        with self._lock:
            self._seek_target = max(0.0, position_secs)
            self._seek_requested = True

    def seek_to_track(self, track_id: int):
        """Jump to a specific track by ID."""
        for si, entry in enumerate(self.playlist):
            for t in entry.tracks:
                if t["id"] == track_id:
                    if si != self._side_idx:
                        # Different side — restart on that side
                        self._change_side(si, t.get("start_secs") or 0.0)
                    else:
                        self.seek_to(t.get("start_secs") or 0.0)
                    return

    def next_track(self):
        """Skip to next track."""
        if not self.playlist:
            return
        entry = self.playlist[self._side_idx]
        next_idx = self._current_track_idx + 1

        if next_idx < len(entry.tracks):
            pos = entry.tracks[next_idx].get("start_secs") or 0.0
            self.seek_to(pos)
        elif self._side_idx + 1 < len(self.playlist):
            # Next side
            self._change_side(self._side_idx + 1, 0.0)
        else:
            # End of album
            self.stop()
            self._on_finished()

    def prev_track(self):
        """Go to previous track (or start of current if >3s in)."""
        if not self.playlist:
            return
        entry = self.playlist[self._side_idx]
        current = self.current_track

        if current:
            track_start = current.get("start_secs") or 0.0
            # If more than 3 seconds into the track, restart it
            if self._position - track_start > 3.0:
                self.seek_to(track_start)
                return

        prev_idx = self._current_track_idx - 1
        if prev_idx >= 0:
            pos = entry.tracks[prev_idx].get("start_secs") or 0.0
            self.seek_to(pos)
        elif self._side_idx > 0:
            # Previous side, last track
            prev_entry = self.playlist[self._side_idx - 1]
            if prev_entry.tracks:
                pos = prev_entry.tracks[-1].get("start_secs") or 0.0
                self._change_side(self._side_idx - 1, pos)
            else:
                self._change_side(self._side_idx - 1, 0.0)
        else:
            # Already at start — restart first track
            self.seek_to(0.0)

    # ── Internal: Side Management ────────────────────────────────────────────

    def _change_side(self, new_side_idx: int, start_pos: float = 0.0):
        """Switch to a different side (kills ffmpeg, restarts feed loop)."""
        with self._lock:
            self._side_change_target = (new_side_idx, start_pos)
            self._side_change_requested = True
            # Signal the feed loop to check
            self._kill_ffmpeg()

    # ── Internal: ffmpeg Decode ──────────────────────────────────────────────

    def _start_ffmpeg(self, audio_path: str, start_secs: float = 0.0) -> bool:
        """Start ffmpeg decoding FLAC → raw s16le PCM pipe."""
        self._kill_ffmpeg()

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if start_secs > 0:
            cmd += ["-ss", f"{start_secs:.3f}"]
        cmd += [
            "-i", audio_path,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "pipe:1",
        ]

        try:
            self._ffmpeg = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            return True
        except Exception as e:
            print(f"[player] ffmpeg start failed: {e}")
            self._ffmpeg = None
            return False

    def _kill_ffmpeg(self):
        if self._ffmpeg:
            try:
                self._ffmpeg.kill()
                self._ffmpeg.wait(timeout=2)
            except Exception:
                pass
            self._ffmpeg = None

    # ── Internal: Track Boundary Detection ───────────────────────────────────

    def _check_track_boundary(self):
        """Check if position has crossed into a new track, fire callback."""
        if not self.playlist:
            return
        entry = self.playlist[self._side_idx]
        pos   = self._position

        if not entry.tracks:
            if self._current_track_idx == -1:
                print(f"[player] WARNING: No tracks for Side {entry.side}")
                self._current_track_idx = -2  # avoid repeated warnings
            return

        # Find which track we're in
        new_idx = -1
        for i, t in enumerate(entry.tracks):
            start = t.get("start_secs") or 0.0
            end   = t.get("end_secs") or entry.duration_secs
            if start <= pos < end:
                new_idx = i
                break

        # If no track matched (might be in gap or before first track),
        # try finding the closest preceding track
        if new_idx == -1:
            for i in range(len(entry.tracks) - 1, -1, -1):
                if (entry.tracks[i].get("start_secs") or 0.0) <= pos:
                    new_idx = i
                    break

        # Still no match — default to first track (common when timestamps
        # are offsets from stream start, not from FLAC file start)
        if new_idx == -1 and entry.tracks:
            new_idx = 0

        if new_idx != self._current_track_idx and new_idx >= 0:
            self._current_track_idx = new_idx
            track = entry.tracks[new_idx]
            info = {
                "track_id":     track["id"],
                "track_title":  track["title"],
                "track_artist": track.get("artist") or self.album_info.get("artist", ""),
                "album_id":     self.album_id,
                "album_title":  self.album_info.get("title", ""),
                "album_artist": self.album_info.get("artist", ""),
                "year":         self.album_info.get("year"),
                "artwork_path":      self.album_info.get("artwork_path"),
                "user_artwork_path": self.album_info.get("user_artwork_path"),
                "side":         entry.side,
            }
            print(f"[player] Now playing: {track['title']} "
                  f"(Side {entry.side}, track {new_idx + 1})")
            self._on_track_change(info)

    # ── Internal: Feed Loop ──────────────────────────────────────────────────

    def _feed_loop(self):
        """
        Main playback thread. Decodes FLAC and feeds PCM to streams
        at real-time rate.
        """
        self._seek_requested = False
        self._seek_target = 0.0
        self._side_change_requested = False
        self._side_change_target = (0, 0.0)

        while not self._stop_event.is_set() and self._side_idx < len(self.playlist):
            entry = self.playlist[self._side_idx]
            print(f"[player] Starting Side {entry.side}: {entry.audio_path} "
                  f"(at {self._position:.1f}s, {len(entry.tracks)} tracks)")

            if not Path(entry.audio_path).exists():
                print(f"[player] File not found: {entry.audio_path}")
                self._side_idx += 1
                self._position = 0.0
                continue

            if not self._start_ffmpeg(entry.audio_path, self._position):
                print(f"[player] Failed to start ffmpeg for {entry.audio_path}")
                self._side_idx += 1
                self._position = 0.0
                continue

            # Fire initial track boundary check
            self._check_track_boundary()

            # Real-time feed loop
            t_start   = time.monotonic()
            pos_start = self._position
            status_ticker = 0

            while not self._stop_event.is_set():
                # Handle pause
                if not self._pause_event.is_set():
                    t_paused = time.monotonic()
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break
                    # Adjust timing baseline for time spent paused
                    t_start += time.monotonic() - t_paused

                # Handle side change request
                with self._lock:
                    if self._side_change_requested:
                        self._side_change_requested = False
                        new_si, new_pos = self._side_change_target
                        self._side_idx = new_si
                        self._position = new_pos
                        self._current_track_idx = -1
                        self._kill_ffmpeg()
                        break  # restart outer loop on new side

                # Handle seek within current side
                with self._lock:
                    if self._seek_requested:
                        self._seek_requested = False
                        target = min(self._seek_target, entry.duration_secs)
                        self._position = target
                        self._current_track_idx = -1
                        pos_start = target
                        t_start = time.monotonic()
                        self._kill_ffmpeg()
                        if not self._start_ffmpeg(entry.audio_path, target):
                            break
                        self._check_track_boundary()
                        continue

                # Read a chunk from ffmpeg
                try:
                    data = self._ffmpeg.stdout.read(CHUNK_BYTES)
                except Exception:
                    data = b""

                if not data:
                    # Side finished
                    break

                # Pad partial final chunk with silence
                if len(data) < CHUNK_BYTES:
                    data += b'\x00' * (CHUNK_BYTES - len(data))

                # Update position
                self._position = pos_start + (time.monotonic() - t_start)

                # Apply EQ
                audio_f32 = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS).astype(np.float32) / 32767.0
                processed = self.eq.process(audio_f32)
                pcm_out   = (processed * 32767).astype(np.int16).tobytes()

                # Feed all streams
                for stream in self.streams:
                    stream.put(pcm_out)

                # Check track boundaries
                self._check_track_boundary()

                # Broadcast position every ~1 second
                status_ticker += 1
                if status_ticker % 11 == 0:  # ~11 chunks/sec × 1 ≈ 1s
                    self._on_status_change(self.get_status())

                # Rate limit: sleep to maintain real-time pace
                # Target time for this chunk based on bytes fed
                elapsed  = time.monotonic() - t_start
                expected = self._position - pos_start
                # Use a small lead (feed slightly ahead) so AirPlay buffer stays full
                ahead = elapsed - expected
                if ahead < -0.02:
                    # We're behind — don't sleep, catch up
                    pass
                else:
                    # Sleep to maintain real-time rate
                    sleep_time = CHUNK_SECS - ahead
                    if sleep_time > 0.001:
                        time.sleep(sleep_time)

            self._kill_ffmpeg()

            # If we're stopped or side-changed, don't auto-advance
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._side_change_requested:
                    continue

            # Auto-advance to next side
            self._side_idx += 1
            self._position = 0.0
            self._current_track_idx = -1

            if self._side_idx < len(self.playlist):
                print(f"[player] Auto-advancing to Side {self.playlist[self._side_idx].side}")
                self._on_status_change(self.get_status())
            # Loop continues with next side

        # Playback finished
        if not self._stop_event.is_set():
            self._state = "stopped"
            print("[player] Playlist finished")
            self._on_finished()
            self._on_status_change(self.get_status())
