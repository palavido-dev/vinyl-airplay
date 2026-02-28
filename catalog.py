#!/usr/bin/env python3
"""
Vinyl AirPlay — Record Catalog
Chromaprint fingerprinting → AcoustID lookup (once) → SQLite storage → local matching forever.

Flow:
  1. Audio callback feeds FingerprintBuffer
  2. Background task samples 15s of audio every 20s
  3. Writes temp WAV, runs fpcalc to get fingerprint
  4. Checks local SQLite first  →  if match, done (no internet)
  5. If no local match + API key set  →  AcoustID lookup + MusicBrainz metadata
  6. Saves everything to SQLite including artwork
  7. On all future plays: step 4 matches locally, steps 5-6 never run again
"""

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import requests
import wave
from pathlib import Path
from typing import Optional

import musicbrainzngs
import numpy as np
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

SAMPLE_RATE         = 44100
CHANNELS            = 2
FINGERPRINT_SECS    = 20          # 20s gives ~10 overlapping windows of 10s → enough votes
                                  # for confident matching while keeping fpcalc fast (~0.4s on Pi)
FINGERPRINT_WINDOW_SECS = 10.0    # canonical stored window size — ALL fingerprints in the DB
                                  # must be this duration so comparison windows line up correctly
FINGERPRINT_WINDOW_STEP = 3.0     # step between stored windows during learning
FINGERPRINT_INTERVAL = 8          # seconds between recognition attempts while unmatched
                                  # 8s → first match at ~15s after needle drop
MIN_SIMILARITY      = 0.60        # minimum per-window similarity to count as a vote
MIN_VOTES           = 2           # minimum votes required to declare a match
                                  # prevents single-window false positives (votes=1 is noise)
ARTWORK_SIZE        = 600         # px — artwork stored at this square size
DB_PATH             = Path("catalog.db")
ARTWORK_DIR         = Path("artwork")
ALBUM_AUDIO_DIR     = Path("album_audio")
MB_APP              = "VinylAirPlay/1.0 (local)"  # MusicBrainz user-agent


# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS albums (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    title                   TEXT    NOT NULL,
    artist                  TEXT    NOT NULL,
    year                    INTEGER,
    genre                   TEXT,
    label                   TEXT,
    country                 TEXT,
    musicbrainz_release_id  TEXT,
    artwork_path            TEXT,       -- fetched from Cover Art Archive
    user_artwork_path       TEXT,       -- user's own photo (takes priority)
    notes                   TEXT,
    created_at              TEXT DEFAULT (datetime('now')),
    updated_at              TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tracks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id                INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    title                   TEXT    NOT NULL,
    artist                  TEXT,
    track_number            TEXT,
    side                    TEXT,       -- 'A' or 'B'
    duration_secs           INTEGER,
    acoustid                TEXT,
    musicbrainz_track_id    TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    fingerprint TEXT    NOT NULL,   -- JSON array of raw ints from fpcalc -raw
    duration    REAL    NOT NULL,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER REFERENCES tracks(id),
    album_id    INTEGER REFERENCES albums(id),
    played_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS album_audio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id    INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    side        TEXT,               -- 'A', 'B', 'C', 'D', or NULL for full album
    file_path   TEXT NOT NULL,      -- relative path: album_audio/Artist - Album - SideA.flac
    format      TEXT DEFAULT 'flac',
    duration_secs REAL,
    file_size   INTEGER,            -- bytes
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_plays_album  ON plays(album_id);
CREATE INDEX IF NOT EXISTS idx_plays_track  ON plays(track_id);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_album_audio  ON album_audio(album_id);
"""


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    ARTWORK_DIR.mkdir(exist_ok=True)
    ALBUM_AUDIO_DIR.mkdir(exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    # Migrations for existing databases — add columns if missing
    _migrate_db(db)
    db.commit()
    db.close()
    purge_oversized_fingerprints()


def _migrate_db(db: sqlite3.Connection):
    """Add new columns/tables to existing databases without breaking anything."""
    # Check if tracks table has start_secs/end_secs columns
    cols = {row[1] for row in db.execute("PRAGMA table_info(tracks)").fetchall()}
    if "start_secs" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN start_secs REAL")
        print("[catalog] Migration: added tracks.start_secs")
    if "end_secs" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN end_secs REAL")
        print("[catalog] Migration: added tracks.end_secs")


# ── Fingerprint Buffer ────────────────────────────────────────────────────────

class FingerprintBuffer:
    """
    Accumulates PCM audio from the sounddevice callback.
    Thread-safe — put() called from audio thread, get_wav() from background thread.
    """

    def __init__(self, target_secs: int = FINGERPRINT_SECS):
        self._lock       = threading.Lock()
        self._chunks     = []
        self._target     = int(target_secs * SAMPLE_RATE * CHANNELS * 2)  # int16 bytes
        self._total      = 0

    def put(self, pcm: bytes):
        with self._lock:
            self._chunks.append(pcm)
            self._total += len(pcm)
            # Keep only the most recent target_secs worth of audio
            while self._total > self._target * 2 and len(self._chunks) > 1:
                removed = self._chunks.pop(0)
                self._total -= len(removed)

    def ready(self) -> bool:
        with self._lock:
            return self._total >= self._target

    def get_wav(self) -> Optional[bytes]:
        """
        Return a complete WAV file as bytes, or None if not enough data or too quiet.
        Checks RMS level to avoid fingerprinting silence between tracks.
        """
        with self._lock:
            if self._total < self._target:
                return None
            pcm = b"".join(self._chunks)
            pcm = pcm[-self._target:]  # trim to exactly target_secs

        # Silence check on raw PCM (no WAV header) — skip if RMS below -50 dBFS
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        db  = 20 * np.log10(rms + 1e-9)
        print(f"[catalog] Audio level: RMS={rms:.5f} ({db:.1f} dBFS)")
        if rms < 0.003:  # ~-50 dBFS
            print(f"[catalog] Audio too quiet — is the needle on the record?")
            return None

        # Build WAV in memory
        import io
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # int16
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._total = 0


# ── Chromaprint Fingerprinting ────────────────────────────────────────────────

def fingerprint_wav(wav_bytes: bytes) -> Optional[tuple[list[int], str, float]]:
    """
    Run fpcalc on a WAV file in memory.
    Returns (raw_ints, compressed_str, duration_secs) or None on failure.

    raw_ints       — signed int32 list for local BER comparison
    compressed_str — chromaprint-compressed base64 string for AcoustID API
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name

    try:
        # Two calls: -raw for local matching ints, plain for API-ready compressed str
        r_raw = subprocess.run(["fpcalc", "-raw", "-json", "-length", "120", tmp_path],
                               capture_output=True, text=True, timeout=30)
        r_enc = subprocess.run(["fpcalc", "-json", "-length", "120", tmp_path],
                               capture_output=True, text=True, timeout=30)

        if r_raw.returncode != 0 or r_enc.returncode != 0:
            print(f"[catalog] fpcalc error: {(r_raw.stderr or r_enc.stderr).strip()}")
            return None

        d_raw = json.loads(r_raw.stdout)
        d_enc = json.loads(r_enc.stdout)

        raw_ints       = d_raw["fingerprint"]   # list of signed int32
        compressed_str = d_enc["fingerprint"]   # chromaprint-encoded base64url string
        duration       = d_raw["duration"]

        print(f"[catalog] fpcalc: {len(raw_ints)} raw ints, duration={duration:.1f}s")

        if len(raw_ints) < 100:
            print("[catalog] Fingerprint too short — audio may be silent or below threshold")
            return None

        return raw_ints, compressed_str, duration

    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, FileNotFoundError) as e:
        print(f"[catalog] fingerprint_wav exception: {e}")
        return None
    finally:
        os.unlink(tmp_path)


