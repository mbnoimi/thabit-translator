import os

from . import opensubtitles as _opensubtitles
from . import subdl as _subdl
from . import subsource as _subsource
from .opensubtitles import download_opensubtitles, quota_exhausted, quota_reset_label
from .subdl import download_subdl
from .subsource import download_subsource
from .limits import announce, blocked_providers, blocked_reason, is_blocked
from core.match import NO_MATCH

_quota_notice_shown = False


def _is_valid_srt(path):
    """Check if a subtitle file is valid SRT."""
    if not path or not os.path.exists(path):
        return False
    if os.path.getsize(path) == 0:
        return False
    try:
        import srt
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        subtitles = list(srt.parse(content))
        return bool(subtitles)
    except:
        return False


def download_subtitle(video_path, config, language, query=None, use_hash=True, 
                      provider_list=None, select_index=None, auto_select=True,
                      interactive=True):
    """
    Download subtitle from available providers with selection support.
    For each provider: first try hash, then retry with query if hash failed.
    Tries multiple subtitles from search results until valid one found.
    
    Args:
        select_index: int or None - 0-based index to auto-select subtitle
        auto_select: bool - if True, auto-pick best match when multiple results
        interactive: bool - if False, providers never prompt on stdin (auto/batch runs)
    """
    if provider_list is None:
        enabled = config['providers'].get('enabled', 'opensubtitles,subdl,subsource')
        provider_list = [p.strip() for p in enabled.split(',') if p.strip()]

    global _quota_notice_shown
    if "opensubtitles" in provider_list and quota_exhausted():
        provider_list = [p for p in provider_list if p != "opensubtitles"]
        if not _quota_notice_shown:
            _quota_notice_shown = True
            until = quota_reset_label()
            print(
                "[INFO] OpenSubtitles skipped: your account's daily download limit "
                f"{'until ' + until if until else 'reached'} "
                f"- trying: {', '.join(provider_list) or 'none'}"
            )
    
    # A provider over an API/rate limit is dropped for the rest of the run: its
    # limit is not a missing subtitle, and re-asking only spends the quota.
    for prov in blocked_providers():
        if prov in provider_list:
            provider_list = [p for p in provider_list if p != prov]
            announce(
                f"skip:{prov}",
                f"[INFO] {prov} skipped: {blocked_reason(prov)} "
                f"- trying: {', '.join(provider_list) or 'none'}"
            )

    providers = {
        'opensubtitles': download_opensubtitles,
        'subdl': download_subdl,
        'subsource': download_subsource
    }

    # Only OpenSubtitles can search by file hash. SubDL and SubSource match on
    # title/episode alone, so the "failed, retry by query" pass below would send
    # the very same request again - doubling the load on providers that rate
    # limit us, for a result that cannot differ.
    supports_hash = {
        'opensubtitles': _opensubtitles.SUPPORTS_HASH,
        'subdl': _subdl.SUPPORTS_HASH,
        'subsource': _subsource.SUPPORTS_HASH,
    }
    
    result = None
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    
    for prov in provider_list:
        if prov not in providers:
            continue
        
        if is_blocked(prov):
            continue
        
        if config['settings'].get('verbose'):
            print(f"\n[INFO] Trying provider: {prov}")
        
        # Try multiple subtitle results from this provider
        for attempt in range(15):
            # A quota or rate limit can hit mid-loop; stop instead of 14 no-op calls
            if prov == "opensubtitles" and quota_exhausted():
                break
            if is_blocked(prov):
                break
            idx = attempt
            
            # First: by hash
            search_query = query if query else None
            result = providers[prov](video_path, config, language, search_query, 
                                    use_hash, select_index=idx, 
                                    auto_select=False,
                                    interactive=interactive)
            first_empty = result is NO_MATCH

            if not first_empty:
                if result and _is_valid_srt(result):
                    return result
                if result and os.path.exists(result):
                    os.remove(result)
            
            # If failed, try query for same index (only where a hash pass exists)
            second_empty = None
            if use_hash and not query and supports_hash.get(prov, False):
                if prov == "opensubtitles" and quota_exhausted():
                    break
                if is_blocked(prov):
                    break
                if config['settings'].get('verbose'):
                    print(f"[INFO] {prov} trying query...")
                result = providers[prov](video_path, config, language, video_name, 
                                        False, select_index=idx, 
                                        auto_select=False,
                                        interactive=interactive)
                second_empty = result is NO_MATCH
                if not second_empty:
                    if result and _is_valid_srt(result):
                        return result
                    if result and os.path.exists(result):
                        os.remove(result)

            # Search came back empty on every path: a higher index changes nothing
            if first_empty and (second_empty is None or second_empty):
                blocked = is_blocked(prov)
                if config['settings'].get('verbose') and not blocked:
                    print(f"[INFO] {prov}: nothing usable for this video")
                break
    
    if config['settings'].get('verbose'):
        print("[ERROR] All providers failed to download subtitle.")
    return None