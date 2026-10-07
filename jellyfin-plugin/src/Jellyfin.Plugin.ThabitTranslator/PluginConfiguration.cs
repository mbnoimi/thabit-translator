using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.ThabitTranslator;

/// <summary>
/// Plugin settings. Serialized as XML to
/// {PluginConfigurationsPath}/Jellyfin.Plugin.ThabitTranslator.xml by Jellyfin.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
    // ---- provider credentials (rendered into thabit_translator.conf) ----

    public string OpenSubtitlesApiKey { get; set; } = string.Empty;

    public string OpenSubtitlesUsername { get; set; } = string.Empty;

    public string OpenSubtitlesPassword { get; set; } = string.Empty;

    public string SubDlApiKey { get; set; } = string.Empty;

    public string SubSourceApiKey { get; set; } = string.Empty;

    /// <summary>Comma separated ISO-639-1 target languages, e.g. "ar,en".</summary>
    public string TargetLanguages { get; set; } = "ar";

    /// <summary>ISO-639-1 language the downloaded/generate subtitle is expected to be in.</summary>
    public string SourceLanguage { get; set; } = "en";

    /// <summary>Comma separated provider keys: opensubtitles,subdl,subsource.</summary>
    public string EnabledProviders { get; set; } = "opensubtitles,subdl,subsource";

    /// <summary>
    /// What to do when providers, embedded tracks and translation all failed:
    /// "no" skips the video, "yes" runs Vosk speech-to-text, "ask" behaves like
    /// "no" unless the process has a terminal (a scheduled run never does).
    /// </summary>
    public string SttPolicy { get; set; } = "ask";

    // ---- runtime detection ----

    /// <summary>
    /// Interpreter used to launch the CLI. Empty = auto-detect: try the configured
    /// value, then <c>python3</c>/<c>python</c> on PATH and the usual absolute
    /// locations. The interpreter only hosts <c>thabit_translator/__main__.py</c>;
    /// the CLI bootstraps its own venv at <c>&lt;plugin data&gt;/.venv_thabit</c>.
    /// </summary>
    public string PythonPath { get; set; } = string.Empty;

    // ---- scheduled task ----

    public bool AutoTaskEnabled { get; set; } = true;

    /// <summary>Maximum items processed per sweep; 0 means unlimited.</summary>
    public int AutoTaskMaxItems { get; set; } = 0;

    /// <summary>
    /// When true, a library's own "Download subtitles in these languages"
    /// setting narrows the languages used for that library.
    /// </summary>
    public bool AutoTaskUseLibraryLanguages { get; set; } = true;

    /// <summary>Comma separated provider keys the sweep should use (empty = EnabledProviders).</summary>
    public string AutoTaskProviders { get; set; } = string.Empty;
}