# ── Local Fingerprint Matching ────────────────────────────────────────────────

def _compare_fingerprints(
    fp_np_a: np.ndarray,
    fp_np_b: np.ndarray,
    *,
    min_overlap: int = 40,
) -> float:
    """
    Bidirectional offset-aware BER comparison of two Chromaprint fingerprint arrays.

    Accepts pre-converted numpy uint8 arrays (4 bytes per chromaprint int, from
    np.array(ints, dtype=np.int32).view(np.uint8)). Pre-conversion is done once
    per live window in match_local, and stored arrays are cached at load time.

    Searches offsets 0, 1, -1, 2, -2, ... so the common case (near-zero offset)
    is found quickly and the early exit fires fast. step=1 is required because
    step=3 starting from a negative offset will skip offset=0 entirely, and
    chromaprint ints at adjacent positions share ~0% bits by design.
    """
    nl_bytes = len(fp_np_a)
    ns_bytes = len(fp_np_b)
    if nl_bytes < ns_bytes:
        fp_np_a, fp_np_b = fp_np_b, fp_np_a
        nl_bytes, ns_bytes = ns_bytes, nl_bytes

    nl = nl_bytes // 4   # number of chromaprint ints in longer
    ns = ns_bytes // 4   # number in shorter
    if min(nl, ns) < min_overlap:
        return 0.0

    pad     = ns // 3
    off_max = (nl - ns) + pad

    best = 0.0

    # Spiral outward from 0: 0, 1, -1, 2, -2, ...
    # This hits the true alignment (usually near 0) first → early exit fires fast
    for step in range(0, off_max + pad + 1):
        for off in ([step] if step == 0 else [step, -step]):
            if off > off_max or off < -pad:
                continue
            l_s = max(0, off) * 4
            l_e = min(nl_bytes, (off + ns) * 4)
            s_s = max(0, -off) * 4
            s_e = s_s + (l_e - l_s)
            if s_e > ns_bytes:
                s_e = ns_bytes
                l_e = l_s + (s_e - s_s)
            overlap_bytes = l_e - l_s
            if overlap_bytes < min_overlap * 4:
                continue
            errors = int(np.unpackbits(
                np.bitwise_xor(fp_np_a[l_s:l_e], fp_np_b[s_s:s_e])
            ).sum())
            sim = 1.0 - (errors / (overlap_bytes * 8.0))
            if sim > best:
                best = sim
                if best >= 0.88:
                    return best
    return best


# Simple in-memory cache to avoid re-loading and JSON-parsing the full DB on every attempt.
# For a ~200-record collection this is plenty and keeps recognition snappy on a Pi.
_FP_CACHE = {
    "count":     None,   # total row count — used to detect DB changes
    "rows":      [],     # list[tuple[int, np.ndarray]] kept for compatibility
    "matrix":    None,   # np.ndarray shape (N, window_bytes) — all stored fps stacked
    "track_ids": None,   # np.ndarray shape (N,) dtype int32 — parallel track IDs
}

# Popcount lookup table — faster than np.unpackbits (avoids 8× memory expansion)
_POPCOUNT_LUT = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint16)

def _refresh_fingerprint_cache(db: sqlite3.Connection, force: bool = False) -> None:
    """Refresh cached fingerprints if DB has changed (or force=True)."""
    try:
        cnt_row = db.execute("SELECT COUNT(*) AS n FROM fingerprints").fetchone()
        cnt = int(cnt_row["n"]) if cnt_row else 0
    except Exception:
        cnt = 0

    if (not force) and (_FP_CACHE["count"] == cnt) and _FP_CACHE["rows"]:
        return

    rows = db.execute("SELECT track_id, fingerprint FROM fingerprints").fetchall()
    parsed = []
    for r in rows:
        try:
            ints = json.loads(r["fingerprint"])
            np_bytes = np.array(ints, dtype=np.uint32).view(np.uint8).copy()
            parsed.append((int(r["track_id"]), np_bytes))
        except Exception:
            continue

    _FP_CACHE["count"] = cnt
    _FP_CACHE["rows"]  = parsed
    if parsed:
        # Stack into matrix for vectorized matching — shape (N, window_bytes)
        # Pad shorter arrays to max length so all rows have equal width
        max_len = max(len(p[1]) for p in parsed)
        mat = np.zeros((len(parsed), max_len), dtype=np.uint8)
        for i, (_, arr) in enumerate(parsed):
            mat[i, :len(arr)] = arr
        _FP_CACHE["matrix"]    = mat
        _FP_CACHE["track_ids"] = np.array([p[0] for p in parsed], dtype=np.int32)
    else:
        _FP_CACHE["matrix"]    = None
        _FP_CACHE["track_ids"] = None
    print(f"[catalog] Fingerprint cache refreshed: {len(parsed)} fingerprints")

