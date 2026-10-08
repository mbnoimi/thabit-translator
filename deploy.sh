#!/bin/bash
#
# Thabit Translator - prepare every release artifact into <repo>/dist/.
#
# metadata.conf is the ONE file you edit before a release; this script then:
#
#   1. stamps jellyfin-plugin/build.yaml (version, guid, targetAbi, catalog fields)
#   2. stamps core/pyproject.toml        (version, description, authors, license, urls)
#   3. plugin zip       dist/thabit-translator-plugin_<version>.zip
#   4. plugin manifest  dist/manifest.json (GitHub plugin repository)
#   5. pipx artifacts   dist/thabit_translator-<version>.*.whl + .tar.gz
#      (built in Docker and twine-checked; push tag v<version> and
#       .github/workflows/release.yml publishes everything automatically)
#
# Stamping is idempotent: a re-run with unchanged metadata rewrites nothing.
#
# Usage: ./deploy.sh

set -euo pipefail

# A single unreadable directory in $PATH makes every PATH lookup fail with
# "Input/output error" (`env`, `find -exec`, ...), so drop the offenders first.
# /usr/bin/ls is used by absolute path: the lookup for `ls` itself would fail.
sanitize_path() {
    local dir safe="" IFS=':'
    for dir in $PATH; do
        [ -n "$dir" ] || continue
        if [ -d "$dir" ] && /usr/bin/ls -f "$dir" >/dev/null 2>&1; then
            safe="$safe${safe:+:}$dir"
        elif [ -d "$dir" ]; then
            printf 'deploy.sh: dropping unreadable PATH entry: %s\n' "$dir" >&2
        fi
    done
    PATH="${safe:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
    export PATH
}
sanitize_path

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SRC_DIR")"
PLUGIN_DIR="$SRC_DIR/jellyfin-plugin"
CORE_DIR="$SRC_DIR/core"
META_FILE="$SRC_DIR/metadata.conf"
BUILD_YAML="$PLUGIN_DIR/build.yaml"
PYPROJECT="$CORE_DIR/pyproject.toml"
DIST_DIR="${DIST_DIR:-$REPO_DIR/dist}"

