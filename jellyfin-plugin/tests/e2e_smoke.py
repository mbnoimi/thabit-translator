#!/usr/bin/env python3
"""End-to-end smoke test for the Thabit Translator Jellyfin plugin.

Talks to a running Jellyfin (the one from src/jellyfin-plugin/docker-compose.yml):
creates the admin account, builds a library out of ``media-test``, searches for
subtitles through the plugin's provider and downloads one - which makes the
plugin run the real Python pipeline inside the container and lets Jellyfin save
``<stem>.<lang>.srt`` next to the video.

    python3 tests/e2e_smoke.py [http://127.0.0.1:8096]

Only stdlib is used. The download step needs the embedded English subtitle that
``media-test/Test Clip (2026)`` ships with, so it never touches a provider's
download quota.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8096"
USERNAME = "thabit"
PASSWORD = "ThabitTest#2026"
LIBRARY_NAME = "Thabit Test"
MEDIA_PATH = "/media"
TARGET_LANG = "ar"
CLIP_NAME = "Test Clip (2026)"
CLIP_SRT = f"{MEDIA_PATH}/{CLIP_NAME}/{CLIP_NAME}.{TARGET_LANG}.srt"

AUTH_HEADER = (
    'MediaBrowser Client="ThabitE2E", DeviceId="thabit-e2e-1", '
    'Device="script", Version="1.0.0"'
)


def call(method, path, *, token=None, body=None, headers=None, timeout=30, expect=None):
    url = BASE + path
    data = None
    if token:
        request_headers = {
            "Authorization": f'MediaBrowser Token="{token}", {AUTH_HEADER.split(" ", 1)[1]}',
            "X-Emby-Token": token,
        }
    else:
        request_headers = {"Authorization": AUTH_HEADER}
    request_headers.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read()
        status = error.code

    if expect is not None and status != expect:
        raise SystemExit(
            f"{method} {path} -> {status} (expected {expect}): {payload[:500]!r}"
        )

    if payload and payload[:1] in (b"{", b"["):
        return status, json.loads(payload)
    return status, payload


def step(message):
    print(f"\n== {message}", flush=True)


def wait_for_server():
    step("waiting for Jellyfin")
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            status, payload = call("GET", "/System/Info/Public", timeout=5)
            if status == 200:
                return payload
        except (OSError, SystemExit):
            pass
        time.sleep(2)
    raise SystemExit("Jellyfin did not come up")


def complete_wizard(public_info):
    if public_info.get("StartupWizardCompleted"):
        step("startup wizard")
        print("    already completed")
        return

    step("startup wizard")
    _, first_user = call("GET", "/Startup/User", expect=200)
    print(f"    first user: {first_user.get('Name')}")
    call("POST", "/Startup/User", body={"Name": USERNAME, "Password": PASSWORD}, expect=204)
    call(
        "POST",
        "/Startup/Configuration",
        body={
            "ServerName": "thabit-e2e",
            "UICulture": "en-us",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
        },
        expect=204,
    )
    call("POST", "/Startup/Complete", expect=204)


def authenticate():
    step("login")
    status, payload = call(
        "POST",
        "/Users/AuthenticateByName",
        body={"Username": USERNAME, "Pw": PASSWORD},
        expect=200,
    )
    token = payload["AccessToken"]
    print(f"    token acquired ({len(token)} chars)")
    return token


def ensure_library(token):
    step("library")
    _, folders = call("GET", "/Library/VirtualFolders", token=token, expect=200)
    names = [folder.get("Name") for folder in folders]
    if LIBRARY_NAME not in names:
        call(
            "POST",
            "/Library/VirtualFolders"
            f"?name={urllib.parse.quote(LIBRARY_NAME)}&collectionType=tvshows"
            f"&paths={urllib.parse.quote(MEDIA_PATH)}&refreshLibrary=false",
            token=token,
            body={},
            expect=204,
        )
        print(f"    created library {LIBRARY_NAME!r} -> {MEDIA_PATH}")

    # SaveSubtitlesWithMedia defaults to true, which is what puts the produced file
    # next to the video under the name this project promises (<stem>.<lang>.srt).
    call("POST", "/Library/Refresh?Recursive=true&MetadataRefreshMode=Default", token=token, body={}, expect=204)


def find_clip(token):
    step("scanning for the test clip")
    query = urllib.parse.urlencode(
        {
            "Recursive": "true",
            "IncludeItemTypes": "Episode,Movie",
            "Fields": "Path",
        }
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        _, page = call("GET", f"/Items?{query}", token=token, expect=200)
        matches = [
            item
            for item in page.get("Items", [])
            if (item.get("Path") or "").startswith(f"{MEDIA_PATH}/{CLIP_NAME}/")
        ]
        if matches:
            item = matches[0]
            print(f"    found {item.get('Name')} ({item.get('Id')}) at {item.get('Path')}")
            return item
        time.sleep(3)
    raise SystemExit(f"{CLIP_NAME} never showed up in the library")


def search_and_download(token, item):
    step("subtitle search")
    _, results = call(
        "GET",
        f"/Items/{item['Id']}/RemoteSearch/Subtitles/{TARGET_LANG}",
        token=token,
        expect=200,
    )
    ours = [r for r in results if r.get("ProviderName") == "Thabit Translator"]
    print(f"    {len(results)} result(s), {len(ours)} from Thabit Translator")
    for result in ours:
        print(f"      - {result.get('Name')} (id {result.get('Id')[:24]}...)")

    if not ours:
        raise SystemExit("the plugin provider returned nothing to download")

    subtitle_id = ours[0]["Id"]
    step("download (runs the Python pipeline; first run downloads an Argos pack)")
    call(
        "POST",
        f"/Items/{item['Id']}/RemoteSearch/Subtitles/{urllib.parse.quote(subtitle_id)}",
        token=token,
        expect=204,
        timeout=900,
    )
    print("    Jellyfin accepted the subtitle")


def verify(token, item):
    step("verification")
    plugin_root = Path(__file__).resolve().parents[1]
    media_dir = plugin_root / "media-test" / CLIP_NAME
    produced = media_dir / f"{CLIP_NAME}.{TARGET_LANG}.srt"

    if not produced.is_file() or produced.stat().st_size == 0:
        raise SystemExit(f"missing or empty: {produced}")

    print(f"    {produced.name}: {produced.stat().st_size} bytes")
    print("      " + produced.read_text(encoding="utf-8").splitlines()[2][:70])

    duplicates = sorted(
        p.name for p in media_dir.iterdir()
        if p.name != produced.name and p.suffix in {".srt", ".ass"}
    )
    if duplicates:
        raise SystemExit(f"unexpected extra subtitle files in {media_dir}: {duplicates}")
    print("    no duplicate subtitle files next to the video")

    _, results = call(
        "GET",
        f"/Items/{item['Id']}/RemoteSearch/Subtitles/{TARGET_LANG}",
        token=token,
        expect=200,
    )
    ours = [r for r in results if r.get("ProviderName") == "Thabit Translator"]
    if ours:
        raise SystemExit("provider still offers a download for a language that exists")
    print("    provider hides the language it already produced (second download impossible)")

    staging = plugin_root / "jf-config" / "plugins" / "Jellyfin.Plugin.ThabitTranslator" / "staging"
    leftovers = [p for p in staging.rglob("*")] if staging.is_dir() else []
    if leftovers:
        raise SystemExit(f"staging was not cleaned up: {leftovers}")
    print("    staging folder is clean")

    _, status_payload = call("GET", "/ThabitTranslator/status", token=token, expect=200)
    print(f"    plugin status: {json.dumps(status_payload)[:400]}")

    print(f"\nOK - {produced}")


def main():
    public_info = wait_for_server()
    complete_wizard(public_info)
    token = authenticate()
    ensure_library(token)
    item = find_clip(token)
    search_and_download(token, item)
    verify(token, item)


if __name__ == "__main__":
    main()
