#!/usr/bin/env python3
"""
OpenSubtitles Provider - Fixed Authentication Flow
Downloads subtitles from opensubtitles.com using proper OAuth2-style auth
"""
import os
import sys
import json
import time
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from core.config import extract_imdb_id
from core.match import is_relevant, episode_key, show_title, feature_episode, NO_MATCH
from providers.limits import block, is_blocked, wait_then_retry

# The only provider with a real file-hash lookup (the others match on title).
SUPPORTS_HASH = True

# Constants
API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "ThabitTranslator v1.0"
TOKEN_CACHE_FILE = os.path.expanduser("~/.cache/thabit_translator/opensubtitles_token.json")

# Set from the HTTP 406 body (daily download quota spent) and honoured until
# OpenSubtitles' own reset_time, so a long batch re-enables the provider without
# a restart.
_QUOTA_UNTIL = None  # datetime (UTC) or None
_QUOTA_LABEL = ""     # human reset time, e.g. "Sun 02:59 +03"


def quota_exhausted() -> bool:
    """True while OpenSubtitles' daily download quota is spent."""
    global _QUOTA_UNTIL
    if _QUOTA_UNTIL is None:
        return False
    if datetime.now(timezone.utc) >= _QUOTA_UNTIL:
        print("[INFO] OpenSubtitles: daily download limit reset - provider re-enabled")
        _QUOTA_UNTIL = None
        return False
    return True


def quota_reset_label() -> str:
    """When the quota resets ("Sun 02:59 +03"), or "" while the quota is fine."""
    if _QUOTA_UNTIL is None:
        return ""
    return _QUOTA_LABEL or "in up to 24h"


