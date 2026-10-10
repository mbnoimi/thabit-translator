using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>One unit of work: produce <c>&lt;stem&gt;.&lt;iso2&gt;.srt</c> for one video.</summary>
public sealed record ThabitJob
{
    public required Guid ItemId { get; init; }

    public required string VideoPath { get; init; }

    /// <summary>ISO-639-1 target language of the produced subtitle.</summary>
    public required string TargetLang { get; init; }

    public string SourceLang { get; init; } = "en";

    public bool Force { get; init; } = true;

    /// <summary>Library STT policy: "no" / "yes" / "ask" (never blocks: there is no terminal).</summary>
    public string SttPolicy { get; init; } = "no";

    /// <summary>
    /// Manual download: point the library at a symlink inside the plugin's staging
    /// folder instead of the real video, so nothing is ever written next to (or
    /// deleted from) the user's media and the host cannot save a duplicate.
    /// </summary>
    public bool Staging { get; init; }

    /// <summary>Deduplication key: the same item + language never runs twice at once.</summary>
    public string Key => ItemId.ToString("N") + ":" + TargetLang;
}

/// <summary>Outcome of a CLI run.</summary>
public sealed record ThabitJobResult(bool Ok, string? Path, string? Reason, byte[]? Bytes = null)
{
    public static ThabitJobResult Failure(string reason) => new(false, null, reason);
}

/// <summary>
/// The plugin as a launcher for the ThabitTranslator CLI: spawns
/// <c>python3 &lt;extracted library&gt;/thabit_translator/__main__.py auto &lt;video&gt; ...</c>,
/// streams its stdout (classified by <see cref="ThabitOutputParser"/>) into the
/// Jellyfin log and an <see cref="IProgress{T}"/>, and turns the parsed
/// <c>Output:</c> path plus the exit code into a <see cref="ThabitJobResult"/>.
///
/// The CLI bootstraps its own venv (<c>&lt;plugin data&gt;/.venv_thabit</c>) on
/// first use - that install runs inside this process invocation and its pip output
/// is streamed to the log like everything else.
/// </summary>
public sealed class ThabitRunner
{
    private readonly ILogger<ThabitRunner> _logger;

    public ThabitRunner(ILogger<ThabitRunner> logger)
    {
        _logger = logger;
    }

