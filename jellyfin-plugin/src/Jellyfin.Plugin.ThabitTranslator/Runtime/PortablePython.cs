using System;
using System.Formats.Tar;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>A pinned python-build-standalone release asset (name + sha256 of the .tar.gz).</summary>
public sealed record PortablePythonAsset(string FileName, string Sha256);

/// <summary>
/// Downloads and installs a pinned, SHA-256-verified CPython into the plugin data
/// folder, so a catalog install works on the official Jellyfin image (which ships
/// no python) without root, apt or a custom image.
///
/// Layout: <c>&lt;plugin data&gt;/runtime/python/bin/python3</c> - a sibling of
/// <c>library/</c>, <c>.venv_thabit/</c> and the rest, living entirely on the
/// config volume. The install is staged in a temp folder and only moved into
/// place after the interpreter has been probed successfully, so a crash
/// mid-download never leaves a half Python behind.
///
/// The release tag, file names and checksums are pinned in this file: the
/// download is verified against them before anything is unpacked.
/// </summary>
public static class PortablePython
{
    /// <summary>python-build-standalone release the assets below were taken from.</summary>
    public const string ReleaseTag = "20261009";

    /// <summary>CPython version shipped in that release.</summary>
    public const string CPythonVersion = "3.12.15";

    /// <summary>Folder inside the plugin data folder the interpreter lives in.</summary>
    public const string RuntimeFolderName = "runtime";

    /// <summary>Download URL of a pinned asset (also useful for manual downloads).</summary>
    public static string DownloadUrl(PortablePythonAsset asset)
        => $"https://github.com/astral-sh/python-build-standalone/releases/download/{ReleaseTag}/{asset.FileName}";