def match_local(fingerprint: list[int], duration: float = FINGERPRINT_SECS) -> Optional[dict]:
    """
    Compare a live fingerprint against stored fingerprints using a voting approach.

    The live buffer is 120s (~948 ints). Stored windows are 10s (~78 ints).
    Rather than comparing the full 120s against each 10s window (which causes
    the alignment search to collapse to only ±28 positions), we slice the live
    fingerprint into 10s windows and let each window vote independently.
    The track that wins the most votes AND clears MIN_SIMILARITY wins.
    """
    if not fingerprint:
        return None

    db = get_db()
    try:
        _refresh_fingerprint_cache(db)
        if not _FP_CACHE["rows"]:
            return None

        # Derive ints/sec from the live fpcalc call — precise and consistent
        fp_rate     = len(fingerprint) / duration if duration > 0 else 7.0
        window_size = max(40, int(FINGERPRINT_WINDOW_SECS * fp_rate))
        step_size   = max(1, window_size // 2)  # 50% overlap

        # Slice live fingerprint into windows matching stored window size
        live_windows = []
        n = len(fingerprint)
        if n <= window_size:
            live_windows = [fingerprint]
        else:
            pos = 0
            while pos + window_size <= n:
                live_windows.append(fingerprint[pos:pos + window_size])
                pos += step_size
            # Always include the tail
            tail = fingerprint[max(0, n - window_size):]
            if tail not in live_windows[-2:]:
                live_windows.append(tail)

        mat       = _FP_CACHE["matrix"]
        track_ids = _FP_CACHE["track_ids"]
        if mat is None:
            return None

        col_width = mat.shape[1]  # bytes per stored window = window_ints * 4

        # Pre-convert live windows to numpy uint8 arrays
        live_np_windows = [
            np.array(w, dtype=np.uint32).view(np.uint8).copy()
            for w in live_windows
        ]

        # For each live window, try a small set of offsets against the full matrix.
        # Vectorized: one XOR on (N, bytes) matrix per offset → ~5ms vs ~500ms loop.
        # Offsets to try: 0 first (most common alignment), then ±1..±3 (covers ~0.4s drift)
        OFFSETS = [0, 1, -1, 2, -2, 3, -3]

        votes: dict = {}

        for live_np in live_np_windows:
            best_score = 0.0
            best_idx   = -1

            # Try offset 0 first (most common alignment) — single matrix op
            # Clamp to the shorter of live_np and mat column width to avoid
            # broadcast errors when the live window differs from stored window size
            cw = min(len(live_np), col_width)
            xor  = np.bitwise_xor(live_np[:cw], mat[:, :cw])
            bits = _POPCOUNT_LUT[xor].sum(axis=1)
            sims = 1.0 - bits.astype(np.float32) / (cw * 8.0)
            best_idx   = int(sims.argmax())
            best_score = float(sims[best_idx])

            # Only try shifted offsets if offset-0 wasn't confident
            if best_score < 0.88:
                for off in [1, -1, 2, -2, 3, -3]:
                    a_s = max(0,  off) * 4
                    b_s = max(0, -off) * 4
                    length = min(cw, col_width) - abs(off) * 4
                    if length < 40 * 4:
                        continue
                    live_slice = live_np[a_s:a_s + length]
                    if len(live_slice) < 40 * 4:
                        continue
                    xor2  = np.bitwise_xor(live_slice, mat[:, b_s:b_s + length])
                    bits2 = _POPCOUNT_LUT[xor2].sum(axis=1)
                    sims2 = 1.0 - bits2.astype(np.float32) / (length * 8.0)
                    ci    = int(sims2.argmax())
                    cs    = float(sims2[ci])
                    if cs > best_score:
                        best_score = cs
                        best_idx   = ci
                        if best_score >= 0.88:
                            break

            if best_score >= MIN_SIMILARITY and best_idx >= 0:
                tid  = int(track_ids[best_idx])
                prev = votes.get(tid, (0.0, 0))
                votes[tid] = (prev[0] + best_score, prev[1] + 1)

        if not votes:
            return None

        # Winner = most votes; ties broken by total score
        best_track_id = max(votes, key=lambda tid: (votes[tid][1], votes[tid][0]))
        total_score, vote_count = votes[best_track_id]
        avg_score = total_score / vote_count

        # Require minimum votes to avoid single-window false positives
        if vote_count < MIN_VOTES:
            print(f"[catalog] Weak match discarded: votes={vote_count} < MIN_VOTES={MIN_VOTES} "
                  f"(best score={avg_score:.3f}) — waiting for more audio")
            return None

        match = _get_track_full(db, best_track_id)
        if match:
            match["match_score"] = round(avg_score, 3)
            match["match_votes"] = vote_count
        return match
    finally:
        db.close()


def _get_track_full(db: sqlite3.Connection, track_id: int) -> Optional[dict]:
    """Fetch full track + album info as a dict."""
    row = db.execute("""
        SELECT t.id as track_id, t.title as track_title, t.artist as track_artist,
               t.track_number, t.side, t.duration_secs,
               a.id as album_id, a.title as album_title, a.artist as album_artist,
               a.year, a.genre, a.label,
               a.artwork_path, a.user_artwork_path
        FROM tracks t
        JOIN albums a ON t.album_id = a.id
        WHERE t.id = ?
    """, (track_id,)).fetchone()
    if not row:
        return None
    return dict(row)


# ── AcoustID + MusicBrainz Lookup ─────────────────────────────────────────────

def lookup_acoustid(fingerprint: str, duration: float, client_key: str) -> Optional[dict]:
    """
    Lookup an AcoustID by Chromaprint fingerprint.

    We request only recording IDs from AcoustID, then fetch human-friendly metadata
    from MusicBrainz (recording + releases).

    Returns a dict compatible with save_identified_track():
      {
        track_title, track_artist,
        album_title, album_artist,
        year, acoustid,
        mb_recording, mb_release,
        duration_secs
      }
    """
    client_key = (client_key or "").strip()
    if not client_key:
        return None

    try:
        params = {
            "client": client_key,
            "meta": "recordingids",
            "duration": str(int(round(duration))),
            "fingerprint": fingerprint,
            "format": "json",
        }
        # POST is preferred for long fingerprints; keep it simple: application/x-www-form-urlencoded
        resp = requests.post("https://api.acoustid.org/v2/lookup", data=params, timeout=15)
        data = resp.json()
        if data.get("status") != "ok":
            print(f"[catalog] AcoustID error: {data.get('error', 'unknown')}")
            return None

        results = data.get("results") or []
        if not results:
            print("[catalog] AcoustID: no results")
            return None

        # Choose highest-score AcoustID result
        results.sort(key=lambda r: float(r.get("score", 0.0) or 0.0), reverse=True)
        best = results[0]
        acoustid_id = best.get("id")
        score = float(best.get("score", 0.0) or 0.0)

        recordings = best.get("recordings") or []
        if not recordings:
            print("[catalog] AcoustID: result had no recordings")
            return None

        # Prefer first recording ID (often best); you can later improve heuristics if needed
        mb_recording = recordings[0].get("id")
        if not mb_recording:
            return None

        mb_meta = _lookup_musicbrainz_recording(mb_recording)
        if not mb_meta:
            return None

        mb_meta["acoustid"] = acoustid_id
        mb_meta["acoustid_score"] = round(score, 3)
        return mb_meta

    except Exception as e:
        print(f"[catalog] AcoustID lookup failed: {e}")
        return None


def _lookup_musicbrainz_recording(mb_recording_id: str) -> Optional[dict]:
    """Fetch title/artist and a reasonable album candidate from MusicBrainz."""
    if not mb_recording_id:
        return None

    musicbrainzngs.set_useragent(*MB_APP.split("/", 1))

    def _artist_from_credit(credit) -> str:
        if not credit:
            return ""
        parts = []
        for c in credit:
            if isinstance(c, dict):
                a = c.get("artist", {})
                parts.append(a.get("name") or c.get("name") or "")
            elif isinstance(c, str):
                parts.append(c)
        return " ".join([p for p in parts if p]).strip()

    try:
        rec = musicbrainzngs.get_recording_by_id(
            mb_recording_id, includes=["artists", "releases"]
        )
        recording = rec.get("recording", {})

        track_title = recording.get("title") or "Unknown Track"
        track_artist = _artist_from_credit(recording.get("artist-credit")) or "Unknown Artist"

        # Pick a release (album) if present. Heuristic: choose the first one returned.
        releases = recording.get("release-list") or []
        mb_release = releases[0].get("id") if releases else None
        album_title = releases[0].get("title") if releases else "Unknown Album"

        # Album artist is usually same as track artist for most records; can be refined via release lookup if needed.
        album_artist = track_artist

        year = None
        if releases and releases[0].get("date"):
            try:
                year = int(str(releases[0]["date"])[:4])
            except Exception:
                year = None

        return {
            "track_title": track_title,
            "track_artist": track_artist,
            "album_title": album_title,
            "album_artist": album_artist,
            "year": year,
            "mb_recording": mb_recording_id,
            "mb_release": mb_release,
            "duration_secs": 0,
        }
    except Exception as e:
        print(f"[catalog] MusicBrainz recording lookup failed: {e}")
        return None

def enrich_from_musicbrainz(mb_release_id: str) -> dict:
    """
    Fetch additional metadata from MusicBrainz for a release ID.
    Returns dict with genre, label, country, tracks.
    """
    if not mb_release_id:
        return {}

    musicbrainzngs.set_useragent(*MB_APP.split("/", 1))

    try:
        result = musicbrainzngs.get_release_by_id(
            mb_release_id,
            includes=["artists", "recordings", "release-groups", "labels", "tags"]
        )
        release = result.get("release", {})

        # Genre from tags
        tags   = release.get("tag-list", [])
        genre  = tags[0]["name"].title() if tags else None

        # Label
        label_info = release.get("label-info-list", [])
        label      = None
        if label_info:
            lbl   = label_info[0].get("label", {})
            label = lbl.get("name")

        country = release.get("country")

        # Track listing
        tracks = []
        medium_list = release.get("medium-list", [])
        for medium in medium_list:
            position = medium.get("position", 1)
            side     = chr(64 + int(position)) if position <= 26 else str(position)
            for track in medium.get("track-list", []):
                rec = track.get("recording", {})
                tracks.append({
                    "title":        rec.get("title", track.get("title", "Unknown")),
                    "track_number": track.get("number"),
                    "side":         side,
                    "duration_secs": int(rec.get("length", 0) or 0) // 1000,
                    "mb_id":        rec.get("id"),
                })

        return {
            "genre":   genre,
            "label":   label,
            "country": country,
            "tracks":  tracks,
        }

    except Exception as e:
        print(f"[catalog] MusicBrainz enrichment failed: {e}")
        return {}




# ── MusicBrainz Search ────────────────────────────────────────────────────────

def search_musicbrainz(artist: str, album: str, limit: int = 8) -> list[dict]:
    """
    Search MusicBrainz for releases matching artist + album.
    Returns a list of release summaries for the user to pick from.
    No API key required.
    """
    musicbrainzngs.set_useragent(*MB_APP.split("/", 1))
    try:
        result = musicbrainzngs.search_releases(
            artist=artist, release=album, limit=limit
        )
        releases = result.get("release-list", [])
        out = []
        for r in releases:
            artist_credit = r.get("artist-credit", [])
            artist_name = ""
            if artist_credit:
                first = artist_credit[0]
                if isinstance(first, dict):
                    artist_name = first.get("artist", {}).get("name", "")
            out.append({
                "id":      r.get("id"),
                "title":   r.get("title", ""),
                "artist":  artist_name,
                "date":    r.get("date", ""),
                "country": r.get("country", ""),
                "label":   (r.get("label-info-list") or [{}])[0].get("label", {}).get("name", "") if r.get("label-info-list") else "",
                "tracks":  r.get("medium-list", [{}])[0].get("track-count", 0) if r.get("medium-list") else 0,
            })
        return out
    except Exception as e:
        print(f"[catalog] MusicBrainz search failed: {e}")
        return []


def search_discogs(artist: str, album: str, token: str = "", limit: int = 8) -> list[dict]:
    """
    Search Discogs for vinyl releases matching artist + album.
    Pass a personal access token for higher rate limits (60/min vs 25/min).
    Get one free at https://www.discogs.com/settings/developers
    """
    import urllib.request, urllib.parse
    params = {"type": "release", "format": "Vinyl", "per_page": limit, "page": 1}
    if artist: params["artist"] = artist
    if album:  params["release_title"] = album
    url = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": MB_APP}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    try:
        req  = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        out = []
        for r in data.get("results", []):
            # Discogs title is usually "Artist - Album" or just "Album"
            full_title = r.get("title", "")
            if " - " in full_title:
                d_artist, d_album = full_title.split(" - ", 1)
            else:
                d_artist, d_album = r.get("artist", [""])[0] if r.get("artist") else "", full_title
            year = str(r.get("year", "")) or (r.get("labels") or [{}])[0].get("catno", "")
            out.append({
                "id":      str(r.get("id", "")),
                "title":   d_album,
                "artist":  d_artist,
                "date":    str(r.get("year", "")),
                "country": r.get("country", ""),
                "label":   (r.get("label") or [""])[0],
                "tracks":  len(r.get("tracklist") or []),
                "format":  ", ".join(r.get("format") or []),
                "catno":   (r.get("labels") or [{}])[0].get("catno", ""),
                "barcode": (r.get("barcode") or [""])[0] if r.get("barcode") else "",
                "thumb":   r.get("thumb", ""),
                "source":  "discogs",
            })
        return out
    except Exception as e:
        print(f"[catalog] Discogs search failed: {e}")
        return []


def get_discogs_release(discogs_id: str, token: str = "") -> dict:
    """
    Fetch full track listing and metadata for a Discogs release ID.
    Returns same shape as get_release_tracks() for drop-in compatibility.
    """
    import urllib.request
    url = f"https://api.discogs.com/releases/{discogs_id}"
    headers = {"User-Agent": MB_APP}
    if token:
        headers["Authorization"] = f"Discogs token={token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())

        # Artist
        artists = data.get("artists") or []
        artist  = " & ".join(a.get("name", "").rstrip(" (0123456789)") for a in artists)

        # Label + catno
        labels = data.get("labels") or []
        label  = labels[0].get("name", "") if labels else ""
        catno  = labels[0].get("catno", "") if labels else ""

        # Tracks — Discogs uses position like "A1", "A2", "B1"
        tracks = []
        for t in data.get("tracklist", []):
            pos = t.get("position", "")
            # Parse side from position: A1→side=A, B2→side=B, 1→side=A, etc.
            if pos and pos[0].isalpha():
                side = pos[0].upper()
                num  = pos[1:] or str(len([x for x in tracks if x["side"]==side])+1)
            else:
                side = "A"
                num  = pos or str(len(tracks)+1)

            # Duration
            dur_str = t.get("duration", "")
            dur_secs = 0
            if dur_str and ":" in dur_str:
                try:
                    m, s = dur_str.split(":")
                    dur_secs = int(m)*60 + int(s)
                except Exception:
                    pass

            tracks.append({
                "title":         t.get("title", ""),
                "track_number":  num,
                "side":          side,
                "duration_secs": dur_secs,
                "musicbrainz_track_id": None,
            })

        # Cover art: use primary image if available
        images    = data.get("images") or []
        art_url   = next((i["uri"] for i in images if i.get("type") == "primary"), None)
        art_url   = art_url or (images[0]["uri"] if images else None)

        return {
            "ok": True,
            "release": {
                "id":          f"discogs:{discogs_id}",
                "mb_id":       None,
                "title":       data.get("title", ""),
                "artist":      artist,
                "year":        str(data.get("year", "") or ""),
                "label":       label,
                "catno":       catno,
                "country":     data.get("country", ""),
                "genre":       ", ".join(data.get("genres") or []),
                "style":       ", ".join(data.get("styles") or []),
                "tracks":      tracks,
                "artwork_url": art_url,
                "source":      "discogs",
            }
        }
    except Exception as e:
        print(f"[catalog] Discogs release fetch failed: {e}")
        return {"ok": False, "error": str(e)}


def get_release_tracks(mb_release_id: str) -> dict:
    """
    Fetch full track listing for a MusicBrainz release ID.
    Returns dict with album info and tracks list ready for display/saving.
    """
    musicbrainzngs.set_useragent(*MB_APP.split("/", 1))
    try:
        result = musicbrainzngs.get_release_by_id(
            mb_release_id,
            includes=["artists", "recordings", "labels", "tags", "release-groups"]
        )
        release = result.get("release", {})

        # Artist
        artist_credit = release.get("artist-credit", [])
        artist = ""
        if artist_credit:
            first = artist_credit[0]
            if isinstance(first, dict):
                artist = first.get("artist", {}).get("name", "")

        # Year
        year = None
        date = release.get("date", "")
        if date:
            try: year = int(str(date)[:4])
            except (ValueError, TypeError): pass

        # Genre from tags
        tags  = release.get("tag-list", [])
        genre = tags[0]["name"].title() if tags else None

        # Label
        label_info = release.get("label-info-list", [])
        label = None
        if label_info:
            label = label_info[0].get("label", {}).get("name")

        country = release.get("country")

        # Tracks — grouped by medium (side)
        tracks = []
        medium_list = release.get("medium-list", [])
        for medium in medium_list:
            position = medium.get("position", 1)
            # Convert position to side letter: 1=A, 2=B, 3=C, 4=D
            side = chr(64 + int(position)) if int(position) <= 26 else str(position)
            for track in medium.get("track-list", []):
                rec = track.get("recording", {})
                duration_ms = rec.get("length") or track.get("length") or 0
                tracks.append({
                    "title":        rec.get("title") or track.get("title", "Unknown"),
                    "track_number": track.get("number", ""),
                    "side":         side,
                    "duration_secs": int(duration_ms) // 1000,
                    "mb_id":        rec.get("id", ""),
                })

        return {
            "mb_release_id": mb_release_id,
            "title":   release.get("title", ""),
            "artist":  artist,
            "year":    year,
            "genre":   genre,
            "label":   label,
            "country": country,
            "tracks":  tracks,
        }

    except Exception as e:
        print(f"[catalog] get_release_tracks failed: {e}")
        return {}


def save_release_to_catalog(release_data: dict,
                             fingerprint: Optional[list[int]] = None,
                             duration: Optional[float] = None) -> Optional[int]:
    """
    Save a full MusicBrainz release (album + all tracks) to the catalog.
    Returns the new album_id, or None on failure.
    """
    db = get_db()
    try:
        # Check if album already exists
        existing = db.execute(
            "SELECT id FROM albums WHERE musicbrainz_release_id = ?",
            (release_data.get("mb_release_id"),)
        ).fetchone()
        if existing:
            return existing["id"]

        cur = db.execute("""
            INSERT INTO albums
                (title, artist, year, genre, label, country, musicbrainz_release_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            release_data.get("title", "Unknown Album"),
            release_data.get("artist", "Unknown Artist"),
            release_data.get("year"),
            release_data.get("genre"),
            release_data.get("label"),
            release_data.get("country"),
            release_data.get("mb_release_id"),
        ))
        album_id = cur.lastrowid

        for t in release_data.get("tracks", []):
            cur2 = db.execute("""
                INSERT INTO tracks
                    (album_id, title, artist, track_number, side,
                     duration_secs, musicbrainz_track_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                album_id,
                t.get("title", "Unknown"),
                release_data.get("artist"),
                t.get("track_number"),
                t.get("side"),
                t.get("duration_secs", 0),
                t.get("mb_id"),
            ))
            # Save fingerprint against first track if provided
            if fingerprint and duration and t == release_data["tracks"][0]:
                db.execute("""
                    INSERT INTO fingerprints (track_id, fingerprint, duration)
                    VALUES (?, ?, ?)
                """, (cur2.lastrowid, json.dumps(fingerprint), duration))

        db.commit()
        print(f"[catalog] Saved album '{release_data.get('title')}' with {len(release_data.get('tracks', []))} tracks")
        return album_id

    except Exception as e:
        print(f"[catalog] save_release_to_catalog failed: {e}")
        db.rollback()
        return None
    finally:
        db.close()

# ── Album Art ─────────────────────────────────────────────────────────────────

def fetch_artwork(mb_release_id: str, album_id: int) -> Optional[str]:
    """
    Download artwork from the MusicBrainz Cover Art Archive.
    Saves as JPEG to artwork/ dir. Returns relative path or None.
    """
    if not mb_release_id:
        return None

    url = f"https://coverartarchive.org/release/{mb_release_id}/front-500"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": MB_APP})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()

        path = _save_artwork(data, album_id, user=False)
        return path

    except Exception as e:
        print(f"[catalog] Artwork fetch failed: {e}")
        return None


def fetch_artwork_from_url(url: str, album_id: int) -> Optional[str]:
    """Download artwork from any URL (e.g. Discogs). Returns relative path or None."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": MB_APP})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        path = _save_artwork(data, album_id, user=False)
        print(f"[catalog] Artwork downloaded from {url[:60]}...")
        return path
    except Exception as e:
        print(f"[catalog] Artwork URL fetch failed: {e}")
        return None


def save_user_artwork(image_bytes: bytes, album_id: int) -> Optional[str]:
    """Save user-uploaded photo as album artwork. Returns relative path."""
    return _save_artwork(image_bytes, album_id, user=True)


def _save_artwork(image_bytes: bytes, album_id: int, user: bool) -> Optional[str]:
    """Resize and save artwork, return relative path."""
    try:
        import io
        img  = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img  = img.resize((ARTWORK_SIZE, ARTWORK_SIZE), Image.LANCZOS)
        suffix = "user" if user else "fetch"
        fname  = f"album_{album_id}_{suffix}.jpg"
        path   = ARTWORK_DIR / fname
        img.save(path, "JPEG", quality=90)
        return str(path)
    except Exception as e:
        print(f"[catalog] Artwork save failed: {e}")
        return None


# ── Database Write ────────────────────────────────────────────────────────────

def save_identified_track(
    acoustid_result: dict,
    mb_extra: dict,
    fingerprint: list[int],
    duration: float,
    artwork_path: Optional[str] = None,
) -> Optional[dict]:
    """
    Save a newly identified track + album to the DB.
    Returns the full track dict for broadcasting to the UI.
    """
    db = get_db()
    try:
        # Check if album already exists by MusicBrainz release ID
        mb_release = acoustid_result.get("mb_release")
        album_row  = None
        if mb_release:
            album_row = db.execute(
                "SELECT id FROM albums WHERE musicbrainz_release_id = ?",
                (mb_release,)
            ).fetchone()

        if album_row:
            album_id = album_row["id"]
        else:
            cur = db.execute("""
                INSERT INTO albums
                    (title, artist, year, genre, label, country,
                     musicbrainz_release_id, artwork_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                acoustid_result.get("album_title", "Unknown Album"),
                acoustid_result.get("album_artist", "Unknown Artist"),
                acoustid_result.get("year"),
                mb_extra.get("genre"),
                mb_extra.get("label"),
                mb_extra.get("country"),
                mb_release,
                artwork_path,
            ))
            album_id = cur.lastrowid

            # Save full track listing if we got it from MusicBrainz
            for t in mb_extra.get("tracks", []):
                db.execute("""
                    INSERT INTO tracks
                        (album_id, title, artist, track_number, side,
                         duration_secs, musicbrainz_track_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    album_id,
                    t["title"],
                    acoustid_result.get("album_artist"),
                    t.get("track_number"),
                    t.get("side"),
                    t.get("duration_secs"),
                    t.get("mb_id"),
                ))

        # Find or create the specific track being played
        track_title = acoustid_result.get("track_title", "Unknown Track")
        track_row   = db.execute(
            "SELECT id FROM tracks WHERE album_id = ? AND title = ?",
            (album_id, track_title)
        ).fetchone()

        if track_row:
            track_id = track_row["id"]
        else:
            cur = db.execute("""
                INSERT INTO tracks
                    (album_id, title, artist, duration_secs,
                     acoustid, musicbrainz_track_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                album_id,
                track_title,
                acoustid_result.get("track_artist"),
                acoustid_result.get("duration_secs"),
                acoustid_result.get("acoustid"),
                acoustid_result.get("mb_recording"),
            ))
            track_id = cur.lastrowid

        # Save fingerprint
        db.execute("""
            INSERT INTO fingerprints (track_id, fingerprint, duration)
            VALUES (?, ?, ?)
        """, (track_id, json.dumps(fingerprint), duration))

        db.commit()
        return _get_track_full(db, track_id)

    except Exception as e:
        print(f"[catalog] save_identified_track failed: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def save_manual_track(data: dict, fingerprint: Optional[list[int]] = None,
                      duration: Optional[float] = None) -> Optional[dict]:
    """
    Save a manually entered album + bulk track list to the DB.
    data["tracks"] is a list of {title, side, track_number} dicts.
    Falls back to single-track mode if no tracks list provided.
    """
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO albums (title, artist, year, genre, label, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.get("album_title", "Unknown Album"),
            data.get("album_artist", "Unknown Artist"),
            data.get("year"),
            data.get("genre"),
            data.get("label"),
            data.get("notes"),
        ))
        album_id = cur.lastrowid

        tracks = data.get("tracks")
        if not tracks:
            # Legacy single-track fallback
            tracks = [{"title": data.get("track_title", "Unknown Track"),
                       "side":  data.get("side", "A"),
                       "track_number": data.get("track_number", "1")}]

        last_track_id = None
        for t in tracks:
            cur2 = db.execute("""
                INSERT INTO tracks (album_id, title, artist, track_number, side)
                VALUES (?, ?, ?, ?, ?)
            """, (
                album_id,
                t.get("title", "Unknown Track"),
                data.get("album_artist"),
                t.get("track_number"),
                t.get("side", "A"),
            ))
            last_track_id = cur2.lastrowid

        if fingerprint and duration and last_track_id:
            db.execute("""
                INSERT INTO fingerprints (track_id, fingerprint, duration)
                VALUES (?, ?, ?)
            """, (last_track_id, json.dumps(fingerprint), duration))

        db.commit()
        print(f"[catalog] Saved manual album '{data.get('album_title')}' with {len(tracks)} tracks")
        return _get_track_full(db, last_track_id)
    except Exception as e:
        print(f"[catalog] save_manual_track failed: {e}")
        db.rollback()
        return None
    finally:
        db.close()



