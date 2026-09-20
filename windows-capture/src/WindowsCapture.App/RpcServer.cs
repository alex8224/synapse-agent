using System.IO;
using System.IO.Pipes;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// Newline-framed JSON-RPC 2.0 server on a named pipe.
///
/// The pipe is created with <see cref="PipeOptions.CurrentUserOnly"/>, so only processes running as
/// the same user can connect; no TCP port is ever opened and no remote endpoint exists.
///
/// Concurrency and liveness:
/// <list type="bullet">
/// <item>The number of live server instances is capped by a <see cref="SemaphoreSlim"/> that is
/// reserved <em>before</em> a <see cref="NamedPipeServerStream"/> is created and released when the
/// connection ends. The accept loop therefore never creates more instances than
/// <see cref="Limits.MaxPipeInstances"/> and never sees the "all pipe instances are busy"
/// <see cref="IOException"/> that would otherwise fault it permanently.</item>
/// <item>Every transient accept failure (instance creation or connection wait) frees its slot and
/// backs off briefly before retrying, so the loop recovers instead of dying or spinning hot.</item>
/// <item>Each request read has an idle deadline (<see cref="Limits.PipeIdleTimeoutMs"/>) and each
/// response write a deadline (<see cref="Limits.PipeWriteTimeoutMs"/>), so a client that connects
/// and then goes silent is dropped and its slot reused.</item>
/// </list>
/// </summary>
public sealed class RpcServer : IDisposable
{
    /// <summary>Pause after a transient accept failure so the loop cannot spin hot.</summary>
    private static readonly TimeSpan AcceptRetryDelay = TimeSpan.FromMilliseconds(100);

    private readonly string _pipeName;
    private readonly RpcDispatcher _dispatcher;
    private readonly Limits _limits;

    /// <summary>
    /// Bounds the number of pipe instances that exist at once (one per served connection). Acquired
    /// before an instance is created and released when the connection ends.
    /// </summary>
    private readonly SemaphoreSlim _slots;

    private readonly CancellationTokenSource _cts = new();
    private readonly List<Task> _connections = new();
    private readonly object _sync = new();
    private Task? _acceptLoop;
    private bool _disposed;

    public RpcServer(string pipeName, RpcDispatcher dispatcher, Limits limits)
    {
        _pipeName = pipeName;
        _dispatcher = dispatcher;
        _limits = limits;
        _slots = new SemaphoreSlim(Math.Max(1, limits.MaxPipeInstances));
    }

    public string PipeName => _pipeName;

    /// <summary>Number of currently connected clients. Exposed for diagnostics/self test.</summary>
    public int ActiveConnections
    {
        get { lock (_sync) return _connections.Count; }
    }

    public void Start()
    {
        _acceptLoop = Task.Run(() => AcceptLoopAsync(_cts.Token));
    }