    /// <summary>Asset for the current machine's CPU architecture.</summary>
    /// <exception cref="PlatformNotSupportedException">No build for that architecture.</exception>
    public static PortablePythonAsset AssetFor(Architecture architecture)
        => architecture switch
        {
            Architecture.X64 => new PortablePythonAsset(
                "cpython-3.12.15+20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                "ffa8f85f1b56e88b687f08d743d817015524b86a9365fd921243132b80f1a36d"),
            Architecture.Arm64 => new PortablePythonAsset(
                "cpython-3.12.15+20261009-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
                "2e1ba0e7f22e01c534caebf7f09e7820322bc0d055271d3434af671e8400f96f"),
            _ => throw new PlatformNotSupportedException(
                $"no portable Python build for {architecture} - install python3 manually or set an interpreter path")
        };

    /// <summary>Absolute path of the interpreter inside the given plugin data folder.</summary>
    public static string InterpreterPath(string dataFolder)
        => Path.Combine(RootPath(dataFolder), "python", "bin", "python3");

    /// <summary>Root folder the portable Python is installed under.</summary>
    public static string RootPath(string dataFolder)
        => Path.Combine(dataFolder, RuntimeFolderName);

    /// <summary>True when an interpreter file exists (runnability is probed separately).</summary>
    public static bool IsInstalled(string dataFolder)
        => File.Exists(InterpreterPath(dataFolder));

    /// <summary>
    /// Ensures a runnable interpreter exists in the plugin data folder. Returns
    /// null when ready, otherwise the reason. Never throws except on cancellation.
    ///
    /// Downloads are throttled by a cooldown after a failure so a scheduled sweep
    /// cannot hammer a dead network; <paramref name="force"/> (the explicit
    /// "Prepare runtime" button) bypasses it.
    /// </summary>
    public static async Task<string?> EnsureAsync(
        string dataFolder,
        Action<string>? log = null,
        IProgress<double>? progress = null,
        bool force = false,
        CancellationToken cancellationToken = default)
    {
        var interpreter = InterpreterPath(dataFolder);
        if (File.Exists(interpreter))
        {
            var (version, _, probeError) = PythonRuntime.ProbeCandidate(interpreter);
            if (version is not null)
            {
                return null;
            }

            // Present but unrunnable (foreign architecture, deleted shared libs, ...).
            log?.Invoke($"The portable Python at {interpreter} does not run here ({probeError}) - reinstalling.");
            try
            {
                DeleteDirectory(RootPath(dataFolder));
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                return $"the broken portable Python could not be removed: {ex.Message}";
            }
        }

        if (!OperatingSystem.IsLinux())
        {
            return "a portable Python can only be installed on Linux - install python3 manually or set an interpreter path";
        }

        if (RuntimeInformation.OSArchitecture is not (Architecture.X64 or Architecture.Arm64))
        {
            return $"no portable Python build for {RuntimeInformation.OSArchitecture} - install python3 manually or set an interpreter path";
        }

        if (!force && WithinCooldown())
        {
            return "a recent portable Python download failed - wait a few minutes before retrying (or press Prepare to force it)";
        }

        await Gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (File.Exists(interpreter))
            {
                return null; // raced another bootstrapper
            }

            var root = RootPath(dataFolder);
            Directory.CreateDirectory(root);
            CleanupStale(root);

            var archivePath = Path.Combine(root, $"download-{Guid.NewGuid():N}.tar.gz");
            var extractDir = Path.Combine(root, $"extract-{Guid.NewGuid():N}");
            try
            {
                var asset = AssetFor(RuntimeInformation.OSArchitecture);
                log?.Invoke(
                    $"No system python found - downloading portable Python {CPythonVersion} "
                    + $"({asset.FileName}, {ReleaseTag}) into the plugin data folder...");

                await DownloadAsync(asset, archivePath, log, progress, cancellationToken).ConfigureAwait(false);

                var actual = await ComputeSha256Async(archivePath, cancellationToken).ConfigureAwait(false);
                if (!string.Equals(actual, asset.Sha256, StringComparison.Ordinal))
                {
                    throw new InvalidDataException(
                        $"checksum mismatch for {asset.FileName}: got {actual}, expected {asset.Sha256}");
                }

                progress?.Report(0.85);
                log?.Invoke("Unpacking the portable Python...");
                await Task.Run(
                    () =>
                    {
                        ExtractArchive(archivePath, extractDir);
                        EnsureExecutableLayout(Path.Combine(extractDir, "python", "bin"));
                    },
                    cancellationToken).ConfigureAwait(false);

                var staged = Path.Combine(extractDir, "python", "bin", "python3");
                var (version, venvOk, probeError) = PythonRuntime.ProbeCandidate(staged);
                if (version is null)
                {
                    throw new InvalidDataException($"the downloaded Python does not run here: {probeError}");
                }

                if (!venvOk)
                {
                    throw new InvalidDataException("the downloaded Python cannot create virtual environments (ensurepip missing)");
                }

                // Promote: only a fully probed interpreter ever reaches the final path.
                var finalDir = Path.Combine(root, "python");
                if (Directory.Exists(finalDir))
                {
                    Directory.Delete(finalDir, recursive: true);
                }

                Directory.Move(Path.Combine(extractDir, "python"), finalDir);

                progress?.Report(1d);
                log?.Invoke($"Portable Python {version} installed at {finalDir}");
                Interlocked.Exchange(ref s_lastFailureUtcTicks, 0L);
                return null;
            }
            finally
            {
                TryDeleteFile(archivePath);
                TryDeleteDirectory(extractDir);
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException ex)
        {
            // Our own download deadline fired, not the caller's token.
            return ReportFailure(
                $"the portable Python download timed out after {(int)DownloadTimeout.TotalMinutes} minutes ({ex.Message})",
                log);
        }
        catch (Exception ex) when (ex is HttpRequestException
            or IOException
            or UnauthorizedAccessException
            or InvalidDataException
            or CryptographicException
            or PlatformNotSupportedException)
        {
            return ReportFailure($"portable Python bootstrap failed: {ex.Message}", log);
        }
        finally
        {
            Gate.Release();
        }
    }

    /// <summary>
    /// Unpacks the python-build-standalone <c>.tar.gz</c> into
    /// <paramref name="destinationDirectory"/>, preserving symlinks and file modes.
    ///
    /// Done by hand on purpose: a packer quirk in those archives writes some ustar
    /// name fields as <c>name</c> + NUL + a duplicated tail of the name (for
    /// example <c>python/bin/2to3</c>, NUL, <c>in/2to3</c>). GNU tar and CPython's
    /// tarfile stop at the first NUL; System.Formats.Tar only trims *trailing*
    /// NULs, which garbles every such path and makes extraction fail. Every name
    /// is therefore cut at its first NUL before it becomes a path.
    /// </summary>
    public static void ExtractArchive(string archivePath, string destinationDirectory)
    {
        Directory.CreateDirectory(destinationDirectory);
        var root = Path.GetFullPath(destinationDirectory);

        using var file = File.OpenRead(archivePath);
        using var gzip = new GZipStream(file, CompressionMode.Decompress);
        using var reader = new TarReader(gzip);

        while (reader.GetNextEntry() is { } entry)
        {
            var name = FirstSegment(entry.Name);
            if (string.IsNullOrEmpty(name) || name is "." or "./")
            {
                continue;
            }

            var destination = Path.GetFullPath(Path.Combine(root, name));
            if (!destination.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            {
                throw new InvalidDataException($"archive entry escapes the destination: {entry.Name}");
            }

            switch (entry.EntryType)
            {
                case TarEntryType.Directory:
                    Directory.CreateDirectory(destination);
                    break;

                case TarEntryType.RegularFile:
                    EnsureParentDirectory(destination);
                    DeleteIfExists(destination);
                    entry.ExtractToFile(destination, overwrite: true);
                    File.SetUnixFileMode(destination, entry.Mode);
                    break;

                case TarEntryType.SymbolicLink:
                    EnsureParentDirectory(destination);
                    DeleteIfExists(destination);
                    File.CreateSymbolicLink(destination, FirstSegment(entry.LinkName));
                    break;

                case TarEntryType.HardLink:
                    var linkSource = Path.GetFullPath(Path.Combine(root, FirstSegment(entry.LinkName)));
                    if (!linkSource.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.Ordinal))
                    {
                        throw new InvalidDataException($"archive hard link escapes the destination: {entry.Name}");
                    }

                    EnsureParentDirectory(destination);
                    File.Copy(linkSource, destination, overwrite: true);
                    break;

                default:
                    // Nothing on disk (the archive has no extended-header entries;
                    // TarReader does not surface those anyway).
                    break;
            }
        }
    }

    /// <summary>The part of a tar name up to its first NUL, if any.</summary>
    private static string FirstSegment(string rawName)
    {
        var nul = rawName.IndexOf('\0');
        return nul < 0 ? rawName : rawName[..nul];
    }

    private static void EnsureParentDirectory(string path)
    {
        var parent = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(parent))
        {
            Directory.CreateDirectory(parent);
        }
    }

