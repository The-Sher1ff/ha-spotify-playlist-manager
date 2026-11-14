"""Back up a playlist to SQLite and to a plain CSV file."""
import csv
import io
import logging
import re
from pathlib import Path

from .db import DB

log = logging.getLogger(__name__)

# First six columns match the original export format, so old CSVs and new ones work
# interchangeably as sort order files. ISRC is the column other services match on.
CSV_HEADER = ["Position", "Track", "Album", "Artist", "Date Added", "Track ID",
              "ISRC", "Spotify URI", "Duration (ms)", "Release Date", "Spotify URL"]


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "playlist"


def rows_to_csv(rows, fh):
    w = csv.writer(fh)
    w.writerow(CSV_HEADER)
    for r in rows:
        r = dict(r)
        w.writerow([r["position"], r["name"], r["album"], r["artists"], r["added_at"], r["track_id"] or "",
                    r["isrc"] or "", r["uri"] or "", r["duration_ms"] or "", r["release_date"] or "",
                    r["url"] or ""])


def csv_bytes(rows) -> bytes:
    buf = io.StringIO()
    rows_to_csv(rows, buf)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens accented names correctly


def backup_playlist(sp, db: DB, data_dir: Path, playlist_id: str, now: str) -> dict:
    meta = sp.playlist_meta(playlist_id)
    rows = sp.playlist_items(playlist_id)

    latest = db.latest_backup_id(playlist_id)
    previous = [(r["track_id"] or r["uri"], r["added_at"]) for r in db.backup_rows(latest)] if latest else None
    current = [(r["track_id"] or r["uri"], r["added_at"]) for r in rows]

    csv_path = data_dir / "playlists" / f"{safe_name(meta['name'])}.csv"
    changed = previous != current
    if changed:
        db.save_backup(meta, rows, now)
    else:
        db.touch_playlist(playlist_id, now)
    if changed or not csv_path.exists():
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            rows_to_csv(rows, fh)

    log.info("Backed up %s (%d tracks, %s)", meta["name"], len(rows), "changed" if changed else "unchanged")
    return {"name": meta["name"], "tracks": len(rows), "changed": changed}