    /// <summary>Runs the pipeline for one job. Throws <see cref="OperationCanceledException"/> on cancel.</summary>
    public async Task<ThabitJobResult> RunAsync(ThabitJob job, IProgress<double>? progress, CancellationToken cancellationToken)
    {
        var plugin = Plugin.Instance;
        if (plugin is null)
        {
            return ThabitJobResult.Failure("the plugin is not loaded");
        }

        if (!File.Exists(job.VideoPath))
        {
            return ThabitJobResult.Failure($"video not found: {job.VideoPath}");
        }

        var preflight = await PrepareAsync(plugin, progress, cancellationToken).ConfigureAwait(false);
        if (preflight is not null)
        {
            return ThabitJobResult.Failure(preflight);
        }

        string configPath;
        try
        {
            configPath = plugin.EnsureLibraryConfig();
            EnsureLayout(plugin);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            return ThabitJobResult.Failure($"plugin data folder is not usable: {ex.Message}");
        }

        string? stagingDir = null;
        var runPath = job.VideoPath;
        if (job.Staging)
        {
            try
            {
                stagingDir = Path.Combine(plugin.DataFolder, "staging", job.Key.Replace(':', '_'));
                if (Directory.Exists(stagingDir))
                {
                    Directory.Delete(stagingDir, recursive: true);
                }

                Directory.CreateDirectory(stagingDir);

                // A symlink (not a copy: episodes are hundreds of MB). The library names
                // its output after the path it was handed, so the subtitle lands in
                // staging and the real video is only ever read.
                var stagedPath = Path.Combine(stagingDir, Path.GetFileName(job.VideoPath));
                File.CreateSymbolicLink(stagedPath, job.VideoPath);
                runPath = stagedPath;
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
            {
                stagingDir = null;
                return ThabitJobResult.Failure($"could not stage {job.VideoPath}: {ex.Message}");
            }
        }

        // Removed on every exit path (success, failure, cancel): the staging folder is
        // ours, the media folder is not.
        using var stagingScope = new StagingScope(stagingDir, _logger);

        var python = PythonRuntime.Resolve(plugin.Configuration);
        var arguments = new List<string>
        {
            Path.Combine(LibraryBundle.LibraryPath(plugin), LibraryBundle.EntryScript),
            "auto",
            runPath,
            "-t", job.TargetLang,
            "-s", job.SourceLang,
            "-c", configPath,
            "--stt", job.SttPolicy
        };

        if (job.Force)
        {
            arguments.Add("--force");
        }

        var run = await ExecuteCliAsync(plugin, python.Path!, arguments, progress, cancellationToken).ConfigureAwait(false);

        var output = run.Parser.OutputPath;
        if (output is not null)
        {
            if (!File.Exists(output))
            {
                return ThabitJobResult.Failure($"the CLI reported {output} but no such file exists");
            }

            if (stagingDir is not null)
            {
                var stagedRoot = Path.GetFullPath(stagingDir) + Path.DirectorySeparatorChar;
                if (!Path.GetFullPath(output).StartsWith(stagedRoot, StringComparison.Ordinal))
                {
                    // The library must never write next to the real video on this path:
                    // reading it back would ship a file we did not just produce.
                    return ThabitJobResult.Failure($"the library wrote outside staging: {output}");
                }

                byte[] bytes;
                try
                {
                    bytes = await File.ReadAllBytesAsync(output, cancellationToken).ConfigureAwait(false);
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
                {
                    return ThabitJobResult.Failure($"could not read the produced subtitle {output}: {ex.Message}");
                }

                progress?.Report(1d);
                return new ThabitJobResult(true, output, null, bytes);
            }

            progress?.Report(1d);
            return new ThabitJobResult(true, output, null);
        }

        return ThabitJobResult.Failure(
            run.Parser.FirstError
            ?? (run.ExitCode == 0
                ? "no subtitle produced (the CLI finished without an output file)"
                : $"the CLI exited with code {run.ExitCode}"));
    }

    /// <summary>
    /// Installs/warms the runtime: runs the CLI far enough to bootstrap the venv
    /// (and verify the system requirements), without touching any video.
    /// Returns null when the runtime is ready, otherwise the failure reason.
    /// </summary>
    public async Task<string?> PrepareRuntimeAsync(IProgress<double>? progress, CancellationToken cancellationToken)
    {
        var plugin = Plugin.Instance;
        if (plugin is null)
        {
            return "the plugin is not loaded";
        }

        return await PrepareAsync(plugin, progress, cancellationToken, runCli: true).ConfigureAwait(false);
    }

    /// <summary>
    /// Shared preflight: extraction + python probe + data folder layout.
    /// Returns null when ready, otherwise the reason; when <paramref name="runCli"/>
    /// is set, also runs <c>thabit_translator/__main__.py -h</c> (which forces the venv
    /// bootstrap and the system requirements check).
    /// </summary>
    private async Task<string?> PrepareAsync(
        Plugin plugin,
        IProgress<double>? progress,
        CancellationToken cancellationToken,
        bool runCli = false)
    {
        try
        {
            var extraction = plugin.EnsureLibrary();
            if (extraction.Extracted)
            {
                Log(LogLevel.Information, $"Extracted library ({extraction.FileCount} files) -> {extraction.Path}");
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            return $"plugin data folder is not usable: {ex.Message}";
        }

        var config = plugin.Configuration;
        var python = PythonRuntime.Resolve(config);
        string? bootstrapFailure = null;
        if (!python.Available && string.IsNullOrWhiteSpace(config.PythonPath))
        {
            // Stock Jellyfin images ship no python: fetch the pinned portable build
            // into the plugin data folder (throttled after failures; the explicit
            // Prepare button forces a retry), then probe again - the interpreter
            // path is a candidate in PythonRuntime now.
            bootstrapFailure = await PortablePython.EnsureAsync(
                plugin.DataFolder,
                message => Log(LogLevel.Information, message),
                progress,
                force: runCli,
                cancellationToken).ConfigureAwait(false);

            if (bootstrapFailure is null)
            {
                python = PythonRuntime.Resolve(config);
            }
        }

        if (!python.Available)
        {
            var hint = string.IsNullOrWhiteSpace(config.PythonPath)
                ? "install python3 (with the venv module), set an interpreter path, or press "
                    + "'Prepare Python runtime' on the configuration page to download a portable Python."
                : "set a working interpreter path on the configuration page.";
            var reason = "python not found - " + hint
                + (bootstrapFailure is null ? string.Empty : " Portable bootstrap: " + bootstrapFailure + ".")
                + " " + python.Error;
            Log(LogLevel.Error, reason);
            return reason;
        }

        if (!runCli)
        {
            return null;
        }

        try
        {
            EnsureLayout(plugin);
            plugin.EnsureLibraryConfig();
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or InvalidOperationException)
        {
            return $"plugin data folder is not usable: {ex.Message}";
        }

        var arguments = new List<string>
        {
            Path.Combine(LibraryBundle.LibraryPath(plugin), LibraryBundle.EntryScript),
            "-h"
        };

        var run = await ExecuteCliAsync(plugin, python.Path!, arguments, progress, cancellationToken).ConfigureAwait(false);
        if (run.ExitCode == 0)
        {
            progress?.Report(1d);
            return null;
        }

        return run.Parser.FirstError ?? $"the CLI exited with code {run.ExitCode}";
    }

    /// <summary>Spawns the CLI and pumps its output; throws <see cref="OperationCanceledException"/> on cancel.</summary>
    private async Task<(int ExitCode, ThabitOutputParser Parser)> ExecuteCliAsync(
        Plugin plugin,
        string python,
        IReadOnlyList<string> arguments,
        IProgress<double>? progress,
        CancellationToken cancellationToken)
    {
        var startInfo = CreateStartInfo(plugin, python, arguments);
        using var process = new Process { StartInfo = startInfo };
        var parser = new ThabitOutputParser(value => progress?.Report(value));

        void HandleLine(string line, bool fromStderr)
        {
            if (fromStderr)
            {
                // Python's own tracebacks land here; argos/stanza chatter does too, so
                // only the exception markers are worth error-level attention.
                if (parser.ClassifyStderr(line))
                {
                    _logger.LogError("[thabit:err] {Line}", line);
                    ThabitLogBuffer.Add(DateTimeOffset.Now, "error", line);
                }
                else
                {
                    _logger.LogDebug("[thabit:err] {Line}", line);
                }

                return;
            }

            var evt = parser.Classify(line);
            if (evt is null)
            {
                return;
            }

            switch (evt.Level)
            {
                case ThabitLogLevel.Warn:
                    _logger.LogWarning("[thabit] {Message}", evt.Message);
                    break;
                case ThabitLogLevel.Error:
                    _logger.LogError("[thabit] {Message}", evt.Message);
                    break;
                default:
                    _logger.LogInformation("[thabit] {Message}", evt.Message);
                    break;
            }

            ThabitLogBuffer.Add(DateTimeOffset.Now, evt.Level.ToString().ToLowerInvariant(), evt.Message);
        }

        try
        {
            if (!process.Start())
            {
                return (-1, parser);
            }
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or FileNotFoundException or DirectoryNotFoundException)
        {
            _logger.LogError(ex, "Could not start {Python}", python);
            return (-1, SeedError(parser, $"cannot execute {python}: {ex.Message}"));
        }

        // No terminal: the CLI must never block on a prompt (its --stt ask falls
        // back to "skip" on a non-tty anyway).
        process.StandardInput.Close();

        var stdout = ReadAsync(process.StandardOutput, line => HandleLine(line, false), cancellationToken);
        var stderr = ReadAsync(process.StandardError, line => HandleLine(line, true), cancellationToken);

        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            TryKill(process);
            await DrainAsync(stdout, stderr).ConfigureAwait(false);
            throw;
        }

        await DrainAsync(stdout, stderr).ConfigureAwait(false);
        return (process.ExitCode, parser);
    }

    private static ProcessStartInfo CreateStartInfo(Plugin plugin, string python, IReadOnlyList<string> arguments)
    {
        var home = HomeDirectory(plugin);
        var startInfo = new ProcessStartInfo
        {
            FileName = python,
            WorkingDirectory = home,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };

        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        // The base image preloads jemalloc into every child process; a Python child
        // gains nothing from it and only gets noise.
        startInfo.Environment.Remove("LD_PRELOAD");

        startInfo.Environment["HOME"] = home;
        startInfo.Environment["XDG_DATA_HOME"] = Path.Combine(home, ".local", "share");
        startInfo.Environment["XDG_CONFIG_HOME"] = Path.Combine(home, ".config");
        startInfo.Environment["XDG_CACHE_HOME"] = Path.Combine(home, ".cache");
        startInfo.Environment["ARGOS_DEVICE_TYPE"] = "cpu";
        startInfo.Environment["CUDA_VISIBLE_DEVICES"] = string.Empty;
        startInfo.Environment["PYTORCH_NO_CUDA_MEMORY_CACHING"] = "1";
        startInfo.Environment["PYTHONUNBUFFERED"] = "1";
        startInfo.Environment["PYTHONIOENCODING"] = "utf-8";

        return startInfo;
    }

    private void Log(LogLevel level, string message)
    {
        _logger.Log(level, "[thabit] {Message}", message);
        ThabitLogBuffer.Add(DateTimeOffset.Now, level == LogLevel.Error ? "error" : "info", message);
    }

    private static string HomeDirectory(Plugin plugin)
        => Path.Combine(plugin.DataFolder, "home");

    private static void EnsureLayout(Plugin plugin)
    {
        var home = HomeDirectory(plugin);
        Directory.CreateDirectory(home);
        Directory.CreateDirectory(Path.Combine(home, ".local", "share"));
        Directory.CreateDirectory(Path.Combine(home, ".config"));
        Directory.CreateDirectory(Path.Combine(home, ".cache"));
        Directory.CreateDirectory(Path.Combine(plugin.DataFolder, "staging"));
    }

    /// <summary>Makes a launch failure visible as the parser's first error.</summary>
    private static ThabitOutputParser SeedError(ThabitOutputParser parser, string reason)
    {
        parser.Classify("[ERROR] " + reason);
        return parser;
    }

    private static Task ReadAsync(StreamReader reader, Action<string> onLine, CancellationToken cancellationToken)
        => Task.Run(async () =>
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                string? line;
                try
                {
                    line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (ObjectDisposedException)
                {
                    return;
                }

                if (line is null)
                {
                    return;
                }

                try
                {
                    onLine(line);
                }
                catch (Exception ex)
                {
                    // A single malformed line must never take the run down.
                    Debug.WriteLine(ex);
                }
            }
        }, CancellationToken.None);

    private static async Task DrainAsync(Task stdout, Task stderr)
    {
        try
        {
            await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            Debug.WriteLine(ex);
        }
    }

    private static void TryKill(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex) when (ex is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            Debug.WriteLine(ex);
        }
    }

    /// <summary>Deletes a staging folder (symlink + produced subtitle) when the run ends.</summary>
    private sealed class StagingScope : IDisposable
    {
        private readonly string? _directory;
        private readonly ILogger _logger;

        public StagingScope(string? directory, ILogger logger)
        {
            _directory = directory;
            _logger = logger;
        }

        public void Dispose()
        {
            if (_directory is null)
            {
                return;
            }

            try
            {
                if (Directory.Exists(_directory))
                {
                    Directory.Delete(_directory, recursive: true);
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                _logger.LogWarning("Could not clean staging folder {Path}: {Message}", _directory, ex.Message);
            }
        }
    }
}
