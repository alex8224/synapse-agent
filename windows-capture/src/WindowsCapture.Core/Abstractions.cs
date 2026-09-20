using System.Diagnostics.CodeAnalysis;

namespace WindowsCapture.Core;

/// <summary>Stable machine-readable error codes shared by the RPC surface, CLI and tests.</summary>
public static class ErrorCodes
{
    public const string InvalidParams = "invalid_params";
    public const string InvalidRequest = "invalid_request";
    public const string ParseError = "parse_error";
    public const string MethodNotFound = "method_not_found";
    public const string Internal = "internal_error";

    public const string TargetRequired = "target_required";
    public const string TargetNotFound = "target_not_found";
    public const string TargetIdentityMismatch = "target_identity_mismatch";
    public const string TargetClosed = "target_closed";
    public const string TargetMinimized = "target_minimized";
    public const string NoFrame = "no_frame";
    public const string NoNewFrame = "no_new_frame";

    public const string CaptureBusy = "capture_busy";
    public const string CaptureFailed = "capture_failed";
    public const string Cancelled = "cancelled";
    public const string Timeout = "timeout";
    public const string LimitExceeded = "limit_exceeded";

    public const string JobNotFound = "job_not_found";
    public const string JobNotFinished = "job_not_finished";
    public const string ResultReleased = "result_released";
    public const string ResultExpired = "result_expired";
    public const string ResultRangeInvalid = "result_range_invalid";

    public const string RequestIdConflict = "request_id_conflict";
    public const string UiUnavailable = "ui_unavailable";
    public const string NotSupported = "not_supported";
}

/// <summary>
/// Error raised anywhere in the tool that must reach the caller with a stable code.
/// The RPC dispatcher converts it into a JSON-RPC error object carrying
/// <c>data.error_code</c> so non-.NET consumers (e.g. Python stdlib) can branch on it.
/// </summary>
public class CaptureException : Exception
{
    public string ErrorCode { get; }

    /// <summary>Optional extra detail surfaced as <c>data.detail</c>.</summary>
    public string? Detail { get; }

    public CaptureException(string errorCode, string message, string? detail = null, Exception? inner = null)
        : base(message, inner)
    {
        ErrorCode = errorCode;
        Detail = detail;
    }

    public static CaptureException InvalidParams(string message, string? detail = null)
        => new(ErrorCodes.InvalidParams, message, detail);
}

/// <summary>Hard upper bounds for the RPC surface and for capture jobs. Never unbounded.</summary>
public sealed record Limits
{
    /// <summary>Maximum size of a single newline-delimited JSON-RPC request frame.</summary>
    public int MaxRequestBytes { get; init; } = 1024 * 1024;

    /// <summary>Maximum base64 payload returned by one <c>read_result</c> call (decoded bytes).</summary>
    public int MaxChunkBytes { get; init; } = 256 * 1024;

    /// <summary>Maximum number of jobs retained at once.</summary>
    public int MaxJobs { get; init; } = 32;

    /// <summary>Maximum frames a single job may capture.</summary>
    public int MaxFramesPerJob { get; init; } = 600;

    /// <summary>Maximum total encoded bytes retained for one job's results.</summary>
    public long MaxJobResultBytes { get; init; } = 128L * 1024 * 1024;

    public int MaxIntervalMs { get; init; } = 600_000;
    public int MaxStartDelayMs { get; init; } = 600_000;

    /// <summary>Maximum value accepted for <c>max_edge</c> (0 means "keep native size").</summary>
    public int MaxEdge { get; init; } = 8192;

    public int DefaultTtlSeconds { get; init; } = 300;
    public int MaxTtlSeconds { get; init; } = 3600;

    /// <summary>How long to wait for a *new* frame from Windows Graphics Capture before reusing the last one.</summary>
    public int DefaultFrameTimeoutMs { get; init; } = 5_000;
    public int MaxFrameTimeoutMs { get; init; } = 60_000;

    /// <summary>Idempotency cache capacity, keyed by <c>request_id</c>.</summary>
    public int MaxRequestRegistryEntries { get; init; } = 256;

    /// <summary>Maximum number of clients served concurrently on the named pipe.</summary>
    public int MaxPipeInstances { get; init; } = 16;

    /// <summary>
    /// How long an established pipe connection may stay silent between requests before the server
    /// drops it. Without this deadline a client that connects and never sends anything (or a
    /// malicious same-user client) would hold one of the <see cref="MaxPipeInstances"/> slots
    /// forever and starve every other client.
    /// </summary>
    public int PipeIdleTimeoutMs { get; init; } = 30_000;

    /// <summary>How long the server waits to flush one response line before giving up on the client.</summary>
    public int PipeWriteTimeoutMs { get; init; } = 30_000;

    /// <summary>
    /// How often finished jobs are swept for TTL expiry, independently of incoming requests, so
    /// retained results do not outlive their TTL just because nobody called in.
    /// </summary>
    public int SweepIntervalMs { get; init; } = 30_000;

    /// <summary>Maximum <c>invoke_cli</c> wall clock before the forwarded call is abandoned.</summary>
    public int InvokeCliTimeoutMs { get; init; } = 300_000;
}

/// <summary>Injectable clock so TTL/idempotency logic is deterministic in tests.</summary>
public interface IClock
{
    long UtcNowMs { get; }
}

public sealed class SystemClock : IClock
{
    public static readonly SystemClock Instance = new();
    public long UtcNowMs => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
}

/// <summary>Manually advanced clock used by unit tests.</summary>
public sealed class ManualClock : IClock
{
    private long _nowMs;

    public ManualClock(long startMs = 1_700_000_000_000) => _nowMs = startMs;

    public long UtcNowMs => Interlocked.Read(ref _nowMs);
    public void Advance(long ms) => Interlocked.Add(ref _nowMs, ms);
    public void Set(long ms) => Interlocked.Exchange(ref _nowMs, ms);
}

/// <summary>Minimal process query surface so target-identity checks stay unit-testable.</summary>
public interface IProcessInfo
{
    bool TryGetStartUtc(int pid, out DateTime startUtc);
}

/// <summary>One captured frame, already encoded to an in-memory image.</summary>
public sealed record CapturedFrame
{
    public required byte[] Data { get; init; }
    public required string Mime { get; init; }
    public required int Width { get; init; }
    public required int Height { get; init; }

    /// <summary>Unix ms at which the underlying surface was produced by the OS (not when it was handed out).</summary>
    public required long CapturedAtUnixMs { get; init; }

    /// <summary>
    /// <c>fresh</c> when the OS produced this frame for the current tick; <c>reused</c> when the window
    /// had not recomposed and the previous frame is being reported again with its original timestamp.
    /// </summary>
    public string Source { get; init; } = FrameSources.Fresh;
}

public static class FrameSources
{
    public const string Fresh = "fresh";
    public const string Reused = "reused";
}

/// <summary>
/// Platform capture backend. The Windows implementation is a real Windows Graphics Capture
/// session created for the target HWND; tests supply a fake.
/// </summary>
public interface ICaptureEngine : IDisposable
{
    /// <summary>
    /// Capture a single frame for <paramref name="target"/>. Must never return a frame produced
    /// before the call unless it is explicitly flagged <see cref="FrameSources.Reused"/>.
    /// </summary>
    CapturedFrame Capture(TargetRef target, CapturePlan plan, CancellationToken cancellationToken);

    /// <summary>Drop any cached session/resources (target changed, identity failed, ...).</summary>
    void Invalidate();
}
