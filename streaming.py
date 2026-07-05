#!/usr/bin/env python3
"""Vinyl AirPlay: live-capture stream coordinator + audio-only listen mode.

Owns the capture InputStream through CaptureManager (one shared open; consumers
attach/detach, so streaming and recording can run at once): the auto-stream
watcher (idle RMS polling), run_stream / _run_stream_inner (AirPlay + local +
Bluetooth + HTTP fan-out with recognition and recording wired in), and the
listen mode used during learn and recording sessions. Shares AppState via
app_state.
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
from audio_streams import AsyncAudioStream, LocalOutputStream, _browser_streams, make_callback, run_device_stream
from device_helpers import _capture_channels, _get_local_outputs
from recognition import _art_jpeg, _make_on_match, _make_on_unknown
from recording_engine import _auto_finalize_album_side

SAMPLE_RATE   = 44100
BLOCK_SIZE    = 8192
INPUT_LATENCY = 0.5


def _poll_capture(device, frames):
    """Open the capture device briefly and return one block of float32 frames.

    Runs in a worker thread (blocking read) so it never stalls the event loop.
    The caller holds state.capture_lock for the duration.
    """
    with sd.InputStream(device=device, samplerate=SAMPLE_RATE,
                        channels=_capture_channels(device), dtype="float32",
                        blocksize=frames) as stream:
        data, _ = stream.read(frames)
    return data


def _open_input_stream(device, callback):
    """Open and start the main capture InputStream, retrying briefly if ALSA has
    not finished releasing the device from a previous open.

    Runs in a worker thread. The caller holds state.capture_lock and is
    responsible for stop()/close() on the returned stream.
    """
    last_err = None
    for _ in range(4):
        try:
            stream = sd.InputStream(device=device, samplerate=SAMPLE_RATE,
                                    channels=_capture_channels(device), dtype="float32",
                                    blocksize=BLOCK_SIZE, latency=INPUT_LATENCY,
                                    callback=callback)
            stream.start()
            return stream
        except sd.PortAudioError as e:
            last_err = e
            time.sleep(0.25)
    raise last_err


class _SinkSet:
    """Thread-safe fan-out list for the capture callback.

    Consumers register their output sinks under a token; the audio callback
    iterates an immutable snapshot, so attach/detach never races the hot loop.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}
        self._snapshot = ()

    def add(self, token, sinks):
        with self._lock:
            self._by_token[token] = list(sinks)
            self._rebuild()

    def remove(self, token):
        with self._lock:
            self._by_token.pop(token, None)
            self._rebuild()

    def _rebuild(self):
        flat = []
        for sinks in self._by_token.values():
            flat.extend(sinks)
        self._snapshot = tuple(flat)

    def __iter__(self):
        return iter(self._snapshot)


