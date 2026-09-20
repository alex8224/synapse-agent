using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// Runs a command line inside the host process. Used both for the local CLI paths and for
/// <c>invoke_cli</c> when a second process forwards its arguments to the running instance.
/// </summary>
public static class CliRunner
{
    public static async Task<CliInvocationResult> ExecuteAsync(IReadOnlyList<string> args, IRpcHost host, CancellationToken cancellationToken)
    {
        var options = CliOptions.Parse(args.ToArray());

        if (options.Error is not null) return Fail(ExitCodes.Usage, options.Error);
        if (options.ShowHelp) return Text(CliOptions.HelpText);

        if (options.ShowVersion)
            return Text(JsonSerializer.Serialize(new { ok = true, version = host.Version }, Json.Options));

        if (options.ShowUi)
        {
            host.ShowUi();
            return JsonResult(ExitCodes.Ok, new { ok = true, ui_visible = host.UiVisible });
        }

        if (options.Hidden)
            return JsonResult(ExitCodes.Ok, new { ok = true, ui_visible = host.UiVisible });

        if (options.Exit)
        {
            host.RequestShutdown();
            return JsonResult(ExitCodes.Ok, new { ok = true, shutting_down = true });
        }

        if (options.Capture) return await CaptureAsync(options, host, cancellationToken).ConfigureAwait(false);

        return Fail(ExitCodes.Usage, "no command given; run with --help");
    }

    private static async Task<CliInvocationResult> CaptureAsync(CliOptions options, IRpcHost host, CancellationToken cancellationToken)
    {
        TargetRef? target;
        try
        {
            target = ResolveTarget(options, host);
        }
        catch (CaptureException ex)
        {
            return ErrorJson(ex, ExitCodes.Usage);
        }

        var request = new CaptureRequest
        {
            Target = target,
            Count = options.Count,
            IntervalMs = options.IntervalMs,
            StartDelayMs = options.StartDelayMs,
            MaxEdge = options.MaxEdge,
            TtlSeconds = options.TtlSeconds,
            FrameTimeoutMs = options.FrameTimeoutMs,
            AllowReuse = options.NoReuse ? false : null,
            OutputDir = options.OutputDir,
        };

        JobView view;
        try
        {
            view = host.Jobs.Start(request, host.Config);
        }
        catch (CaptureException ex)
        {
            if (ex.ErrorCode == ErrorCodes.TargetRequired)
            {
                // v1: open the picker but do not block on it. The caller gets a structured error.
                host.ShowUi();
                return ErrorJson(ex, ExitCodes.TargetRequired);
            }
            return ErrorJson(ex, ExitCodes.CaptureFailed);
        }

        host.SetSelectedTarget(view.Target);

        var waitMs = options.WaitTimeoutMs ?? 120_000;
        var deadline = Environment.TickCount64 + waitMs;
        var timedOut = false;

        while (view.State is "queued" or "running")
        {
            if (Environment.TickCount64 >= deadline)
            {
                timedOut = true;
                try { view = host.Jobs.Cancel(view.JobId); } catch (CaptureException) { }
                break;
            }

            try
            {
                await Task.Delay(100, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                try { host.Jobs.Cancel(view.JobId); } catch (CaptureException) { }
                return Fail(ExitCodes.CaptureCancelled, "the capture was cancelled while waiting for the job");
            }

            try { view = host.Jobs.GetJob(view.JobId); }
            catch (CaptureException ex) { return ErrorJson(ex, ExitCodes.CaptureFailed); }
        }

        var exitCode = timedOut
            ? ExitCodes.CaptureTimeout
            : view.State switch
            {
                "completed" => ExitCodes.Ok,
                "cancelled" => ExitCodes.CaptureCancelled,
                "failed" => ExitCodes.CaptureFailed,
                _ => ExitCodes.CaptureTimeout,
            };

        var node = Json.ToNode(view)!.AsObject();
        node["ok"] = view.State == "completed";
        if (timedOut) node["timed_out"] = true;

        return new CliInvocationResult
        {
            ExitCode = exitCode,
            Stdout = node.ToJsonString(Json.Options),
        };
    }

    private static TargetRef? ResolveTarget(CliOptions options, IRpcHost host)
    {
        if (options.TargetJson is not null)
        {
            try
            {
                var parsed = JsonSerializer.Deserialize<TargetRef>(options.TargetJson, Json.Options);
                if (parsed is null || parsed.Hwnd == 0)
                    throw CaptureException.InvalidParams("--target-json did not contain a usable hwnd");
                return parsed;
            }
            catch (JsonException ex)
            {
                throw CaptureException.InvalidParams("--target-json is not valid JSON", ex.Message);
            }
        }

        if (options.Hwnd is { } hwnd)
        {
            var info = WindowEnumerator.Describe(new IntPtr(hwnd));
            if (info is null)
                throw new CaptureException(ErrorCodes.TargetNotFound, $"no window exists with hwnd 0x{hwnd:X}");
            return new TargetRef { Hwnd = hwnd, Pid = info.Pid, ProcessStartUtc = info.ProcessStartUtc, Title = info.Title };
        }

        if (options.Pid is { } pid)
        {
            var match = WindowEnumerator.List().FirstOrDefault(w => w.Pid == pid);
            if (match is null)
                throw new CaptureException(ErrorCodes.TargetNotFound, $"no capturable top-level window belongs to pid {pid}");
            return new TargetRef
            {
                Hwnd = match.HwndValue,
                Pid = match.Pid,
                ProcessStartUtc = match.ProcessStartUtc,
                Title = match.Title,
            };
        }

        // No explicit target: fall back to whatever the GUI/config selected.
        return host.SelectedTarget;
    }

    private static CliInvocationResult Text(string text) => new() { ExitCode = ExitCodes.Ok, Stdout = text };

    private static CliInvocationResult JsonResult(int exitCode, object payload) => new()
    {
        ExitCode = exitCode,
        Stdout = JsonSerializer.Serialize(payload, Json.Options),
    };

    private static CliInvocationResult Fail(int exitCode, string message) => new()
    {
        ExitCode = exitCode,
        Stderr = message,
    };

    private static CliInvocationResult ErrorJson(CaptureException ex, int exitCode)
    {
        var node = new JsonObject
        {
            ["ok"] = false,
            ["error"] = new JsonObject
            {
                ["error_code"] = ex.ErrorCode,
                ["message"] = ex.Message,
                ["detail"] = ex.Detail,
            },
        };
        return new CliInvocationResult { ExitCode = exitCode, Stdout = node.ToJsonString(Json.Options) };
    }
}
