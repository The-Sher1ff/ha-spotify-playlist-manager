"""Loads add-on options. Home Assistant writes them to /data/options.json."""
import json
import os
from pathlib import Path

OPTIONS_PATH = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
# Private add-on storage (token cache). Not visible over Samba.
PRIVATE_DIR = Path(os.environ.get("PRIVATE_DIR", "/data"))

DEFAULTS = {
    "client_id": "",
    "client_secret": "",
    "redirect_uri": "http://127.0.0.1:8888/callback",
    "schedule": "0 3 * * *",
    "run_on_start": False,
    "backup_all_my_playlists": False,
    "backup_liked_songs": False,
    "backup_playlists": [],
    "sort_playlists": [],
    "dry_run": False,
    "notify_on_failure": True,
    "data_dir": "/share/spotify_backup",
}


def load_options() -> dict:
    opts = dict(DEFAULTS)
    if OPTIONS_PATH.exists():
        opts.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
    return opts


def data_dir(opts: dict) -> Path:
    d = Path(opts["data_dir"])
    for sub in ("", "playlists", "orders", "exports"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d
