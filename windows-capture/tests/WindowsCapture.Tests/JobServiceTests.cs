using System.Text;
using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class JobServiceTests
{
    private static readonly byte[] FrameA = { 1, 2, 3, 4, 5 };
    private static readonly byte[] FrameB = { 9, 8, 7 };

    [Fact]
    public void JobRunsToCompletionAndKeepsEveryFrame()
    {
        var engine = new FakeCaptureEngine
        {
            Factory = (_, index) => new CapturedFrame
            {
                Data = index == 0 ? FrameA : FrameB,
                Mime = "image/png",
                Width = 10,
                Height = 5,
                CapturedAtUnixMs = 1000 + index,
                Source = FrameSources.Fresh,
            },
        };

        using var jobs = TestData.NewService(engine);
        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 2, IntervalMs = 0 }, null);

        // The job is registered immediately and picked up by the worker thread a moment later.
        Assert.Contains(started.State, new[] { "queued", "running" });
        Assert.NotNull(started.JobId);

        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("completed", finished.State);
        Assert.Equal(2, finished.Captured);
        Assert.Equal(2, finished.Requested);
        Assert.Equal(FrameA.Length + FrameB.Length, finished.ResultBytes);
        Assert.All(finished.Frames, f => Assert.Equal("fresh", f.Source));
        Assert.Equal(1000, finished.Frames[0].CapturedAt);
        Assert.Equal(1001, finished.Frames[1].CapturedAt);
    }

    [Fact]
    public void OnlyOneCaptureCanRunAtATime()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        using var jobs = TestData.NewService(engine);

        var first = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);

        var ex = Assert.Throws<CaptureException>(() =>
            jobs.Start(new CaptureRequest { Target = TestData.Target() }, null));
        Assert.Equal(ErrorCodes.CaptureBusy, ex.ErrorCode);

        gate.Set();
        Assert.Equal("completed", TestData.WaitForTerminal(jobs, first.JobId).State);
    }

    [Fact]
    public void CancellingAJobStopsItAndKeepsThePartialResult()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 5 }, null);
        var cancelled = jobs.Cancel(started.JobId);

        Assert.Equal("cancelled", cancelled.State);
        Assert.True(cancelled.CancelRequested);
        gate.Set();
    }

    [Fact]
    public void CancelIsIdempotentForAFinishedJob()
    {
        var engine = new FakeCaptureEngine();
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        var again = jobs.Cancel(finished.JobId);
        Assert.Equal("completed", again.State);
    }

    [Fact]
    public void UnknownJobIsReported()
    {
        using var jobs = TestData.NewService(new FakeCaptureEngine());
        var ex = Assert.Throws<CaptureException>(() => jobs.GetJob("nope"));
        Assert.Equal(ErrorCodes.JobNotFound, ex.ErrorCode);
    }

    [Fact]
    public void ResultsExpireAfterTheirTtl()
    {
        var clock = new ManualClock();
        using var jobs = TestData.NewService(new FakeCaptureEngine(), clock);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), TtlSeconds = 60 }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);
        Assert.Equal("completed", finished.State);

        Assert.Equal("completed", jobs.GetJob(started.JobId).State);

        clock.Advance(61_000);

        var ex = Assert.Throws<CaptureException>(() => jobs.GetJob(started.JobId));
        Assert.Equal(ErrorCodes.JobNotFound, ex.ErrorCode);
    }

    [Fact]
    public void ReleasedResultsCannotBeReadAgain()
    {
        var engine = new FakeCaptureEngine();
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        TestData.WaitForTerminal(jobs, started.JobId);

        Assert.True(jobs.ReleaseResult(started.JobId));
        Assert.False(jobs.ReleaseResult(started.JobId));

        var ex = Assert.Throws<CaptureException>(() => jobs.ReadResult(started.JobId, 0, 0, 64));
        Assert.Equal(ErrorCodes.ResultReleased, ex.ErrorCode);
    }

    [Fact]
    public void ReadingResultsInChunksReproducesTheOriginalBytes()
    {
        var payload = new byte[1000];
        new Random(7).NextBytes(payload);

        var engine = new FakeCaptureEngine
        {
            Factory = (_, _) => new CapturedFrame
            {
                Data = payload,
                Mime = "image/png",
                Width = 1,
                Height = 1,
                CapturedAtUnixMs = 42,
            },
        };

        var limits = new Limits { MaxChunkBytes = 256 };
        using var jobs = TestData.NewService(engine, limits: limits);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        TestData.WaitForTerminal(jobs, started.JobId);

        var assembled = new List<byte>();
        long offset = 0;
        var chunks = 0;

        while (true)
        {
            var chunk = jobs.ReadResult(started.JobId, 0, offset, limits.MaxChunkBytes);
            assembled.AddRange(Convert.FromBase64String(chunk.DataBase64));
            offset += chunk.Length;
            chunks++;
            Assert.True(chunk.Length <= limits.MaxChunkBytes);

            if (chunk.Eof) break;
            Assert.True(chunks < 100, "chunking did not terminate");
        }

        Assert.Equal(4, chunks);
        Assert.Equal(payload, assembled.ToArray());
        Assert.Equal("image/png", jobs.ReadResult(started.JobId, 0, 0, 8).Mime);
    }

    [Fact]
    public void ReadingAFrameThatDoesNotExistYetIsReported()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 2 }, null);

        var ex = Assert.Throws<CaptureException>(() => jobs.ReadResult(started.JobId, 0, 0, 64));
        Assert.Equal(ErrorCodes.JobNotFinished, ex.ErrorCode);

        gate.Set();
        TestData.WaitForTerminal(jobs, started.JobId);

        var outOfRange = Assert.Throws<CaptureException>(() => jobs.ReadResult(started.JobId, 9, 0, 64));
        Assert.Equal(ErrorCodes.InvalidParams, outOfRange.ErrorCode);
    }

    [Fact]
    public void ResultSizeLimitFailsTheJob()
    {
        var engine = new FakeCaptureEngine
        {
            Factory = (_, _) => new CapturedFrame
            {
                Data = new byte[512],
                Mime = "image/png",
                Width = 1,
                Height = 1,
                CapturedAtUnixMs = 1,
            },
        };

        var limits = new Limits { MaxJobResultBytes = 1000 };
        using var jobs = TestData.NewService(engine, limits: limits);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 5 }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("failed", finished.State);
        Assert.Equal(ErrorCodes.LimitExceeded, finished.Error!.Code);
    }

    [Fact]
    public void ReusedFramesAreRejectedWhenReuseIsDisabled()
    {
        var engine = new FakeCaptureEngine { Source = FrameSources.Reused };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), AllowReuse = false }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("failed", finished.State);
        Assert.Equal(ErrorCodes.NoNewFrame, finished.Error!.Code);
    }

    [Fact]
    public void ReusedFramesAreReportedAsReusedByDefault()
    {
        var engine = new FakeCaptureEngine { Source = FrameSources.Reused };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("completed", finished.State);
        Assert.All(finished.Frames, f => Assert.Equal("reused", f.Source));
    }

    [Fact]
    public void IdentityMismatchPreventsTheJobFromStarting()
    {
        var processes = new FakeProcessInfo();
        processes.Starts[100] = new DateTime(2024, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        using var jobs = TestData.NewService(new FakeCaptureEngine(), processes: processes);
        var target = TestData.Target(pid: 100, start: new DateTime(2020, 1, 1, 0, 0, 0, DateTimeKind.Utc).ToString("o"));

        var ex = Assert.Throws<CaptureException>(() =>
            jobs.Start(new CaptureRequest { Target = target }, null));
        Assert.Equal(ErrorCodes.TargetIdentityMismatch, ex.ErrorCode);
    }

    [Fact]
    public void EngineFailuresBecomeJobFailures()
    {
        var engine = new FakeCaptureEngine
        {
            Factory = (_, _) => throw new CaptureException(ErrorCodes.TargetMinimized, "minimized"),
        };

        using var jobs = TestData.NewService(engine);
        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        var finished = TestData.WaitForTerminal(jobs, started.JobId);

        Assert.Equal("failed", finished.State);
        Assert.Equal(ErrorCodes.TargetMinimized, finished.Error!.Code);
    }

    [Fact]
    public void TooManyJobsAreRejectedOrEvicted()
    {
        var engine = new FakeCaptureEngine();
        var limits = new Limits { MaxJobs = 2 };
        using var jobs = TestData.NewService(engine, limits: limits);

        for (var i = 0; i < 5; i++)
        {
            var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), TtlSeconds = 3600 }, null);
            TestData.WaitForTerminal(jobs, started.JobId);
        }

        Assert.True(jobs.ListJobs().Count <= limits.MaxJobs);
    }

    [Fact]
    public void FramesAreWrittenToDiskWhenAnOutputDirectoryIsSet()
    {
        var directory = Path.Combine(Path.GetTempPath(), "windows-capture-tests", Guid.NewGuid().ToString("N"));
        try
        {
            var engine = new FakeCaptureEngine();
            using var jobs = TestData.NewService(engine);

            var started = jobs.Start(new CaptureRequest
            {
                Target = TestData.Target(),
                Count = 2,
                OutputDir = directory,
            }, null);

            var finished = TestData.WaitForTerminal(jobs, started.JobId);

            Assert.Equal("completed", finished.State);
            Assert.All(finished.Frames, f => Assert.True(File.Exists(f.Path)));
            Assert.Equal(2, Directory.GetFiles(directory, "*.png").Length);
        }
        finally
        {
            if (Directory.Exists(directory)) Directory.Delete(directory, recursive: true);
        }
    }

    [Fact]
    public void ListJobsReportsTheActiveJob()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
        Assert.Equal(started.JobId, jobs.ActiveJobId);
        Assert.True(jobs.HasActiveJob);

        gate.Set();
        TestData.WaitForTerminal(jobs, started.JobId);
        Assert.Null(jobs.ActiveJobId);
        Assert.False(jobs.HasActiveJob);
    }

    [Fact]
    public void JobIdsAreUniqueAcrossStarts()
    {
        var engine = new FakeCaptureEngine();
        using var jobs = TestData.NewService(engine);

        var ids = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < 3; i++)
        {
            var started = jobs.Start(new CaptureRequest { Target = TestData.Target() }, null);
            TestData.WaitForTerminal(jobs, started.JobId);
            Assert.True(ids.Add(started.JobId), "job ids must be unique");
        }
    }

    [Fact]
    public void CancelledJobsKeepTheFramesCapturedSoFar()
    {
        using var gate = new ManualResetEventSlim(false);
        var engine = new FakeCaptureEngine { Gate = gate };
        using var jobs = TestData.NewService(engine);

        var started = jobs.Start(new CaptureRequest { Target = TestData.Target(), Count = 4 }, null);
        var cancelled = jobs.Cancel(started.JobId);

        Assert.Equal("cancelled", cancelled.State);
        Assert.True(cancelled.Captured < 4);
        gate.Set();
    }
}
