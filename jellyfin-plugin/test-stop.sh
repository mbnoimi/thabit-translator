#!/bin/bash
# Shut the test instance down safely: graceful SIGTERM via `docker compose stop`
# with enough grace time for Jellyfin to flush its database and finish writing,
# then confirm the container really reached `exited`.
#
#   ./test-stop.sh
#
# Nothing is deleted: jf-config/, jf-cache/ and media-test/ are bind mounts and
# keep all state (library DB, accounts, the installed plugin, produced .srt
# files). A stop in the middle of a subtitle job can at worst leave a stale
# staging folder under jf-config/ - the pipeline never writes into the media
# folder itself, and the next run rebuilds staging from scratch.
#
# Bring it back with:   ./test.sh            (rebuild + recreate, the full loop)
# or quickly with:      env UID=$(id -u) GID=$(id -g) docker compose start jellyfin
set -euo pipefail

# Constraint: PATH entries under ~/.android cause EIO through execvp on this
# machine, so every command must run with a sanitized PATH.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

cd "$(dirname "$0")"

# bash's UID is readonly, so it must be injected per-invocation via env(1).
dc() { env UID="$(id -u)" GID="$(id -g)" docker compose "$@"; }

cid=$(dc ps -aq jellyfin 2>/dev/null | head -n1 || true)
if [ -z "$cid" ]; then
    echo "Nothing to stop: this compose project has no Jellyfin container."
    exit 0
fi

state=$(docker inspect -f '{{.State.Status}}' "$cid")
if [ "$state" != "running" ]; then
    echo "Already stopped: container state is '$state'."
    exit 0
fi

echo "==> stopping Jellyfin (SIGTERM, 30s grace to flush the database)"
if ! dc stop -t 30 jellyfin; then
    echo "ERROR: docker compose stop failed" >&2
    exit 1
fi

state=$(docker inspect -f '{{.State.Status}}' "$cid")
exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$cid")
if [ "$state" != "exited" ]; then
    echo "ERROR: container is '$state', expected 'exited'" >&2
    exit 1
fi
if [ "$exit_code" != "0" ]; then
    echo "WARNING: stopped, but exit code was $exit_code (grace period may have been too short)" >&2
fi

echo "    state: exited (code $exit_code) - state preserved in jf-config/, media-test/"
echo "OK - test instance stopped. Restart with ./test.sh or 'docker compose start jellyfin'."
