using System.Formats.Tar;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Text;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class PortablePythonTests
{
    [Fact]
    public void AssetFor_X64AndArm64_ArePinnedToTheSameRelease()
    {
        var x64 = PortablePython.AssetFor(Architecture.X64);
        var arm64 = PortablePython.AssetFor(Architecture.Arm64);

        foreach (var asset in new[] { x64, arm64 })
        {
            Assert.StartsWith($"cpython-{PortablePython.CPythonVersion}+", asset.FileName);
            Assert.Contains($"+{PortablePython.ReleaseTag}-", asset.FileName);
            Assert.Contains("unknown-linux-gnu-install_only_stripped.tar.gz", asset.FileName);
            Assert.Matches("^[0-9a-f]{64}$", asset.Sha256);
        }

        Assert.Contains("x86_64-", x64.FileName);
        Assert.Contains("aarch64-", arm64.FileName);
        Assert.NotEqual(x64.FileName, arm64.FileName);
    }

    [Fact]
    public void AssetFor_UnsupportedArchitecture_Throws()
    {
        Assert.Throws<PlatformNotSupportedException>(() => PortablePython.AssetFor(Architecture.X86));
        Assert.Throws<PlatformNotSupportedException>(() => PortablePython.AssetFor(Architecture.S390x));
    }

    [Fact]
    public void DownloadUrl_PointsAtThePinnedGitHubRelease()
    {
        var url = PortablePython.DownloadUrl(PortablePython.AssetFor(Architecture.X64));

        Assert.StartsWith(
            $"https://github.com/astral-sh/python-build-standalone/releases/download/{PortablePython.ReleaseTag}/",
            url);
        Assert.EndsWith(".tar.gz", url);
    }

    [Fact]
    public void InterpreterPath_LivesInTheRuntimeFolderOfTheDataFolder()
    {
        var path = PortablePython.InterpreterPath("/config/plugins/Jellyfin.Plugin.ThabitTranslator");

        Assert.Equal(
            Path.Combine("/config/plugins/Jellyfin.Plugin.ThabitTranslator", "runtime", "python", "bin", "python3"),
            path);
        Assert.StartsWith(PortablePython.RootPath("/config/plugins/Jellyfin.Plugin.ThabitTranslator"), path);
    }

    [Fact]
    public async Task ComputeSha256Async_KnownVector()
    {
        var file = Path.Combine(Path.GetTempPath(), $"thabit-sha-{Guid.NewGuid():N}.txt");
        try
        {
            await File.WriteAllTextAsync(file, "abc");
            Assert.Equal(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                await PortablePython.ComputeSha256Async(file));
        }
        finally
        {
            File.Delete(file);
        }
    }

    [Fact]
    public void ExtractArchive_PreservesSymlinks_LayoutStaysRunnable()
    {
        var root = NewTempDirectory();
        try
        {
            var archive = Path.Combine(root, "fixture.tar.gz");
            WriteFixtureArchive(archive);

            var dest = Path.Combine(root, "out");
            PortablePython.ExtractArchive(archive, dest);

            var bin = Path.Combine(dest, "python", "bin");
            var versioned = Path.Combine(bin, "python3.12");
            var python3 = Path.Combine(bin, "python3");

            Assert.True(File.Exists(versioned));
            Assert.True(File.Exists(python3), "python3 entry point must survive extraction");
            Assert.True((File.GetAttributes(python3) & FileAttributes.ReparsePoint) != 0, "python3 must be a symlink");

            // The fixture is written without exec bits: the layout fixer adds them.
            PortablePython.EnsureExecutableLayout(bin);
            Assert.True((File.GetUnixFileMode(versioned) & UnixFileMode.UserExecute) != 0, "versioned interpreter must be executable");
        }
        finally
        {
            Delete(root);
        }
    }

    [Fact]
    public void EnsureExecutableLayout_RebuildsEntryPoints_WhateverTheExtractorDid()
    {
        var bin = Path.Combine(NewTempDirectory(), "bin");
        Directory.CreateDirectory(bin);

        // A versioned binary with no exec bit, plus a "python3" that is a regular
        // file (what a naive extractor would materialize a symlink as).
        File.WriteAllText(Path.Combine(bin, "python3.12"), "#!/bin/sh\n");
        File.WriteAllText(Path.Combine(bin, "python3"), "python3.12");

        PortablePython.EnsureExecutableLayout(bin);

        Assert.True((File.GetUnixFileMode(Path.Combine(bin, "python3.12")) & UnixFileMode.UserExecute) != 0, "versioned interpreter must be executable");

        var python3 = Path.Combine(bin, "python3");
        Assert.True((File.GetAttributes(python3) & FileAttributes.ReparsePoint) != 0);
        Assert.Equal("python3.12", File.ResolveLinkTarget(python3, returnFinalTarget: false)!.Name);

        var python = Path.Combine(bin, "python");
        Assert.True((File.GetAttributes(python) & FileAttributes.ReparsePoint) != 0);
        Assert.Equal("python3", File.ResolveLinkTarget(python, returnFinalTarget: false)!.Name);
    }

    [Fact]
    public void EnsureExecutableLayout_MissingOrEmptyDirectory_ThrowsWithReason()
    {
        var root = NewTempDirectory();
        try
        {
            var missing = Assert.Throws<InvalidDataException>(
                () => PortablePython.EnsureExecutableLayout(Path.Combine(root, "missing")));
            Assert.Contains("unexpected Python archive layout", missing.Message);

            var empty = Path.Combine(root, "empty");
            Directory.CreateDirectory(empty);
            var noPython = Assert.Throws<InvalidDataException>(
                () => PortablePython.EnsureExecutableLayout(empty));
            Assert.Contains("no versioned interpreter", noPython.Message);
        }
        finally
        {
            Delete(root);
        }
    }

    [Fact]
    public async Task EnsureAsync_AlreadyInstalled_UsesItWithoutDownloading()
    {
        var dataFolder = NewTempDirectory();
        try
        {
            var bin = Path.Combine(dataFolder, "runtime", "python", "bin");
            Directory.CreateDirectory(bin);

            // The builder image's python3 stands in for a healthy portable install.
            File.CreateSymbolicLink(Path.Combine(bin, "python3"), "/usr/bin/python3");
            Assert.True(PortablePython.IsInstalled(dataFolder));

            var logged = new List<string>();
            var reason = await PortablePython.EnsureAsync(dataFolder, logged.Add);

            Assert.Null(reason);
            Assert.Empty(logged); // nothing was downloaded, nothing to report
        }
        finally
        {
            Delete(dataFolder);
        }
    }

    [Fact]
    public void ExtractArchive_CutsNamesAtTheFirstEmbeddedNull()
    {
        // Regression: python-build-standalone ustar name fields can carry
        // "name" + NUL + a duplicated tail ("dir/quirk.txt" + NUL + "irk.txt").
        // Only the first segment is the real path - GNU tar and CPython's
        // tarfile stop at the NUL, and so must we.
        var root = NewTempDirectory();
        var destination = NewTempDirectory();
        try
        {
            var archive = Path.Combine(root, "quirk.tar.gz");
            WriteUstarArchiveWithEmbeddedNullName(archive);
            PortablePython.ExtractArchive(archive, destination);

            var clean = Path.Combine(destination, "dir", "quirk.txt");
            Assert.True(File.Exists(clean));
            Assert.Equal("payload", File.ReadAllText(clean));
            Assert.Single(Directory.GetFileSystemEntries(Path.Combine(destination, "dir")));
        }
        finally
        {
            Delete(root);
            Delete(destination);
        }
    }

    private static void WriteFixtureArchive(string path)
    {
        using var file = File.Create(path);
        using var gzip = new GZipStream(file, CompressionMode.Compress);
        using var writer = new TarWriter(gzip, leaveOpen: false);

        var binary = new PaxTarEntry(TarEntryType.RegularFile, "python/bin/python3.12")
        {
            // Deliberately no exec bit: EnsureExecutableLayout must add it.
            Mode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
            DataStream = new MemoryStream(Encoding.ASCII.GetBytes("#!/bin/sh\n"))
        };
        writer.WriteEntry(binary);

        writer.WriteEntry(new PaxTarEntry(TarEntryType.SymbolicLink, "python/bin/python3")
        {
            LinkName = "python3.12"
        });
        writer.WriteEntry(new PaxTarEntry(TarEntryType.SymbolicLink, "python/bin/python")
        {
            LinkName = "python3"
        });
    }

    /// <summary>
    /// Hand-built POSIX ustar archive with a single "dir/quirk.txt" file whose
    /// name field is "dir/quirk.txt" + NUL + "irk.txt" - the embedded-NUL quirk
    /// of the python-build-standalone tarballs that TarFile mishandles.
    /// </summary>
    private static void WriteUstarArchiveWithEmbeddedNullName(string path)
    {
        var header = new byte[512];

        "dir/quirk.txt\0irk.txt"u8.CopyTo(header);
        WriteOctal(header, offset: 100, fieldLength: 8, value: 0x1A4); // mode 0644
        WriteOctal(header, offset: 108, fieldLength: 8, value: 0);     // uid
        WriteOctal(header, offset: 116, fieldLength: 8, value: 0);     // gid
        WriteOctal(header, offset: 124, fieldLength: 12, value: 7);    // size of "payload"
        WriteOctal(header, offset: 136, fieldLength: 12, value: 0);    // mtime
        header[156] = (byte)'0';                                      // typeflag: regular file
        "ustar\0"u8.CopyTo(header.AsSpan(257));                       // POSIX magic
        header[263] = (byte)'0';                                      // version "00"
        header[264] = (byte)'0';

        var checksum = 0;
        for (var i = 0; i < header.Length; i++)
        {
            checksum += i is >= 148 and < 156 ? ' ' : header[i];
        }

        var digits = Convert.ToString(checksum, 8).PadLeft(6, '0');
        Encoding.ASCII.GetBytes(digits).CopyTo(header, 148);
        header[154] = 0;
        header[155] = (byte)' ';

        using var file = File.Create(path);
        using var gzip = new GZipStream(file, CompressionMode.Compress);
        gzip.Write(header);
        gzip.Write("payload"u8);
        gzip.Write(new byte[512 - "payload"u8.Length]);
        gzip.Write(new byte[1024]); // two zero blocks: end of archive
    }

    private static void WriteOctal(byte[] header, int offset, int fieldLength, int value)
    {
        var digits = Convert.ToString(value, 8).PadLeft(fieldLength - 1, '0');
        Encoding.ASCII.GetBytes(digits).CopyTo(header, offset);
        header[offset + fieldLength - 1] = 0;
    }

    private static string NewTempDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), "thabit-portable-tests", Guid.NewGuid().ToString("N"));
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
            // Best effort - temp cleanup must never fail a test run.
        }
    }
}
