"""Offline tests: a fake Spotify client that applies reorders like the real API."""
import csv
import json
import random
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests" / "fixtures" / "order.csv"


def make_item(tid, name, artist, added, album="Album", release="2020-01-01", isrc=None):
    return {"added_at": added, "added_by": {"id": "me"}, "is_local": False,
            "item": {"id": tid, "uri": f"spotify:track:{tid}", "name": name, "type": "track",
                     "artists": [{"name": artist}], "album": {"name": album, "release_date": release},
                     "duration_ms": 200000, "external_ids": {"isrc": isrc or f"ISRC{tid[:6]}"},
                     "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"}}}


class FakeSpotify:
    def __init__(self, playlists):
        self.playlists = playlists  # id -> {"name":..., "items":[raw...]}
        self.calls = 0

    def me(self):
        return {"id": "me", "display_name": "Me"}

    def my_playlists(self):
        return [{"id": k} for k in self.playlists]

    def playlist_meta(self, pid):
        return {"id": pid, "name": self.playlists[pid]["name"], "snapshot_id": f"s{self.calls}",
                "owner": {"id": "me"}, "description": ""}

    def playlist_items(self, pid):
        from app.spotify import normalize_item
        return [normalize_item(r, i) for i, r in enumerate(self.playlists[pid]["items"])]

    def reorder(self, pid, start, before, length, snapshot):
        items = self.playlists[pid]["items"]
        block = items[start:start + length]
        rest = items[:start] + items[start + length:]
        ins = before if before < start else before - length
        self.playlists[pid]["items"] = rest[:ins] + block + rest[ins:]
        self.calls += 1
        return f"s{self.calls}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    opts_path = tmp_path / "options.json"
    monkeypatch.setenv("OPTIONS_PATH", str(opts_path))
    monkeypatch.setenv("PRIVATE_DIR", str(tmp_path))
    for mod in [m for m in sys.modules if m.startswith("app")]:
        del sys.modules[mod]

    def write(**over):
        from app.config import DEFAULTS
        opts = {**DEFAULTS, "data_dir": str(tmp_path / "share"), **over}
        opts_path.write_text(json.dumps(opts))
        return opts
    return tmp_path, write


