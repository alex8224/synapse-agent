namespace WindowsCapture.App;

/// <summary>Bounded, documented process exit codes so callers (e.g. a Python driver) can branch.</summary>
public static class ExitCodes
{
    public const int Ok = 0;
    public const int Unexpected = 1;
    public const int Usage = 2;

    /// <summary>No running instance could be reached (and autostart was disabled or failed).</summary>
    public const int NoHost = 3;

    /// <summary>The RPC call itself returned a JSON-RPC error object.</summary>
    public const int RpcError = 4;

    public const int CaptureFailed = 5;
    public const int CaptureCancelled = 6;
    public const int CaptureTimeout = 7;
    public const int TargetRequired = 8;
    public const int SelfTestFailed = 9;
}

/// <summary>
/// Parsed command line. Parsing never throws: a problem is reported through <see cref="Error"/>
/// so the caller can print usage and exit with a bounded code.
/// </summary>
public sealed class CliOptions
{
    public string[] RawArgs { get; private init; } = Array.Empty<string>();
    public string? Error { get; private set; }

    public bool ShowHelp { get; private set; }
    public bool ShowVersion { get; private set; }
    public bool ShowUi { get; private set; }
    public bool Hidden { get; private set; }
    public bool Capture { get; private set; }
    public bool SelfTest { get; private set; }
    public bool Exit { get; private set; }
    public bool NoAutostart { get; private set; }
    public bool ListWindows { get; private set; }

    public string? RpcJson { get; private set; }
    public string? RpcFile { get; private set; }
    public string? PipeName { get; private set; }
    public string? OutputDir { get; private set; }
    public string? TargetJson { get; private set; }
    public string? SelfTestOutDir { get; private set; }

    public int? Count { get; private set; }
    public int? IntervalMs { get; private set; }
    public int? StartDelayMs { get; private set; }
    public int? MaxEdge { get; private set; }
    public int? TtlSeconds { get; private set; }
    public int? FrameTimeoutMs { get; private set; }
    public int? WaitTimeoutMs { get; private set; }
    public bool NoReuse { get; private set; }
    public long? Hwnd { get; private set; }
    public int? Pid { get; private set; }

    /// <summary>True when the process should act as the single GUI/RPC host.</summary>
    public bool HostMode => !Capture && !SelfTest && !Exit && RpcJson is null && RpcFile is null && !ListWindows;

    public static CliOptions Parse(string[] args)
    {
        var o = new CliOptions { RawArgs = args };

        try
        {
            for (var i = 0; i < args.Length; i++)
            {
                var arg = args[i];
                string Value(string name)
                {
                    if (i + 1 >= args.Length) throw new CliParseException($"'{name}' requires a value");
                    return args[++i];
                }
                int IntValue(string name)
                {
                    var raw = Value(name);
                    if (!int.TryParse(raw, out var v)) throw new CliParseException($"'{name}' expects an integer, got '{raw}'");
                    return v;
                }

                switch (arg)
                {
                    case "--help" or "-h" or "/?": o.ShowHelp = true; break;
                    case "--version": o.ShowVersion = true; break;
                    case "--show-ui": o.ShowUi = true; break;
                    case "--hidden": o.Hidden = true; break;
                    case "--capture": o.Capture = true; break;
                    case "--self-test": o.SelfTest = true; break;
                    case "--exit": o.Exit = true; break;
                    case "--list-windows": o.ListWindows = true; break;
                    case "--no-autostart": o.NoAutostart = true; break;
                    case "--no-reuse": o.NoReuse = true; break;
                    case "--rpc": o.RpcJson = Value(arg); break;
                    case "--rpc-file": o.RpcFile = Value(arg); break;
                    case "--pipe": o.PipeName = Value(arg); break;
                    case "--output-dir": o.OutputDir = Value(arg); break;
                    case "--target-json": o.TargetJson = Value(arg); break;
                    case "--self-test-out": o.SelfTestOutDir = Value(arg); break;
                    case "--count": o.Count = IntValue(arg); break;
                    case "--interval-ms": o.IntervalMs = IntValue(arg); break;
                    case "--start-delay-ms": o.StartDelayMs = IntValue(arg); break;
                    case "--max-edge": o.MaxEdge = IntValue(arg); break;
                    case "--ttl-seconds": o.TtlSeconds = IntValue(arg); break;
                    case "--frame-timeout-ms": o.FrameTimeoutMs = IntValue(arg); break;
                    case "--wait-timeout-ms": o.WaitTimeoutMs = IntValue(arg); break;
                    case "--pid": o.Pid = IntValue(arg); break;
                    case "--hwnd":
                        var hwndRaw = Value(arg).Trim();
                        o.Hwnd = hwndRaw.StartsWith("0x", StringComparison.OrdinalIgnoreCase)
                            ? Convert.ToInt64(hwndRaw[2..], 16)
                            : long.Parse(hwndRaw);
                        break;
                    default:
                        throw new CliParseException($"unknown argument '{arg}'");
                }
            }
        }
        catch (CliParseException ex)
        {
            o.Error = ex.Message;
        }
        catch (Exception ex)
        {
            o.Error = ex.Message;
        }

        return o;
    }

    public static string HelpText => """
windows-capture - Windows Graphics Capture window screenshotter (GUI + JSON-RPC)

USAGE
  windows-capture [--show-ui | --hidden]        start (or focus) the GUI + RPC host
  windows-capture --capture [options]           capture from the running host
  windows-capture --rpc '<json>'                forward one JSON-RPC line to the host
  windows-capture --list-windows                list capturable top-level windows (JSON)
  windows-capture --exit                        shut the host down and release everything
  windows-capture --self-test [--self-test-out DIR]
                                                create a private window and verify real WGC
  windows-capture --help | --version

CAPTURE OPTIONS (with --capture)
  --count N                 frames to capture                (default 1)
  --interval-ms N           delay between frames            (default from config)
  --start-delay-ms N        delay before the first frame
  --max-edge N              downscale so the longest edge is N px (0 = native)
  --ttl-seconds N           how long the result stays readable
  --frame-timeout-ms N      how long to wait for a NEW frame before reusing the last one
  --no-reuse                fail instead of reporting a reused frame for a static window
  --output-dir DIR          also write each frame as <job>_<seq>.png into DIR
  --hwnd 0x1A2B | --pid N   capture this window instead of the GUI selection
  --target-json '<json>'    full target object, e.g. {"hwnd":"0x1A2B","pid":123,
                            "process_start_utc":"2024-01-01T00:00:00Z"}
  --wait-timeout-ms N       give up waiting for the job after N ms (default 120000)

COMMON
  --pipe NAME               override the named pipe (testing)
  --no-autostart            fail instead of starting a hidden host when none is running

STDOUT
  --rpc / --capture / --list-windows print exactly one JSON document on stdout.
  Diagnostics go to stderr. Exit codes: 0 ok, 1 unexpected, 2 usage, 3 no host,
  4 rpc error, 5 capture failed, 6 cancelled, 7 timeout, 8 target required, 9 self test failed.
""";

    private sealed class CliParseException(string message) : Exception(message);
}
