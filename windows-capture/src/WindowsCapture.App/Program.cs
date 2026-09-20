using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// Single entry point for every mode. The same executable is the GUI/RPC host, the CLI client that
/// forwards work to a running host, and the self test.
/// </summary>
public static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        var options = CliOptions.Parse(args);
        var pipeName = options.PipeName ?? PipeClient.DefaultPipeName();

        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
        {
            try { Console.Error.WriteLine("unhandled: " + e.ExceptionObject); } catch { }
        };

        // Human-facing CLI modes need a console; redirected stdout works with or without one.
        if (!options.HostMode || options.ShowHelp || options.ShowVersion || options.Error is not null)
            Win32.AttachParentConsole();

        try
        {
            if (options.ShowHelp)
            {
                Console.Out.Write(CliOptions.HelpText);
                return ExitCodes.Ok;
            }

            if (options.ShowVersion)
            {
                Console.Out.WriteLine(Version());
                return ExitCodes.Ok;
            }

            if (options.Error is not null)
            {
                Console.Error.WriteLine(options.Error);
                Console.Error.WriteLine("run 'windows-capture --help' for usage");
                return ExitCodes.Usage;
            }

            if (options.SelfTest) return SelfTest.Run(options);
            if (options.RpcJson is not null || options.RpcFile is not null) return RunRpc(options, pipeName);
            if (options.ListWindows) return RunListWindows(options, pipeName);
            if (options.Exit) return Forward(options.RawArgs, pipeName, options.NoAutostart);
            if (options.Capture) return Forward(options.RawArgs, pipeName, options.NoAutostart);

            return RunHost(options, pipeName);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("unexpected failure: " + ex);
            return ExitCodes.Unexpected;
        }
    }

    private static int RunHost(CliOptions options, string pipeName)
    {
        using var guard = SingleInstance.TryAcquire(pipeName);
        if (guard is null)
        {
            // Another instance owns the pipe: hand our arguments over to it and exit.
            var forward = options.RawArgs.Length == 0 ? new[] { "--show-ui" } : options.RawArgs;
            return Forward(forward, pipeName, noAutostart: false);
        }

        var app = new App(options);
        return app.Run();
    }

    /// <summary>Relays a command line to the running host through <c>invoke_cli</c>.</summary>
    private static int Forward(IReadOnlyList<string> args, string pipeName, bool noAutostart)
    {
        if (!noAutostart && !PipeClient.EnsureHost(pipeName))
        {
            Console.Error.WriteLine($"no running windows-capture instance on pipe '{pipeName}' and it could not be started");
            return ExitCodes.NoHost;
        }

        var request = JsonSerializer.Serialize(new
        {
            jsonrpc = "2.0",
            id = "cli",
            method = "invoke_cli",
            @params = new { args },
        }, Json.Options);

        string response;
        try
        {
            response = PipeClient.SendAsync(pipeName, request, 5_000, 300_000, CancellationToken.None)
                .GetAwaiter().GetResult();
        }
        catch (CaptureException ex)
        {
            Console.Error.WriteLine($"{ex.ErrorCode}: {ex.Message}");
            return ExitCodes.NoHost;
        }

        var node = JsonNode.Parse(response);
        if (node?["error"] is JsonObject error)
        {
            Console.Error.WriteLine(error.ToJsonString(Json.Options));
            return ExitCodes.RpcError;
        }

        var result = node?["result"]?.AsObject();
        if (result is null)
        {
            Console.Error.WriteLine("malformed response: " + response);
            return ExitCodes.RpcError;
        }

        var stdout = result["stdout"]?.GetValue<string>();
        var stderr = result["stderr"]?.GetValue<string>();
        var exitCode = result["exit_code"]?.GetValue<int>() ?? ExitCodes.Unexpected;

        if (!string.IsNullOrEmpty(stdout)) Console.Out.WriteLine(stdout);
        if (!string.IsNullOrEmpty(stderr)) Console.Error.WriteLine(stderr);
        return exitCode;
    }

    /// <summary>Sends one JSON-RPC line verbatim and prints the raw response line.</summary>
    private static int RunRpc(CliOptions options, string pipeName)
    {
        string line;
        try
        {
            line = options.RpcJson ?? File.ReadAllText(options.RpcFile!);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("could not read the RPC request: " + ex.Message);
            return ExitCodes.Usage;
        }

        line = line.Trim();
        if (line.Length == 0)
        {
            Console.Error.WriteLine("the RPC request is empty");
            return ExitCodes.Usage;
        }

        return SendRaw(line, pipeName, options.NoAutostart);
    }

    /// <summary>Sugar for <c>--rpc '{"jsonrpc":"2.0","id":1,"method":"list_windows"}'</c>.</summary>
    private static int RunListWindows(CliOptions options, string pipeName)
        => SendRaw("""{"jsonrpc":"2.0","id":"list-windows","method":"list_windows"}""", pipeName, options.NoAutostart);

    private static int SendRaw(string line, string pipeName, bool noAutostart)
    {
        if (!noAutostart && !PipeClient.TryPing(pipeName) && !PipeClient.EnsureHost(pipeName))
        {
            Console.Error.WriteLine($"no running windows-capture instance on pipe '{pipeName}'");
            return ExitCodes.NoHost;
        }

        string response;
        try
        {
            response = PipeClient.SendAsync(pipeName, line, 5_000, 300_000, CancellationToken.None)
                .GetAwaiter().GetResult();
        }
        catch (CaptureException ex)
        {
            Console.Error.WriteLine($"{ex.ErrorCode}: {ex.Message}");
            return ExitCodes.NoHost;
        }

        Console.Out.WriteLine(response);

        var node = JsonNode.Parse(response);
        if (node?["error"] is not null) return ExitCodes.RpcError;
        return node?["result"] is not null ? ExitCodes.Ok : ExitCodes.RpcError;
    }

    private static string Version()
    {
        var assembly = Assembly.GetExecutingAssembly();
        return assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
            ?? assembly.GetName().Version?.ToString()
            ?? "0.0.0";
    }
}
