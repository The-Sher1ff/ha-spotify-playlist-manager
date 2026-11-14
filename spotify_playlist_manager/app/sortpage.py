"""Spreadsheet-style sort editor: settings, per-song overrides, CSV upload, preview."""
import csv
import io
import json
import re
from urllib.parse import quote

from flask import Response, abort, jsonify, redirect, request

from . import jobs, spotify
from .backup import csv_bytes, safe_name
from .config import data_dir, load_options
from .sorter import SORT_FIELDS, build_order, song_key

DEFAULT_SETTINGS = {"enabled": 0, "sort_by": "added_at", "reverse": 1, "keep_original_dates": 1}

# Column names we accept in uploaded CSVs: our own export, plus Exportify-style names.
COLUMNS = {
    "track_id": ["Track ID", "Track URI", "Spotify URI"],
    "name": ["Track", "Track Name", "Name"],
    "artists": ["Artist", "Artist Name(s)", "Artists"],
    "added_at": ["Date Added", "Added At"],
    "isrc": ["ISRC"],
}


def pick(row: dict, field: str) -> str:
    for col in COLUMNS[field]:
        v = (row.get(col) or "").strip()
        if v:
            if field == "track_id" and v.startswith("spotify:track:"):
                v = v.split(":")[-1]
            return v
    return ""


