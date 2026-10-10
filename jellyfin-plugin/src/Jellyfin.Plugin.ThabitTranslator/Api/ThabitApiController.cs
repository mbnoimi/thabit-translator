using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Jellyfin.Plugin.ThabitTranslator.Subtitles;
using MediaBrowser.Common.Api;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ThabitTranslator.Api;

/// <summary>
/// Status/log/trigger endpoints for the configuration page. Route prefix is the
/// plugin's, so the web UI calls <c>/ThabitTranslator/status</c> etc.
/// </summary>
[ApiController]
[Authorize(Policy = Policies.RequiresElevation)]
[Route("ThabitTranslator")]
public sealed class ThabitApiController : ControllerBase
{
    private readonly ThabitJobQueue _queue;
    private readonly ThabitScheduledTask _task;
    private readonly ILogger<ThabitApiController> _logger;
    private int _sweepRunning;
    private int _prepareRunning;

    public ThabitApiController(
        ThabitJobQueue queue,
        ThabitScheduledTask task,
        ILogger<ThabitApiController> logger)
    {
        _queue = queue;
        _task = task;
        _logger = logger;
    }

    /// <summary>Everything the configuration page shows at once.</summary>
    [HttpGet("status")]
    public ActionResult<ThabitStatus> GetStatus()
    {
        var plugin = Plugin.Instance;
        var config = plugin?.Configuration;

        var libraryPath = plugin is null ? string.Empty : LibraryBundle.LibraryPath(plugin);
        var libraryExtracted = plugin is not null
            && System.IO.File.Exists(Path.Combine(libraryPath, LibraryBundle.EntryScript));

        var python = plugin is null ? null : PythonRuntime.Resolve(plugin.Configuration);
        var venvReady = plugin is not null && PythonRuntime.VenvReady(plugin);

        return new ThabitStatus
        {
            Loaded = plugin is not null,
            DataFolder = plugin?.DataFolder,
            TargetLanguages = config?.TargetLanguages ?? string.Empty,
            EnabledProviders = config?.EnabledProviders ?? string.Empty,
            PythonPath = python?.Path ?? config?.PythonPath ?? string.Empty,
            PythonAvailable = python?.Available ?? false,
            PythonDetail = python is null
                ? "plugin not loaded"
                : python.Available
                    ? $"Python {python.Version} at {python.Path}"
                    + (python.VenvOk ? string.Empty : " (cannot create venvs - install python3-venv)")
                    : (python.Error ?? "no python interpreter found")
                    + (string.IsNullOrWhiteSpace(config?.PythonPath)
                        ? " - press 'Prepare Python runtime' to download a portable Python"
                        : string.Empty),
            VenvReady = venvReady,
            LibraryPath = libraryPath,
            LibraryExtracted = libraryExtracted,
            AutoTaskEnabled = config?.AutoTaskEnabled ?? false,
            IsBusy = _queue.IsBusy,
            PendingJobs = _queue.PendingCount
        };
    }

    /// <summary>Tail of the CLI output (provider quotas, translation failures, ...).</summary>
    [HttpGet("logs")]
    public ActionResult<IReadOnlyList<string>> GetLogs([FromQuery] int tail = 200)
        => new(ThabitLogBuffer.Tail(tail));

    /// <summary>Runs one sweep now, in the background (a sweep can take hours).</summary>
    [HttpPost("run")]
    public ActionResult<ThabitRunResponse> RunNow()
    {
        if (Interlocked.CompareExchange(ref _sweepRunning, 1, 0) != 0)
        {
            return new ThabitRunResponse { Started = false, Reason = "a sweep is already running" };
        }

        _logger.LogInformation("Manual sweep requested from the configuration page.");

        var progress = new Progress<double>(value =>
            _logger.LogInformation("Manual sweep progress: {Percent}%", (int)(value * 100)));

        _ = Task.Run(async () =>
        {
            try
            {
                await _task.ExecuteAsync(progress, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Manual sweep failed.");
            }
            finally
            {
                Interlocked.Exchange(ref _sweepRunning, 0);
            }
        });

        return new ThabitRunResponse { Started = true };
    }

    /// <summary>
    /// Warms the Python runtime on demand: runs the CLI far enough to create the
    /// venv and install its packages (first time ~1.6 GB / minutes), streaming the
    /// pip output into the log below. Serialized with real jobs by the queue gate.
    /// </summary>
    [HttpPost("prepare")]
    public ActionResult<ThabitRunResponse> Prepare()
    {
        if (Interlocked.CompareExchange(ref _prepareRunning, 1, 0) != 0)
        {
            return new ThabitRunResponse { Started = false, Reason = "a prepare is already running" };
        }

        _logger.LogInformation("Runtime prepare requested from the configuration page.");

        var progress = new Progress<double>(value =>
            _logger.LogInformation("Prepare progress: {Percent}%", (int)(value * 100)));

        _ = Task.Run(async () =>
        {
            try
            {
                var reason = await _queue.PrepareRuntimeAsync(progress, CancellationToken.None).ConfigureAwait(false);
                if (reason is null)
                {
                    _logger.LogInformation("Runtime is ready.");
                }
                else
                {
                    _logger.LogWarning("Runtime prepare failed: {Reason}", reason);
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Runtime prepare failed.");
            }
            finally
            {
                Interlocked.Exchange(ref _prepareRunning, 0);
            }
        });

        return new ThabitRunResponse { Started = true };
    }
}

/// <summary>Response of <c>GET /ThabitTranslator/status</c>.</summary>
public sealed class ThabitStatus
{
    public bool Loaded { get; set; }

    public string? DataFolder { get; set; }

    public string TargetLanguages { get; set; } = string.Empty;

    public string EnabledProviders { get; set; } = string.Empty;

    /// <summary>Resolved interpreter (or the configured value when nothing was probed yet).</summary>
    public string PythonPath { get; set; } = string.Empty;

    public bool PythonAvailable { get; set; }

    /// <summary>Version + location, or the reason no interpreter works.</summary>
    public string PythonDetail { get; set; } = string.Empty;

    /// <summary>True when the CLI's own venv exists (<c>&lt;data&gt;/.venv_thabit</c>).</summary>
    public bool VenvReady { get; set; }

    /// <summary>Where the embedded library is extracted to.</summary>
    public string LibraryPath { get; set; } = string.Empty;

    public bool LibraryExtracted { get; set; }

    public bool AutoTaskEnabled { get; set; }

    public bool IsBusy { get; set; }

    public int PendingJobs { get; set; }
}

/// <summary>Response of <c>POST /ThabitTranslator/run</c>.</summary>
public sealed class ThabitRunResponse
{
    public bool Started { get; set; }

    public string? Reason { get; set; }
}
