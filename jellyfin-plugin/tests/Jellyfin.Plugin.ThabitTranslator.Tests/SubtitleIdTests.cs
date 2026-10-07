using System;
using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class SubtitleIdTests
{
    [Fact]
    public void EncodeThenDecode_RoundTrips()
    {
        var item = Guid.Parse("01234567-89ab-cdef-0123-456789abcdef");
        var encoded = SubtitleId.Encode("ar", item);

        var decoded = SubtitleId.Decode(encoded);

        Assert.NotNull(decoded);
        Assert.Equal("ar", decoded.Value.Iso2);
        Assert.Equal(item, decoded.Value.ItemId);
    }

    [Fact]
    public void Encode_ProducesNoUnderscoreSoTheHostPrefixStaysParseable()
    {
        var encoded = SubtitleId.Encode("en", Guid.NewGuid());

        Assert.DoesNotContain('_', encoded);
        Assert.StartsWith("srt-en-", encoded);
        Assert.Equal(32, encoded.Split('-')[^1].Length);
    }

    [Fact]
    public void Decode_StripsTheProviderHashJellyfinPrepends()
    {
        var item = Guid.Parse("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
        var ours = SubtitleId.Encode("ar", item);
        var hostPrefixed = new string('a', 32) + "_" + ours;

        var decoded = SubtitleId.Decode(hostPrefixed);

        Assert.NotNull(decoded);
        Assert.Equal(item, decoded.Value.ItemId);
    }

    [Fact]
    public void Decode_ReturnsNullForIdsThatAreNotOurs()
    {
        Assert.Null(SubtitleId.Decode(null));
        Assert.Null(SubtitleId.Decode(string.Empty));
        Assert.Null(SubtitleId.Decode("opensubtitles-12345"));
        Assert.Null(SubtitleId.Decode("srt-arf-not-a-guid"));
        Assert.Null(SubtitleId.Decode("srt-abc-0123456789abcdef0123456789abcdef"));
        Assert.Null(SubtitleId.Decode(new string('a', 32) + "_junk"));
    }

    [Fact]
    public void Encode_RejectsLanguagesThatAreNotTwoLetters()
    {
        Assert.Throws<ArgumentException>(() => SubtitleId.Encode("ara", Guid.NewGuid()));
        Assert.Throws<ArgumentException>(() => SubtitleId.Encode(string.Empty, Guid.NewGuid()));
    }
}
