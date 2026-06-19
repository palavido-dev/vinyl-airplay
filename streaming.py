#!/usr/bin/env python3
"""Vinyl AirPlay: live-capture stream coordinator + audio-only listen mode.

Drives the capture InputStream: the auto-stream watcher (idle RMS polling),
run_stream / _run_stream_inner (AirPlay + local + Bluetooth + HTTP fan-out with
recognition and recording wired in), and the listen mode used during learn
sessions. Shares AppState via app_state.
"""

import asyncio
import math
import threading
import time
import traceback
from contextlib import suppress

import numpy as np
import pyatv
import sounddevice as sd
from pyatv.interface import MediaMetadata

import catalog as cat
import recorder as rec
from app_state import broadcast, state
from audio_streams import AsyncAudioStream, LocalOutputStream, make_callback, run_device_stream
from device_helpers import _capture_channels, _get_local_outputs
from recognition import _art_jpeg, _make_on_match, _make_on_unknown
from recording_engine import _auto_finalize_album_side

SAMPLE_RATE   = 44100
BLOCK_SIZE    = 8192
INPUT_LATENCY = 0.5


async def _auto_stream_watcher():
    """
    Poll Scarlett RMS while idle; auto-start stream when record plays.

    Opens the InputStream only when NOT streaming, and closes it the moment
    streaming starts: this prevents ALSA 'device busy' errors when run_stream
    opens its own InputStream.
    """
    RMS_THRESHOLD = 0.008
    SUSTAIN_SECS  = 2.0
    POLL_SECS     = 1.0     # longer interval: open/close device each cycle
    COOLDOWN_SECS = 15.0
    POLL_FRAMES   = int(44100 * POLL_SECS)

    print("[auto-stream] Watcher started")
    sustained = 0.0
    cooldown  = 0.0

    try:
        while True:
            await asyncio.sleep(POLL_SECS)

            # While streaming, just count down cooldown: don't touch the device
            if state.is_streaming:
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            # Also skip while listen mode or album recording has the device open
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            # Skip while catalog playback is active (or starting up)
            if state.player_task or (state.player and state.player.state != "stopped"):
                sustained = 0.0
                cooldown  = COOLDOWN_SECS
                continue

            if cooldown > 0:
                cooldown = max(0.0, cooldown - POLL_SECS)
                continue

            # Open device, read one chunk, close immediately: never holds it open
            # Re-check right before open (race condition: listen/album may have started)
            if state.listen_task or (state.album_recorder and state.album_recorder.is_active):
                sustained = 0.0
                cooldown = COOLDOWN_SECS
                continue
            audio_idx = state.settings.get("audio_device_index")
            try:
                with sd.InputStream(device=audio_idx, samplerate=44100,
                                    channels=_capture_channels(audio_idx), dtype="float32",
                                    blocksize=POLL_FRAMES) as stream:
                    data, _ = stream.read(POLL_FRAMES)
                rms = float(np.sqrt(np.mean(data[:, :min(2, data.shape[1])] ** 2)))
            except Exception as e:
                # Suppress noisy errors when something else has the device
                if not (state.listen_task or state.is_streaming
                        or (state.album_recorder and state.album_recorder.is_active)):
                    print(f"[auto-stream] Read error: {e}")
                await asyncio.sleep(5.0)
                continue

            if rms >= RMS_THRESHOLD:
                sustained += POLL_SECS
                if sustained >= SUSTAIN_SECS:
                    sustained = 0.0
                    if time.monotonic() < state.manual_stop_until:
                        print("[auto-stream] Suppressed: manual stop cooldown active")
                        continue
                    dev = state.settings.get("auto_stream_device")
                    if not dev:
                        print("[auto-stream] Audio detected but no default device set in Settings")
                        continue
                    volume = state.settings.get("volume", 80)
                    aidx   = state.settings.get("audio_device_index")
                    print(f"[auto-stream] Starting stream to {dev.get('name')} (RMS={rms:.4f})")
                    await broadcast("auto_stream_starting", {
                        "device":  dev.get("name"),
                        "message": f"Auto-stream: starting to {dev.get('name')}…"
                    })
                    state.stream_task = asyncio.create_task(
                        run_stream([dev], aidx, volume)
                    )
                    cooldown = COOLDOWN_SECS
            else:
                sustained = 0.0

    except asyncio.CancelledError:
        print("[auto-stream] Watcher stopped")
    except Exception as e:
        print(f"[auto-stream] Watcher error: {type(e).__name__}: {e}")


