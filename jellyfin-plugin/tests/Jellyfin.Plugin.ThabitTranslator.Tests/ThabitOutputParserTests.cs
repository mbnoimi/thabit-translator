using Jellyfin.Plugin.ThabitTranslator.Runtime;
using Xunit;

namespace Jellyfin.Plugin.ThabitTranslator.Tests;

public class ThabitOutputParserTests
{
    [Fact]
    public void Classify_NullOrBlank_IsIgnored()
    {
        var parser = new ThabitOutputParser();
        Assert.Null(parser.Classify(null));
        Assert.Null(parser.Classify(""));

        // Blank after ANSI stripping (a pure color reset) is ignored too.
        Assert.Null(parser.Classify("\x1b[0m   "));
    }

    [Fact]
    public void Classify_ProgressLine_ReportsFractionalProgress()
    {
        var reported = new List<double>();
        var parser = new ThabitOutputParser(reported.Add);

        var evt = parser.Classify("[PROGRESS] 12/60");

        Assert.NotNull(evt);
        Assert.Equal(ThabitLogLevel.Info, evt!.Level);
        Assert.Single(reported);
        Assert.Equal(0.4 + 0.55 * (12d / 60d), reported[0], 5);
        Assert.Equal(reported[0], parser.Progress);
    }

    [Fact]
    public void Classify_FolderProgressLine_ReportsFractionalProgress()
    {
        var parser = new ThabitOutputParser();

        var evt = parser.Classify("[AUTO] [2/4] Monk.S01E02.mkv");

        Assert.NotNull(evt);
        Assert.Equal(0.4 + 0.55 * (2d / 4d), parser.Progress, 5);
    }

    [Fact]
    public void Classify_StageMarker_ReportsCoarseStage()
    {
        var parser = new ThabitOutputParser();

        parser.Classify("checking for downloadable subtitles (opensubtitles) ...");

        Assert.Equal(0.05, parser.Progress, 5);
    }

    [Fact]
    public void Classify_ProgressNeverMovesBackwards()
    {
        var parser = new ThabitOutputParser();
        parser.Classify("[PROGRESS] 60/60");
        Assert.Equal(0.4 + 0.55, parser.Progress, 5);

        // A stage marker seen after a later progress line must not reset it.
        parser.Classify("checking for embedded subtitles ...");

        Assert.Equal(0.95, parser.Progress, 5);
    }

    [Fact]
    public void Classify_OutputLine_CapturesPathThroughAnsi()
    {
        var parser = new ThabitOutputParser();

        parser.Classify("  \u251c\u2500 Output: \x1b[36m/media/Monk/S01E12.ar.srt\x1b[0m");

        Assert.Equal("/media/Monk/S01E12.ar.srt", parser.OutputPath);
    }

    [Fact]
    public void Classify_OutputLine_OnlyMatchesTheOutputLabel()
    {
        var parser = new ThabitOutputParser();

        parser.Classify("  \u251c\u2500 Input: /media/Monk/S01E12.mkv");

        Assert.Null(parser.OutputPath);
    }

    [Fact]
    public void Classify_ErrorLine_IsErrorAndKeepsFirstError()
    {
        var parser = new ThabitOutputParser();

        var first = parser.Classify("[ERROR] no subtitle produced");
        var second = parser.Classify("[ERROR] something else");

        Assert.Equal(ThabitLogLevel.Error, first!.Level);
        Assert.Equal(ThabitLogLevel.Error, second!.Level);
        Assert.Equal("[ERROR] no subtitle produced", parser.FirstError);
    }

    [Fact]
    public void Classify_WarnPrefixAndMarkers_AreWarnings()
    {
        var parser = new ThabitOutputParser();

        Assert.Equal(ThabitLogLevel.Warn, parser.Classify("[WARN] SubSource returned nothing")!.Level);
        Assert.Equal(ThabitLogLevel.Warn, parser.Classify("OpenSubtitles daily download limit reached")!.Level);
        Assert.Equal(ThabitLogLevel.Warn, parser.Classify("SubSource is blocked: rate limit")!.Level);
        Assert.Null(parser.FirstError);
    }

    [Fact]
    public void Classify_StripsAnsiFromLogMessage()
    {
        var parser = new ThabitOutputParser();

        var evt = parser.Classify("\x1b[32m\u2713 translated: done\x1b[0m");

        Assert.NotNull(evt);
        Assert.False(
            evt!.Message.Contains('\x1b'),
            "ANSI not stripped, got: " + string.Concat(evt.Message.Select(c => c < 32 ? "<" + ((int)c).ToString("x2") + ">" : c.ToString())));
        Assert.Contains("translated: done", evt.Message);
    }

    [Theory]
    [InlineData("Traceback (most recent call last):", true)]
    [InlineData("RuntimeError: boom", true)]
    [InlineData("python3: can't open file '/config/library': [Errno 21] Is a directory", true)]
    [InlineData("some harmless chatter", false)]
    public void ClassifyStderr_MarksSevereLinesOnly(string line, bool severe)
    {
        var parser = new ThabitOutputParser();

        Assert.Equal(severe, parser.ClassifyStderr(line));
        if (severe)
        {
            Assert.Equal(line, parser.FirstError);
        }
    }
}
