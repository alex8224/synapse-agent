using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace WindowsCapture.Core;

/// <summary>Single source of truth for the wire format.</summary>
public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DictionaryKeyPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
        WriteIndented = false,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    public static string Serialize<T>(T value) => JsonSerializer.Serialize(value, Options);

    /// <summary>Same contract, pretty printed. Used only for the on-disk config file.</summary>
    public static readonly JsonSerializerOptions IndentedOptions = new(Options) { WriteIndented = true };

    /// <summary>
    /// Serialise a value into a <see cref="JsonNode"/> so it can be embedded into a JSON-RPC
    /// envelope without an intermediate string round-trip.
    /// </summary>
    public static JsonNode? ToNode<T>(T value) => JsonSerializer.SerializeToNode(value, Options);

    public static T? Deserialize<T>(JsonElement element) => element.Deserialize<T>(Options);

    /// <summary>
    /// Deterministic, key-sorted serialisation of a JSON value. Used to fingerprint a request so a
    /// retried <c>request_id</c> with identical parameters replays and a retried id with different
    /// parameters is reported as a conflict.
    /// </summary>
    public static string Canonicalize(JsonElement? element)
    {
        if (element is null || element.Value.ValueKind == JsonValueKind.Undefined) return "null";
        var sb = new StringBuilder();
        WriteCanonical(sb, element.Value);
        return sb.ToString();
    }

    private static void WriteCanonical(StringBuilder sb, JsonElement element)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                var props = new List<JsonProperty>();
                foreach (var p in element.EnumerateObject()) props.Add(p);
                props.Sort(static (a, b) => string.CompareOrdinal(a.Name, b.Name));
                sb.Append('{');
                for (var i = 0; i < props.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append(JsonSerializer.Serialize(props[i].Name));
                    sb.Append(':');
                    WriteCanonical(sb, props[i].Value);
                }
                sb.Append('}');
                break;

            case JsonValueKind.Array:
                sb.Append('[');
                var first = true;
                foreach (var item in element.EnumerateArray())
                {
                    if (!first) sb.Append(',');
                    first = false;
                    WriteCanonical(sb, item);
                }
                sb.Append(']');
                break;

            case JsonValueKind.String:
                sb.Append(JsonSerializer.Serialize(element.GetString()));
                break;

            case JsonValueKind.Number:
                // Normalise numeric spelling so 1, 1.0 and "1" fingerprint identically.
                if (element.TryGetInt64(out var l)) sb.Append(l.ToString(System.Globalization.CultureInfo.InvariantCulture));
                else if (element.TryGetDouble(out var d)) sb.Append(d.ToString("R", System.Globalization.CultureInfo.InvariantCulture));
                else sb.Append(element.GetRawText());
                break;

            case JsonValueKind.True: sb.Append("true"); break;
            case JsonValueKind.False: sb.Append("false"); break;
            default: sb.Append("null"); break;
        }
    }
}
