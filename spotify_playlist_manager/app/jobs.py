"""One scheduled run: back up everything configured, then apply sorts."""
import json
import logging
import threading
from datetime import datetime, timezone

from . import spotify
from .ha import notify
from .backup import backup_playlist
from .config import data_dir, load_options
from .db import DB
from .sorter import sort_playlist

log = logging.getLogger(__name__)
_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_db(opts: dict) -> DB:
    return DB(data_dir(opts) / "library.db")


def is_running() -> bool:
    return _lock.locked()


def sort_rules(opts: dict, db: DB) -> dict[str, dict]:
    """Playlist id -> sort rule. Settings saved in the web UI win over the add-on config."""
    rules = {}
    for r in opts["sort_playlists"]:
        rules[spotify.parse_playlist_id(r["playlist"])] = {**r, "source": "config"}
    for s in db.all_sort_settings():
        if s["enabled"]:
            rules[s["playlist_id"]] = rule_from_settings(s)
        else:  # turned off in the UI, even if the config lists it
            rules.pop(s["playlist_id"], None)
    return rules


def rule_from_settings(s: dict) -> dict:
    rule = {"reverse": bool(s["reverse"]), "keep_original_dates": bool(s["keep_original_dates"]),
            "source": "web"}
    if s["sort_by"] == "custom":
        rule["order_file"] = f"{s['playlist_id']}.csv"
    else:
        rule["sort_by"] = s["sort_by"]
    return rule


def run_job(trigger: str, sp_factory=spotify.client, only: str | None = None) -> dict | None:
    """Run everything, or with `only` just back up and sort that one playlist."""
    if not _lock.acquire(blocking=False):
        log.info("Run already in progress; ignoring %s trigger", trigger)
        return None
    try:
        return _run(trigger, sp_factory, only)
    finally:
        _lock.release()


def _run(trigger: str, sp_factory, only: str | None) -> dict:
    opts = load_options()
    ddir = data_dir(opts)
    db = open_db(opts)
    run_id = db.start_run(trigger, now_iso())
    errors, backed_up, sorted_ = [], [], []
    log.info("Run started (%s%s)", trigger, f", playlist {only}" if only else "")

    try:
        if sp_factory is spotify.client and not spotify.is_authorized(opts):
            raise RuntimeError("Spotify is not connected. Open the add-on web UI and click 'Connect Spotify'.")
        sp = sp_factory(opts)
        rules = sort_rules(opts, db)

        targets: dict[str, None] = {}  # ordered set
        if only:
            targets[only] = None
            rules = {only: rules[only]} if only in rules else {}
        else:
            for p in opts["backup_playlists"]:
                targets[spotify.parse_playlist_id(p)] = None
            if opts["backup_all_my_playlists"]:
                for p in sp.my_playlists():
                    targets[p["id"]] = None
            if opts["backup_liked_songs"]:
                targets[spotify.LIKED_SONGS] = None
        for pid in rules:  # always back up before re-ordering
            targets[pid] = None

        for pid in targets:
            try:
                backed_up.append(backup_playlist(sp, db, ddir, pid, now_iso()))
            except Exception as e:  # noqa: BLE001 - keep going with the other playlists
                log.exception("Backup failed for %s", pid)
                errors.append(f"backup {pid}: {e}")

        for pid, rule in rules.items():
            if pid == spotify.LIKED_SONGS:
                errors.append("Liked Songs can't be re-ordered through the Spotify API")
                continue
            if any(e.startswith(f"backup {pid}:") for e in errors):
                errors.append(f"sort {pid}: skipped because its backup failed")
                continue
            try:
                result = sort_playlist(sp, pid, rule, ddir / "orders", opts["dry_run"],
                                       db.history(pid), db.overrides(pid))
                sorted_.append(result)
                if result["moves"] and not result["dry_run"]:
                    backup_playlist(sp, db, ddir, pid, now_iso())  # record the new order
            except Exception as e:  # noqa: BLE001
                log.exception("Sort failed for %s", pid)
                errors.append(f"sort {pid}: {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("Run failed")
        errors.append(str(e))

    summary = {"backed_up": backed_up, "sorted": sorted_, "errors": errors}
    status = "ok" if not errors else ("partial" if backed_up or sorted_ else "failed")
    db.finish_run(run_id, now_iso(), status, json.dumps(summary))
    log.info("Run finished: %s (%d backed up, %d sorted, %d errors)", status, len(backed_up), len(sorted_),
             len(errors))
    if errors and opts["notify_on_failure"]:
        notify("Spotify Playlist Manager: run " + status, "\n".join(f"- {e}" for e in errors))
    return {"status": status, **summary}
