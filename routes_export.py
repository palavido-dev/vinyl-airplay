#!/usr/bin/env python3
"""Vinyl AirPlay: recorded-audio serving + audio export routes.

FLAC playback/serving, one-time needle-drop trim, and AAC/MP3 export jobs
with their download endpoints. Shares AppState via app_state.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

import catalog as cat
import exporter as exp
import recorder as rec
from app_state import broadcast, state

router = APIRouter()


# ── Album Audio Serving ──────────────────────────────────────────────────────

@router.get("/api/album-audio/{album_id}")
async def get_album_audio(album_id: int):
    """List all recorded audio files for an album."""
    audio = cat.get_album_audio(album_id)
    return {"audio": audio}


@router.get("/api/album-audio/{album_id}/play/{audio_id}")
async def play_album_audio(album_id: int, audio_id: int, request: Request):
    """
    Serve a recorded album audio file (FLAC) for playback.
    Supports HTTP Range requests for seeking.
    """
    audio = cat.get_album_audio_by_id(audio_id)
    if not audio or audio["album_id"] != album_id:
        return HTMLResponse("Not found", 404)
    path = Path(audio["file_path"])
    if not path.exists():
        return HTMLResponse("File missing", 404)
    return FileResponse(str(path), media_type="audio/flac", filename=path.name)


@router.post("/api/album-audio/trim-needle-drops")
async def trim_all_needle_drops():
    """
    One-time cleanup: scan all recorded FLAC files for needle-drop
    transients and trim them. Also updates track timestamps in the DB.
    """
    loop = asyncio.get_event_loop()
    albums = cat.get_all_albums()
    results = []

    for album in albums:
        audio_files = cat.get_album_audio(album["id"])
        if not audio_files:
            continue
        all_tracks = cat.get_album_tracks(album["id"])

        for af in audio_files:
            path = af["file_path"]
            info = await loop.run_in_executor(
                None, rec.trim_needle_drop_flac, path
            )
            trimmed = info["trimmed_secs"]
            results.append({
                "album": album["title"],
                "side": af["side"],
                "file": Path(path).name,
                "trimmed_secs": round(trimmed, 3),
                "success": info["success"],
                "error": info.get("error"),
            })

            # Update track timestamps if we trimmed
            if info["success"] and trimmed > 0:
                side_tracks = [t for t in all_tracks if t["side"] == af["side"]]
                for t in side_tracks:
                    new_start = max(0.0, (t.get("start_secs") or 0.0) - trimmed)
                    new_end = max(0.0, (t.get("end_secs") or 0.0) - trimmed)
                    cat.update_track_timestamps(t["id"], new_start, new_end)

                # Update duration in album_audio table
                new_dur = max(0.0, (af.get("duration_secs") or 0.0) - trimmed)
                try:
                    db = cat.get_db()
                    db.execute(
                        "UPDATE album_audio SET duration_secs = ? WHERE id = ?",
                        (new_dur, af["id"])
                    )
                    db.commit()
                    db.close()
                except Exception as e:
                    print(f"[trim] Failed to update duration: {e}")

    total_trimmed = sum(r["trimmed_secs"] for r in results if r["success"])
    trimmed_count = sum(1 for r in results if r["success"] and r["trimmed_secs"] > 0)
    return {
        "ok": True,
        "files_processed": len(results),
        "files_trimmed": trimmed_count,
        "total_secs_trimmed": round(total_trimmed, 2),
        "details": results,
    }


@router.delete("/api/album-audio/{album_id}")
async def delete_album_audio_route(album_id: int):
    """Delete all recorded audio for an album."""
    count = cat.delete_album_audio(album_id)
    return {"ok": True, "deleted": count}


@router.delete("/api/album-audio/{album_id}/{audio_id}")
async def delete_album_audio_single(album_id: int, audio_id: int):
    """Delete a single recorded audio file (one side)."""
    ok = cat.delete_album_audio_by_id(audio_id)
    if not ok:
        return {"ok": False, "error": "Audio not found"}
    return {"ok": True}


# ── Audio Export (AAC / MP3) ──────────────────────────────────────────────────

def _export_broadcast(msg: dict):
    """Broadcast export progress to all WebSocket clients."""
    text = json.dumps(msg)
    loop = state.loop
    if loop and loop.is_running():
        async def _send():
            dead = []
            for ws in list(state.ws_clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                if ws in state.ws_clients:
                    state.ws_clients.remove(ws)
        asyncio.run_coroutine_threadsafe(_send(), loop)


@router.post("/api/export/album/{album_id}")
async def api_export_album(album_id: int, request: Request):
    """Export all tracks for a single album to M4A or MP3."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    fmt = body.get("format", "m4a")
    if fmt not in ("m4a", "mp3"):
        return {"ok": False, "error": "Format must be m4a or mp3"}

    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    loop = asyncio.get_event_loop()

    force = body.get("force", False)
    result = await loop.run_in_executor(
        None, lambda: exp.export_album(album_id, fmt, export_dir, force=force)
    )
    return result


