using System.Text.Json;
using System.Text.Json.Nodes;

namespace WindowsCapture.Core;

/// <summary>Result of a CLI invocation relayed through the running instance.</summary>
public sealed record CliInvocationResult
{
    public int ExitCode { get; init; }
    public string Stdout { get; init; } = "";
    public string Stderr { get; init; } = "";
}

/// <summary>
/// Everything the protocol layer needs from the process that hosts it. Keeping this an interface is
/// what lets the whole RPC surface be unit tested in <c>WindowsCapture.Core</c> with no Windows APIs.
/// </summary>
public interface IRpcHost
{
    string Version { get; }
    string PipeName { get; }
    int ProcessId { get; }
    string? ConfigPath { get; }
    string? ConfigError { get; }

    Limits Limits { get; }
    JobService Jobs { get; }
    AppConfig Config { get; }

    long NowMs { get; }
    bool UiVisible { get; }
    void ShowUi();

    TargetRef? SelectedTarget { get; }
    void SetSelectedTarget(TargetRef? target);

    IReadOnlyList<WindowInfo> ListWindows();

    /// <summary>Persists the config (best effort; failures are reported via <see cref="ConfigError"/>).</summary>
    void SaveConfig(AppConfig config);

    /// <summary>Runs a CLI command in-process on behalf of a second process.</summary>
    Task<CliInvocationResult> InvokeCliAsync(IReadOnlyList<string> args, CancellationToken cancellationToken);

    /// <summary>
    /// Asks the host to shut down and release every resource. The reply is written first, so the
    /// shutdown is scheduled rather than immediate.
    /// </summary>
    void RequestShutdown();
}

// ---------------------------------------------------------------- params DTOs

public sealed class StartParams
{
    public TargetRef? Target { get; set; }
    public int? Count { get; set; }
    public int? IntervalMs { get; set; }
    public int? StartDelayMs { get; set; }
    public int? MaxEdge { get; set; }
    public string? OutputDir { get; set; }
    public string? Format { get; set; }
    public int? TtlSeconds { get; set; }
    public int? FrameTimeoutMs { get; set; }
    public bool? AllowReuse { get; set; }
    public bool? SaveConfig { get; set; }
    public bool? InteractiveIfNeeded { get; set; }

    public CaptureRequest ToCaptureRequest() => new()
    {
        Target = Target,
        Count = Count,
        IntervalMs = IntervalMs,
        StartDelayMs = StartDelayMs,
        MaxEdge = MaxEdge,
        OutputDir = OutputDir,
        Format = Format,
        TtlSeconds = TtlSeconds,
        FrameTimeoutMs = FrameTimeoutMs,
        AllowReuse = AllowReuse,
        SaveConfig = SaveConfig,
        InteractiveIfNeeded = InteractiveIfNeeded,
    };
}

public sealed class JobIdParams
{
    public string? JobId { get; set; }
}

public sealed class ReadResultParams
{
    public string? JobId { get; set; }
    public int Index { get; set; }
    public long Offset { get; set; }
    public int? Length { get; set; }
}

public sealed class TargetParams
{
    public TargetRef? Target { get; set; }
}

public sealed class InvokeCliParams
{
    public List<string>? Args { get; set; }
    public int? TimeoutMs { get; set; }
}

// ---------------------------------------------------------------- dispatcher

/// <summary>
/// The whole RPC surface. Transport-agnostic: it takes one request line and produces one response
/// line, which makes it directly unit testable and keeps the named-pipe server trivial.
/// </summary>
public sealed class RpcDispatcher
{
    private readonly IRpcHost _host;
    private readonly RequestRegistry _registry;

    public RpcDispatcher(IRpcHost host)
    {
        _host = host;
        _registry = new RequestRegistry(host.Limits.MaxRequestRegistryEntries);
    }

    public RequestRegistry Registry => _registry;

    public static readonly string[] Methods =
    {
        "ping", "get_state", "show_ui", "list_windows", "set_target",
        "start", "list_jobs", "get_job", "cancel", "read_result", "release_result", "invoke_cli",
    };

