#!/usr/bin/env python3
"""Vinyl AirPlay: album-recording finalization engine.

Auto-finalizes a recorded side (encode FLAC, save, advance) plus the stream-stall
watchdog. Stops capture by signalling state.stop_event and
state.listen_stop_event so the coordinators own cleanup runs (no import of the
route layer). Shares AppState via app_state.
"""

import asyncio
import time
import traceback
from contextlib import suppress

import catalog as cat
import recorder as rec
from app_state import broadcast, state


async def _auto_finalize_album_side():
    """Called when RecordingBuffer detects end-of-side silence during album recording.
    Encodes the current side to FLAC and notifies the UI."""
    try:
        await _auto_finalize_album_side_inner()
    except Exception as e:
        print(f"[auto-finalize] ERROR: {e}")
        traceback.print_exc()
        await broadcast("album_recording_status", {
            "recording": False,
            "error": True,
            "message": f"Auto-finalize failed: {e}",
        })


async def _auto_finalize_album_side_inner():
    ar = state.album_recorder
    if not ar or not ar.is_active:
        return

    album_id = ar.album_id
    side = ar.side
    # Keep album_recorder around (but inactive after finish()) so the
    # flip endpoint can read album_id from it.  The audio callback
    # already checks is_active before calling put(), so no PCM is fed.
    _stop_stall_watchdog()

    # Stop learn session for this side
    if state.learn_session:
        state.learn_session.active = False
        state.learn_session = None
    if state.recogniser:
        state.recogniser.set_learning_mode(False)

    await broadcast("album_recording_status", {
        "recording": False,
        "album_id": album_id,
        "side": side,
        "message": f"End of Side {side} detected: encoding FLAC…",
    })

    loop = asyncio.get_event_loop()
    path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

    if path:
        file_size = path.stat().st_size
        cat.save_album_audio(album_id, side, str(path), duration, file_size)

        for b in boundaries:
            if b["track_id"] and b["end_secs"] is not None:
                cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

        # Sanity-check boundaries against catalog durations and correct if needed
        cat.correct_side_boundaries(album_id, side, duration)

        # Check if album has more sides to record
        all_tracks = cat.get_album_tracks(album_id)
        album_sides = sorted({t.get("side") or "A" for t in all_tracks})
        current_idx = album_sides.index(side) if side in album_sides else -1
        has_next_side = current_idx >= 0 and current_idx < len(album_sides) - 1
        next_side = album_sides[current_idx + 1] if has_next_side else None

        await broadcast("album_recording_side_saved", {
            "album_id": album_id,
            "side": side,
            "duration_secs": round(duration, 1),
            "size_mb": round(file_size / (1024 * 1024), 1),
            "tracks_captured": len(boundaries),
            "has_next_side": has_next_side,
            "next_side": next_side,
        })

        if has_next_side:
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "message": f"Side {side} saved: {duration:.0f}s, "
                           f"{file_size / (1024*1024):.1f} MB. Flip and record Side {next_side} when ready.",
            })
        else:
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "message": f"Side {side} saved: {duration:.0f}s, "
                           f"{file_size / (1024*1024):.1f} MB. All sides complete!",
            })
            # Last side done: clean up recorder and stop capture. Signal both
            # the stream and listen coordinators (no import of the route
            # layer); the capture manager tears down the shared recogniser and
            # rec_buffer when the last consumer detaches. Suppress
            # auto-restart for 60s.
            state.album_recorder = None
            if state.stop_event:
                state.stop_event.set()
            if state.listen_stop_event:
                state.listen_stop_event.set()
            if state.stream_task:
                state.stream_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await state.stream_task
                state.stream_task = None
            state.manual_stop_until = time.monotonic() + 60.0

        print(f"[album-rec] Auto-finalized Side {side}: {duration:.0f}s, "
              f"{file_size / (1024*1024):.1f} MB"
              f"{' (last side)' if not has_next_side else ''}")
    else:
        await broadcast("album_recording_status", {
            "recording": False,
            "message": "Side too short or encoding failed",
        })


