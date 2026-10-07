"""Decide whether a search result can belong to the video we asked for.

Episode/movie parsing is delegated to guessit (subliminal/bazarr/flexget use it)
instead of hand-rolled regex - it understands S01E12, 4x02, "Season 4 Episode 2",
scene releases, multi-part episodes and plain movie names.
"""
import os

from guessit import guessit


class _NoMatch:
    """Sentinel: the search succeeded but found nothing for this video.

    Returned instead of None so the dispatcher knows repeating the same search with
    the next candidate index is pointless (None means "candidate tried and failed").
    """

    def __repr__(self):
        return "<no matching subtitle>"


NO_MATCH = _NoMatch()


def _guess(text):
    if not text:
        return {}
    try:
        return guessit(str(text))
    except Exception:
        return {}


def _first(value):
    """guessit returns a list for multi-episode/multi-part names; take the first."""
    return value[0] if isinstance(value, (list, tuple)) else value


def episode_key(text):
    """(season, episode) carried by a filename/title, or None when it has no code."""
    match = _guess(text)
    if match.get("type") != "episode":
        return None
    season = _first(match.get("season"))
    episode = _first(match.get("episode"))
    if season is None or episode is None:
        return None
    try:
        return int(season), int(episode)
    except (TypeError, ValueError):
        return None


def show_title(text):
    """Show/movie title guessit sees: "Monk" from "Monk - S01E12" or any release name."""
    return str(_guess(text).get("title") or "").strip()


def video_stem(path):
    return os.path.splitext(os.path.basename(str(path)))[0]


def feature_episode(feature_details):
    """(season, episode) from an API's feature_details dict, or None."""
    if not feature_details:
        return None
    season = _first(feature_details.get("season_number"))
    episode = _first(feature_details.get("episode_number"))
    try:
        if season is None or episode is None:
            return None
        return int(season), int(episode)
    except (TypeError, ValueError):
        return None


def is_relevant(candidate_name, search_key, by_hash=False, feature_key=None):
    """False when the search key names a specific episode and the candidate is not it.

    - key has no episode code (a movie) -> anything goes
    - API metadata (feature_details season/episode) matches -> yes
    - the candidate's own name matches -> yes (OpenSubtitles sometimes files
      "Monk.S01E12..." under episode 13; the release name is what the user sees)
    - no signal at all -> only trusted when it came from an exact file-hash lookup
    - a signal exists but disagrees -> rejected
    """
    want = episode_key(search_key)
    if want is None:
        return True
    if feature_key is not None and tuple(feature_key) == want:
        return True
    got = episode_key(candidate_name)
    if got is not None:
        return got == want
    if feature_key is None:
        return by_hash
    return False