def _mark_quota_exhausted(resp):
    global _QUOTA_UNTIL, _QUOTA_LABEL
    if quota_exhausted():
        return
    try:
        data = resp.json() or {}
    except Exception:
        data = {}
    reset_utc = data.get("reset_time_utc")
    if reset_utc:
        try:
            when = datetime.fromisoformat(str(reset_utc).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            _QUOTA_UNTIL = when
            _QUOTA_LABEL = when.astimezone().strftime("%a %H:%M %Z")
        except ValueError:
            pass
    if _QUOTA_UNTIL is None:
        _QUOTA_UNTIL = datetime.now(timezone.utc) + timedelta(hours=24)
        _QUOTA_LABEL = "in up to 24h"
    print(
        f"[WARN] OpenSubtitles: your account hit its daily download limit (20/day), "
        f"reset {_QUOTA_LABEL} - skipping this provider until then"
    )

def _ensure_cache_dir():
    """Ensure cache directory exists."""
    cache_dir = os.path.dirname(TOKEN_CACHE_FILE)
    os.makedirs(cache_dir, exist_ok=True)

def _load_token(config, verbose=False):
    """Load cached JWT token if still valid."""
    _ensure_cache_dir()
    if not os.path.exists(TOKEN_CACHE_FILE):
        return None, None
    
    try:
        with open(TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Check if token is still valid (24h expiry)
        if data.get('expires_at', 0) > time.time():
            # Verify API key matches (in case user changed it)
            api_key = config['opensubtitles'].get('api_key', '').strip().strip('"\'')
            if data.get('api_key_hash') == hashlib.sha256(api_key.encode()).hexdigest():
                return data['token'], data.get('base_url', 'api.opensubtitles.com')
    except Exception:
        if verbose:
            print(f"[WARN] Could not load cached token")
    return None, None

def _save_token(token, base_url, api_key, verbose=False):
    """Save JWT token with 24h expiry."""
    _ensure_cache_dir()
    data = {
        'token': token,
        'base_url': base_url,
        'expires_at': time.time() + 86400,  # 24 hours
        'api_key_hash': hashlib.sha256(api_key.encode()).hexdigest(),
    }
    try:
        with open(TOKEN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        if verbose:
            print(f"[WARN] Could not cache token: {e}")

def _login(config, verbose=False):
    """Authenticate user and get JWT token."""
    api_key = config['opensubtitles'].get('api_key', '').strip().strip('"\'')
    username = config['opensubtitles'].get('username', '').strip()
    password = config['opensubtitles'].get('password', '').strip()
    
    if not api_key:
        if verbose:
            print("[WARN] OpenSubtitles: No API key configured")
        return None, None
    
    # Username/password are now MANDATORY for downloads
    if not username or not password:
        if verbose:
            print("[WARN] OpenSubtitles: Username and password are required for downloads")
        return None, None
    
    headers = {
        'Api-Key': api_key,
        'User-Agent': USER_AGENT,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    
    payload = {
        'username': username,
        'password': password,
    }
    
    try:
        if verbose:
            print("-> OpenSubtitles: authenticating user...")
        
        resp = requests.post(
            f"{API_BASE}/login",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if resp.status_code == 401:
            if verbose:
                print("[WARN] OpenSubtitles: Authentication failed (401) - check username/password")
            return None, None
        elif resp.status_code == 403:
            if verbose:
                print(f"[WARN] OpenSubtitles: Forbidden (403) - {resp.text[:200]}")
            return None, None
        elif resp.status_code != 200:
            if verbose:
                print(f"[WARN] OpenSubtitles: Login failed {resp.status_code}: {resp.text[:200]}")
            return None, None
        
        data = resp.json()
        token = data.get('token')
        base_url = data.get('base_url', 'api.opensubtitles.com')
        
        if token:
            _save_token(token, base_url, api_key, verbose)
            if verbose:
                print(f"-> OpenSubtitles: authenticated successfully, downloads allowed: {data.get('user', {}).get('allowed_downloads', '?')}")
            return token, base_url
        return None, None
        
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] OpenSubtitles: Connection error during login: {e}")
        return None, None
    except json.JSONDecodeError as e:
        if verbose:
            print(f"[WARN] OpenSubtitles: Invalid JSON response: {e}")
        return None, None

def _get_headers(config, token=None):
    """Build request headers with proper auth."""
    api_key = config['opensubtitles'].get('api_key', '').strip().strip('"\'')
    headers = {
        'Api-Key': api_key,
        'User-Agent': USER_AGENT,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers

def _calculate_file_hash(filepath):
    """Calculate OpenSubtitles-compatible hash for video file."""
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 65536:
            return None
        
        with open(filepath, 'rb') as f:
            chunk_size = 65536
            first_chunk = f.read(chunk_size)
            f.seek(-chunk_size, os.SEEK_END)
            last_chunk = f.read(chunk_size)
        
        data = first_chunk + last_chunk
        return hashlib.md5(data).hexdigest()
    except Exception:
        return None

def _format_subtitle_info(sub, index):
    """Format subtitle metadata for user display."""
    attrs = sub.get('attributes', {})
    lang = attrs.get('language', 'Unknown')
    release = attrs.get('release', 'Unknown')
    rating = attrs.get('rating', attrs.get('score', 'N/A'))
    fps = attrs.get('fps', 'N/A')
    files = attrs.get('files', [])
    file_id = files[0].get('file_id') if files else None
    return f"{index + 1}. [{lang}] {release}\n   ★ {rating} | FPS: {fps} | ID: {file_id}", file_id


_SERIES_IDS = {}  # "monk" -> parent IMDb id as str, or None when it could not be found


def _api_get(url, params, headers, config, verbose):
    """GET with one re-auth attempt on 401. Returns (resp, headers); resp may be None."""
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 401:
            if verbose:
                print("-> OpenSubtitles: token expired, re-authenticating...")
            token, base_url = _login(config, verbose)
            if not token:
                return None, headers
            headers = _get_headers(config, token)
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        return resp, headers
    except requests.exceptions.RequestException as exc:
        if verbose:
            print(f"[WARN] OpenSubtitles request failed: {exc}")
        return None, headers


def _resolve_series_id(show, url, headers, config, verbose):
    """Find a series' parent IMDb id - text search never matches 'Monk - S01E12'.

    Results are cached per show name, so a 124-episode folder pays for this once.
    """
    if not show:
        return None
    cache_key = show.lower()
    if cache_key in _SERIES_IDS:
        return _SERIES_IDS[cache_key]

    resp, _ = _api_get(url, {'query': show, 'type': 'episode'}, headers, config, verbose)
    series_id = None
    if resp is not None and resp.status_code == 200:
        for item in resp.json().get('data', []):
            fd = (item.get('attributes') or {}).get('feature_details') or {}
            parent = fd.get('parent_imdb_id')
            if parent:
                series_id = str(parent).replace('tt', '')
                break
    _SERIES_IDS[cache_key] = series_id
    if verbose:
        if series_id:
            print(f"-> OpenSubtitles: series {show!r} -> tt{series_id}")
        else:
            print(f"-> OpenSubtitles: no series id for {show!r}, falling back to text search")
    return series_id


def download_opensubtitles(video_path, config, language, query=None, use_hash=True,
                          select_index=None, auto_select=True, interactive=True):
    """Download subtitle from OpenSubtitles.com with proper authentication.
    
    Args:
        select_index: int or None - 0-based index to auto-select, or None for interactive
        auto_select: bool - if True and multiple results, auto-pick best match by rating
        interactive: bool - if False, never ask on stdin (pick option 1 / give up)
    """
    verbose = config['settings'].get('verbose', False)
    api_key = config['opensubtitles'].get('api_key', '').strip().strip('"\'')
    
    if not api_key:
        block("opensubtitles", "no API key configured (see [opensubtitles] api_key)", kind="config")
        return NO_MATCH

    if quota_exhausted():
        return None

    if is_blocked("opensubtitles"):
        return None
    
    video_basename = os.path.basename(video_path)
    
    # Step 1: Get or refresh JWT token
    token, base_url = _load_token(config, verbose)
    if not token:
        if not config['opensubtitles'].get('username', '').strip() or not config['opensubtitles'].get('password', '').strip():
            block(
                "opensubtitles",
                "no username/password configured - downloads need both "
                "(see [opensubtitles] username/password)",
                kind="config",
            )
            return NO_MATCH
        token, base_url = _login(config, verbose)
        if not token:
            return NO_MATCH
    
    api_host = base_url or 'api.opensubtitles.com'
    
    # Step 2: Build search parameters. Text search never matches "Monk - S01E12",
    # so episodes go through the series' IMDb id, then widen to the whole season
    # (OpenSubtitles sometimes files "Monk.S01E12..." under episode 13).
    headers = _get_headers(config, token)
    search_url = f"https://{api_host}/api/v1/subtitles"
    search_key = query or os.path.splitext(video_basename)[0]
    want = episode_key(search_key)
    fallback_params = None
    search_params = {'languages': language}

    if use_hash:
        file_hash = _calculate_file_hash(video_path)
        if file_hash:
            search_params['moviehash'] = file_hash
            if verbose:
                print("-> OpenSubtitles: searching by file hash")

    if 'moviehash' not in search_params:
        series_id = None
        if want:
            series_id = _resolve_series_id(
                show_title(search_key), search_url, headers, config, verbose
            )

        if series_id:
            search_params = {
                'languages': language,
                'parent_imdb_id': series_id,
                'season_number': want[0],
                'episode_number': want[1],
            }
            fallback_params = {
                'languages': language,
                'parent_imdb_id': series_id,
                'season_number': want[0],
            }
            if verbose:
                print(
                    f"-> OpenSubtitles: searching {show_title(search_key)!r} "
                    f"S{want[0]:02d}E{want[1]:02d} (series tt{series_id})"
                )
        elif query:
            search_params['query'] = query
            if verbose:
                print(f"-> OpenSubtitles: searching by query '{query}'")
        else:
            imdb_id = extract_imdb_id(video_basename)
            if imdb_id:
                search_params['imdb_id'] = imdb_id.replace('tt', '')
                if verbose:
                    print(f"-> OpenSubtitles: searching by IMDB ID {imdb_id}")
            else:
                search_params['query'] = search_key
                if verbose:
                    print(f"-> OpenSubtitles: searching by filename '{search_key}'")

    # Step 3: Search for subtitles
    try:
        attempts = [search_params] + ([fallback_params] if fallback_params else [])
        subtitles = []
        by_hash = 'moviehash' in search_params

        for attempt, params in enumerate(attempts):
            if verbose:
                print(f"-> OpenSubtitles: GET {search_url} params={params}")

            resp, headers = _api_get(search_url, params, headers, config, verbose)
            if resp is None:
                return None

            if resp.status_code == 429 and wait_then_retry(resp):
                resp, headers = _api_get(search_url, params, headers, config, verbose)
                if resp is None:
                    return None

            if resp.status_code == 429:
                block(
                    "opensubtitles",
                    "the OpenSubtitles API is rate limiting this key",
                    resp,
                )
                return None

            if resp.status_code != 200:
                if verbose:
                    print(f"[WARN] OpenSubtitles search failed: {resp.status_code} - {resp.text[:200]}")
                return None

            subtitles = resp.json().get('data', [])
            by_hash = 'moviehash' in params
            if subtitles:
                break
            if attempt + 1 < len(attempts) and verbose:
                print("-> OpenSubtitles: exact episode lookup was empty, widening to the whole season")
        
        if not subtitles:
            if verbose:
                print("[WARN] OpenSubtitles: No subtitles found")
            return NO_MATCH
        
        # Step 4: Collect all candidates with file IDs
        all_candidates = []
        for sub in subtitles:
            attrs = sub.get('attributes', {})
            files = attrs.get('files', [])
            if not files:
                continue
            file_id = files[0].get('file_id')
            if file_id:
                all_candidates.append((sub, file_id))

        # Hard-reject results that are clearly another episode/title
        kept = []
        for item in all_candidates:
            attrs = item[0].get('attributes', {}) or {}
            name = attrs.get('release') or attrs.get('title') or ''
            if is_relevant(
                name, search_key, by_hash,
                feature_episode(attrs.get('feature_details')),
            ):
                kept.append(item)
        if verbose and len(kept) < len(all_candidates):
            print(
                f"[WARN] OpenSubtitles: dropped {len(all_candidates) - len(kept)} "
                f"result(s) for a different episode"
            )
        all_candidates = kept

        if not all_candidates:
            if verbose:
                print("[WARN] OpenSubtitles: No downloadable subtitles found")
            return NO_MATCH

        # === SUBTITLE SELECTION LOGIC ===
        selected = None

        if select_index is not None and 0 <= select_index < len(all_candidates):
            selected = all_candidates[select_index]
            if verbose:
                print(f"-> Selected subtitle #{select_index + 1} by index")

        elif auto_select and len(all_candidates) > 1:
            scored = []
            for sub, file_id in all_candidates:
                attrs = sub.get('attributes', {})
                try:
                    rating = float(attrs.get('rating', 0) or 0)
                except (ValueError, TypeError):
                    rating = 0
                downloads = int(attrs.get('download_count', 0) or 0)
                score = rating * 10 + downloads
                scored.append((score, sub, file_id))
            scored.sort(key=lambda x: x[0], reverse=True)
            selected = (scored[0][1], scored[0][2])
            if verbose:
                print(f"-> Auto-selected best match (score: {scored[0][0]})")

        elif len(all_candidates) == 1:
            selected = all_candidates[0]

        else:
            if not interactive or not sys.stdin.isatty():
                if select_index is None:
                    selected = all_candidates[0]
                    if verbose:
                        print(
                            f"-> Auto-selected subtitle #1 of "
                            f"{len(all_candidates)} (non-interactive)"
                        )
                else:
                    if verbose:
                        print(
                            f"[INFO] OpenSubtitles: no more candidates "
                            f"({len(all_candidates)} available, all tried)"
                        )
                    return None
            else:
                print(f"\n[OpenSubtitles] Found {len(all_candidates)} subtitles. Choose one:")
                for i, (sub, _) in enumerate(all_candidates):
                    info, _ = _format_subtitle_info(sub, i)
                    print(info)
                print(f"Enter number (1-{len(all_candidates)}) or '0' to cancel: ", end="", flush=True)
                try:
                    choice = input().strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(all_candidates):
                        selected = all_candidates[idx]
                        print(f"-> Selected #{choice}")
                    else:
                        print("[WARN] Invalid selection, cancelling.")
                        return None
                except (ValueError, KeyboardInterrupt, EOFError):
                    print("\n[WARN] Selection cancelled.")
                    return None

        if not selected:
            return None

        sub, file_id = selected

        # Step 5: Download selected subtitle
        dl_headers = _get_headers(config, token)
        dl_url = f"https://{api_host}/api/v1/download"

        try:
            dl_resp = requests.post(
                dl_url,
                json={'file_id': file_id},
                headers=dl_headers,
                timeout=30
            )

            if dl_resp.status_code == 401:
                if verbose:
                    print("-> OpenSubtitles: download token expired, clearing cache")
                if os.path.exists(TOKEN_CACHE_FILE):
                    os.remove(TOKEN_CACHE_FILE)
                return None

            if dl_resp.status_code == 429 and wait_then_retry(dl_resp):
                dl_resp = requests.post(
                    dl_url,
                    json={'file_id': file_id},
                    headers=dl_headers,
                    timeout=30
                )

            if dl_resp.status_code == 429:
                block("opensubtitles", "the OpenSubtitles API is rate limiting this key", dl_resp)
                return None

            if dl_resp.status_code == 406:
                _mark_quota_exhausted(dl_resp)
                return None

            if dl_resp.status_code != 200:
                if verbose:
                    print(f"[WARN] Download request failed: {dl_resp.status_code}")
                return None

            dl_data = dl_resp.json()
            srt_url = dl_data.get('link')

            if not srt_url:
                if verbose:
                    print("[WARN] No download link in response")
                return None

            srt_resp = requests.get(srt_url, timeout=30)
            if srt_resp.status_code != 200:
                if verbose:
                    print(f"[WARN] Failed to fetch subtitle content: {srt_resp.status_code}")
                return None

            output_path = os.path.join(
                os.path.dirname(video_path),
                f"{os.path.splitext(video_basename)[0]}.{language}.srt"
            )
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(srt_resp.text)
            print(f"[SUCCESS] OpenSubtitles: downloaded to {output_path}")
            return output_path

        except requests.RequestException as e:
            if verbose:
                print(f"[WARN] OpenSubtitles download error: {e}")
            return None
        
        if verbose:
            print("[WARN] OpenSubtitles: Could not download any subtitle")
        return None
        
    except requests.RequestException as e:
        if verbose:
            print(f"[WARN] OpenSubtitles network error: {e}")
        return None
    except json.JSONDecodeError as e:
        if verbose:
            print(f"[WARN] OpenSubtitles JSON error: {e}")
        return None
    except Exception as e:
        if verbose:
            print(f"[WARN] OpenSubtitles unexpected error: {e}")
        return None