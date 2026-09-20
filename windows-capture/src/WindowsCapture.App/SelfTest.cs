using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// Creates a throw-away window owned by this process and verifies the real Windows Graphics
/// Capture path against it. Nothing belonging to the user is ever captured: the test window is
/// created, painted, captured, and destroyed by this process alone.
/// </summary>
public static class SelfTest
{
    // COLORREF is 0x00BBGGRR (the LOW byte is red), while the decoded buffer is BGRA and
    // ImageCodec packs it as b | g<<8 | r<<16 | a<<24.
    //   COLORREF 0x00804020 -> R=0x20 G=0x40 B=0x80 -> BGRA buffer -> 0xFF204080
    private const uint TargetColorRef = 0x00804020;
    private const uint TargetBgra = 0xFF204080;

    //   COLORREF 0x0020A060 -> R=0x60 G=0xA0 B=0x20 -> BGRA buffer -> 0xFF60A020
    private const uint OccluderColorRef = 0x0020A060;
    private const uint OccluderBgra = 0xFF60A020;

    //   COLORREF 0x0000C0FF -> R=0xFF G=0xC0 B=0x00 -> BGRA buffer -> 0xFFFFC000
    private const uint SecondColorRef = 0x0000C0FF;
    private const uint SecondBgra = 0xFFFFC000;

