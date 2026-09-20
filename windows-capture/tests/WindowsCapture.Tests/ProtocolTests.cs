using System.Text;
using System.Text.Json;
using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class LineFramingTests
{
    private static async Task<string?> ReadOne(string content, int maxBytes = 1024)
    {
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes(content));
        var reader = new LineReader(stream, maxBytes);
        return await reader.ReadLineAsync(CancellationToken.None);
    }

    [Fact]
    public async Task ReadsASingleLineAndStripsTheNewline()
    {
        Assert.Equal("""{"a":1}""", await ReadOne("{\"a\":1}\n"));
    }

    [Fact]
    public async Task StripsCarriageReturnFromCrlfFrames()
    {
        Assert.Equal("""{"a":1}""", await ReadOne("{\"a\":1}\r\n"));
    }

    [Fact]
    public async Task ReturnsNullAtEndOfStream()
    {
        Assert.Null(await ReadOne(""));
    }

    [Fact]
    public async Task ReturnsTrailingLineWithoutNewline()
    {
        Assert.Equal("tail", await ReadOne("tail"));
    }

    [Fact]
    public async Task ReadsPipelinedFramesFromOneBuffer()
    {
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes("one\ntwo\nthree\n"));
        var reader = new LineReader(stream, 1024);

        Assert.Equal("one", await reader.ReadLineAsync(CancellationToken.None));
        Assert.Equal("two", await reader.ReadLineAsync(CancellationToken.None));
        Assert.Equal("three", await reader.ReadLineAsync(CancellationToken.None));
        Assert.Null(await reader.ReadLineAsync(CancellationToken.None));
    }

    [Fact]
    public async Task HandlesUtf8SplitAcrossReads()
    {
        var payload = "{\"t\":\"\u4e2d\u6587\"}\n";
        using var stream = new SlowStream(Encoding.UTF8.GetBytes(payload), chunkSize: 3);
        var reader = new LineReader(stream, 1024);

        var line = await reader.ReadLineAsync(CancellationToken.None);
        Assert.Equal("{\"t\":\"\u4e2d\u6587\"}", line);
    }

    [Fact]
    public async Task OversizedFrameIsRejected()
    {
        using var stream = new MemoryStream(Encoding.UTF8.GetBytes(new string('x', 4096) + "\n"));
        var reader = new LineReader(stream, 128);

        var ex = await Assert.ThrowsAsync<CaptureException>(() => reader.ReadLineAsync(CancellationToken.None));
        Assert.Equal(ErrorCodes.LimitExceeded, ex.ErrorCode);
    }

    [Fact]
    public async Task WriteLineAppendsExactlyOneNewline()
    {
        using var stream = new MemoryStream();
        await LineWriter.WriteLineAsync(stream, "hello", CancellationToken.None);
        await LineWriter.WriteLineAsync(stream, "world", CancellationToken.None);

        Assert.Equal("hello\nworld\n", Encoding.UTF8.GetString(stream.ToArray()));
    }

    /// <summary>Delivers the buffer in small chunks so multi-byte sequences span reads.</summary>
    private sealed class SlowStream : Stream
    {
        private readonly byte[] _data;
        private readonly int _chunk;
        private int _position;

        public SlowStream(byte[] data, int chunkSize)
        {
            _data = data;
            _chunk = chunkSize;
        }

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => _data.Length;
        public override long Position { get => _position; set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count) => ReadAsync(buffer.AsMemory(offset, count)).AsTask().GetAwaiter().GetResult();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            if (_position >= _data.Length) return ValueTask.FromResult(0);
            var take = Math.Min(Math.Min(_chunk, buffer.Length), _data.Length - _position);
            _data.AsSpan(_position, take).CopyTo(buffer.Span);
            _position += take;
            return ValueTask.FromResult(take);
        }
    }
}

public class JsonCanonicalizeTests
{
    private static string Canon(string json)
    {
        using var document = JsonDocument.Parse(json);
        return Json.Canonicalize(document.RootElement);
    }

    [Fact]
    public void PropertyOrderDoesNotChangeTheFingerprint()
    {
        Assert.Equal(Canon("""{"a":1,"b":2}"""), Canon("""{"b":2,"a":1}"""));
    }

