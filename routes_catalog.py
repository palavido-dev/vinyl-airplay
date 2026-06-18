#!/usr/bin/env python3
"""Vinyl AirPlay: catalog CRUD routes (albums, tracks, Discogs sync, artwork, collage).

The data/CRUD half of the catalog API. Learn- and playback-coupled routes
(learn_album, the /play endpoints) stay in main with the recognition/player engine.
Shares AppState via app_state.
"""

import asyncio
import math
import os
import random
import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import JSONResponse

import catalog as cat
from app_state import broadcast, state

router = APIRouter()


@router.get("/api/catalog")
async def get_catalog():
    return {"albums": cat.get_all_albums()}


@router.get("/api/catalog/shelves")
async def get_shelves():
    albums = cat.get_all_albums()

    # Recently played
    recently_played = sorted([a for a in albums if a.get('last_played')],
                            key=lambda x: x.get('last_played', ''), reverse=True)[:10]

    # Recently added
    recently_added = sorted([a for a in albums if a.get('created_at')],
                           key=lambda x: x.get('created_at', ''), reverse=True)[:10]

    # Most played (by play count)
    most_played = sorted([a for a in albums if a.get('play_count', 0) > 0],
                        key=lambda x: x.get('play_count', 0), reverse=True)[:10]

    # Unplayed
    unplayed = [a for a in albums if a.get('play_count', 0) == 0][:10]

    # Favorites
    favorites = [a for a in albums if a.get('favorite')][:10]

    # Top rated
    top_rated = sorted([a for a in albums if a.get('rating', 0) > 0],
                      key=lambda x: x.get('rating', 0), reverse=True)[:10]

    # By decade
    decades = {}
    for album in albums:
        year = album.get('year')
        if year:
            decade = (int(year) // 10) * 10
            decade_label = f"{decade}s"
            if decade_label not in decades:
                decades[decade_label] = []
            decades[decade_label].append(album)

    # By genre (split comma-separated tags so albums appear in each genre shelf)
    genres = {}
    for album in albums:
        raw = album.get('genre', 'Unknown')
        for g in raw.split(","):
            g = g.strip()
            if not g:
                continue
            if g not in genres:
                genres[g] = []
            genres[g].append(album)

    return {
        "recently_played": recently_played,
        "recently_added": recently_added,
        "most_played": most_played,
        "unplayed": unplayed,
        "favorites": favorites,
        "top_rated": top_rated,
        "decades": decades,
        "genres": genres
    }


@router.get("/api/catalog/shelves/optimized")
async def get_catalog_shelves():
    """Get home screen shelf data with optimized catalog structure."""
    all_albums = cat.get_all_albums()
    recently_played = [a for a in all_albums if a.get("last_played") and a["audio_count"] > 0]
    recently_played.sort(key=lambda a: a.get("last_played") or "", reverse=True)
    recently_played = recently_played[:8]
    recently_added = sorted(all_albums, key=lambda a: a.get("created_at") or "", reverse=True)[:8]
    most_played = sorted(all_albums, key=lambda a: a.get("play_count") or 0, reverse=True)
    most_played = [a for a in most_played if (a.get("play_count") or 0) > 0][:8]
    unplayed = [a for a in all_albums if a["audio_count"] > 0 and (a.get("play_count") or 0) == 0][:8]
    favorites = [a for a in all_albums if a.get("favorite")][:8]
    top_rated = [a for a in all_albums if (a.get("rating") or 0) >= 4][:8]
    decades = cat.get_decades_with_albums()
    decade_shelves = {}
    for decade in decades:
        albums_in_decade = cat.get_albums_by_decade(decade)[:8]
        if albums_in_decade:
            decade_shelves[f"The {decade}s"] = albums_in_decade
    genres = cat.get_genres_with_count(min_count=2)
    genre_shelves = {}
    for genre, _ in genres:
        genre_albums = [a for a in all_albums
                        if genre in [g.strip() for g in (a.get("genre") or "Unknown").split(",")]][:8]
        if genre_albums:
            genre_shelves[genre] = genre_albums
    return {
        "recently_played": recently_played,
        "recently_added":  recently_added,
        "most_played":     most_played,
        "unplayed":        unplayed,
        "favorites":       favorites,
        "top_rated":       top_rated,
        "decades":         decade_shelves,
        "genres":          genre_shelves,
    }


@router.get("/api/catalog/history")
async def get_history():
    return {"plays": cat.get_recent_plays()}


@router.get("/api/catalog/stats")
async def get_stats():
    return cat.get_listening_stats()


@router.get("/api/catalog/tracks/search")
async def search_tracks(q: str = ""):
    if not q.strip():
        return {"tracks": []}
    return {"tracks": cat.search_tracks(q.strip())}


@router.get("/api/catalog/{album_id}/tracks")
async def get_tracks(album_id: int):
    return {"tracks": cat.get_album_tracks(album_id)}


@router.post("/api/catalog/{album_id}/tracks")
async def add_track(album_id: int, body: Annotated[dict, Body()]):
    title = body.get("title", "").strip()
    if not title:
        return {"ok": False, "error": "Title is required"}
    side = body.get("side", "A")
    track_number = body.get("track_number")
    artist = body.get("artist")
    tid = cat.add_track(album_id, title, side, track_number, artist)
    if tid:
        return {"ok": True, "track_id": tid}
    return {"ok": False, "error": "Failed to add track"}


@router.delete("/api/catalog/track/{track_id}")
async def delete_track(track_id: int):
    ok = cat.delete_track(track_id)
    return {"ok": ok}


@router.put("/api/catalog/track/{track_id}")
async def update_track(track_id: int, body: Annotated[dict, Body()]):
    ok = cat.update_track(
        track_id,
        title=body.get("title"),
        artist=body.get("artist"),
        track_number=body.get("track_number"),
        side=body.get("side"),
        duration_secs=body.get("duration_secs"),
    )
    return {"ok": ok}


@router.put("/api/catalog/track/{track_id}/boundaries")
async def update_boundaries(track_id: int, body: Annotated[dict, Body()]):
    start = body.get("start_secs")
    end = body.get("end_secs")
    if start is None or end is None:
        return {"ok": False, "error": "start_secs and end_secs required"}
    cat.update_track_timestamps(track_id, float(start), float(end))
    return {"ok": True}


@router.post("/api/catalog/{album_id}/artwork")
async def upload_artwork(album_id: int, file: Annotated[UploadFile, File()]):
    data = await file.read()
    path = cat.save_user_artwork(data, album_id)
    if not path:
        return {"ok": False, "error": "Failed to save image"}
    cat.update_album_artwork(album_id, path, user=True)
    return {"ok": True, "artwork_url": f"/artwork/{Path(path).name}"}


@router.post("/api/catalog/manual")
async def manual_entry(body: dict):
    track = cat.save_manual_track(body)
    if not track:
        return {"ok": False, "error": "Failed to save"}
    return {"ok": True, "track": track}





@router.get("/api/catalog/search/discogs")
async def search_discogs(artist: str = "", album: str = "", barcode: str = ""):
    token = state.settings.get("discogs_token", "")
    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: cat.search_discogs(artist, album, token=token, barcode=barcode)
    )
    return {"releases": results}


