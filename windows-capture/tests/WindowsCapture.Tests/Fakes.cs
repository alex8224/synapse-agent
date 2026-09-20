using System.Diagnostics;
using System.Text.Json.Nodes;
using WindowsCapture.Core;

namespace WindowsCapture.Tests;

/// <summary>Capture engine double. Never touches Windows, so every test runs anywhere.</summary>
internal sealed class FakeCaptureEngine : ICaptureEngine
{
    private int _calls;

    public int Calls => Volatile.Read(ref _calls);
    public int InvalidateCount { get; private set; }
    public bool Disposed { get; private set; }

    /// <summary>When set, the first capture blocks until this is signalled (for concurrency tests).</summary>
    public ManualResetEventSlim? Gate { get; set; }

    public Func<CapturePlan, int, CapturedFrame>? Factory { get; set; }

    public string Source { get; set; } = FrameSources.Fresh;

    public CapturedFrame Capture(TargetRef target, CapturePlan plan, CancellationToken cancellationToken)
    {
        var index = Interlocked.Increment(ref _calls) - 1;

        Gate?.Wait(cancellationToken);
        cancellationToken.ThrowIfCancellationRequested();

        if (Factory is not null) return Factory(plan, index);

        return new CapturedFrame
        {
            Data = new byte[] { (byte)index, 0x01, 0x02, 0x03 },
            Mime = "image/png",
            Width = 4,
            Height = 1,
            CapturedAtUnixMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            Source = Source,
        };
    }

    public void Invalidate() => InvalidateCount++;

    public void Dispose() => Disposed = true;
}

internal sealed class FakeProcessInfo : IProcessInfo
{
    public Dictionary<int, DateTime> Starts { get; } = new();

    public bool TryGetStartUtc(int pid, out DateTime startUtc) => Starts.TryGetValue(pid, out startUtc);
}

internal sealed class FakeHost : IRpcHost
{
    private readonly List<WindowInfo> _windows = new();

    public FakeHost(JobService jobs)
    {
        Jobs = jobs;
        Config = AppConfig.Empty;
    }

    public string Version { get; set; } = "0.0.0-test";
    public string PipeName { get; set; } = "test-pipe";
    public int ProcessId => 4242;
    public string? ConfigPath { get; set; } = "/tmp/config.json";
    public string? ConfigError { get; set; }
    public Limits Limits { get; set; } = new();
    public JobService Jobs { get; }
    public AppConfig Config { get; set; }
    public long NowMs => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    public bool UiVisible { get; private set; }
    public TargetRef? SelectedTarget { get; private set; }
    public int ShowUiCalls { get; private set; }
    public int ShutdownCalls { get; private set; }
    public Func<IReadOnlyList<string>, CancellationToken, Task<CliInvocationResult>>? CliHandler { get; set; }

    public IReadOnlyList<WindowInfo> Windows => _windows;

    public void AddWindow(WindowInfo window) => _windows.Add(window);

    public void ShowUi()
    {
        ShowUiCalls++;
        UiVisible = true;
    }

    public void RequestShutdown() => ShutdownCalls++;

    public void SetSelectedTarget(TargetRef? target) => SelectedTarget = target;

    public IReadOnlyList<WindowInfo> ListWindows() => _windows;

    public void SaveConfig(AppConfig config) => Config = config;

    public Task<CliInvocationResult> InvokeCliAsync(IReadOnlyList<string> args, CancellationToken cancellationToken)
        => CliHandler is null
            ? Task.FromResult(new CliInvocationResult { ExitCode = 0, Stdout = "{\"ok\":true}" })
            : CliHandler(args, cancellationToken);
}

internal static class TestData
{
    public static TargetRef Target(long hwnd = 0x1234, int pid = 100, string? start = null)
        => new() { Hwnd = hwnd, Pid = pid, ProcessStartUtc = start, Title = "test" };

    public static CapturePlan Plan(long hwnd = 0x1234, int pid = 100)
        => new() { Target = Target(hwnd, pid), Count = 1, FrameTimeoutMs = 1000 };

    public static JobService NewService(ICaptureEngine engine, IClock? clock = null, IProcessInfo? processes = null, Limits? limits = null, Func<string>? ids = null)
        => new(engine, limits ?? new Limits(), clock ?? SystemClock.Instance, processes ?? new FakeProcessInfo(), ids);

    public static JobView WaitForTerminal(JobService jobs, string jobId, int timeoutMs = 10_000)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            var view = jobs.GetJob(jobId);
            if (view.State is "completed" or "cancelled" or "failed") return view;
            Thread.Sleep(10);
        }

        throw new TimeoutException($"job {jobId} did not finish within {timeoutMs} ms");
    }

    public static JsonNode ResultNode(string response) => JsonNode.Parse(response)!["result"]!;
}