async def _encode_and_save_album_side(ar: rec.AlbumRecorder):
    """Finalize a recorded side and persist it in the background."""
    album_id = ar.album_id
    side = ar.side
    loop = asyncio.get_event_loop()

    state.album_encoding.update({
        "in_progress": True,
        "album_id": album_id,
        "side": side,
        "started_at": time.time(),
        "finished_at": None,
        "ok": None,
        "message": f"Encoding Side {side}...",
    })

    try:
        path, duration, boundaries = await loop.run_in_executor(None, ar.finish)

        if not path:
            msg = "Recording too short or encoding failed"
            state.album_encoding.update({
                "in_progress": False,
                "finished_at": time.time(),
                "ok": False,
                "message": msg,
            })
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "error": True,
                "message": msg,
            })
            return

        file_size = path.stat().st_size
        cat.save_album_audio(album_id, side, str(path), duration, file_size)

        for b in boundaries:
            if b["track_id"] and b["end_secs"] is not None:
                cat.update_track_timestamps(b["track_id"], b["start_secs"], b["end_secs"])

        cat.correct_side_boundaries(album_id, side, duration)

        all_tracks = cat.get_album_tracks(album_id)
        album_sides = sorted({t.get("side") or "A" for t in all_tracks})
        current_idx = album_sides.index(side) if side in album_sides else -1
        has_next_side = current_idx >= 0 and current_idx < len(album_sides) - 1
        next_side = album_sides[current_idx + 1] if has_next_side else None

        done_msg = f"Side {side} saved: {duration:.0f}s, {file_size / (1024*1024):.1f} MB"
        state.album_encoding.update({
            "in_progress": False,
            "finished_at": time.time(),
            "ok": True,
            "message": done_msg,
        })

        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "side": side,
            "message": done_msg,
        })
        await broadcast("album_recording_side_saved", {
            "album_id": album_id,
            "side": side,
            "duration_secs": round(duration, 1),
            "size_mb": round(file_size / (1024 * 1024), 1),
            "tracks_captured": len(boundaries),
            "has_next_side": has_next_side,
            "next_side": next_side,
        })
    except Exception as e:
        state.album_encoding.update({
            "in_progress": False,
            "finished_at": time.time(),
            "ok": False,
            "message": f"Encoding failed: {e}",
        })
        await broadcast("album_recording_status", {
            "recording": False,
            "album_id": album_id,
            "side": side,
            "error": True,
            "message": f"Encoding failed: {e}",
        })


async def _stream_stall_watchdog():
    """Periodically checks whether the audio stream has stopped delivering data.
    USB audio interfaces can silently die after an input overflow (ALSA/URB errors).
    When detected, flushes accumulated audio and finalizes the recording so the
    user isn't left staring at a frozen timer."""
    POLL_INTERVAL = 3.0  # seconds between checks
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            rb = state.rec_buffer
            if not rb or not rb.is_active:
                continue
            if not rb.stream_stalled:
                continue

            print("[watchdog] Audio stream stall detected: no data for "
                  f"{rec.STREAM_STALL_SECS:.0f}s. Flushing recording.")

            # Force-flush any accumulated audio as the final track
            if rb._total_bytes > 0:
                rb._silence_start_byte = rb._total_bytes
                rb._silence_secs = 0.0
                rb._split_track()

            # Fire end-of-side to finalize album recording
            if rb._on_end_of_side:
                rb._on_end_of_side()

            # Notify UI about the error
            album_id = state.album_recorder.album_id if state.album_recorder else None
            side = state.album_recorder.side if state.album_recorder else None
            await broadcast("album_recording_status", {
                "recording": False,
                "album_id": album_id,
                "side": side,
                "error": True,
                "message": "Audio stream lost (USB/hardware glitch). "
                           "Recording saved with what was captured. "
                           "Try recording this side again.",
            })
            break  # watchdog's job is done for this recording session
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[watchdog] Error: {e}")


def _start_stall_watchdog():
    """Start the audio stream stall watchdog (idempotent)."""
    if state.stall_watchdog_task and not state.stall_watchdog_task.done():
        return  # already running
    state.stall_watchdog_task = asyncio.ensure_future(_stream_stall_watchdog())


def _stop_stall_watchdog():
    """Cancel the stall watchdog."""
    if state.stall_watchdog_task and not state.stall_watchdog_task.done():
        state.stall_watchdog_task.cancel()
    state.stall_watchdog_task = None
