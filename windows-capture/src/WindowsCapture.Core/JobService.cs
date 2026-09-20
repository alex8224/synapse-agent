namespace WindowsCapture.Core;

/// <summary>
/// Owns all capture jobs. There is exactly one instance per process and it is shared by the GUI and
/// the RPC server, so a single capture job can be active at a time regardless of who asked for it.
/// </summary>
public sealed class JobService : IDisposable
{
    private sealed class Job
    {
        public required string Id;
        public required CapturePlan Plan;
        public JobState State = JobState.Queued;
        public readonly List<FrameMeta> Frames = new();
        public readonly List<byte[]> Payloads = new();
        public long ResultBytes;
        public long CreatedAt;
        public long? StartedAt;
        public long? FinishedAt;
        public bool CancelRequested;
        public bool ResultReleased;
        public JobError? Error;
        public CancellationTokenSource Cts = new();
        public Task? Runner;
        public readonly object Sync = new();
    }

    private readonly ICaptureEngine _engine;
    private readonly Limits _limits;
    private readonly IClock _clock;
    private readonly IProcessInfo _processes;
    private readonly Func<string> _idFactory;
    private readonly object _sync = new();
    private readonly Dictionary<string, Job> _jobs = new(StringComparer.Ordinal);
    private readonly Timer? _sweepTimer;
    private bool _disposed;

    public JobService(ICaptureEngine engine, Limits limits, IClock clock, IProcessInfo processes, Func<string>? idFactory = null)
    {
        _engine = engine;
        _limits = limits;
        _clock = clock;
        _processes = processes;
        _idFactory = idFactory ?? DefaultJobId;

        // Sweep TTL-expired jobs on a timer so retained results do not outlive their TTL just because
        // no request happened to arrive. The sweep reads the injected clock, so tests with a
        // ManualClock stay deterministic.
        var sweepMs = _limits.SweepIntervalMs;
        if (sweepMs > 0)
        {
            _sweepTimer = new System.Threading.Timer(
                static state => { try { ((JobService)state!).SweepExpired(); } catch (Exception) { } },
                this,
                sweepMs,
                sweepMs);
        }
    }

    public Limits Limits => _limits;

    /// <summary>
    /// Number of jobs currently retained. Diagnostics only; unlike <see cref="ListJobs"/> it does not
    /// sweep, so it can be used to observe the background TTL sweep.
    /// </summary>
    public int JobCount
    {
        get { lock (_sync) return _jobs.Count; }
    }

    public bool HasActiveJob
    {
        get
        {
            lock (_sync)
                return _jobs.Values.Any(static j => j.State is JobState.Queued or JobState.Running);
        }
    }

    public string? ActiveJobId
    {
        get
        {
            lock (_sync)
                return _jobs.Values.FirstOrDefault(static j => j.State is JobState.Queued or JobState.Running)?.Id;
        }
    }

    /// <summary>
    /// Validate + plan + schedule. Returns as soon as the job is registered; frames are captured on
    /// a background task. Throws <see cref="CaptureException"/> for anything the caller must fix.
    /// </summary>
    public JobView Start(CaptureRequest request, AppConfig? saved)
    {
        ThrowIfDisposed();

        var plan = CapturePlanner.Resolve(request, saved, _limits);
        TargetIdentity.ThrowIfMismatch(plan.Target, _processes);

        var now = _clock.UtcNowMs;
        var job = new Job
        {
            Id = _idFactory(),
            Plan = plan,
            CreatedAt = now,
        };

        lock (_sync)
        {
            SweepExpiredLocked(now);

            var active = _jobs.Values.FirstOrDefault(static j => j.State is JobState.Queued or JobState.Running);
            if (active is not null)
            {
                throw new CaptureException(
                    ErrorCodes.CaptureBusy,
                    "another capture job is already running",
                    $"active job {active.Id}; wait for it or cancel it first");
            }

            if (_jobs.Count >= _limits.MaxJobs && !EvictOneLocked())
            {
                throw new CaptureException(
                    ErrorCodes.LimitExceeded,
                    "too many jobs are being retained",
                    $"limit {_limits.MaxJobs}; release results or wait for the TTL to expire");
            }

            _jobs[job.Id] = job;
            // Assign the worker while still holding the lock so any observer that can see the job also
            // sees a non-null Runner (used by Cancel/Dispose to wait for completion).
            job.Runner = Task.Run(() => RunJob(job));
        }

        return Snapshot(job);
    }

