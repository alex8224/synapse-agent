using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// A deliberately small command line boundary for AI-issued gamepad calls. It only emits the
/// white-listed RPC methods and every physical input requires a finite <c>--hold-ms</c> value.
/// </summary>
public static class GameControlCli
{
    private const string State = "status";
    private const string Connect = "connect";
    private const string Disconnect = "disconnect";
    private const string Release = "release-all";
    private const string Press = "press";
    private const string Move = "move";
    private const string Look = "look";
    private const string Trigger = "trigger";
    private const string Focus = "focus";
    private const string Screenshot = "screenshot";

    public static int Run(string[] args)
    {
        if (args.Length == 0 || args[0] is "--help" or "-h")
        {
            Console.Out.Write(GameControlHelp.Text);
            return GameControlExitCodes.Ok;
        }

        var command = args[0].ToLowerInvariant();
        try
        {
            if (command == Screenshot)
            {
                return RunTool(ParseScreenshotArgs(args.Skip(1).ToArray()));
            }

            if (command == Focus)
            {
                return FocusTarget(args.Skip(1).ToArray());
            }

            var parameters = Parse(command, args.Skip(1).ToArray());
            var method = command switch
            {
                State => "gamepad.get_state",
                Connect => "gamepad.connect",
                Disconnect => "gamepad.disconnect",
                Release => "gamepad.release_all",
                Press => "gamepad.press",
                Move => "gamepad.move",
                Look => "gamepad.look",
                Trigger => "gamepad.trigger",
                _ => throw new ArgumentException($"unknown command '{args[0]}'"),
            };
            return RunRpc(method, parameters);
        }
        catch (ArgumentException ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine("run 'game-control --help' for usage");
            return GameControlExitCodes.Usage;
        }
        catch (CaptureException ex)
        {
            Console.Error.WriteLine($"{ex.ErrorCode}: {ex.Message}");
            Console.Error.WriteLine("run 'game-control --help' for usage");
            return GameControlExitCodes.Usage;
        }
    }

    private static int ParseTriggerValue(string value)
    {
        if (!int.TryParse(value, out var parsed))
            throw new ArgumentException("'--value' must be an integer between 0 and 255");
        GamepadSafety.ValidateTriggerValue(parsed);
        return parsed;
    }

    private static JsonObject Parse(string command, string[] args)
    {
        var values = ParseOptions(args);
        return command switch
        {
            State or Connect or Disconnect or Release when values.Count == 0 => new JsonObject(),
            Press => new JsonObject
            {
                ["button"] = ParseEnum<GamepadButton>(Required(values, "button"), "button").ToString(),
                ["hold_ms"] = ParseHold(values),
            },
            Move => new JsonObject
            {
                ["direction"] = ParseEnum<GamepadDirection>(Required(values, "direction"), "direction").ToString(),
                ["hold_ms"] = ParseHold(values),
            },
            Look => new JsonObject
            {
                ["x"] = ParseAxis(Required(values, "x"), "x"),
                ["y"] = ParseAxis(Required(values, "y"), "y"),
                ["hold_ms"] = ParseHold(values),
            },
            Trigger => new JsonObject
            {
                ["side"] = ParseEnum<GamepadTrigger>(Required(values, "side"), "side").ToString(),
                ["value"] = ParseTriggerValue(Required(values, "value")),
                ["hold_ms"] = ParseHold(values),
            },
            State or Connect or Disconnect or Release => throw new ArgumentException($"'{command}' does not accept options"),
            _ => throw new ArgumentException($"unknown command '{command}'"),
        };
    }

    private static IReadOnlyList<string> ParseScreenshotArgs(string[] args)
    {
        var values = ParseOptions(args);
        RejectUnknown(values, "count", "interval-ms", "start-delay-ms", "max-edge", "no-reuse", "output-dir");
        var captureArgs = new List<string> { "--capture" };
        AddCaptureOption(captureArgs, values, "count");
        AddCaptureOption(captureArgs, values, "interval-ms");
        AddCaptureOption(captureArgs, values, "start-delay-ms");
        AddCaptureOption(captureArgs, values, "max-edge");
        AddCaptureOption(captureArgs, values, "output-dir");
        if (values.ContainsKey("no-reuse")) captureArgs.Add("--no-reuse");
        return captureArgs;
    }

