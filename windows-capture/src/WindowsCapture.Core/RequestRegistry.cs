namespace WindowsCapture.Core;

public enum RegistryOutcome
{
    /// <summary>Never seen this <c>request_id</c>.</summary>
    Miss,

    /// <summary>Seen with identical parameters: replay the stored payload.</summary>
    Replay,

    /// <summary>Seen with different parameters: refuse instead of silently doing something else.</summary>
    Conflict,
}

public readonly record struct RegistryEntry(string Fingerprint, bool IsError, string PayloadJson);

public readonly record struct RegistryLookup(RegistryOutcome Outcome, RegistryEntry? Entry);

/// <summary>
/// Bounded cache implementing <c>request_id</c> idempotency. A retried request with the same id and
/// the same parameters returns the original payload; the same id with different parameters is a
/// conflict. Eviction is least-recently-recorded, so the cache can never grow without bound.
/// </summary>
public sealed class RequestRegistry
{
    private readonly int _capacity;
    private readonly object _sync = new();
    private readonly Dictionary<string, RegistryEntry> _map = new(StringComparer.Ordinal);
    private readonly LinkedList<string> _order = new();

    public RequestRegistry(int capacity) => _capacity = Math.Max(1, capacity);

    public int Count
    {
        get { lock (_sync) return _map.Count; }
    }

    public RegistryLookup Lookup(string requestId, string fingerprint)
    {
        lock (_sync)
        {
            if (!_map.TryGetValue(requestId, out var entry))
                return new RegistryLookup(RegistryOutcome.Miss, null);

            if (!string.Equals(entry.Fingerprint, fingerprint, StringComparison.Ordinal))
                return new RegistryLookup(RegistryOutcome.Conflict, entry);

            _order.Remove(requestId);
            _order.AddLast(requestId);
            return new RegistryLookup(RegistryOutcome.Replay, entry);
        }
    }

    public void Record(string requestId, string fingerprint, bool isError, string payloadJson)
    {
        lock (_sync)
        {
            if (_map.ContainsKey(requestId)) _order.Remove(requestId);

            _map[requestId] = new RegistryEntry(fingerprint, isError, payloadJson);
            _order.AddLast(requestId);

            while (_map.Count > _capacity && _order.First is { } first)
            {
                _order.RemoveFirst();
                _map.Remove(first.Value);
            }
        }
    }

    public void Clear()
    {
        lock (_sync)
        {
            _map.Clear();
            _order.Clear();
        }
    }
}

/// <summary>
/// Pure helper for the chunked <c>read_result</c> protocol. Kept separate from the job store so the
/// boundary behaviour (clamping, EOF, invalid ranges) can be unit tested directly.
/// </summary>
public static class ResultChunker
{
    public static (int Length, bool Eof) Normalise(long totalBytes, long offset, int requestedLength, int maxChunk)
    {
        if (totalBytes < 0) throw CaptureException.InvalidParams("total bytes must not be negative");
        if (offset < 0) throw CaptureException.InvalidParams("offset must not be negative", $"got {offset}");
        if (offset > totalBytes)
            throw new CaptureException(ErrorCodes.ResultRangeInvalid, "offset is past the end of the result", $"offset={offset} total={totalBytes}");
        if (requestedLength <= 0) throw CaptureException.InvalidParams("length must be positive", $"got {requestedLength}");
        if (maxChunk <= 0) throw new ArgumentOutOfRangeException(nameof(maxChunk));

        var remaining = totalBytes - offset;
        var length = (int)Math.Min(Math.Min((long)requestedLength, maxChunk), remaining);
        var eof = offset + length >= totalBytes;
        return (length, eof);
    }
}
