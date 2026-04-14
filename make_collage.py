#!/usr/bin/env python3
"""Generate a seamless grid collage of all album artwork."""
import sqlite3, os, math, random
from PIL import Image

DB = "catalog.db"
THUMB = 300  # px per cover

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

rows = db.execute("""
    SELECT id, artist, title,
           COALESCE(NULLIF(user_artwork_path,''), artwork_path) as art
    FROM albums
    WHERE deleted_at IS NULL
    ORDER BY CASE WHEN artist LIKE 'The %%' THEN SUBSTR(artist, 5) ELSE artist END, title
""").fetchall()
db.close()

images = [r["art"] for r in rows if r["art"] and os.path.exists(r["art"])]
n = len(images)
print(f"Found {n} albums with artwork")

cols = math.ceil(math.sqrt(n))
rows_count = math.ceil(n / cols)
print(f"Grid: {cols}x{rows_count} = {cols * rows_count} cells")

collage = Image.new("RGB", (cols * THUMB, rows_count * THUMB), (20, 20, 20))

for i, path in enumerate(images):
    r, c = divmod(i, cols)
    try:
        img = Image.open(path).convert("RGB").resize((THUMB, THUMB), Image.LANCZOS)
        collage.paste(img, (c * THUMB, r * THUMB))
    except Exception as e:
        print(f"  Skip {path}: {e}")

# Fill empty trailing cells with random picks for a seamless look
empty = cols * rows_count - n
if empty > 0:
    fill = random.sample(images, min(empty, len(images)))
    for j in range(empty):
        idx = n + j
        r, c = divmod(idx, cols)
        try:
            img = Image.open(fill[j % len(fill)]).convert("RGB").resize((THUMB, THUMB), Image.LANCZOS)
            collage.paste(img, (c * THUMB, r * THUMB))
        except:
            pass

out = "/tmp/vinyl_collection_collage.jpg"
collage.save(out, "JPEG", quality=90)
sz = os.path.getsize(out) / 1024 / 1024
print(f"Saved: {out} ({cols * THUMB}x{rows_count * THUMB}px, {sz:.1f} MB)")
