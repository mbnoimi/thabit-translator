import os
import re
from pathlib import Path
from typing import Optional

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts",
}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS


def _normalize_stem(stem: str) -> str:
    """Case/punctuation-insensitive stem with leading zeros stripped from numbers.

    'Monk - S01E03' and 'Monk - S01E3' both normalize to 'monks1e3'.
    """
    compact = re.sub(r"[^0-9a-z]+", "", stem.lower())
    return re.sub(r"\d+", lambda m: str(int(m.group(0))), compact)


def resolve_input_path(path: Optional[str], verbose: bool = True) -> Optional[str]:
    """Return an existing file path for `path`.

    If `path` itself does not exist, look in the same folder for a media file
    whose name normalizes to the same thing (`Monk - S01E3.mp4` ->
    `Monk - S01E03.mp4`). Outputs are always named after the real file, never
    after a mistyped one.
    """
    if not path:
        return path

    expanded = os.path.expanduser(path)
    if os.path.exists(expanded):
        return expanded

    requested = Path(expanded)
    folder = requested.parent if str(requested.parent) else Path(".")
    if not folder.is_dir():
        if verbose:
            print(f"[WARN] Input not found: {path}")
        return path

    want = _normalize_stem(requested.stem)
    try:
        entries = list(folder.iterdir())
    except OSError:
        if verbose:
            print(f"[WARN] Input not found: {path}")
        return path

    candidates = [
        entry
        for entry in entries
        if entry.is_file()
        and entry.suffix.lower() in MEDIA_EXTENSIONS
        and _normalize_stem(entry.stem) == want
    ]
    if not candidates:
        if verbose:
            print(f"[WARN] Input not found: {path}")
        return path

    def sort_key(candidate: Path):
        return (
            candidate.suffix.lower() != requested.suffix.lower(),
            len(candidate.name),
            candidate.name.lower(),
        )

    chosen = sorted(candidates, key=sort_key)[0]
    if verbose:
        print(f"[INFO] Input not found: {path}")
        print(f"[INFO] Using existing file: {chosen}")
    return str(chosen)
