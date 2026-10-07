using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Controller.Subtitles;
using MediaBrowser.Model.Providers;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ThabitTranslator.Subtitles;

/// <summary>
/// Jellyfin's per-item subtitle menu entry.
///
/// <see cref="Search"/> returns a promise, not a lookup: the library has no
/// list-only mode, so the result is "pick this and we will run the pipeline".
/// <see cref="GetSubtitles"/> does the actual work.
/// </summary>
public sealed class ThabitSubtitleProvider : ISubtitleProvider
{
    /// <summary>
    /// Must stay byte-identical between Search and GetSubtitles: the host derives
    /// its lookup key from md5(Name.ToLowerInvariant()).
    /// </summary>
    public const string ProviderName = "Thabit Translator";

    private readonly ILogger<ThabitSubtitleProvider> _logger;
    private readonly ILibraryManager _libraryManager;
    private readonly ThabitJobQueue _queue;

    public ThabitSubtitleProvider(
        ILogger<ThabitSubtitleProvider> logger,
        ILibraryManager libraryManager,
        ThabitJobQueue queue)
    {
        _logger = logger;
        _libraryManager = libraryManager;
        _queue = queue;
    }

    /// <inheritdoc />
    public string Name => ProviderName;

    /// <inheritdoc />
    public IEnumerable<VideoContentType> SupportedMediaTypes { get; } = new[]
    {
        VideoContentType.Movie,
        VideoContentType.Episode
    };

    /// <inheritdoc />
    public Task<IEnumerable<RemoteSubtitleInfo>> Search(SubtitleSearchRequest request, CancellationToken cancellationToken)
    {
        if (request.DisabledSubtitleFetchers.Contains(Name, StringComparer.OrdinalIgnoreCase))
        {
            return Task.FromResult(Enumerable.Empty<RemoteSubtitleInfo>());
        }

        if (request.IsAutomated)
        {
            // Jellyfin's own "Download missing subtitles" task would call us back for
            // every item; the plugin's scheduled task owns that sweep instead (it can
            // dedupe, log quota reasons and refresh the item afterwards).
            _logger.LogDebug("Ignoring automated subtitle search for {Path}", request.MediaPath);
            return Task.FromResult(Enumerable.Empty<RemoteSubtitleInfo>());
        }

        if (string.IsNullOrEmpty(request.MediaPath))
        {
            return Task.FromResult(Enumerable.Empty<RemoteSubtitleInfo>());
        }

        var languages = ResolveLanguages(request);
        if (languages.Count == 0)
        {
            return Task.FromResult(Enumerable.Empty<RemoteSubtitleInfo>());
        }

        var item = _libraryManager.FindByPath(request.MediaPath, false);
        if (item is not Video video)
        {
            _logger.LogDebug("No library item for {Path}", request.MediaPath);
            return Task.FromResult(Enumerable.Empty<RemoteSubtitleInfo>());
        }

        var results = languages
            .Where(lang => ExistingSubtitle(video, lang) is null)
            .Select(lang => CreateInfo(lang, video))
            .ToArray();

        if (results.Length != languages.Count)
        {
            _logger.LogDebug("Hiding {Hidden} language(s) that already exist for {Path}", languages.Count - results.Length, request.MediaPath);
        }

        return Task.FromResult<IEnumerable<RemoteSubtitleInfo>>(results);
    }

    /// <summary>
    /// Builds one entry for the download dialog. The <c>Name</c> must stay stable:
    /// the host keys its lookup on md5(Name.ToLowerInvariant()) + "_" + our id.
    /// </summary>
    private static RemoteSubtitleInfo CreateInfo(string lang, Video video)
    {
        var iso3 = LanguageCodes.DisplayThreeLetter(lang);
        return new RemoteSubtitleInfo
        {
            Id = SubtitleId.Encode(lang, video.Id),
            ProviderName = ProviderName,
            Name = $"{ProviderName} ({iso3}) - download, extract or transcribe, then translate",
            ThreeLetterISOLanguageName = iso3,
            Format = "srt",
            Author = ProviderName,
            Comment = "Runs the ThabitTranslator pipeline inside the Jellyfin container.",
            DateCreated = DateTime.UtcNow
        };
    }