    private static void AddCaptureOption(ICollection<string> args, IReadOnlyDictionary<string, string> values, string name)
    {
        if (!values.TryGetValue(name, out var value)) return;
        if (name == "count") ParsePositiveInt(value, name);
        else if (name != "output-dir") ParseNonNegativeInt(value, name);
        else if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("'--output-dir' must not be empty");
        args.Add("--" + name);
        args.Add(value);
    }

    private static Dictionary<string, string> ParseOptions(string[] args)
    {
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < args.Length; index++)
        {
            var argument = args[index];
            if (!argument.StartsWith("--", StringComparison.Ordinal))
                throw new ArgumentException($"expected an option, got '{argument}'");
            var name = argument[2..];
            if (name == "no-reuse")
            {
                if (!values.TryAdd(name, "true")) throw new ArgumentException($"option '--{name}' was supplied more than once");
                continue;
            }
            if (index + 1 >= args.Length) throw new ArgumentException($"'--{name}' requires a value");
            if (!values.TryAdd(name, args[++index])) throw new ArgumentException($"option '--{name}' was supplied more than once");
        }
        return values;
    }

    private static int ParseHold(IReadOnlyDictionary<string, string> values)
    {
        RejectUnknown(values, "hold-ms", "button", "direction", "x", "y", "side", "value");
        return GamepadSafety.ValidateHoldMs(ParsePositiveInt(Required(values, "hold-ms"), "hold-ms"));
    }

    private static string Required(IReadOnlyDictionary<string, string> values, string name)
        => values.TryGetValue(name, out var value) && !string.IsNullOrWhiteSpace(value)
            ? value
            : throw new ArgumentException($"'--{name}' is required");

    private static T ParseEnum<T>(string value, string name) where T : struct, Enum
        => Enum.TryParse<T>(value.Replace("-", "", StringComparison.Ordinal), ignoreCase: true, out var result)
            ? result
            : throw new ArgumentException($"'--{name}' has an unsupported value '{value}'");

    private static double ParseAxis(string value, string name)
    {
        if (!double.TryParse(value, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var parsed))
            throw new ArgumentException($"'--{name}' must be a number between -1 and 1");
        GamepadSafety.NormalizeAxis(parsed, name);
        return parsed;
    }

    private static int ParsePositiveInt(string value, string name)
    {
        if (!int.TryParse(value, out var parsed) || parsed < 1)
            throw new ArgumentException($"'--{name}' must be a positive integer");
        return parsed;
    }

    private static int ParseNonNegativeInt(string value, string name)
    {
        if (!int.TryParse(value, out var parsed) || parsed < 0)
            throw new ArgumentException($"'--{name}' must be a non-negative integer");
        return parsed;
    }

    private static void RejectUnknown(IReadOnlyDictionary<string, string> values, params string[] allowed)
    {
        foreach (var name in values.Keys)
        {
            if (!allowed.Contains(name, StringComparer.OrdinalIgnoreCase))
                throw new ArgumentException($"option '--{name}' is not supported for this command");
        }
    }

    private static int RunRpc(string method, JsonObject parameters)
    {
        var request = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = "game-control",
            ["method"] = method,
            ["params"] = parameters,
        }.ToJsonString(Json.Options);

        return RunTool(new[] { "--rpc", request });
    }

    private static int RunTool(IEnumerable<string> arguments)
    {
        var (exitCode, stdout, stderr) = RunToolCapture(arguments);
        if (!string.IsNullOrWhiteSpace(stdout)) Console.Out.WriteLine(stdout.Trim());
        if (!string.IsNullOrWhiteSpace(stderr)) Console.Error.WriteLine(stderr.Trim());
        return exitCode;
    }

    /// <summary>
    /// Brings the capture tool's selected target window to the foreground. Games only accept
    /// gamepad input while their window is focused, so callers run this before sending input.
    /// </summary>
    private static int FocusTarget(string[] args)
    {
        if (args.Length != 0) throw new ArgumentException("'focus' does not accept options");

        var request = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = "game-control-focus",
            ["method"] = "get_state",
            ["params"] = new JsonObject(),
        }.ToJsonString(Json.Options);

        var (exitCode, stdout, stderr) = RunToolCapture(new[] { "--rpc", request });
        if (exitCode != GameControlExitCodes.Ok)
        {
            if (!string.IsNullOrWhiteSpace(stderr)) Console.Error.WriteLine(stderr.Trim());
            return exitCode;
        }

        var hwnd = ReadSelectedTargetHwnd(stdout);
        if (hwnd == 0)
        {
            Console.Error.WriteLine("no capture target is selected; pick a window in the capture tool first");
            return GameControlExitCodes.Usage;
        }

        var focused = SetForegroundWindow(new IntPtr(hwnd));
        Console.Out.WriteLine(new JsonObject { ["ok"] = focused, ["hwnd"] = hwnd }.ToJsonString(Json.Options));
        return focused ? GameControlExitCodes.Ok : GameControlExitCodes.Usage;
    }

    private static long ReadSelectedTargetHwnd(string stdout)
    {
        try
        {
            var node = JsonNode.Parse(stdout);
            return node?["result"]?["selected_target"]?["hwnd_value"]?.GetValue<long>() ?? 0;
        }
        catch (JsonException)
        {
            // Degradation boundary: an unparsable response means "no target", reported by the caller.
            return 0;
        }
    }

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    private static (int ExitCode, string Stdout, string Stderr) RunToolCapture(IEnumerable<string> arguments)
    {
        var startInfo = new System.Diagnostics.ProcessStartInfo(CaptureToolPath())
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (var argument in arguments) startInfo.ArgumentList.Add(argument);

        using var process = System.Diagnostics.Process.Start(startInfo)
            ?? throw new InvalidOperationException("could not start windows-capture");
        var stdout = process.StandardOutput.ReadToEnd();
        var stderr = process.StandardError.ReadToEnd();
        if (!process.WaitForExit(20_000))
        {
            try { process.Kill(entireProcessTree: true); } catch { }
            Console.Error.WriteLine("windows-capture did not respond within 20 seconds");
            return (GameControlExitCodes.NoHost, "", "");
        }

        return (process.ExitCode, stdout, stderr);
    }

    private static string CaptureToolPath()
    {
        var configured = Environment.GetEnvironmentVariable("WINDOWS_CAPTURE_EXE");
        if (!string.IsNullOrWhiteSpace(configured) && File.Exists(configured)) return configured;

        var sibling = Path.Combine(AppContext.BaseDirectory, "windows-capture.exe");
        if (File.Exists(sibling)) return sibling;
        throw new InvalidOperationException("set WINDOWS_CAPTURE_EXE to the windows-capture.exe path");
    }
}

