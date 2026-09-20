using System.Collections.Concurrent;
using System.Diagnostics;
using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

/// <summary>
/// Regression tests for the parts of <see cref="JobService"/> that used to be racy:
/// releasing a result while the job is still running, the disposal of the per-job completion
/// primitives, and the TTL sweep that must happen even when nobody calls in.
/// </summary>
public class JobServiceLifetimeTests
{
    private static void WaitUntil(Func<bool> condition, int timeoutMs = 5_000)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            if (condition()) return;
            Thread.Sleep(5);
        }

        throw new TimeoutException("condition was not met in time");
    }

    [Fact]
    public void ReleasingResultOfARunningJobFreesPayloadsAndStopsRetention()
    {
        using var holdSecondFrame = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine
        {
            Factory = (_, index) =>
            {
                // Park the second capture so the release lands while the job is still running.
                if (index == 1) holdSecondFrame.Wait(TimeSpan.FromSeconds(10));
                return new CapturedFrame
                {
                    Data = new byte[] { (byte)index, 1, 2, 3 },
                    Mime = "image/png",
                    Width = 1,
                    Height = 1,
                    CapturedAtUnixMs = 1000 + index,
                };
            },
        };

        using var jobs = TestData.NewService(engine);
        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 4, IntervalMs = 0 }, null);

        WaitUntil(() => jobs.GetJob(started.JobId).Captured >= 1);

        Assert.True(jobs.ReleaseResult(started.JobId));
        var released = jobs.GetJob(started.JobId);
        Assert.True(released.ResultReleased);
        Assert.Equal(0, released.ResultBytes);

        holdSecondFrame.Set();

        var finished = TestData.WaitForTerminal(jobs, started.JobId);
        Assert.Equal("completed", finished.State);

        // Nothing captured after the release is retained: the freed memory stays freed and the
        // Frames/Payloads lists cannot drift apart.
        Assert.Equal(0, finished.ResultBytes);
        Assert.Equal(1, finished.Captured);

        var ex = Assert.Throws<CaptureException>(() => jobs.ReadResult(started.JobId, 0, 0, 64));
        Assert.Equal(ErrorCodes.ResultReleased, ex.ErrorCode);
    }

    [Fact]
    public void ReusedFramesKeepTheirOriginalTimestamp()
    {
        const long capturedAt = 1_234_567;
        var engine = new FakeCaptureEngine
        {
            Factory = (_, _) => new CapturedFrame
            {
                Data = new byte[] { 7 },
                Mime = "image/png",
                Width = 1,
                Height = 1,
                CapturedAtUnixMs = capturedAt,
                Source = FrameSources.Reused,
            },
        };

        using var jobs = TestData.NewService(engine);
        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 3 }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("completed", finished.State);
        Assert.All(finished.Frames, f => Assert.Equal(FrameSources.Reused, f.Source));
        // A reused frame is reported with its original timestamp, never a fabricated "now".
        Assert.All(finished.Frames, f => Assert.Equal(capturedAt, f.CapturedAt));
    }

    [Fact]
    public void ExpiredJobsAreSweptWithoutAnyRequest()
    {
        var clock = new ManualClock();
        var limits = new Limits { SweepIntervalMs = 25 };
        using var jobs = TestData.NewService(new FakeCaptureEngine(), clock: clock, limits: limits);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), TtlSeconds = 1 }, null);
        TestData.WaitForTerminal(jobs, started.JobId);

        // JobCount does not sweep, so the value can only change because of the background timer.
        Assert.Equal(1, jobs.JobCount);

        clock.Advance(2_000); // past the 1 s TTL

        WaitUntil(() => jobs.JobCount == 0);
    }

    [Fact]
    public async Task DisposingWhileAJobRunsCancelsItCleanly()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 3 }, null);

        var disposed = Task.Run(() => jobs.Dispose());
        var completed = await Task.WhenAny(disposed, Task.Delay(TimeSpan.FromSeconds(10)));
        Assert.Same(disposed, completed);

        gate.Set();
        Assert.Throws<ObjectDisposedException>(() => jobs.GetJob(started.JobId));
    }

    [Fact]
    public async Task CancelSweepAndStartCanRunConcurrentlyWithoutRacingDisposal()
    {
        var limits = new Limits { MaxJobs = 2 };
        using var jobs = TestData.NewService(new FakeCaptureEngine(), limits: limits);

        var flag = new Flag();
        var errors = new ConcurrentBag<Exception>();
        var ids = new ConcurrentQueue<string>();

        void Starter()
        {
            try
            {
                while (!flag.Stopped)
                {
                    try { ids.Enqueue(jobs.Start(new CaptureRequest { Target = TestData.Target(), TtlSeconds = 1 }, null).JobId); }
                    catch (CaptureException) { /* capture_busy is expected */ }
                }
            }
            catch (Exception ex) { errors.Add(ex); }
        }

        void Canceller()
        {
            try
            {
                while (!flag.Stopped)
                {
                    foreach (var id in ids)
                    {
                        try { jobs.Cancel(id); }
                        catch (CaptureException) { /* job_not_found after eviction is expected */ }
                    }
                }
            }
            catch (Exception ex) { errors.Add(ex); }
        }

        void Sweeper()
        {
            try
            {
                while (!flag.Stopped)
                {
                    try { jobs.ListJobs(); jobs.SweepExpired(); }
                    catch (CaptureException) { }
                }
            }
            catch (Exception ex) { errors.Add(ex); }
        }

        var workers = new[] { Task.Run(Starter), Task.Run(Canceller), Task.Run(Sweeper) };
        await Task.Delay(750);
        flag.Stopped = true;
        await Task.WhenAll(workers);

        Assert.Empty(errors);
    }

    private sealed class Flag
    {
        public volatile bool Stopped;
    }
}
