using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace Jellyfin.Plugin.ThabitTranslator.Runtime;

/// <summary>
/// Serializes CLI runs (torch/Argos saturate the CPU and Jellyfin transcodes on
/// the same box) and de-duplicates in-flight work by (item, language), so a manual
/// click and the scheduled task can never process the same subtitle twice.
///
/// Cancelling one waiter only stops that waiter; the CLI process is killed once
/// the last waiter for that key is gone.
/// </summary>
public sealed class ThabitJobQueue
{
    private readonly ThabitRunner _runner;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly ConcurrentDictionary<string, Inflight> _inflight = new(StringComparer.Ordinal);
    private int _active;

    public ThabitJobQueue(ThabitRunner runner)
    {
        _runner = runner;
    }

    /// <summary>Number of distinct jobs waiting or running.</summary>
    public int PendingCount => _inflight.Count;

    /// <summary>True while a CLI process is running.</summary>
    public bool IsBusy => Volatile.Read(ref _active) > 0;

    /// <summary>
    /// Warms the runtime (venv bootstrap) under the same gate as real jobs, so a
    /// pip install can never race a subtitle run. Returns null when ready.
    /// </summary>
    public async Task<string?> PrepareRuntimeAsync(IProgress<double>? progress, CancellationToken cancellationToken)
    {
        try
        {
            await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (ObjectDisposedException)
        {
            return "the job queue is shutting down";
        }

        Interlocked.Increment(ref _active);
        try
        {
            return await _runner.PrepareRuntimeAsync(progress, cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            return ex.Message;
        }
        finally
        {
            Interlocked.Decrement(ref _active);
            _gate.Release();
        }
    }

    /// <summary>Enqueues a job, or attaches to an identical one already in flight.</summary>
    public async Task<ThabitJobResult> EnqueueAsync(ThabitJob job, IProgress<double>? progress, CancellationToken cancellationToken)
    {
        var key = job.Key;
        var created = false;
        Inflight inflight;
        while (true)
        {
            if (_inflight.TryGetValue(key, out var existing))
            {
                inflight = existing;
                break;
            }

            var candidate = new Inflight();
            if (_inflight.TryAdd(key, candidate))
            {
                inflight = candidate;
                created = true;
                break;
            }

            // Lost the race to another thread - retry and attach to theirs.
        }

        if (created)
        {
            inflight.Start(job, this);
            var finished = inflight.Task;
            _ = finished.ContinueWith(
                static (task, state) =>
                {
                    _ = task.Exception; // observe faults from cancelled runs nobody awaits
                    var tuple = ((ThabitJobQueue Queue, string Key, Inflight Job))state!;
                    tuple.Queue._inflight.TryRemove(new KeyValuePair<string, Inflight>(tuple.Key, tuple.Job));
                },
                (this, key, inflight),
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }

        inflight.AddWaiter();
        inflight.AddProgress(progress);
        var waiterRemoved = false;
        try
        {
            using var registration = cancellationToken.Register(() =>
            {
                if (!waiterRemoved)
                {
                    waiterRemoved = true;
                    inflight.RemoveWaiter();
                }
            });

            return await inflight.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            if (!waiterRemoved)
            {
                waiterRemoved = true;
                inflight.RemoveWaiter();
            }
        }
    }

    /// <summary>A single shared run with fan-out progress and ref-counted cancellation.</summary>
    private sealed class Inflight
    {
        private readonly CancellationTokenSource _cts = new();
        private readonly List<IProgress<double>> _progress = new();
        private readonly object _sync = new();
        private int _waiters;
        private TaskCompletionSource<ThabitJobResult>? _source;

        public Task<ThabitJobResult> Task
        {
            get
            {
                lock (_sync)
                {
                    return _source?.Task ?? throw new InvalidOperationException("Job was not started.");
                }
            }
        }

        public void Start(ThabitJob job, ThabitJobQueue owner)
        {
            lock (_sync)
            {
                _source ??= new TaskCompletionSource<ThabitJobResult>(TaskCreationOptions.RunContinuationsAsynchronously);
            }

            _ = RunAsync(job, owner);
        }

        public void AddWaiter() => Interlocked.Increment(ref _waiters);

        public void RemoveWaiter()
        {
            if (Interlocked.Decrement(ref _waiters) > 0)
            {
                return;
            }

            // Nobody is waiting for this run any more - stop burning CPU on it.
            try
            {
                _cts.Cancel();
            }
            catch (ObjectDisposedException)
            {
                // The run already finished and disposed its own source.
            }
        }

        public void AddProgress(IProgress<double>? progress)
        {
            if (progress is null)
            {
                return;
            }

            lock (_sync)
            {
                _progress.Add(progress);
            }
        }

        private void Report(double value)
        {
            IProgress<double>[] targets;
            lock (_sync)
            {
                if (_progress.Count == 0)
                {
                    return;
                }

                targets = _progress.ToArray();
            }

            foreach (var target in targets)
            {
                target.Report(value);
            }
        }

        private async Task RunAsync(ThabitJob job, ThabitJobQueue owner)
        {
            TaskCompletionSource<ThabitJobResult> source;
            lock (_sync)
            {
                source = _source!;
            }

            try
            {
                await owner._gate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
            }
            catch (ObjectDisposedException)
            {
                source.TrySetCanceled();
                return;
            }

            Interlocked.Increment(ref owner._active);
            ThabitJobResult result;
            try
            {
                result = await owner._runner
                    .RunAsync(job, new FanOutProgress(Report), _cts.Token)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                source.TrySetCanceled();
                return;
            }
            catch (Exception ex)
            {
                source.TrySetException(ex);
                return;
            }
            finally
            {
                Interlocked.Decrement(ref owner._active);
                owner._gate.Release();
                _cts.Dispose();
            }

            source.TrySetResult(result);
        }

        private sealed class FanOutProgress : IProgress<double>
        {
            private readonly Action<double> _report;

            public FanOutProgress(Action<double> report) => _report = report;

            public void Report(double value) => _report(value);
        }
    }
}
