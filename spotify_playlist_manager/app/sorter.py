"""Work out the wanted order of a playlist, then re-order it in place.

Ordering inputs, in priority order:
  1. Pins: an override can pin a song to a fixed position from the top.
  2. Override dates: used instead of the date added when sorting by date.
  3. Original dates: if a song was in the playlist before (by track id, ISRC or
     title+artist) its earliest date added is used, so re-added songs keep their place.
"""
import csv
import logging
import re
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger(__name__)

SORT_FIELDS = {
    "added_at": lambda r: r["effective_date"],
    "name": lambda r: r["name"].casefold(),
    "artist": lambda r: (r["artists"].casefold(), r["album"].casefold()),
    "album": lambda r: r["album"].casefold(),
    "release_date": lambda r: r["release_date"],
    "duration": lambda r: r["duration_ms"] or 0,
}


def key_of(row: dict) -> str:
    return row["track_id"] or row["uri"]


def song_key(name: str, artists: str) -> str:
    """Normalised 'title|first artist', so 'Song - Remastered 2011 (feat. X)' matches 'Song'."""
    title = (name or "").casefold()
    title = re.sub(r"\s[-–]\s.*$", "", title)
    title = re.sub(r"[(\[].*?[)\]]", "", title)
    title = re.sub(r"[\W_]+", "", title)
    artist = re.sub(r"[\W_]+", "", (artists or "").split(",")[0].casefold())
    return f"{title}|{artist}" if title else ""


def identities(row: dict) -> list[tuple[str, str]]:
    ids = [("id", row.get("track_id") or ""), ("isrc", (row.get("isrc") or "").upper()),
           ("song", row.get("song_key") or song_key(row.get("name"), row.get("artists")))]
    return [i for i in ids if i[1]]


def normalize_date(value: str | None) -> str | None:
    """'2024-05-01' -> '2024-05-01T00:00:00Z' so it compares with Spotify timestamps."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value + "T00:00:00Z"
    return value


def annotate(rows: list[dict], history: list[dict], overrides: list[dict], keep_original: bool) -> list[dict]:
    earliest: dict[tuple, str] = {}
    if keep_original:
        for h in history:
            for ident in identities(h):
                if h["added_at"] and (ident not in earliest or h["added_at"] < earliest[ident]):
                    earliest[ident] = h["added_at"]
    by_ident = {}
    for o in overrides:
        for ident in identities(o):
            by_ident.setdefault(ident, o)

    out = []
    for r in rows:
        r = dict(r)
        r["song_key"] = song_key(r["name"], r["artists"])
        ids = identities(r)
        original = r["added_at"]
        for ident in ids:
            d = earliest.get(ident)
            if d and (not original or d < original):
                original = d
        ov = next((by_ident[i] for i in ids if i in by_ident), None)
        r["original_added"] = original
        r["override_id"] = ov["override_id"] if ov and "override_id" in ov else None
        r["override_date"] = (ov or {}).get("override_date")
        r["pin"] = (ov or {}).get("pin")
        r["effective_date"] = normalize_date(r["override_date"]) or original
        r["date_source"] = ("override" if r["override_date"] else
                            "history" if original != r["added_at"] else "spotify")
        out.append(r)
    return out


def read_order_file(path: Path) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        out = []
        for r in csv.DictReader(fh):
            tid = (r.get("Track ID") or "").strip()
            if not tid and (r.get("Spotify URI") or "").startswith("spotify:track:"):
                tid = r["Spotify URI"].split(":")[-1]
            if tid:
                out.append(tid)
    if not out:
        raise ValueError(f"{path.name} has no 'Track ID' values")
    return out


def build_order(rows: list[dict], rule: dict, orders_dir: Path, history=(), overrides=()) -> list[dict]:
    """Return annotated rows in their wanted order, each with 'new_position'."""
    rows = annotate(rows, list(history), list(overrides), rule.get("keep_original_dates", True))

    if rule.get("order_file"):
        path = Path(rule["order_file"])
        if not path.is_absolute():
            path = orders_dir / path
        # Each order entry claims one occurrence; songs not in the file keep their
        # relative order at the end, so tracks added later are never lost.
        remaining = defaultdict(deque)
        for idx, r in enumerate(rows):
            remaining[key_of(r)].append(idx)
        used, ordered = set(), []
        for tid in read_order_file(path):
            if remaining[tid]:
                idx = remaining[tid].popleft()
                used.add(idx)
                ordered.append(rows[idx])
        ordered += [r for i, r in enumerate(rows) if i not in used]
        if rule.get("reverse"):
            ordered.reverse()
    else:
        field = rule.get("sort_by") or "added_at"
        ordered = sorted(rows, key=SORT_FIELDS[field], reverse=bool(rule.get("reverse")))

    pinned = sorted((r for r in ordered if r["pin"]), key=lambda r: (r["pin"], r["position"]))
    ordered = [r for r in ordered if not r["pin"]]
    for r in pinned:
        ordered.insert(min(max(r["pin"], 1) - 1, len(ordered)), r)
    for i, r in enumerate(ordered):
        r["new_position"] = i
    return ordered


def plan_moves(current: list[str], wanted: list[str]) -> list[tuple[int, int, int]]:
    """Moves as (range_start, insert_before, range_length), Spotify reorder semantics.

    Walks the target left to right; when a slot is wrong, pulls the wanted track
    (plus any run of following tracks already in the right relative order) into place.
    """
    if sorted(current) != sorted(wanted):
        raise ValueError("wanted order does not contain the same tracks as the playlist")
    cur = list(current)
    n = len(cur)
    moves = []
    i = 0
    while i < n:
        if cur[i] == wanted[i]:
            i += 1
            continue
        j = cur.index(wanted[i], i + 1)
        k = 1
        while j + k < n and i + k < n and cur[j + k] == wanted[i + k]:
            k += 1
        moves.append((j, i, k))
        cur = cur[:i] + cur[j:j + k] + cur[i:j] + cur[j + k:]
        i += k
    assert cur == wanted
    return moves


def sort_playlist(sp, playlist_id: str, rule: dict, orders_dir: Path, dry_run: bool,
                  history=(), overrides=()) -> dict:
    meta = sp.playlist_meta(playlist_id)
    rows = sp.playlist_items(playlist_id)
    wanted = [key_of(r) for r in build_order(rows, rule, orders_dir, history, overrides)]
    moves = plan_moves([key_of(r) for r in rows], wanted)

    if dry_run:
        log.info("[dry run] %s: would make %d moves", meta["name"], len(moves))
    else:
        snapshot = meta.get("snapshot_id")
        for start, before, length in moves:
            snapshot = sp.reorder(playlist_id, start, before, length, snapshot)
        log.info("Sorted %s with %d moves", meta["name"], len(moves))
    return {"name": meta["name"], "moves": len(moves), "dry_run": dry_run}
