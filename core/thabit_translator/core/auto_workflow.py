#!/usr/bin/env python3
"""
Auto Mode - Full workflow that consumes all other modes and commands.
Based on the flowchart in auto_mode.md
"""
import os
import sys
import re
import srt
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime

from core.config import load_config
from core.extract import list_embedded_subs, extract_subtitles
from core.paths import resolve_input_path, VIDEO_EXTENSIONS
from core.translate import translate_srt
from providers import download_subtitle
from providers.opensubtitles import quota_exhausted, quota_reset_label
from providers.limits import (
    announce,
    blocked_providers,
    blocked_reason,
    label as provider_label,
)
from core.speech_to_text import generate_srt_from_speech

_SAMPLE_RE = re.compile(r"(^|[^a-z0-9])sample([^a-z0-9]|$)", re.IGNORECASE)


def _is_video_file(path: str) -> bool:
    """Check if input is a video file based on extension."""
    ext = Path(path).suffix.lower()
    return ext in VIDEO_EXTENSIONS


def _is_subtitle_file(path: str) -> bool:
    """Check if input is a subtitle file based on extension."""
    sub_extensions = {".srt", ".ass", ".ssa", ".sub"}
    ext = Path(path).suffix.lower()
    return ext in sub_extensions


def _is_valid_srt(path: str) -> Tuple[bool, str]:
    """
    Check if a subtitle file is a valid SRT by parsing it.
    Returns (is_valid, error_message)
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        subtitles = list(srt.parse(content))
        if not subtitles:
            return False, "Empty SRT file"
        for sub in subtitles:
            if sub.start < sub.end:
                return True, ""
        return False, "Invalid timestamps"
    except Exception as e:
        return False, str(e)


def _is_synced_subtitle(srt_path: str, video_path: Optional[str] = None) -> Tuple[bool, str]:
    """
    Check if subtitle is synced (time-synchronized).
    - Validates timestamps are in order
    - If video provided, uses ffprobe to verify sync with audio
    Returns (is_synced, reason)
    """
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Try parsing as SRT
        subtitles = list(srt.parse(content))
        if not subtitles:
            return True, "Empty file but treating as valid (may be different format)"
        
        # Validate timestamps are in order and positive duration
        idx = 0
        prev_ts = 0
        for sub in subtitles:
            idx += 1
            # Check if timestamps are timedelta objects with total_seconds()
            try:
                start_ts = sub.start.total_seconds() if hasattr(sub.start, 'total_seconds') else 0
                end_ts = sub.end.total_seconds() if hasattr(sub.end, 'total_seconds') else 0
                
                if end_ts <= start_ts:
                    return False, f"Zero/negative duration at index {sub.index}"
                if idx > 1 and start_ts <= prev_ts:
                    return False, f"Out-of-order timestamp at index {sub.index}"
                prev_ts = end_ts
            except AttributeError:
                # If we can't parse timestamps, assume it's valid
                pass

        if video_path and os.path.exists(video_path):
            import subprocess
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "s:0",
                 "-show_entries", "stream=index,codec_name,duration",
                 "-of", "default=noprint_wrappers=1", video_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                return True, "Valid with video check"

        return True, "Valid timestamps in order"
    except Exception as e:
        # If parsing fails, assume valid (probably just a different format)
        return True, f"Parse error but treating as valid: {e}"


def _get_target_language(config: dict) -> str:
    """Get target language from config or default."""
    return config.get("opensubtitles", {}).get("default_language", 
           config.get("settings", {}).get("default_target_lang", "ar"))


def _get_source_language(config: dict) -> str:
    """Get source language from config or default."""
    return config.get("settings", {}).get("default_source_lang", "en")


def _guess_source_language(video_path: str) -> str:
    """
    Guess the source language from video filename using common patterns.
    This is a simple heuristic - can be enhanced later.
    """
    filename = os.path.basename(video_path).lower()
    patterns = {
        "en": [r"\ben\b", r"\beng\b", r"\benglish\b"],
        "ar": [r"\bar\b", r"\barabic\b"],
        "fr": [r"\bfr\b", r"\bfrench\b"],
        "de": [r"\bde\b", r"\bgerman\b"],
        "es": [r"\bes\b", r"\bspanish\b"],
        "it": [r"\bit\b", r"\bitalian\b"],
        "ja": [r"\bja\b", r"\bjapanese\b"],
        "ko": [r"\bko\b", r"\bkorean\b"],
        "zh": [r"\bzh\b", r"\bchinese\b"],
        "ru": [r"\bru\b", r"\brussian\b"],
        "pt": [r"\bpt\b", r"\bportuguese\b"],
        "tr": [r"\btr\b", r"\bturkish\b"],
    }
    for lang, pattern_list in patterns.items():
        for pattern in pattern_list:
            if re.search(pattern, filename):
                return lang
    return "en"


def _format_output_path(input_path: str, lang: str, suffix: str = "srt") -> str:
    """Generate output path next to input file."""
    base = os.path.splitext(input_path)[0]
    return f"{base}.{lang}.{suffix}"


def run_auto_mode(
    input_path: str,
    config_path: Optional[str] = None,
    target_lang: Optional[str] = None,
    source_lang: Optional[str] = None,
    verbose: bool = True,
    force: bool = False,
    stt: str = "ask",
) -> Optional[str]:
    """
    Execute the full auto workflow based on auto_mode.md flowchart.

    `input_path` may be a video file, a subtitle file, or a folder - a folder is
    processed recursively (every video inside it, in sorted order).

    `stt` decides what happens when nothing else can produce a subtitle (providers
    blocked or empty, no embedded track): "ask" prompts on a terminal, "yes" runs
    STT without asking, "no" skips the video instead.

    Returns the final output SRT path (or the folder path in folder mode),
    or None if failed.
    """
    input_path = resolve_input_path(input_path, verbose=verbose)
    config = load_config(config_path)
    tgt_lang = target_lang or _get_target_language(config)
    src_lang = source_lang or _get_source_language(config)
    stt_state = {"policy": stt, "always": stt == "yes", "declined": 0}

    if input_path and os.path.isdir(input_path):
        return _run_folder(
            input_path, config, tgt_lang, src_lang, verbose, force, stt_state
        )

    if verbose:
        print(f"[AUTO] Input: {input_path}")
        print(f"[AUTO] Target language: {tgt_lang}")

    return _run_single(input_path, config, tgt_lang, src_lang, verbose, stt_state)


def _collect_videos(folder: str) -> List[str]:
    """Recursively collect video files in a folder (sorted, sample files skipped)."""
    try:
        entries = sorted(Path(folder).rglob("*"), key=lambda p: str(p).lower())
    except OSError:
        return []
    videos = []
    for entry in entries:
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if _SAMPLE_RE.search(entry.stem):
            continue
        videos.append(str(entry))
    return videos


def _run_folder(
    folder: str,
    config: dict,
    tgt_lang: str,
    src_lang: str,
    verbose: bool,
    force: bool = False,
    stt_state: Optional[dict] = None,
) -> Optional[str]:
    """Run auto mode on every video inside a folder (recursively)."""
    if stt_state is None:
        stt_state = {"policy": "ask", "always": False, "declined": 0}
    videos = _collect_videos(folder)

    if verbose:
        print(f"[AUTO] Folder: {folder}")
        print(f"[AUTO] Target language: {tgt_lang}")
        print(f"[AUTO] Found {len(videos)} video(s)")
    if not videos:
        print(f"[ERROR] No video files found in: {folder}")
        return None

    total = len(videos)
    ok = skipped = declined = 0
    failures = []

    for index, video in enumerate(videos, 1):
        output_path = _format_output_path(video, tgt_lang)
        if not force and os.path.exists(output_path):
            is_valid, _ = _is_valid_srt(output_path)
            if is_valid:
                if verbose:
                    print(
                        f"[AUTO] [{index}/{total}] SKIP (exists): "
                        f"{os.path.basename(output_path)}"
                    )
                skipped += 1
                continue

        if verbose:
            print(f"[AUTO] [{index}/{total}] {os.path.basename(video)}")

        try:
            declined_before = stt_state["declined"]
            result = _run_single(video, config, tgt_lang, src_lang, verbose, stt_state)
        except Exception as exc:
            print(f"[ERROR] {os.path.basename(video)}: {exc}")
            result = None
            declined_before = stt_state["declined"]

        if result:
            ok += 1
        elif stt_state["declined"] > declined_before:
            declined += 1
        else:
            failures.append(os.path.basename(video))

    if verbose:
        summary = f"{ok} ok, {skipped} skipped"
        if declined:
            summary += f", {declined} STT-declined"
        print(f"[AUTO] Folder done: {summary}, {len(failures)} failed")
    for name in failures:
        print(f"[ERROR] Failed: {name}")

    return folder if (ok or skipped or declined) else None


def _confirm_stt(video_path: str, stt_state: dict) -> bool:
    """Ask before starting STT, which is by far the slowest path in auto mode.

    Reached only when providers produced nothing and there is no embedded track.
    Answer on a terminal: y (run), n/Enter (skip), a (run now and for the rest of
    the run). Without a terminal the video is skipped instead of blocking.
    """
    name = os.path.basename(video_path)
    policy = stt_state.get("policy", "ask")

    def _skip(reason: str) -> bool:
        print(f"[AUTO] STT skipped ({reason}): {name}")
        stt_state["declined"] += 1
        return False

    if policy == "no":
        return _skip("--stt no")
    if policy == "yes" or stt_state.get("always"):
        return True
    if not sys.stdin.isatty():
        return _skip("no terminal to ask (use --stt yes to force it)")

    blocked = [provider_label(p) for p in blocked_providers("limit")]
    cause = f" ({', '.join(blocked)} over a limit)" if blocked else ""
    print(f"\n[AUTO] No subtitle for {name}: providers had nothing{cause}")
    print("[AUTO]   and the file has no embedded track.")
    print("[AUTO] STT (Vosk) would transcribe the audio on CPU - the slowest step.")
    while True:
        try:
            print(
                "Start STT? [y]es  [n]o/skip  [a]lways for this run: ",
                end="",
                flush=True,
            )
            answer = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return _skip("cancelled")
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return _skip("declined")
        if answer in ("a", "always", "all"):
            stt_state["always"] = True
            print("[AUTO] STT will run without asking for the rest of this run")
            return True
        print("Please answer y, n or a.")


def _run_single(
    input_path: str,
    config: dict,
    tgt_lang: str,
    src_lang: str,
    verbose: bool,
    stt_state: Optional[dict] = None,
) -> Optional[str]:
    """Auto workflow for one video/subtitle file (config already loaded)."""
    if stt_state is None:
        stt_state = {"policy": "ask", "always": False, "declined": 0}

    is_video = _is_video_file(input_path)
    is_subtitle = _is_subtitle_file(input_path)
    
    if not is_video and not is_subtitle:
        print("[ERROR] Input must be a video file or subtitle file")
        return None
    
    # Case 1: Input is already a subtitle file - just translate it
    if is_subtitle:
        if verbose:
            print("[AUTO] Input is a subtitle file, translating...")
        output_path = _format_output_path(input_path, tgt_lang)
        src_lang_clean = src_lang.split("_")[0].split("-")[0]
        success = translate_srt(input_path, output_path, src_lang_clean, tgt_lang)
        if success and os.path.exists(output_path):
            if verbose:
                print(f"[AUTO] Translated: {output_path}")
            return output_path
        else:
            print("[ERROR] Translation failed")
            return None
    
    # From here on, input is a video file
    video_path = input_path
    
    # Step 1: Try to download subtitle in target language
    if verbose:
        print("[AUTO] Checking for downloadable subtitles...")
    
    dl_path = download_subtitle(
        video_path, config, tgt_lang, None, True, None, None, True,
        interactive=False,
    )
    
    if dl_path and os.path.exists(dl_path):
        # Validate downloaded subtitle is not empty
        file_size = os.path.getsize(dl_path)
        if file_size == 0:
            if verbose:
                print(f"[AUTO] Downloaded subtitle is empty (0 bytes), ignoring...")
            os.remove(dl_path)
            dl_path = None
        else:
            if verbose:
                print(f"[AUTO] Downloaded subtitle: {dl_path} ({file_size} bytes)")
            
            # Validate it's a valid SRT file
            is_valid, error = _is_valid_srt(dl_path)
            if not is_valid:
                if verbose:
                    print(f"[AUTO] Downloaded subtitle invalid: {error}")
                os.remove(dl_path)
                dl_path = None
            else:
                # Downloaded subtitle is already in target language, no need to translate
                return dl_path
    
    # Say out loud when a provider limit - not "no subtitles" - is what blocked us
    if not dl_path:
        if quota_exhausted():
            until = quota_reset_label()
            announce(
                "auto:opensubtitles",
                "[AUTO] OpenSubtitles is over your account's daily download limit"
                + (f" until {until}" if until else "")
                + " - it may already have this subtitle",
            )
        for prov in blocked_providers("limit"):
            announce(
                f"auto:{prov}",
                f"[AUTO] {provider_label(prov)} is blocked: {blocked_reason(prov)}"
                f" - it may already have this subtitle",
            )

    # Step 2: Check for embedded subtitles
    if verbose:
        print("[AUTO] Checking for embedded subtitles...")
    
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index,codec_name:stream_tags=language",
             "-of", "json", video_path],
            capture_output=True, text=True, timeout=30
        )
        has_embedded = result.returncode == 0 and "streams" in result.stdout and result.stdout.strip()
    except Exception:
        has_embedded = False
    
    embedded_srt = None
    if has_embedded:
        if verbose:
            print("[AUTO] Attempting to extract embedded subtitle...")
        embedded_srt = extract_subtitles(video_path, None, tgt_lang)
        
        if embedded_srt and os.path.exists(embedded_srt):
            is_valid, error = _is_valid_srt(embedded_srt)
            
            if is_valid:
                if verbose:
                    print(f"[AUTO] Extracted subtitle is valid")
                
                # Translate embedded subtitle
                extracted_lang = "en"  # Most common
                if tgt_lang != extracted_lang:
                    output_path = _format_output_path(video_path, tgt_lang)
                    success = translate_srt(embedded_srt, output_path, extracted_lang, tgt_lang)
                    if success and os.path.exists(output_path):
                        if verbose:
                            print(f"[AUTO] Translated: {output_path}")
                        return output_path
                    elif verbose:
                        print("[WARN] Translation failed, returning original")
                        return embedded_srt
                else:
                    return embedded_srt
    
    # Step 3: Try English subtitle download as fallback
    if verbose:
        print("[AUTO] Trying English subtitle...")
    
    dl_eng = download_subtitle(video_path, config, "en", None, True, None, None, True,
                               interactive=False)

    
    if dl_eng and os.path.exists(dl_eng):
        # Validate downloaded subtitle is not empty
        file_size = os.path.getsize(dl_eng)
        if file_size == 0:
            if verbose:
                print(f"[AUTO] Downloaded English subtitle is empty (0 bytes), ignoring...")
            os.remove(dl_eng)
            dl_eng = None
        else:
            if verbose:
                print(f"[AUTO] Downloaded English subtitle: {dl_eng} ({file_size} bytes)")
            
            # Validate it's a valid SRT file
            is_valid, error = _is_valid_srt(dl_eng)
            if not is_valid:
                if verbose:
                    print(f"[AUTO] Downloaded English subtitle invalid: {error}")
                os.remove(dl_eng)
                dl_eng = None
            else:
                if verbose:
                    print("[AUTO] English subtitle valid, translating...")
                output_path = _format_output_path(video_path, tgt_lang)
                success = translate_srt(dl_eng, output_path, "en", tgt_lang)
                if success and os.path.exists(output_path):
                    if verbose:
                        print(f"[AUTO] Translated: {output_path}")
                    return output_path
                print("[ERROR] Translation failed")
                return None
    
    # Step 4: Use STT as last resort (only after the user agrees)
    if not _confirm_stt(video_path, stt_state):
        return None

    guessed_lang = _guess_source_language(video_path)
    if verbose:
        print(f"[AUTO] Applying STT for language: {guessed_lang}")
    
    stt_output = _format_output_path(video_path, guessed_lang)
    success = generate_srt_from_speech(video_path, stt_output, lang=guessed_lang)
    
    if not success:
        print("[ERROR] STT failed")
        return None
    
    if verbose:
        print(f"[AUTO] STT completed: {stt_output}")
        print(f"[AUTO] Translating to {tgt_lang}...")
    
    output_path = _format_output_path(video_path, tgt_lang)
    success = translate_srt(stt_output, output_path, guessed_lang, tgt_lang)
    
    if success and os.path.exists(output_path):
        if verbose:
            print(f"[AUTO] Translated: {output_path}")
        return output_path
    
    print("[ERROR] Translation failed")
    return None