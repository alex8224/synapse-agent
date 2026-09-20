using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace WindowsCapture.Core;

/// <summary>
/// Identity of the window we are allowed to capture: HWND plus the owning PID plus (when known)
/// the owning process start time. The start time is what makes an HWND safe to re-validate across
/// process restarts, because Windows recycles window handles.
/// </summary>
[JsonConverter(typeof(TargetRefJsonConverter))]
public sealed record TargetRef
{
    public long Hwnd { get; init; }
    public int Pid { get; init; }

    /// <summary>Owning process start time, ISO-8601 UTC. Optional but strongly recommended.</summary>
    public string? ProcessStartUtc { get; init; }

    public string? Title { get; init; }

    [JsonIgnore]
    public string HwndHex => "0x" + Hwnd.ToString("X", CultureInfo.InvariantCulture);

    public override string ToString()
        => $"{HwndHex}/pid={Pid}{(string.IsNullOrEmpty(Title) ? "" : $" \"{Title}\"")}";
}

/// <summary>
/// Reads/writes <see cref="TargetRef"/> with <c>hwnd</c> accepted either as a hex string
/// (<c>"0x1A2B"</c>), a decimal string, or a JSON number, because Python callers naturally
/// produce all three.
/// </summary>
public sealed class TargetRefJsonConverter : JsonConverter<TargetRef>
{
    public override TargetRef? Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        if (reader.TokenType == JsonTokenType.Null) return null;
        if (reader.TokenType != JsonTokenType.StartObject)
            throw new JsonException("target must be a JSON object");

        long hwnd = 0;
        int pid = 0;
        string? startUtc = null;
        string? title = null;
        var sawHwnd = false;

        while (reader.Read())
        {
            if (reader.TokenType == JsonTokenType.EndObject) break;
            if (reader.TokenType != JsonTokenType.PropertyName) continue;
            var name = reader.GetString();
            reader.Read();
            switch (name)
            {
                case "hwnd":
                case "Hwnd":
                    hwnd = ReadHandle(ref reader);
                    sawHwnd = true;
                    break;
                case "pid":
                case "Pid":
                case "process_id":
                    pid = reader.TokenType == JsonTokenType.Number
                        ? reader.GetInt32()
                        : int.Parse(reader.GetString() ?? "0", CultureInfo.InvariantCulture);
                    break;
                case "process_start_utc":
                case "ProcessStartUtc":
                case "process_start_time_utc":
                    startUtc = reader.TokenType == JsonTokenType.Null ? null : reader.GetString();
                    break;
                case "title":
                case "Title":
                    title = reader.TokenType == JsonTokenType.Null ? null : reader.GetString();
                    break;
                default:
                    reader.Skip();
                    break;
            }
        }