    public static int Run(CliOptions options)
    {
        var report = new Dictionary<string, object?>();
        var failures = new List<string>();

        // The pipe checks are independent of WGC (they only exercise pipe instances this process
        // creates) so they run first and are reported even if the capture part cannot run.
        RunPipeSaturationTest(report, failures);

        try
        {
            using var windows = new TestWindowHost();

            var target = windows.CreateWindow("WgcSelfTestTarget", "windows-capture self-test target",
                x: 120, y: 120, width: 700, height: 500, popup: false, colorRef: TargetColorRef);
            var occluder = windows.CreateWindow("WgcSelfTestOccluder", "windows-capture self-test occluder",
                x: 110, y: 110, width: 720, height: 520, popup: true, colorRef: OccluderColorRef);
            windows.Show(occluder, 105, 105, 740, 540);
            Thread.Sleep(800);

            var targetRef = new TargetRef
            {
                Hwnd = target.ToInt64(),
                Pid = Environment.ProcessId,
                ProcessStartUtc = Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("o"),
                Title = "windows-capture self-test target",
            };
            report["target"] = targetRef;

            using var engine = new WgcCaptureEngine();
            var plan = new CapturePlan
            {
                Target = targetRef,
                Count = 1,
                FrameTimeoutMs = 10_000,
                AllowReuse = true,
            };

            // --- 1. the target is fully covered by the occluder; WGC must still return ITS pixels
            var first = engine.Capture(targetRef, plan, CancellationToken.None);
            var (pixels, width, height) = ImageCodec.DecodePng(first.Data);
            var match = ImageCodec.MatchRatioInCentre(pixels, width, height, TargetBgra);
            var occluderMatch = ImageCodec.MatchRatioInCentre(pixels, width, height, OccluderBgra);

            report["occluded_capture"] = new
            {
                ok = match > 0.9,
                target_match_ratio = Math.Round(match, 4),
                occluder_match_ratio = Math.Round(occluderMatch, 4),
                width,
                height,
                png_bytes = first.Data.Length,
                source = first.Source,
            };

            if (match <= 0.9) failures.Add($"occluded target pixels not captured (match={match:F3})");
            if (occluderMatch > 0.1) failures.Add($"capture leaked the occluding window (match={occluderMatch:F3})");
            WriteSample(options.SelfTestOutDir, "01-occluded-target.png", first.Data);

            // --- 2. a static window: document what WGC actually does across ticks
            var staticTicks = new List<object>();
            for (var i = 0; i < 3; i++)
            {
                var frame = engine.Capture(targetRef, plan with { FrameTimeoutMs = 700 }, CancellationToken.None);
                staticTicks.Add(new { tick = i, source = frame.Source, captured_at = frame.CapturedAtUnixMs });
                Thread.Sleep(200);
            }
            report["static_window_ticks"] = staticTicks;

            // --- 3. repaint the target: a genuinely new frame with new pixels must arrive
            windows.SetColor(target, SecondColorRef);
            windows.Invalidate(target);
            var second = engine.Capture(targetRef, plan with { FrameTimeoutMs = 6_000 }, CancellationToken.None);
            var (pixels2, width2, height2) = ImageCodec.DecodePng(second.Data);
            var secondMatch = ImageCodec.MatchRatioInCentre(pixels2, width2, height2, SecondBgra);

            report["repaint_capture"] = new
            {
                ok = secondMatch > 0.9,
                second_match_ratio = Math.Round(secondMatch, 4),
                source = second.Source,
                captured_at = second.CapturedAtUnixMs,
            };

            if (secondMatch <= 0.9) failures.Add($"repainted window did not produce fresh pixels (match={secondMatch:F3})");
            WriteSample(options.SelfTestOutDir, "02-repainted-target.png", second.Data);

            // --- 4. a minimized window must fail clearly, never silently return a stale frame
            windows.Minimize(target);
            Thread.Sleep(400);
            try
            {
                var stale = engine.Capture(targetRef, plan with { FrameTimeoutMs = 1_500 }, CancellationToken.None);
                report["minimized"] = new { ok = false, unexpected_source = stale.Source };
                failures.Add("capturing a minimized window did not fail");
            }
            catch (CaptureException ex)
            {
                report["minimized"] = new { ok = true, error_code = ex.ErrorCode, message = ex.Message };
            }

            windows.Restore(target);
            Thread.Sleep(300);

            // --- 5. a destroyed window must fail clearly too
            windows.DestroyWindow(target);
            Thread.Sleep(400);
            try
            {
                engine.Capture(targetRef, plan with { FrameTimeoutMs = 1_500 }, CancellationToken.None);
                report["closed"] = new { ok = false };
                failures.Add("capturing a closed window did not fail");
            }
            catch (CaptureException ex)
            {
                report["closed"] = new { ok = true, error_code = ex.ErrorCode, message = ex.Message };
            }
        }
        catch (Exception ex)
        {
            report["fatal"] = ex.ToString();
            failures.Add("self test threw: " + ex.Message);
        }

        report["ok"] = failures.Count == 0;
        report["failures"] = failures;

        Console.Out.WriteLine(JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));
        Console.Out.Flush();
        return failures.Count == 0 ? ExitCodes.Ok : ExitCodes.SelfTestFailed;
    }

    private static void WriteSample(string? directory, string name, byte[] data)
    {
        if (string.IsNullOrWhiteSpace(directory)) return;
        try
        {
            Directory.CreateDirectory(directory);
            File.WriteAllBytes(Path.Combine(directory, name), data);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not write sample '{name}': {ex.Message}");
        }
    }

    // ------------------------------------------------------------- named-pipe self test

    /// <summary>
    /// Exercises the real named-pipe server against itself. It saturates every pipe instance with
    /// idle clients that never send anything, checks that a further client cannot be served, then
    /// releases the clients and checks that the server recovers and answers a fresh ping. It also
    /// checks that an idle client is dropped once its idle deadline passes. Only pipe instances this
    /// test creates are used; no user window (or user pipe) is ever touched.
    /// </summary>
    private static void RunPipeSaturationTest(Dictionary<string, object?> report, List<string> failures)
    {
        try
        {
            var saturation = RunPipeSaturationCase(maxInstances: 3, idleTimeoutMs: 60_000, expectIdleDrop: false);
            report["pipe_saturation"] = saturation;
            if (saturation["ok"] is not true)
                failures.Add("the named-pipe server did not survive instance saturation and recover");
        }
        catch (Exception ex)
        {
            report["pipe_saturation"] = new Dictionary<string, object?> { ["ok"] = false, ["fatal"] = ex.ToString() };
            failures.Add("pipe saturation self test threw: " + ex.Message);
        }

        try
        {
            var idle = RunPipeSaturationCase(maxInstances: 2, idleTimeoutMs: 2_000, expectIdleDrop: true);
            report["pipe_idle_timeout"] = idle;
            if (idle["ok"] is not true)
                failures.Add("an idle named-pipe client was not dropped after the idle deadline");
        }
        catch (Exception ex)
        {
            report["pipe_idle_timeout"] = new Dictionary<string, object?> { ["ok"] = false, ["fatal"] = ex.ToString() };
            failures.Add("pipe idle-timeout self test threw: " + ex.Message);
        }
    }

    private static Dictionary<string, object?> RunPipeSaturationCase(int maxInstances, int idleTimeoutMs, bool expectIdleDrop)
    {
        var pipeName = "windows-capture-selftest-" + Guid.NewGuid().ToString("N");
        var limits = new Limits
        {
            MaxPipeInstances = maxInstances,
            PipeIdleTimeoutMs = idleTimeoutMs,
            PipeWriteTimeoutMs = 5_000,
        };

        var host = new PipeTestHost(pipeName, limits);
        var server = new RpcServer(pipeName, new RpcDispatcher(host), limits);
        try
        {
            server.Start();
            if (!WaitForPing(pipeName, 5_000))
                return new Dictionary<string, object?> { ["ok"] = false, ["error"] = "the server never answered a ping" };

            // Hold every instance with a client that connects and then sends nothing.
            var idles = new List<NamedPipeClientStream>();
            for (var i = 0; i < maxInstances; i++)
            {
                var client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.None);
                client.Connect(5_000);
                idles.Add(client);
            }

            // Wait until every instance is held, then let the accept loop park on its full slot set.
            var allHeld = WaitUntil(() => server.ActiveConnections >= maxInstances, 3_000);
            Thread.Sleep(100);

            var saturated = !TryPing(pipeName, 700);

            bool recovered;
            if (expectIdleDrop)
            {
                // The server should drop the silent clients by itself once the idle deadline passes.
                recovered = WaitForPing(pipeName, 6_000);
            }
            else
            {
                foreach (var client in idles)
                {
                    try { client.Dispose(); } catch (Exception) { }
                }

                recovered = WaitForPing(pipeName, 5_000);
            }

            foreach (var client in idles)
            {
                try { client.Dispose(); } catch (Exception) { }
            }

            return new Dictionary<string, object?>
            {
                ["ok"] = allHeld && saturated && recovered,
                ["max_instances"] = maxInstances,
                ["idle_timeout_ms"] = idleTimeoutMs,
                ["instances_held"] = allHeld,
                ["saturated"] = saturated,
                ["recovered"] = recovered,
                ["active_connections"] = server.ActiveConnections,
            };
        }
        finally
        {
            server.Dispose();
            host.Dispose();
        }
    }

    private static bool TryPing(string pipeName, int timeoutMs)
    {
        try
        {
            using var client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.None);
            client.Connect(timeoutMs);

            var request = Encoding.UTF8.GetBytes("{\"jsonrpc\":\"2.0\",\"id\":\"ping\",\"method\":\"ping\"}\n");
            client.Write(request, 0, request.Length);
            client.Flush();

            using var timeout = new CancellationTokenSource(timeoutMs);
            var line = new LineReader(client, 64 * 1024).ReadLineAsync(timeout.Token).GetAwaiter().GetResult();
            return line is not null && line.Contains("\"pong\"", StringComparison.Ordinal);
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool WaitForPing(string pipeName, int timeoutMs)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (Environment.TickCount64 < deadline)
        {
            if (TryPing(pipeName, 800)) return true;
            Thread.Sleep(100);
        }

        return false;
    }

    private static bool WaitUntil(Func<bool> condition, int timeoutMs)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        while (Environment.TickCount64 < deadline)
        {
            if (condition()) return true;
            Thread.Sleep(25);
        }

        return false;
    }

    /// <summary>
    /// Minimal <see cref="IRpcHost"/> for the pipe self test. It never captures anything and never
    /// touches a user window: the RPC surface only has to answer <c>ping</c>.
    /// </summary>
    private sealed class PipeTestHost : IRpcHost, IDisposable
    {
        public PipeTestHost(string pipeName, Limits limits)
        {
            PipeName = pipeName;
            Limits = limits;
            Jobs = new JobService(new NoopCaptureEngine(), limits, SystemClock.Instance, new NoopProcessInfo());
        }

        public string Version => "self-test";
        public string PipeName { get; }
        public int ProcessId => Environment.ProcessId;
        public string? ConfigPath => null;
        public string? ConfigError => null;
        public Limits Limits { get; }
        public JobService Jobs { get; }
        public IGamepadController Gamepad { get; } = new UnavailableGamepadController();
        public AppConfig Config => AppConfig.Empty;
        public long NowMs => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        public bool UiVisible => false;
        public void ShowUi() { }
        public TargetRef? SelectedTarget => null;
        public void SetSelectedTarget(TargetRef? target) { }
        public IReadOnlyList<WindowInfo> ListWindows() => Array.Empty<WindowInfo>();
        public void SaveConfig(AppConfig config) { }
        public Task<CliInvocationResult> InvokeCliAsync(IReadOnlyList<string> args, CancellationToken cancellationToken)
            => Task.FromResult(new CliInvocationResult { ExitCode = 0, Stdout = "{}" });
        public void RequestShutdown() { }

        public void Dispose()
        {
            Gamepad.Dispose();
            Jobs.Dispose();
        }
    }

    private sealed class NoopCaptureEngine : ICaptureEngine
    {
        public CapturedFrame Capture(TargetRef target, CapturePlan plan, CancellationToken cancellationToken)
            => throw new CaptureException(ErrorCodes.CaptureFailed, "the pipe self test never captures");

        public void Invalidate() { }
        public void Dispose() { }
    }

    private sealed class NoopProcessInfo : IProcessInfo
    {
        public bool TryGetStartUtc(int pid, out DateTime startUtc)
        {
            startUtc = default;
            return false;
        }
    }

    // ------------------------------------------------------------------ test windows

    /// <summary>
    /// Owns a message-pump thread and the throw-away windows created on it. Everything is destroyed
    /// in <see cref="Dispose"/>, so the test never leaves a window or a process behind.
    /// </summary>
    private sealed class TestWindowHost : IDisposable
    {
        private const int SW_SHOW = 5;
        private const int SW_MINIMIZE = 6;
        private const int SW_RESTORE = 9;
        private const uint SWP_NOACTIVATE = 0x0010;
        private const uint SWP_SHOWWINDOW = 0x0040;
        private const uint PM_REMOVE = 0x0001;
        private const uint WM_PAINT = 0x000F;
        private const uint WM_ERASEBKGND = 0x0014;
        private const int WS_OVERLAPPEDWINDOW = 0x00CF0000;
        private const int WS_POPUP = unchecked((int)0x80000000);

        private readonly ConcurrentQueue<Action> _queue = new();
        private readonly ManualResetEventSlim _ready = new(false);
        private readonly Thread _thread;
        private readonly List<IntPtr> _created = new();
        private volatile bool _stopped;

        public TestWindowHost()
        {
            _thread = new Thread(Pump) { IsBackground = true, Name = "windows-capture-self-test" };
            _thread.SetApartmentState(ApartmentState.STA);
            _thread.Start();
            if (!_ready.Wait(TimeSpan.FromSeconds(10)))
                throw new InvalidOperationException("the self-test message pump did not start");
        }

        private void Pump()
        {
            try
            {
                RegisterClasses();
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[pump] RegisterClasses failed: " + ex);
            }
            _ready.Set();

            try
            {
                var message = default(MSG);
                while (!_stopped)
                {
                    while (PeekMessageW(out message, IntPtr.Zero, 0, 0, PM_REMOVE))
                    {
                        TranslateMessage(ref message);
                        DispatchMessageW(ref message);
                    }

                    if (_queue.TryDequeue(out var action))
                    {
                        try { action(); }
                        catch (Exception ex) { Console.Error.WriteLine("self-test window action failed: " + ex); }
                        continue;
                    }

                    Thread.Sleep(2);
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[pump] died: " + ex);
            }

            foreach (var hwnd in _created)
            {
                try { NativeDestroyWindow(hwnd); } catch (Exception) { }
            }
        }

        private static readonly WndProc WndProcDelegate = WndProcImpl;
        private static readonly ConcurrentDictionary<IntPtr, uint> SharedColors = new();

        private void RegisterClasses()
        {
            var instance = GetModuleHandleW(null);
            foreach (var name in new[] { "WgcSelfTestTarget", "WgcSelfTestOccluder" })
            {
                var wc = new WNDCLASSEXW
                {
                    cbSize = Marshal.SizeOf<WNDCLASSEXW>(),
                    lpfnWndProc = Marshal.GetFunctionPointerForDelegate(WndProcDelegate),
                    hInstance = instance,
                    lpszClassName = name,
                };
                RegisterClassExW(ref wc);
            }
        }

        private static IntPtr WndProcImpl(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam)
        {
            try
            {
                switch (msg)
                {
                    case WM_ERASEBKGND:
                        return new IntPtr(1);
                    case WM_PAINT:
                    {
                        var color = SharedColors.TryGetValue(hwnd, out var c) ? c : 0u;
                        var paint = default(PAINTSTRUCT);
                        var hdc = BeginPaint(hwnd, ref paint);
                        if (hdc != IntPtr.Zero)
                        {
                            // FillRect clips to the DC, so one large rect covers the whole window.
                            var rect = new RECT { Left = -16384, Top = -16384, Right = 16384, Bottom = 16384 };
                            var brush = CreateSolidBrush(color);
                            try { FillRect(hdc, ref rect, brush); }
                            finally { DeleteObject(brush); }
                            EndPaint(hwnd, ref paint);
                        }
                        return IntPtr.Zero;
                    }
                }

                return DefWindowProcW(hwnd, msg, wParam, lParam);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("self-test WndProc failed: " + ex.Message);
                return IntPtr.Zero;
            }
        }

        public IntPtr CreateWindow(string className, string title, int x, int y, int width, int height, bool popup, uint colorRef)
        {
            return Invoke(() =>
            {
                var style = popup ? WS_POPUP : WS_OVERLAPPEDWINDOW;
                var hwnd = CreateWindowExW(0, className, title, style, x, y, width, height,
                    IntPtr.Zero, IntPtr.Zero, GetModuleHandleW(null), IntPtr.Zero);
                if (hwnd == IntPtr.Zero)
                    throw new InvalidOperationException($"CreateWindowEx failed for {className} (error {Marshal.GetLastWin32Error()})");

                SharedColors[hwnd] = colorRef;
                _created.Add(hwnd);
                ShowWindow(hwnd, SW_SHOW);
                UpdateWindow(hwnd);
                return hwnd;
            });
        }

        public void Show(IntPtr hwnd, int x, int y, int width, int height)
            => Invoke(() => SetWindowPos(hwnd, IntPtr.Zero, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW));

        public void SetColor(IntPtr hwnd, uint colorRef)
        {
            SharedColors[hwnd] = colorRef;
        }

        public void Invalidate(IntPtr hwnd) => Invoke(() => InvalidateRect(hwnd, IntPtr.Zero, true));

        public void Minimize(IntPtr hwnd) => Invoke(() => ShowWindow(hwnd, SW_MINIMIZE));

        public void Restore(IntPtr hwnd) => Invoke(() => ShowWindow(hwnd, SW_RESTORE));

        public void DestroyWindow(IntPtr hwnd) => Invoke(() =>
        {
            NativeDestroyWindow(hwnd);
            SharedColors.TryRemove(hwnd, out _);
            _created.Remove(hwnd);
        });

        private T Invoke<T>(Func<T> action)
        {
            T result = default!;
            Exception? error = null;
            using var done = new ManualResetEventSlim(false);
            _queue.Enqueue(() =>
            {
                try { result = action(); }
                catch (Exception ex) { error = ex; }
                finally { done.Set(); }
            });
            if (!done.Wait(TimeSpan.FromSeconds(15)))
                throw new TimeoutException("the self-test window thread did not respond");
            if (error is not null) throw error;
            return result;
        }

        private void Invoke(Action action) => Invoke(() => { action(); return 0; });

        public void Dispose()
        {
            _stopped = true;
            _thread.Join(TimeSpan.FromSeconds(5));
            _ready.Dispose();
        }

        // ------------------------------------------------------------ P/Invoke

        private delegate IntPtr WndProc(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct WNDCLASSEXW
        {
            public int cbSize;
            public int style;
            public IntPtr lpfnWndProc;
            public int cbClsExtra;
            public int cbWndExtra;
            public IntPtr hInstance;
            public IntPtr hIcon;
            public IntPtr hCursor;
            public IntPtr hbrBackground;
            public string? lpszMenuName;
            public string lpszClassName;
            public IntPtr hIconSm;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT
        {
            public int Left, Top, Right, Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MSG
        {
            public IntPtr hwnd;
            public uint message;
            public IntPtr wParam;
            public IntPtr lParam;
            public uint time;
            public int ptX;
            public int ptY;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PAINTSTRUCT
        {
            public IntPtr hdc;
            public int fErase;
            public RECT rcPaint;
            public int fRestore;
            public int fIncUpdate;
            public long reserved0;
            public long reserved1;
            public long reserved2;
            public long reserved3;
        }

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern ushort RegisterClassExW(ref WNDCLASSEXW lpwcx);

        [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateWindowExW(int exStyle, string className, string windowName, int style,
            int x, int y, int width, int height, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);

        [DllImport("user32.dll", EntryPoint = "DestroyWindow")]
        private static extern bool NativeDestroyWindow(IntPtr hwnd);

        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hwnd, int cmd);

        [DllImport("user32.dll")]
        private static extern bool UpdateWindow(IntPtr hwnd);

        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);

        [DllImport("user32.dll")]
        private static extern bool InvalidateRect(IntPtr hwnd, IntPtr rect, bool erase);

        [DllImport("user32.dll")]
        private static extern bool PeekMessageW(out MSG message, IntPtr hwnd, uint filterMin, uint filterMax, uint remove);

        [DllImport("user32.dll")]
        private static extern bool TranslateMessage(ref MSG message);

        [DllImport("user32.dll")]
        private static extern IntPtr DispatchMessageW(ref MSG message);

        [DllImport("user32.dll")]
        private static extern IntPtr DefWindowProcW(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern IntPtr BeginPaint(IntPtr hwnd, ref PAINTSTRUCT paint);

        [DllImport("user32.dll")]
        private static extern bool EndPaint(IntPtr hwnd, ref PAINTSTRUCT paint);

        [DllImport("user32.dll")]
        private static extern int FillRect(IntPtr hdc, ref RECT rect, IntPtr brush);

        [DllImport("gdi32.dll")]
        private static extern IntPtr CreateSolidBrush(uint color);

        [DllImport("gdi32.dll")]
        private static extern bool DeleteObject(IntPtr obj);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr GetModuleHandleW(string? name);
    }
}
