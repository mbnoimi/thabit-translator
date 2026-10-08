#!/usr/bin/env python3
"""Fail the release pipeline when metadata sources disagree.

metadata.conf is the single source of truth (AGENTS.md). Before anything is
built or published, this script asserts that every derived copy still matches:

  * metadata.conf      version / plugin.guid / plugin.target_abi
  * jellyfin-plugin/build.yaml   version / guid / targetAbi
  * core/pyproject.toml          version
  * .../Plugin.cs                PluginGuid
  * git tag vX.Y.Z               (passed via --tag)
  * dist/manifest.json           (passed via --dist): guid, version, checksum
    and sourceUrl of the built plugin zip

Usage:
  validate-release-metadata.py [--tag vX.Y.Z] [--dist DIR]

Exits 0 when everything is in sync, 1 with a list of mismatches otherwise.
"""

import argparse
import configparser
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
errors = []


def fail(msg):
    errors.append(msg)


def read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"cannot read {path}: {exc}")
        return ""


def metadata_values():
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(REPO / "metadata.conf")
        return (
            cfg["project"]["version"].strip(),
            cfg["plugin"]["guid"].strip().lower(),
            cfg["plugin"]["target_abi"].strip(),
        )
    except (configparser.Error, KeyError) as exc:
        fail(f"metadata.conf is malformed: {exc}")
        return ("", "", "")


def yaml_scalar(text, key):
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return None
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def check(label, found, expected):
    if found is None:
        fail(f"{label}: not found (expected {expected!r})")
    elif found != expected:
        fail(f"{label}: {found!r} != metadata.conf {expected!r}")


def check_tracked_confs():
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8", "replace").split("\0")
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f"git ls-files failed: {exc}")
        return
    confs = sorted(p for p in out if p.endswith(".conf"))
    if confs != ["metadata.conf"]:
        fail(
            "tracked *.conf files must be exactly ['metadata.conf'], "
            f"got {confs} - untrack leaked configs first"
        )


def check_tag(tag, version):
    if not re.fullmatch(r"v[0-9][0-9A-Za-z.+-]*", tag):
        fail(f"tag {tag!r} does not look like v<version>")
    elif tag != f"v{version}":
        fail(
            f"tag {tag!r} != metadata.conf version ({f'v{version}'!r}) - "
            "the manifest sourceUrl embeds v<version>, retag or bump metadata.conf"
        )


def check_dist(dist, version, guid):
    manifest_path = dist / "manifest.json"
    if not manifest_path.is_file():
        fail(f"{manifest_path} not found")
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = data[0]
        rel = entry["versions"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        fail(f"{manifest_path} is not a valid plugin manifest: {exc}")
        return

    if entry.get("guid", "").lower() != guid:
        fail(f"manifest guid {entry.get('guid')!r} != metadata.conf {guid!r}")
    if rel.get("version") != version:
        fail(f"manifest version {rel.get('version')!r} != {version!r}")

    zip_name = f"thabit-translator-plugin_{version}.zip"
    zip_path = dist / zip_name
    if not zip_path.is_file():
        fail(f"{zip_path} not found - manifest would ship a dead download link")
        return

    checksum = hashlib.md5(zip_path.read_bytes()).hexdigest()
    if rel.get("checksum") != checksum:
        fail(
            f"manifest checksum {rel.get('checksum')!r} != md5({zip_name}) "
            f"{checksum!r} - Jellyfin would refuse the zip"
        )

    source_url = rel.get("sourceUrl", "")
    expected_tail = f"/releases/download/v{version}/{zip_name}"
    if not source_url.endswith(expected_tail):
        fail(
            f"manifest sourceUrl {source_url!r} does not end with "
            f"{expected_tail!r} - release assets would 404"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="git tag being released, e.g. v1.0.0.0")
    parser.add_argument("--dist", type=Path, help="directory with built artifacts")
    args = parser.parse_args()

    version, guid, target_abi = metadata_values()
    if not version:
        fail("metadata.conf: [project] version is empty")
    if not GUID_RE.match(guid):
        fail(f"metadata.conf: plugin.guid {guid!r} is not a lowercase UUID")

    build_yaml = read(REPO / "jellyfin-plugin" / "build.yaml")
    check("build.yaml version", yaml_scalar(build_yaml, "version"), version)
    check(
        "build.yaml guid",
        (yaml_scalar(build_yaml, "guid") or "").lower() or None,
        guid,
    )
    check("build.yaml targetAbi", yaml_scalar(build_yaml, "targetAbi"), target_abi)

    pyproject = read(REPO / "core" / "pyproject.toml")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    check("core/pyproject.toml version", m.group(1) if m else None, version)

    plugin_cs = read(
        REPO / "jellyfin-plugin" / "src"
        / "Jellyfin.Plugin.ThabitTranslator" / "Plugin.cs"
    )
    m = re.search(r'PluginGuid\s*=\s*new(?:\s+Guid)?\s*\(\s*"([0-9a-fA-F-]+)"', plugin_cs)
    check("Plugin.cs PluginGuid", m.group(1).lower() if m else None, guid)

    check_tracked_confs()

    if args.tag:
        check_tag(args.tag, version)
    if args.dist:
        check_dist(args.dist, version, guid)

    if errors:
        print(f"release metadata validation FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1
    print(f"release metadata OK (version {version}, guid {guid}, targetAbi {target_abi})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
