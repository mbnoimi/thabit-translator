using System;
using System.IO;
using System.Linq;
using System.Reflection;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>Outcome of <see cref="LibraryBundle.Ensure"/>.</summary>
public sealed record LibraryBundleResult(string Path, bool Extracted, int FileCount);

/// <summary>
/// Extracts the ThabitTranslator library embedded in the assembly (every
/// <c>src/core/**/*.py</c>, resource prefix <see cref="ResourcePrefix"/>)
/// into <c>&lt;plugin data&gt;/library/</c>.
///
/// The plugin is self-contained: there is no library baked into the image and no
/// <c>jellyfin_runner.py</c> - the extracted <c>thabit_translator/</c> package is
/// launched as a CLI (its <c>__main__.py</c>), and its own bootstrap creates the
/// venv at <c>&lt;plugin data&gt;/.venv_thabit</c> (a <b>sibling</b> of
/// <c>library/</c>, so re-extracting on a plugin update never touches it).
///
/// Re-extraction is keyed on the assembly's module version id, which changes when
/// either the C# code or the embedded Python files change (deterministic builds:
/// an unchanged build keeps its id, so restarts do not re-extract).
/// </summary>
public static class LibraryBundle
{
    /// <summary>Manifest-resource prefix of every embedded library file.</summary>
    public const string ResourcePrefix = "ThabitLibrary/";

    /// <summary>Folder (inside the plugin data folder) the library is extracted to.</summary>
    public const string LibraryFolderName = "library";

    /// <summary>Entry point launched by the plugin (spawns as a plain script).</summary>
    public const string EntryScript = "thabit_translator/__main__.py";

    private const string MarkerFileName = ".bundle";

    /// <summary>Absolute path of the extracted library directory.</summary>
    public static string LibraryPath(Plugin plugin)
        => Path.Combine(plugin.DataFolder, LibraryFolderName);

    /// <summary>
    /// Extracts the embedded library when it is missing or was produced by a
    /// different build, and returns its path. Throws when the data folder is not
    /// writable or the assembly carries no library resources.
    /// </summary>
    public static LibraryBundleResult Ensure(Plugin plugin)
    {
        var marker = typeof(Plugin).Assembly.ManifestModule.ModuleVersionId.ToString();
        return Ensure(typeof(Plugin).Assembly, plugin.DataFolder, marker);
    }

    /// <summary>Testable core: extract <paramref name="assembly"/>'s library into <paramref name="dataFolder"/>.</summary>
    public static LibraryBundleResult Ensure(Assembly assembly, string dataFolder, string markerValue)
    {
        var libraryDir = Path.Combine(dataFolder, LibraryFolderName);
        var markerPath = Path.Combine(libraryDir, MarkerFileName);
        var entryPath = Path.Combine(libraryDir, EntryScript);

        if (File.Exists(markerPath)
            && string.Equals(File.ReadAllText(markerPath).Trim(), markerValue, StringComparison.Ordinal)
            && File.Exists(entryPath))
        {
            return new LibraryBundleResult(libraryDir, false, CountFiles(assembly));
        }

        var resources = assembly.GetManifestResourceNames()
            .Where(static name => name.StartsWith(ResourcePrefix, StringComparison.Ordinal))
            .OrderBy(static name => name, StringComparer.Ordinal)
            .ToArray();

        if (resources.Length == 0)
        {
            throw new InvalidOperationException(
                "No embedded ThabitTranslator library found in the plugin assembly "
                + "(build must run where src/core/**.py is reachable).");
        }

        // Extract beside the target and swap, so a crash mid-write never leaves a
        // half library behind. The venv (.venv_thabit) is a sibling and untouched.
        var tempDir = Path.Combine(dataFolder, LibraryFolderName + ".tmp-" + Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(tempDir);
            foreach (var name in resources)
            {
                var relative = name[ResourcePrefix.Length..].Replace('/', Path.DirectorySeparatorChar);
                var destination = Path.Combine(tempDir, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                using var source = assembly.GetManifestResourceStream(name)
                    ?? throw new InvalidOperationException($"Embedded resource vanished: {name}");
                using var target = File.Create(destination);
                source.CopyTo(target);
            }

            File.WriteAllText(Path.Combine(tempDir, MarkerFileName), markerValue);

            if (Directory.Exists(libraryDir))
            {
                Directory.Delete(libraryDir, recursive: true);
            }

            Directory.Move(tempDir, libraryDir);
        }
        catch
        {
            try
            {
                if (Directory.Exists(tempDir))
                {
                    Directory.Delete(tempDir, recursive: true);
                }
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                // Best effort: the real failure is the one being rethrown.
            }

            throw;
        }

        return new LibraryBundleResult(libraryDir, true, resources.Length);
    }

    private static int CountFiles(Assembly assembly)
        => assembly.GetManifestResourceNames().Count(static name => name.StartsWith(ResourcePrefix, StringComparison.Ordinal));
}
