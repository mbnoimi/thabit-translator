#!/bin/sh
# Installs the plugin into Jellyfin's data directory (a volume) on every start,
# then hands over to the base image's entrypoint.
set -eu

STAGE=/opt/thabit/plugin
PLUGIN_DIR="${JELLYFIN_DATA_DIR:-/config}/plugins/Jellyfin.Plugin.ThabitTranslator"

if [ -d "$STAGE" ]; then
    mkdir -p "$PLUGIN_DIR"
    # cp, not mv: the staging area lives in a read-only layer and must survive a
    # restart. Files are created by whoever runs this script (the compose file
    # runs the container as the host user), so Jellyfin can always rewrite them.
    cp -f "$STAGE"/* "$PLUGIN_DIR"/
    # Upgrade hygiene: the JSONL bridge (pre-CLI-launcher images) left this behind;
    # the library now travels inside the DLL as embedded *.py resources.
    rm -f "$PLUGIN_DIR/jellyfin_runner.py"
fi

exec /jellyfin/jellyfin "$@"