public static class GameControlExitCodes
{
    public const int Ok = 0;
    public const int Usage = 2;
    public const int NoHost = 3;
}

public static class GameControlHelp
{
    public const string Text = """
game-control - bounded virtual Xbox controller for the local windows-capture host

USAGE
  game-control status
  game-control focus
  game-control connect | disconnect | release-all
  game-control press --button BUTTON --hold-ms N
  game-control move --direction forward|backward|left|right --hold-ms N
  game-control look --x -1..1 --y -1..1 --hold-ms N
  game-control trigger --side left|right --value 0..255 --hold-ms N
  game-control screenshot [--count N] [--interval-ms N] [--start-delay-ms N] [--max-edge N] [--output-dir DIR] [--no-reuse]

SAFETY
  Every physical input requires --hold-ms and N must be 1..5000. The virtual controller releases
  the button or stick in a finally block, even if the command is cancelled. Run release-all before
  changing task or after an unexpected error. The tool never injects into a process or bypasses
  anti-cheat; it creates an ordinary system-wide virtual Xbox controller through ViGEmBus.

FOCUS
  Bring the capture tool's selected target window to the foreground. Games only accept gamepad
  input while their window is focused, so run this before sending input.

SCREENSHOTS
  screenshot waits for completion and prints the capture job. Pass --output-dir DIR when the caller
  needs PNG files to inspect directly; otherwise use the host's read_result RPC before its TTL expires.

BUTTONS
  a b x y dpad-up dpad-down dpad-left dpad-right left-bumper right-bumper left-stick right-stick menu view

ENVIRONMENT
  WINDOWS_CAPTURE_EXE  Required unless game-control.exe is placed beside windows-capture.exe.
""";
}
