import os
import re
import sys
import requests
import zipfile
import io
from core.config import extract_imdb_id
from core.match import is_relevant, video_stem, episode_key, show_title, NO_MATCH
from providers.limits import block, is_blocked, wait_then_retry

# SubDL has no file-hash lookup: every search is by title/episode, so asking it
# twice per candidate (the dispatcher's hash-then-query fallback) only doubles
# the requests against its rate limit.
SUPPORTS_HASH = False

# Words SubDL uses in its error bodies when it is a limit and not a missing subtitle
_LIMIT_WORDS = ("limit", "quota", "rate", "too many", "busy")


def _safe_url(url):
    """Hide the API key before a download URL reaches the log (it is streamed to the web UI)."""
    return re.sub(r"([?&]api_key=)[^&]+", r"\1***", url)


def _download_limit_reason(url):
    """Name the limit a 429 on the download link actually means."""
    if "api_key=" in url:
        return (
            "your account's daily download limit is spent "
            "(50 downloads/day on the free plan)"
        )
    return (
        "SubDL's anonymous download limit for this IP is spent (300/day); a download "
        "link carrying api_key is counted against your account quota instead"
    )


def _is_limit_error(text):
    low = text.lower()
    return any(word in low for word in _LIMIT_WORDS)


def _detect_and_decode_arabic(content_bytes, target_lang="ar"):
    """
    Detect and decode Arabic subtitle content with fallback encodings.
    Tries: UTF-8 > Windows-1256 > ISO-8859-6 > auto-detect with chardet
    """
    # Common encodings for Arabic subtitles, in priority order
    encodings_to_try = ["utf-8", "windows-1256", "iso-8859-6", "cp1256"]

    for enc in encodings_to_try:
        try:
            decoded = content_bytes.decode(enc)
            # Basic sanity check: should contain common Arabic chars or SRT structure
            if decoded.strip().startswith("1") or "\n" in decoded:
                # If target is Arabic, verify we got Arabic text
                if target_lang == "ar":
                    # Check for Arabic Unicode range (U+0600 to U+06FF)
                    if any(0x0600 <= ord(c) <= 0x06FF for c in decoded[:500]):
                        return decoded
                    # If no Arabic chars found but user wants Arabic, keep trying
                    if enc == "utf-8":
                        continue
                return decoded
        except (UnicodeDecodeError, LookupError):
            continue

    # Fallback: try chardet if available
    try:
        import chardet

        result = chardet.detect(content_bytes)
        if result["encoding"] and result["confidence"] > 0.7:
            return content_bytes.decode(result["encoding"], errors="replace")
    except ImportError:
        pass

    # Last resort: decode with utf-8, replacing errors
    return content_bytes.decode("utf-8", errors="replace")


