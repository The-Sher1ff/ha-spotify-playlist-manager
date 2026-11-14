"""Small web UI served through Home Assistant Ingress (sidebar panel)."""
import io
import json
import os
import zipfile
from datetime import datetime
from urllib.parse import quote

from flask import Flask, Response, abort, redirect, render_template_string, request, send_file

from . import jobs, sortpage, spotify
from .backup import csv_bytes, safe_name
from .config import data_dir, load_options

INGRESS_IP = "172.30.32.2"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Spotify Backup</title>
<style>
:root{--bg:#fafafa;--fg:#1c1c1c;--muted:#666;--card:#fff;--line:#e3e3e3;--acc:#1db954;--bad:#d33;--warn:#c80}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#e8e8e8;--muted:#999;--card:#1c1c1c;--line:#333}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
main{max-width:960px;margin:auto}h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}a{color:var(--acc)}.muted{color:var(--muted)}
.btn{display:inline-block;background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 12px;font:inherit;cursor:pointer;text-decoration:none}
.btn.alt{background:transparent;color:var(--fg);border:1px solid var(--line)}
.ok{color:var(--acc)}.failed{color:var(--bad)}.partial,.running{color:var(--warn)}
input[type=text]{width:100%;box-sizing:border-box;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
</style></head><body><main>{{ body|safe }}</main></body></html>"""


def create_app(sched):
    app = Flask(__name__)

    @app.before_request
    def only_ingress():
        # Under the Supervisor, only accept traffic coming through Ingress.
        # /health reveals nothing and is what the Supervisor watchdog polls.
        if request.path != "/health" and os.environ.get("SUPERVISOR_TOKEN") \
                and request.remote_addr != INGRESS_IP:
            abort(403)

    @app.get("/health")
    def health():
        # Kept cheap on purpose (no Spotify or DB calls) so a slow API can't trigger restarts.
        return ("ok", 200) if sched.healthy() else ("scheduler stalled", 503)

    def base() -> str:
        return request.headers.get("X-Ingress-Path", "")

    def page(body: str, **ctx):
        return render_template_string(PAGE, body=render_template_string(body, base=base(), **ctx))

    @app.get("/")
    def index():
        opts = load_options()
        db = jobs.open_db(opts)
        runs = []
        for r in db.recent_runs():
            s = json.loads(r["summary"]) if r["summary"] else {}
            runs.append({**dict(r), "errors": s.get("errors", []), "n_backup": len(s.get("backed_up", [])),
                         "n_sort": len(s.get("sorted", []))})
        return page(INDEX, opts=opts, connected=spotify.is_authorized(opts), playlists=db.playlists(),
                    runs=runs, running=jobs.is_running(), sched=sched,
                    data_dir=str(data_dir(opts)))

    @app.post("/run")
    def run_now():
        sched.trigger("web")
        return redirect(f"{base()}/")

    @app.get("/connect")
    def connect():
        opts = load_options()
        if not opts["client_id"] or not opts["client_secret"]:
            return page("<h1>Connect Spotify</h1><div class='card'>Set <b>client_id</b> and <b>client_secret</b> "
                        "in the add-on Configuration tab first, then restart the add-on.</div>"
                        "<a href='{{base}}/'>Back</a>")
        url = spotify.auth_manager(opts).get_authorize_url()
        return page(CONNECT, url=url, redirect_uri=opts["redirect_uri"], error=request.args.get("error"))

    @app.post("/connect")
    def connect_finish():
        opts = load_options()
        am = spotify.auth_manager(opts)
        pasted = request.form.get("url", "").strip()
        try:
            if "code=" not in pasted:
                raise ValueError("That doesn't look like the address Spotify redirected you to "
                                 "(it should contain '?code=').")
            am.get_access_token(am.parse_response_code(pasted), as_dict=False, check_cache=False)
        except Exception as e:  # noqa: BLE001
            return redirect(f"{base()}/connect?error={quote(str(e)[:200])}")
        return redirect(f"{base()}/")

    @app.get("/playlist/<pid>")
    def playlist(pid):
        db = jobs.open_db(load_options())
        backups = db.backups_for(pid)
        if not backups:
            abort(404)
        name = db.backup(backups[0]["backup_id"])["name"]
        return page(HISTORY, name=name, backups=backups, pid=pid,
                    rows=db.backup_rows(backups[0]["backup_id"]))

    @app.get("/backup/<int:backup_id>.csv")
    def backup_csv(backup_id):
        db = jobs.open_db(load_options())
        b = db.backup(backup_id)
        if not b:
            abort(404)
        fname = f"{safe_name(b['name'])} {b['captured_at'][:10]}.csv"
        return Response(csv_bytes(db.backup_rows(backup_id)), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.get("/library.db")
    def database():
        path = data_dir(load_options()) / "library.db"
        return send_file(path, as_attachment=True, download_name="spotify_library.db")

    @app.get("/export.zip")
    def export_zip():
        opts = load_options()
        db = jobs.open_db(opts)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in db.playlists():
                bid = db.latest_backup_id(p["playlist_id"])
                if bid:
                    z.writestr(f"playlists/{safe_name(p['name'])}.csv", csv_bytes(db.backup_rows(bid)))
            z.write(data_dir(opts) / "library.db", "library.db")
            z.writestr("README.txt", EXPORT_README)
        buf.seek(0)
        stamp = datetime.now().strftime("%Y-%m-%d")
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"spotify_export_{stamp}.zip")

    sortpage.register(app, sched, page, base)

    @app.get("/status.json")
    def status():
        opts = load_options()
        runs = jobs.open_db(opts).recent_runs(1)
        return {"connected": spotify.is_authorized(opts), "running": jobs.is_running(),
                "next_run": sched.next_run.isoformat() if sched.next_run else None,
                "last_run": dict(runs[0]) if runs else None}

    return app


INDEX = """
<h1>Spotify Playlist Manager</h1>
<div class="card row">
  {% if connected %}<span class="ok">● Spotify connected</span>{% else %}
  <span class="failed">● Spotify not connected</span> <a class="btn" href="{{base}}/connect">Connect Spotify</a>{% endif %}
  <span class="muted">Schedule <code>{{ sched.cron }}</code>{% if sched.next_run %} · next run
  {{ sched.next_run.strftime('%a %d %b %H:%M') }}{% endif %}</span>
  <span style="flex:1"></span>
  {% if running %}<span class="running">Running…</span>{% else %}
  <form method="post" action="{{base}}/run"><button class="btn">Run now</button></form>{% endif %}
</div>

<div class="card">
  <div class="row" style="margin-bottom:8px"><h2 style="margin:0">Backed-up playlists</h2><span style="flex:1"></span>
    <a class="btn alt" href="{{base}}/export.zip">Download everything (.zip)</a>
    <a class="btn alt" href="{{base}}/library.db">Download database</a></div>
  {% if playlists %}
  <table><tr><th>Playlist</th><th>Tracks</th><th>Versions</th><th>Last checked</th><th></th></tr>
  {% for p in playlists %}<tr>
    <td><a href="{{base}}/playlist/{{p.playlist_id}}">{{ p.name }}</a></td><td>{{ p.track_count }}</td>
    <td>{{ p.versions }}</td><td class="muted">{{ p.last_backup[:16].replace('T',' ') }} UTC</td>
    <td><a href="{{base}}/playlist/{{p.playlist_id}}/sort">sort &amp; overrides</a> ·
      <a href="{{base}}/playlist/{{p.playlist_id}}">history</a></td></tr>{% endfor %}
  </table>{% else %}<p class="muted">Nothing backed up yet. Add playlists in the add-on Configuration tab, then Run now.</p>{% endif %}
  <p class="muted">Files are also in <code>{{ data_dir }}</code>, which is the <code>share</code> folder over Samba / File editor.</p>
</div>

<div class="card"><h2>Recent runs</h2>
  {% if runs %}<table><tr><th>Started (UTC)</th><th>Trigger</th><th>Status</th><th>Backed up</th><th>Sorted</th><th>Errors</th></tr>
  {% for r in runs %}<tr><td>{{ r.started_at[:16].replace('T',' ') }}</td><td>{{ r.trigger }}</td>
    <td class="{{r.status}}">{{ r.status }}</td><td>{{ r.n_backup }}</td><td>{{ r.n_sort }}</td>
    <td style="white-space:normal">{{ r.errors|join('; ') }}</td></tr>{% endfor %}</table>
  {% else %}<p class="muted">No runs yet.</p>{% endif %}
</div>
"""

CONNECT = """
<h1>Connect Spotify</h1>
{% if error %}<div class="card failed">{{ error }}</div>{% endif %}
<div class="card">
<ol>
  <li>In the <a href="https://developer.spotify.com/dashboard" target="_blank">Spotify developer dashboard</a>,
      make sure <code>{{ redirect_uri }}</code> is listed under Redirect URIs.</li>
  <li><a class="btn" href="{{ url }}" target="_blank" rel="noopener">Authorize with Spotify</a></li>
  <li>After you approve, your browser opens a page that fails to load. That's expected. Copy the
      whole address from the address bar (it starts with <code>{{ redirect_uri }}?code=</code>) and paste it below.</li>
</ol>
<form method="post" action="{{base}}/connect" class="row">
  <input type="text" name="url" placeholder="{{ redirect_uri }}?code=..." required>
  <button class="btn">Finish connecting</button>
</form></div>
<a href="{{base}}/">Back</a>
"""

HISTORY = """
<p><a href="{{base}}/">← All playlists</a></p>
<h1>{{ name }}</h1>
<p><a class="btn alt" href="{{base}}/playlist/{{pid}}/sort">Sort &amp; overrides</a></p>
<div class="card"><h2>Versions</h2>
<p class="muted">A new version is saved only when the playlist changed. Any version's CSV can be used as an
<code>order_file</code> to put the playlist back in that order.</p>
<table><tr><th>Captured (UTC)</th><th>Tracks</th><th></th></tr>
{% for b in backups %}<tr><td>{{ b.captured_at[:16].replace('T',' ') }}</td><td>{{ b.track_count }}</td>
<td><a href="{{base}}/backup/{{b.backup_id}}.csv">Download CSV</a></td></tr>{% endfor %}</table></div>
<div class="card"><h2>Latest tracks</h2>
<table><tr><th>#</th><th>Track</th><th>Artist</th><th>Album</th><th>Added</th></tr>
{% for r in rows %}<tr><td>{{ r.position }}</td><td>{{ r.name }}</td><td>{{ r.artists }}</td><td>{{ r.album }}</td>
<td class="muted">{{ (r.added_at or '')[:10] }}</td></tr>{% endfor %}</table></div>
"""

EXPORT_README = """Spotify playlist export
=======================

playlists/*.csv  Latest version of each playlist. Columns: Position, Track, Album, Artist,
                 Date Added, Track ID, ISRC, Spotify URI, Duration (ms), Release Date, Spotify URL.
                 To move to another service, import these CSVs with a transfer tool
                 (e.g. Soundiiz or TuneMyMusic). ISRC is the most reliable match key.

library.db       SQLite database with every saved version of every playlist.
                 Open it with "DB Browser for SQLite". The view `latest_playlist_tracks`
                 is the quickest place to start.
"""
