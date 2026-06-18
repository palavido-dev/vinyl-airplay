#!/usr/bin/env python3
"""Vinyl AirPlay: catalog playback engine.

Plays recorded album FLACs back to AirPlay/local/Bluetooth outputs via plr.Player,
with track-boundary now-playing updates. Shares AppState via app_state.
"""

import asyncio
import threading
from contextlib import suppress

import pyatv
from pyatv.interface import MediaMetadata

import catalog as cat
import player as plr
from app_state import broadcast, state
from audio_streams import AsyncAudioStream, LocalOutputStream, _browser_streams, run_device_stream
from device_helpers import _get_local_outputs
from recognition import _art_jpeg, _art_url


async def _stop_playback():
    """Stop any active catalog playback and clean up AirPlay connections."""
    if state.player:
        state.player.stop()
        state.player = None
    if state.player_task:
        state.player_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.player_task
        state.player_task = None


async def _run_playback(album_id: int, targets: list[dict], volume: int,
                        start_track_id: int | None = None,
                        resume_position_secs: float | None = None):
    """
    Connect to AirPlay devices and run catalog playback.
    Similar to _run_stream_inner but feeds from FLAC files instead of sounddevice.
    """
    main_loop = asyncio.get_event_loop()

    # Build playlist from album_audio records + track timestamps
    audio_files = cat.get_album_audio(album_id)
    if not audio_files:
        await broadcast("error", {"message": "No recorded audio for this album"})
        return

    albums = cat.get_all_albums()
    album_info = next((a for a in albums if a["id"] == album_id), None)
    if not album_info:
        await broadcast("error", {"message": "Album not found"})
        return

    all_tracks = cat.get_album_tracks(album_id)

    playlist = []
    for af in audio_files:
        side = af["side"]
        # Get tracks for this side with timestamps
        side_tracks = [
            {
                "id":           t["id"],
                "title":        t["title"],
                "artist":       t.get("artist") or album_info["artist"],
                "track_number": t["track_number"],
                "start_secs":   t.get("start_secs"),
                "end_secs":     t.get("end_secs"),
            }
            for t in all_tracks
            if t["side"] == side
        ]

        # Normalize timestamps: shift so first track starts at 0
        # (DB stores offsets from stream start, not FLAC file start)
        if side_tracks:
            first_start = min(
                (t["start_secs"] for t in side_tracks if t["start_secs"] is not None),
                default=0,
            )
            if first_start > 5.0:  # only shift if offset is significant
                print(f"[player] Side {side}: normalizing timestamps, "
                      f"shifting by -{first_start:.1f}s")
                for t in side_tracks:
                    if t["start_secs"] is not None:
                        t["start_secs"] -= first_start
                    if t["end_secs"] is not None:
                        t["end_secs"] -= first_start
        print(f"[player] Side {side}: {len(side_tracks)} tracks, "
              f"audio={af['file_path']}")
        if side_tracks:
            print(f"[player]   first track: {side_tracks[0]['title']} "
                  f"start={side_tracks[0].get('start_secs')} "
                  f"end={side_tracks[0].get('end_secs')}")
        playlist.append(plr.PlaylistEntry(
            audio_path    = af["file_path"],
            side          = side,
            duration_secs = af.get("duration_secs") or 0,
            tracks        = side_tracks,
            album_id      = album_id,
            album_title   = album_info["title"],
            album_artist  = album_info["artist"],
            artwork_path  = album_info.get("user_artwork_path") or album_info.get("artwork_path"),
        ))

    if not playlist:
        await broadcast("error", {"message": "No audio files found"})
        return

    # Create AirPlay metadata object for Now Playing display on devices
    state.airplay_metadata = MediaMetadata(
        title=None, artist=None, album=album_info.get("title"), artwork=None,
    )

    # Separate local / Bluetooth / browser / AirPlay targets
    local_targets     = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    browser_targets   = [t for t in targets if str(t.get("id", "")).startswith("browser:")]
    airplay_targets   = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:", "browser:"))]
    if len(bluetooth_targets) > 1:
        bluetooth_targets = bluetooth_targets[:1]

    # Scan and connect to AirPlay devices. storage= so saved creds attach.
    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs      = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    if not confs and not local_targets and not bluetooth_targets and not browser_targets:
        await broadcast(
            "error", {"message": "No paired devices found on network"}
        )
        state.airplay_metadata = None
        return

    n_devices = (
        len(confs) + len(local_targets)
        + len(bluetooth_targets) + len(browser_targets)
    )
    await broadcast("player_status", {
        "state": "loading",
        "album_id": album_id,
        "message": f"Connecting to {n_devices} device(s)…",
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    local_streams = []

    # Set up local output streams
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            print(f"[player] No ALSA device for {lt.get('id')}, skipping")
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[player] Failed to open local device {alsa_dev}: {e}")

    # Set up Bluetooth output streams
    bt_streams = []
    for bt in bluetooth_targets:
        address = bt.get("address") or bt.get("id", "").replace("bt:", "")
        if not address:
            continue
        alsa_dev = f"bluealsa:DEV={address},PROFILE=a2dp"
        try:
            bts = LocalOutputStream(alsa_dev)
            bts.start()
            bt_streams.append(bts)
        except Exception as e:
            print(f"[player] Failed to open BT device {address}: {e}")

    # Set up browser output streams
    browser_streams = []
    for br in browser_targets:
        stream_id = br.get("id", "").replace("browser:", "")
        if stream_id and stream_id in _browser_streams:
            browser_streams.append(_browser_streams[stream_id])
            print(f"[player] Added browser stream {stream_id}")


    # Always include HTTP MP3 stream if enabled
    all_streams = list(audio_streams.values()) + local_streams + bt_streams + browser_streams
    if state.settings.get("http_stream_enabled", False):
        all_streams.append(state.live_mp3)

    if not all_streams:
        await broadcast("error", {"message": "No output devices available"})
        state.airplay_metadata = None
        return

    active_count  = len(confs)
    threads_done  = asyncio.Event()
    if active_count == 0:
        threads_done.set()  # No AirPlay threads to wait for

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
            daemon=True,
        ).start()

    # Player callbacks
    def on_track_change(track_info):
        state.now_playing = track_info
        if state.airplay_metadata is not None:
            state.airplay_metadata.title   = track_info.get("track_title")
            state.airplay_metadata.artist  = (
                track_info.get("track_artist") or track_info.get("album_artist")
            )
            state.airplay_metadata.album   = track_info.get("album_title")
            state.airplay_metadata.artwork = _art_jpeg(track_info)
        asyncio.run_coroutine_threadsafe(broadcast("now_playing", {
            "track_title":  track_info.get("track_title"),
            "track_artist": track_info.get("track_artist"),
            "album_title":  track_info.get("album_title"),
            "album_artist": track_info.get("album_artist"),
            "year":         track_info.get("year"),
            "album_id":     track_info.get("album_id"),
            "track_id":     track_info.get("track_id"),
            "artwork_url":  _art_url(track_info),
            "source":       "player",
        }), main_loop)
        # Log the play
        if track_info.get("track_id") and track_info.get("album_id"):
            cat.log_play(track_info["track_id"], track_info["album_id"])

    def on_status_change(status):
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", status), main_loop
        )

    def on_finished():
        state.now_playing = None
        asyncio.run_coroutine_threadsafe(
            broadcast("now_playing", {"track_title": None}), main_loop
        )
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", {"state": "stopped", "album_id": album_id}),
            main_loop,
        )

    # Create and start the player
    player = plr.Player(
        eq              = state.eq,
        streams         = all_streams,
        on_track_change = on_track_change,
        on_status_change= on_status_change,
        on_finished     = on_finished,
    )
    state.player = player
    player.set_crossfade(state.settings.get("crossfade_secs", 0))

    player.play(album_id, album_info, playlist, start_track_id=start_track_id)

    # If resuming mid-track, seek to exact position
    if resume_position_secs is not None and resume_position_secs > 0:
        player.seek_to(resume_position_secs)
        print(f"[player] Resuming at {resume_position_secs:.1f}s")

    try:
        # Wait for either: player finishes, devices disconnect, or external stop
        while player.state != "stopped":
            if threads_done.is_set() and not local_streams and not bt_streams and not browser_streams:
                # All AirPlay devices disconnected and no local/BT/browser fallback
                player.stop()
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        player.stop()
    finally:
        for s in audio_streams.values():
            s.stop()
        for s in local_streams:
            s.stop()
        for s in bt_streams:
            s.stop()
        for s in browser_streams:
            s.stop()
        state.player = None
        state.player_task = None
        state.now_playing = None
        state.airplay_metadata = None
        await broadcast("player_status", {"state": "stopped"})
        await broadcast("now_playing", {"track_title": None})


