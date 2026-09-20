using System.Diagnostics;
using System.IO.Pipes;
using System.Security.Principal;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>Client side of the named pipe protocol, plus the single-instance plumbing.</summary>
public static class PipeClient
{
    /// <summary>Default pipe name: per user, so two users on one machine never collide.</summary>
    public static string DefaultPipeName()
    {
        string sid;
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            sid = identity.User?.Value ?? "unknown";
        }
        catch
        {
            sid = "unknown";
        }

        return "windows-capture-" + sid.Replace('-', '_');
    }

    /// <summary>Sends one request line and returns the response line.</summary>
    public static async Task<string> SendAsync(string pipeName, string requestLine, int connectTimeoutMs, int readTimeoutMs, CancellationToken cancellationToken)
    {
        using var client = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);

        try
        {
            await client.ConnectAsync(connectTimeoutMs, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            throw new CaptureException(
                ErrorCodes.Timeout,
                "no running windows-capture instance answered on the pipe",
                $"pipe '{pipeName}': {ex.Message}");
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(readTimeoutMs);

        var reader = new LineReader(client, 32 * 1024 * 1024);
        await LineWriter.WriteLineAsync(client, requestLine, timeout.Token).ConfigureAwait(false);

        string? line;
        try
        {
            line = await reader.ReadLineAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw new CaptureException(ErrorCodes.Timeout, "the host did not answer in time", $"waited {readTimeoutMs} ms");
        }

        if (line is null)
            throw new CaptureException(ErrorCodes.Timeout, "the host closed the connection without answering");

        return line;
    }

    /// <summary>Cheap liveness probe used before deciding whether to start a host.</summary>
    public static bool TryPing(string pipeName, int timeoutMs = 700)
    {
        try
        {
            var line = SendAsync(pipeName, """{"jsonrpc":"2.0","id":"ping","method":"ping"}""", timeoutMs, timeoutMs, CancellationToken.None)
                .GetAwaiter().GetResult();
            return line.Contains("\"pong\"", StringComparison.Ordinal);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Starts a hidden host for the current executable and waits until its pipe answers.
    /// Used by the CLI so <c>--capture</c> works without the user starting the GUI first.
    /// </summary>
    public static bool EnsureHost(string pipeName, int waitMs = 20_000)
    {
        if (TryPing(pipeName)) return true;

        var exe = Environment.ProcessPath;
        if (string.IsNullOrEmpty(exe)) return false;

        try
        {
            var psi = new ProcessStartInfo(exe, "--hidden")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            Process.Start(psi);
        }
        catch
        {
            return false;
        }

        var deadline = Environment.TickCount64 + waitMs;
        while (Environment.TickCount64 < deadline)
        {
            if (TryPing(pipeName)) return true;
            Thread.Sleep(150);
        }

        return false;
    }
}

/// <summary>Process-wide single instance guard.</summary>
public sealed class SingleInstance : IDisposable
{
    private Mutex? _mutex;

    private SingleInstance(Mutex mutex) => _mutex = mutex;

    /// <summary>
    /// Returns a guard when this process became the host, or <c>null</c> when another instance
    /// already owns the name (in which case the caller should forward its arguments instead).
    /// </summary>
    public static SingleInstance? TryAcquire(string pipeName)
    {
        var mutex = new Mutex(initiallyOwned: true, name: @"Local\" + pipeName + ".mutex", out var createdNew);
        if (!createdNew)
        {
            mutex.Dispose();
            return null;
        }

        return new SingleInstance(mutex);
    }

    public void Dispose()
    {
        var mutex = Interlocked.Exchange(ref _mutex, null);
        if (mutex is null) return;
        try { mutex.ReleaseMutex(); } catch (ApplicationException) { }
        mutex.Dispose();
    }
}
