"""SQLite store: one row per backup snapshot, so you keep the history of every playlist."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    playlist_id  TEXT PRIMARY KEY,
    name         TEXT,
    owner        TEXT,
    description  TEXT,
    last_backup  TEXT
);
CREATE TABLE IF NOT EXISTS tracks (
    track_key    TEXT PRIMARY KEY,   -- Spotify track id, or the URI for local files
    track_id     TEXT,
    uri          TEXT,
    name         TEXT,
    artists      TEXT,
    album        TEXT,
    release_date TEXT,
    duration_ms  INTEGER,
    isrc         TEXT,
    type         TEXT,
    url          TEXT
);
CREATE TABLE IF NOT EXISTS backups (
    backup_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id  TEXT NOT NULL,
    snapshot_id  TEXT,
    captured_at  TEXT NOT NULL,
    track_count  INTEGER
);
CREATE TABLE IF NOT EXISTS backup_items (
    backup_id    INTEGER NOT NULL REFERENCES backups(backup_id),
    position     INTEGER NOT NULL,
    track_key    TEXT NOT NULL REFERENCES tracks(track_key),
    added_at     TEXT,
    added_by     TEXT,
    PRIMARY KEY (backup_id, position)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger      TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    status       TEXT,
    summary      TEXT
);
CREATE VIEW IF NOT EXISTS latest_playlist_tracks AS
SELECT p.name AS playlist, bi.position, t.name AS track, t.artists, t.album,
       bi.added_at, t.isrc, t.track_id, t.uri, b.captured_at
FROM backups b
JOIN (SELECT playlist_id, MAX(backup_id) AS backup_id FROM backups GROUP BY playlist_id) lb
  ON lb.backup_id = b.backup_id
JOIN playlists p ON p.playlist_id = b.playlist_id
JOIN backup_items bi ON bi.backup_id = b.backup_id
JOIN tracks t ON t.track_key = bi.track_key;

-- Sort settings made in the web UI (take precedence over sort_playlists in the add-on config).
CREATE TABLE IF NOT EXISTS sort_settings (
    playlist_id          TEXT PRIMARY KEY,
    enabled              INTEGER NOT NULL DEFAULT 0,
    sort_by              TEXT NOT NULL DEFAULT 'added_at',   -- or 'custom' = uploaded order CSV
    reverse              INTEGER NOT NULL DEFAULT 1,
    keep_original_dates  INTEGER NOT NULL DEFAULT 1
);
-- Per-song overrides. Matched by track id, ISRC or normalised title+artist,
-- so they survive a song being re-added under a different track id.
CREATE TABLE IF NOT EXISTS overrides (
    override_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id    TEXT NOT NULL,
    track_id       TEXT,
    isrc           TEXT,
    song_key       TEXT,
    name           TEXT,
    artists        TEXT,
    override_date  TEXT,     -- ISO date; used instead of the date added
    pin            INTEGER,  -- 1-based position from the top
    updated_at     TEXT
);
-- Old exports uploaded through the UI, used only to find original "date added" values.
CREATE TABLE IF NOT EXISTS imported_history (
    playlist_id  TEXT NOT NULL,
    track_id     TEXT,
    isrc         TEXT,
    name         TEXT,
    artists      TEXT,
    added_at     TEXT,
    source       TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # --- backups -----------------------------------------------------------
    def last_snapshot(self, playlist_id: str) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT snapshot_id FROM backups WHERE playlist_id=? ORDER BY backup_id DESC LIMIT 1",
                          (playlist_id,)).fetchone()
            return r["snapshot_id"] if r else None

    def save_backup(self, meta: dict, rows: list[dict], captured_at: str) -> int:
        with self.conn() as c:
            c.execute(
                "INSERT INTO playlists(playlist_id,name,owner,description,last_backup) VALUES(?,?,?,?,?) "
                "ON CONFLICT(playlist_id) DO UPDATE SET name=excluded.name, owner=excluded.owner, "
                "description=excluded.description, last_backup=excluded.last_backup",
                (meta["id"], meta.get("name"), (meta.get("owner") or {}).get("id"), meta.get("description"),
                 captured_at))
            cur = c.execute("INSERT INTO backups(playlist_id,snapshot_id,captured_at,track_count) VALUES(?,?,?,?)",
                            (meta["id"], meta.get("snapshot_id"), captured_at, len(rows)))
            backup_id = cur.lastrowid
            for r in rows:
                key = r["track_id"] or r["uri"]
                c.execute(
                    "INSERT INTO tracks(track_key,track_id,uri,name,artists,album,release_date,duration_ms,isrc,type,url) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(track_key) DO UPDATE SET "
                    "name=excluded.name, artists=excluded.artists, album=excluded.album, "
                    "release_date=excluded.release_date, duration_ms=excluded.duration_ms, "
                    "isrc=COALESCE(NULLIF(excluded.isrc,''), tracks.isrc), url=excluded.url",
                    (key, r["track_id"], r["uri"], r["name"], r["artists"], r["album"], r["release_date"],
                     r["duration_ms"], r["isrc"], r["type"], r["url"]))
                c.execute("INSERT INTO backup_items(backup_id,position,track_key,added_at,added_by) VALUES(?,?,?,?,?)",
                          (backup_id, r["position"], key, r["added_at"], r["added_by"]))
            return backup_id

    def touch_playlist(self, playlist_id: str, when: str):
        with self.conn() as c:
            c.execute("UPDATE playlists SET last_backup=? WHERE playlist_id=?", (when, playlist_id))

    def playlists(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM backups b WHERE b.playlist_id=p.playlist_id) AS versions, "
                "(SELECT track_count FROM backups b WHERE b.playlist_id=p.playlist_id ORDER BY backup_id DESC LIMIT 1) "
                "AS track_count FROM playlists p ORDER BY p.name COLLATE NOCASE").fetchall()

    def backups_for(self, playlist_id: str) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM backups WHERE playlist_id=? ORDER BY backup_id DESC",
                             (playlist_id,)).fetchall()

    def latest_backup_id(self, playlist_id: str) -> int | None:
        rows = self.backups_for(playlist_id)
        return rows[0]["backup_id"] if rows else None

    def backup_rows(self, backup_id: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute(
                "SELECT bi.position, t.name, t.album, t.artists, bi.added_at, t.track_id, t.isrc, t.uri, "
                "t.duration_ms, t.release_date, t.url FROM backup_items bi JOIN tracks t USING(track_key) "
                "WHERE bi.backup_id=? ORDER BY bi.position", (backup_id,)).fetchall()

    def backup(self, backup_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT b.*, p.name FROM backups b JOIN playlists p USING(playlist_id) "
                             "WHERE backup_id=?", (backup_id,)).fetchone()

    def playlist(self, playlist_id: str) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM playlists WHERE playlist_id=?", (playlist_id,)).fetchone()

    # --- sorting -----------------------------------------------------------
    def sort_settings(self, playlist_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM sort_settings WHERE playlist_id=?", (playlist_id,)).fetchone()
            return dict(r) if r else None

    def all_sort_settings(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sort_settings")]

    def save_sort_settings(self, playlist_id: str, enabled: bool, sort_by: str, reverse: bool,
                           keep_original_dates: bool):
        with self.conn() as c:
            c.execute(
                "INSERT INTO sort_settings(playlist_id,enabled,sort_by,reverse,keep_original_dates) "
                "VALUES(?,?,?,?,?) ON CONFLICT(playlist_id) DO UPDATE SET enabled=excluded.enabled, "
                "sort_by=excluded.sort_by, reverse=excluded.reverse, "
                "keep_original_dates=excluded.keep_original_dates",
                (playlist_id, int(enabled), sort_by, int(reverse), int(keep_original_dates)))

    def overrides(self, playlist_id: str) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM overrides WHERE playlist_id=? ORDER BY override_id", (playlist_id,))]

    def save_override(self, playlist_id: str, override_id: int | None, ident: dict,
                      override_date: str | None, pin: int | None, now: str):
        with self.conn() as c:
            if override_id:
                if not override_date and not pin:
                    c.execute("DELETE FROM overrides WHERE override_id=? AND playlist_id=?",
                              (override_id, playlist_id))
                else:
                    c.execute("UPDATE overrides SET override_date=?, pin=?, updated_at=?, track_id=?, isrc=?, "
                              "song_key=?, name=?, artists=? WHERE override_id=? AND playlist_id=?",
                              (override_date, pin, now, ident["track_id"], ident["isrc"], ident["song_key"],
                               ident["name"], ident["artists"], override_id, playlist_id))
            elif override_date or pin:
                c.execute("INSERT INTO overrides(playlist_id,track_id,isrc,song_key,name,artists,override_date,"
                          "pin,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                          (playlist_id, ident["track_id"], ident["isrc"], ident["song_key"], ident["name"],
                           ident["artists"], override_date, pin, now))

    def history(self, playlist_id: str) -> list[dict]:
        """Every (track, date added) this playlist has ever had, from backups and imports."""
        with self.conn() as c:
            rows = c.execute(
                "SELECT t.track_id, t.isrc, t.name, t.artists, MIN(bi.added_at) AS added_at "
                "FROM backups b JOIN backup_items bi USING(backup_id) JOIN tracks t USING(track_key) "
                "WHERE b.playlist_id=? AND bi.added_at != '' GROUP BY t.track_key "
                "UNION ALL SELECT track_id, isrc, name, artists, added_at FROM imported_history "
                "WHERE playlist_id=? AND added_at != ''", (playlist_id, playlist_id)).fetchall()
            return [dict(r) for r in rows]

    def import_history(self, playlist_id: str, rows: list[dict], source: str) -> int:
        with self.conn() as c:
            c.executemany(
                "INSERT INTO imported_history(playlist_id,track_id,isrc,name,artists,added_at,source) "
                "VALUES(?,?,?,?,?,?,?)",
                [(playlist_id, r.get("track_id"), r.get("isrc"), r.get("name"), r.get("artists"),
                  r.get("added_at"), source) for r in rows])
            return len(rows)

    # --- runs --------------------------------------------------------------
    def start_run(self, trigger: str, started_at: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs(trigger,started_at,status) VALUES(?,?,'running')",
                             (trigger, started_at)).lastrowid

    def finish_run(self, run_id: int, finished_at: str, status: str, summary: str):
        with self.conn() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE run_id=?",
                      (finished_at, status, summary, run_id))

    def last_success(self) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT finished_at FROM runs WHERE status='ok' ORDER BY run_id DESC LIMIT 1").fetchone()
            return r["finished_at"] if r else None

    def recent_runs(self, limit: int = 15) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