def save_fingerprint_for_album(album_id: int, fingerprint: list[int], duration: float) -> bool:
    """
    Save a fingerprint for the current moment of audio against this album.
    Saves against the first track that doesn't yet have a fingerprint,
    so repeated calls as the record plays through will progressively
    cover every track. If all tracks are already learned, adds an
    additional fingerprint to the first track (improves match confidence).
    """
    db = get_db()
    try:
        # Get all tracks for this album in play order
        tracks = db.execute(
            "SELECT id FROM tracks WHERE album_id = ? ORDER BY side, CAST(track_number AS INTEGER)",
            (album_id,)
        ).fetchall()
        if not tracks:
            print(f"[catalog] save_fingerprint_for_album: no tracks for album {album_id}")
            return False

        # Find the first track that has no fingerprint yet
        target_id = None
        for t in tracks:
            has_fp = db.execute(
                "SELECT 1 FROM fingerprints WHERE track_id = ?", (t["id"],)
            ).fetchone()
            if not has_fp:
                target_id = t["id"]
                break

        # All tracks already have fingerprints — add to first track for extra coverage
        if target_id is None:
            target_id = tracks[0]["id"]

        # Slice the raw fingerprint into canonical windows so they survive
        # the oversized-fingerprint purge (which removes any entry > 100 ints).
        # Each window is FINGERPRINT_WINDOW_SECS long, stepped every FINGERPRINT_WINDOW_STEP secs.
        fp_rate     = len(fingerprint) / max(duration, 1.0)  # ints/sec
        window_ints = max(40, int(FINGERPRINT_WINDOW_SECS * fp_rate))
        step_ints   = max(1, int(FINGERPRINT_WINDOW_STEP * fp_rate))
        windows     = []
        pos = 0
        while pos + window_ints <= len(fingerprint):
            windows.append(fingerprint[pos:pos + window_ints])
            pos += step_ints
        # Always include the tail so nothing is lost
        tail = fingerprint[max(0, len(fingerprint) - window_ints):]
        if tail not in windows:
            windows.append(tail)

        if not windows:
            windows = [fingerprint]  # fallback: save whole thing

        window_dur = FINGERPRINT_WINDOW_SECS
        for w in windows:
            db.execute(
                "INSERT INTO fingerprints (track_id, fingerprint, duration) VALUES (?, ?, ?)",
                (target_id, json.dumps(w), window_dur)
            )
        db.commit()

        # Count how many tracks still need fingerprints
        remaining = sum(
            1 for t in tracks
            if not db.execute("SELECT 1 FROM fingerprints WHERE track_id = ?", (t["id"],)).fetchone()
        )
        print(f"[catalog] Saved {len(windows)} fingerprint windows → track {target_id} "
              f"| {remaining} tracks still unlearned")
        return True
    except Exception as e:
        print(f"[catalog] save_fingerprint_for_album failed: {e}")
        db.rollback()
        return False
    finally:
        db.close()



