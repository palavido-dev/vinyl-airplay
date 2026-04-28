#!/usr/bin/env python3
"""
Vinyl AirPlay -- Audio Exporter
Converts recorded FLAC side files into per-track AAC (M4A) or MP3
with embedded metadata and album art, organized for iTunes import.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Callable

import catalog as cat

# ── Config ────────────────────────────────────────────────────────────────────

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Apple Music uses 256 kbps AAC; 320 kbps MP3 is the lossless-adjacent standard
FORMAT_PRESETS = {
    "m4a": {"codec": "aac", "bitrate": "256k", "ext": "m4a", "mime": "audio/mp4"},
    "mp3": {"codec": "libmp3lame", "bitrate": "320k", "ext": "mp3", "mime": "audio/mpeg"},
}

# Check for the standard install location first, fall back to local
_INSTALL_EXPORT = Path("/opt/vinyl-streamer/exports")
DEFAULT_EXPORT_DIR = _INSTALL_EXPORT if _INSTALL_EXPORT.exists() else Path("exports")


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    # Replace characters that are problematic on Windows/Mac/Linux
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = name.strip(". ")
    return name or "Unknown"


def _get_flac_duration(path: str) -> float:
    """Get the duration of a FLAC file in seconds via ffprobe."""
    try:
        out = subprocess.check_output([
            FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path)
        ], timeout=30)
        return float(out.strip())
    except Exception:
        return 0.0


def _resolve_track_bounds(track: dict, side_duration: float,
                          prev_end: float = None):
    """Return (start_secs, duration_secs) for extracting a track from a side FLAC.

    Handles edge cases:
      - Last track where end_secs == start_secs or is None
      - Track whose start_secs >= side duration (bad timestamp data)
      - Falls back to catalog duration_secs when timestamps are unusable
    """
    start = track.get("start_secs") or 0.0
    end = track.get("end_secs")
    catalog_dur = track.get("duration_secs") or 0

    # If start is at or past the file end, try to compute from previous track
    if start >= side_duration:
        if prev_end is not None and prev_end < side_duration:
            start = prev_end
        elif catalog_dur > 0:
            # Work backwards from file end
            start = max(0, side_duration - catalog_dur)
        else:
            start = max(0, side_duration - 60)  # last resort

    # Determine end point
    if end is None or end <= start:
        end = side_duration

    duration = end - start
    if duration <= 0:
        # Use catalog duration as fallback
        duration = catalog_dur if catalog_dur > 0 else (side_duration - start)

    return start, max(duration, 0.1)


def _get_artwork_path(album: dict) -> Optional[str]:
    """Return the best available artwork path for an album (absolute)."""
    # The app may store relative paths (e.g. "artwork/album_14_user.jpg").
    # Resolve them relative to the app's source directory.
    app_dir = Path(__file__).parent.resolve()

    for key in ("user_artwork_path", "artwork_path"):
        p = album.get(key)
        if not p:
            continue
        # Try as-is (absolute or CWD-relative)
        if os.path.isfile(p):
            return str(Path(p).resolve())
        # Try relative to the app directory
        candidate = app_dir / p
        if candidate.is_file():
            return str(candidate)
        # Try just the filename in the artwork subdir
        candidate2 = app_dir / "artwork" / Path(p).name
        if candidate2.is_file():
            return str(candidate2)
    return None


# ── Single Track Export ───────────────────────────────────────────────────────

def export_track_to_file(
    track: dict,
    album: dict,
    flac_path: str,
    side_duration: float,
    output_path: Path,
    fmt: str = "m4a",
    artwork_path: Optional[str] = None,
    total_tracks_on_album: int = 0,
    prev_end: float = None,
    override_track_num: int = None,
) -> dict:
    """Export a single track from a side FLAC to M4A or MP3.

    Returns {"ok": True, "path": str, "size": int} or {"ok": False, "error": str}.
    """
    preset = FORMAT_PRESETS.get(fmt)
    if not preset:
        return {"ok": False, "error": f"Unknown format: {fmt}"}

    if not os.path.isfile(flac_path):
        return {"ok": False, "error": f"FLAC not found: {flac_path}"}

    start, duration = _resolve_track_bounds(track, side_duration, prev_end)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build ffmpeg command
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]

    # Input: FLAC with seek
    cmd += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(flac_path)]

    # Artwork input (if available)
    has_art = artwork_path and os.path.isfile(artwork_path)
    if has_art:
        cmd += ["-i", str(artwork_path)]

    # Output mapping
    if has_art:
        cmd += ["-map", "0:a", "-map", "1:0"]
        if fmt == "m4a":
            # M4A needs MJPEG codec for cover art, not raw copy
            cmd += ["-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
        elif fmt == "mp3":
            cmd += ["-c:v", "copy", "-id3v2_version", "3"]

    # Audio codec
    cmd += ["-c:a", preset["codec"], "-b:a", preset["bitrate"]]

    # Metadata
    title = track.get("title") or "Unknown"
    artist = track.get("artist") or album.get("artist") or "Unknown"
    album_title = album.get("title") or "Unknown"
    year = album.get("year") or ""
    genre = album.get("genre") or ""
    track_num = str(override_track_num) if override_track_num else (track.get("track_number") or "1")

    cmd += [
        "-metadata", f"title={title}",
        "-metadata", f"artist={artist}",
        "-metadata", f"album={album_title}",
        "-metadata", f"album_artist={album.get('artist', '')}",
        "-metadata", f"track={track_num}/{total_tracks_on_album}" if total_tracks_on_album else f"track={track_num}",
        "-metadata", f"date={year}",
        "-metadata", f"genre={genre}",
    ]

    # M4A-specific: use MP4 container
    if fmt == "m4a":
        cmd += ["-movflags", "+faststart"]

    cmd.append(str(output_path))

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace").strip()
        return {"ok": False, "error": f"ffmpeg error: {err[:200]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg timed out"}

    if output_path.exists():
        return {"ok": True, "path": str(output_path), "size": output_path.stat().st_size}
    return {"ok": False, "error": "Output file not created"}


# ── Album Export ──────────────────────────────────────────────────────────────

def export_album(
    album_id: int,
    fmt: str = "m4a",
    export_dir: Optional[Path] = None,
    on_progress: Optional[Callable] = None,
) -> dict:
    """Export all tracks for an album to M4A or MP3.

    Returns {
        "ok": True,
        "album": str,
        "format": str,
        "tracks_exported": int,
        "tracks_failed": int,
        "output_dir": str,
        "files": [{"track": str, "path": str, "size": int}, ...],
        "errors": [{"track": str, "error": str}, ...]
    }
    """
    album = cat.get_album(album_id)
    if not album:
        return {"ok": False, "error": "Album not found"}

    audio_files = cat.get_album_audio(album_id)
    if not audio_files:
        return {"ok": False, "error": "No recordings for this album"}

    tracks = cat.get_album_tracks(album_id)
    if not tracks:
        return {"ok": False, "error": "No tracks for this album"}

    preset = FORMAT_PRESETS.get(fmt)
    if not preset:
        return {"ok": False, "error": f"Unknown format: {fmt}"}

    # Build output directory: Artist/Album (Year)/
    base_dir = export_dir or DEFAULT_EXPORT_DIR.resolve()
    artist_dir = _safe_filename(album.get("artist") or "Unknown Artist")
    album_name = _safe_filename(album.get("title") or "Unknown Album")
    year = album.get("year")
    if year:
        album_folder = f"{album_name} ({year})"
    else:
        album_folder = album_name
    out_dir = base_dir / artist_dir / album_folder
    out_dir.mkdir(parents=True, exist_ok=True)

    # Map sides to their FLAC paths and durations
    side_map = {}
    for af in audio_files:
        side = af.get("side", "A")
        path = af.get("file_path", "")
        if os.path.isfile(path):
            dur = _get_flac_duration(path)
            side_map[side] = {"path": path, "duration": dur}

    artwork = _get_artwork_path(album)
    total_tracks = len(tracks)
    results = []
    errors = []

    # Track the previous track's end per side for fallback computation
    prev_end_by_side = {}

    for i, track in enumerate(tracks):
        side = track.get("side", "A")
        if side not in side_map:
            errors.append({"track": track.get("title", "?"), "error": f"No recording for side {side}"})
            continue

        sf = side_map[side]

        # Use continuous numbering across all sides for iTunes
        album_track_num = i + 1
        tnum_str = f"{album_track_num:02d}"

        title = _safe_filename(track.get("title") or "Unknown")
        filename = f"{tnum_str} - {title}.{preset['ext']}"
        out_path = out_dir / filename

        if on_progress:
            on_progress({
                "step": "encoding",
                "track_index": i,
                "total_tracks": total_tracks,
                "track_title": track.get("title", ""),
                "percent": int(i / total_tracks * 100),
            })

        # Resolve bounds with prev_end fallback for last-track edge case
        prev_end = prev_end_by_side.get(side)

        result = export_track_to_file(
            track=track,
            album=album,
            flac_path=sf["path"],
            side_duration=sf["duration"],
            output_path=out_path,
            fmt=fmt,
            artwork_path=artwork,
            total_tracks_on_album=total_tracks,
            prev_end=prev_end,
            override_track_num=album_track_num,
        )

        # Update prev_end for next track on this side
        t_end = track.get("end_secs")
        t_start = track.get("start_secs") or 0
        if t_end and t_end > t_start:
            prev_end_by_side[side] = t_end
        else:
            prev_end_by_side[side] = t_start

        if result["ok"]:
            results.append({
                "track": track.get("title", ""),
                "path": str(out_path),
                "size": result["size"],
            })
        else:
            errors.append({"track": track.get("title", ""), "error": result["error"]})

    if on_progress:
        on_progress({
            "step": "done",
            "track_index": total_tracks,
            "total_tracks": total_tracks,
            "percent": 100,
        })

    return {
        "ok": True,
        "album": f"{album.get('artist', '')} - {album.get('title', '')}",
        "format": fmt,
        "tracks_exported": len(results),
        "tracks_failed": len(errors),
        "output_dir": str(out_dir),
        "files": results,
        "errors": errors,
    }


# ── Bulk Export ───────────────────────────────────────────────────────────────

# Active export jobs (thread-safe)
_jobs = {}
_jobs_lock = threading.Lock()


def _job_set(job_id: str, data: dict):
    with _jobs_lock:
        _jobs[job_id] = data


def _job_get(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _jobs.get(job_id, {}).copy()


def get_export_status(job_id: str) -> Optional[dict]:
    return _job_get(job_id)


def bulk_export(
    fmt: str = "m4a",
    export_dir: Optional[Path] = None,
    ws_broadcast: Optional[Callable] = None,
) -> str:
    """Start a bulk export of all albums with recordings. Returns job_id."""
    job_id = str(uuid.uuid4())[:8]
    base_dir = export_dir or DEFAULT_EXPORT_DIR.resolve()

    _job_set(job_id, {
        "status": "starting",
        "job_id": job_id,
        "format": fmt,
        "album_index": 0,
        "total_albums": 0,
        "current_album": "",
        "percent": 0,
        "tracks_exported": 0,
        "tracks_failed": 0,
        "started_at": time.time(),
        "errors": [],
    })

    def run():
        try:
            _bulk_export_worker(job_id, fmt, base_dir, ws_broadcast)
        except Exception as e:
            _job_set(job_id, {**_job_get(job_id), "status": "error", "error": str(e)})

    t = threading.Thread(target=run, daemon=True, name=f"export-{job_id}")
    t.start()
    return job_id


def _bulk_export_worker(
    job_id: str,
    fmt: str,
    base_dir: Path,
    ws_broadcast: Optional[Callable],
):
    """Worker thread for bulk export."""
    # Get all albums that have recordings
    all_albums = cat.get_all_albums()
    albums_with_audio = []
    for a in all_albums:
        audio = cat.get_album_audio(a["id"])
        if audio:
            albums_with_audio.append(a)

    total = len(albums_with_audio)
    _job_set(job_id, {**_job_get(job_id), "total_albums": total, "status": "running"})

    total_exported = 0
    total_failed = 0
    all_errors = []

    for idx, album in enumerate(albums_with_audio):
        label = f"{album.get('artist', '')} - {album.get('title', '')}"
        progress = {
            **_job_get(job_id),
            "status": "running",
            "album_index": idx,
            "current_album": label,
            "percent": int(idx / total * 100) if total else 0,
        }
        _job_set(job_id, progress)

        if ws_broadcast:
            ws_broadcast({"event": "export_progress", **progress})

        def on_track_progress(tp):
            p = {
                **_job_get(job_id),
                "current_track": tp.get("track_title", ""),
                "track_percent": tp.get("percent", 0),
            }
            _job_set(job_id, p)
            if ws_broadcast:
                ws_broadcast({"event": "export_progress", **p})

        result = export_album(
            album_id=album["id"],
            fmt=fmt,
            export_dir=base_dir,
            on_progress=on_track_progress,
        )

        total_exported += result.get("tracks_exported", 0)
        total_failed += result.get("tracks_failed", 0)
        if result.get("errors"):
            all_errors.extend(result["errors"])

        _job_set(job_id, {
            **_job_get(job_id),
            "tracks_exported": total_exported,
            "tracks_failed": total_failed,
        })

    elapsed = time.time() - _job_get(job_id).get("started_at", time.time())
    final = {
        "status": "done",
        "job_id": job_id,
        "format": fmt,
        "album_index": total,
        "total_albums": total,
        "percent": 100,
        "tracks_exported": total_exported,
        "tracks_failed": total_failed,
        "elapsed_secs": round(elapsed, 1),
        "errors": all_errors,
        "output_dir": str(base_dir),
    }
    _job_set(job_id, final)

    if ws_broadcast:
        ws_broadcast({"event": "export_progress", **final})

    print(f"[export] Bulk export complete: {total_exported} tracks from "
          f"{total} albums in {elapsed:.0f}s ({total_failed} failures)")
