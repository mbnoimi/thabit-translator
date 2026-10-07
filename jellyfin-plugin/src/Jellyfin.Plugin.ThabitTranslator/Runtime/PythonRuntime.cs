using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>Result of probing a python interpreter (install-time check).</summary>
public sealed record PythonRuntimeInfo
{
    /// <summary>Interpreter to launch, or null when none works.</summary>
    public string? Path { get; init; }

    /// <summary>Version string of the interpreter, e.g. "3.12.3".</summary>
    public string? Version { get; init; }

    /// <summary>True when the interpreter can create a venv (the CLI's bootstrap needs it).</summary>
    public bool VenvOk { get; init; }

    /// <summary>Human readable reason when <see cref="Path"/> is null.</summary>
    public string? Error { get; init; }

    /// <summary>Candidates that were tried, in order.</summary>
    public IReadOnlyList<string> Tried { get; init; } = Array.Empty<string>();

    /// <summary>True when a launchable interpreter was found.</summary>
    public bool Available => Path is not null;
}

/// <summary>
/// The install-time "does this server have Python?" check: probes the configured
/// interpreter first, then <c>python3</c>/<c>python</c> on PATH and the usual
/// absolute locations. A successful probe is cached for the process (keyed on the
/// configured path); failures are never cached, so installing python later works
/// without a restart.
///
/// The interpreter only has to *host* the CLI: <c>thabit_translator/__main__.py</c> then
/// bootstraps its own venv at <c>&lt;plugin data&gt;/.venv_thabit</c>.
/// </summary>
public static class PythonRuntime
{
    private const int ProbeTimeoutMs = 10_000;

    /// <summary>
    /// One spawn: prints "&lt;version&gt; &lt;can_create_venv&gt;".
    /// Checks <c>ensurepip</c> rather than <c>venv</c>: on Debian/Ubuntu the venv
    /// module exists even without the python3-venv package, but creating a venv
    /// fails exactly when ensurepip is missing.
    /// </summary>
    private const string ProbeCode =
        "import sys\n"
        + "try:\n"
        + "    import venv, ensurepip\n"
        + "    venv_ok = 'True'\n"
        + "except Exception:\n"
        + "    venv_ok = 'False'\n"
        + "print('%d.%d.%d %s' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], venv_ok))\n";

    private static readonly object Gate = new();
    private static PythonRuntimeInfo? s_cached;
    private static string? s_cachedKey;

    /// <summary>Interpreters to try, in order (configured path first).</summary>
    public static IReadOnlyList<string> Candidates(string? configured)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var candidates = new List<string>(5);

        void Add(string? candidate)
        {
            var trimmed = candidate?.Trim();
            if (!string.IsNullOrEmpty(trimmed) && seen.Add(trimmed))
            {
                candidates.Add(trimmed);
            }
        }

        Add(configured);
        Add("python3");
        Add("python");
        Add("/usr/bin/python3");
        Add("/usr/local/bin/python3");
        return candidates;
    }

    /// <summary>
    /// Resolves the interpreter for this configuration (cached while it keeps
    /// working). Never throws.
    /// </summary>
    public static PythonRuntimeInfo Resolve(PluginConfiguration config)
    {
        var key = config.PythonPath?.Trim() ?? string.Empty;
        lock (Gate)
        {
            if (s_cached is not null && s_cached.Available && string.Equals(s_cachedKey, key, StringComparison.Ordinal))
            {
                return s_cached;
            }

            var info = Probe(key);
            if (info.Available)
            {
                s_cached = info;
                s_cachedKey = key;
            }

            return info;
        }
    }

    /// <summary>Probe for tests and for a forced re-check; never throws.</summary>
    public static PythonRuntimeInfo Probe(string? configured)
    {
        var candidates = Candidates(configured);
        string? firstError = null;

        foreach (var candidate in candidates)
        {
            var (version, venvOk, error) = ProbeCandidate(candidate);
            if (version is null)
            {
                firstError ??= $"{candidate}: {error}";
                continue;
            }

            return new PythonRuntimeInfo
            {
                Path = candidate,
                Version = version,
                VenvOk = venvOk,
                Tried = candidates
            };
        }

        return new PythonRuntimeInfo
        {
            Error = firstError is null
                ? "no python interpreter found (tried: " + string.Join(", ", candidates) + ")"
                : $"no working python interpreter (tried {candidates.Count}: {firstError})",
            Tried = candidates
        };
    }

    /// <summary>Runs the probe against one candidate; version is null when it failed.</summary>
    public static (string? Version, bool VenvOk, string? Error) ProbeCandidate(string candidate)
    {
        using var process = new Process();
        process.StartInfo = new ProcessStartInfo
        {
            FileName = candidate,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        process.StartInfo.ArgumentList.Add("-c");
        process.StartInfo.ArgumentList.Add(ProbeCode);

        try
        {
            if (!process.Start())
            {
                return (null, false, "could not start");
            }
        }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException or FileNotFoundException or DirectoryNotFoundException)
        {
            return (null, false, ex.Message);
        }

        using var timeout = new CancellationTokenSource(ProbeTimeoutMs);
        try
        {
            var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
            var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
            if (!process.WaitForExit(ProbeTimeoutMs))
            {
                TryKill(process);
                return (null, false, $"timed out after {ProbeTimeoutMs / 1000}s");
            }

            var output = stdout.GetAwaiter().GetResult().Trim();
            var errorOutput = stderr.GetAwaiter().GetResult().Trim();

            if (process.ExitCode != 0)
            {
                return (null, false, string.IsNullOrEmpty(errorOutput) ? $"exit code {process.ExitCode}" : errorOutput.Split('\n')[0]);
            }

            var parts = output.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length != 2 || !parts[0].Contains('.'))
            {
                return (null, false, $"unexpected probe output: {Truncate(output)}");
            }

            return (parts[0], parts[1] == "True", null);
        }
        catch (Exception ex) when (ex is InvalidOperationException or IOException or OperationCanceledException)
        {
            TryKill(process);
            return (null, false, ex.Message);
        }
    }

    /// <summary>True when the CLI's own venv already exists in the plugin data folder.</summary>
    public static bool VenvReady(Plugin plugin)
    {
        var venvDir = Path.Combine(plugin.DataFolder, ".venv_thabit");
        return File.Exists(Path.Combine(venvDir, "bin", "python3"))
            || File.Exists(Path.Combine(venvDir, "bin", "python"));
    }

    private static string Truncate(string value)
        => value.Length <= 120 ? value : value[..120] + "…";

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
}
