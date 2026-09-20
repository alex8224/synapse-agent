namespace WindowsCapture.Core;

/// <summary>
/// Resolves a caller supplied <see cref="CaptureRequest"/> against the saved <see cref="AppConfig"/>
/// and the built-in defaults, then validates it against <see cref="Limits"/>.
///
/// Precedence (highest first): temporary override &gt; saved config &gt; built-in default.
/// A temporary override is never written to the config file unless <c>save_config</c> is set.
/// </summary>
public static class CapturePlanner
{
    public static CapturePlan Resolve(CaptureRequest request, AppConfig? saved, Limits limits)
    {
        saved ??= AppConfig.Empty;

        var target = request.Target ?? saved.LastTarget;
        if (target is null || target.Hwnd == 0)
        {
            throw new CaptureException(
                ErrorCodes.TargetRequired,
                "no capture target is selected",
                "pass params.target {hwnd,pid[,process_start_utc]} or select a window in the UI");
        }

        var plan = new CapturePlan
        {
            Target = target,
            Count = request.Count ?? saved.Count ?? CaptureDefaults.Count,
            IntervalMs = request.IntervalMs ?? saved.IntervalMs ?? CaptureDefaults.IntervalMs,
            StartDelayMs = request.StartDelayMs ?? saved.StartDelayMs ?? CaptureDefaults.StartDelayMs,
            MaxEdge = request.MaxEdge ?? saved.MaxEdge ?? CaptureDefaults.MaxEdge,
            OutputDir = Normalise(request.OutputDir) ?? Normalise(saved.OutputDir),
            Format = (request.Format ?? saved.Format ?? CaptureDefaults.Format).Trim().ToLowerInvariant(),
            TtlSeconds = request.TtlSeconds ?? saved.TtlSeconds ?? limits.DefaultTtlSeconds,
            FrameTimeoutMs = request.FrameTimeoutMs ?? saved.FrameTimeoutMs ?? limits.DefaultFrameTimeoutMs,
            AllowReuse = request.AllowReuse ?? saved.AllowReuse ?? CaptureDefaults.AllowReuse,
        };

        Validate(plan, limits);
        return plan;
    }

    /// <summary>Throws <see cref="CaptureException"/> with <c>invalid_params</c> for anything out of range.</summary>
    public static void Validate(CapturePlan plan, Limits limits)
    {
        if (plan.Count < 1 || plan.Count > limits.MaxFramesPerJob)
            throw CaptureException.InvalidParams($"count must be between 1 and {limits.MaxFramesPerJob}", $"got {plan.Count}");

        if (plan.IntervalMs < 0 || plan.IntervalMs > limits.MaxIntervalMs)
            throw CaptureException.InvalidParams($"interval_ms must be between 0 and {limits.MaxIntervalMs}", $"got {plan.IntervalMs}");

        if (plan.StartDelayMs < 0 || plan.StartDelayMs > limits.MaxStartDelayMs)
            throw CaptureException.InvalidParams($"start_delay_ms must be between 0 and {limits.MaxStartDelayMs}", $"got {plan.StartDelayMs}");

        if (plan.MaxEdge < 0 || plan.MaxEdge > limits.MaxEdge)
            throw CaptureException.InvalidParams($"max_edge must be between 0 and {limits.MaxEdge} (0 = native size)", $"got {plan.MaxEdge}");

        if (plan.TtlSeconds < 1 || plan.TtlSeconds > limits.MaxTtlSeconds)
            throw CaptureException.InvalidParams($"ttl_seconds must be between 1 and {limits.MaxTtlSeconds}", $"got {plan.TtlSeconds}");

        if (plan.FrameTimeoutMs < 1 || plan.FrameTimeoutMs > limits.MaxFrameTimeoutMs)
            throw CaptureException.InvalidParams($"frame_timeout_ms must be between 1 and {limits.MaxFrameTimeoutMs}", $"got {plan.FrameTimeoutMs}");

        if (plan.Format is not ("png" or "image/png"))
            throw CaptureException.InvalidParams("only the png format is supported in v1", $"got '{plan.Format}'");
    }

    /// <summary>
    /// Applies a resolved plan (and its target) back into a config record. Used by
    /// <c>start</c> with <c>save_config: true</c>. Only fields the caller actually supplied are
    /// persisted, so a temporary override does not silently rewrite unrelated settings.
    /// </summary>
    public static AppConfig Merge(AppConfig current, CaptureRequest request, CapturePlan plan)
    {
        return current with
        {
            Count = request.Count ?? current.Count,
            IntervalMs = request.IntervalMs ?? current.IntervalMs,
            StartDelayMs = request.StartDelayMs ?? current.StartDelayMs,
            MaxEdge = request.MaxEdge ?? current.MaxEdge,
            OutputDir = Normalise(request.OutputDir) ?? current.OutputDir,
            Format = request.Format?.Trim().ToLowerInvariant() ?? current.Format,
            TtlSeconds = request.TtlSeconds ?? current.TtlSeconds,
            FrameTimeoutMs = request.FrameTimeoutMs ?? current.FrameTimeoutMs,
            AllowReuse = request.AllowReuse ?? current.AllowReuse,
            LastTarget = plan.Target,
        };
    }

    private static string? Normalise(string? value)
        => string.IsNullOrWhiteSpace(value) ? null : value.Trim();
}
