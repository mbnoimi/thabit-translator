using System;
using System.Text.RegularExpressions;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>Severity of a library CLI line.</summary>
public enum ThabitLogLevel
{
    Info,
    Warn,
    Error
}

/// <summary>One classified line of CLI output.</summary>
public sealed record ThabitLogEvent(ThabitLogLevel Level, string Message);

/// <summary>
/// Classifies raw stdout lines of <c>thabit_translator</c> (the CLI), the way
/// the old <c>jellyfin_runner.py</c> bridge used to before it turned them into
/// JSONL: coarse stage progress, <c>[PROGRESS] n/m</c> and <c>[AUTO] [i/j]</c>
/// counters, error/warn markers, and the final <c>Output: &lt;path&gt;</c> line
/// that tells us which file the run produced.
///
/// Stateful and single-threaded: feed it lines from one run.
/// </summary>
public sealed class ThabitOutputParser
{
    private static readonly Regex Ansi = new(@"\x1b\[[0-9;?]*[A-Za-z]", RegexOptions.Compiled);

    private static readonly Regex ProgressLine = new(@"^\[PROGRESS\]\s*(\d+)/(\d+)", RegexOptions.Compiled);

    private static readonly Regex FolderProgressLine = new(@"^\[AUTO\]\s*\[(\d+)/(\d+)\]", RegexOptions.Compiled);

    private static readonly Regex OutputLine = new(@"Output:\s*(.+)$", RegexOptions.Compiled);

    /// <summary>Library marker (lower case) -&gt; coarse stage progress (ported from the bridge).</summary>
    private static readonly (string Marker, double Stage)[] StageMarkers =
    {
        ("checking for downloadable subtitles", 0.05),
        ("checking for embedded subtitles", 0.30),
        ("-> extracting stream", 0.32),
        ("extracting embedded", 0.32),
        ("trying english subtitle", 0.40),
        ("translating", 0.45),
        ("translated:", 0.55),
        ("applying stt", 0.95),
        ("transcribing", 0.95),
        ("vosk", 0.95),
        // The bootstrap's own milestones, so a first run shows something too.
        ("creating virtual environment", 0.01),
        ("installing", 0.02),
    };

    private static readonly string[] WarnMarkers =
    {
        "is blocked:",
        "daily download limit",
        "throttling us",
        "quota",
        "skipped:"
    };

    private readonly Action<double>? _reportProgress;
    private double _progress;

    public ThabitOutputParser(Action<double>? reportProgress = null)
    {
        _reportProgress = reportProgress;
    }

    /// <summary>Last progress value reported in the 0..1 range.</summary>
    public double Progress => _progress;

    /// <summary>The file the run produced, parsed from the CLI's <c>Output:</c> line.</summary>
    public string? OutputPath { get; private set; }

    /// <summary>First error-level message seen (stdout or stderr), used as the failure reason.</summary>
    public string? FirstError { get; private set; }

    /// <summary>Classifies one stdout line; null for blank/unclassifiable lines that need no log entry.</summary>
    public ThabitLogEvent? Classify(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return null;
        }

        var line = Ansi.Replace(raw, string.Empty).Trim();
        if (line.Length == 0)
        {
            return null;
        }

        var lowered = line.ToLowerInvariant();

        var match = ProgressLine.Match(line);
        if (match.Success)
        {
            var done = int.Parse(match.Groups[1].Value);
            var total = int.Parse(match.Groups[2].Value);
            if (total > 0)
            {
                SetProgress(0.4 + 0.55 * ((double)done / total));
            }

            return Log(ThabitLogLevel.Info, line);
        }

        match = FolderProgressLine.Match(line);
        if (match.Success)
        {
            var done = int.Parse(match.Groups[1].Value);
            var total = int.Parse(match.Groups[2].Value);
            if (total > 0)
            {
                SetProgress(0.4 + 0.55 * ((double)done / total));
            }

            return Log(ThabitLogLevel.Info, line);
        }

        var output = OutputLine.Match(line);
        if (output.Success)
        {
            var path = output.Groups[1].Value.Trim();
            if (path.Length > 0)
            {
                OutputPath = path;
            }
        }

        foreach (var (marker, stage) in StageMarkers)
        {
            if (lowered.Contains(marker, StringComparison.Ordinal))
            {
                SetProgress(stage);
                break;
            }
        }

        if (line.StartsWith("[ERROR]", StringComparison.Ordinal))
        {
            return Log(ThabitLogLevel.Error, line);
        }

        if (line.StartsWith("[WARN]", StringComparison.Ordinal))
        {
            return Log(ThabitLogLevel.Warn, line);
        }

        foreach (var marker in WarnMarkers)
        {
            if (lowered.Contains(marker, StringComparison.Ordinal))
            {
                return Log(ThabitLogLevel.Warn, line);
            }
        }

        return Log(ThabitLogLevel.Info, line);
    }

    /// <summary>Records a stderr line; returns true when it is severe enough to log at error level.</summary>
    public bool ClassifyStderr(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw))
        {
            return false;
        }

        // Python's own tracebacks land here; argos/stanza chatter does too, so only the
        // exception markers are worth error-level attention - plus the launcher's own
        // failure mode: `can't open file '...': [Errno 21] Is a directory`, which has
        // no "Error"/"Traceback" in it but says exactly why the run died.
        var severe = raw.Contains("Traceback", StringComparison.OrdinalIgnoreCase)
            || raw.Contains("Error", StringComparison.OrdinalIgnoreCase)
            || raw.Contains("Exception", StringComparison.OrdinalIgnoreCase)
            || raw.Contains("can't open file", StringComparison.OrdinalIgnoreCase)
            || raw.Contains("Errno", StringComparison.OrdinalIgnoreCase);

        if (severe)
        {
            FirstError ??= raw.Trim();
        }

        return severe;
    }

    private ThabitLogEvent Log(ThabitLogLevel level, string message)
    {
        if (level == ThabitLogLevel.Error)
        {
            FirstError ??= message;
        }

        return new ThabitLogEvent(level, message);
    }

    /// <summary>Progress only ever moves forward; a stage we cannot place keeps the old value.</summary>
    private void SetProgress(double value)
    {
        value = Math.Clamp(value, 0d, 1d);
        if (value < _progress + 0.005)
        {
            return;
        }

        _progress = value;
        _reportProgress?.Invoke(value);
    }
}
