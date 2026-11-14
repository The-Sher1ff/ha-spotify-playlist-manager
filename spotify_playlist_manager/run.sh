#!/usr/bin/with-contenv bashio
# Home Assistant writes the add-on options to /data/options.json; the app reads it directly.
bashio::log.info "Starting Spotify Playlist Manager"
cd /app
exec python3 -u -m app.main
