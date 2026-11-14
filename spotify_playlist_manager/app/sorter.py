"""Re-order a playlist in place, either from an order CSV or by a field."""
import csv
import logging
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger(__name__)

SORT_KEYS = {
    "name": lambda r: r["name"].casefold(),
    "artist": lambda r: (r["artists"].casefold(), r["album"].casefold()),
    "album": lambda r: r["album"].casefold(),
    "added_at": lambda r: r["added_at"],
    "release_date": lambda r: r["release_date"],
    "duration": lambda r: r["duration_ms"] or 0,
}


def key_of(row: dict) -> str:
    return row["track_id"] or row["uri"]


def read_order_file(path: Path) -> list[str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        out = []
        for r in reader:
            tid = (r.get("Track ID") or "").strip()
            if not tid and r.get("Spotify URI", "").startswith("spotify:track:"):
                tid = r["Spotify URI"].split(":")[-1]
            if tid:
                out.append(tid)
        if not out:
            raise ValueError(f"{path} has no 'Track ID' values")
        return out


def desired_order(rows: list[dict], rule: dict, orders_dir: Path) -> list[str]:
    """Return the wanted list of track keys (same multiset as the current playlist)."""
    if rule.get("order_file"):
        path = Path(rule["order_file"])
        if not path.is_absolute():
            path = orders_dir / path
        wanted = read_order_file(path)
        # Each order entry claims one occurrence; anything not in the file keeps its
        # relative order at the end, so tracks you add later are never lost.
        remaining = defaultdict(deque)
        for idx, r in enumerate(rows):
            remaining[key_of(r)].append(idx)
        used, result = set(), []
        for tid in wanted:
            if remaining[tid]:
                idx = remaining[tid].popleft()
                used.add(idx)
                result.append(key_of(rows[idx]))
        result += [key_of(r) for i, r in enumerate(rows) if i not in used]
        if rule.get("reverse"):
            result.reverse()
        return result

    field = rule.get("sort_by") or "added_at"
    ordered = sorted(rows, key=SORT_KEYS[field], reverse=bool(rule.get("reverse")))
    return [key_of(r) for r in ordered]


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


def sort_playlist(sp, playlist_id: str, rule: dict, orders_dir: Path, dry_run: bool) -> dict:
    meta = sp.playlist_meta(playlist_id)
    rows = sp.playlist_items(playlist_id)
    current = [key_of(r) for r in rows]
    wanted = desired_order(rows, rule, orders_dir)
    moves = plan_moves(current, wanted)

    if dry_run:
        log.info("[dry run] %s: would make %d moves", meta["name"], len(moves))
    else:
        snapshot = meta.get("snapshot_id")
        for start, before, length in moves:
            snapshot = sp.reorder(playlist_id, start, before, length, snapshot)
        log.info("Sorted %s with %d moves", meta["name"], len(moves))
    return {"name": meta["name"], "moves": len(moves), "dry_run": dry_run}
