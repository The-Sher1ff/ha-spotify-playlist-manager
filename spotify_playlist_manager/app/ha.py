"""Talks to Home Assistant Core through the Supervisor proxy (needs homeassistant_api: true)."""
import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

STATUS_ENTITY = "sensor.spotify_backup_status"


def call(path: str, payload: dict) -> bool:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:  # running outside Home Assistant
        return False
    req = urllib.request.Request(
        f"http://supervisor/core/api/{path}", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:  # noqa: BLE001 - reporting must never break a run
        log.warning("Home Assistant API call %s failed: %s", path, e)
        return False


def notify(title: str, message: str):
    call("services/persistent_notification/create",
         {"title": title, "message": message, "notification_id": "spotify_playlist_manager"})


def status_payload(db, next_run, running: bool) -> dict:
    runs = db.recent_runs(1)
    last = runs[0] if runs else None
    summary = json.loads(last["summary"]) if last and last["summary"] else {}
    if running:
        state = "running"
    elif last:
        state = last["status"]
    else:
        state = "never_run"
    playlists = db.playlists()
    return {
        "state": state,
        "attributes": {
            "friendly_name": "Spotify backup status",
            "icon": "mdi:spotify" if state in ("ok", "running", "never_run") else "mdi:alert-circle",
            "last_run": last["finished_at"] if last else None,
            "last_success": db.last_success(),
            "next_run": next_run.isoformat() if next_run else None,
            "errors": summary.get("errors", []),
            "playlists_backed_up": len(playlists),
            "total_tracks": sum(p["track_count"] or 0 for p in playlists),
            "playlists_changed_last_run": [b["name"] for b in summary.get("backed_up", []) if b.get("changed")],
        },
    }


def publish_status(db, next_run, running: bool = False):
    call(f"states/{STATUS_ENTITY}", status_payload(db, next_run, running))
