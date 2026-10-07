import os
import subprocess
import json
import shutil
import tempfile
import imageio_ffmpeg


# --- Prefer system ffmpeg/ffprobe if available ---
def _get_ffmpeg_bin():
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _get_ffprobe_bin():
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe
    ff_dir = os.path.dirname(_get_ffmpeg_bin())
    candidate = os.path.join(ff_dir, "ffprobe")
    return candidate if os.path.exists(candidate) else "ffprobe"


FFMPEG_BIN = _get_ffmpeg_bin()
FFPROBE_BIN = _get_ffprobe_bin()


def get_sub_streams(video):
    if not os.path.exists(video):
        print(f"[ERROR] Video file not found: {video}")
        return []
    # Fetch more details to aid diagnosis
    cmd = [
        FFPROBE_BIN,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "stream=index,codec_name,codec_type,codec_tag_string:stream_tags=language",
        "-select_streams",
        "s",
        video,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0 or not res.stdout.strip():
            return []
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        for s in streams:
            if "tags" not in s:
                s["tags"] = {}
            # Some older ffprobe put language in stream_tags
            if "stream_tags" in s and "language" in s["stream_tags"]:
                s["tags"]["language"] = s["stream_tags"]["language"]
        return streams
    except Exception as e:
        print(f"[ERROR] ffprobe failed: {e}")
        return []


def list_embedded_subs(video):
    subs = get_sub_streams(video)
    if not subs:
        print("[INFO] No embedded subtitle streams found.")
        return
    print(
        f"[INFO] Found {len(subs)} subtitle stream(s) in '{os.path.basename(video)}':"
    )
    print(f"{'Index':<6} | {'Language':<10} | {'Codec':<12} | {'Title'}")
    print("-" * 60)
    for s in subs:
        idx = s.get("index", "?")
        lang = s.get("tags", {}).get("language", "und")
        codec = s.get("codec_name", "unknown")
        title = s.get("tags", {}).get("title", "")
        print(f"{idx:<6} | {lang:<10} | {codec:<12} | {title}")


def extract_subtitles(video, stream_global_idx=None, lang_hint="en"):
    subs = get_sub_streams(video)
    if not subs:
        print("[ERROR] Cannot extract: No subtitle streams detected.")
        return None

    # Select target stream
    target_stream = None
    relative_pos = 0
    if stream_global_idx is not None:
        for rel_i, s in enumerate(subs):
            if s.get("index") == stream_global_idx:
                target_stream = s
                relative_pos = rel_i
                break
        if not target_stream:
            available = [s.get("index") for s in subs]
            print(
                f"[ERROR] Stream index {stream_global_idx} not found. Available: {available}"
            )
            return None
    else:
        target_stream = subs[0]

    tags = target_stream.get("tags", {})
    lang_tag = tags.get("language", lang_hint) or lang_hint
    lang_code = lang_tag.split("-")[0].lower()
    dir_name = os.path.dirname(os.path.abspath(video))
    base_name = os.path.splitext(os.path.basename(video))[0]
    output_path = os.path.join(dir_name, f"{base_name}.{lang_code}.srt")

    print(f"-> Extracting stream {target_stream.get('index')} to: {output_path}")

    def run_ffmpeg(cmd, description):
        print(f"   Trying: {description}")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if (
                res.returncode == 0
                and os.path.exists(output_path)
                and os.path.getsize(output_path) > 100
            ):
                return True
            else:
                # Print full stderr for debugging (truncated to 2000 chars to avoid overwhelming)
                err = res.stderr.strip()
                if err:
                    print(f"   [FFmpeg stderr] {err[:2000]}")
                return False
        except Exception as e:
            print(f"   [Exception] {e}")
            return False

    # --- FFmpeg attempts ---
    # 1. Direct SRT
    if run_ffmpeg(
        [
            FFMPEG_BIN,
            "-y",
            "-i",
            video,
            "-map",
            f"0:s:{relative_pos}",
            "-c:s",
            "srt",
            "-f",
            "srt",
            output_path,
        ],
        "Direct SRT conversion",
    ):
        print(f"[SUCCESS] Extracted successfully. Saved to: {output_path}")
        return output_path

    # 2. With -fix_sub_duration
    if run_ffmpeg(
        [
            FFMPEG_BIN,
            "-y",
            "-i",
            video,
            "-map",
            f"0:s:{relative_pos}",
            "-c:s",
            "srt",
            "-fix_sub_duration",
            output_path,
        ],
        "SRT with -fix_sub_duration",
    ):
        print(f"[SUCCESS] Extracted successfully. Saved to: {output_path}")
        return output_path

    # 3. Two-stage: copy to temp MKV then convert
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_mkv = os.path.join(tmpdir, "temp_sub.mkv")
        cmd_copy = [
            FFMPEG_BIN,
            "-y",
            "-i",
            video,
            "-map",
            f"0:s:{relative_pos}",
            "-c",
            "copy",
            "-f",
            "matroska",
            temp_mkv,
        ]
        print("   Trying two‑stage extraction: copy to MKV then convert to SRT")
        res_copy = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=120)
        if res_copy.returncode == 0 and os.path.exists(temp_mkv):
            if run_ffmpeg(
                [
                    FFMPEG_BIN,
                    "-y",
                    "-i",
                    temp_mkv,
                    "-c:s",
                    "srt",
                    "-f",
                    "srt",
                    output_path,
                ],
                "Convert from temporary MKV",
            ):
                print(
                    f"[SUCCESS] Extracted via two‑stage method. Saved to: {output_path}"
                )
                return output_path
        else:
            err = res_copy.stderr.strip()
            print(f"   [FFmpeg copy failed] {err[:2000] if err else 'Unknown error'}")

    # 4. Extract as ASS then convert
    ass_output = os.path.join(dir_name, f"{base_name}.{lang_code}.ass")
    if run_ffmpeg(
        [
            FFMPEG_BIN,
            "-y",
            "-i",
            video,
            "-map",
            f"0:s:{relative_pos}",
            "-c:s",
            "ass",
            ass_output,
        ],
        "Extract as ASS",
    ):
        print("[INFO] Extracted ASS subtitle, attempting conversion to SRT...")
        if run_ffmpeg(
            [FFMPEG_BIN, "-y", "-i", ass_output, "-c:s", "srt", output_path],
            "Convert ASS → SRT",
        ):
            os.remove(ass_output)
            print(f"[SUCCESS] Converted ASS to SRT. Saved to: {output_path}")
            return output_path
        else:
            print(
                f"[WARN] ASS extracted but conversion failed. ASS file kept at: {ass_output}"
            )

    # --- mkvextract fallback (only for .mkv files) ---
    if video.lower().endswith(".mkv"):
        mkvextract_bin = shutil.which("mkvextract")
        if mkvextract_bin:
            print("   Trying mkvextract (MKVToolNix) for robust extraction...")
            mkvmerge_bin = shutil.which("mkvmerge")
            if not mkvmerge_bin:
                print("   [mkvextract] mkvmerge not found; cannot determine track ID.")
            else:
                try:
                    # Get track IDs from mkvmerge identification
                    cmd_id = [mkvmerge_bin, "-J", video]
                    res_id = subprocess.run(
                        cmd_id, capture_output=True, text=True, timeout=30
                    )
                    if res_id.returncode != 0:
                        print("   [mkvextract] mkvmerge -J failed.")
                    else:
                        info = json.loads(res_id.stdout)
                        tracks = info.get("tracks", [])
                        sub_tracks = [t for t in tracks if t.get("type") == "subtitles"]
                        if relative_pos < len(sub_tracks):
                            track_id = sub_tracks[relative_pos].get("id")
                            track_codec = sub_tracks[relative_pos].get(
                                "codec", "unknown"
                            )
                            print(
                                f"   [mkvextract] Track ID = {track_id}, codec = {track_codec}"
                            )

                            # Use a temporary file for raw extraction
                            with tempfile.NamedTemporaryFile(
                                suffix=".raw", delete=False
                            ) as tmp:
                                tmp_name = tmp.name

                            cmd_extract = [
                                mkvextract_bin,
                                "tracks",
                                video,
                                f"{track_id}:{tmp_name}",
                            ]
                            print(f"   Running: {' '.join(cmd_extract)}")
                            # Increase timeout to 300 seconds for large tracks
                            res_extract = subprocess.run(
                                cmd_extract, capture_output=True, text=True, timeout=300
                            )

                            if (
                                res_extract.returncode == 0
                                and os.path.exists(tmp_name)
                                and os.path.getsize(tmp_name) > 0
                            ):
                                # Try to convert to SRT
                                if run_ffmpeg(
                                    [
                                        FFMPEG_BIN,
                                        "-y",
                                        "-i",
                                        tmp_name,
                                        "-c:s",
                                        "srt",
                                        output_path,
                                    ],
                                    f"Convert mkvextract output ({track_codec}) to SRT",
                                ):
                                    os.unlink(tmp_name)
                                    print(
                                        f"[SUCCESS] mkvextract + ffmpeg succeeded. Saved to: {output_path}"
                                    )
                                    return output_path
                                else:
                                    # Keep raw file for manual processing
                                    ext = (
                                        ".sup"
                                        if "pgs" in track_codec.lower()
                                        else ".sub"
                                    )
                                    raw_output = os.path.join(
                                        dir_name, f"{base_name}.{lang_code}{ext}"
                                    )
                                    shutil.move(tmp_name, raw_output)
                                    print(
                                        f"[WARN] mkvextract extracted a raw subtitle stream that could not be converted to SRT."
                                    )
                                    print(
                                        f"       The raw file was saved as: {raw_output}"
                                    )
                                    print(
                                        f"       You may need OCR (e.g., Subtitle Edit) to convert it to text."
                                    )
                                    return None
                            else:
                                err = res_extract.stderr.strip()
                                print(
                                    f"   [mkvextract failed] {err[:1000] if err else 'Unknown error'}"
                                )
                                if os.path.exists(tmp_name):
                                    os.unlink(tmp_name)
                        else:
                            print(
                                f"   [mkvextract] Could not map stream position {relative_pos} to MKV track ID."
                            )
                except subprocess.TimeoutExpired:
                    print(f"   [mkvextract] Extraction timed out after 300 seconds.")
                    print(
                        f"   The file may be very large or corrupted. Try remuxing with:"
                    )
                    print(f"   mkvmerge -o fixed.mkv '{video}'")
                except Exception as e:
                    print(f"   [mkvextract exception] {e}")
        else:
            print(
                "   mkvextract not found. Install 'mkvtoolnix' for better MKV subtitle support."
            )

    # Cleanup
    if os.path.exists(output_path):
        os.remove(output_path)
    print("[ERROR] All extraction methods failed.")
    print(
        "[HINT] The subtitle stream may be bitmap‑based (e.g., PGS) and cannot be converted to SRT without OCR."
    )
    return None