async def _restart_auto_stream_watcher():
    if state.auto_stream_task and not state.auto_stream_task.done():
        state.auto_stream_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.auto_stream_task
        state.auto_stream_task = None
    if state.settings.get("auto_stream_enabled"):
        state.auto_stream_task = asyncio.create_task(_auto_stream_watcher())
        print("[auto-stream] Watcher (re)started")
    else:
        print("[auto-stream] Disabled")



async def run_stream(targets, audio_device_index, volume):
    try:
        await _run_stream_inner(targets, audio_device_index, volume)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[airplay] run_stream crashed: {e}")
        traceback.print_exc()
        await broadcast("error", {"message": f"Stream error: {e}"})
        state.is_streaming = False
        state.active_devices = []
        state.stop_event = None
        state.airplay_metadata = None
        await broadcast("status", {"streaming": False, "message": "Stopped (error)"})


async def _run_stream_inner(targets, audio_device_index, volume):
    state.stop_event     = asyncio.Event()
    main_loop            = asyncio.get_event_loop()

    # Separate local vs Bluetooth vs AirPlay targets
    local_targets      = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets  = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    airplay_targets    = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:"))]

    # A2DP supports only one device at a time
    if len(bluetooth_targets) > 1:
        print("[bluetooth] Multiple BT devices selected; using first only")
        bluetooth_targets = bluetooth_targets[:1]

    # Create a shared mutable MediaMetadata object: passed once to stream_file
    # and updated in-place via on_match() whenever a new track is identified
    np = state.now_playing or {}
    state.airplay_metadata = MediaMetadata(
        title  = np.get("track_title"),
        artist = np.get("track_artist") or np.get("album_artist"),
        album  = np.get("album_title"),
        artwork= _art_jpeg(np) if np else None,
    )

    # Set up AirPlay devices (if any). storage= attaches saved
    # credentials to the conf services so pyatv.connect doesn't have
    # to re-pair every time.
    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs      = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    # Set up local output streams
    local_streams = []
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            print(f"[local-out] No ALSA device for {lt.get('id')}, skipping")
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[local-out] Failed to open {alsa_dev}: {e}")

    # Set up Bluetooth output streams
    bt_streams = []
    for bt in bluetooth_targets:
        address = bt.get("address") or bt.get("id", "").replace("bt:", "")
        if not address:
            print(f"[bluetooth] No address for {bt.get('id')}, skipping")
            continue
        alsa_dev = f"bluealsa:DEV={address},PROFILE=a2dp"
        try:
            bts = LocalOutputStream(alsa_dev)
            bts.start()
            bt_streams.append(bts)
            print(f"[bluetooth] Opened stream to {address}")
        except Exception as e:
            print(f"[bluetooth] Failed to open {address}: {e}")

    http_only = False
    if not confs and not local_streams and not bt_streams:
        if state.settings.get("http_stream_enabled"):
            http_only = True
            print("[http-stream] No playback targets selected; running capture for /live.mp3 only")
        else:
            await broadcast("error", {"message": "No paired devices found on network"})
            state.stop_event=None
            return

    # Only mark streaming=True now that we have confirmed devices
    state.is_streaming   = True
    state.active_devices = [d["name"] for d in targets] if targets else ["HTTP MP3"]
    status_message = (
        "Streaming (HTTP MP3 live URL active)"
        if http_only
        else f"Streaming to {len(confs) + len(local_streams) + len(bt_streams)} device(s)"
    )
    await broadcast("status", {
        "streaming": True, "devices": state.active_devices,
        "message": status_message
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    active_count  = len(confs)
    threads_done  = asyncio.Event()
    if active_count == 0:
        threads_done.set()  # No AirPlay threads: mark done immediately

    def on_device_done(name, err):
        nonlocal active_count
        if err:
            asyncio.run_coroutine_threadsafe(
                broadcast("error", {"message": f"{name}: {err}"}), main_loop
            )
        active_count -= 1
        if active_count <= 0:
            main_loop.call_soon_threadsafe(threads_done.set)

    for conf in confs:
        threading.Thread(
            target=run_device_stream,
            args=(conf, audio_streams[conf.identifier], volume, on_device_done),
            daemon=True
        ).start()

    # Init recorder buffer
    def _on_track_ready(pcm, duration):
        """Called by RecordingBuffer when silence gap detected: new track starting."""
        # Always reset recogniser on track boundary, even when not recording
        if state.recogniser and not (state.learn_session and state.learn_session.active):
            state.recogniser.reset_match()

    def _on_level(rms):
        state.rec_level = rms
        # Broadcast real-time input level to all WebSocket clients using main_loop
        try:
            db = 20 * math.log10(max(rms, 1e-8))
            asyncio.run_coroutine_threadsafe(
                broadcast("level", {"db": db, "rms": rms}),
                main_loop
            )
        except Exception:
            pass

    def _on_audio_detected():
        """Fires when startup gate opens: needle dropped, new side starting."""
        # Reset recogniser so it starts fresh for the first track
        if state.recogniser and not (state.learn_session and state.learn_session.active):
            state.recogniser.reset_match()
        if state.learn_session and state.learn_session.active:
            s = state.learn_session
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_audio_detected", {
                    "learned":     s.learned,
                    "track_count": s.track_count,
                    "next_track":  s.next_track_name(),
                }),
                main_loop
            )

    def _on_end_of_side():
        """Fires after END_OF_SIDE_SECS of silence: final track flushed, gate re-armed."""
        # Auto-finalize album recording if active
        if state.album_recorder and state.album_recorder.is_active:
            asyncio.run_coroutine_threadsafe(
                _auto_finalize_album_side(), main_loop)

        if state.learn_session and state.learn_session.active:
            s = state.learn_session
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_end_of_side", {
                    "learned":     s.learned,
                    "track_count": s.track_count,
                    "message":     "End of side detected: last track saved. "
                                   + ("Flip the record and press Continue."
                                      if s.pending_tracks else "All tracks learned!"),
                }),
                main_loop
            )

    state.rec_buffer = rec.RecordingBuffer(
        on_track_ready     = _on_track_ready,
        on_level_update    = _on_level,
        on_audio_detected  = _on_audio_detected,
        on_end_of_side     = _on_end_of_side,
        auto_split         = True,
    )

    # Start recogniser
    state.fp_buffer.clear()
    state.recogniser = cat.Recogniser(
        buffer           = state.fp_buffer,
        on_match         = _make_on_match(main_loop),
        on_unknown       = _make_on_unknown(main_loop),
    )
    state.recogniser.start()

    callback = make_callback(list(audio_streams.values()) + local_streams + bt_streams, state.eq, state.fp_buffer)

    try:
        with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                            channels=_capture_channels(audio_device_index), dtype="float32",
                            blocksize=BLOCK_SIZE, latency=INPUT_LATENCY,
                            callback=callback):
            stop_task    = asyncio.create_task(state.stop_event.wait())
            if confs:
                threads_task = asyncio.create_task(threads_done.wait())
                done, pending = await asyncio.wait(
                    [stop_task, threads_task], return_when=asyncio.FIRST_COMPLETED
                )
            else:
                # Local-only: just wait for stop
                await stop_task
                pending = set()
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        if state.recogniser:
            state.recogniser.set_auto_learn_album(None)
            state.recogniser.stop()
            state.recogniser = None
        if state.rec_buffer and state.rec_buffer.is_active:
            state.rec_buffer.stop()
        state.rec_buffer = None
        state.now_playing = None
        for s in audio_streams.values():
            s.stop()
        for lo in local_streams:
            lo.stop()
        for bts in bt_streams:
            bts.stop()
        state.is_streaming = False
        state.active_devices = []
        state.stop_event = None
        await broadcast("status",      {"streaming": False, "message": "Stopped"})
        await broadcast("now_playing", {"track_title": None})


