#!/usr/bin/env bash
#
# Packaging test for the PyPI distribution (thabit-translator -> pipx).
#
# Everything happens inside Docker: the wheel is built, twine-checked, installed
# into a container-local pipx and smoke-tested there. The host pipx, venvs and
# Python are never invoked - a stray installation in the user's pipx is exactly
# what this test exists to prevent.
#
# Usage: src/core/test-pipx.sh    (first run downloads the ML stack, ~1.6 GB,
#                                 cached afterwards in the docker volume
#                                 "thabit-pip-cache")
#
set -euo pipefail

cd "$(dirname "$0")"   # core/; the Docker build context is the repo root (..)

# Project rule: packaging tests run with a clean PATH and talk only to docker.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

IMAGE=thabit/pipx-test:local
CACHE_VOL=thabit-pip-cache

echo "[1/2] building the test image (context: src/, credentials excluded)"
docker build -q -f Dockerfile.pipx-test -t "$IMAGE" ..

echo "[2/2] build + install + smoke-test inside the container"
docker run --rm -i -v "$CACHE_VOL":/root/.cache/pip "$IMAGE" bash -s <<'EOF'
set -euo pipefail

cd /work/core   # where pyproject.toml lives

# setuptools refuses paths outside the project root: stage the repo-root
# README/LICENSE next to pyproject.toml (in-container only).
cp /work/README.md /work/LICENSE .

echo "--- python -m build (sdist + wheel)"
python -m build

echo "--- twine check"
twine check dist/*

echo "--- pipx install (container-local, PIPX_HOME=$PIPX_HOME)"
pipx install --force dist/thabit_translator-*.whl

echo "--- console script: thabit-translator resolves via PATH"
resolved=$(command -v thabit-translator) \
  || { echo "FAIL: thabit-translator is not a recognized command"; exit 1; }
[ "$resolved" = "$PIPX_BIN_DIR/thabit-translator" ] \
  || { echo "FAIL: resolves to $resolved, not $PIPX_BIN_DIR/thabit-translator"; exit 1; }
echo "    $resolved"

echo "--- console script: thabit-translator --help"
echo "    (first run bootstraps the CPU-only ML stack into the pipx venv)"
thabit-translator --help

echo "--- module entry: runpy equivalent of python3 -m thabit_translator (no bootstrap)"
python3 -c "import runpy; runpy.run_module('thabit_translator', run_name='probe'); print('runpy entry OK')"

echo "--- first-run config creation in the pipx venv"
/opt/pipx/venvs/thabit-translator/bin/python -c \
  "from thabit_translator.core.config import load_config; p = load_config(None); assert p, 'no config path'; print('config path:', p)"
test -s /root/.config/thabit/thabit_translator.conf \
  || { echo "FAIL: template was not copied to ~/.config/thabit/"; exit 1; }
grep -q '\[opensubtitles\]' /root/.config/thabit/thabit_translator.conf \
  || { echo "FAIL: created config lacks [opensubtitles]"; exit 1; }
echo "created config OK"

echo
echo "PIPELINE-TESTS-OK - sdist, wheel, metadata, console script and module entry verified in Docker"
EOF
