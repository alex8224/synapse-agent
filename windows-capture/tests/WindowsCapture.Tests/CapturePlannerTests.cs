using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class CapturePlannerTests
{
    private static readonly Limits L = new();

    [Fact]
    public void DefaultsAreUsedWhenNothingElseIsProvided()
    {
        var plan = CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target() }, null, L);

        Assert.Equal(CaptureDefaults.Count, plan.Count);
        Assert.Equal(CaptureDefaults.IntervalMs, plan.IntervalMs);
        Assert.Equal(CaptureDefaults.StartDelayMs, plan.StartDelayMs);
        Assert.Equal(CaptureDefaults.MaxEdge, plan.MaxEdge);
        Assert.Equal("png", plan.Format);
        Assert.Equal(L.DefaultTtlSeconds, plan.TtlSeconds);
        Assert.Equal(L.DefaultFrameTimeoutMs, plan.FrameTimeoutMs);
    }

    [Fact]
    public void SavedConfigOverridesDefaults()
    {
        var saved = new AppConfig { Count = 7, IntervalMs = 900, MaxEdge = 640, TtlSeconds = 42 };
        var plan = CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target() }, saved, L);

        Assert.Equal(7, plan.Count);
        Assert.Equal(900, plan.IntervalMs);
        Assert.Equal(640, plan.MaxEdge);
        Assert.Equal(42, plan.TtlSeconds);
    }

    [Fact]
    public void TemporaryOverrideWinsOverSavedConfigAndIsNotPersisted()
    {
        var saved = new AppConfig { Count = 7, IntervalMs = 900 };
        var request = new CaptureRequest { Target = TestData.Target(), Count = 3 };

        var plan = CapturePlanner.Resolve(request, saved, L);
        Assert.Equal(3, plan.Count);
        Assert.Equal(900, plan.IntervalMs); // not overridden -> still from config

        var merged = CapturePlanner.Merge(saved, request, plan);
        Assert.Equal(3, merged.Count);
        Assert.Equal(900, merged.IntervalMs);
        Assert.Equal(plan.Target, merged.LastTarget);
    }

    [Fact]
    public void MergeDoesNotWriteFieldsTheCallerDidNotSupply()
    {
        var saved = new AppConfig { Count = 7, IntervalMs = 900, MaxEdge = 640 };
        var request = new CaptureRequest { Target = TestData.Target(), Count = 2 };
        var plan = CapturePlanner.Resolve(request, saved, L);

        var merged = CapturePlanner.Merge(saved, request, plan);
        Assert.Equal(2, merged.Count);
        Assert.Equal(900, merged.IntervalMs);
        Assert.Equal(640, merged.MaxEdge);
    }

    [Fact]
    public void SavedTargetIsUsedWhenTheRequestHasNone()
    {
        var saved = new AppConfig { LastTarget = TestData.Target(hwnd: 0xABCD, pid: 5) };
        var plan = CapturePlanner.Resolve(new CaptureRequest(), saved, L);
        Assert.Equal(0xABCD, plan.Target.Hwnd);
    }

    [Fact]
    public void MissingTargetRaisesTargetRequired()
    {
        var ex = Assert.Throws<CaptureException>(() => CapturePlanner.Resolve(new CaptureRequest(), null, L));
        Assert.Equal(ErrorCodes.TargetRequired, ex.ErrorCode);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(10_000)]
    public void CountOutOfRangeIsRejected(int count)
    {
        var ex = Assert.Throws<CaptureException>(() =>
            CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), Count = count }, null, L));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }

    [Fact]
    public void IntervalAboveLimitIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() =>
            CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), IntervalMs = L.MaxIntervalMs + 1 }, null, L));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }

    [Fact]
    public void MaxEdgeAboveLimitIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() =>
            CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), MaxEdge = L.MaxEdge + 1 }, null, L));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }

    [Fact]
    public void UnsupportedFormatIsRejected()
    {
        var ex = Assert.Throws<CaptureException>(() =>
            CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), Format = "jpeg" }, null, L));
        Assert.Equal(ErrorCodes.InvalidParams, ex.ErrorCode);
    }

    [Fact]
    public void FormatIsNormalised()
    {
        var plan = CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), Format = "PNG" }, null, L);
        Assert.Equal("png", plan.Format);
    }

    [Fact]
    public void BlankOutputDirectoryIsTreatedAsUnset()
    {
        var plan = CapturePlanner.Resolve(new CaptureRequest { Target = TestData.Target(), OutputDir = "   " }, null, L);
        Assert.Null(plan.OutputDir);
    }
}