async def _start_listen_mode():
    """Open sounddevice input without AirPlay streaming: for learning/recording only."""
    if state.is_streaming or state.listen_task:
        return  # already running
    audio_device_index = int(state.settings.get("audio_device_index") or 0)
    loop = asyncio.get_event_loop()

    def _on_track_ready(pcm, dur):
        if state.learn_session and state.learn_session.active:
            state.learn_executor.submit(state.learn_session.on_track_captured, pcm)

    def _on_level(rms):
        db = 20 * np.log10(rms + 1e-9)
        asyncio.run_coroutine_threadsafe(
            broadcast("level", {"rms": round(rms, 5), "db": round(db, 1)}), loop)

    def _on_audio_detected():
        asyncio.run_coroutine_threadsafe(broadcast("audio_detected", {}), loop)

    def _on_end_of_side():
        # Auto-finalize album recording if active
        if state.album_recorder and state.album_recorder.is_active:
            asyncio.run_coroutine_threadsafe(
                _auto_finalize_album_side(), loop)

        if state.learn_session:
            asyncio.run_coroutine_threadsafe(
                broadcast("learn_end_of_side", {
                    "learned": state.learn_session.learned,
                    "track_count": state.learn_session.track_count,
                    "message": "End of side: flip record and press Continue.",
                }), loop)

    state.rec_buffer = rec.RecordingBuffer(
        on_track_ready    = _on_track_ready,
        on_level_update   = _on_level,
        on_audio_detected = _on_audio_detected,
        on_end_of_side    = _on_end_of_side,
        auto_split        = True,
    )
    state.fp_buffer.clear()
    state.recogniser = cat.Recogniser(
        buffer           = state.fp_buffer,
        on_match         = _make_on_match(loop),
        on_unknown       = _make_on_unknown(loop),
    )
    state.recogniser.start()
    stop_event = asyncio.Event()
    state.stop_event = stop_event

    async def _run():
        callback = make_callback({}, state.eq, state.fp_buffer)
        try:
            with sd.InputStream(device=audio_device_index, samplerate=SAMPLE_RATE,
                                channels=_capture_channels(audio_device_index), dtype="float32",
                                blocksize=BLOCK_SIZE, latency=INPUT_LATENCY,
                                callback=callback):
                print("[listen] Audio-only mode started")
                await broadcast("status", {"streaming": False, "listening": True,
                                           "message": "Listening (no AirPlay)"})
                await stop_event.wait()
        except Exception as e:
            print(f"[listen] ERROR: {e}")
            traceback.print_exc()
        finally:
            if state.recogniser:
                state.recogniser.stop()
                state.recogniser = None
            if state.rec_buffer and state.rec_buffer.is_active:
                state.rec_buffer.stop()
            state.rec_buffer  = None
            state.stop_event  = None
            state.listen_task = None
            print("[listen] Audio-only mode stopped")
            await broadcast("status", {"streaming": False, "listening": False, "message": "Stopped"})

    state.listen_task = asyncio.create_task(_run())


def _stop_listen_mode():
    if state.stop_event and state.listen_task:
        state.stop_event.set()


def _ensure_audio_active() -> bool:
    return state.is_streaming or (state.listen_task is not None)