async def _run_playback_queue(album_id: int, album_info: dict,
                              playlist: list, targets: list[dict], volume: int):
    """
    Run playback from a pre-built playlist (used by play-queue for multi-album).
    Reuses the same device-connection and player logic as _run_playback.
    """
    main_loop = asyncio.get_event_loop()

    if not playlist:
        await broadcast("error", {"message": "No audio files found"})
        return

    state.airplay_metadata = MediaMetadata(
        title=None, artist=None, album=album_info.get("title"), artwork=None,
    )

    local_targets      = [t for t in targets if str(t.get("id", "")).startswith("local:")]
    bluetooth_targets  = [t for t in targets if str(t.get("id", "")).startswith("bt:")]
    browser_targets    = [t for t in targets if str(t.get("id", "")).startswith("browser:")]
    airplay_targets    = [t for t in targets if not str(t.get("id", "")).startswith(("local:", "bt:", "browser:"))]

    confs = []
    if airplay_targets:
        if state.atv_storage is not None:
            await state.atv_storage.load()
        found = await pyatv.scan(
            main_loop, timeout=7, storage=state.atv_storage,
        )
        id_to_conf = {d.identifier: d for d in found}
        confs = [id_to_conf[t["id"]] for t in airplay_targets if t["id"] in id_to_conf]

    if not confs and not local_targets and not bluetooth_targets and not browser_targets:
        await broadcast(
            "error", {"message": "No paired devices found on network"}
        )
        state.airplay_metadata = None
        return

    n_albums = len({e.album_id for e in playlist if e.album_id})
    await broadcast("player_status", {
        "state": "loading",
        "album_id": album_id,
        "message": f"Connecting: {n_albums} album(s) queued…",
    })

    audio_streams = {conf.identifier: AsyncAudioStream() for conf in confs}
    local_streams = []
    for lt in local_targets:
        alsa_dev = lt.get("alsa_device")
        if not alsa_dev:
            local_devs = {d["id"]: d for d in _get_local_outputs()}
            info = local_devs.get(lt["id"])
            if info:
                alsa_dev = info["alsa_device"]
        if not alsa_dev:
            continue
        try:
            lo = LocalOutputStream(alsa_dev)
            lo.start()
            local_streams.append(lo)
        except Exception as e:
            print(f"[player] Failed to open local device {alsa_dev}: {e}")

    bt_streams = []
    if bluetooth_targets:
        bt_target = bluetooth_targets[0]  # A2DP: max one BT device
        bt_addr = str(bt_target.get("id", "")).replace("bt:", "")
        if bt_addr:
            try:
                bt_lo = LocalOutputStream(f"bluealsa:DEV={bt_addr},PROFILE=a2dp")
                bt_lo.start()
                bt_streams.append(bt_lo)
                print(f"[player-playlist] Bluetooth stream started → {bt_addr}")
            except Exception as e:
                print(f"[player-playlist] Failed to open BT device {bt_addr}: {e}")

    browser_streams = []
    for br in browser_targets:
        stream_id = br.get("id", "").replace("browser:", "")
        if stream_id and stream_id in _browser_streams:
            browser_streams.append(_browser_streams[stream_id])
            print(f"[player-playlist] Added browser stream {stream_id}")

    all_streams = list(audio_streams.values()) + local_streams + bt_streams + browser_streams
    if not all_streams:
        await broadcast("error", {"message": "No output devices available"})
        state.airplay_metadata = None
        return

    active_count = len(confs)
    threads_done = asyncio.Event()
    if active_count == 0:
        threads_done.set()

    def on_device_done(name, err):
        nonlocal active_count
        if err:
            asyncio.run_coroutine_threadsafe(
                broadcast("error", {"message": f"{name}: {err}"}), main_loop)
        active_count -= 1
        if active_count <= 0:
            main_loop.call_soon_threadsafe(threads_done.set)

    for conf in confs:
        threading.Thread(
            target=run_device_stream,
            args=(conf, audio_streams[conf.identifier], volume, on_device_done),
            daemon=True,
        ).start()

    def on_track_change(track_info):
        state.now_playing = track_info
        if state.airplay_metadata is not None:
            state.airplay_metadata.title = track_info.get("track_title")
            state.airplay_metadata.artist = (
                track_info.get("track_artist") or track_info.get("album_artist"))
            state.airplay_metadata.album = track_info.get("album_title")
            state.airplay_metadata.artwork = _art_jpeg(track_info)
        asyncio.run_coroutine_threadsafe(broadcast("now_playing", {
            "track_title":  track_info.get("track_title"),
            "track_artist": track_info.get("track_artist"),
            "album_title":  track_info.get("album_title"),
            "album_artist": track_info.get("album_artist"),
            "year":         track_info.get("year"),
            "album_id":     track_info.get("album_id"),
            "track_id":     track_info.get("track_id"),
            "artwork_url":  _art_url(track_info),
            "source":       "player",
        }), main_loop)
        if track_info.get("track_id") and track_info.get("album_id"):
            cat.log_play(track_info["track_id"], track_info["album_id"])

    def on_status_change(status):
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", status), main_loop)

    def on_finished():
        state.now_playing = None
        asyncio.run_coroutine_threadsafe(
            broadcast("now_playing", {"track_title": None}), main_loop)
        asyncio.run_coroutine_threadsafe(
            broadcast("player_status", {"state": "stopped"}), main_loop)

    player = plr.Player(
        eq=state.eq, streams=all_streams,
        on_track_change=on_track_change,
        on_status_change=on_status_change,
        on_finished=on_finished,
    )
    state.player = player
    player.set_crossfade(state.settings.get("crossfade_secs", 0))
    player.play(album_id, album_info, playlist)

    try:
        while player.state != "stopped":
            if threads_done.is_set() and not local_streams and not bt_streams and not browser_streams:
                player.stop()
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        player.stop()
    finally:
        for s in audio_streams.values():
            s.stop()
        for s in local_streams:
            s.stop()
        for s in bt_streams:
            s.stop()
        for s in browser_streams:
            s.stop()
        state.player = None
        state.player_task = None
        state.now_playing = None
        state.airplay_metadata = None
        await broadcast("player_status", {"state": "stopped"})
        await broadcast("now_playing", {"track_title": None})


