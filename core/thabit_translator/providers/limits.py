"""Shared bookkeeping for provider limits: quota, throttling, rejected credentials.

A provider answering 429/403 is not saying "this subtitle does not exist" - it is
saying *your account* is out of quota or blocked for now. Every provider records
that here, the dispatcher then skips the provider for the rest of the run instead
of spending up to 30 more doomed HTTP calls per video on it, and every message
names the cause.

A short `Retry-After` is worth waiting out (one retry); a longer one is not, since
a folder run would stall, so the provider is blocked for the run instead.
"""
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

# provider key (as used in [providers] enabled) -> (reason, monotonic deadline or
# None for "the rest of this run", kind: "limit" or "config")
_BLOCKED = {}

# notice keys already printed this run (a folder must not repeat them per video)
_ANNOUNCED = set()

PROVIDER_LABELS = {
    "opensubtitles": "OpenSubtitles",
    "subdl": "SubDL",
    "subsource": "SubSource",
}

# Longest pause we ever sit through to retry a throttled call.
MAX_RETRY_WAIT = 30


def label(provider):
    """Display name, e.g. "subdl" -> "SubDL"."""
    return PROVIDER_LABELS.get(provider, provider)


def _to_seconds(raw):
    """Retry-After style value (delta-seconds, epoch or HTTP-date) to seconds."""
    try:
        value = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return 0.0
        if when is None:
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        value = when.timestamp() - time.time()
    if value > 1e12:                      # epoch milliseconds
        value = value / 1000.0 - time.time()
    elif value > 1e9:                     # epoch seconds
        value -= time.time()
    return max(0.0, value)


def retry_after(resp):
    """Seconds until a limit clears, from Retry-After / X-RateLimit-Reset (0 if unknown)."""
    headers = getattr(resp, "headers", None)
    if not headers:
        return 0.0
    for name in ("Retry-After", "X-RateLimit-Reset"):
        raw = (headers.get(name) or "").strip()
        if raw:
            seconds = _to_seconds(raw)
            if seconds:
                return seconds
    return 0.0


def wait_then_retry(resp):
    """Sleep off a short throttle; True when the caller should repeat the request."""
    seconds = retry_after(resp)
    if not 0 < seconds <= MAX_RETRY_WAIT:
        return False
    print(
        f"[INFO] The provider is throttling us - waiting {int(seconds) + 1}s, retrying once"
    )
    time.sleep(seconds + 1)
    return True


def _human(seconds):
    if seconds < 90:
        return f"{int(seconds) + 1}s"
    if seconds < 5400:
        return f"{int(seconds / 60) + 1}min"
    return f"{round(seconds / 3600)}h"


def announce(key, line):
    """Print a per-run notice at most once; False when it was already shown.

    A 124-video folder must not repeat the same three blocked-provider lines for
    every video, but the reason still has to be on screen at least once.
    """
    if key in _ANNOUNCED:
        return False
    _ANNOUNCED.add(key)
    print(line)
    return True


def block(provider, reason, resp=None, kind="limit"):
    """Mark `provider` unusable for the rest of the run, printing the cause once.

    `kind` is "limit" (quota/rate limit, worth telling the user about when a video
    falls through to STT) or "config" (no or rejected credentials - a setup
    problem, not something a retry can fix).
    """
    wait = retry_after(resp) if resp is not None else 0.0
    text = reason if wait <= 0 else f"{reason} - retry in about {_human(wait)}"
    deadline = time.monotonic() + wait if 0 < wait <= MAX_RETRY_WAIT else None
    entry = (text, deadline, kind)
    if _BLOCKED.get(provider) == entry:
        return
    _BLOCKED[provider] = entry
    print(f"[WARN] {label(provider)}: {text}")


def is_blocked(provider):
    """True while the provider is over a limit; a short one expires by itself."""
    entry = _BLOCKED.get(provider)
    if not entry:
        return False
    deadline = entry[1]
    if deadline is not None and time.monotonic() >= deadline:
        del _BLOCKED[provider]
        print(f"[INFO] {label(provider)}: the rate limit is over - trying it again")
        return False
    return True


def blocked_reason(provider):
    entry = _BLOCKED.get(provider)
    return entry[0] if entry else ""


def blocked_providers(kind=None):
    """Blocked providers (one whose window passed drops out), optionally by kind."""
    blocked = []
    for provider in list(_BLOCKED):
        if not is_blocked(provider):
            continue
        if kind is None or _BLOCKED[provider][2] == kind:
            blocked.append(provider)
    return blocked