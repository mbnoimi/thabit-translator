using System;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>
/// The id we put into <c>RemoteSubtitleInfo.Id</c> and read back in
/// <c>GetSubtitles</c>.
///
/// Jellyfin rewrites every search result id to
/// <c>md5(providerName.ToLowerInvariant()) + "_" + ourId</c> before it ever
/// reaches a client, and splits it back on the FIRST underscore
/// (<c>SubtitleManager.GetRemoteSubtitles</c>), so:
///   * our part may not start with an underscore and should stay underscore-free
///     so the host prefix never gets ambiguous;
///   * it must be short, URL safe and self describing.
///
/// Format: <c>srt-&lt;iso2&gt;-&lt;32 hex digits of the item guid&gt;</c>
/// </summary>
public static class SubtitleId
{
    private const string Prefix = "srt-";

    /// <summary>Encodes the language + item into a provider id.</summary>
    public static string Encode(string iso2, Guid itemId)
    {
        if (string.IsNullOrWhiteSpace(iso2) || iso2.Length != 2 || !char.IsLetter(iso2[0]) || !char.IsLetter(iso2[1]))
        {
            throw new ArgumentException("Language must be a 2 letter ISO-639-1 code.", nameof(iso2));
        }

        return Prefix + iso2.ToLowerInvariant() + "-" + itemId.ToString("N");
    }

    /// <summary>
    /// Decodes an id produced by <see cref="Encode"/>, transparently stripping the
    /// provider hash Jellyfin prepends. Returns null when the id is not ours.
    /// </summary>
    public static (string Iso2, Guid ItemId)? Decode(string? id)
    {
        if (string.IsNullOrEmpty(id))
        {
            return null;
        }

        var own = StripHostPrefix(id);
        if (!own.StartsWith(Prefix, StringComparison.Ordinal))
        {
            return null;
        }

        var rest = own[Prefix.Length..];
        var separator = rest.LastIndexOf('-');
        if (separator != 2)
        {
            return null;
        }

        var iso2 = rest[..separator];
        if (iso2.Length != 2 || !char.IsAsciiLetter(iso2[0]) || !char.IsAsciiLetter(iso2[1]))
        {
            return null;
        }

        var guidText = rest[(separator + 1)..];
        if (guidText.Length != 32 || !Guid.TryParseExact(guidText, "N", out var itemId))
        {
            return null;
        }

        return (iso2.ToLowerInvariant(), itemId);
    }

    /// <summary>
    /// Removes the host's <c>md5hash_</c> provider prefix when present.
    /// </summary>
    public static string StripHostPrefix(string id)
    {
        // md5 hex (32 chars) + '_' is what SubtitleManager prepends.
        if (id.Length > 33 && id[32] == '_' && IsHex(id.AsSpan(0, 32)))
        {
            return id[33..];
        }

        return id;
    }

    private static bool IsHex(ReadOnlySpan<char> text)
    {
        foreach (var c in text)
        {
            if (!char.IsAsciiHexDigit(c))
            {
                return false;
            }
        }

        return true;
    }
}
