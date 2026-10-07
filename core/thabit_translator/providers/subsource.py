import os
import re
import sys
import requests
import zipfile
import io
import chardet
from core.match import is_relevant, NO_MATCH
from providers.limits import block, is_blocked, wait_then_retry

# SubSource searches by text only - no hash lookup - so the dispatcher's
# hash-then-query fallback would repeat the identical request.
SUPPORTS_HASH = False


def _format_subtitle_info(sub, index):
    """Format subtitle metadata for user display."""
    sub_id = sub.get('subtitleId') or sub.get('id', 'Unknown')
    fps = sub.get('fps', 'N/A')
    rating = sub.get('rating', 'N/A')
    downloads = sub.get('downloads', 'N/A')
    return f"{index + 1}. [ID: {sub_id}]\n   ★ {rating} | ↓ {downloads} | {fps} FPS", sub_id


def download_subsource(video_path, config, language, query=None, use_hash=True,
                       select_index=None, auto_select=True, interactive=True):
    """Download subtitle from SubSource with multi-selection support.
    
    Args:
        select_index: int or None - 0-based index to auto-select, or None for interactive
        auto_select: bool - if True and multiple results, auto-pick best match by rating
        interactive: bool - if False, never ask on stdin (pick option 1 / give up)
    """
    api_key = config['subsource'].get('api_key', '').strip()
    if not api_key:
        block("subsource", "no API key configured (see [subsource] api_key)", kind="config")
        return NO_MATCH

    if is_blocked("subsource"):
        return None

    # Language mapping (based on a4kSubtitles)
    lang_map = {
        'ar': 'arabic', 'en': 'english', 'fr': 'french', 'es': 'spanish',
        'de': 'german', 'it': 'italian', 'pt': 'portuguese', 'ru': 'russian',
        'zh': 'chinese', 'ja': 'japanese', 'ko': 'korean', 'nl': 'dutch',
        'pl': 'polish', 'tr': 'turkish'
    }
    target_lang = lang_map.get(language, language)

    video_basename = os.path.basename(video_path)
    verbose = config['settings'].get('verbose', False)

    if query:
        search_term = query
    else:
        search_term = os.path.splitext(video_basename)[0]
        search_term = re.sub(r'\s*\(\d{4}\)\s*', '', search_term).strip()

    url = "https://api.subsource.net/api/v1/movies/search"
    headers = {
        'X-API-Key': api_key,
        'User-Agent': 'Kodi/20.0 (Windows NT 10.0; Win64; x64) App/2.0',
        'Accept': 'application/json',
    }
    params = {
        'q': search_term,
        'searchType': 'text',
        'type': 'movie',
    }

    try:
        if verbose:
            print(f"-> SubSource: searching '{search_term}' (language {target_lang})")

        resp = requests.get(url, params=params, headers=headers, timeout=30)

        if resp.status_code == 429 and wait_then_retry(resp):
            resp = requests.get(url, params=params, headers=headers, timeout=30)

        if resp.status_code == 429:
            block("subsource", "the SubSource API is rate limiting this key", resp)
            return None

        if resp.status_code in (401, 403):
            block("subsource", "the API key was rejected - check [subsource] api_key", kind="config")
            return None

        if resp.status_code != 200:
            if verbose:
                print(f"[WARN] SubSource HTTP {resp.status_code}: {resp.text[:300]}")
            return None

        data = resp.json()
        results = data.get('data', [])
        if not results:
            if verbose:
                print("[WARN] SubSource: No movies found")
            return NO_MATCH

        # Hard-reject titles that are clearly another episode/movie. SubSource has
        # no Monk episodes: every "Monk - SxxExx" query returns "Bulletproof Monk".
        kept = [
            item for item in results
            if is_relevant(item.get('title') or item.get('name') or '', search_term)
        ]
        if verbose and len(kept) < len(results):
            print(
                f"[WARN] SubSource: dropped {len(results) - len(kept)} "
                f"result(s) for a different episode"
            )
        results = kept
        if not results:
            if verbose:
                print("[WARN] SubSource: no result matches this episode")
            return NO_MATCH

        movie_id = results[0].get('movieId')
        if not movie_id:
            if verbose:
                print("[WARN] SubSource: No movieId in result")
            return NO_MATCH

        subs_url = "https://api.subsource.net/api/v1/subtitles"
        subs_params = {
            'movieId': movie_id,
            'language': target_lang,
        }
        subs_resp = requests.get(subs_url, params=subs_params, headers=headers, timeout=30)

        if subs_resp.status_code == 429:
            block("subsource", "the SubSource API is rate limiting this key", subs_resp)
            return None

        if subs_resp.status_code != 200:
            if verbose:
                print(f"[WARN] SubSource subtitles fetch failed: {subs_resp.status_code}")
            return None

        subs_data = subs_resp.json()
        if not subs_data.get('success'):
            if verbose:
                print("[WARN] SubSource: No subtitles available")
            return NO_MATCH

        subtitles = subs_data.get('data', [])
        if not subtitles:
            if verbose:
                print("[WARN] SubSource: No subtitles found")
            return NO_MATCH

        # === SUBTITLE SELECTION LOGIC ===
        all_candidates = []
        for sub in subtitles:
            sub_id = sub.get('subtitleId') or sub.get('id')
            if sub_id:
                all_candidates.append(sub)

        if not all_candidates:
            if verbose:
                print("[WARN] SubSource: No valid subtitles found")
            return None

        selected_sub = None

        if select_index is not None and 0 <= select_index < len(all_candidates):
            selected_sub = all_candidates[select_index]
            if verbose:
                print(f"-> Selected subtitle #{select_index + 1} by index")

        elif auto_select and len(all_candidates) > 1:
            scored = []
            for sub in all_candidates:
                try:
                    rating = float(sub.get('rating', 0) or 0)
                except (ValueError, TypeError):
                    rating = 0
                downloads = int(sub.get('downloads', 0) or 0)
                score = rating * 10 + downloads
                scored.append((score, sub))
            scored.sort(key=lambda x: x[0], reverse=True)
            selected_sub = scored[0][1]
            if verbose:
                print(f"-> Auto-selected best match (score: {scored[0][0]})")

        elif len(all_candidates) == 1:
            selected_sub = all_candidates[0]

        else:
            if not interactive or not sys.stdin.isatty():
                if select_index is None:
                    selected_sub = all_candidates[0]
                    if verbose:
                        print(
                            f"-> Auto-selected subtitle #1 of "
                            f"{len(all_candidates)} (non-interactive)"
                        )
                else:
                    if verbose:
                        print(
                            f"[INFO] SubSource: no more candidates "
                            f"({len(all_candidates)} available, all tried)"
                        )
                    return None
            else:
                print(f"\n[SubSource] Found {len(all_candidates)} subtitles. Choose one:")
                for i, sub in enumerate(all_candidates):
                    info, _ = _format_subtitle_info(sub, i)
                    print(info)
                print(f"Enter number (1-{len(all_candidates)}) or '0' to cancel: ", end="", flush=True)
                try:
                    choice = input().strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(all_candidates):
                        selected_sub = all_candidates[idx]
                        print(f"-> Selected #{choice}")
                    else:
                        print("[WARN] Invalid selection, cancelling.")
                        return None
                except (ValueError, KeyboardInterrupt, EOFError):
                    print("\n[WARN] Selection cancelled.")
                    return None

        if not selected_sub:
            return None

        subtitle_id = selected_sub.get('subtitleId') or selected_sub.get('id')
        if not subtitle_id:
            if verbose:
                print("[WARN] SubSource: No subtitle ID found")
            return None

        # Download the ZIP archive
        download_url = f"https://api.subsource.net/api/v1/subtitles/{subtitle_id}/download"
        dl_resp = requests.get(download_url, headers=headers, timeout=30)

        if dl_resp.status_code == 429:
            block("subsource", "the SubSource API is rate limiting this key", dl_resp)
            return None

        if dl_resp.status_code != 200:
            if verbose:
                print(f"[WARN] SubSource download failed: {dl_resp.status_code}")
            return None

        # Process the ZIP content
        zip_data = io.BytesIO(dl_resp.content)
        if not zipfile.is_zipfile(zip_data):
            if verbose:
                print("[WARN] SubSource: Downloaded file is not a ZIP")
            return None

        with zipfile.ZipFile(zip_data, 'r') as zf:
            # List all .srt files
            srt_files = [name for name in zf.namelist() if name.lower().endswith('.srt')]
            if not srt_files:
                if verbose:
                    print("[WARN] SubSource: No .srt file found in ZIP")
                return None

            # Prefer non-HI/SDH subtitles if available
            selected = srt_files[0]
            for name in srt_files:
                lower_name = name.lower()
                if 'hi' not in lower_name and 'sdh' not in lower_name:
                    selected = name
                    break

            # Read raw bytes
            raw_bytes = zf.read(selected)

        # Detect encoding
        detection = chardet.detect(raw_bytes)
        encoding = detection.get('encoding') or 'utf-8'
        confidence = detection.get('confidence', 0)

        if verbose:
            print(f"[INFO] Detected encoding: {encoding} (confidence: {confidence:.2f})")

        # Decode to text
        try:
            text = raw_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # Fallback to common Arabic encodings
            for fallback in ['windows-1256', 'cp1256', 'iso-8859-6', 'utf-8']:
                try:
                    text = raw_bytes.decode(fallback)
                    encoding = fallback
                    break
                except:
                    continue
            else:
                # Last resort: replace errors
                text = raw_bytes.decode('utf-8', errors='replace')
                encoding = 'utf-8 (with errors)'

        # Save as UTF-8
        output_path = os.path.join(
            os.path.dirname(video_path),
            f"{os.path.splitext(video_basename)[0]}.{language}.srt"
        )
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(text)

        print(f"[SUCCESS] SubSource: downloaded and extracted '{selected}' to {output_path} (converted from {encoding})")
        return output_path

    except Exception as e:
        if verbose:
            print(f"[WARN] SubSource error: {e}")
        return None