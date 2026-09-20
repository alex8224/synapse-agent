using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace WindowsCapture.Core;

/// <summary>Standard JSON-RPC 2.0 codes plus one application server-error code.</summary>
public static class JsonRpcErrorCode
{
    public const int ParseError = -32700;
    public const int InvalidRequest = -32600;
    public const int MethodNotFound = -32601;
    public const int InvalidParams = -32602;
    public const int InternalError = -32603;

    /// <summary>Server error bucket; the precise cause is always in <c>error.data.error_code</c>.</summary>
    public const int ServerError = -32000;
}

/// <summary>
/// One JSON-RPC 2.0 request. <c>request_id</c> is an optional extra member (outside the JSON-RPC
/// spec, which allows additional members) used for idempotent retries.
/// </summary>
public sealed class JsonRpcRequest
{
    [JsonPropertyName("jsonrpc")] public string? JsonRpc { get; set; }
    [JsonPropertyName("id")] public JsonElement? Id { get; set; }
    [JsonPropertyName("method")] public string? Method { get; set; }
    [JsonPropertyName("params")] public JsonElement? Params { get; set; }
    [JsonPropertyName("request_id")] public string? RequestId { get; set; }
}

public static class JsonRpcResponses
{
    public static string Success(JsonElement? id, JsonNode? result)
    {
        var obj = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = IdNode(id),
            ["result"] = result ?? new JsonObject(),
        };
        return obj.ToJsonString(Json.Options);
    }

    public static string Error(JsonElement? id, int code, string message, string errorCode, string? detail)
    {
        var data = new JsonObject { ["error_code"] = errorCode };
        if (detail is not null) data["detail"] = detail;

        var obj = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = IdNode(id),
            ["error"] = new JsonObject
            {
                ["code"] = code,
                ["message"] = message,
                ["data"] = data,
            },
        };
        return obj.ToJsonString(Json.Options);
    }

    private static JsonNode? IdNode(JsonElement? id)
    {
        if (id is null) return null;
        var raw = id.Value.GetRawText();
        return JsonNode.Parse(raw);
    }
}

/// <summary>
/// Newline-delimited UTF-8 framing. One JSON document per line; the reader keeps any bytes that
/// arrived past the newline so pipelined requests are not lost, and refuses to buffer more than
/// <c>maxBytes</c> for a single frame.
/// </summary>
public sealed class LineReader
{
    private readonly Stream _stream;
    private readonly int _maxBytes;
    private readonly byte[] _buffer = new byte[8192];
    private int _start;
    private int _end;

    public LineReader(Stream stream, int maxBytes)
    {
        _stream = stream;
        _maxBytes = maxBytes;
    }

    /// <summary>Returns the next line, or <c>null</c> at end of stream.</summary>
    public async Task<string?> ReadLineAsync(CancellationToken cancellationToken)
    {
        using var accumulated = new MemoryStream();

        while (true)
        {
            if (_start < _end)
            {
                var newline = Array.IndexOf(_buffer, (byte)'\n', _start, _end - _start);
                if (newline >= 0)
                {
                    accumulated.Write(_buffer, _start, newline - _start);
                    _start = newline + 1;
                    return Decode(accumulated);
                }

                accumulated.Write(_buffer, _start, _end - _start);
                _start = _end = 0;
                Guard(accumulated.Length);
            }

            _start = 0;
            _end = await _stream.ReadAsync(_buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
            if (_end == 0)
            {
                if (accumulated.Length == 0) return null;
                return Decode(accumulated);
            }
        }
    }

    private void Guard(long length)
    {
        if (length > _maxBytes)
        {
            throw new CaptureException(
                ErrorCodes.LimitExceeded,
                "request frame is larger than the configured limit",
                $"{length} > {_maxBytes} bytes; increase Limits.MaxRequestBytes");
        }
    }

    private string Decode(MemoryStream accumulated)
    {
        Guard(accumulated.Length);
        var text = Encoding.UTF8.GetString(accumulated.GetBuffer(), 0, (int)accumulated.Length);
        return text.TrimEnd('\r', '\n');
    }
}

public static class LineWriter
{
    public static async Task WriteLineAsync(Stream stream, string line, CancellationToken cancellationToken)
    {
        var bytes = Encoding.UTF8.GetBytes(line + "\n");
        await stream.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }
}