    public async Task<string> DispatchLineAsync(string line, CancellationToken cancellationToken)
    {
        JsonRpcRequest? request;
        try
        {
            request = JsonSerializer.Deserialize<JsonRpcRequest>(line, Json.Options);
        }
        catch (JsonException ex)
        {
            return JsonRpcResponses.Error(null, JsonRpcErrorCode.ParseError, "parse error", ErrorCodes.ParseError, ex.Message);
        }

        if (request is null || !string.Equals(request.JsonRpc, "2.0", StringComparison.Ordinal) || string.IsNullOrEmpty(request.Method))
        {
            return JsonRpcResponses.Error(request?.Id, JsonRpcErrorCode.InvalidRequest, "invalid request", ErrorCodes.InvalidRequest,
                "expected {\"jsonrpc\":\"2.0\",\"id\":...,\"method\":\"...\"}");
        }

        var fingerprint = request.Method + "|" + Json.Canonicalize(request.Params);

        if (!string.IsNullOrEmpty(request.RequestId))
        {
            var lookup = _registry.Lookup(request.RequestId!, fingerprint);
            if (lookup.Outcome == RegistryOutcome.Conflict)
            {
                return JsonRpcResponses.Error(request.Id, JsonRpcErrorCode.InvalidRequest, "request_id conflict",
                    ErrorCodes.RequestIdConflict,
                    $"request_id '{request.RequestId}' was already used with different parameters");
            }

            if (lookup.Outcome == RegistryOutcome.Replay && lookup.Entry is { } entry)
            {
                return entry.IsError
                    ? BuildErrorFromPayload(request.Id, entry.PayloadJson)
                    : BuildSuccessFromPayload(request.Id, entry.PayloadJson);
            }
        }

        JsonNode? result;
        try
        {
            result = await ExecuteAsync(request, cancellationToken).ConfigureAwait(false);
        }
        catch (CaptureException ex)
        {
            var (code, errorCode, message, detail) = MapError(ex);
            var payload = ErrorPayload(code, message, errorCode, detail);
            Remember(request, fingerprint, isError: true, payload);
            return JsonRpcResponses.Error(request.Id, code, message, errorCode, detail);
        }
        catch (OperationCanceledException)
        {
            const string message = "the request was cancelled";
            var payload = ErrorPayload(JsonRpcErrorCode.ServerError, message, ErrorCodes.Cancelled, null);
            Remember(request, fingerprint, isError: true, payload);
            return JsonRpcResponses.Error(request.Id, JsonRpcErrorCode.ServerError, message, ErrorCodes.Cancelled, null);
        }
        catch (Exception ex)
        {
            var payload = ErrorPayload(JsonRpcErrorCode.InternalError, ex.Message, ErrorCodes.Internal, ex.GetType().Name);
            Remember(request, fingerprint, isError: true, payload);
            return JsonRpcResponses.Error(request.Id, JsonRpcErrorCode.InternalError, ex.Message, ErrorCodes.Internal, ex.GetType().Name);
        }

        Remember(request, fingerprint, isError: false, result);
        return JsonRpcResponses.Success(request.Id, result);
    }

    private void Remember(JsonRpcRequest request, string fingerprint, bool isError, JsonNode? payload)
    {
        if (string.IsNullOrEmpty(request.RequestId)) return;
        _registry.Record(request.RequestId!, fingerprint, isError, payload?.ToJsonString(Json.Options) ?? "null");
    }

    private static JsonNode ErrorPayload(int code, string message, string errorCode, string? detail)
    {
        var data = new JsonObject { ["error_code"] = errorCode };
        if (detail is not null) data["detail"] = detail;
        return new JsonObject { ["code"] = code, ["message"] = message, ["data"] = data };
    }

    private static string BuildSuccessFromPayload(JsonElement? id, string payloadJson)
        => JsonRpcResponses.Success(id, JsonNode.Parse(payloadJson));

    private static string BuildErrorFromPayload(JsonElement? id, string payloadJson)
    {
        var node = JsonNode.Parse(payloadJson)!.AsObject();
        var code = node["code"]!.GetValue<int>();
        var message = node["message"]!.GetValue<string>();
        var data = node["data"]!.AsObject();
        var errorCode = data["error_code"]!.GetValue<string>();
        var detail = data.TryGetPropertyValue("detail", out var d) && d is not null ? d.GetValue<string>() : null;
        return JsonRpcResponses.Error(id, code, message, errorCode, detail);
    }

    private static (int Code, string ErrorCode, string Message, string? Detail) MapError(CaptureException ex) => ex.ErrorCode switch
    {
        ErrorCodes.MethodNotFound => (JsonRpcErrorCode.MethodNotFound, ex.ErrorCode, ex.Message, ex.Detail),
        ErrorCodes.RequestIdConflict => (JsonRpcErrorCode.InvalidRequest, ex.ErrorCode, ex.Message, ex.Detail),
        ErrorCodes.Internal => (JsonRpcErrorCode.InternalError, ex.ErrorCode, ex.Message, ex.Detail),
        ErrorCodes.CaptureFailed or ErrorCodes.NoFrame or ErrorCodes.TargetClosed or ErrorCodes.TargetMinimized
            or ErrorCodes.CaptureBusy or ErrorCodes.Timeout or ErrorCodes.JobNotFound or ErrorCodes.JobNotFinished
            or ErrorCodes.ResultReleased or ErrorCodes.ResultExpired
            => (JsonRpcErrorCode.ServerError, ex.ErrorCode, ex.Message, ex.Detail),
        _ => (JsonRpcErrorCode.InvalidParams, ex.ErrorCode, ex.Message, ex.Detail),
    };