    public JobView GetJob(string jobId)
    {
        ThrowIfDisposed();
        lock (_sync)
        {
            SweepExpiredLocked(_clock.UtcNowMs);
            return Snapshot(GetJobLocked(jobId));
        }
    }

    public IReadOnlyList<JobView> ListJobs()
    {
        ThrowIfDisposed();
        lock (_sync)
        {
            SweepExpiredLocked(_clock.UtcNowMs);
            return _jobs.Values
                .OrderByDescending(static j => j.CreatedAt)
                .Select(Snapshot)
                .ToList();
        }
    }

    /// <summary>
    /// Requests cancellation and waits (bounded) for the job to reach a terminal state so the
    /// returned snapshot is usually already <c>cancelled</c>.
    /// </summary>
    public JobView Cancel(string jobId)
    {
        ThrowIfDisposed();
        Job job;
        lock (_sync)
        {
            SweepExpiredLocked(_clock.UtcNowMs);
            job = GetJobLocked(jobId);
            if (job.State is JobState.Completed or JobState.Cancelled or JobState.Failed) return Snapshot(job);
            job.CancelRequested = true;
        }

        try { job.Cts.Cancel(); }
        catch (ObjectDisposedException) { /* already finished */ }

        // Wait (bounded) on the worker task, not a disposable event: the task is never disposed, so a
        // concurrent sweep that removes the job cannot make this wait throw.
        try { job.Runner?.Wait(TimeSpan.FromSeconds(5)); }
        catch (AggregateException) { /* the worker catches everything; never let it escape */ }
        lock (_sync) return Snapshot(job);
    }

    /// <summary>Reads one base64 chunk of one frame. Partial reads of a running job are allowed.</summary>
    public ResultChunk ReadResult(string jobId, int index, long offset, int length)
    {
        ThrowIfDisposed();
        Job job;
        lock (_sync)
        {
            SweepExpiredLocked(_clock.UtcNowMs);
            job = GetJobLocked(jobId);
            lock (job.Sync)
            {
                if (job.ResultReleased)
                    throw new CaptureException(ErrorCodes.ResultReleased, "the result of this job has already been released", $"job {jobId}");

                if (index < 0 || index >= job.Frames.Count)
                {
                    var stillRunning = job.State is JobState.Queued or JobState.Running;
                    throw new CaptureException(
                        stillRunning ? ErrorCodes.JobNotFinished : ErrorCodes.InvalidParams,
                        stillRunning
                            ? $"frame {index} is not available yet ({job.Frames.Count} captured so far)"
                            : $"frame index {index} is out of range",
                        $"job {jobId} has {job.Frames.Count} frames");
                }

                var payload = job.Payloads[index];
                var (effective, eof) = ResultChunker.Normalise(payload.LongLength, offset, length, _limits.MaxChunkBytes);
                var meta = job.Frames[index];

                return new ResultChunk
                {
                    JobId = job.Id,
                    Index = index,
                    FrameCount = job.Frames.Count,
                    Offset = offset,
                    Length = effective,
                    TotalBytes = payload.LongLength,
                    Eof = eof,
                    Mime = meta.Mime,
                    Width = meta.Width,
                    Height = meta.Height,
                    CapturedAt = meta.CapturedAt,
                    Source = meta.Source,
                    DataBase64 = Convert.ToBase64String(payload, (int)offset, effective),
                };
            }
        }
    }

    /// <summary>Frees a job's frames immediately. Idempotent: releasing twice is not an error.</summary>
    public bool ReleaseResult(string jobId)
    {
        ThrowIfDisposed();
        lock (_sync)
        {
            SweepExpiredLocked(_clock.UtcNowMs);
            var job = GetJobLocked(jobId);
            lock (job.Sync)
            {
                var had = !job.ResultReleased;
                job.ResultReleased = true;
                job.Payloads.Clear();
                job.ResultBytes = 0;
                return had;
            }
        }
    }

