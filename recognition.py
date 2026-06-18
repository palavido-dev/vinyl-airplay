#!/usr/bin/env python3
Artwork URL/JPEG helpers and the on_match/on_unknown factories that the stream
coordinator and player engine wire into the recogniser. Shares AppState via app_state.
"""

import asyncio
from pathlib import Path

import catalog as cat
from app_state import broadcast, state


# ── Recognition Callbacks ─────────────────────────────────────────────────────

def _art_url(track: dict) -> str | None:
    art = track.get("user_artwork_path") or track.get("artwork_path")
    return f"/artwork/{Path(art).name}" if art else None


def _art_jpeg(track: dict) -> bytes | None:
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
        # Update shared MediaMetadata in-place: RAOP sender picks up changes live
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