class CaptureManager:
    """Single owner of the ALSA capture InputStream (issue #42).

    ALSA capture is exclusive, so streaming and listen/recording used to fight
    over the device. Now every consumer attaches to one shared InputStream:
    the first attach opens the device and creates the shared RecordingBuffer +
    Recogniser, later attaches just register their output sinks, and the last
    detach tears everything down.
    """

    def __init__(self):
        self._alock = asyncio.Lock()   # serializes attach/detach
        self._stream = None
        self._device = None
        self._tokens = set()
        self._sinks = _SinkSet()

    @property
    def active(self) -> bool:
        return self._stream is not None

    async def attach(self, token, sinks, device_index):
        """Register a consumer; opens the capture stream on first attach."""
        async with self._alock:
            if self._stream is not None and device_index != self._device:
                print(f"[capture] Device {device_index} requested but capture "
                      f"already open on {self._device}; reusing open device")
            self._sinks.add(token, sinks)
            self._tokens.add(token)
            if self._stream is None:
                try:
                    await self._open(device_index)
                except Exception:
                    self._sinks.remove(token)
                    self._tokens.discard(token)
                    raise

    async def detach(self, token) -> bool:
        """Unregister a consumer. Returns True when this was the last consumer
        and the capture stream was fully torn down."""
        async with self._alock:
            self._sinks.remove(token)
            self._tokens.discard(token)
            if self._tokens or self._stream is None:
                return False
            await self._close()
            return True

    async def _open(self, device_index):
        loop = asyncio.get_running_loop()
        got = await asyncio.to_thread(state.capture_lock.acquire, True, 5.0)
        if not got:
            raise RuntimeError("Capture device busy: could not acquire within 5s")
        try:
            self._make_shared_consumers(loop)
            callback = make_callback(self._sinks, state.eq, state.fp_buffer)
            self._stream = await asyncio.to_thread(_open_input_stream, device_index, callback)
            self._device = device_index
            print(f"[capture] Opened shared capture on device {device_index}")
        except Exception:
            self._teardown_shared_consumers()
            state.capture_lock.release()
            raise

    async def _close(self):
        stream, self._stream = self._stream, None
        self._device = None
        with suppress(Exception):
            await asyncio.to_thread(stream.stop)
            await asyncio.to_thread(stream.close)
        state.capture_lock.release()
        self._teardown_shared_consumers()
        state.now_playing = None
        print("[capture] Shared capture closed")

    def _make_shared_consumers(self, loop):
        """Create the shared RecordingBuffer + Recogniser that every capture
        consumer uses. Album recording overrides _on_track_ready dynamically."""

        def _on_track_ready(pcm, duration):
            # Learn sessions consume the captured track; otherwise a track
            # boundary just resets the recogniser for the next track.
            if state.learn_session and state.learn_session.active:
                state.learn_executor.submit(state.learn_session.on_track_captured, pcm)
            elif state.recogniser:
                state.recogniser.reset_match()

        def _on_level(rms):
            state.rec_level = rms
            try:
                db = 20 * math.log10(max(rms, 1e-8))
                asyncio.run_coroutine_threadsafe(
                    broadcast("level", {"db": db, "rms": rms}), loop)
            except Exception:
                pass

        def _on_audio_detected():
            """Fires when startup gate opens: needle dropped, new side starting."""
            if state.recogniser and not (state.learn_session and state.learn_session.active):
                state.recogniser.reset_match()
            asyncio.run_coroutine_threadsafe(broadcast("audio_detected", {}), loop)
            if state.learn_session and state.learn_session.active:
                s = state.learn_session
                asyncio.run_coroutine_threadsafe(
                    broadcast("learn_audio_detected", {
                        "learned":     s.learned,
                        "track_count": s.track_count,
                        "next_track":  s.next_track_name(),
                    }), loop)

        def _on_end_of_side():
            """Fires after END_OF_SIDE_SECS of silence: final track flushed."""
            if state.album_recorder and state.album_recorder.is_active:
                asyncio.run_coroutine_threadsafe(_auto_finalize_album_side(), loop)
            if state.learn_session and state.learn_session.active:
                s = state.learn_session
                asyncio.run_coroutine_threadsafe(
                    broadcast("learn_end_of_side", {
                        "learned":     s.learned,
                        "track_count": s.track_count,
                        "message":     "End of side detected: last track saved. "
                                       + ("Flip the record and press Continue."
                                          if s.pending_tracks else "All tracks learned!"),
                    }), loop)

        state.rec_buffer = rec.RecordingBuffer(
            on_track_ready    = _on_track_ready,
            on_level_update   = _on_level,
            on_audio_detected = _on_audio_detected,
            on_end_of_side    = _on_end_of_side,
            auto_split        = True,
            gate_threshold    = state.settings.get("audio_detect_threshold", 0.006),
        )
        state.fp_buffer.clear()
        state.recogniser = cat.Recogniser(
            buffer     = state.fp_buffer,
            on_match   = _make_on_match(loop),
            on_unknown = _make_on_unknown(loop),
        )
        state.recogniser.start()

    def _teardown_shared_consumers(self):
        if state.recogniser:
            state.recogniser.set_auto_learn_album(None)
            state.recogniser.stop()
            state.recogniser = None
        if state.rec_buffer and state.rec_buffer.is_active:
            state.rec_buffer.stop()
        state.rec_buffer = None


capture = CaptureManager()


