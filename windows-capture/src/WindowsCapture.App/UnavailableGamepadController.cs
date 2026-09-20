using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// A no-op controller for infrastructure self-tests. It deliberately never loads ViGEm or creates
/// a device, while preserving the ordinary structured <c>gamepad_unavailable</c> failure.
/// </summary>
internal sealed class UnavailableGamepadController : IGamepadController
{
    public bool IsAvailable => false;
    public bool IsConnected => false;
    public string? UnavailableReason => "not enabled for this host";
    public int? UserIndex => null;

    public void Connect() => throw Unavailable();
    public void Press(GamepadButton button, int holdMs, CancellationToken cancellationToken) => throw Unavailable();
    public void Move(GamepadDirection direction, int holdMs, CancellationToken cancellationToken) => throw Unavailable();
    public void Look(double x, double y, int holdMs, CancellationToken cancellationToken) => throw Unavailable();
    public void Trigger(GamepadTrigger side, byte value, int holdMs, CancellationToken cancellationToken) => throw Unavailable();
    public void ReleaseAll() { }
    public void Disconnect() { }
    public void Dispose() { }

    private static CaptureException Unavailable()
        => new(GamepadErrorCodes.Unavailable, "the virtual gamepad is unavailable", "not enabled for this host");
}