    private async Task AcceptLoopAsync(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            // Reserve a connection slot *before* creating the pipe instance. Creating more instances
            // than MaxPipeInstances throws IOException; doing this up front means we simply wait for a
            // slot to free instead of racing the instance limit.
            try
            {
                await _slots.WaitAsync(cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }

            var handedOff = false;
            try
            {
                NamedPipeServerStream server;
                try
                {
                    server = new NamedPipeServerStream(
                        _pipeName,
                        PipeDirection.InOut,
                        _limits.MaxPipeInstances,
                        PipeTransmissionMode.Byte,
                        PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                }
                catch (Exception) when (cancellationToken.IsCancellationRequested)
                {
                    return;
                }
                catch (Exception)
                {
                    // Transient (e.g. the name is momentarily taken). Free the slot, back off briefly
                    // and retry rather than letting the accept loop die.
                    await DelayQuietlyAsync(cancellationToken).ConfigureAwait(false);
                    continue;
                }

                try
                {
                    await server.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    server.Dispose();
                    return;
                }
                catch (Exception)
                {
                    // The instance failed before a client connected: drop it, free the slot, retry.
                    server.Dispose();
                    await DelayQuietlyAsync(cancellationToken).ConfigureAwait(false);
                    continue;
                }

                handedOff = true;
                var task = Task.Run(() => ServeConnectionAsync(server, cancellationToken), CancellationToken.None);
                lock (_sync) _connections.Add(task);
                _ = task.ContinueWith(
                    t => { lock (_sync) _connections.Remove(t); },
                    CancellationToken.None,
                    TaskContinuationOptions.ExecuteSynchronously,
                    TaskScheduler.Default);
            }
            finally
            {
                // A handed-off instance releases its own slot from ServeConnectionAsync once the
                // connection ends; only free it here when we did not hand it off.
                if (!handedOff) _slots.Release();
            }
        }
    }

    private static async Task DelayQuietlyAsync(CancellationToken cancellationToken)
    {
        try
        {
            await Task.Delay(AcceptRetryDelay, cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // Shutting down.
        }
    }

    private async Task ServeConnectionAsync(NamedPipeServerStream stream, CancellationToken cancellationToken)
    {
        try
        {
            using var connection = stream;
            var reader = new LineReader(connection, _limits.MaxRequestBytes);

            while (!cancellationToken.IsCancellationRequested)
            {
                string? line;
                try
                {
                    line = await ReadLineWithIdleTimeoutAsync(reader, cancellationToken).ConfigureAwait(false);
                }
                catch (CaptureException ex)
                {
                    // Oversized frame: answer with a structured error instead of dropping the client.
                    var payload = JsonRpcResponses.Error(null, JsonRpcErrorCode.InvalidRequest, ex.Message, ex.ErrorCode, ex.Detail);
                    await WriteLineWithTimeoutAsync(connection, payload, cancellationToken).ConfigureAwait(false);
                    return;
                }
                catch (Exception)
                {
                    return;
                }

                if (line is null) return;
                if (line.Length == 0) continue;

                string response;
                try
                {
                    response = await _dispatcher.DispatchLineAsync(line, cancellationToken).ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    response = JsonRpcResponses.Error(null, JsonRpcErrorCode.InternalError, ex.Message, ErrorCodes.Internal, ex.GetType().Name);
                }

                if (!await WriteLineWithTimeoutAsync(connection, response, cancellationToken).ConfigureAwait(false))
                    return;
            }
        }
        finally
        {
            _slots.Release();
        }
    }

    /// <summary>
    /// Reads one request line, dropping the connection (returns <c>null</c>) when the client stays
    /// silent for longer than <see cref="Limits.PipeIdleTimeoutMs"/>. Without the deadline an idle or
    /// malicious same-user client could hold one of the <see cref="Limits.MaxPipeInstances"/> slots
    /// indefinitely.
    /// </summary>
    private async Task<string?> ReadLineWithIdleTimeoutAsync(LineReader reader, CancellationToken cancellationToken)
    {
        var timeoutMs = _limits.PipeIdleTimeoutMs;
        if (timeoutMs <= 0)
            return await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);

        using var idle = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        idle.CancelAfter(timeoutMs);
        try
        {
            return await reader.ReadLineAsync(idle.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            // Idle deadline hit: signal end-of-connection to the caller.
            return null;
        }
    }

    /// <summary>Writes one response line under <see cref="Limits.PipeWriteTimeoutMs"/>. Returns false on any failure.</summary>
    private async Task<bool> WriteLineWithTimeoutAsync(Stream stream, string line, CancellationToken cancellationToken)
    {
        var timeoutMs = _limits.PipeWriteTimeoutMs;
        if (timeoutMs <= 0)
        {
            try { await LineWriter.WriteLineAsync(stream, line, cancellationToken).ConfigureAwait(false); return true; }
            catch (Exception) { return false; }
        }

        using var write = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        write.CancelAfter(timeoutMs);
        try
        {
            await LineWriter.WriteLineAsync(stream, line, write.Token).ConfigureAwait(false);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        try { _cts.Cancel(); } catch (Exception) { }

        Task[] pending;
        lock (_sync) pending = _connections.ToArray();

        try { Task.WaitAll(pending, TimeSpan.FromSeconds(2)); } catch (Exception) { }
        try { _acceptLoop?.Wait(TimeSpan.FromSeconds(2)); } catch (Exception) { }

        _slots.Dispose();
        _cts.Dispose();
    }
}
