# Spotify Playlist Manager

Backs up your Spotify playlists to CSV and SQLite on a schedule, and can re-order playlists
from an order file or by a field. It replaces running `Reorder Playlist.py` by hand.

## 1. Spotify app (one-time)

1. Go to <https://developer.spotify.com/dashboard> and open your app. You can reuse the
   one from the original script.
2. Under **Redirect URIs**, add `http://127.0.0.1:8888/callback`.
3. Copy the **Client ID** and **Client secret**.

> Spotify Development Mode apps need the account owner to have Premium. They can only read
> playlists you **own or collaborate on**. Playlists you just follow can't be backed up.

## 2. Install the add-on

**Option A: from GitHub (recommended, updates show up in Home Assistant)**

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FThe-Sher1ff%2Fha-spotify-playlist-manager)

Or manually: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, then add
`https://github.com/The-Sher1ff/ha-spotify-playlist-manager`. Find **Spotify Playlist
Manager** in the store and click **Install**. The first build takes a few minutes.

**Option B: local add-on (no GitHub needed)**

1. Install the **Samba share** or **Studio Code Server** add-on, so you can reach the `/addons` folder.
2. Copy the `spotify_playlist_manager` folder into `/addons/` on Home Assistant.
3. Go to **Settings → Add-ons → Add-on store → ⋮ → Check for updates**. It appears under **Local add-ons**.
4. Click **Install**. The first build takes a few minutes.


## 3. Configure

Open the add-on's **Configuration** tab and switch to **Edit in YAML**:

```yaml
client_id: "your-client-id"
client_secret: "your-client-secret"
redirect_uri: "http://127.0.0.1:8888/callback"
schedule: "0 3 * * *"          # daily at 3am, see below
run_on_start: false
backup_all_my_playlists: true  # every playlist you own
backup_liked_songs: true
backup_playlists:              # extra/specific playlists (links, URIs or IDs)
  - https://open.spotify.com/playlist/4X8ptjd40Fo10bdzAI1Yrk
sort_playlists:
  - playlist: https://open.spotify.com/playlist/4X8ptjd40Fo10bdzAI1Yrk
    order_file: Order.csv      # file in /share/spotify_backup/orders/
  - playlist: https://open.spotify.com/playlist/5MIjcSN6exeHVBu88XdGij
    sort_by: added_at          # name | artist | album | added_at | release_date | duration
    reverse: true              # newest first
dry_run: false
notify_on_failure: true
data_dir: /share/spotify_backup
```

Save, then **restart** the add-on so the new options take effect.

### Schedule

`schedule` uses cron syntax, in Home Assistant's timezone:

| You want | `schedule` |
| --- | --- |
| Daily at 3am | `0 3 * * *` |
| Weekly, Monday 3am | `0 3 * * 1` |
| Monthly, 1st at 3am | `0 3 1 * *` |
| Every 6 hours | `0 */6 * * *` |

You can also trigger a run from a Home Assistant automation or script:

```yaml
action: hassio.addon_stdin
data:
  addon: local_spotify_playlist_manager
  input: run
```

`local_spotify_playlist_manager` is the ID for a local install. If you installed from GitHub,
the ID starts with a short code instead of `local_`. Copy it from the add-on page's URL
(the part after `/hassio/addon/`).

### Sorting

* **`order_file`**: a CSV in `/share/spotify_backup/orders/` with a `Track ID` column. Your
  existing `Order.csv` and `Practice Order.csv` work as-is. Tracks missing from the file keep
  their relative order at the end of the playlist, so songs you add later are never lost.
  **Any backup CSV is also a valid order file.** To roll a playlist back to an earlier order,
  download that version and use it as the order file.
* **`sort_by`**: sort by a field. Add `reverse: true` for descending order.
* Every sorted playlist is backed up **before** it's re-ordered.
* Set `dry_run: true` to see how many moves a sort would make (in the **Log** tab) without
  changing anything.

## 4. Connect Spotify (one-time)

1. Start the add-on and open **Spotify Backup** in the sidebar (or **Open Web UI**).
2. Click **Connect Spotify → Authorize with Spotify** and approve.
3. Your browser opens a `127.0.0.1` page that fails to load. That's expected. Copy the whole
   address from the address bar, paste it into the box, and click **Finish connecting**.

The token is saved privately in the add-on and refreshes itself. Click **Run now** to test.

## 5. Get to your data

All your data is in `/share/spotify_backup/` (the `share` folder over Samba):

| Path | What |
| --- | --- |
| `playlists/<name>.csv` | Latest copy of each playlist |
| `library.db` | SQLite. Every version of every playlist (a new version is saved only when it changed) |
| `orders/` | Put your sort order CSVs here |

From the web UI you can also download **everything as a .zip**, download the database, or
download any past version of a playlist as a CSV.

**Changing provider:** import the CSVs with a transfer tool such as Soundiiz or TuneMyMusic.
The `ISRC` column is a universal recording ID, so it matches the right version of a song
far more reliably than title/artist.

**Querying the database:** open `library.db` with *DB Browser for SQLite*. Then, for example:

```sql
SELECT * FROM latest_playlist_tracks WHERE playlist = 'Delectable Tunas' ORDER BY position;
```

## 6. Monitoring from Home Assistant

The add-on keeps an entity called **`sensor.spotify_backup_status`** up to date:

| | |
| --- | --- |
| State | `ok`, `partial` (some playlists failed), `failed`, `running` or `never_run` |
| `last_success` | When the last fully successful run finished (UTC) |
| `last_run`, `next_run` | When the last run finished, and when the next one is scheduled |
| `errors` | What went wrong in the last run |
| `playlists_backed_up`, `total_tracks` | Size of your backup |
| `playlists_changed_last_run` | Playlists that had changed since their previous backup |

It's refreshed every 5 minutes. After a Home Assistant restart it may take that long to
reappear, and until then automations may see it as `unavailable`.

**A watchdog** is also built in. If the add-on stops responding, the Supervisor restarts it.
Turn it on with the **Watchdog** switch on the add-on's Info tab.

### Example: alert your phone when backups stop

This catches failed runs, a stopped add-on and a broken schedule. All of them show up as no
successful run for over 2 days. (For a weekly schedule, use 8 days.)

```yaml
alias: Spotify backup is stale
triggers:
  - trigger: template
    value_template: >
      {% set s = state_attr('sensor.spotify_backup_status', 'last_success') %}
      {{ s is none or (now() - as_datetime(s)) > timedelta(days=2) }}
    for: "00:10:00"
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: Spotify backup needs attention
      message: >
        Last successful backup: {{ state_attr('sensor.spotify_backup_status', 'last_success') or 'never' }}.
        Errors: {{ state_attr('sensor.spotify_backup_status', 'errors') | join(', ') or 'none' }}
```

Template triggers re-check every minute because they use `now()`. You can also trigger on
the state changing to `failed` if you want to know straight away.

## Troubleshooting

* **"Spotify is not connected"**: redo step 4. This also happens if you change the
  client ID or secret.
* **403 errors on a playlist**: you don't own it or collaborate on it. Spotify blocks
  reading it for Development Mode apps.
* Failed runs create a Home Assistant notification, and the full details are in the
  add-on **Log** tab.