    /// <summary>
    /// Removes finished jobs whose TTL elapsed. Released jobs keep their (tiny) metadata until then
    /// so a later read reports <c>result_released</c> instead of <c>job_not_found</c>. Runs on a timer
    /// and on every request.
    /// </summary>
    public int SweepExpired()
    {
        lock (_sync) return SweepExpiredLocked(_clock.UtcNowMs);
    }

    private int SweepExpiredLocked(long now)
    {
        // Note: releasing a result frees the payloads but keeps the (tiny) job metadata until its
        // TTL, so a later read reports `result_released` instead of a confusing `job_not_found`.
        // The job-count cap plus EvictOneLocked keeps the number of retained jobs bounded.
        var doomed = _jobs.Values
            .Where(j => j.State is JobState.Completed or JobState.Cancelled or JobState.Failed)
            .Where(j => now >= ExpiryOf(j))
            .Select(static j => j.Id)
            .ToList();

        foreach (var id in doomed) RemoveJobLocked(id);
        return doomed.Count;
    }

    private bool EvictOneLocked()
    {
        var oldest = _jobs.Values
            .Where(static j => j.State is JobState.Completed or JobState.Cancelled or JobState.Failed)
            .OrderBy(static j => j.FinishedAt ?? j.CreatedAt)
            .FirstOrDefault();

        if (oldest is null) return false;
        RemoveJobLocked(oldest.Id);
        return true;
    }

    private void RemoveJobLocked(string id)
    {
        if (!_jobs.Remove(id, out var job)) return;
        // The worker owns the CTS for its whole lifetime. If it has not stopped yet, leave the CTS to
        // the GC instead of racing a token that may still be in flight.
        if (job.Runner is null || job.Runner.IsCompleted)
        {
            try { job.Cts.Dispose(); } catch (ObjectDisposedException) { }
        }
    }

    private long ExpiryOf(Job job) => (job.FinishedAt ?? job.CreatedAt) + job.Plan.TtlSeconds * 1000L;

    private Job GetJobLocked(string jobId)
    {
        if (string.IsNullOrEmpty(jobId))
            throw CaptureException.InvalidParams("job_id is required");
        if (!_jobs.TryGetValue(jobId, out var job))
            throw new CaptureException(ErrorCodes.JobNotFound, "no such job", $"job_id={jobId}");
        return job;
    }

    private void RunJob(Job job)
    {
        var ct = job.Cts.Token;
        try
        {
            lock (_sync) job.StartedAt = _clock.UtcNowMs;
            SetState(job, JobState.Running);

            if (job.Plan.StartDelayMs > 0) Task.Delay(job.Plan.StartDelayMs, ct).GetAwaiter().GetResult();

            for (var i = 0; i < job.Plan.Count; i++)
            {
                ct.ThrowIfCancellationRequested();
                if (i > 0 && job.Plan.IntervalMs > 0) Task.Delay(job.Plan.IntervalMs, ct).GetAwaiter().GetResult();

                var frame = _engine.Capture(job.Plan.Target, job.Plan, ct);

                if (!job.Plan.AllowReuse && frame.Source == FrameSources.Reused)
                {
                    throw new CaptureException(
                        ErrorCodes.NoNewFrame,
                        "the target window did not produce a new frame",
                        $"waited {job.Plan.FrameTimeoutMs} ms; the window may be static. Set allow_reuse=true to accept the previous frame.");
                }

                string? path = null;
                if (!string.IsNullOrEmpty(job.Plan.OutputDir))
                    path = WriteFrameFile(job, i, frame);

                lock (job.Sync)
                {
                    // A release that lands while the job is still running frees the payloads and stops
                    // further retention, so Frames and Payloads can never drift out of index alignment
                    // (and the freed memory stays freed).
                    if (!job.ResultReleased)
                    {
                        if (job.ResultBytes + frame.Data.Length > _limits.MaxJobResultBytes)
                        {
                            throw new CaptureException(
                                ErrorCodes.LimitExceeded,
                                "job result size limit exceeded",
                                $"{job.ResultBytes + frame.Data.Length} > {_limits.MaxJobResultBytes} bytes; lower count/max_edge or save to output_dir");
                        }

                        job.Frames.Add(new FrameMeta
                        {
                            Index = i,
                            CapturedAt = frame.CapturedAtUnixMs,
                            Mime = frame.Mime,
                            Width = frame.Width,
                            Height = frame.Height,
                            Bytes = frame.Data.Length,
                            Source = frame.Source,
                            Path = path,
                        });
                        job.Payloads.Add(frame.Data);
                        job.ResultBytes += frame.Data.Length;
                    }
                }
            }

            SetState(job, JobState.Completed);
        }
        catch (OperationCanceledException)
        {
            SetState(job, JobState.Cancelled);
        }
        catch (CaptureException ex)
        {
            Fail(job, ex.ErrorCode, ex.Message);
        }
        catch (Exception ex)
        {
            Fail(job, ErrorCodes.CaptureFailed, ex.Message);
        }
        finally
        {
            lock (_sync)
            {
                job.FinishedAt ??= _clock.UtcNowMs;
                if (job.State == JobState.Running || job.State == JobState.Queued) job.State = JobState.Failed;
            }
        }
    }

