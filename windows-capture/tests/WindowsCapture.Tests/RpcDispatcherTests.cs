using System.Text.Json.Nodes;
using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class RpcDispatcherTests
{
    private static (RpcDispatcher Dispatcher, FakeHost Host, FakeCaptureEngine Engine) Build(Limits? limits = null)
    {
        var engine = new FakeCaptureEngine();
        var jobs = TestData.NewService(engine, limits: limits);
        var host = new FakeHost(jobs) { Limits = limits ?? new Limits() };
        return (new RpcDispatcher(host), host, engine);
    }

    [Fact]
    public async Task GamepadLookRejectsAnAxisOutsideTheSafeRange()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher,
            """{"jsonrpc":"2.0","id":1,"method":"gamepad.look","params":{"x":1.1,"y":0,"hold_ms":20}}""");

        Assert.Equal(ErrorCodes.InvalidParams, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task GetStateDescribesTheVirtualGamepad()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"gamepad.get_state"}""");

        Assert.True(node["result"]!["available"]!.GetValue<bool>());
        Assert.False(node["result"]!["connected"]!.GetValue<bool>());
        Assert.Equal(GamepadSafety.MaxHoldMs, node["result"]!["max_hold_ms"]!.GetValue<int>());
    }

    [Fact]
    public async Task PressUsesTheBoundedGamepadSurface()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher,
            """{"jsonrpc":"2.0","id":1,"method":"gamepad.press","params":{"button":"view","hold_ms":80}}""");

        var gamepad = Assert.IsType<FakeGamepadController>(host.Gamepad);
        Assert.True(node["result"]!["connected"]!.GetValue<bool>());
        Assert.Contains("press:View:80", gamepad.Calls);
    }

    [Fact]
    public async Task MoveAndLookStayWithinTheTypedSurface()
    {
        var (dispatcher, host, _) = Build();

        await Send(dispatcher,
            """{"jsonrpc":"2.0","id":1,"method":"gamepad.move","params":{"direction":"forward","hold_ms":600}}""");
        await Send(dispatcher,
            """{"jsonrpc":"2.0","id":2,"method":"gamepad.look","params":{"x":0.5,"y":-0.25,"hold_ms":100}}""");

        var gamepad = Assert.IsType<FakeGamepadController>(host.Gamepad);
        Assert.Contains("move:Forward:600", gamepad.Calls);
        Assert.Contains("look:0.5:-0.25:100", gamepad.Calls);
    }

    [Fact]
    public async Task GamepadInputRejectsUnboundedDuration()
    {
        var (dispatcher, _, _) = Build();
        var missing = await Send(dispatcher,
            """{"jsonrpc":"2.0","id":1,"method":"gamepad.press","params":{"button":"a"}}""");
        var oversized = await Send(dispatcher,
            """{"jsonrpc":"2.0","id":2,"method":"gamepad.move","params":{"direction":"forward","hold_ms":5001}}""");

        Assert.Equal(GamepadErrorCodes.DurationExceeded, missing["error"]!["data"]!["error_code"]!.GetValue<string>());
        Assert.Equal(GamepadErrorCodes.DurationExceeded, oversized["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task ReleaseAllAndDisconnectAreSafeAndIdempotent()
    {
        var (dispatcher, host, _) = Build();
        await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"gamepad.connect"}""");
        var released = await Send(dispatcher, """{"jsonrpc":"2.0","id":2,"method":"gamepad.release_all"}""");
        var disconnected = await Send(dispatcher, """{"jsonrpc":"2.0","id":3,"method":"gamepad.disconnect"}""");

        var gamepad = Assert.IsType<FakeGamepadController>(host.Gamepad);
        Assert.True(released["result"]!["connected"]!.GetValue<bool>());
        Assert.False(disconnected["result"]!["connected"]!.GetValue<bool>());
        Assert.Contains("release_all", gamepad.Calls);
        Assert.Contains("disconnect", gamepad.Calls);
    }

    private static async Task<JsonNode> Send(RpcDispatcher dispatcher, string line)
        => JsonNode.Parse(await dispatcher.DispatchLineAsync(line, CancellationToken.None))!;

    /// <summary>Builds a request line mentioning a job id without interpolated raw-string brace ambiguity.</summary>
    private static string JobLine(int id, string method, string jobId, string extraParams = "")
        => "{\"jsonrpc\":\"2.0\",\"id\":" + id + ",\"method\":\"" + method
           + "\",\"params\":{\"job_id\":\"" + jobId + "\"" + extraParams + "}}";

    private const string StartWithTarget =
        """{"jsonrpc":"2.0","id":1,"method":"start","params":{"target":{"hwnd":"0x1234","pid":100},"count":1}}""";

    [Fact]
    public async Task MalformedJsonIsAParseError()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, "{not json");

        Assert.Equal(-32700, node["error"]!["code"]!.GetValue<int>());
        Assert.Equal(ErrorCodes.ParseError, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task MissingMethodIsAnInvalidRequest()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1}""");

        Assert.Equal(-32600, node["error"]!["code"]!.GetValue<int>());
        Assert.Equal(ErrorCodes.InvalidRequest, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task UnknownMethodIsReported()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"nope"}""");

        Assert.Equal(-32601, node["error"]!["code"]!.GetValue<int>());
        Assert.Equal(ErrorCodes.MethodNotFound, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task PingAnswers()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":"p","method":"ping"}""");

        Assert.True(node["result"]!["pong"]!.GetValue<bool>());
        Assert.Equal("p", node["id"]!.GetValue<string>());
    }

    [Fact]
    public async Task NumericIdsAreEchoedWithTheirOriginalType()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":17,"method":"ping"}""");

        Assert.Equal(17, node["id"]!.GetValue<int>());
    }

    [Fact]
    public async Task StartWithoutTargetOpensThePickerAndReportsTargetRequired()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"start","params":{}}""");

        Assert.Equal(-32602, node["error"]!["code"]!.GetValue<int>());
        Assert.Equal(ErrorCodes.TargetRequired, node["error"]!["data"]!["error_code"]!.GetValue<string>());
        Assert.Equal(1, host.ShowUiCalls);
    }

    [Fact]
    public async Task StartWithoutTargetDoesNotOpenThePickerWhenInteractiveIsDisabled()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"start","params":{"interactive_if_needed":false}}""");

        Assert.Equal(ErrorCodes.TargetRequired, node["error"]!["data"]!["error_code"]!.GetValue<string>());
        Assert.Equal(0, host.ShowUiCalls);
    }

    [Fact]
    public async Task StartReturnsAJobIdAndRemembersTheTarget()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher, StartWithTarget);

        var jobId = node["result"]!["job_id"]!.GetValue<string>();
        Assert.False(string.IsNullOrEmpty(jobId));
        Assert.NotNull(host.SelectedTarget);
        Assert.Equal(0x1234, host.SelectedTarget!.Hwnd);
    }

    [Fact]
    public async Task RepeatedRequestIdWithTheSameParametersReplaysTheSameJob()
    {
        var (dispatcher, host, engine) = Build();
        var line = """{"jsonrpc":"2.0","id":1,"request_id":"retry-1","method":"start","params":{"target":{"hwnd":"0x1234","pid":100},"count":1}}""";

        var first = await Send(dispatcher, line);
        var second = await Send(dispatcher, line);

        Assert.Equal(
            first["result"]!["job_id"]!.GetValue<string>(),
            second["result"]!["job_id"]!.GetValue<string>());

        // Only one job was actually created.
        Assert.Single(host.Jobs.ListJobs());
        Assert.True(engine.Calls >= 1);
    }

    [Fact]
    public async Task ReplayedResponsesUseTheIdOfTheRetry()
    {
        var (dispatcher, _, _) = Build();
        var first = """{"jsonrpc":"2.0","id":1,"request_id":"retry-2","method":"start","params":{"target":{"hwnd":"0x1234","pid":100}}}""";
        var second = """{"jsonrpc":"2.0","id":99,"request_id":"retry-2","method":"start","params":{"target":{"hwnd":"0x1234","pid":100}}}""";

        await Send(dispatcher, first);
        var replayed = await Send(dispatcher, second);

        Assert.Equal(99, replayed["id"]!.GetValue<int>());
        Assert.NotNull(replayed["result"]);
    }

    [Fact]
    public async Task RepeatedRequestIdWithDifferentParametersConflicts()
    {
        var (dispatcher, _, _) = Build();
        var first = """{"jsonrpc":"2.0","id":1,"request_id":"retry-3","method":"start","params":{"target":{"hwnd":"0x1234","pid":100},"count":1}}""";
        var second = """{"jsonrpc":"2.0","id":1,"request_id":"retry-3","method":"start","params":{"target":{"hwnd":"0x1234","pid":100},"count":2}}""";

        await Send(dispatcher, first);
        var conflict = await Send(dispatcher, second);

        Assert.Equal(ErrorCodes.RequestIdConflict, conflict["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task ReplayedErrorsKeepTheirErrorCode()
    {
        var (dispatcher, _, _) = Build();
        var line = """{"jsonrpc":"2.0","id":1,"request_id":"err-1","method":"get_job","params":{"job_id":"missing"}}""";

        var first = await Send(dispatcher, line);
        var second = await Send(dispatcher, line);

        Assert.Equal(ErrorCodes.JobNotFound, first["error"]!["data"]!["error_code"]!.GetValue<string>());
        Assert.Equal(ErrorCodes.JobNotFound, second["error"]!["data"]!["error_code"]!.GetValue<string>());
        Assert.Equal(-32000, second["error"]!["code"]!.GetValue<int>());
    }

    [Fact]
    public async Task GetJobReportsTheLifecycle()
    {
        var (dispatcher, host, _) = Build();
        var started = await Send(dispatcher, StartWithTarget);
        var jobId = started["result"]!["job_id"]!.GetValue<string>();

        TestData.WaitForTerminal(host.Jobs, jobId);

        var node = await Send(dispatcher, JobLine(2, "get_job", jobId));
        Assert.Equal("completed", node["result"]!["state"]!.GetValue<string>());
        Assert.Equal(1, node["result"]!["captured"]!.GetValue<int>());
        Assert.Equal(jobId, node["result"]!["job_id"]!.GetValue<string>());
    }

    [Fact]
    public async Task GetJobOnAnUnknownJobIsAServerError()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"get_job","params":{"job_id":"nope"}}""");

        Assert.Equal(-32000, node["error"]!["code"]!.GetValue<int>());
        Assert.Equal(ErrorCodes.JobNotFound, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task ReadResultReturnsBase64ChunksAndThenReleases()
    {
        var engine = new FakeCaptureEngine
        {
            Factory = (_, _) => new CapturedFrame
            {
                Data = new byte[600],
                Mime = "image/png",
                Width = 3,
                Height = 2,
                CapturedAtUnixMs = 77,
            },
        };

        var limits = new Limits { MaxChunkBytes = 256 };
        var jobs = TestData.NewService(engine, limits: limits);
        var host = new FakeHost(jobs) { Limits = limits };
        var dispatcher = new RpcDispatcher(host);

        var started = await Send(dispatcher, StartWithTarget);
        var jobId = started["result"]!["job_id"]!.GetValue<string>();
        TestData.WaitForTerminal(jobs, jobId);

        var chunk = await Send(dispatcher, JobLine(3, "read_result", jobId, ",\"index\":0,\"offset\":0"));
        var result = chunk["result"]!;

        Assert.Equal(256, result["length"]!.GetValue<int>());
        Assert.False(result["eof"]!.GetValue<bool>());
        Assert.Equal(600, result["total_bytes"]!.GetValue<int>());
        Assert.Equal(256, Convert.FromBase64String(result["data_base64"]!.GetValue<string>()).Length);
        Assert.Equal(77, result["captured_at"]!.GetValue<long>());

        var released = await Send(dispatcher, JobLine(4, "release_result", jobId));
        Assert.True(released["result"]!["released"]!.GetValue<bool>());

        var afterRelease = await Send(dispatcher, JobLine(5, "read_result", jobId, ",\"index\":0"));
        Assert.Equal(ErrorCodes.ResultReleased, afterRelease["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task CancelUnknownJobIsReported()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"cancel","params":{"job_id":"nope"}}""");
        Assert.Equal(ErrorCodes.JobNotFound, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task ListWindowsReturnsTheHostWindows()
    {
        var (dispatcher, host, _) = Build();
        host.AddWindow(new WindowInfo
        {
            Hwnd = "0x1234",
            HwndValue = 0x1234,
            Pid = 100,
            Title = "notepad",
            Process = "notepad",
        });

        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"list_windows"}""");
        var windows = node["result"]!["windows"]!.AsArray();

        Assert.Single(windows);
        Assert.Equal("notepad", windows[0]!["title"]!.GetValue<string>());
        Assert.Equal("0x1234", windows[0]!["hwnd"]!.GetValue<string>());
    }

    [Fact]
    public async Task GetStateDescribesTheHost()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"get_state"}""");
        var result = node["result"]!;

        Assert.Equal("0.0.0-test", result["version"]!.GetValue<string>());
        Assert.Equal(4242, result["pid"]!.GetValue<int>());
        Assert.Equal("test-pipe", result["pipe"]!.GetValue<string>());
        Assert.NotNull(result["limits"]);
        Assert.NotNull(result["jobs"]);
        Assert.NotNull(result["config"]);
    }

    [Fact]
    public async Task ShowUiMarksTheUiVisible()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"show_ui"}""");

        Assert.True(node["result"]!["ui_visible"]!.GetValue<bool>());
        Assert.Equal(1, host.ShowUiCalls);
    }

    [Fact]
    public async Task SetTargetStoresTheSelection()
    {
        var (dispatcher, host, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"set_target","params":{"target":{"hwnd":"0xAA","pid":7}}}""");

        Assert.Equal(0xAA, host.SelectedTarget!.Hwnd);
        Assert.Equal(7, host.SelectedTarget!.Pid);
        Assert.NotNull(node["result"]!["selected_target"]);
    }

    [Fact]
    public async Task InvokeCliRelaysTheResult()
    {
        var (dispatcher, host, _) = Build();
        host.CliHandler = (args, _) => Task.FromResult(new CliInvocationResult
        {
            ExitCode = 0,
            Stdout = "{\"ok\":true,\"echo\":\"" + string.Join(' ', args) + "\"}",
        });

        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"invoke_cli","params":{"args":["--capture","--count","1"]}}""");

        Assert.Equal(0, node["result"]!["exit_code"]!.GetValue<int>());
        Assert.Contains("--capture --count 1", node["result"]!["stdout"]!.GetValue<string>());
    }

    [Fact]
    public async Task InvokeCliWithoutArgsIsRejected()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"invoke_cli","params":{"args":[]}}""");
        Assert.Equal(ErrorCodes.InvalidParams, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task ArrayParamsAreRejected()
    {
        var (dispatcher, _, _) = Build();
        var node = await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"get_job","params":[1,2]}""");
        Assert.Equal(ErrorCodes.InvalidParams, node["error"]!["data"]!["error_code"]!.GetValue<string>());
    }

    [Fact]
    public async Task HwndMayBeSentAsADecimalStringOrNumber()
    {
        var (dispatcher, host, _) = Build();

        await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"set_target","params":{"target":{"hwnd":"4660","pid":1}}}""");
        Assert.Equal(4660, host.SelectedTarget!.Hwnd);

        await Send(dispatcher, """{"jsonrpc":"2.0","id":2,"method":"set_target","params":{"target":{"hwnd":4660,"pid":1}}}""");
        Assert.Equal(4660, host.SelectedTarget!.Hwnd);

        await Send(dispatcher, """{"jsonrpc":"2.0","id":3,"method":"set_target","params":{"target":{"hwnd":"0x1234","pid":1}}}""");
        Assert.Equal(0x1234, host.SelectedTarget!.Hwnd);
    }

    [Fact]
    public async Task SaveConfigPersistsTheSuppliedOverrides()
    {
        var (dispatcher, host, _) = Build();
        await Send(dispatcher, """{"jsonrpc":"2.0","id":1,"method":"start","params":{"target":{"hwnd":"0x1234","pid":100},"count":4,"interval_ms":500,"save_config":true}}""");

        Assert.Equal(4, host.Config.Count);
        Assert.Equal(500, host.Config.IntervalMs);
        Assert.Equal(0x1234, host.Config.LastTarget!.Hwnd);
    }
}