# ── Fingerprint Slicing ───────────────────────────────────────────────────────

# Chromaprint produces ~7.8 integers per second of audio
CHROMA_RATE = 7.8   # ints/sec (empirical; computed as len(raw_ints)/duration)

def slice_fingerprint(
    raw_ints: list[int],
    duration: float,
    window_secs: float = 10.0,   # 10s = ~78 ints, fast ID, still reliable on vinyl
    step_secs:   float = 3.0,    # new window every 3s → ~20 windows per 3-min track
) -> list[tuple[list[int], float]]:
    """
    Slice a full-track fingerprint array into overlapping windows.

    Returns a list of (window_ints, window_duration) tuples.
    Each window is suitable for storage in the fingerprints table and
    comparison via _compare_fingerprints().

    Example: a 3-minute track → ~32 windows of 20s each, stepping every 5s.
    This means local matching will work regardless of where in the track
    the 20s capture window happens to land.
    """
    if not raw_ints or duration < window_secs:
        # Track shorter than window — store as-is
        return [(raw_ints, duration)]

    rate        = len(raw_ints) / duration   # actual ints/sec for this recording
    window_ints = max(10, int(window_secs * rate))
    step_ints   = max(1,  int(step_secs   * rate))
    n           = len(raw_ints)

    slices = []
    start  = 0
    while start + window_ints <= n:
        window = raw_ints[start : start + window_ints]
        slices.append((window, window_secs))
        start += step_ints

    # Always include a window anchored at the end (catches last few seconds)
    tail = raw_ints[max(0, n - window_ints):]
    if tail not in [s[0] for s in slices[-3:]]:   # avoid exact duplicates
        slices.append((tail, min(window_secs, duration)))

    print(f"[catalog] slice_fingerprint: {len(raw_ints)} ints / {duration:.1f}s "
          f"→ {len(slices)} windows")
    return slices


