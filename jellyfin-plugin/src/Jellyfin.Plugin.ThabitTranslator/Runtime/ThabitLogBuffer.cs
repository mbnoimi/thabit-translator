using System;
using System.Collections.Generic;
using System.Linq;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>
/// Keeps the tail of the CLI output so the configuration page can show what the
/// last run actually did (provider quotas, translation failures, ...).
/// </summary>
public static class ThabitLogBuffer
{
    private const int Capacity = 500;
    private static readonly LinkedList<string> Lines = new();
    private static readonly object Sync = new();

    /// <summary>Records one line; thread safe.</summary>
    public static void Add(DateTimeOffset timestamp, string level, string message)
    {
        var line = $"{timestamp:HH:mm:ss} [{level.ToUpperInvariant()}] {message}";
        lock (Sync)
        {
            Lines.AddLast(line);
            while (Lines.Count > Capacity)
            {
                Lines.RemoveFirst();
            }
        }
    }

    /// <summary>The last <paramref name="tail"/> lines, oldest first.</summary>
    public static IReadOnlyList<string> Tail(int tail)
    {
        lock (Sync)
        {
            return Lines.Reverse().Take(Math.Max(1, Math.Min(tail, Capacity))).Reverse().ToList();
        }
    }

    /// <summary>Empties the buffer (used by tests).</summary>
    public static void Clear()
    {
        lock (Sync)
        {
            Lines.Clear();
        }
    }
}
