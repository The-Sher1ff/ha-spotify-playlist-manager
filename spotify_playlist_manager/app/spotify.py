"""Thin Spotify layer.

Uses the post-February-2026 endpoints (/playlists/{id}/items) directly, because
the older /tracks endpoints return 403 for Development Mode apps.
"""
import re

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth

from .config import PRIVATE_DIR

SCOPE = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
    "user-library-read",
])

LIKED_SONGS = "liked"  # pseudo playlist id for the Liked Songs library


def auth_manager(opts: dict) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=opts["client_id"],
        client_secret=opts["client_secret"],
        redirect_uri=opts["redirect_uri"],
        scope=SCOPE,
        open_browser=False,
        cache_handler=CacheFileHandler(cache_path=str(PRIVATE_DIR / ".spotify_token")),
    )


def is_authorized(opts: dict) -> bool:
    if not opts["client_id"] or not opts["client_secret"]:
        return False
    am = auth_manager(opts)
    return am.validate_token(am.cache_handler.get_cached_token()) is not None


def client(opts: dict) -> "SpotifyClient":
    return SpotifyClient(spotipy.Spotify(auth_manager=auth_manager(opts), retries=5))


def parse_playlist_id(value: str) -> str:
    """Accepts a share URL, a spotify:playlist: URI or a bare ID."""
    value = value.strip()
    if value.lower() in ("liked", "liked songs"):
        return LIKED_SONGS
    m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{22}", value):
        return value
    raise ValueError(f"Not a Spotify playlist link or ID: {value!r}")


def normalize_item(raw: dict, position: int) -> dict | None:
    """Flatten one playlist/library entry into the row we store."""
    track = raw.get("item") or raw.get("track")  # 'track' was renamed to 'item' in 2026
    if not track:
        return None
    album = track.get("album") or {}
    artists = track.get("artists") or []
    return {
        "position": position,
        "track_id": track.get("id"),
        "uri": track.get("uri"),
        "name": track.get("name") or "",
        "artists": ", ".join(a.get("name", "") for a in artists),
        "album": album.get("name", ""),
        "release_date": album.get("release_date", ""),
        "duration_ms": track.get("duration_ms"),
        "isrc": (track.get("external_ids") or {}).get("isrc", ""),
        "type": track.get("type", "track"),
        "is_local": bool(raw.get("is_local")),
        "added_at": raw.get("added_at") or "",
        "added_by": (raw.get("added_by") or {}).get("id", ""),
        "url": (track.get("external_urls") or {}).get("spotify", ""),
    }


class SpotifyClient:
    def __init__(self, sp: spotipy.Spotify):
        self.sp = sp

    def me(self) -> dict:
        return self.sp.me()

    def _paged(self, first: dict) -> list:
        items = list(first.get("items", []))
        page = first
        while page.get("next"):
            page = self.sp._get(page["next"])
            items.extend(page.get("items", []))
        return items

    def my_playlists(self) -> list[dict]:
        """Playlists the user owns or collaborates on (the only ones the API lets us read)."""
        me = self.me()["id"]
        out = []
        for p in self._paged(self.sp._get("me/playlists", limit=50)):
            if p and (p.get("owner", {}).get("id") == me or p.get("collaborative")):
                out.append(p)
        return out

    def playlist_meta(self, playlist_id: str) -> dict:
        if playlist_id == LIKED_SONGS:
            return {"id": LIKED_SONGS, "name": "Liked Songs", "snapshot_id": "", "owner": {"id": "me"},
                    "description": ""}
        return self.sp._get(f"playlists/{playlist_id}",
                            fields="id,name,snapshot_id,owner(id,display_name),description")

    def playlist_items(self, playlist_id: str) -> list[dict]:
        if playlist_id == LIKED_SONGS:
            raw = self._paged(self.sp._get("me/tracks", limit=50))
        else:
            raw = self._paged(self.sp._get(f"playlists/{playlist_id}/items", limit=100,
                                           additional_types="track,episode"))
        rows = []
        for pos, r in enumerate(raw):
            row = normalize_item(r, pos)
            if row:
                rows.append(row)
        return rows

    def reorder(self, playlist_id: str, range_start: int, insert_before: int,
                range_length: int, snapshot_id: str | None) -> str:
        body = {"range_start": range_start, "insert_before": insert_before, "range_length": range_length}
        if snapshot_id:
            body["snapshot_id"] = snapshot_id
        return self.sp._put(f"playlists/{playlist_id}/items", payload=body)["snapshot_id"]