@router.post("/api/export/bulk")
async def api_export_bulk(request: Request):
    """Start bulk export of all albums with recordings. Returns job_id."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    fmt = body.get("format", "m4a")
    if fmt not in ("m4a", "mp3"):
        return {"ok": False, "error": "Format must be m4a or mp3"}

    # Store event loop reference for WebSocket broadcasts
    state.loop = asyncio.get_event_loop()
    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    job_id = exp.bulk_export(fmt=fmt, export_dir=export_dir, ws_broadcast=_export_broadcast)
    return {"ok": True, "job_id": job_id}


@router.get("/api/export/status/{job_id}")
async def api_export_status(job_id: str):
    """Check progress of a bulk export job."""
    status = exp.get_export_status(job_id)
    if not status:
        return {"ok": False, "error": "Job not found"}
    return {"ok": True, **status}


@router.get("/api/export/stats")
async def api_export_stats():
    """Get export directory stats: total files, size, albums exported."""
    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    if not export_dir.exists():
        return {"ok": True, "total_files": 0, "total_size": 0, "artists": []}

    total_files = 0
    total_size = 0
    artists = set()

    for root, dirs, files in os.walk(export_dir):
        for f in files:
            fp = Path(root) / f
            if fp.suffix in (".m4a", ".mp3"):
                total_files += 1
                total_size += fp.stat().st_size
        # Top-level dirs are artist names
        if Path(root) == export_dir:
            artists = set(dirs)

    return {
        "ok": True,
        "total_files": total_files,
        "total_size": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 1),
        "artist_count": len(artists),
        "export_dir": str(export_dir),
    }


@router.get("/api/export/album-status/{album_id}")
async def api_export_album_status(album_id: int):
    """Check if an album has exported files."""
    info = exp.get_album_export_info(album_id)
    return {"ok": True, **info}


@router.get("/api/export/all-status")
async def api_export_all_status():
    """Get export status for all albums (used for badges)."""
    status = exp.get_all_export_status()
    return {"ok": True, "albums": {str(k): v for k, v in status.items()}}


@router.get("/api/export/browse")
async def api_export_browse(path: str = ""):
    """Browse the export directory. Returns folders and files at the given subpath."""
    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    target = (export_dir / path).resolve()

    # Prevent directory traversal
    if not str(target).startswith(str(export_dir)):
        return {"ok": False, "error": "Invalid path"}
    if not target.exists():
        return {"ok": False, "error": "Path not found"}

    if target.is_file():
        return {"ok": True, "type": "file", "name": target.name,
                "size": target.stat().st_size}

    folders = []
    files = []
    for item in sorted(target.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            # Count audio files inside
            count = sum(1 for f in item.rglob("*") if f.suffix in (".m4a", ".mp3"))
            folders.append({"name": item.name, "track_count": count})
        elif item.suffix in (".m4a", ".mp3"):
            files.append({"name": item.name, "size": item.stat().st_size,
                          "size_mb": round(item.stat().st_size / (1024*1024), 1)})

    return {"ok": True, "path": path, "folders": folders, "files": files}


@router.get("/api/export/download")
async def api_export_download(path: str):
    """Download a single exported file."""
    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    target = (export_dir / path).resolve()

    if not str(target).startswith(str(export_dir)):
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)

    media_type = "audio/mp4" if target.suffix == ".m4a" else "audio/mpeg"
    return FileResponse(str(target), filename=target.name, media_type=media_type)


@router.get("/api/export/download-album")
async def api_export_download_album(path: str):
    """Download an entire album folder as a ZIP file."""
    import io
    import zipfile

    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    target = (export_dir / path).resolve()

    if not str(target).startswith(str(export_dir)):
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    if not target.is_dir():
        return JSONResponse({"ok": False, "error": "Folder not found"}, status_code=404)

    # Stream the zip so we don't buffer huge files in memory
    def zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for fp in sorted(target.rglob("*")):
                if fp.is_file() and fp.suffix in (".m4a", ".mp3"):
                    arcname = fp.relative_to(target)
                    zf.write(fp, arcname)
        buf.seek(0)
        yield buf.read()

    zip_name = f"{target.name}.zip"
    return StreamingResponse(
        zip_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@router.get("/api/export/download-all")
async def api_export_download_all():
    """Download all exports as a single ZIP file."""
    import io
    import zipfile

    export_dir = exp.DEFAULT_EXPORT_DIR.resolve()
    if not export_dir.exists():
        return JSONResponse({"ok": False, "error": "No exports"}, status_code=404)

    def zip_generator():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for fp in sorted(export_dir.rglob("*")):
                if fp.is_file() and fp.suffix in (".m4a", ".mp3"):
                    arcname = fp.relative_to(export_dir)
                    zf.write(fp, arcname)
        buf.seek(0)
        yield buf.read()

    return StreamingResponse(
        zip_generator(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="vinyl-exports.zip"'},
    )
