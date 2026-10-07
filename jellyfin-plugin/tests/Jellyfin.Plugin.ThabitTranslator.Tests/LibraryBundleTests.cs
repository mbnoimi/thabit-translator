using System.Reflection;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class LibraryBundleTests
{
    private static Assembly PluginAssembly => typeof(LibraryBundle).Assembly;

    [Theory]
    [InlineData("ThabitLibrary/thabit_translator/app.py")]
    [InlineData("ThabitLibrary/thabit_translator/cli.py")]
    [InlineData("ThabitLibrary/thabit_translator/core/auto_workflow.py")]
    [InlineData("ThabitLibrary/thabit_translator/core/paths.py")]
    [InlineData("ThabitLibrary/thabit_translator/providers/opensubtitles.py")]
    [InlineData("ThabitLibrary/thabit_translator/providers/subdl.py")]
    public void Assembly_CarriesTheLibrary(string resource)
    {
        Assert.Contains(resource, PluginAssembly.GetManifestResourceNames());
    }

    [Fact]
    public void Assembly_NeverCarriesTheCredentialsFile()
    {
        var names = PluginAssembly.GetManifestResourceNames();

        // Only *.py is embedded (build guard in the csproj), so the real
        // thabit_translator.conf can never travel inside the DLL. Match the full
        // suffix - ".conf" as a substring would also hit "...Configuration.configPage.html".
        Assert.DoesNotContain(names, name => name.EndsWith(".conf", StringComparison.OrdinalIgnoreCase));
        Assert.DoesNotContain(names, name => name.Contains("thabit_translator.conf", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public void Ensure_ExtractsIntoTheDataFolder_AndIsIdempotent()
    {
        var dataFolder = NewDataFolder();
        try
        {
            var first = LibraryBundle.Ensure(PluginAssembly, dataFolder, "marker-1");

            Assert.True(first.Extracted);
            Assert.Equal(Path.Combine(dataFolder, LibraryBundle.LibraryFolderName), first.Path);
            Assert.True(File.Exists(Path.Combine(first.Path, LibraryBundle.EntryScript)));
            Assert.True(File.Exists(Path.Combine(first.Path, ".bundle")));
            Assert.Equal("marker-1", File.ReadAllText(Path.Combine(first.Path, ".bundle")).Trim());

            // The extracted entry point is byte-identical to the embedded resource.
            using var embedded = PluginAssembly.GetManifestResourceStream("ThabitLibrary/" + LibraryBundle.EntryScript)!;
            using var extracted = File.OpenRead(Path.Combine(first.Path, LibraryBundle.EntryScript));
            Assert.Equal(embedded.ReadAllBytes(), extracted.ReadAllBytes());

            var second = LibraryBundle.Ensure(PluginAssembly, dataFolder, "marker-1");
            Assert.False(second.Extracted);
            Assert.Equal(first.Path, second.Path);
        }
        finally
        {
            Delete(dataFolder);
        }
    }

    [Fact]
    public void Ensure_DifferentBuildMarker_Reextracts()
    {
        var dataFolder = NewDataFolder();
        try
        {
            LibraryBundle.Ensure(PluginAssembly, dataFolder, "old-build");

            var updated = LibraryBundle.Ensure(PluginAssembly, dataFolder, "new-build");

            Assert.True(updated.Extracted);
            Assert.Equal("new-build", File.ReadAllText(Path.Combine(updated.Path, ".bundle")).Trim());
        }
        finally
        {
            Delete(dataFolder);
        }
    }

    [Fact]
    public void Ensure_WithoutResources_ThrowsWithClearMessage()
    {
        var dataFolder = NewDataFolder();
        try
        {
            // An assembly without a ThabitLibrary prefix must fail loudly instead of
            // leaving an empty folder behind (that is the broken-build case).
            var ex = Assert.Throws<InvalidOperationException>(
                () => LibraryBundle.Ensure(typeof(object).Assembly, dataFolder, "x"));
            Assert.Contains("embedded ThabitTranslator library", ex.Message);
        }
        finally
        {
            Delete(dataFolder);
        }
    }

    private static string NewDataFolder()
    {
        var path = Path.Combine(Path.GetTempPath(), "thabit-bundle-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    private static void Delete(string path)
    {
        try
        {
            if (Directory.Exists(path))
            {
                Directory.Delete(path, recursive: true);
            }
        }
        catch (IOException)
        {
            // Best effort cleanup of the temp folder.
        }
    }
}

internal static class StreamTestExtensions
{
    public static byte[] ReadAllBytes(this Stream stream)
    {
        using var ms = new MemoryStream();
        stream.CopyTo(ms);
        return ms.ToArray();
    }
}
