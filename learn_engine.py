#!/usr/bin/env python3
"""Vinyl AirPlay: learn-session state machine.

Tracks which tracks on a side still need fingerprint learning during an album
recording and drives the learn-progress broadcasts. Shares AppState via app_state.
"""

import asyncio

import catalog as cat
from app_state import broadcast, state


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
