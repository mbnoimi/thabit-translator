using System;
using System.Collections.Generic;
using System.Linq;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>
/// Minimal ISO-639-1 (2 letter) &lt;-&gt; ISO-639-2/T (3 letter) mapping for the
/// languages this plugin deals with, including the ISO-639-2/B aliases.
///
/// Jellyfin hands us 3-letter codes ("ara"), the ThabitTranslator library works
/// with 2-letter codes ("ar"), and the saved file name must be the 2-letter form
/// (&lt;stem&gt;.ar.srt).
/// </summary>
public static class LanguageCodes
{
    private static readonly Dictionary<string, string> Iso2To3 = new(StringComparer.Ordinal)
    {
        ["af"] = "afr", ["am"] = "amh", ["ar"] = "ara", ["az"] = "aze", ["be"] = "bel",
        ["bg"] = "bul", ["bn"] = "ben", ["bs"] = "bos", ["ca"] = "cat", ["cs"] = "ces",
        ["cy"] = "cym", ["da"] = "dan", ["de"] = "deu", ["el"] = "ell", ["en"] = "eng",
        ["es"] = "spa", ["et"] = "est", ["eu"] = "eus", ["fa"] = "fas", ["fi"] = "fin",
        ["fr"] = "fra", ["ga"] = "gle", ["gl"] = "glg", ["gu"] = "guj", ["he"] = "heb",
        ["hi"] = "hin", ["hr"] = "hrv", ["hu"] = "hun", ["hy"] = "hye", ["id"] = "ind",
        ["ig"] = "ibo", ["is"] = "isl", ["it"] = "ita", ["ja"] = "jpn", ["ka"] = "kat",
        ["kk"] = "kaz", ["km"] = "khm", ["kn"] = "kan", ["ko"] = "kor", ["ky"] = "kir",
        ["lo"] = "lao", ["lt"] = "lit", ["lv"] = "lav", ["mk"] = "mkd", ["ml"] = "mal",
        ["mn"] = "mon", ["mr"] = "mar", ["ms"] = "msa", ["my"] = "mya", ["ne"] = "nep",
        ["nl"] = "nld", ["no"] = "nor", ["pa"] = "pan", ["pl"] = "pol", ["pt"] = "por",
        ["ro"] = "ron", ["ru"] = "rus", ["si"] = "sin", ["sk"] = "slk", ["sl"] = "slv",
        ["sn"] = "sna", ["so"] = "som", ["sq"] = "sqi", ["sr"] = "srp", ["st"] = "sot",
        ["sv"] = "swe", ["sw"] = "swa", ["ta"] = "tam", ["te"] = "tel", ["tg"] = "tgk",
        ["th"] = "tha", ["tk"] = "tuk", ["tn"] = "tsn", ["tr"] = "tur", ["ug"] = "uig",
        ["uk"] = "ukr", ["ur"] = "urd", ["uz"] = "uzb", ["vi"] = "vie", ["xh"] = "xho",
        ["yo"] = "yor", ["zh"] = "zho", ["zu"] = "zul"
    };

    // ISO-639-2/B (bibliographic) codes -> the /T (terminologic) code we use.
    private static readonly Dictionary<string, string> Iso3Aliases = new(StringComparer.Ordinal)
    {
        ["alb"] = "sqi", ["arm"] = "hye", ["baq"] = "eus", ["bur"] = "mya",
        ["chi"] = "zho", ["cze"] = "ces", ["dut"] = "nld", ["fre"] = "fra",
        ["geo"] = "kat", ["ger"] = "deu", ["gre"] = "ell", ["ice"] = "isl",
        ["mac"] = "mkd", ["mao"] = "mri", ["may"] = "msa", ["per"] = "fas",
        ["rum"] = "ron", ["slo"] = "slk", ["tib"] = "bod", ["wel"] = "cym"
    };

    private static readonly Dictionary<string, string> Iso3To2 =
        Iso2To3.ToDictionary(static pair => pair.Value, static pair => pair.Key, StringComparer.Ordinal);

    /// <summary>
    /// Normalizes any ISO-639 code (2 or 3 letters, any case, /B or /T form) to
    /// the 2-letter form, or null when it is not a language code we know.
    /// </summary>
    public static string? ToIso2(string? code)
    {
        var normalized = Normalize(code);
        if (normalized is null)
        {
            return null;
        }

        if (normalized.Length == 2)
        {
            return normalized;
        }

        if (Iso3To2.TryGetValue(normalized, out var iso2))
        {
            return iso2;
        }

        if (Iso3Aliases.TryGetValue(normalized, out var alias) && Iso3To2.TryGetValue(alias, out var fromAlias))
        {
            return fromAlias;
        }

        return null;
    }

    /// <summary>
    /// Normalizes any ISO-639 code to the 3-letter /T form, or null when unknown.
    /// </summary>
    public static string? ToIso3(string? code)
    {
        var normalized = Normalize(code);
        if (normalized is null)
        {
            return null;
        }

        if (normalized.Length == 3)
        {
            // Keep unknown 3-letter codes as-is: the caller prefers showing what
            // Jellyfin sent over dropping the language entirely.
            return Iso3Aliases.TryGetValue(normalized, out var alias) ? alias : normalized;
        }

        return Iso2To3.TryGetValue(normalized, out var iso3) ? iso3 : null;
    }

    /// <summary>
    /// The 3-letter form to show in Jellyfin's UI, falling back to the input.
    /// </summary>
    public static string DisplayThreeLetter(string? code)
        => ToIso3(code) ?? code?.Trim().ToLowerInvariant() ?? string.Empty;

    /// <summary>
    /// Parses a comma/semicolon/whitespace separated list of language codes into
    /// distinct, normalized 2-letter codes (input order kept).
    /// </summary>
    public static IReadOnlyList<string> ParseList(string? csv)
    {
        if (string.IsNullOrWhiteSpace(csv))
        {
            return Array.Empty<string>();
        }

        var result = new List<string>();
        foreach (var raw in csv.Split(new[] { ',', ';', ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries))
        {
            var iso2 = ToIso2(raw);
            if (iso2 is not null && !result.Contains(iso2, StringComparer.Ordinal))
            {
                result.Add(iso2);
            }
        }

        return result;
    }

    private static string? Normalize(string? code)
    {
        if (string.IsNullOrWhiteSpace(code))
        {
            return null;
        }

        var trimmed = code.Trim().ToLowerInvariant();
        return trimmed.Length is 2 or 3 ? trimmed : null;
    }
}