    private async Task<JsonNode?> ExecuteAsync(JsonRpcRequest request, CancellationToken cancellationToken)
    {
        switch (request.Method)
        {
            case "ping":
                return new JsonObject { ["pong"] = true, ["now_ms"] = _host.NowMs };

            case "get_state":
                return BuildState();

            case "show_ui":
                _host.ShowUi();
                return new JsonObject { ["ok"] = true, ["ui_visible"] = _host.UiVisible };

            case "list_windows":
                return new JsonObject
                {
                    ["windows"] = JsonSerializer.SerializeToNode(_host.ListWindows(), Json.Options),
                };

            case "set_target":
            {
                var p = Params<TargetParams>(request);
                _host.SetSelectedTarget(p.Target);
                return new JsonObject
                {
                    ["ok"] = true,
                    ["selected_target"] = Json.ToNode(_host.SelectedTarget),
                };
            }

            case "start":
                return StartJob(Params<StartParams>(request));

            case "list_jobs":
                return new JsonObject
                {
                    ["jobs"] = JsonSerializer.SerializeToNode(_host.Jobs.ListJobs(), Json.Options),
                    ["active_job"] = _host.Jobs.ActiveJobId,
                };

            case "get_job":
                return Json.ToNode(_host.Jobs.GetJob(Params<JobIdParams>(request).JobId ?? ""));

            case "cancel":
                return Json.ToNode(_host.Jobs.Cancel(Params<JobIdParams>(request).JobId ?? ""));

            case "read_result":
            {
                var p = Params<ReadResultParams>(request);
                var chunk = _host.Jobs.ReadResult(
                    p.JobId ?? "",
                    p.Index,
                    p.Offset,
                    p.Length ?? _host.Limits.MaxChunkBytes);
                return Json.ToNode(chunk);
            }

            case "release_result":
            {
                var p = Params<JobIdParams>(request);
                var released = _host.Jobs.ReleaseResult(p.JobId ?? "");
                return new JsonObject { ["ok"] = true, ["released"] = released, ["job_id"] = p.JobId };
            }

            case "invoke_cli":
            {
                var p = Params<InvokeCliParams>(request);
                if (p.Args is null || p.Args.Count == 0)
                    throw CaptureException.InvalidParams("invoke_cli requires a non-empty 'args' array");

                var timeoutMs = p.TimeoutMs ?? _host.Limits.InvokeCliTimeoutMs;
                if (timeoutMs < 1 || timeoutMs > _host.Limits.InvokeCliTimeoutMs)
                    throw CaptureException.InvalidParams($"timeout_ms must be between 1 and {_host.Limits.InvokeCliTimeoutMs}");

                using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                linked.CancelAfter(timeoutMs);
                var result = await _host.InvokeCliAsync(p.Args, linked.Token).ConfigureAwait(false);
                return Json.ToNode(result);
            }

            default:
                throw new CaptureException(
                    ErrorCodes.MethodNotFound,
                    $"unknown method '{request.Method}'",
                    "known methods: " + string.Join(", ", Methods));
        }
    }

    private JsonNode StartJob(StartParams p)
    {
        var request = p.ToCaptureRequest();
        var interactive = p.InteractiveIfNeeded ?? true;

        JobView view;
        try
        {
            view = _host.Jobs.Start(request, _host.Config);
        }
        catch (CaptureException ex) when (ex.ErrorCode == ErrorCodes.TargetRequired && interactive)
        {
            // v1 behaviour: open the picker so the user can choose, but do NOT block on it.
            // The caller gets a structured target_required error and can retry.
            _host.ShowUi();
            throw new CaptureException(
                ErrorCodes.TargetRequired,
                "no capture target is selected; the window picker was opened",
                "select a window in the UI and retry start (this build does not wait for the selection)");
        }

        _host.SetSelectedTarget(view.Target);

        if (p.SaveConfig == true && view.Plan is not null)
            _host.SaveConfig(CapturePlanner.Merge(_host.Config, request, view.Plan));

        return Json.ToNode(view)!;
    }

    private JsonNode BuildState()
    {
        return new JsonObject
        {
            ["ok"] = true,
            ["version"] = _host.Version,
            ["pid"] = _host.ProcessId,
            ["pipe"] = _host.PipeName,
            ["ui_visible"] = _host.UiVisible,
            ["selected_target"] = Json.ToNode(_host.SelectedTarget),
            ["config"] = Json.ToNode(_host.Config),
            ["config_path"] = _host.ConfigPath,
            ["config_error"] = _host.ConfigError,
            ["limits"] = Json.ToNode(_host.Limits),
            ["active_job"] = _host.Jobs.ActiveJobId,
            ["jobs"] = JsonSerializer.SerializeToNode(_host.Jobs.ListJobs(), Json.Options),
            ["now_ms"] = _host.NowMs,
        };
    }

    private static T Params<T>(JsonRpcRequest request) where T : new()
    {
        if (request.Params is null || request.Params.Value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
            return new T();

        if (request.Params.Value.ValueKind != JsonValueKind.Object)
            throw CaptureException.InvalidParams("params must be a JSON object");

        try
        {
            return Json.Deserialize<T>(request.Params.Value) ?? new T();
        }
        catch (JsonException ex)
        {
            throw CaptureException.InvalidParams("params could not be parsed", ex.Message);
        }
    }
}