        if (!sawHwnd) throw new JsonException("target.hwnd is required");
        return new TargetRef { Hwnd = hwnd, Pid = pid, ProcessStartUtc = startUtc, Title = title };
    }

    private static long ReadHandle(ref Utf8JsonReader reader)
    {
        switch (reader.TokenType)
        {
            case JsonTokenType.Number:
                return reader.GetInt64();
            case JsonTokenType.String:
                var raw = (reader.GetString() ?? "").Trim();
                if (raw.StartsWith("0x", StringComparison.OrdinalIgnoreCase))
                    return long.Parse(raw.AsSpan(2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                return long.Parse(raw, CultureInfo.InvariantCulture);
            default:
                throw new JsonException("target.hwnd must be a string or a number");
        }
    }

    public override void Write(Utf8JsonWriter writer, TargetRef value, JsonSerializerOptions options)
    {
        writer.WriteStartObject();
        writer.WriteString("hwnd", value.HwndHex);
        writer.WriteNumber("hwnd_value", value.Hwnd);
        writer.WriteNumber("pid", value.Pid);
        if (value.ProcessStartUtc is not null) writer.WriteString("process_start_utc", value.ProcessStartUtc);
        if (value.Title is not null) writer.WriteString("title", value.Title);
        writer.WriteEndObject();
    }
}

/// <summary>
/// A capture request as supplied by a caller. Every field is optional so that the resolver can
/// implement "temporary override &gt; saved config &gt; built-in default".
/// </summary>
public sealed record CaptureRequest
{
    public TargetRef? Target { get; init; }
    public int? Count { get; init; }
    public int? IntervalMs { get; init; }
    public int? StartDelayMs { get; init; }

    /// <summary>0 or null keeps the native window size; otherwise the longest edge is scaled down to this.</summary>
    public int? MaxEdge { get; init; }

    /// <summary>When set, each frame is also written to this directory as <c>&lt;job&gt;_&lt;seq&gt;.png</c>.</summary>
    public string? OutputDir { get; init; }

    /// <summary>Only <c>png</c> is supported in v1.</summary>
    public string? Format { get; init; }

    public int? TtlSeconds { get; init; }
    public int? FrameTimeoutMs { get; init; }

    /// <summary>Reuse the previous frame when the window did not recompose (reported as <c>reused</c>).</summary>
    public bool? AllowReuse { get; init; }

    /// <summary>Persist the resolved values (and the target) into the config file.</summary>
    public bool? SaveConfig { get; init; }

    /// <summary>Open the picker UI when no target is known instead of failing outright.</summary>
    public bool? InteractiveIfNeeded { get; init; }
}

/// <summary>A fully resolved, validated capture plan actually handed to the capture engine.</summary>
public sealed record CapturePlan
{
    public required TargetRef Target { get; init; }
    public int Count { get; init; } = 1;
    public int IntervalMs { get; init; }
    public int StartDelayMs { get; init; }
    public int MaxEdge { get; init; }
    public string? OutputDir { get; init; }
    public string Format { get; init; } = "png";
    public int TtlSeconds { get; init; } = 300;
    public int FrameTimeoutMs { get; init; } = 5_000;
    public bool AllowReuse { get; init; } = true;

    [JsonIgnore]
    public string JobTag => Target.HwndHex;
}

/// <summary>Built-in defaults used when neither a temporary override nor a saved value exists.</summary>
public static class CaptureDefaults
{
    public const int Count = 1;
    public const int IntervalMs = 250;
    public const int StartDelayMs = 0;
    public const int MaxEdge = 0;
    public const string Format = "png";
    public const int TtlSeconds = 300;
    public const int FrameTimeoutMs = 5_000;
    public const bool AllowReuse = true;
}

/// <summary>
/// Persisted configuration. All capture fields are nullable: <c>null</c> means "not configured,
/// fall through to the built-in default".
/// </summary>
public sealed record AppConfig
{
    public int? Count { get; init; }
    public int? IntervalMs { get; init; }
    public int? StartDelayMs { get; init; }
    public int? MaxEdge { get; init; }
    public string? OutputDir { get; init; }
    public string? Format { get; init; }
    public int? TtlSeconds { get; init; }
    public int? FrameTimeoutMs { get; init; }
    public bool? AllowReuse { get; init; }

    /// <summary>
    /// Last used target. Persisted for convenience only and never trusted blindly: every job
    /// re-validates HWND + PID + process start time before capturing.
    /// </summary>
    public TargetRef? LastTarget { get; init; }

    public static AppConfig Empty { get; } = new();
}

/// <summary>A top-level window as reported by <c>list_windows</c>.</summary>
public sealed record WindowInfo
{
    public required string Hwnd { get; init; }
    public required long HwndValue { get; init; }
    public int Pid { get; init; }
    public string Title { get; init; } = "";
    public string? Process { get; init; }
    public string? ProcessStartUtc { get; init; }
    public string? ClassName { get; init; }
    public bool Visible { get; init; }
    public bool Minimized { get; init; }
    public bool IsForeground { get; init; }

    /// <summary>True when this window is the one the running instance would capture by default.</summary>
    public bool IsSelected { get; init; }
}

public enum JobState
{
    Queued,
    Running,
    Completed,
    Cancelled,
    Failed,
}

public sealed record FrameMeta
{
    public int Index { get; init; }
    public long CapturedAt { get; init; }
    public string Mime { get; init; } = "image/png";
    public int Width { get; init; }
    public int Height { get; init; }
    public int Bytes { get; init; }
    public string Source { get; init; } = FrameSources.Fresh;
    public string? Path { get; init; }
}

public sealed record JobError
{
    public required string Code { get; init; }
    public required string Message { get; init; }
}

/// <summary>Serialisable snapshot of a capture job.</summary>
public sealed record JobView
{
    public required string JobId { get; init; }
    public required string State { get; init; }
    public required TargetRef Target { get; init; }
    public int Requested { get; init; }
    public int Captured { get; init; }
    public long CreatedAt { get; init; }
    public long? StartedAt { get; init; }
    public long? FinishedAt { get; init; }
    public long ExpiresAt { get; init; }
    public bool ResultReleased { get; init; }
    public bool CancelRequested { get; init; }
    public long ResultBytes { get; init; }
    public IReadOnlyList<FrameMeta> Frames { get; init; } = Array.Empty<FrameMeta>();
    public JobError? Error { get; init; }
    public CapturePlan? Plan { get; init; }
}

/// <summary>One base64 chunk of one frame's bytes.</summary>
public sealed record ResultChunk
{
    public required string JobId { get; init; }
    public int Index { get; init; }
    public int FrameCount { get; init; }
    public long Offset { get; init; }
    public int Length { get; init; }
    public long TotalBytes { get; init; }
    public bool Eof { get; init; }
    public string Mime { get; init; } = "image/png";
    public int Width { get; init; }
    public int Height { get; init; }
    public long CapturedAt { get; init; }
    public string Source { get; init; } = FrameSources.Fresh;

    [JsonPropertyName("data_base64")]
    public required string DataBase64 { get; init; }
}
