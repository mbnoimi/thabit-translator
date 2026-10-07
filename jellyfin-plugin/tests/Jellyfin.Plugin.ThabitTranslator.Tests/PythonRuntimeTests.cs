using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class PythonRuntimeTests
{
    [Fact]
    public void Candidates_ConfiguredInterpreterComesFirst_AndDuplicatesAreRemoved()
    {
        var candidates = PythonRuntime.Candidates("/usr/bin/python3");

        // The configured path is also one of the defaults - it must appear once, up front.
        Assert.Equal(
            new[] { "/usr/bin/python3", "python3", "python", "/usr/local/bin/python3" },
            candidates);
    }

    [Fact]
    public void Candidates_EmptyConfigFallsBackToTheUsualNames()
    {
        var candidates = PythonRuntime.Candidates(null);

        Assert.Equal(new[] { "python3", "python", "/usr/bin/python3", "/usr/local/bin/python3" }, candidates);

        Assert.Equal(new[] { "python3", "python", "/usr/bin/python3", "/usr/local/bin/python3" }, PythonRuntime.Candidates("   "));
    }

    [Fact]
    public void ProbeCandidate_MissingPath_FailsWithReason()
    {
        var (version, _, error) = PythonRuntime.ProbeCandidate("/nonexistent/definitely-not-python3");

        Assert.Null(version);
        Assert.NotNull(error);
    }

    [Fact]
    public void Probe_FindsTheBuilderImagesPython3()
    {
        // The test container (Dockerfile.builder) always provides python3.
        var info = PythonRuntime.Probe(null);

        Assert.True(info.Available, $"python3 should be probeable: {info.Error}");
        Assert.NotNull(info.Version);
        Assert.Matches(@"^\d+\.\d+\.\d+$", info.Version);
        Assert.Equal("python3", info.Path);
    }

    [Fact]
    public void Probe_ConfiguredWrongPath_StillFallsBack()
    {
        var info = PythonRuntime.Probe("/nonexistent/definitely-not-python3");

        Assert.True(info.Available, info.Error);
        Assert.Equal("python3", info.Path);
    }

    [Fact]
    public void Resolve_CachesTheSuccessfulProbe()
    {
        var config = new PluginConfiguration { PythonPath = string.Empty };

        var first = PythonRuntime.Resolve(config);
        var second = PythonRuntime.Resolve(config);

        Assert.True(first.Available, first.Error);
        Assert.Same(first, second);
    }
}
