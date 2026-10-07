using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class LanguageCodesTests
{
    [Theory]
    [InlineData("ar", "ara")]
    [InlineData("en", "eng")]
    [InlineData("AR", "ara")]
    [InlineData("de", "deu")]
    [InlineData("zh", "zho")]
    [InlineData("pt", "por")]
    public void MapsTwoLetterToThreeLetter(string iso2, string expected)
    {
        Assert.Equal(expected, LanguageCodes.ToIso3(iso2));
    }

    [Theory]
    [InlineData("ara", "ar")]
    [InlineData("eng", "en")]
    [InlineData("ENG", "en")]
    [InlineData("ger", "de")] // ISO-639-2/B bibliographic code
    [InlineData("fre", "fr")]
    [InlineData("chi", "zh")]
    public void MapsThreeLetterToTwoLetter(string iso3, string expected)
    {
        Assert.Equal(expected, LanguageCodes.ToIso2(iso3));
    }

    [Fact]
    public void RoundTripsEveryConfiguredLanguage()
    {
        var all = new[]
        {
            "af", "am", "ar", "az", "be", "bg", "bn", "bs", "ca", "cs", "cy", "da", "de",
            "el", "en", "es", "et", "eu", "fa", "fi", "fr", "ga", "gl", "gu", "he", "hi",
            "hr", "hu", "hy", "id", "is", "it", "ja", "ka", "kk", "ko", "ky", "lt", "lv",
            "mk", "ml", "mn", "mr", "ms", "my", "ne", "nl", "no", "pa", "pl", "pt", "ro",
            "ru", "si", "sk", "sl", "so", "sq", "sr", "sv", "sw", "ta", "te", "th", "tr",
            "uk", "ur", "uz", "vi", "yo", "zh", "zu"
        };

        foreach (var iso2 in all)
        {
            var iso3 = LanguageCodes.ToIso3(iso2);
            Assert.False(string.IsNullOrEmpty(iso3), iso2);
            Assert.Equal(iso2, LanguageCodes.ToIso2(iso3));
        }
    }

    [Fact]
    public void RejectsNonLanguageInput()
    {
        Assert.Null(LanguageCodes.ToIso2(null));
        Assert.Null(LanguageCodes.ToIso2("english"));
        Assert.Null(LanguageCodes.ToIso2("x"));
        Assert.Null(LanguageCodes.ToIso3("subtitle"));
    }

    [Fact]
    public void ParseListNormalizesDeduplicatesAndKeepsOrder()
    {
        // Two-letter codes pass through unvalidated: ISO-639-1 has more languages
        // than this plugin knows by name, and dropping a real one would be worse
        // than letting a typo surface as "no subtitle found" in the log.
        var parsed = LanguageCodes.ParseList("ara, en ,EN; fr  ,zz, 2");

        Assert.Equal(new[] { "ar", "en", "fr", "zz" }, parsed);
        Assert.Empty(LanguageCodes.ParseList(null));
        Assert.Empty(LanguageCodes.ParseList("  "));
        Assert.Empty(LanguageCodes.ParseList("2"));
        Assert.Empty(LanguageCodes.ParseList("english"));
    }

    [Fact]
    public void DisplayThreeLetterNeverReturnsEmpty()
    {
        Assert.Equal("ara", LanguageCodes.DisplayThreeLetter("ar"));
        Assert.Equal("xyz", LanguageCodes.DisplayThreeLetter("xyz"));
        Assert.Equal(string.Empty, LanguageCodes.DisplayThreeLetter(null));
    }
}