def practice_playlist():
    """The fixture order-file tracks in a shuffled starting order."""
    with open(FIXTURE, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    items = [make_item(r["Track ID"], r["Track"], r["Artist"], r["Date Added"], r["Album"]) for r in rows]
    random.Random(1).shuffle(items)
    return rows, items


def test_plan_moves_random():
    from app.sorter import plan_moves
    rng = random.Random(0)
    for _ in range(500):
        n = rng.randint(0, 40)
        cur = [f"t{rng.randint(0, n // 2 + 1)}" for _ in range(n)]  # includes duplicates
        wanted = cur[:]
        rng.shuffle(wanted)
        sim = cur[:]
        for start, before, length in plan_moves(cur, wanted):
            block = sim[start:start + length]
            rest = sim[:start] + sim[start + length:]
            ins = before if before < start else before - length
            sim = rest[:ins] + block + rest[ins:]
        assert sim == wanted


def test_backup_then_sort_with_order_file(env):
    tmp, write = env
    rows, items = practice_playlist()
    extra = make_item("X" * 22, "New Song", "Someone", "2025-03-01T00:00:00Z")
    items.append(extra)  # a track added after the order file was made
    pid = "5MIjcSN6exeHVBu88XdGij"
    write(sort_playlists=[{"playlist": f"https://open.spotify.com/playlist/{pid}?si=abc",
                           "order_file": "Practice Order.csv"}])
    share = tmp / "share"
    (share / "orders").mkdir(parents=True)
    (share / "orders" / "Practice Order.csv").write_bytes((FIXTURE).read_bytes())

    from app import jobs
    fake = FakeSpotify({pid: {"name": "Practice: playlist", "items": items}})
    result = jobs.run_job("test", sp_factory=lambda o: fake)
    assert result["status"] == "ok", result
    final = [i["item"]["id"] for i in fake.playlists[pid]["items"]]
    assert final == [r["Track ID"] for r in rows] + ["X" * 22]
    assert fake.calls == result["sorted"][0]["moves"] > 0

    # CSV written with a filesystem-safe name and the original column layout
    out = share / "playlists" / "Practice_ playlist.csv"
    header = out.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("Position,Track,Album,Artist,Date Added,Track ID,ISRC")

    # Second run: the playlist changed (it was sorted) so one more version is stored; third run: unchanged.
    jobs.run_job("test", sp_factory=lambda o: fake)
    jobs.run_job("test", sp_factory=lambda o: fake)
    db = jobs.open_db(jobs.load_options())
    assert len(db.backups_for(pid)) == 2
    assert db.playlists()[0]["versions"] == 2


def test_sort_by_field_and_dry_run(env):
    _, write = env
    items = [make_item("A" * 22, "b", "Zed", "2021"), make_item("B" * 22, "a", "Abe", "2023"),
             make_item("C" * 22, "c", "Moe", "2022")]
    pid = "P" * 22
    from app import jobs
    write(sort_playlists=[{"playlist": pid, "sort_by": "artist"}], dry_run=True)
    fake = FakeSpotify({pid: {"name": "x", "items": items}})
    assert jobs.run_job("t", sp_factory=lambda o: fake)["sorted"][0]["moves"] > 0
    assert fake.calls == 0  # dry run touched nothing

    write(sort_playlists=[{"playlist": pid, "sort_by": "added_at", "reverse": True}])
    jobs.run_job("t", sp_factory=lambda o: fake)
    assert [i["item"]["name"] for i in fake.playlists[pid]["items"]] == ["a", "c", "b"]


def test_failure_is_recorded_and_other_playlists_continue(env):
    _, write = env
    good, bad = "G" * 22, "B" * 22
    write(backup_playlists=[bad, good])
    from app import jobs
    fake = FakeSpotify({good: {"name": "good", "items": [make_item("A" * 22, "a", "x", "2020")]}})
    res = jobs.run_job("t", sp_factory=lambda o: fake)
    assert res["status"] == "partial" and len(res["backed_up"]) == 1 and res["errors"]


def test_parse_playlist_id():
    from app.spotify import parse_playlist_id
    assert parse_playlist_id("https://open.spotify.com/playlist/4X8ptjd40Fo10bdzAI1Yrk?si=6") == "4X8ptjd40Fo10bdzAI1Yrk"
    assert parse_playlist_id("spotify:playlist:4X8ptjd40Fo10bdzAI1Yrk") == "4X8ptjd40Fo10bdzAI1Yrk"
    assert parse_playlist_id("Liked Songs") == "liked"
    with pytest.raises(ValueError):
        parse_playlist_id("delectable tunas")


def test_web_ui(env):
    _, write = env
    pid = "W" * 22
    write(backup_playlists=[pid], client_id="id", client_secret="secret")
    from app import jobs
    from app.scheduler import Scheduler
    from app.web import create_app
    jobs.run_job("t", sp_factory=lambda o: FakeSpotify(
        {pid: {"name": "Web", "items": [make_item("A" * 22, "Song", "Art", "2020")]}}))
    c = create_app(Scheduler("0 3 * * 1")).test_client()
    h = {"X-Ingress-Path": "/api/hassio_ingress/abc"}
    page = c.get("/", headers=h).get_data(as_text=True)
    assert "Web" in page and "/api/hassio_ingress/abc/export.zip" in page and "Connect Spotify" in page
    assert "Song" in c.get(f"/playlist/{pid}").get_data(as_text=True)
    assert c.get("/backup/1.csv").get_data(as_text=True).lstrip("﻿").startswith("Position,")
    z = c.get("/export.zip")
    assert z.status_code == 200 and z.data[:2] == b"PK"
    assert c.get("/library.db").status_code == 200
    assert "authorize" in c.get("/connect").get_data(as_text=True)
    assert "/connect?error=" in c.post("/connect", data={"url": "nope"}).headers["Location"]
    assert c.get("/status.json").json["next_run"] is None


def test_addon_config_is_consistent():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert set(cfg["options"]) == set(cfg["schema"])
    from app.config import DEFAULTS
    assert set(DEFAULTS) == set(cfg["options"])
    assert DEFAULTS == {k: v for k, v in cfg["options"].items()}


def test_status_sensor_published(env, monkeypatch):
    _, write = env
    pid = "S" * 22
    write(backup_playlists=[pid])
    from app import ha, jobs
    from app.scheduler import Scheduler
    sent = []
    monkeypatch.setattr(ha, "call", lambda path, payload: sent.append((path, payload)) or True)
    fake = FakeSpotify({pid: {"name": "S", "items": [make_item("A" * 22, "a", "x", "2020")]}})
    monkeypatch.setattr(jobs.spotify, "client", lambda o: fake)
    monkeypatch.setattr(jobs.spotify, "is_authorized", lambda o: True)
    monkeypatch.setattr(jobs, "run_job", lambda t, _orig=jobs.run_job: _orig(t, sp_factory=lambda o: fake))

    s = Scheduler("0 3 * * *")
    s._compute_next()
    s.publish_status()
    assert sent[-1][0] == "states/sensor.spotify_backup_status"
    assert sent[-1][1]["state"] == "never_run"

    s._run("test")
    states = [p["state"] for path, p in sent if path.startswith("states/")]
    assert states[-2:] == ["running", "ok"]
    attrs = sent[-1][1]["attributes"]
    assert attrs["last_success"] and attrs["next_run"] and attrs["errors"] == []
    assert attrs["playlists_backed_up"] == 1 and attrs["total_tracks"] == 1
    assert attrs["playlists_changed_last_run"] == ["S"]

    # A failing run: state flips, errors listed, last_success unchanged, notification sent.
    write(backup_playlists=["Q" * 22])
    s._run("test")
    status = [p for path, p in sent if path.startswith("states/")][-1]
    assert status["state"] == "failed" and status["attributes"]["errors"]
    assert status["attributes"]["last_success"] == attrs["last_success"]
    assert any(path.startswith("services/persistent_notification") for path, _ in sent)


def test_health_endpoint(env, monkeypatch):
    _, write = env
    write()
    from datetime import datetime, timedelta
    from app.scheduler import Scheduler
    from app.web import create_app
    s = Scheduler("0 3 * * *")
    monkeypatch.setenv("SUPERVISOR_TOKEN", "x")  # under Supervisor, other pages are ingress-only
    c = create_app(s).test_client()
    assert c.get("/").status_code == 403
    assert c.get("/health").status_code == 503  # loop never started
    s.heartbeat = datetime.now().astimezone()
    assert c.get("/health").status_code == 200
    s.heartbeat -= timedelta(hours=1)
    assert c.get("/health").status_code == 503