    [Fact]
    public void NestedOrderDoesNotChangeTheFingerprint()
    {
        Assert.Equal(Canon("""{"x":{"a":1,"b":[1,2]}}"""), Canon("""{"x":{"b":[1,2],"a":1}}"""));
    }

    [Fact]
    public void DifferentValuesProduceDifferentFingerprints()
    {
        Assert.NotEqual(Canon("""{"a":1}"""), Canon("""{"a":2}"""));
    }

    [Fact]
    public void NumericSpellingIsNormalised()
    {
        Assert.Equal(Canon("""{"a":1}"""), Canon("""{"a":1.0}"""));
    }

    [Fact]
    public void MissingParamsAreStable()
    {
        Assert.Equal("null", Json.Canonicalize(null));
    }
}

public class ResultChunkerTests
{
    [Fact]
    public void ReturnsTheRequestedSlice()
    {
        var (length, eof) = ResultChunker.Normalise(1000, 0, 100, 256);
        Assert.Equal(100, length);
        Assert.False(eof);
    }

    [Fact]
    public void ClampsToTheChunkLimit()
    {
        var (length, eof) = ResultChunker.Normalise(1_000_000, 0, 500_000, 4096);
        Assert.Equal(4096, length);
        Assert.False(eof);
    }

    [Fact]
    public void ClampsToWhatIsLeft()
    {
        var (length, eof) = ResultChunker.Normalise(100, 90, 4096, 4096);
        Assert.Equal(10, length);
        Assert.True(eof);
    }

    [Fact]
    public void ReadingExactlyToTheEndReportsEof()
    {
        var (length, eof) = ResultChunker.Normalise(100, 50, 50, 4096);
        Assert.Equal(50, length);
        Assert.True(eof);
    }

    [Fact]
    public void OffsetPastTheEndIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() => ResultChunker.Normalise(100, 101, 10, 4096));
        Assert.Equal(ErrorCodes.ResultRangeInvalid, ex.ErrorCode);
    }

    [Fact]
    public void NegativeOffsetIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() => ResultChunker.Normalise(100, -1, 10, 4096));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }

    [Fact]
    public void NonPositiveLengthIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() => ResultChunker.Normalise(100, 0, 0, 4096));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }
}

public class RequestRegistryTests
{
    [Fact]
    public void UnknownRequestIsAMiss()
    {
        var registry = new RequestRegistry(4);
        Assert.Equal(RegistryOutcome.Miss, registry.Lookup("r1", "fp").Outcome);
    }

    [Fact]
    public void SameIdAndFingerprintReplays()
    {
        var registry = new RequestRegistry(4);
        registry.Record("r1", "fp", false, """{"job_id":"j1"}""");

        var lookup = registry.Lookup("r1", "fp");
        Assert.Equal(RegistryOutcome.Replay, lookup.Outcome);
        Assert.Equal("""{"job_id":"j1"}""", lookup.Entry!.Value.PayloadJson);
    }

    [Fact]
    public void SameIdWithDifferentFingerprintConflicts()
    {
        var registry = new RequestRegistry(4);
        registry.Record("r1", "fp1", false, "{}");
        Assert.Equal(RegistryOutcome.Conflict, registry.Lookup("r1", "fp2").Outcome);
    }

    [Fact]
    public void EvictionKeepsTheCacheBounded()
    {
        var registry = new RequestRegistry(2);
        registry.Record("a", "fp", false, "{}");
        registry.Record("b", "fp", false, "{}");
        registry.Record("c", "fp", false, "{}");

        Assert.Equal(2, registry.Count);
        Assert.Equal(RegistryOutcome.Miss, registry.Lookup("a", "fp").Outcome);
        Assert.Equal(RegistryOutcome.Replay, registry.Lookup("c", "fp").Outcome);
    }

    [Fact]
    public void RerecordingRefreshesTheOrder()
    {
        var registry = new RequestRegistry(2);
        registry.Record("a", "fp", false, "{}");
        registry.Record("b", "fp", false, "{}");
        registry.Lookup("a", "fp");
        registry.Record("c", "fp", false, "{}");

        Assert.Equal(RegistryOutcome.Replay, registry.Lookup("a", "fp").Outcome);
        Assert.Equal(RegistryOutcome.Miss, registry.Lookup("b", "fp").Outcome);
    }
}