    /// <inheritdoc />
    public async Task<SubtitleResponse> GetSubtitles(string id, CancellationToken cancellationToken)
    {
        var decoded = SubtitleId.Decode(id)
            ?? throw new ArgumentException("Unrecognized subtitle id.", nameof(id));

        var video = _libraryManager.GetItemById<Video>(decoded.ItemId)
            ?? throw new InvalidOperationException($"Item {decoded.ItemId} no longer exists.");

        if (string.IsNullOrEmpty(video.Path) || !File.Exists(video.Path))
        {
            throw new FileNotFoundException("The video file is missing.", video.Path);
        }

        var plugin = Plugin.Instance
            ?? throw new InvalidOperationException("The plugin is not loaded.");

        // The host would save <stem>.<lang>.0.srt over the file that is already there.
        var existing = ExistingSubtitle(video, decoded.Iso2);
        if (existing is not null)
        {
            throw new InvalidOperationException(
                $"A {decoded.Iso2} subtitle already exists at {existing}. Delete it first if you want a new one.");
        }

        var job = new ThabitJob
        {
            ItemId = video.Id,
            VideoPath = video.Path,
            TargetLang = decoded.Iso2,
            SourceLang = LanguageCodes.ToIso2(plugin.Configuration.SourceLanguage) ?? "en",
            SttPolicy = NormalizeSttPolicy(plugin.Configuration.SttPolicy),
            Force = true,
            Staging = true
        };

        _logger.LogInformation("Producing {Language} subtitles for {Path}", decoded.Iso2, video.Path);

        var progress = new LoggerProgress(_logger, video.Path);
        var result = await _queue.EnqueueAsync(job, progress, cancellationToken).ConfigureAwait(false);

        if (!result.Ok || result.Bytes is null)
        {
            throw new InvalidOperationException(result.Reason ?? "ThabitTranslator did not produce a subtitle.");
        }

        _logger.LogInformation("Collected {Bytes} bytes for {Video}", result.Bytes.Length, video.Name);

        return new SubtitleResponse
        {
            // Two letters, so the host saves <stem>.<ar>.srt exactly like the library does.
            Language = decoded.Iso2,
            Format = "srt",
            IsForced = false,
            IsHearingImpaired = false,
            Stream = new MemoryStream(result.Bytes, writable: false)
        };
    }

    /// <summary>
    /// Where the host would save this language, when that file already exists. Both
    /// destinations are checked - the media folder (SaveSubtitlesWithMedia) and the
    /// item's metadata folder - because a hit in either one turns a second download
    /// into <c>&lt;stem&gt;.&lt;lang&gt;.0.srt</c>, and because this project's output
    /// name (<c>&lt;stem&gt;.&lt;lang&gt;.srt</c>) must never be clobbered by a run
    /// nobody asked for.
    /// </summary>
    private static string? ExistingSubtitle(Video video, string iso2)
    {
        var stem = Path.GetFileNameWithoutExtension(video.Path ?? string.Empty);
        if (string.IsNullOrEmpty(stem))
        {
            return null;
        }

        var codes = new[] { iso2, LanguageCodes.ToIso3(iso2) }
            .Where(static code => !string.IsNullOrEmpty(code))
            .Distinct(StringComparer.OrdinalIgnoreCase);

        var folder = video.ContainingFolderPath;
        if (!string.IsNullOrEmpty(folder))
        {
            foreach (var code in codes)
            {
                var candidate = Path.Combine(folder, stem + "." + code + ".srt");
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
        }

        var metadata = Path.Combine(video.GetInternalMetadataPath(), stem + "." + iso2 + ".srt");
        return File.Exists(metadata) ? metadata : null;
    }

    private static IReadOnlyList<string> ResolveLanguages(SubtitleSearchRequest request)
    {
        // The dialog always carries a language; fall back to what the user configured.
        var requested = LanguageCodes.ToIso2(request.TwoLetterISOLanguageName)
            ?? LanguageCodes.ToIso2(request.Language);
        if (requested is not null)
        {
            return new[] { requested };
        }

        var configured = LanguageCodes.ParseList(Plugin.Instance?.Configuration.TargetLanguages);
        return configured;
    }

    private static string NormalizeSttPolicy(string? policy)
        => policy?.Trim().ToLowerInvariant() switch
        {
            "yes" => "yes",
            "ask" => "ask",
            _ => "no"
        };

    private sealed class LoggerProgress : IProgress<double>
    {
        private readonly ILogger _logger;
        private readonly string _path;
        private int _lastBucket = -1;

        public LoggerProgress(ILogger logger, string path)
        {
            _logger = logger;
            _path = path;
        }

        public void Report(double value)
        {
            var bucket = (int)Math.Clamp(value * 10, 0, 10);
            if (bucket == _lastBucket)
            {
                return;
            }

            _lastBucket = bucket;
            _logger.LogInformation("ThabitTranslator {Percent}% for {Path}", bucket * 10, _path);
        }
    }
}
