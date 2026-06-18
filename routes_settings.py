#!/usr/bin/env python3
"""Vinyl AirPlay: settings utility routes (backup/restore, screenshot, folder picker, storage migration).

The /api/settings POST stays in main (it drives the auto-stream watcher); these
are the self-contained settings utilities. Shares AppState via app_state.
"""

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

import catalog as cat
from app_state import state
from config import save_settings

router = APIRouter()


@router.post("/api/settings/backup")
async def backup_settings():
    """Create a backup of all settings including EQ and playlists."""
    backup_data = {
        "settings": state.settings,
        "eq": {
            "bass": state.eq.values[0],
            "treble": state.eq.values[1],
        },
        "backup_timestamp": time.time(),
        "backup_version": 1,
    }
    return backup_data


@router.get("/api/settings/backup/download")
async def download_settings_backup():
    """Serve settings backup as a downloadable JSON file."""
    backup_data = {
        "settings": state.settings,
        "eq": {
            "bass": state.eq.values[0],
            "treble": state.eq.values[1],
        },
        "backup_timestamp": time.time(),
        "backup_version": 1,
    }
    date_str = datetime.now().date().isoformat()
    content = json.dumps(backup_data, indent=2)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="vinyl-settings-{date_str}.json"'
        }
    )


@router.post("/api/settings/restore")
async def restore_settings(body: dict):
    """Restore settings from a backup JSON."""
    try:
        if "backup_version" not in body or body.get("backup_version")!=1:
            return {"ok": False, "error": "Invalid backup format or version"}

        if "settings" in body and isinstance(body["settings"], dict):
            state.settings.update(body["settings"])
            save_settings(state.settings)

        if "eq" in body and isinstance(body["eq"], dict):
            bass=float(body["eq"].get("bass", 0))
            treble=float(body["eq"].get("treble", 0))
            state.settings["bass"]=bass
            state.settings["treble"]=treble
            state.eq.set_eq(bass, treble)
            save_settings(state.settings)

        return {"ok": True, "message": "Settings restored successfully"}
    except Exception as e:
        print(f"[settings] Restore error: {e}")
        return {"ok": False, "error": str(e)}


@router.get("/api/screenshot")
async def take_screenshot():
    """Capture a screenshot of the kiosk display (Wayland/grim)."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        env = {**os.environ, "WAYLAND_DISPLAY": "wayland-0",
               "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        result = subprocess.run(["grim", tmp.name], env=env,
                                capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "Screenshot failed"}
        with open(tmp.name, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="image/png",
                        headers={"Content-Disposition": 'attachment; filename="vinyl-streamer-screenshot.png"'})
    except FileNotFoundError:
        return {"ok": False, "error": "grim not installed (apt install grim)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)


@router.get("/api/browse-dirs")
async def browse_dirs(path: str = "/"):
    """List subdirectories of a given path for the folder picker."""
    p = Path(path).resolve()
    if not p.is_dir():
        return {"ok": False, "error": "Not a directory", "path": str(p), "dirs": []}
    dirs = []
    try:
        for entry in sorted(p.iterdir()):
            if entry.is_dir() and not entry.name.startswith('.'):
                try:
                    # Check we can actually read it
                    list(entry.iterdir())
                    dirs.append(entry.name)
                except PermissionError:
                    pass
    except PermissionError:
        return {"ok": False, "error": "Permission denied", "path": str(p), "dirs": []}
    return {"ok": True, "path": str(p), "dirs": dirs}


@router.post("/api/settings/storage")
async def change_storage_path(body: dict):
    """Change the FLAC recording storage location, migrating existing files."""
    new_path = (body.get("path") or "").strip()
    if not new_path:
        return {"ok": False, "error": "Path is required"}

    # Create-only mode: just ensure the directory exists (for folder picker)
    if body.get("create_only"):
        try:
            Path(new_path).mkdir(parents=True, exist_ok=True)
            return {"ok": True, "message": "Directory created"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Block if recording is active
    if state.album_recorder and state.album_recorder.is_active:
        return {"ok": False, "error": "Cannot change storage while recording is in progress"}

    new_dir = Path(new_path).resolve()
    old_dir = cat.get_audio_storage_dir(state.settings)

    if new_dir == old_dir:
        return {"ok": True, "migrated": 0, "message": "Already using this path"}

    # Run migration (copy files, update DB, delete originals)
    result = cat.migrate_audio_storage(old_dir, new_dir)

    if result["ok"]:
        state.settings["audio_storage_path"] = str(new_dir)
        save_settings(state.settings)
        msg = f"Storage moved to {new_dir}"
        if result["migrated"] > 0:
            msg += f" ({result['migrated']} files migrated)"
        return {"ok": True, "migrated": result["migrated"], "message": msg}
    else:
        return {"ok": False, "error": result["error"]}