@router.get("/api/catalog/release/discogs/{discogs_id}")
async def discogs_release(discogs_id: str):
    token = state.settings.get("discogs_token", "")
    data = await asyncio.get_event_loop().run_in_executor(
        None, lambda: cat.get_discogs_release(discogs_id, token=token)
    )
    return data




# -- Discogs Collection Sync --------------------------------------------------

_discogs_sync_status = {"state": "idle"}
_discogs_sync_lock = threading.Lock()


@router.post("/api/catalog/sync/discogs")
async def start_discogs_sync():
    """Import the user's Discogs collection into the local catalog."""
    username = state.settings.get("discogs_username", "").strip()
    token = state.settings.get("discogs_token", "")
    if not username:
        return JSONResponse({"ok": False, "error": "Discogs username not set"}, status_code=400)
    if not token:
        return JSONResponse({"ok": False, "error": "Discogs token not set"}, status_code=400)

    if not _discogs_sync_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "Sync already running"}, status_code=409)

    global _discogs_sync_status
    _discogs_sync_status = {"state": "starting", "total": 0, "checked": 0,
                            "imported": 0, "skipped": 0, "failed": 0}

    loop = asyncio.get_event_loop()

    def _run_sync():
        global _discogs_sync_status
        try:
            # Phase 1: Pull from Discogs into local catalog
            def on_pull_progress(info):
                global _discogs_sync_status
                _discogs_sync_status = {**info, "phase": "pull"}
                asyncio.run_coroutine_threadsafe(
                    broadcast("sync_progress", _discogs_sync_status), loop
                )

            pull_result = cat.sync_from_discogs(username, token, on_progress=on_pull_progress)

            # Phase 2: Push local albums to Discogs
            def on_push_progress(info):
                global _discogs_sync_status
                _discogs_sync_status = {**info, "phase": "push"}
                asyncio.run_coroutine_threadsafe(
                    broadcast("sync_progress", _discogs_sync_status), loop
                )

            push_result = cat.push_to_discogs(username, token, on_progress=on_push_progress)

            # Combined summary
            combined = {
                "state": "complete",
                "phase": "done",
                "pulled": pull_result.get("imported", 0),
                "pull_skipped": pull_result.get("skipped", 0),
                "pushed": push_result.get("pushed", 0),
                "push_skipped": push_result.get("skipped", 0),
                "failed": pull_result.get("failed", 0) + push_result.get("failed", 0),
                "errors": pull_result.get("errors", []) + push_result.get("errors", []),
            }
            _discogs_sync_status = combined
            asyncio.run_coroutine_threadsafe(
                broadcast("sync_progress", combined), loop
            )
        except Exception as e:
            _discogs_sync_status = {"state": "error", "errors": [str(e)]}
            asyncio.run_coroutine_threadsafe(
                broadcast("sync_progress", _discogs_sync_status), loop
            )
        finally:
            _discogs_sync_lock.release()

    threading.Thread(target=_run_sync, daemon=True).start()
    return {"ok": True, "message": "Sync started"}