def save_track_fingerprints(
    track_id: int,
    raw_ints: list[int],
    duration: float,
    window_secs: float = 10.0,
    step_secs:   float = 3.0,
) -> int:
    """
    Save a full-track fingerprint and all its sliced windows to the DB.
    Returns the number of fingerprint rows inserted.
    Old fingerprints for this track are replaced.
    """
    db = get_db()
    try:
        # Clear existing fingerprints for this track
        db.execute("DELETE FROM fingerprints WHERE track_id = ?", (track_id,))

        rows_added = 0

        # 1. Save the full fingerprint (used by AcoustID path, future-proof)
        db.execute(
            "INSERT INTO fingerprints (track_id, fingerprint, duration) VALUES (?,?,?)",
            (track_id, json.dumps(raw_ints), duration)
        )
        rows_added += 1

        # 2. Save all sliced windows for local rolling-window matching
        for window, win_dur in slice_fingerprint(raw_ints, duration, window_secs, step_secs):
            db.execute(
                "INSERT INTO fingerprints (track_id, fingerprint, duration) VALUES (?,?,?)",
                (track_id, json.dumps(window), win_dur)
            )
            rows_added += 1

        db.commit()
        print(f"[catalog] save_track_fingerprints: track {track_id} → {rows_added} rows")
        return rows_added

    except Exception as e:
        print(f"[catalog] save_track_fingerprints failed: {e}")
        db.rollback()
        return 0
    finally:
        db.close()

# ── Play Logging ──────────────────────────────────────────────────────────────

