#!/bin/bash
# One-shot dev loop: build the plugin zip, install it into the config volume,
# start the OFFICIAL Jellyfin image on top of it, then verify that the running
# server actually carries the freshly built DLL.
#
#   ./test.sh
#
# There is deliberately no custom image: the compose file runs stock
# jellyfin/jellyfin:10.11, so what ./test.sh exercises is exactly what a user's
# instance does - the plugin zip unpacked into jf-config/plugins/... . The
# install step below replaces the plugin's top-level files (keeping data dirs
# such as .venv_thabit/ and runtime/) on every run, so a rebuild is picked up
# without touching the expensive state. MEDIA_ROOT defaults to media-test/ (the
# self-contained fixture folder); export MEDIA_ROOT elsewhere to mount a
# different library.
#
# Bigger checks live next to this file:
#   env UID=$(id -u) GID=$(id -g) docker compose run --rm plugin-test   # xunit
#   python3 tests/e2e_smoke.py                                         # full HTTP e2e
set -euo pipefail

# Constraint: PATH entries under ~/.android cause EIO through execvp on this
# machine, so every command must run with a sanitized PATH.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

cd "$(dirname "$0")"

export MEDIA_ROOT="${MEDIA_ROOT:-$PWD/media-test}"

# bash's UID is readonly, so it must be injected per-invocation via env(1).
dc() { env UID="$(id -u)" GID="$(id -g)" docker compose "$@"; }

echo "==> [1/3] building the plugin zip (dotnet build + jprm)"
dc run --rm plugin-build

# Newest by mtime, not by name: alphabetical sort would pick a stale
# 1.0.0.0 zip over a freshly built 0.0.2.x one.
ZIP=$(ls -1t artifacts/*.zip 2>/dev/null | head -n1)
if [ -z "$ZIP" ]; then
    echo "ERROR: plugin-build produced no zip in artifacts/" >&2
    exit 1
fi
echo "    $(basename "$ZIP")"

echo "==> [2/3] installing the zip into the config volume"
PLUGIN_DIR="jf-config/plugins/Jellyfin.Plugin.ThabitTranslator"
mkdir -p "$PLUGIN_DIR"
# unzip -o overwrites the top-level files (dll, deps) but never touches the
# data directories next to them - .venv_thabit/ (1.6 GB), runtime/ (portable
# Python), library/ (versioned, self-invalidating), staging/, home/.
unzip -o -q "$ZIP" -d "$PLUGIN_DIR"
# Upgrade hygiene: the JSONL bridge (pre-CLI-launcher releases) may still be in
# an old volume; the library now travels inside the DLL as embedded *.py.
rm -f "$PLUGIN_DIR/jellyfin_runner.py"
echo "    installed into $PLUGIN_DIR"

echo "==> [3/3] starting the official image (jellyfin/jellyfin:10.11)"
dc pull jellyfin >/dev/null 2>&1 || true   # tolerate offline if it is already local
dc up -d --force-recreate jellyfin

echo "==> waiting for the server on http://localhost:8096"
code=000
for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8096/System/Info/Public || true)
    [ "$code" = 200 ] && break
    sleep 2
done
if [ "$code" != 200 ]; then
    echo "ERROR: server did not answer (HTTP $code); last logs:" >&2
    dc logs --tail 40 jellyfin >&2 || true
    exit 1
fi

echo "==> verifying the installation"
INSTALLED="$PLUGIN_DIR/Jellyfin.Plugin.ThabitTranslator.dll"
if [ ! -f "$INSTALLED" ]; then
    echo "ERROR: $INSTALLED missing - the install step did not place it" >&2
    exit 1
fi

zip_md5=$(unzip -p "$ZIP" Jellyfin.Plugin.ThabitTranslator.dll | md5sum | cut -d' ' -f1)
installed_md5=$(md5sum "$INSTALLED" | cut -d' ' -f1)
if [ "$zip_md5" != "$installed_md5" ]; then
    echo "ERROR: installed DLL differs from $ZIP (stale install?)" >&2
    echo "    zip:       $zip_md5" >&2
    echo "    installed: $installed_md5" >&2
    exit 1
fi
echo "    DLL installed and matches the fresh build ($installed_md5)"

# The server answers /System/Info/Public before plugin loading finishes, so poll
# the log instead of grepping once.
loaded=0
for _ in $(seq 1 30); do
    if dc logs --tail 300 jellyfin 2>/dev/null | grep -q "Loaded plugin: Thabit Translator"; then
        loaded=1
        break
    fi
    sleep 2
done
if [ "$loaded" = 1 ]; then
    echo "    server reports: Loaded plugin: Thabit Translator"
else
    echo "ERROR: server is up but never logged 'Loaded plugin: Thabit Translator'" >&2
    exit 1
fi

echo
echo "OK - the official Jellyfin image is running with the freshly built plugin:"
echo "    http://localhost:8096   (admin: thabit / ThabitTest#2026, if the wizard was completed)"
echo "    config page: http://localhost:8096/web/ConfigurationPage?name=Thabit%20Translator"
