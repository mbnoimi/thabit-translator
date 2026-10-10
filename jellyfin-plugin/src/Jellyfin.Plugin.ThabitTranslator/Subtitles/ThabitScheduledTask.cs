using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Data.Enums;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using MediaBrowser.Controller.Dto;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Model.Configuration;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.IO;
using MediaBrowser.Model.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ThabitTranslator.Subtitles;

/// <summary>
/// Sweeps libraries and fills in missing subtitles through the same queue the
/// subtitle menu uses, so a manual request and a sweep never race each other.
/// </summary>
public sealed class ThabitScheduledTask : IScheduledTask, IConfigurableScheduledTask
{
    private readonly ILibraryManager _libraryManager;
    private readonly IProviderManager _providerManager;
    private readonly IFileSystem _fileSystem;
    private readonly ThabitJobQueue _queue;
    private readonly ILogger<ThabitScheduledTask> _logger;

    public ThabitScheduledTask(
        ILibraryManager libraryManager,
        IProviderManager providerManager,
        IFileSystem fileSystem,
        ThabitJobQueue queue,
        ILogger<ThabitScheduledTask> logger)
    {
        _libraryManager = libraryManager;
        _providerManager = providerManager;
        _fileSystem = fileSystem;
        _queue = queue;
        _logger = logger;
    }

    /// <inheritdoc />
    public string Name => "Fill in missing subtitles (Thabit Translator)";

    /// <inheritdoc />
    public string Description =>
        "Downloads, extracts or speech-to-texts subtitles for movies and episodes that "
        + "do not have one in the configured languages, then translates them. Uses the "
        + "ThabitTranslator Python library running inside this container.";

    /// <inheritdoc />
    public string Category => "Library";

    /// <inheritdoc />
    public string Key => "ThabitFillMissingSubtitles";

    /// <inheritdoc />
    public bool IsHidden => false;

    /// <inheritdoc />
    public bool IsEnabled => true;

    /// <inheritdoc />
    public bool IsLogged => true;