log()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# meta_get <section> <key> - one value per line from metadata.conf
meta_get() {
    awk -v s="[$1]" -v k="$2" '
        /^\[/ { sec = $0; next }
        sec != s { next }
        /^[[:space:]]*(#|$)/ { next }
        {
            eq = index($0, "=")
            if (!eq) next
            key = substr($0, 1, eq - 1)
            gsub(/[[:space:]]/, "", key)
            if (key != k) next
            val = substr($0, eq + 1)
            sub(/^[[:space:]]+/, "", val)
            sub(/[[:space:]]+$/, "", val)
            print val
            exit
        }
    ' "$META_FILE"
}

usage() {
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        -h|--help)   usage ;;
        *)           die "unknown option: $arg (try --help)" ;;
    esac
done

[ -f "$META_FILE" ]   || die "missing metadata file: $META_FILE"
[ -f "$BUILD_YAML" ]  || die "missing $BUILD_YAML"
[ -f "$PYPROJECT" ]   || die "missing $PYPROJECT"

VERSION="$(meta_get project version)"
[ -n "$VERSION" ] || die "project.version missing in $META_FILE"

# ----------------------------------------------------------------- stamps
# metadata.conf is master: rewrite build.yaml / pyproject.toml to match it.
# Only files that actually change are written, so re-running deploy.sh with
# unchanged metadata leaves git clean.

stamp_build_yaml() {
    log "stamping jellyfin-plugin/build.yaml from metadata.conf"
    META_FILE="$META_FILE" BUILD_YAML="$BUILD_YAML" python3 - <<'PY'
import configparser, json, os, re, sys

meta = configparser.ConfigParser(interpolation=None)
meta.read(os.environ["META_FILE"])
project, plugin = meta["project"], meta["plugin"]
path = os.environ["BUILD_YAML"]
with open(path, encoding="utf-8") as fh:
    old = fh.read()
lines = old.splitlines()

version = project["version"]

def scalar_line(key, value):
    # Quote only when the plain form would be ambiguous YAML.
    unsafe = (
        ": " in value
        or value.endswith(":")
        or re.match(r"^[-?:,\[\]{}#&*!|>%@`']", value) is not None
    )
    return f"{key}: {json.dumps(value) if unsafe else value}"

def split(value):
    return [" ".join(part.split()) for part in value.split("|") if part.strip()]

def block_lines(key, segments):
    return key + ": >\n" + "\n".join("  " + s for s in segments)

scalars = {
    "name": plugin["name"],
    "guid": plugin["guid"],
    "version": version,
    "targetAbi": plugin["target_abi"],
    "overview": plugin["overview"],
    "owner": plugin["owner"],
    "category": plugin["category"],
}
changelog = split(plugin["changelog"])
blocks = {
    "description": split(plugin["description_long"]),
    "changelog": [version + ": " + changelog[0]] + changelog[1:],
}

out, found = [], set()
key_re = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(?:[ \t]|$)")
i = 0
while i < len(lines):
    line = lines[i]
    m = key_re.match(line)
    if m and (m.group(1) in scalars or m.group(1) in blocks):
        key = m.group(1)
        found.add(key)
        if key in scalars:
            out.append(scalar_line(key, scalars[key]))
            i += 1
        else:
            out.append(block_lines(key, blocks[key]))
            i += 1
            while i < len(lines) and lines[i][:1] in " \t" and lines[i].strip():
                i += 1
        continue
    out.append(line)
    i += 1

for key, value in scalars.items():
    if key not in found:
        out.append(scalar_line(key, value))
for key, segments in blocks.items():
    if key not in found:
        out.append(block_lines(key, segments))

new = "\n".join(out) + "\n"
if new == old:
    print("    build.yaml: up to date")
else:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print("    build.yaml: stamped")
PY
}

stamp_pyproject() {
    log "stamping core/pyproject.toml from metadata.conf"
    META_FILE="$META_FILE" PYPROJECT="$PYPROJECT" python3 - <<'PY'
import configparser, json, os, re, sys

meta = configparser.ConfigParser(interpolation=None)
meta.read(os.environ["META_FILE"])
project = meta["project"]
path = os.environ["PYPROJECT"]
with open(path, encoding="utf-8") as fh:
    old = fh.read()

homepage = project["homepage"].rstrip("/")
# (pattern, replacement, human name)
repls = [
    (r'^version = ".*"$', "version = " + json.dumps(project["version"]), "version"),
    (r'^description = ".*"$', "description = " + json.dumps(project["summary"]), "description"),
    (r'^license = ".*"$', "license = " + json.dumps(project["license"]), "license"),
    (r"^authors = \[.*\]$",
     "authors = [{ name = %s, email = %s }]"
     % (json.dumps(project["maintainer"]), json.dumps(project["email"])),
     "authors"),
    (r'^Homepage = ".*"$', "Homepage = " + json.dumps(project["homepage"]), "Homepage"),
    (r'^Issues = ".*"$', "Issues = " + json.dumps(homepage + "/issues"), "Issues"),
]

new = old
missing = []
for pattern, value, name in repls:
    rx = re.compile(pattern, re.M)
    if not rx.search(new):
        missing.append(name)
        continue
    new = rx.sub(lambda _m, v=value: v, new)

if missing:
    sys.exit("pyproject.toml fields not found: " + ", ".join(missing))
if new == old:
    print("    pyproject.toml: up to date")
else:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print("    pyproject.toml: stamped")
PY
}

# -------------------------------------------------------------- plugin zip
build_plugin() {
    log "building the Jellyfin plugin zip (docker compose plugin-build)"
    (cd "$PLUGIN_DIR" && env UID="$(id -u)" GID="$(id -g)" docker compose run --rm plugin-build) \
        || die "plugin build failed"

    local zip
    zip="$(ls -t "$PLUGIN_DIR"/artifacts/*.zip 2>/dev/null | head -1)"
    [ -n "$zip" ] || die "no zip produced in $PLUGIN_DIR/artifacts"

    unzip -l "$zip" | grep -q 'Jellyfin.Plugin.ThabitTranslator.dll' || die "$zip has no plugin dll"
    unzip -l "$zip" | grep -q 'meta.json' || die "$zip has no meta.json"

    # Credential guard: the zip carries only the DLL and meta.json, never a
    # provider config (src/core/thabit_translator.conf holds real keys).
    if unzip -l "$zip" | grep -q '\.conf'; then
        die "$zip contains a .conf file - credentials must never be packaged"
    fi
    if [ -f "$CORE_DIR/thabit_translator.conf" ] && command -v git >/dev/null 2>&1 \
        && git -C "$SRC_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        && [ -n "$(git -C "$SRC_DIR" ls-files -- core/thabit_translator.conf)" ]; then
        die "core/thabit_translator.conf is tracked by git - untrack it first"
    fi

    cp -f "$zip" "$DIST_DIR/thabit-translator-plugin_${VERSION}.zip"
    printf '    %s\n' "$DIST_DIR/thabit-translator-plugin_${VERSION}.zip"
}

# dist/manifest.json is what gets committed to the GitHub plugin repository
# (release checklist). It is regenerated on every run so its "checksum" always
# matches the zip sitting next to it.
write_manifest() {
    local zip="$DIST_DIR/thabit-translator-plugin_${VERSION}.zip"
    if [ ! -f "$zip" ]; then
        log "no plugin zip in $DIST_DIR - skipping manifest.json"
        return 0
    fi

    local checksum
    checksum="$(md5sum "$zip" | cut -d' ' -f1)"
    META_FILE="$META_FILE" VERSION="$VERSION" ZIP="$(basename "$zip")" \
    CHECKSUM="$checksum" OUT="$DIST_DIR/manifest.json" python3 - <<'PY'
import configparser, datetime, json, os

cfg = configparser.ConfigParser(interpolation=None)
cfg.read(os.environ["META_FILE"])
project, plugin = cfg["project"], cfg["plugin"]

def as_text(value):
    return " ".join(part.strip() for part in value.split("|"))

manifest = [
    {
        "guid": plugin["guid"],
        "name": plugin["name"],
        "owner": plugin["owner"],
        "category": plugin["category"],
        "overview": plugin["overview"],
        "description": as_text(plugin["description_long"]),
        "versions": [
            {
                "version": os.environ["VERSION"],
                "targetAbi": plugin["target_abi"],
                "changelog": as_text(plugin["changelog"]),
                "sourceUrl": "{}/releases/download/v{}/{}".format(
                    project["homepage"].rstrip("/"),
                    os.environ["VERSION"],
                    os.environ["ZIP"],
                ),
                "checksum": os.environ["CHECKSUM"],
                "timestamp": datetime.datetime.now().replace(microsecond=0).isoformat(),
            }
        ],
    }
]

with open(os.environ["OUT"], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
PY
    printf '    %s\n' "$DIST_DIR/manifest.json"
}

# ------------------------------------------------------------- pipx track
# The wheel/sdist are built inside the packaging image (host Python and pipx
# are never invoked) and land next to the plugin zip in $DIST_DIR.
build_pipx() {
    log "building the pipx sdist + wheel (Docker, host Python untouched)"
    docker build -q -f "$CORE_DIR/Dockerfile.pipx-test" -t thabit/pipx-build:local "$SRC_DIR" \
        || die "packaging image build failed"

    docker run --rm \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        -v "$DIST_DIR":/dist \
        -v thabit-pip-cache:/root/.cache/pip \
        thabit/pipx-build:local \
        bash -c 'set -euo pipefail
                 cd /work/core
                 # setuptools refuses paths outside the project root: stage the
                 # repo-root README/LICENSE next to pyproject.toml (in-container
                 # only - the host checkout never sees these copies).
                 cp /work/README.md /work/LICENSE .
                 rm -rf dist ./*.egg-info
                 python -m build --outdir /dist
                 twine check /dist/*.whl /dist/*.tar.gz
                 chown -R "$HOST_UID:$HOST_GID" /dist' \
        || die "pipx artifact build failed"

    printf '    %s\n' "$DIST_DIR"/thabit_translator-*.whl "$DIST_DIR"/thabit_translator-*.tar.gz
}

# ------------------------------------------------------------------- main
mkdir -p "$DIST_DIR"
log "version $VERSION -> $DIST_DIR"

stamp_build_yaml
stamp_pyproject
build_plugin
write_manifest
build_pipx

log "done - $DIST_DIR:"
ls -la "$DIST_DIR"
echo
echo "md5 checksums:"
(cd "$DIST_DIR" && md5sum -- *.zip thabit_translator-*.whl thabit_translator-*.tar.gz manifest.json | sed 's/^/  /')
cat <<EOF

Manual Jellyfin plugin install (no plugin repository):
  scp $DIST_DIR/thabit-translator-plugin_${VERSION}.zip jellyfin@server:
  ssh jellyfin@server
  mkdir -p /config/plugins/Jellyfin.Plugin.ThabitTranslator
  unzip -o thabit-translator-plugin_${VERSION}.zip \\
        -d /config/plugins/Jellyfin.Plugin.ThabitTranslator
  restart Jellyfin -> Dashboard -> Plugins -> Thabit Translator
  (the server also needs python3 + ffmpeg - the container image in
   jellyfin-plugin/ ships both; see README.md)

Plugin repository (GitHub):
  push tag v$VERSION - the release workflow uploads dist/* to the GitHub
  release and commits dist/manifest.json to main automatically

PyPI upload:
  handled by the release workflow (PYPI_API_TOKEN secret); manual fallback:
  twine upload $DIST_DIR/thabit_translator-*.whl $DIST_DIR/thabit_translator-*.tar.gz
  pipx install thabit-translator
EOF
