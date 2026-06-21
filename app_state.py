#!/usr/bin/env python3
"""Vinyl AirPlay: the AppState singleton and the helpers that close over it.

Importable core (no import back into main) so router modules can share the same
`state`, `spawn_bg`, and `broadcast`. The player/recorder/pyatv types used in
AppState are instance-attribute annotations (PEP 526), so they are never
evaluated at runtime; they are imported under TYPE_CHECKING only for tooling.
"""

import asyncio
import concurrent.futures
import json
import threading
from typing import TYPE_CHECKING

import catalog as cat
from audio_eq import EQ
from audio_mp3 import LiveMP3Broadcaster
from config import load_settings

if TYPE_CHECKING:
    from pyatv.interface import MediaMetadata
    from pyatv.storage.file_storage import FileStorage

    import player as plr
    import recorder as rec


class AppState:
    def __init__(self):
        self.settings            = load_settings()
        self.is_streaming        = False
        self.active_devices      = []
        self.available_devices   = []
        self.audio_devices       = []
        self.stream_task: asyncio.Task | None = None
        self.stop_event: asyncio.Event | None = None
        # Serializes access to the single ALSA capture device. The watcher,
        # AirPlay streaming, and listen mode each open an InputStream on it, and
        # ALSA capture is exclusive, so only one may hold it at a time.
        self.capture_lock = threading.Lock()
        self.ws_clients          = []
        self.eq = EQ(
            bass_db   = self.settings.get("bass",   0),
            treble_db = self.settings.get("treble", 0),
            volume    = self.settings.get("volume", 80),
            bands     = self.settings.get("eq_bands"),
        )
        self.fp_buffer              = cat.FingerprintBuffer()
        self.recogniser: cat.Recogniser | None = None
        self.now_playing: dict | None = None
        self.rec_buffer: rec.RecordingBuffer | None = None
        self.rec_level: float = 0.0          # current RMS for UI meter
        self.learn_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fpcalc")
        self.auto_stream_task: asyncio.Task | None = None
        self.manual_stop_until: float = 0.0  # monotonic: auto-stream suppressed after manual stop
        self.pairing_sessions: dict = {}  # device_id → active pyatv pairing object
        self.listen_task: asyncio.Task | None = None  # audio-only (no AirPlay) task
        self.learn_session = None  # LearnSession (defined in main), assigned by the learn routes
        self.album_recorder: rec.AlbumRecorder | None = None  # full-side capture
        self.stall_watchdog_task: asyncio.Task | None = None  # audio stream health monitor
        self.player: plr.Player | None = None
        self.player_task: asyncio.Task | None = None
        self.airplay_metadata: MediaMetadata | None = None
        self.bluetooth_manager = None  # initialized after BluetoothManager is defined
        self.available_bt_devices: list = []
        self.loop = None  # event loop ref for background thread broadcasts
        self.live_mp3 = LiveMP3Broadcaster()
        # Strong refs to fire-and-forget asyncio tasks so they're not GC'd
        # mid-flight. Tasks remove themselves via add_done_callback.
        self.bg_tasks: set = set()
        # pyatv 0.17 needs an explicit FileStorage handed to scan() and pair()
        # so that saved credentials get attached to the conf objects we then
        # pass to pyatv.connect(). Loaded once at startup and reused.
        self.atv_storage: FileStorage | None = None
        self.album_encoding = {
            "in_progress": False,
            "album_id": None,
            "side": None,
            "started_at": None,
            "finished_at": None,
            "ok": None,
            "message": "",
        }
        # State for the "rebuild every track's fingerprints" maintenance job.
        # Driven by /api/maintenance/rebuild-fingerprints; broadcast to the
        # UI over the rebuild_fingerprints_progress WS event.
        self.rebuild_fp = {
            "in_progress":  False,
            "total":        0,
            "done":         0,
            "ok":           0,
            "failed":       0,
            "current":      None,    # {"track_id": int, "title": str, "album": str}
            "started_at":   None,
            "finished_at":  None,
            "backup_path":  None,    # where we wrote the pre-run fp dump
            "last_error":   None,
        }


state = AppState()


def spawn_bg(coro) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine and keep a strong ref to it.

    asyncio.create_task only keeps a weak reference, so a bare
    `asyncio.create_task(coro)` can be garbage collected before it
    finishes. Holding the task in state.bg_tasks until it completes
    avoids that footgun.
    """
    task = asyncio.create_task(coro)
    state.bg_tasks.add(task)
    task.add_done_callback(state.bg_tasks.discard)
    return task


async def broadcast(event: str, data: dict | None = None):
    if data is None:
        data = {}
    msg  = json.dumps({"event": event, **data})
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.remove(ws)
