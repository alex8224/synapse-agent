using System.Globalization;

namespace WindowsCapture.Core;

public enum IdentityStatus
{
    /// <summary>No process start time was supplied; HWND+PID were used as-is.</summary>
    NotRequested,

    /// <summary>Process start time matched (within tolerance).</summary>
    Verified,

    /// <summary>Process start time could not be read (access denied, already exited).</summary>
    Unverifiable,

    /// <summary>Process start time did not match: the HWND almost certainly belongs to another process now.</summary>
    Mismatch,
}

public readonly record struct IdentityResult(IdentityStatus Status, string? Detail)
{
    public bool IsUsable => Status is IdentityStatus.Verified or IdentityStatus.NotRequested or IdentityStatus.Unverifiable;
}

/// <summary>
/// Validates a persisted/selected target before capture. Windows recycles HWNDs, so an HWND alone
/// is never trusted across restarts: the owning PID and its process start time are re-checked.
/// </summary>
public static class TargetIdentity
{
    /// <summary>Tolerance for start-time comparison; process start times round-trip through ISO-8601.</summary>
    public static readonly TimeSpan Tolerance = TimeSpan.FromSeconds(2);

    public static IdentityResult Check(TargetRef target, IProcessInfo processes)
    {
        if (string.IsNullOrWhiteSpace(target.ProcessStartUtc))
            return new IdentityResult(IdentityStatus.NotRequested, "no process_start_utc supplied; HWND+PID used as-is");

        if (!DateTime.TryParse(
                target.ProcessStartUtc,
                CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal,
                out var expected))
        {
            return new IdentityResult(IdentityStatus.Unverifiable, $"unparsable process_start_utc '{target.ProcessStartUtc}'");
        }

        if (target.Pid <= 0)
            return new IdentityResult(IdentityStatus.Unverifiable, "pid is not positive");

        if (!processes.TryGetStartUtc(target.Pid, out var actual))
            return new IdentityResult(IdentityStatus.Unverifiable, $"cannot read start time of pid {target.Pid}");

        var delta = (actual.ToUniversalTime() - expected.ToUniversalTime()).Duration();
        if (delta <= Tolerance)
            return new IdentityResult(IdentityStatus.Verified, null);

        return new IdentityResult(
            IdentityStatus.Mismatch,
            $"pid {target.Pid} start time differs by {delta.TotalSeconds:F1}s; the HWND was most likely reused");
    }

    /// <summary>Throws for a hard mismatch; every other status is reported to the caller as a warning.</summary>
    public static void ThrowIfMismatch(TargetRef target, IProcessInfo processes)
    {
        var result = Check(target, processes);
        if (result.Status == IdentityStatus.Mismatch)
        {
            throw new CaptureException(
                ErrorCodes.TargetIdentityMismatch,
                "the target window no longer belongs to the recorded process",
                result.Detail);
        }
    }
}