def _extract_srt_from_zip(zip_bytes):
    """Extract .srt content from a ZIP archive returned by SubDL."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # Find .srt files, prefer ones with language code in name
            srt_files = [f for f in zf.namelist() if f.lower().endswith(".srt")]
            if not srt_files:
                return None
            # Prioritize files that might contain target language
            for pattern in [".ar.", "_ar.", "arabic", "ara."]:
                for f in srt_files:
                    if pattern in f.lower():
                        return zf.read(f)
            # Return first .srt if no preference match
            return zf.read(srt_files[0])
    except zipfile.BadZipFile:
        return None


def _format_subtitle_info(sub, index):
    """Format subtitle metadata for user display."""
    lang = sub.get("language", "Unknown")
    release = sub.get("release", sub.get("filename", "Unknown"))
    rating = sub.get("rating", sub.get("score", "N/A"))
    uploader = sub.get("uploader", sub.get("user", "Anonymous"))
    fps = sub.get("fps", "N/A")

    return f"{index + 1}. [{lang}] {release}\n   ★ {rating} | 👤 {uploader} | {fps} FPS"


def download_subdl(
    video_path,
    config,
    language,
    query=None,
    use_hash=True,
    select_index=None,
    auto_select=True,
    interactive=True,
):
    """
    Download subtitle from SubDL with encoding fix and multi-selection support.

    Args:
        select_index: int or None - 0-based index to auto-select, or None for interactive
        auto_select: bool - if True and multiple results, auto-pick best match by rating
        interactive: bool - if False, never ask on stdin (pick option 1 / give up)
    """
    api_key = config["subdl"].get("api_key", "").strip().strip('"')
    if not api_key:
        block("subdl", "no API key configured (see [subdl] api_key)", kind="config")
        return NO_MATCH

    if is_blocked("subdl"):
        return None

    # SubDL API requires UPPERCASE language codes
    lang_code = language.upper()
    video_basename = os.path.basename(video_path)
    verbose = config["settings"].get("verbose", False)

    # Build search terms with IMDB priority. A TV episode needs type=tv with
    # season/episode: film_name="Monk - S01E12" is not a movie and returns nothing.
    search_key = query or video_stem(video_path)
    want = episode_key(search_key)
    search_terms = []  # (extra params, human label)

    if want and show_title(search_key):
        show = show_title(search_key)
        search_terms.append((
            {
                "type": "tv",
                "film_name": show,
                "season_number": want[0],
                "episode_number": want[1],
            },
            f"tv {show!r} S{want[0]:02d}E{want[1]:02d}",
        ))
    else:
        imdb_id = extract_imdb_id(video_basename)
        if imdb_id:
            search_terms.append(({"type": "movie", "imdb_id": imdb_id}, f"imdb {imdb_id}"))
        if query:
            search_terms.append(({"type": "movie", "film_name": query}, f"query '{query}'"))
        else:
            base = os.path.splitext(video_basename)[0]
            clean = re.sub(r"\s*\(\d{4}\)\s*", "", base).strip()
            search_terms.append(({"type": "movie", "film_name": clean}, f"film '{clean}'"))
            year_match = re.search(r"\((\d{4})\)", base)
            if year_match:
                search_terms.append(
                    ({"type": "movie", "film_name": f"{clean} {year_match.group(1)}"},
                     f"film '{clean} {year_match.group(1)}'")
                )
                search_terms.append(
                    ({"type": "movie", "film_name": base}, f"film '{base}'")
                )

    headers = {
        "User-Agent": config["opensubtitles"].get("user_agent", "ThabitTranslator v1.0")
    }
    BASE_URL = "https://api.subdl.com/api/v1/subtitles"
    DL_PREFIX = "https://dl.subdl.com"

    all_candidates = []  # Store all found subtitles for selection
    dropped = 0

    for extra, label in search_terms:
        if verbose:
            print(f"-> SubDL: searching {label}")

        params = {
            "api_key": api_key,
            "languages": lang_code,
            "subs_per_page": 20,
            **extra,
        }

        try:
            resp = requests.get(BASE_URL, params=params, headers=headers, timeout=30)
            if resp.status_code == 429 and wait_then_retry(resp):
                resp = requests.get(BASE_URL, params=params, headers=headers, timeout=30)

            if resp.status_code == 429:
                block(
                    "subdl",
                    "hit SubDL's API rate limit (too many requests too fast, or the "
                    "free plan's 2000 requests/day)",
                    resp,
                )
                return NO_MATCH

            if resp.status_code in (401, 403):
                block("subdl", "the API key was rejected - check [subdl] api_key", kind="config")
                return None

            if resp.status_code != 200:
                if verbose:
                    print(f"   Status {resp.status_code}")
                continue

            data = resp.json()
            if not data.get("status"):
                error = str(data.get("error") or "unknown error")
                if _is_limit_error(error):
                    block("subdl", f"SubDL refused the search: {error}", resp)
                    return NO_MATCH
                if verbose:
                    print(f"   API error: {error}")
                continue

            subtitles = data.get("subtitles", [])
            if not subtitles:
                continue

            # Collect all matching subtitles
            for sub in subtitles:
                sub_lang = sub.get("language", "").upper()
                # Accept exact match or Arabic variants
                if sub_lang != lang_code and not (
                    lang_code == "AR" and sub_lang.startswith("AR")
                ):
                    continue
                if sub.get("url"):
                    # Hard-reject results that are clearly another episode/title
                    name = sub.get("release") or sub.get("filename") or ""
                    # A type=tv query already pins season+episode, so the API's
                    # answer is the feature key (SubDL often has no release name)
                    if is_relevant(name, search_key, False, want):
                        all_candidates.append(sub)
                    else:
                        dropped += 1

        except Exception as e:
            if verbose:
                print(f"   Error: {e}")
            continue

    if dropped and verbose:
        print(f"[WARN] SubDL: dropped {dropped} result(s) for a different episode")

    if not all_candidates:
        if verbose:
            print("[WARN] SubDL: No subtitles found after trying all search terms")
        return NO_MATCH

    # === SUBTITLE SELECTION LOGIC ===
    selected_sub = None

    if select_index is not None and 0 <= select_index < len(all_candidates):
        # User specified exact index
        selected_sub = all_candidates[select_index]
        if verbose:
            print(f"-> Selected subtitle #{select_index + 1} by index")

    elif auto_select and len(all_candidates) > 1:
        # Auto-select: prefer highest rating, then most downloads, then first
        scored = []
        for sub in all_candidates:
            # Parse rating (might be string like "9.5" or int)
            try:
                rating = float(sub.get("rating", sub.get("score", 0)) or 0)
            except (ValueError, TypeError):
                rating = 0
            downloads = int(sub.get("downloads", 0) or 0)
            # Score: rating * 10 + downloads (rating weighted higher)
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
                if config["settings"].get("verbose"):
                    print(
                        f"-> Auto-selected subtitle #1 of "
                        f"{len(all_candidates)} (non-interactive)"
                    )
            else:
                if config["settings"].get("verbose"):
                    print(
                        f"[INFO] SubDL: no more candidates "
                        f"({len(all_candidates)} available, all tried)"
                    )
                return None
        else:
            # Interactive selection: print list and wait for input
            print(f"\n[SubDL] Found {len(all_candidates)} Arabic subtitles. Choose one:")
            for i, sub in enumerate(all_candidates):
                print(_format_subtitle_info(sub, i))
            print(
                f"Enter number (1-{len(all_candidates)}) or '0' to cancel: ",
                end="",
                flush=True,
            )

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

    # === DOWNLOAD AND PROCESS ===
    download_url = selected_sub.get("url")
    if not download_url:
        if verbose:
            print("   No download URL in response")
        return None

    if not download_url.startswith("http"):
        download_url = f"{DL_PREFIX}{download_url}"

    if verbose:
        print(f"   Downloading: {_safe_url(download_url)}")

    try:
        srt_resp = requests.get(download_url, timeout=30)

        if srt_resp.status_code == 429 and wait_then_retry(srt_resp):
            srt_resp = requests.get(download_url, timeout=30)

        if srt_resp.status_code == 429:
            block("subdl", _download_limit_reason(download_url), srt_resp)
            return None

        if srt_resp.status_code != 200:
            detail = (srt_resp.text or "").strip().replace("\n", " ")[:120]
            if verbose:
                print(f"   Download failed: HTTP {srt_resp.status_code}" + (f" - {detail}" if detail else ""))
            return None

        # Handle ZIP or direct SRT
        content_bytes = None
        if (
            download_url.endswith(".zip")
            or srt_resp.headers.get("Content-Type") == "application/zip"
        ):
            content_bytes = _extract_srt_from_zip(srt_resp.content)
            if not content_bytes:
                if verbose:
                    print("   Failed to extract .srt from ZIP")
                return None
        else:
            content_bytes = srt_resp.content

        # Decode with Arabic encoding detection
        content_text = _detect_and_decode_arabic(content_bytes, target_lang=language)

        # Prepare output path
        output_dir = os.path.dirname(video_path)
        output_filename = (
            f"{os.path.splitext(video_basename)[0]}.{language.lower()}.srt"
        )
        output_path = os.path.join(output_dir, output_filename)

        # Write with UTF-8 (standard for modern players)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content_text)

        print(f"[SUCCESS] SubDL: downloaded to {output_path}")
        return output_path

    except Exception as e:
        if verbose:
            print(f"   Error during download/processing: {e}")
        return None

    if verbose:
        print("[WARN] SubDL: Download processing failed")
    return None
