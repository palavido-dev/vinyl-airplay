def get_genres_with_count(min_count: int = 3) -> list[tuple]:
    """Get genres that have at least min_count albums. Returns [(genre, count), ...]."""
    all_albums = get_all_albums()
    genre_counts = {}
    for album in all_albums:
        genre = album.get("genre") or "Unknown"
        genre_counts[genre] = genre_counts.get(genre, 0) + 1
    return [(g, c) for g, c in genre_counts.items() if c >= min_count]


# ── Feature 3: Enhanced Stats ──────────────────────────────────────────────


def get_play_heatmap(months: int = 6) -> dict:
    """Returns a dict of {date: play_count} for the last N months."""
    db = get_db()
    try:
        from datetime import datetime, timedelta
        cutoff_date = datetime.now() - timedelta(days=months * 30)
        cutoff_str = cutoff_date.isoformat()

        result = db.execute("""
            SELECT DATE(played_at) as date, COUNT(*) as count
            FROM plays
            WHERE played_at >= ?
            GROUP BY DATE(played_at)
            ORDER BY date
        """, (cutoff_str,)).fetchall()

        heatmap = {}
        for row in result:
            heatmap[row["date"]] = row["count"]
        return heatmap
    finally:
        db.close()


def get_genre_stats() -> list:
    """Returns list of {genre, count} for all genres."""
    db = get_db()
    try:
        result = db.execute("""
            SELECT genre, COUNT(*) as count
            FROM albums
            WHERE genre IS NOT NULL AND genre != '' AND deleted_at IS NULL
            GROUP BY genre
            ORDER BY count DESC
        """).fetchall()
        return [dict(r) for r in result]
    finally:
        db.close()