def parse_upload(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        rows.append({f: pick(r, f) for f in COLUMNS})
    return rows


def register(app, sched, page, base):
    def context(pid: str):
        opts = load_options()
        db = jobs.open_db(opts)
        meta = db.playlist(pid)
        bid = db.latest_backup_id(pid)
        if not meta or not bid:
            abort(404)
        return opts, db, meta, bid

    def current_rule(opts, db, pid):
        settings = db.sort_settings(pid)
        config_rule = next((r for r in opts["sort_playlists"]
                            if spotify.parse_playlist_id(r["playlist"]) == pid), None)
        if settings:
            return settings, jobs.rule_from_settings(settings), config_rule
        if config_rule:
            s = {"playlist_id": pid, "enabled": 1, "reverse": int(bool(config_rule.get("reverse"))),
                 "keep_original_dates": 1,
                 "sort_by": "custom" if config_rule.get("order_file") else config_rule.get("sort_by") or "added_at"}
            return s, {**config_rule, "keep_original_dates": True}, config_rule
        s = {"playlist_id": pid, **DEFAULT_SETTINGS}
        return s, jobs.rule_from_settings(s), None

    def planned(pid):
        opts, db, meta, bid = context(pid)
        settings, rule, config_rule = current_rule(opts, db, pid)
        rows = [dict(r) for r in db.backup_rows(bid)]
        error = None
        try:
            ordered = build_order(rows, rule, data_dir(opts) / "orders", db.history(pid), db.overrides(pid))
        except (OSError, ValueError) as e:
            error = f"Can't build the order: {e}"
            ordered = build_order(rows, {"sort_by": "added_at", "reverse": True}, data_dir(opts) / "orders",
                                  db.history(pid), db.overrides(pid))
        return opts, db, meta, settings, config_rule, ordered, error

    def last_run(db):
        runs = db.recent_runs(1)
        if not runs:
            return None
        summary = json.loads(runs[0]["summary"]) if runs[0]["summary"] else {}
        return {"status": runs[0]["status"], "trigger": runs[0]["trigger"], "errors": summary.get("errors", [])}

    @app.get("/playlist/<pid>/sort")
    def sort_page(pid):
        _, _, meta, _ = context(pid)
        return page(SORT_PAGE, pid=pid, name=meta["name"], msg=request.args.get("msg"),
                    liked=pid == spotify.LIKED_SONGS)

    @app.get("/api/playlist/<pid>/preview")
    def preview(pid):
        opts, db, meta, settings, config_rule, ordered, error = planned(pid)
        custom_file = (data_dir(opts) / "orders" / f"{pid}.csv").exists()
        keep = ("position", "new_position", "name", "artists", "album", "added_at", "original_added",
                "effective_date", "date_source", "override_id", "override_date", "pin", "track_id",
                "release_date", "duration_ms")
        return jsonify({
            "name": meta["name"], "settings": settings, "error": error, "custom_file": custom_file,
            "config_rule": bool(config_rule), "running": jobs.is_running(), "last_run": last_run(db),
            "moves": sum(1 for r in ordered if r["position"] != r["new_position"]),
            "rows": [{k: r.get(k) for k in keep} for r in ordered],
        })

    @app.post("/api/playlist/<pid>/settings")
    def save_settings(pid):
        _, db, _, _ = context(pid)
        body = request.get_json(force=True)
        sort_by = body.get("sort_by", "added_at")
        if sort_by not in SORT_FIELDS and sort_by != "custom":
            abort(400)
        db.save_sort_settings(pid, bool(body.get("enabled")), sort_by, bool(body.get("reverse")),
                              bool(body.get("keep_original_dates", True)))
        return jsonify(ok=True)

    @app.post("/api/playlist/<pid>/override")
    def save_override(pid):
        _, db, _, bid = context(pid)
        body = request.get_json(force=True)
        rows = {r["position"]: dict(r) for r in db.backup_rows(bid)}
        row = rows.get(body.get("position"))
        if not row or row["track_id"] != body.get("track_id"):
            return jsonify(error="The playlist changed since this page loaded. Refresh and try again."), 409
        date = (body.get("date") or "").strip() or None
        if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return jsonify(error="Dates must look like 2024-05-31."), 400
        pin = body.get("pin")
        pin = int(pin) if pin not in (None, "", 0, "0") else None
        ident = {"track_id": row["track_id"], "isrc": row["isrc"], "name": row["name"], "artists": row["artists"],
                 "song_key": song_key(row["name"], row["artists"])}
        db.save_override(pid, body.get("override_id"), ident, date, pin, jobs.now_iso())
        return jsonify(ok=True)

    @app.post("/api/playlist/<pid>/apply")
    def apply_now(pid):
        context(pid)
        if pid == spotify.LIKED_SONGS:
            abort(400)
        sched.trigger("web-sort", only=pid)
        return jsonify(queued=True)

    @app.get("/playlist/<pid>/planned.csv")
    def planned_csv(pid):
        _, _, meta, _, _, ordered, _ = planned(pid)
        rows = [{**r, "position": r["new_position"], "added_at": r["effective_date"], "url": r.get("url")}
                for r in ordered]
        return Response(csv_bytes(rows), mimetype="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{safe_name(meta["name"])} planned order.csv"'})

    @app.post("/playlist/<pid>/upload")
    def upload(pid):
        opts, db, _, _ = context(pid)
        f = request.files.get("file")
        mode = request.form.get("mode")
        back = f"{base()}/playlist/{pid}/sort?msg="
        if not f or not f.filename:
            return redirect(back + quote("Choose a CSV file first."))
        raw = f.read()
        rows = parse_upload(raw)
        if mode == "order":
            if not any(r["track_id"] for r in rows):
                return redirect(back + quote("That CSV has no 'Track ID' column, so it can't be used as an order."))
            (data_dir(opts) / "orders" / f"{pid}.csv").write_bytes(raw)
            s = db.sort_settings(pid) or {**DEFAULT_SETTINGS}
            db.save_sort_settings(pid, bool(s["enabled"]), "custom", False, bool(s["keep_original_dates"]))
            return redirect(back + quote(f"Custom order uploaded ({len(rows)} rows). Sort is now set to "
                                         f"'Custom order'. Check the preview, then Apply."))
        if mode == "history":
            usable = [r for r in rows if r["added_at"] and (r["track_id"] or r["name"])]
            if not usable:
                return redirect(back + quote("No rows with a 'Date Added' and a track found in that CSV."))
            db.import_history(pid, usable, f.filename)
            return redirect(back + quote(f"Imported {len(usable)} dates from {f.filename}. Songs that match "
                                         f"use the earliest date if 'Keep original dates' is on."))
        abort(400)


SORT_PAGE = r"""
<style>
main{max-width:1400px}
.grid td,.grid th{padding:4px 6px}.grid th{cursor:pointer;user-select:none}
.grid th.sorted::after{content:attr(data-dir);margin-left:4px}
.grid input{width:120px;padding:3px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);font:inherit}
.grid input.pin{width:50px}.grid td.t{max-width:260px;overflow:hidden;text-overflow:ellipsis}
.badge{font-size:11px;padding:1px 6px;border-radius:9px;border:1px solid var(--line);margin-left:4px}
.badge.history{border-color:#3a7bd5;color:#3a7bd5}.badge.override{border-color:var(--warn);color:var(--warn)}
.moved{color:var(--acc);font-weight:600}
select,input[type=search]{padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
label{display:inline-flex;gap:5px;align-items:center}
.settings{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
</style>
<p><a href="{{base}}/">← All playlists</a> · <a href="{{base}}/playlist/{{pid}}">Version history</a></p>
<h1>Sort: {{ name }}</h1>
{% if msg %}<div class="card">{{ msg }}</div>{% endif %}
{% if liked %}<div class="card failed">Spotify doesn't allow re-ordering Liked Songs. You can preview here, but not apply.</div>{% endif %}

<div class="card">
  <div class="settings">
    <label><input type="checkbox" id="enabled"> Sort automatically on each scheduled run</label>
    <label>Sort by <select id="sort_by">
      <option value="added_at">Date added</option><option value="name">Track name</option>
      <option value="artist">Artist</option><option value="album">Album</option>
      <option value="release_date">Release date</option><option value="duration">Duration</option>
      <option value="custom">Custom order (uploaded CSV)</option></select></label>
    <label><select id="reverse"><option value="1">Newest / Z first</option><option value="0">Oldest / A first</option></select></label>
    <label title="If a song was in this playlist before under another version or ID, use the date it was first added">
      <input type="checkbox" id="keep"> Keep original dates for re-added songs</label>
  </div>
  <p class="muted" id="note"></p>
  <div class="row">
    <button class="btn" id="apply" {% if liked %}disabled{% endif %}>Apply to Spotify now</button>
    <span id="status" class="muted"></span>
    <span style="flex:1"></span>
    <a class="btn alt" href="{{base}}/playlist/{{pid}}/planned.csv">Download planned order (CSV)</a>
  </div>
</div>

<div class="card">
  <h2>Upload a CSV</h2>
  <form method="post" enctype="multipart/form-data" action="{{base}}/playlist/{{pid}}/upload" class="row">
    <input type="file" name="file" accept=".csv,text/csv">
    <label><input type="radio" name="mode" value="order" checked> Use as custom order (rows top to bottom)</label>
    <label><input type="radio" name="mode" value="history"> Import original dates from an old export</label>
    <button class="btn alt">Upload</button>
  </form>
  <p class="muted">For a custom order: download the planned order above, rearrange the rows in Excel or Sheets,
    save as CSV and upload it here. Tracks missing from your file go at the end.</p>
</div>

<div class="card">
  <div class="row" style="margin-bottom:8px">
    <input type="search" id="filter" placeholder="Filter by track, artist or album" style="flex:1;min-width:200px">
    <label><input type="checkbox" id="onlyFlagged"> Only re-added / overridden</label>
  </div>
  <p class="muted">Click a column heading to view the table in that order (this doesn't change the playlist).
    <b>Override date</b> sorts the song as if added that day. <b>Pin</b> fixes it at that position (1 = top).
    Changes save on their own.</p>
  <table class="grid"><thead><tr>
    <th data-k="new_position">New #</th><th data-k="position">Now #</th><th data-k="name">Track</th>
    <th data-k="artists">Artist</th><th data-k="album">Album</th><th data-k="added_at">Added (Spotify)</th>
    <th data-k="effective_date">Date used</th><th data-k="override_date">Override date</th><th data-k="pin">Pin</th>
  </tr></thead><tbody id="rows"><tr><td colspan="9" class="muted">Loading…</td></tr></tbody></table>
</div>

<script>
const BASE = {{ base|tojson }}, PID = {{ pid|tojson }};
const api = p => `${BASE}/api/playlist/${PID}/${p}`;
let data = null, view = {k: "new_position", dir: 1};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const day = s => (s || "").slice(0, 10);

async function load() {
  const r = await fetch(api("preview"));
  data = await r.json();
  const s = data.settings;
  $("enabled").checked = !!s.enabled; $("sort_by").value = s.sort_by;
  $("reverse").value = s.reverse ? "1" : "0"; $("keep").checked = !!s.keep_original_dates;
  const notes = [];
  if (data.error) notes.push(`<span class="failed">${esc(data.error)}</span>`);
  if (s.sort_by === "custom" && !data.custom_file) notes.push("Upload a custom order CSV below.");
  if (data.config_rule) notes.push("This playlist also has a sort_playlists entry in the add-on configuration. Settings saved here take priority.");
  notes.push(`${data.moves} of ${data.rows.length} tracks would move.`);
  $("note").innerHTML = notes.join(" ");
  $("status").textContent = data.running ? "A run is in progress…" : "";
  render();
}

function render() {
  const q = $("filter").value.toLowerCase(), only = $("onlyFlagged").checked;
  let rows = data.rows.filter(r => (!q || `${r.name} ${r.artists} ${r.album}`.toLowerCase().includes(q))
                                 && (!only || r.date_source !== "spotify" || r.pin));
  rows.sort((a, b) => { const x = a[view.k] ?? "", y = b[view.k] ?? "";
    return (x < y ? -1 : x > y ? 1 : 0) * view.dir || a.new_position - b.new_position; });
  document.querySelectorAll(".grid th").forEach(th => {
    th.classList.toggle("sorted", th.dataset.k === view.k); th.dataset.dir = view.dir > 0 ? "▲" : "▼"; });
  $("rows").innerHTML = rows.map(r => `<tr>
    <td class="${r.position !== r.new_position ? "moved" : ""}">${r.new_position + 1}</td><td>${r.position + 1}</td>
    <td class="t" title="${esc(r.name)}">${esc(r.name)}</td><td class="t">${esc(r.artists)}</td><td class="t">${esc(r.album)}</td>
    <td>${day(r.added_at)}</td>
    <td>${day(r.effective_date)}${r.date_source === "history" ? '<span class="badge history" title="Re-added song: using the date it was first added">original</span>' : ""}${r.date_source === "override" ? '<span class="badge override">override</span>' : ""}</td>
    <td><input type="date" value="${day(r.override_date)}" data-pos="${r.position}"></td>
    <td><input class="pin" type="number" min="1" value="${r.pin ?? ""}" data-pos="${r.position}"></td></tr>`).join("")
    || '<tr><td colspan="9" class="muted">No tracks match.</td></tr>';
}

async function post(path, body) {
  const r = await fetch(api(path), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!r.ok) { const e = await r.json().catch(() => ({})); alert(e.error || `Save failed (${r.status})`); }
  return r;
}

$("rows").addEventListener("change", async e => {
  const pos = +e.target.dataset.pos; if (isNaN(pos)) return;
  const r = data.rows.find(x => x.position === pos), tr = e.target.closest("tr");
  await post("override", {position: pos, track_id: r.track_id, override_id: r.override_id,
    date: tr.querySelector("input[type=date]").value, pin: tr.querySelector(".pin").value});
  load();
});
["enabled", "sort_by", "reverse", "keep"].forEach(id => $(id).addEventListener("change", async () => {
  await post("settings", {enabled: $("enabled").checked, sort_by: $("sort_by").value,
    reverse: $("reverse").value === "1", keep_original_dates: $("keep").checked});
  load();
}));
document.querySelectorAll(".grid th").forEach(th => th.addEventListener("click", () => {
  view = {k: th.dataset.k, dir: view.k === th.dataset.k ? -view.dir : 1}; render(); }));
$("filter").addEventListener("input", render); $("onlyFlagged").addEventListener("change", render);
$("apply").addEventListener("click", async () => {
  if (!confirm("Back up this playlist, then re-order it on Spotify to match the New # column?")) return;
  await post("apply", {});
  $("status").textContent = "Queued. Backing up and sorting… refresh in a minute to see the result.";
  const t = setInterval(async () => {
    await load();
    if (data.running) return;
    clearInterval(t);
    const lr = data.last_run || {};
    $("status").innerHTML = lr.status === "ok" ? '<span class="ok">Done. Spotify now matches the New # column.</span>'
      : `<span class="failed">Sort ${esc(lr.status || "failed")}: ${esc((lr.errors || []).join("; "))}</span>`;
  }, 3000);
});
load();
</script>
"""