    private static void DeleteIfExists(string path)
    {
        // No-op when missing; removes regular files and even dangling symlinks.
        File.Delete(path);
    }

    /// <summary>
    /// Makes <paramref name="binDir"/> runnable whatever the extractor did: exec
    /// bits for the regular files (tar modes are not always applied) and fresh
    /// <c>python3</c>/<c>python</c> entry points pointing at the versioned binary.
    /// </summary>
    public static void EnsureExecutableLayout(string binDir)
    {
        if (!Directory.Exists(binDir))
        {
            throw new InvalidDataException($"unexpected Python archive layout: no {binDir}");
        }

        string? versionedName = null;
        foreach (var path in Directory.EnumerateFiles(binDir))
        {
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            {
                continue;
            }

            var mode = File.GetUnixFileMode(path);
            File.SetUnixFileMode(
                path,
                mode | UnixFileMode.UserExecute | UnixFileMode.GroupExecute | UnixFileMode.OtherExecute);

            if (Regex.IsMatch(Path.GetFileName(path), @"^python3\.\d+$"))
            {
                versionedName = Path.GetFileName(path);
            }
        }

        if (versionedName is null)
        {
            throw new InvalidDataException($"unexpected Python archive layout: no versioned interpreter in {binDir}");
        }

        foreach (var (entry, target) in new[] { ("python3", versionedName), ("python", "python3") })
        {
            var entryPath = Path.Combine(binDir, entry);
            try
            {
                File.Delete(entryPath); // a stale regular file, symlink or nothing at all
            }
            catch (IOException)
            {
                // Best effort: CreateSymbolicLink below is the real failure if it matters.
            }

            File.CreateSymbolicLink(entryPath, target);
        }
    }