@router.get("/api/catalog/sync/discogs/status")
async def discogs_sync_status():
    """Return current Discogs sync status."""
    return _discogs_sync_status


@router.post("/api/catalog/sync/discogs/backfill-ids")
async def backfill_discogs_ids():
    """One-time migration: match local albums to Discogs collection by title/artist."""
    username = state.settings.get("discogs_username", "").strip()
    token = state.settings.get("discogs_token", "")
    if not username or not token:
        return JSONResponse({"ok": False, "error": "Discogs username and token required"}, status_code=400)

    loop = asyncio.get_event_loop()

    def _run():
        def on_progress(info):
            asyncio.run_coroutine_threadsafe(
                broadcast("backfill_progress", info), loop
            )
        result = cat.backfill_discogs_ids(username, token, on_progress=on_progress)
        asyncio.run_coroutine_threadsafe(
            broadcast("backfill_progress", result), loop
        )

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Backfill started"}


_artwork_fetch_status = {"state": "idle"}
_artwork_fetch_lock = threading.Lock()


@router.post("/api/catalog/artwork/fetch-missing")
async def fetch_missing_artwork():
    """Fetch artwork for Discogs albums that are missing cover art."""
    token = state.settings.get("discogs_token", "")
    if not token:
        return JSONResponse({"ok": False, "error": "Discogs token not set"}, status_code=400)

    if not _artwork_fetch_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "error": "Already fetching artwork"}, status_code=409)

    global _artwork_fetch_status
    _artwork_fetch_status = {"state": "starting", "total": 0, "checked": 0,
                             "fetched": 0, "failed": 0}

    loop = asyncio.get_event_loop()

    def _run():
        global _artwork_fetch_status
        try:
            def on_progress(info):
                global _artwork_fetch_status
                _artwork_fetch_status = info
                asyncio.run_coroutine_threadsafe(
                    broadcast("artwork_fetch_progress", info), loop
                )
            result = cat.fetch_missing_artwork(token, on_progress=on_progress)
            _artwork_fetch_status = result
            asyncio.run_coroutine_threadsafe(
                broadcast("artwork_fetch_progress", result), loop
            )
        except Exception as e:
            _artwork_fetch_status = {"state": "error", "errors": [str(e)]}
            asyncio.run_coroutine_threadsafe(
                broadcast("artwork_fetch_progress", _artwork_fetch_status), loop
            )
        finally:
            _artwork_fetch_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": "Fetching missing artwork"}


