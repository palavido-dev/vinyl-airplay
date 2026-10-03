#!/usr/bin/env python3
"""Vinyl AirPlay: learn-session state machine.

Tracks which tracks on a side still need fingerprint learning during an album
recording and drives the learn-progress broadcasts. Shares AppState via app_state.
"""

import asyncio
import os
import tempfile
import threading
from pathlib import Path

import catalog as cat
from app_state import broadcast, state

# Cap in-flight learn jobs so a slow fpcalc cannot queue unbounded PCM blobs.
MAX_LEARN_INFLIGHT = 2
_learn_inflight = 0
_learn_inflight_lock = threading.Lock()


def submit_learn_pcm(pcm: bytes, session: "LearnSession | None" = None) -> int | None | bool:
    """Spill PCM to a temp file and queue fingerprinting with backpressure.

    On success, returns the next pending track id (for album boundary marking).
    Returns False if the backlog is full; None if there was nothing to claim.
    """
    global _learn_inflight
    sess = session or state.learn_session
    if not sess or not sess.active or pcm is None:
        return None
    with _learn_inflight_lock:
        if _learn_inflight >= MAX_LEARN_INFLIGHT:
            print("[learn] Backlog full — dropping track PCM to protect Pi RAM")
            return False
        _learn_inflight += 1

    claimed = sess.claim_for_learn()
    if claimed is None:
        with _learn_inflight_lock:
            _learn_inflight -= 1
        return None

    next_boundary_id = sess.next_track_id()

    fd, path = tempfile.mkstemp(prefix="learn-", suffix=".pcm")
    try:
        os.write(fd, pcm)
    finally:
        os.close(fd)

    def _run(p=path, track=claimed):
        global _learn_inflight
        try:
            data = Path(p).read_bytes()
            sess.on_track_captured(data, claimed_track=track)
        finally:
            Path(p).unlink(missing_ok=True)
            with _learn_inflight_lock:
                _learn_inflight -= 1

    state.learn_executor.submit(_run)
    return next_boundary_id


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
        self._lock       = threading.Lock()

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
        with self._lock:
            if self.pending_tracks:
                return self.pending_tracks[0]["id"]
            return None

    def next_track_name(self) -> str:
        with self._lock:
            if self.pending_tracks:
                t = self.pending_tracks[0]
                return f"{t.get('side','')}{t.get('track_number','')}: {t['title']}"
            return "Unknown"

    def claim_for_learn(self) -> dict | None:
        """Atomically take the next pending track before async fingerprinting.

        Callers should mark the *new* boundary with next_track_id() after this
        returns, so the upcoming track gets the correct id.
        """
        with self._lock:
            if not self.pending_tracks:
                return None
            return self.pending_tracks.pop(0)

    def on_track_captured(self, pcm: bytes, claimed_track: dict | None = None):
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
            # Put the claimed track back so a retry can pick it up.
            if claimed_track is not None:
                with self._lock:
                    self.pending_tracks.insert(0, claimed_track)
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
        if claimed_track is not None:
            track_id = claimed_track["id"]
            just_learned_name = (
                f"{claimed_track.get('side','')}{claimed_track.get('track_number','')}: "
                f"{claimed_track['title']}"
            )
        else:
            # Legacy path (tests / direct call): claim now.
            claimed_track = self.claim_for_learn()
            if claimed_track is None:
                print("[learn] No more unlearned tracks: stopping session")
                self.active = False
                asyncio.run_coroutine_threadsafe(
                    broadcast("learn_done", {"learned": self.learned, "message": "All tracks already learned!"}),
                    self._loop
                )
                return
            track_id = claimed_track["id"]
            just_learned_name = (
                f"{claimed_track.get('side','')}{claimed_track.get('track_number','')}: "
                f"{claimed_track['title']}"
            )

        rows = cat.save_track_fingerprints(track_id, raw_ints, duration)
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