    private string? WriteFrameFile(Job job, int index, CapturedFrame frame)
    {
        try
        {
            var dir = job.Plan.OutputDir!;
            Directory.CreateDirectory(dir);
            var ext = frame.Mime == "image/png" ? "png" : "bin";
            var path = System.IO.Path.Combine(dir, $"{job.Id}_{index:D4}.{ext}");
            File.WriteAllBytes(path, frame.Data);
            return path;
        }
        catch (Exception ex)
        {
            // Saving to disk is best effort; the in-memory result stays authoritative.
            return $"<write failed: {ex.Message}>";
        }
    }

    private void SetState(Job job, JobState state)
    {
        lock (_sync) job.State = state;
    }

    private void Fail(Job job, string code, string message)
    {
        lock (_sync)
        {
            job.State = JobState.Failed;
            job.Error = new JobError { Code = code, Message = message };
        }
    }

    private JobView Snapshot(Job job)
    {
        lock (job.Sync)
        {
            return new JobView
            {
                JobId = job.Id,
                State = job.State switch
                {
                    JobState.Queued => "queued",
                    JobState.Running => "running",
                    JobState.Completed => "completed",
                    JobState.Cancelled => "cancelled",
                    _ => "failed",
                },
                Target = job.Plan.Target,
                Requested = job.Plan.Count,
                Captured = job.Frames.Count,
                CreatedAt = job.CreatedAt,
                StartedAt = job.StartedAt,
                FinishedAt = job.FinishedAt,
                ExpiresAt = ExpiryOf(job),
                ResultReleased = job.ResultReleased,
                CancelRequested = job.CancelRequested,
                ResultBytes = job.ResultBytes,
                Frames = job.Frames.ToList(),
                Error = job.Error,
                Plan = job.Plan,
            };
        }
    }

    private static string DefaultJobId()
        => $"job-{DateTime.UtcNow:yyyyMMddHHmmssfff}-{Guid.NewGuid().ToString("N")[..8]}";

    private void ThrowIfDisposed()
    {
        if (_disposed) throw new ObjectDisposedException(nameof(JobService));
    }

    public void Dispose()
    {
        _sweepTimer?.Dispose();

        List<Job> jobs;
        lock (_sync)
        {
            if (_disposed) return;
            _disposed = true;
            jobs = _jobs.Values.ToList();
            _jobs.Clear();
        }

        foreach (var job in jobs)
        {
            try { job.Cts.Cancel(); } catch (ObjectDisposedException) { }
        }

        foreach (var job in jobs)
        {
            try { job.Runner?.Wait(TimeSpan.FromSeconds(2)); } catch (Exception) { }
            try { job.Cts.Dispose(); } catch (ObjectDisposedException) { }
        }

        _engine.Dispose();
    }
}
