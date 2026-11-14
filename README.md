# Spotify Playlist Tools: Home Assistant add-on repository

`spotify_playlist_manager/` is a Home Assistant add-on that runs the playlist backup and
re-ordering on a schedule. Setup and usage: [spotify_playlist_manager/DOCS.md](spotify_playlist_manager/DOCS.md).

## Install

In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**, then add
`https://github.com/The-Sher1ff/ha-spotify-playlist-manager`.

## Developing

```bash
cd spotify_playlist_manager
pip install -r requirements.txt pytest pyyaml
python -m pytest tests
```

The tests use a fake Spotify client, so they need no account or network.