    /// <summary>SHA-256 of a file as lowercase hex.</summary>
    public static async Task<string> ComputeSha256Async(string path, CancellationToken cancellationToken = default)
    {
        await using var stream = File.OpenRead(path);
        var hash = await SHA256.HashDataAsync(stream, cancellationToken).ConfigureAwait(false);
        return Convert.ToHexStringLower(hash);
    }

    private static readonly SemaphoreSlim Gate = new(1, 1);
    private static readonly TimeSpan DownloadTimeout = TimeSpan.FromMinutes(10);
    private static readonly TimeSpan FailureCooldown = TimeSpan.FromMinutes(5);
    private static long s_lastFailureUtcTicks;

    private static readonly HttpClient Http = CreateHttpClient();

    private static HttpClient CreateHttpClient()
    {
        var client = new HttpClient(new SocketsHttpHandler
        {
            AutomaticDecompression = System.Net.DecompressionMethods.None,
            ConnectTimeout = TimeSpan.FromSeconds(30),
            MaxAutomaticRedirections = 10
        })
        {
            // The caller's linked token owns the deadline; see DownloadAsync.
            Timeout = Timeout.InfiniteTimeSpan
        };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("ThabitTranslator-Jellyfin-Plugin");
        return client;
    }

    private static async Task DownloadAsync(
        PortablePythonAsset asset,
        string destination,
        Action<string>? log,
        IProgress<double>? progress,
        CancellationToken cancellationToken)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(DownloadTimeout);

        using var response = await Http
            .GetAsync(DownloadUrl(asset), HttpCompletionOption.ResponseHeadersRead, deadline.Token)
            .ConfigureAwait(false);
        response.EnsureSuccessStatusCode();

        var total = response.Content.Headers.ContentLength;
        await using var body = await response.Content.ReadAsStreamAsync(deadline.Token).ConfigureAwait(false);
        await using var target = File.Create(destination);

        var buffer = new byte[81920];
        long copied = 0;
        var lastTenth = -1;
        int read;
        while ((read = await body.ReadAsync(buffer, deadline.Token).ConfigureAwait(false)) > 0)
        {
            await target.WriteAsync(buffer.AsMemory(0, read), deadline.Token).ConfigureAwait(false);
            copied += read;

            if (total is > 0)
            {
                var fraction = (double)copied / total.Value;
                progress?.Report(fraction * 0.85);

                var tenth = (int)(fraction * 10);
                if (tenth > lastTenth)
                {
                    lastTenth = tenth;
                    log?.Invoke($"Downloading portable Python... {(int)(fraction * 100)}%");
                }
            }
        }

        if (total is > 0 && copied != total.Value)
        {
            throw new InvalidDataException($"truncated download: {copied} of {total.Value} bytes");
        }
    }

    private static string ReportFailure(string message, Action<string>? log)
    {
        Interlocked.Exchange(ref s_lastFailureUtcTicks, DateTime.UtcNow.Ticks);
        log?.Invoke(message);
        return message;
    }

    private static bool WithinCooldown()
    {
        var ticks = Interlocked.Read(ref s_lastFailureUtcTicks);
        if (ticks == 0)
        {
            return false;
        }

        return DateTime.UtcNow - new DateTime(ticks, DateTimeKind.Utc) < FailureCooldown;
    }

    /// <summary>Drops download/extract leftovers of a previous crashed install.</summary>
    private static void CleanupStale(string root)
    {
        foreach (var file in Directory.EnumerateFiles(root, "download-*.tar.gz"))
        {
            TryDeleteFile(file);
        }

        foreach (var directory in Directory.EnumerateDirectories(root, "extract-*"))
        {
            TryDeleteDirectory(directory);
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Best effort: temp cleanup must never fail the install.
        }
    }

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            if (Directory.Exists(path))
            {
                Directory.Delete(path, recursive: true);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Best effort: temp cleanup must never fail the install.
        }
    }

    private static void DeleteDirectory(string path)
    {
        if (Directory.Exists(path))
        {
            Directory.Delete(path, recursive: true);
        }
    }
}
