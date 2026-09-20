namespace WindowsCapture.Core;

/// <summary>Stable error codes for the bounded virtual-gamepad surface.</summary>
public static class GamepadErrorCodes
{
    public const string Unavailable = "gamepad_unavailable";
    public const string InvalidButton = "gamepad_invalid_button";
    public const string InvalidDirection = "gamepad_invalid_direction";
    public const string DurationExceeded = "gamepad_duration_exceeded";
    public const string NotConnected = "gamepad_not_connected";
    public const string InvalidTriggerValue = "gamepad_invalid_trigger_value";
}

/// <summary>One white-listed Xbox button exposed to callers.</summary>
public enum GamepadButton
{
    A,
    B,
    X,
    Y,
    DpadUp,
    DpadDown,
    DpadLeft,
    DpadRight,
    LeftBumper,
    RightBumper,
    LeftStick,
    RightStick,
    Menu,
    View,
}

/// <summary>Cardinal direction accepted for a finite left-stick movement.</summary>
public enum GamepadDirection
{
    Forward,
    Backward,
    Left,
    Right,
}

/// <summary>Which analog trigger to drive.</summary>
public enum GamepadTrigger
{
    Left,
    Right,
}

/// <summary>
/// A small, explicitly bounded virtual gamepad surface. Implementations must make
/// <see cref="ReleaseAll"/> idempotent and release every control during disposal.
/// </summary>
public interface IGamepadController : IDisposable
{
    bool IsAvailable { get; }
    bool IsConnected { get; }
    string? UnavailableReason { get; }
    int? UserIndex { get; }

    void Connect();
    void Press(GamepadButton button, int holdMs, CancellationToken cancellationToken);
    void Move(GamepadDirection direction, int holdMs, CancellationToken cancellationToken);
    void Look(double x, double y, int holdMs, CancellationToken cancellationToken);
    void Trigger(GamepadTrigger side, byte value, int holdMs, CancellationToken cancellationToken);
    void ReleaseAll();
    void Disconnect();
}

/// <summary>Factory seam that lets protocol tests run without Windows or a virtual driver.</summary>
public interface IGamepadControllerFactory
{
    IGamepadController Create();
}

/// <summary>Bounded gamepad request helpers shared by RPC, CLI and tests.</summary>
public static class GamepadSafety
{
    /// <summary>Maximum duration for one AI-issued physical input.</summary>
    public const int MaxHoldMs = 5_000;

    /// <summary>Neutral stick magnitude used for cardinal movement commands.</summary>
    public const short MovementMagnitude = 24_000;

    public static int ValidateHoldMs(int? holdMs)
    {
        if (holdMs is null or < 1 or > MaxHoldMs)
        {
            throw new CaptureException(
                GamepadErrorCodes.DurationExceeded,
                $"hold_ms must be between 1 and {MaxHoldMs}");
        }

        return holdMs.Value;
    }

    /// <summary>Validate a trigger pressure. 0 is released, 255 is fully pressed.</summary>
    public static byte ValidateTriggerValue(int? value)
    {
        if (value is null or < 0 or > 255)
        {
            throw new CaptureException(
                GamepadErrorCodes.InvalidTriggerValue,
                "value must be between 0 and 255");
        }

        return (byte)value.Value;
    }

    public static short NormalizeAxis(double value, string field)
    {
        if (double.IsNaN(value) || double.IsInfinity(value) || value is < -1 or > 1)
        {
            throw CaptureException.InvalidParams($"{field} must be a number between -1 and 1");
        }

        return (short)Math.Round(value * short.MaxValue, MidpointRounding.AwayFromZero);
    }
}

/// <summary>Read-only virtual-controller status returned by <c>gamepad.get_state</c>.</summary>
public sealed record GamepadState
{
    public required bool Available { get; init; }
    public required bool Connected { get; init; }
    public string? UnavailableReason { get; init; }
    public int? UserIndex { get; init; }
    public int MaxHoldMs { get; init; } = GamepadSafety.MaxHoldMs;
}