def _build_side_entry(aid: int, side: str, album_info: dict) -> plr.PlaylistEntry | None:
    """Build a PlaylistEntry for one specific side of an album."""
    audio_files = cat.get_album_audio(aid)
    af = next((f for f in audio_files if f["side"] == side), None)
    if not af:
        return None
    all_tracks = cat.get_album_tracks(aid)
    side_tracks = [
        {
            "id":           t["id"],
            "title":        t["title"],
            "artist":       t.get("artist") or album_info["artist"],
            "track_number": t["track_number"],
            "start_secs":   t.get("start_secs"),
            "end_secs":     t.get("end_secs"),
        }
        for t in all_tracks
        if t["side"] == side
    ]
    if side_tracks:
        first_start = min(
            (t["start_secs"] for t in side_tracks if t["start_secs"] is not None),
            default=0,
        )
        if first_start > 5.0:
            for t in side_tracks:
                if t["start_secs"] is not None:
                    t["start_secs"] -= first_start
                if t["end_secs"] is not None:
                    t["end_secs"] -= first_start
    return plr.PlaylistEntry(
        audio_path=af["file_path"],
        side=side,
        duration_secs=af.get("duration_secs"),
        tracks=side_tracks,
        album_id=aid,
        album_title=album_info["title"],
        album_artist=album_info["artist"],
        artwork_path=album_info.get("user_artwork_path") or album_info.get("artwork_path"),
    )
