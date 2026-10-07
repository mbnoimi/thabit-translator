using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class ConfigWriterTests
{
    private static PluginConfiguration Sample() => new()
    {
        OpenSubtitlesApiKey = "os-key",
        OpenSubtitlesUsername = "user",
        OpenSubtitlesPassword = "secret",
        SubDlApiKey = "dl-key",
        SubSourceApiKey = "ss-key",
        TargetLanguages = "ar,en",
        SourceLanguage = "eng",
        EnabledProviders = "subdl, opensubtitles"
    };

    [Fact]
    public void Render_CoversEverySectionTheLibraryReads()
    {
        var rendered = ConfigWriter.Render(Sample(), "/config/plugins/X/home/.cache/thabit_translator");

        Assert.Contains("[opensubtitles]", rendered);
        Assert.Contains("api_key = os-key", rendered);
        Assert.Contains("username = user", rendered);
        Assert.Contains("password = secret", rendered);
        Assert.Contains("default_language = ar", rendered);
        Assert.Contains("user_agent = ThabitTranslator v1.0", rendered);
        Assert.Contains("[subdl]", rendered);
        Assert.Contains("api_key = dl-key", rendered);
        Assert.Contains("[subsource]", rendered);
        Assert.Contains("api_key = ss-key", rendered);
        Assert.Contains("[settings]", rendered);
        Assert.Contains("default_source_lang = en", rendered);
        Assert.Contains("default_target_lang = ar", rendered);
        Assert.Contains("verbose = true", rendered);
        Assert.Contains("cache_dir = /config/plugins/X/home/.cache/thabit_translator", rendered);
        Assert.Contains("[providers]", rendered);
        Assert.Contains("enabled = opensubtitles,subdl", rendered);
    }

    [Fact]
    public void Render_KeepsTheProvenTemplateOrderAndShape()
    {
        var rendered = ConfigWriter.Render(Sample(), "/tmp/cache");

        var sections = new[]
        {
            rendered.IndexOf("[opensubtitles]", System.StringComparison.Ordinal),
            rendered.IndexOf("[subdl]", System.StringComparison.Ordinal),
            rendered.IndexOf("[subsource]", System.StringComparison.Ordinal),
            rendered.IndexOf("[settings]", System.StringComparison.Ordinal),
            rendered.IndexOf("[providers]", System.StringComparison.Ordinal)
        };

        for (var i = 1; i < sections.Length; i++)
        {
            Assert.True(sections[i] > sections[i - 1], $"section {i} is out of order");
        }
    }

    [Fact]
    public void Render_NeverEmitsNewlinesInsideValues()
    {
        var config = Sample();
        config.OpenSubtitlesApiKey = "line1\nline2\rline3";

        var rendered = ConfigWriter.Render(config, "/tmp/cache");

        Assert.Contains("api_key = line1 line2 line3", rendered);
    }

    [Theory]
    [InlineData("opensubtitles,subdl,subsource", "opensubtitles,subdl,subsource")]
    [InlineData("subsource, opensubtitles", "opensubtitles,subsource")]
    [InlineData("opensubtitles,opensubtitles", "opensubtitles")]
    [InlineData("nonsense", "opensubtitles,subdl,subsource")]
    [InlineData("", "opensubtitles,subdl,subsource")]
    [InlineData(null, "opensubtitles,subdl,subsource")]
    public void NormalizeProviders_KeepsKnownProvidersInLibraryOrder(string? input, string expected)
    {
        Assert.Equal(expected, ConfigWriter.NormalizeProviders(input));
    }
}