def get_artist_stats(limit: int = 10) -> list:
    """Returns top N artists by album count, with play count info."""
    db = get_db()
    try:
        result = db.execute("""
            SELECT
                a.artist,
                COUNT(DISTINCT a.id) as album_count,
                COUNT(p.id) as play_count
            FROM albums a
            LEFT JOIN plays p ON p.album_id = a.id
            WHERE a.artist IS NOT NULL AND a.artist != '' AND a.deleted_at IS NULL
            GROUP BY a.artist
            ORDER BY album_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in result]
    finally:
        db.close()


def get_decade_stats() -> list:
    """Returns album count by decade."""
    db = get_db()
    try:
        result = db.execute("""
            SELECT
                CASE
                    WHEN year IS NULL THEN 'Unknown'
                    WHEN year < 1960 THEN 'Before 1960'
                    ELSE (CAST((year / 10) * 10 AS TEXT) || 's')
                END as decade,
                COUNT(*) as count
            FROM albums
            WHERE deleted_at IS NULL
            GROUP BY decade
            ORDER BY decade
        """).fetchall()
        return [dict(r) for r in result]
    finally:
        db.close()


def get_on_this_day() -> list:
    """Returns albums played on this month+day in prior years."""
    db = get_db()
    try:
        from datetime import datetime
        now = datetime.now()
        month_day = f"%{now.month:02d}-{now.day:02d}"

        result = db.execute("""
            SELECT
                DISTINCT a.id,
                a.title,
                a.artist,
                a.artwork_path,
                a.user_artwork_path,
                strftime('%Y', p.played_at) as year,
                p.played_at
            FROM plays p
            JOIN albums a ON p.album_id = a.id
            WHERE strftime('%m-%d', p.played_at) = ? AND a.deleted_at IS NULL
            ORDER BY p.played_at DESC
        """, (month_day,)).fetchall()
        return [dict(r) for r in result]
    finally:
        db.close()


def get_weekly_trend(weeks: int = 12) -> list:
    """Returns play count per week for the last N weeks."""
    db = get_db()
    try:
        from datetime import datetime, timedelta
        cutoff_date = datetime.now() - timedelta(weeks=weeks)
        cutoff_str = cutoff_date.isoformat()

        result = db.execute("""
            SELECT
                strftime('%Y-%W', played_at) as week_key,
                DATE(played_at, '-' || (CAST(strftime('%w', played_at) AS INTEGER)) || ' days') as week_start,
                COUNT(*) as count
            FROM plays
            WHERE played_at >= ?
            GROUP BY week_key
            ORDER BY week_start
        """, (cutoff_str,)).fetchall()
        return [dict(r) for r in result]
    finally:
        db.close()


def update_album_metadata(album_id: int, data: dict) -> bool:
    """Update album metadata fields. data keys: title, artist, year, genre, label."""
    db = get_db()
    try:
        album = db.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if not album:
            return False

        update_fields = []
        update_values = []
        allowed_fields = ["title", "artist", "year", "genre", "label"]

        for field in allowed_fields:
            if field in data and data[field] is not None:
                update_fields.append(f"{field} = ?")
                update_values.append(data[field])

        if not update_fields:
            return True

        update_values.append(album_id)
        query = f"UPDATE albums SET {', '.join(update_fields)}, updated_at = datetime('now') WHERE id = ?"
        db.execute(query, update_values)
        db.commit()
        return True
    finally:
        db.close()


def find_duplicate_albums(similarity_threshold: float = 0.80) -> list:
    """Find groups of albums with similar title+artist (80%+ match)."""
    import difflib

    db = get_db()
    try:
        all_albums = db.execute("""
            SELECT id, title, artist FROM albums WHERE title IS NOT NULL AND artist IS NOT NULL AND deleted_at IS NULL
        """).fetchall()

        def normalize(s: str) -> str:
            """Normalize string for comparison."""
            s = s.lower().strip()
            s = re.sub(r'\b(the|a|an)\b\s+', '', s)
            s = re.sub(r'[^\w\s]', '', s)
            s = re.sub(r'\s+', ' ', s)
            return s

        groups = []
        matched_ids = set()

        for i, album1 in enumerate(all_albums):
            if album1["id"] in matched_ids:
                continue

            norm1_title = normalize(album1["title"])
            norm1_artist = normalize(album1["artist"])
            group = [dict(album1)]

            for j in range(i + 1, len(all_albums)):
                album2 = all_albums[j]
                if album2["id"] in matched_ids:
                    continue

                norm2_title = normalize(album2["title"])
                norm2_artist = normalize(album2["artist"])

                title_sim = difflib.SequenceMatcher(None, norm1_title, norm2_title).ratio()
                artist_sim = difflib.SequenceMatcher(None, norm1_artist, norm2_artist).ratio()

                if title_sim >= similarity_threshold and artist_sim >= similarity_threshold:
                    group.append(dict(album2))
                    matched_ids.add(album2["id"])

            if len(group) > 1:
                groups.append(group)
                matched_ids.add(album1["id"])

        return groups
    finally:
        db.close()


# ── Feature 6: Soft Delete Support ──────────────────────────────────────────


def _add_deleted_at_column():
    """Add deleted_at column to albums table if it doesn't exist."""
    db = get_db()
    try:
        db.execute("""
            ALTER TABLE albums ADD COLUMN deleted_at TEXT DEFAULT NULL
        """)
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def soft_delete_album(album_id: int) -> bool:
    """Soft-delete an album (set deleted_at timestamp)."""
    _add_deleted_at_column()
    db = get_db()
    try:
        db.execute("""
            UPDATE albums SET deleted_at = datetime('now') WHERE id = ?
        """, (album_id,))
        db.commit()
        return True
    finally:
        db.close()


def restore_album(album_id: int) -> bool:
    """Restore a soft-deleted album."""
    db = get_db()
    try:
        db.execute("""
            UPDATE albums SET deleted_at = NULL WHERE id = ?
        """, (album_id,))
        db.commit()
        return True
    finally:
        db.close()