@router.get("/api/catalog/artwork/fetch-missing/status")
async def artwork_fetch_status():
    """Return current artwork fetch status."""
    return _artwork_fetch_status


@router.post("/api/catalog/collage")
async def generate_collage():
    """Generate a grid collage of all album artwork."""
    loop = asyncio.get_event_loop()

    def _build():
        from PIL import Image as PILImage

        db = cat.get_db()
        rows = db.execute("""
            SELECT COALESCE(NULLIF(user_artwork_path,''), artwork_path) as art
            FROM albums WHERE deleted_at IS NULL
            ORDER BY RANDOM()
        """).fetchall()
        db.close()

        images = [r[0] for r in rows if r[0] and os.path.exists(r[0])]
        n = len(images)
        if n == 0:
            return None

        thumb = 300
        cols = math.ceil(math.sqrt(n))
        rows_count = math.ceil(n / cols)
        collage = PILImage.new("RGB", (cols * thumb, rows_count * thumb), (20, 20, 20))

        for i, path in enumerate(images):
            r, c = divmod(i, cols)
            try:
                img = PILImage.open(path).convert("RGB").resize(
                    (thumb, thumb), PILImage.LANCZOS)
                collage.paste(img, (c * thumb, r * thumb))
            except Exception:
                pass

        # fill empty trailing cells with random picks
        empty = cols * rows_count - n
        if empty > 0:
            fill = random.sample(images, min(empty, len(images)))
            for j in range(empty):
                idx = n + j
                r, c = divmod(idx, cols)
                try:
                    img = PILImage.open(fill[j % len(fill)]).convert("RGB").resize(
                        (thumb, thumb), PILImage.LANCZOS)
                    collage.paste(img, (c * thumb, r * thumb))
                except Exception:
                    pass

        out = cat.ARTWORK_DIR / "vinyl_collage.jpg"
        collage.save(str(out), "JPEG", quality=90)
        w, h = cols * thumb, rows_count * thumb
        return {"albums": n, "size": f"{w}x{h}"}

    result = await loop.run_in_executor(None, _build)
    if result is None:
        return {"ok": False, "error": "No albums with artwork found"}
    return {"ok": True, "url": "/artwork/vinyl_collage.jpg", **result}


@router.post("/api/catalog/release")
async def save_release(body: dict):
    """Save a release (from Discogs search) to the catalog."""
    release_data = body.get("release", {})
    if not release_data:
        return {"ok": False, "error": "No release data"}
    loop = asyncio.get_event_loop()
    album_id = await loop.run_in_executor(
        None, lambda: cat.save_release_to_catalog(release_data)
    )
    if album_id is None:
        return {"ok": False, "error": "Failed to save"}
    # Try to fetch artwork from Discogs URL
    artwork_url = release_data.get("artwork_url")
    if artwork_url:
        art = await loop.run_in_executor(
            None, lambda: cat.fetch_artwork_from_url(artwork_url, album_id)
        )
        if art:
            cat.update_album_artwork(album_id, art, user=False)
    # Push to Discogs collection if configured
    username = state.settings.get("discogs_username", "").strip()
    token = state.settings.get("discogs_token", "")
    release_id = release_data.get("id", "") or release_data.get("mb_release_id", "")
    discogs_id = release_id.replace("discogs:", "") if release_id else ""
    if username and token and discogs_id:
        await loop.run_in_executor(
            None, lambda: cat.add_to_discogs_collection(username, token, discogs_id)
        )
    return {"ok": True, "album_id": album_id}
