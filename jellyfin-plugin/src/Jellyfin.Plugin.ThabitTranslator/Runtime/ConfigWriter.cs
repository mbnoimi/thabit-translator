using System;
using System.IO;
using System.Linq;
using System.Text;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>
/// Renders the plugin settings into the <c>thabit_translator.conf</c> the Python
/// library reads. Regenerated whenever settings change and before every run, so
/// the plugin XML stays the single source of truth.
/// </summary>
public static class ConfigWriter
{
    /// <summary>Absolute path of the generated file.</summary>
    public static string ConfigFilePath(Plugin plugin)
        => Path.Combine(plugin.DataFolder, "thabit_translator.conf");

    /// <summary>Cache directory handed to the library (vosk models, argos packs, ...).</summary>
    public static string CacheDirectory(Plugin plugin)
        => Path.Combine(plugin.DataFolder, "home", ".cache", "thabit_translator");

    /// <summary>Writes the file when its content differs from the current settings.</summary>
    public static void Write(Plugin plugin, PluginConfiguration config)
    {
        var path = ConfigFilePath(plugin);
        var rendered = Render(config, CacheDirectory(plugin));

        try
        {
            if (File.Exists(path) && string.Equals(File.ReadAllText(path), rendered, StringComparison.Ordinal))
            {
                return;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, rendered, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Never let a config write abort a subtitle fetch; the library then runs
            // with whatever is already on disk.
            System.Diagnostics.Debug.WriteLine($"Could not write {path}: {ex.Message}");
        }
    }

    /// <summary>
    /// Renders the exact INI shape of
    /// <c>src/core/thabit_translator/thabit_translator.conf.template</c>. Kept pure for unit tests.
    /// </summary>
    public static string Render(PluginConfiguration config, string cacheDir)
    {
        var targets = LanguageCodes.ParseList(config.TargetLanguages);
        var primaryTarget = targets.FirstOrDefault() ?? "ar";
        var source = LanguageCodes.ToIso2(config.SourceLanguage) ?? "en";
        var providers = NormalizeProviders(config.EnabledProviders);

        var sb = new StringBuilder();
        sb.AppendLine("[opensubtitles]");
        sb.AppendLine("api_key = " + Value(config.OpenSubtitlesApiKey));
        sb.AppendLine("username = " + Value(config.OpenSubtitlesUsername));
        sb.AppendLine("password = " + Value(config.OpenSubtitlesPassword));
        sb.AppendLine("default_language = " + primaryTarget);
        sb.AppendLine("user_agent = ThabitTranslator v1.0");
        sb.AppendLine();
        sb.AppendLine("[subdl]");
        sb.AppendLine("api_key = " + Value(config.SubDlApiKey));
        sb.AppendLine();
        sb.AppendLine("[subsource]");
        sb.AppendLine("api_key = " + Value(config.SubSourceApiKey));
        sb.AppendLine();
        sb.AppendLine("[settings]");
        sb.AppendLine("default_source_lang = " + source);
        sb.AppendLine("default_target_lang = " + primaryTarget);
        sb.AppendLine("verbose = true");
        sb.AppendLine("cache_dir = " + Value(cacheDir));
        sb.AppendLine();
        sb.AppendLine("[providers]");
        sb.AppendLine("enabled = " + providers);
        return sb.ToString();
    }

    /// <summary>Sanitized provider list; only the three the library knows are kept.</summary>
    public static string NormalizeProviders(string? csv)
    {
        var known = new[] { "opensubtitles", "subdl", "subsource" };
        var requested = (csv ?? string.Empty)
            .Split(new[] { ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(static p => p.Trim().ToLowerInvariant())
            .ToArray();

        var kept = known.Where(requested.Contains).ToArray();
        return kept.Length == 0 ? string.Join(",", known) : string.Join(",", kept);
    }

    private static string Value(string? raw)
        => (raw ?? string.Empty).Replace("\r", " ").Replace("\n", " ").Trim();
}