def log_play(track_id: int, album_id: int):
    db = get_db()
    try:
        db.execute(
            "INSERT INTO plays (track_id, album_id) VALUES (?, ?)",
            (track_id, album_id)
        )
        db.commit()
    finally:
        db.close()


# ── Catalog Queries ───────────────────────────────────────────────────────────

def get_all_albums() -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("""
            SELECT a.*,
                   COUNT(DISTINCT t.id)   as track_count,
                   COUNT(DISTINCT p.id)   as play_count,
                   MAX(p.played_at)       as last_played,
                   (SELECT COUNT(*) FROM album_audio aa
                    WHERE aa.album_id = a.id) as audio_count,
                   (SELECT COUNT(DISTINCT f.track_id)
                    FROM fingerprints f
                    JOIN tracks t2 ON t2.id = f.track_id
                    WHERE t2.album_id = a.id) as learned_count
            FROM albums a
            LEFT JOIN tracks t ON t.album_id = a.id
            LEFT JOIN plays  p ON p.album_id = a.id
            GROUP BY a.id
            ORDER BY a.artist, a.title
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def clear_track_fingerprints(track_id: int) -> int:
    """Delete all fingerprints for a single track. Returns rows deleted."""
    db = get_db()
    try:
        cur = db.execute("DELETE FROM fingerprints WHERE track_id = ?", (track_id,))
        db.commit()
        print(f"[catalog] Cleared {cur.rowcount} fingerprints for track {track_id}")
        return cur.rowcount
    finally:
        db.close()


def clear_album_fingerprints(album_id: int) -> int:
    """Delete all fingerprints for every track in an album. Returns rows deleted."""
    db = get_db()
    try:
        cur = db.execute("""
            DELETE FROM fingerprints
            WHERE track_id IN (SELECT id FROM tracks WHERE album_id = ?)
        """, (album_id,))
        db.commit()
        print(f"[catalog] Cleared {cur.rowcount} fingerprints for album {album_id}")
        return cur.rowcount
    finally:
        db.close()


def purge_oversized_fingerprints() -> int:
    """
    Remove fingerprints that are larger than the canonical window size.
    Old recognition-time saves stored full 140-int live buffers instead of
    10s windows, polluting the DB. This cleans them up on startup.
    Canonical window ≈ 60-85 ints; full-buffer dumps were 120-200 ints.
    """
    db = get_db()
    try:
        rows = db.execute("SELECT id, fingerprint FROM fingerprints").fetchall()
        to_delete = []
        for r in rows:
            try:
                ints = json.loads(r["fingerprint"])
                if len(ints) > 100:
                    to_delete.append(r["id"])
            except Exception:
                to_delete.append(r["id"])
        total = len(rows)
        if to_delete:
            db.execute(
                "DELETE FROM fingerprints WHERE id IN ({})".format(
                    ",".join("?" * len(to_delete))), to_delete)
            db.commit()
            print(f"[catalog] Purged {len(to_delete)} oversized fingerprints "
                  f"(kept {total - len(to_delete)})")
        else:
            print(f"[catalog] Fingerprint check: {total} fingerprints OK (none oversized)")
        return len(to_delete)
    finally:
        db.close()


def reorder_album_tracks(ordered_track_ids: list) -> bool:
    """
    Update track_number for each track based on the supplied ordered list of IDs.
    Preserves side grouping — numbers are assigned per-side in the order given.
    E.g. [A1,A2,A3,B1,B2] → track_numbers 1,2,3,1,2 within their sides.
    """
    db = get_db()
    try:
        # Fetch current side for each track
        rows = db.execute(
            "SELECT id, side FROM tracks WHERE id IN ({})".format(
                ",".join("?" * len(ordered_track_ids))
            ),
            ordered_track_ids
        ).fetchall()
        side_map = {r["id"]: (r["side"] or "A") for r in rows}

        # Assign track_number per side in the order given
        side_counters: dict = {}
        for track_id in ordered_track_ids:
            side = side_map.get(track_id, "A")
            side_counters[side] = side_counters.get(side, 0) + 1
            db.execute(
                "UPDATE tracks SET track_number = ? WHERE id = ?",
                (str(side_counters[side]), track_id)
            )
        db.commit()
        print(f"[catalog] Reordered {len(ordered_track_ids)} tracks")
        return True
    except Exception as e:
        print(f"[catalog] reorder_album_tracks failed: {e}")
        return False
    finally:
        db.close()


def get_album_tracks(album_id: int) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("""
            SELECT t.*,
                   COUNT(DISTINCT p.id)  as play_count,
                   MAX(p.played_at)      as last_played,
                   COUNT(DISTINCT f.id)  as fingerprint_count
            FROM tracks t
            LEFT JOIN plays        p ON p.track_id = t.id
            LEFT JOIN fingerprints f ON f.track_id = t.id
            WHERE t.album_id = ?
            GROUP BY t.id
            ORDER BY t.side, CAST(t.track_number AS INTEGER)
        """, (album_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def get_recent_plays(limit: int = 20) -> list[dict]:
    db = get_db()
    try:
        rows = db.execute("""
            SELECT p.played_at,
                   t.title as track_title,
                   a.title as album_title,
                   a.artist,
                   a.user_artwork_path,
                   a.artwork_path
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            JOIN albums a ON p.album_id = a.id
            ORDER BY p.played_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def update_album_artwork(album_id: int, path: str, user: bool = True):
    db = get_db()
    try:
        field = "user_artwork_path" if user else "artwork_path"
        db.execute(
            f"UPDATE albums SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (path, album_id)
        )
        db.commit()
    finally:
        db.close()


def delete_album(album_id: int):
    db = get_db()
    try:
        db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        db.commit()
    finally:
        db.close()


# ── Album Audio (Full-Side Recordings) ───────────────────────────────────────

def save_album_audio(album_id: int, side: str, file_path: str,
                     duration_secs: float, file_size: int,
                     fmt: str = "flac") -> Optional[int]:
    """Save a full-side audio file record to the database. Returns row ID."""
    db = get_db()
    try:
        # Remove any existing audio for this album+side (re-recording replaces)
        db.execute(
            "DELETE FROM album_audio WHERE album_id = ? AND side = ?",
            (album_id, side)
        )
        cur = db.execute("""
            INSERT INTO album_audio
                (album_id, side, file_path, format, duration_secs, file_size)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (album_id, side, file_path, fmt, duration_secs, file_size))
        db.commit()
        print(f"[catalog] Saved album audio: album {album_id} side {side} "
              f"({duration_secs:.0f}s, {file_size / (1024*1024):.1f} MB)")
        return cur.lastrowid
    except Exception as e:
        print(f"[catalog] save_album_audio failed: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def get_album_audio(album_id: int) -> list[dict]:
    """Get all audio files for an album, ordered by side."""
    db = get_db()
    try:
        rows = db.execute("""
            SELECT * FROM album_audio
            WHERE album_id = ?
            ORDER BY side
        """, (album_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def get_album_audio_by_id(audio_id: int) -> Optional[dict]:
    """Get a single album audio record by its ID."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM album_audio WHERE id = ?", (audio_id,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def update_track_timestamps(track_id: int, start_secs: float, end_secs: float):
    """Set the start/end offsets for a track within its side's audio file."""
    db = get_db()
    try:
        db.execute(
            "UPDATE tracks SET start_secs = ?, end_secs = ? WHERE id = ?",
            (start_secs, end_secs, track_id)
        )
        db.commit()
    except Exception as e:
        print(f"[catalog] update_track_timestamps failed: {e}")
    finally:
        db.close()


def delete_album_audio(album_id: int) -> int:
    """Delete all audio files and DB records for an album. Returns files deleted."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT file_path FROM album_audio WHERE album_id = ?", (album_id,)
        ).fetchall()
        count = 0
        for r in rows:
            p = Path(r["file_path"])
            if p.exists():
                p.unlink()
                count += 1
        db.execute("DELETE FROM album_audio WHERE album_id = ?", (album_id,))
        db.commit()
        return count
    finally:
        db.close()


def delete_album_audio_by_id(audio_id: int) -> bool:
    """Delete a single album audio record and its file. Returns True if deleted."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT file_path FROM album_audio WHERE id = ?", (audio_id,)
        ).fetchone()
        if not row:
            return False
        p = Path(row["file_path"])
        if p.exists():
            p.unlink()
        db.execute("DELETE FROM album_audio WHERE id = ?", (audio_id,))
        db.commit()
        print(f"[catalog] Deleted album audio id={audio_id}: {p.name}")
        return True
    except Exception as e:
        print(f"[catalog] delete_album_audio_by_id failed: {e}")
        return False
    finally:
        db.close()


# ── Background Recogniser ─────────────────────────────────────────────────────

class Recogniser:
    """
    Background thread that periodically fingerprints buffered audio
    and tries to identify the track playing.

    on_match(track_dict) called when a track is identified.
    on_unknown() called when no match found after lookup attempt.

    Auto-learn mode: when set_auto_learn_album() is called, any unmatched
    fingerprint is automatically saved against the next unlearned track in
    that album, so the whole album gets learned just by playing it through.
    """

    def __init__(self, buffer: FingerprintBuffer,
                 on_match, on_unknown,
                 api_key: Optional[str] = None,
                 acoustid_enabled: bool = False):
        self._buffer          = buffer
        self._on_match        = on_match
        self._on_unknown      = on_unknown
        self._api_key         = api_key
        self._acoustid_enabled = acoustid_enabled
        self._stop            = threading.Event()
        self._thread          = None
        self._last_track_id: Optional[int] = None
        self._auto_learn_album_id: Optional[int] = None  # album being auto-learned
        self._learning_mode   = False  # when True, skip AcoustID lookups entirely
        self._matched         = False  # True after successful match; pauses attempts until reset

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="recogniser"
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_api_key(self, key: str):
        self._api_key = key

    def set_acoustid_enabled(self, enabled: bool):
        self._acoustid_enabled = enabled
        print(f"[catalog] AcoustID lookup {'enabled' if enabled else 'disabled'}")

    def reset_match(self):
        """Call when a new track starts — re-enables recognition attempts."""
        self._matched       = False
        self._last_track_id = None
        self._buffer.clear()
        print("[catalog] Recogniser: match reset — listening for next track")

    def set_learning_mode(self, enabled: bool):
        """Suppress AcoustID lookups while a learn session is active."""
        self._learning_mode = enabled
        print(f"[catalog] Recogniser: learning_mode={'ON — AcoustID suppressed' if enabled else 'OFF'}")

    def set_auto_learn_album(self, album_id: Optional[int]):
        """
        Enable auto-learn for a specific album.
        While active, any unmatched fingerprint is automatically saved
        against the next unlearned track in this album.
        Pass None to disable.
        """
        self._auto_learn_album_id = album_id
        if album_id:
            print(f"[catalog] Auto-learn enabled for album {album_id}")
        else:
            print("[catalog] Auto-learn disabled")

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(FINGERPRINT_INTERVAL)
            if self._stop.is_set():
                break
            if self._learning_mode:
                continue
            if self._matched:
                # Already identified — wait for reset_match() before trying again
                continue
            if not self._buffer.ready():
                continue
            self._attempt()

    def _attempt(self):
        wav = self._buffer.get_wav()
        if not wav:
            return

        result = fingerprint_wav(wav)
        if not result:
            print("[catalog] fpcalc failed or audio too quiet")
            return

        # fingerprint_wav now returns (raw_ints, compressed_str, duration)
        raw_ints, compressed_str, duration = result

        # 1. Local match first — no internet needed
        match = match_local(raw_ints, duration)
        if match:
            # Always save this fingerprint against the matched track so the
            # catalog builds up multiple fingerprints over time — one per
            # ~30s window — covering different sections of each track.
            # Note: we intentionally do NOT save fingerprints during recognition.
            # Doing so poisons the DB when a match is wrong (wrong-track audio saved
            # under wrong ID → cascading mismatches forever). Re-learn via Learn session.

            if match["track_id"] != self._last_track_id:
                self._last_track_id = match["track_id"]
                log_play(match["track_id"], match["album_id"])
                print(f"[catalog] Local match: {match['track_title']} — {match['album_title']} (score={match.get('match_score','?')}, votes={match.get('match_votes','?')})")
                self._on_match(match)
            # Stop attempting until the next track — avoids CPU spikes mid-song
            self._matched = True
            return

        # 2. Auto-learn: if we know which album is playing, save this fingerprint
        #    against the next unlearned track automatically
        if self._auto_learn_album_id is not None:
            saved = save_fingerprint_for_album(self._auto_learn_album_id, raw_ints, duration)
            if saved:
                # Find out which track we just learned and broadcast it
                tracks = get_album_tracks(self._auto_learn_album_id)
                albums = get_all_albums()
                album  = next((a for a in albums if a["id"] == self._auto_learn_album_id), None)
                if album and tracks:
                    # Find the track we most likely just saved to (first with a fingerprint
                    # that was just created — approximate by matching against db now)
                    match = match_local(raw_ints, duration)
                    if match and match["album_id"] == self._auto_learn_album_id:
                        if match["track_id"] != self._last_track_id:
                            self._last_track_id = match["track_id"]
                            log_play(match["track_id"], match["album_id"])
                            print(f"[catalog] Auto-learned: {match['track_title']}")
                            self._on_match(match)
                        return
                # Even if we can't immediately match back, don't call on_unknown
                print(f"[catalog] Auto-learned fingerprint for album {self._auto_learn_album_id}")
                return
        # 3. Online lookup via AcoustID if client key available
        if self._learning_mode:
            # Learn session active — we already know what album is playing,
            # no point making AcoustID calls
            self._on_unknown()
            return

        if not self._acoustid_enabled:
            self._on_unknown()
            return

        if not self._api_key:
            print("[catalog] No AcoustID client key set — skipping online lookup")
            self._on_unknown()
            return

        print("[catalog] No local match — querying AcoustID...")
        acoustid_result = lookup_acoustid(compressed_str, duration, self._api_key)
        if not acoustid_result:
            print("[catalog] AcoustID returned no match")
            self._on_unknown()
            return

        print(f"[catalog] AcoustID matched: {acoustid_result['track_title']} "
              f"— {acoustid_result['album_title']} (score={acoustid_result.get('acoustid_score')})")

        # Enrich with MusicBrainz release details if we have a release ID
        mb_extra = {}
        if acoustid_result.get("mb_release"):
            mb_extra = enrich_from_musicbrainz(acoustid_result["mb_release"])

        # Save to DB — don't pass raw_ints (full 140-int buffer = oversized);
        # fingerprints are only learned via explicit Learn sessions
        track = save_identified_track(
            acoustid_result, mb_extra, [], duration
        )
        if not track:
            self._on_unknown()
            return

        # Fetch artwork if we have a MusicBrainz release ID
        if acoustid_result.get("mb_release"):
            new_art = fetch_artwork(acoustid_result["mb_release"], track["album_id"])
            if new_art:
                update_album_artwork(track["album_id"], new_art, user=False)
                track["artwork_path"] = new_art

        self._last_track_id = track["track_id"]
        log_play(track["track_id"], track["album_id"])
        self._on_match(track)

