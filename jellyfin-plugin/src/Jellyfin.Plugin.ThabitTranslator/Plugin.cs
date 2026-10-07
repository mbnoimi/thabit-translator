using System;
using System.Collections.Generic;
using System.IO;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ThabitTranslator;

/// <summary>
/// Plugin entry point.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    /// <summary>The frozen plugin identity - never regenerate (Jellyfin keys everything off it).</summary>
    public static readonly Guid PluginGuid = new("993545e6-8c2c-4074-bec4-8af2a39a976b");

    /// <summary>
    /// Initializes a new instance of the <see cref="Plugin"/> class.
    /// </summary>
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;

        // Install-time check: probe python and extract the embedded library right
        // when the plugin is installed/loaded. Best effort and never throws - a
        // failure is reported through the status endpoint and the log buffer, and
        // the runner re-attempts both before every run anyway.
        PrepareRuntime(logToServer: false);
    }

    /// <summary>
    /// Install-time preparation: extracts the embedded library (if the build
    /// changed) and probes for a python interpreter. Safe to call repeatedly.
    /// </summary>
    /// <param name="logToServer">Also write one line per outcome to the server log.</param>
    /// <returns>The python probe, when it could run.</returns>
    public PythonRuntimeInfo? PrepareRuntime(bool logToServer)
    {
        try
        {
            var extraction = LibraryBundle.Ensure(this);
            if (extraction.Extracted)
            {
                LogRuntime($"Extracted ThabitTranslator library ({extraction.FileCount} files) -> {extraction.Path}", logToServer);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            LogRuntime($"Library extraction failed: {ex.Message}", logToServer, error: true);
        }

        PythonRuntimeInfo? python = null;
        try
        {
            python = PythonRuntime.Resolve(Configuration);
            if (python.Available)
            {
                LogRuntime($"Python {python.Version} at {python.Path}", logToServer);
            }
            else
            {
                LogRuntime(
                    $"Python not found - install python3 (with the venv module) or set an interpreter path. {python.Error}",
                    logToServer,
                    error: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            LogRuntime($"Python probe failed: {ex.Message}", logToServer, error: true);
        }

        return python;
    }

    private static void LogRuntime(string message, bool logToServer, bool error = false)
    {
        System.Diagnostics.Debug.WriteLine("[thabit] " + message);
        ThabitLogBuffer.Add(DateTimeOffset.Now, error ? "error" : "info", message);

        if (logToServer)
        {
            var factory = ThabitRuntime.GetService<ILoggerFactory>();
            var logger = factory?.CreateLogger("Jellyfin.Plugin.ThabitTranslator");
            if (error)
            {
                logger?.LogWarning("{Message}", message);
            }
            else
            {
                logger?.LogInformation("{Message}", message);
            }
        }
    }

    /// <summary>Gets the current plugin instance.</summary>
    public static Plugin? Instance { get; private set; }

    /// <inheritdoc />
    public override Guid Id => PluginGuid;

    /// <inheritdoc />
    public override string Name => "Thabit Translator";

    /// <inheritdoc />
    public override string Description =>
        "Fetches, extracts and translates subtitles with the ThabitTranslator library: "
        + "a subtitle provider for the per-item download menu plus a scheduled task "
        + "that fills in missing subtitles.";

    /// <inheritdoc />
    public override void UpdateConfiguration(BasePluginConfiguration configuration)
    {
        base.UpdateConfiguration(configuration);

        // Persist the library's own config file alongside the plugin so a run never
        // depends on the XML having been read first.
        ConfigWriter.Write(Instance!, (PluginConfiguration)configuration);
    }

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        // The JavaScript lives inline in the HTML: jellyfin-web inserts the fetched
        // markup into the dashboard and only scripts it created itself are certain to
        // run (every shipped plugin configuration page does the same).
        yield return new PluginPageInfo
        {
            Name = Name,
            DisplayName = Name,
            EmbeddedResourcePath = GetType().Namespace + ".Configuration.configPage.html"
        };
    }

    /// <summary>
    /// Absolute path of the plugin data folder (the plugin's install directory).
    /// </summary>
    public string DataFolder => DataFolderPath;

    /// <summary>
    /// Writes <c>thabit_translator.conf</c> if it is missing or out of date.
    /// Called before every run so a settings change takes effect immediately.
    /// </summary>
    public string EnsureLibraryConfig()
    {
        ConfigWriter.Write(this, Configuration);
        return ConfigWriter.ConfigFilePath(this);
    }

    /// <summary>
    /// Extracts the embedded ThabitTranslator library into the data folder when the
    /// build changed (or it is missing), and returns the extraction outcome.
    /// </summary>
    public LibraryBundleResult EnsureLibrary() => LibraryBundle.Ensure(this);
}