async def _auto_stream_watcher():
    """
    Poll Scarlett RMS while idle; auto-start stream when record plays.

    Opens the InputStream only when NOT streaming, and closes it the moment
    streaming starts: this prevents ALSA 'device busy' errors when run_stream
    opens its own InputStream.
    """
    RMS_THRESHOLD = state.settings.get("audio_detect_threshold", 0.006)
    SUSTAIN_SECS  = 2.0
    POLL_SECS     = 1.0     # longer interval: open/close device each cycle
    COOLDOWN_SECS = 15.0
    POLL_FRAMES   = int(44100 * 0.2)   # 0.2s read: hold the capture device only briefly

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
            # Non-blocking grab of the shared capture device: skip this poll if a
            # stream / listen / recording session holds it. The read runs in a
            # worker thread so it never stalls the event loop.
            if not state.capture_lock.acquire(blocking=False):
                continue
            try:
                data = await asyncio.to_thread(_poll_capture, audio_idx, POLL_FRAMES)
            except Exception as e:
                data = None
                # Suppress noisy errors when something else has the device
                if not (state.listen_task or state.is_streaming
                        or (state.album_recorder and state.album_recorder.is_active)):
                    print(f"[auto-stream] Read error: {e}")
            finally:
                state.capture_lock.release()
            if data is None:
                await asyncio.sleep(5.0)
                continue
            rms = float(np.sqrt(np.mean(data[:, :min(2, data.shape[1])] ** 2)))

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

    # Separate local vs Bluetooth vs browser vs AirPlay targets
    local_targets      = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets  = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    browser_targets    = [t for t in targets if str(t.get("id", "")).startswith("browser")]
    airplay_targets    = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:", "browser"))]

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
    local_failures = []
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            print(f"[local-out] No ALSA device for {lt.get('id')}, skipping")
            local_failures.append(f"{lt.get('name') or lt.get('id')}: not found")
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[local-out] Failed to open {alsa_dev}: {e}")
            local_failures.append(f"{lt.get('name') or alsa_dev}: {e}")

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

    # Set up browser output streams ("This Device" in the picker). The
    # frontend creates the stream via /api/stream/create and passes its id
    # as browser:<stream_id>; we attach that stream as a live sink, same as
    # the catalog player does.
    browser_streams = []
    for br in browser_targets:
        stream_id = str(br.get("id", "")).replace("browser:", "", 1)
        bs_obj = _browser_streams.get(stream_id) if stream_id != "browser" else None
        if bs_obj:
            browser_streams.append(bs_obj)
            print(f"[browser-stream] Added live vinyl sink {stream_id}")
        else:
            print(f"[browser-stream] No browser stream for target {br.get('id')}: "
                  "frontend must call /api/stream/create first")

    http_only = False
    if not confs and not local_streams and not bt_streams and not browser_streams:
        if state.settings.get("http_stream_enabled"):
            http_only = True
            print("[http-stream] No playback targets selected; running capture for /live.mp3 only")
        else:
            # Tell the user what actually went wrong: a local output that
            # failed to open is not a network discovery problem
            if local_failures:
                msg = "Local output failed: " + "; ".join(local_failures)
            elif airplay_targets:
                msg = "No paired devices found on network"
            else:
                msg = "No output devices available"
            await broadcast("error", {"message": msg})
            state.stop_event=None
            return

    # Only mark streaming=True now that we have confirmed devices
    state.is_streaming   = True
    state.active_devices = [d["name"] for d in targets] if targets else ["HTTP MP3"]
    status_message = (
        "Streaming (HTTP MP3 live URL active)"
        if http_only
        else f"Streaming to {len(confs) + len(local_streams) + len(bt_streams) + len(browser_streams)} device(s)"
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

    # Attach to the shared capture stream: opens the device (plus the shared
    # RecordingBuffer and Recogniser) on first attach, otherwise just adds
    # this stream's output sinks to the existing capture.
    token = object()
    sinks = list(audio_streams.values()) + local_streams + bt_streams + browser_streams
    attached = False
    try:
        await capture.attach(token, sinks, audio_device_index)
        attached = True
        stop_task    = asyncio.create_task(state.stop_event.wait())
        if confs:
            threads_task = asyncio.create_task(threads_done.wait())
            _done, pending = await asyncio.wait(
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
        full_stop = False
        if attached:
            full_stop = await capture.detach(token)
        for s in audio_streams.values():
            s.stop()
        for lo in local_streams:
            lo.stop()
        for bts in bt_streams:
            bts.stop()
        for brs in browser_streams:
            brs.stop()
        state.is_streaming = False
        state.active_devices = []
        state.stop_event = None
        await broadcast("status", {"streaming": False,
                                   "listening": state.listen_task is not None,
                                   "message": "Stopped"})
        if full_stop:
            # Capture fully torn down: clear now-playing in the UI. When
            # listen/recording still holds the capture, recognition continues.
            await broadcast("now_playing", {"track_title": None})


async def _start_listen_mode():
    """Ensure audio capture is running for learning/recording, with no output
    sinks of its own. Attaches to the shared capture, so it can run alongside
    streaming: this gives a recording its own capture reference, and stopping
    the stream then leaves the recording running (issue #42)."""
    if state.listen_task:
        return  # already running
    audio_device_index = int(state.settings.get("audio_device_index") or 0)

    stop_event = asyncio.Event()
    state.listen_stop_event = stop_event

    async def _run():
        token = object()
        attached = False
        try:
            await capture.attach(token, [], audio_device_index)
            attached = True
            print("[listen] Audio-only mode started")
            if not state.is_streaming:
                # When attached alongside a stream, keep the streaming status
                await broadcast("status", {"streaming": False, "listening": True,
                                           "message": "Listening (no AirPlay)"})
            await stop_event.wait()
        except Exception as e:
            print(f"[listen] ERROR: {e}")
            traceback.print_exc()
        finally:
            if attached:
                await capture.detach(token)
            state.listen_stop_event = None
            state.listen_task = None
            print("[listen] Audio-only mode stopped")
            await broadcast("status", {"streaming": state.is_streaming, "listening": False,
                                       "message": "Streaming" if state.is_streaming else "Stopped"})

    state.listen_task = asyncio.create_task(_run())


def _stop_listen_mode():
    if state.listen_stop_event and state.listen_task:
        state.listen_stop_event.set()


def _ensure_audio_active() -> bool:
    return state.is_streaming or (state.listen_task is not None)