    /// <inheritdoc />
    public async Task ExecuteAsync(IProgress<double> progress, CancellationToken cancellationToken)
    {
        var token = cancellationToken;

        var config = Plugin.Instance?.Configuration;
        if (config is null)
        {
            _logger.LogInformation("Plugin configuration unavailable, nothing to do.");
            return;
        }

        if (!config.AutoTaskEnabled)
        {
            _logger.LogInformation("Automatic subtitle sweep is disabled in the plugin settings.");
            return;
        }

        var targets = LanguageCodes.ParseList(config.TargetLanguages);
        if (targets.Count == 0)
        {
            _logger.LogInformation("No target languages configured, nothing to do.");
            return;
        }

        var plugin = Plugin.Instance!;
        try
        {
            // Install-time preparation (extract the embedded library, probe python);
            // cached after the first call, so this stays cheap per sweep.
            var python = plugin.PrepareRuntime(logToServer: true);
            if ((python is null || !python.Available) && string.IsNullOrWhiteSpace(config.PythonPath))
            {
                // Stock image, no system python: fetch the portable build first, so
                // the sweep does not abort before any job could bootstrap it.
                var bootstrap = await PortablePython.EnsureAsync(
                    plugin.DataFolder,
                    message => _logger.LogInformation("[thabit] {Message}", message),
                    progress).ConfigureAwait(false);
                if (bootstrap is null)
                {
                    python = plugin.PrepareRuntime(logToServer: true);
                }
                else
                {
                    _logger.LogWarning("Portable Python bootstrap: {Reason}", bootstrap);
                }
            }

            if (python is null || !python.Available)
            {
                _logger.LogError(
                    "Python is not available, aborting the sweep: {Reason}",
                    python?.Error ?? "the interpreter probe did not run");
                return;
            }

            plugin.EnsureLibraryConfig();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            _logger.LogError("Plugin data folder is not usable, aborting the sweep: {Message}", ex.Message);
            return;
        }

        var candidates = Collect(targets, config, token);
        if (candidates.Count == 0)
        {
            _logger.LogInformation("Nothing to do: every candidate already has a matching subtitle.");
            progress.Report(1);
            return;
        }

        _logger.LogInformation("Sweeping {Count} item/language pair(s).", candidates.Count);

        var done = 0;
        foreach (var (video, language) in candidates)
        {
            token.ThrowIfCancellationRequested();

            var job = new ThabitJob
            {
                ItemId = video.Id,
                VideoPath = video.Path!,
                TargetLang = language,
                SourceLang = LanguageCodes.ToIso2(config.SourceLanguage) ?? "en",
                SttPolicy = NormalizeSttPolicy(config.SttPolicy),
                Force = false
            };

            var localDone = done;
            var jobProgress = new Progress<double>(value =>
                progress.Report((localDone + Math.Clamp(value, 0, 1)) / candidates.Count));

            ThabitJobResult result;
            try
            {
                result = await _queue.EnqueueAsync(job, jobProgress, token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Sweep failed for {Video}", video.Name);
                result = ThabitJobResult.Failure(ex.Message);
            }

            done++;
            progress.Report((double)done / candidates.Count);

            if (result.Ok)
            {
                // The file is already where Jellyfin expects it; make the item pick it up.
                _providerManager.QueueRefresh(
                    video.Id,
                    new MetadataRefreshOptions(new DirectoryService(_fileSystem)),
                    RefreshPriority.High);
            }
            else
            {
                _logger.LogWarning("No {Language} subtitle for {Video}: {Reason}", language, video.Name, result.Reason);
            }
        }

        _logger.LogInformation("Sweep finished: {Done}/{Total} processed.", done, candidates.Count);
    }

    /// <inheritdoc />
    public IEnumerable<TaskTriggerInfo> GetDefaultTriggers()
    {
        return new[]
        {
            new TaskTriggerInfo
            {
                Type = TaskTriggerInfoType.IntervalTrigger,
                IntervalTicks = TimeSpan.FromHours(24).Ticks
            }
        };
    }

    private List<(Video Video, string Language)> Collect(
        IReadOnlyList<string> targets,
        PluginConfiguration config,
        CancellationToken token)
    {
        var candidates = new List<(Video, string)>();
        var max = config.AutoTaskMaxItems;

        foreach (var library in _libraryManager.RootFolder.Children.OfType<Folder>())
        {
            token.ThrowIfCancellationRequested();

            var options = _libraryManager.GetLibraryOptions(library);
            if (options.DisabledSubtitleFetchers?.Contains(ThabitSubtitleProvider.ProviderName, StringComparer.OrdinalIgnoreCase) == true)
            {
                _logger.LogInformation("Library {Library} has Thabit Translator disabled.", library.Name);
                continue;
            }

            var languages = ResolveLibraryLanguages(targets, options);
            if (languages.Count == 0)
            {
                continue;
            }

            var query = new InternalItemsQuery
            {
                MediaTypes = new[] { MediaType.Video },
                IncludeItemTypes = new[] { BaseItemKind.Movie, BaseItemKind.Episode },
                Recursive = true,
                Parent = library,
                IsVirtualItem = false,
                DtoOptions = new DtoOptions(true)
            };

            foreach (var item in _libraryManager.GetItemList(query).OfType<Video>())
            {
                token.ThrowIfCancellationRequested();

                if (string.IsNullOrEmpty(item.Path) || !File.Exists(item.Path))
                {
                    continue;
                }

                foreach (var language in languages)
                {
                    if (HasSubtitle(item, language))
                    {
                        continue;
                    }

                    candidates.Add((item, language));
                    if (max > 0 && candidates.Count >= max)
                    {
                        return candidates;
                    }
                }
            }
        }

        return candidates;
    }

    private IReadOnlyList<string> ResolveLibraryLanguages(IReadOnlyList<string> targets, LibraryOptions options)
    {
        if (!Plugin.Instance!.Configuration.AutoTaskUseLibraryLanguages
            || options.SubtitleDownloadLanguages is not { Length: > 0 } configured)
        {
            return targets;
        }

        // The library option uses 3-letter codes; the plugin settings use 2-letter.
        var configuredIso2 = configured
            .Select(LanguageCodes.ToIso2)
            .Where(static code => code is not null)
            .Select(static code => code!)
            .ToHashSet(StringComparer.Ordinal);

        var intersection = targets.Where(configuredIso2.Contains).ToList();
        if (intersection.Count == 0)
        {
            _logger.LogDebug(
                "Library wants {Wanted} but the plugin targets {Targets}; using the plugin languages.",
                string.Join(",", configured),
                string.Join(",", targets));
            return targets;
        }

        return intersection;
    }

    /// <summary>
    /// Skip when the library already has the language: our own <c>&lt;stem&gt;.&lt;lang&gt;.srt</c>
    /// (the name rule this project guarantees) or any matching subtitle stream.
    /// </summary>
    private static bool HasSubtitle(Video video, string iso2)
    {
        var directory = Path.GetDirectoryName(video.Path);
        var stem = Path.GetFileNameWithoutExtension(video.Path);
        if (string.IsNullOrEmpty(directory) || string.IsNullOrEmpty(stem))
        {
            return false;
        }

        var iso3 = LanguageCodes.ToIso3(iso2);
        foreach (var code in new[] { iso2, iso3 }.Where(static c => !string.IsNullOrEmpty(c)).Distinct(StringComparer.OrdinalIgnoreCase))
        {
            if (File.Exists(Path.Combine(directory, stem + "." + code + ".srt")))
            {
                return true;
            }
        }

        foreach (var stream in video.GetMediaStreams())
        {
            if (stream.Type != MediaStreamType.Subtitle || !stream.IsTextSubtitleStream)
            {
                continue;
            }

            if (LanguageCodes.ToIso2(stream.Language) == iso2)
            {
                return true;
            }
        }

        return false;
    }

    private static string NormalizeSttPolicy(string? policy)
        => policy?.Trim().ToLowerInvariant() switch
        {
            "yes" => "yes",
            "ask" => "ask",
            _ => "no"
        };
}
