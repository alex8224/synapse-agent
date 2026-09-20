using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class TargetIdentityTests
{
    private static readonly DateTime Start = new(2024, 5, 1, 12, 0, 0, DateTimeKind.Utc);

    [Fact]
    public void MatchingStartTimeIsVerified()
    {
        var processes = new FakeProcessInfo();
        processes.Starts[100] = Start;

        var target = TestData.Target(pid: 100, start: Start.ToString("o"));
        var result = TargetIdentity.Check(target, processes);

        Assert.Equal(IdentityStatus.Verified, result.Status);
        Assert.True(result.IsUsable);
    }

    [Fact]
    public void DifferentStartTimeIsAMismatch()
    {
        var processes = new FakeProcessInfo();
        processes.Starts[100] = Start.AddMinutes(5);

        var target = TestData.Target(pid: 100, start: Start.ToString("o"));
        var result = TargetIdentity.Check(target, processes);

        Assert.Equal(IdentityStatus.Mismatch, result.Status);
        Assert.False(result.IsUsable);
        Assert.NotNull(result.Detail);
    }

    [Fact]
    public void SmallClockSkewIsStillVerified()
    {
        var processes = new FakeProcessInfo();
        processes.Starts[100] = Start.AddMilliseconds(800);

        var result = TargetIdentity.Check(TestData.Target(pid: 100, start: Start.ToString("o")), processes);
        Assert.Equal(IdentityStatus.Verified, result.Status);
    }

    [Fact]
    public void MissingStartTimeIsReportedAsNotRequested()
    {
        var result = TargetIdentity.Check(TestData.Target(pid: 100), new FakeProcessInfo());
        Assert.Equal(IdentityStatus.NotRequested, result.Status);
        Assert.True(result.IsUsable);
    }

    [Fact]
    public void UnknownProcessIsUnverifiableButUsable()
    {
        var result = TargetIdentity.Check(TestData.Target(pid: 999, start: Start.ToString("o")), new FakeProcessInfo());
        Assert.Equal(IdentityStatus.Unverifiable, result.Status);
        Assert.True(result.IsUsable);
    }

    [Fact]
    public void UnparsableStartTimeIsUnverifiable()
    {
        var result = TargetIdentity.Check(TestData.Target(pid: 100, start: "not-a-date"), new FakeProcessInfo());
        Assert.Equal(IdentityStatus.Unverifiable, result.Status);
    }

    [Fact]
    public void ThrowIfMismatchOnlyThrowsForMismatch()
    {
        var processes = new FakeProcessInfo();
        processes.Starts[100] = Start.AddHours(3);

        var ex = Assert.Throws<CaptureException>(() =>
            TargetIdentity.ThrowIfMismatch(TestData.Target(pid: 100, start: Start.ToString("o")), processes));
        Assert.Equal(ErrorCodes.TargetIdentityMismatch, ex.ErrorCode);

        // Unverifiable must not throw.
        TargetIdentity.ThrowIfMismatch(TestData.Target(pid: 12345, start: Start.ToString("o")), processes);
        TargetIdentity.ThrowIfMismatch(TestData.Target(pid: 100), processes);
    }
}
