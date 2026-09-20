using Nefarius.ViGEm.Client;
using Nefarius.ViGEm.Client.Targets;
using Nefarius.ViGEm.Client.Targets.Xbox360;
using Nefarius.ViGEm.Client.Targets.Xbox360.Exceptions;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>Creates the process-owned ViGEm Xbox 360 controller on demand.</summary>
public sealed class ViGEmGamepadControllerFactory : IGamepadControllerFactory
{
    public IGamepadController Create() => new ViGEmGamepadController();
}

/// <summary>
/// A bounded Xbox 360 virtual controller backed by ViGEmBus. Calls are serialized so a release can
/// never race a new report; every finite action resets its control in a finally block.
/// </summary>
public sealed class ViGEmGamepadController : IGamepadController
{
    private readonly object _sync = new();
    private ViGEmClient? _client;
    private IXbox360Controller? _controller;
    private bool _disposed;

    public bool IsAvailable { get; private set; } = true;
    public bool IsConnected { get; private set; }
    public string? UnavailableReason { get; private set; }
    public int? UserIndex
    {
        get
        {
            if (!IsConnected || _controller is null) return null;
            try
            {
                return _controller.UserIndex;
            }
            catch (Xbox360UserIndexNotReportedException)
            {
                // ViGEm connects before XInput assigns a user slot. The controller is usable;
                // report the slot as pending instead of failing a successful connect response.
                return null;
            }
        }
    }

    public void Trigger(GamepadTrigger side, byte value, int holdMs, CancellationToken cancellationToken)
    {
        GamepadSafety.ValidateHoldMs(holdMs);
        lock (_sync)
        {
            var controller = RequireConnected();
            var slider = side == GamepadTrigger.Left ? Xbox360Slider.LeftTrigger : Xbox360Slider.RightTrigger;
            try
            {
                controller.SetSliderValue(slider, value);
                Wait(holdMs, cancellationToken);
            }
            finally
            {
                controller.SetSliderValue(slider, 0);
                controller.SubmitReport();
            }
        }
    }

    public void Connect()
    {
        lock (_sync)
        {
            ThrowIfDisposed();
            if (IsConnected) return;
            if (!IsAvailable)
            {
                throw new CaptureException(GamepadErrorCodes.Unavailable, "the virtual gamepad is unavailable", UnavailableReason);
            }

            try
            {
                _client ??= new ViGEmClient();
                _controller ??= _client.CreateXbox360Controller();
                _controller.AutoSubmitReport = true;
                _controller.Connect();
                _controller.ResetReport();
                IsConnected = true;
                UnavailableReason = null;
            }
            catch (Exception ex)
            {
                IsAvailable = false;
                UnavailableReason = ex.Message;
                DisposeTransport();
                throw new CaptureException(
                    GamepadErrorCodes.Unavailable,
                    "could not create a ViGEm virtual Xbox controller",
                    ex.GetType().Name,
                    ex);
            }
        }
    }

    public void Press(GamepadButton button, int holdMs, CancellationToken cancellationToken)
    {
        GamepadSafety.ValidateHoldMs(holdMs);
        lock (_sync)
        {
            var controller = RequireConnected();
            var nativeButton = ToNativeButton(button);
            try
            {
                controller.SetButtonState(nativeButton, true);
                Wait(holdMs, cancellationToken);
            }
            finally
            {
                controller.SetButtonState(nativeButton, false);
                controller.SubmitReport();
            }
        }
    }

    public void Move(GamepadDirection direction, int holdMs, CancellationToken cancellationToken)
    {
        GamepadSafety.ValidateHoldMs(holdMs);
        lock (_sync)
        {
            var controller = RequireConnected();
            var (x, y) = direction switch
            {
                GamepadDirection.Forward => ((short)0, GamepadSafety.MovementMagnitude),
                GamepadDirection.Backward => ((short)0, (short)-GamepadSafety.MovementMagnitude),
                GamepadDirection.Left => ((short)-GamepadSafety.MovementMagnitude, (short)0),
                GamepadDirection.Right => (GamepadSafety.MovementMagnitude, (short)0),
                _ => throw new CaptureException(GamepadErrorCodes.InvalidDirection, $"unsupported direction '{direction}'"),
            };

            try
            {
                controller.SetAxisValue(Xbox360Axis.LeftThumbX, x);
                controller.SetAxisValue(Xbox360Axis.LeftThumbY, y);
                Wait(holdMs, cancellationToken);
            }
            finally
            {
                controller.SetAxisValue(Xbox360Axis.LeftThumbX, 0);
                controller.SetAxisValue(Xbox360Axis.LeftThumbY, 0);
                controller.SubmitReport();
            }
        }
    }

    public void Look(double x, double y, int holdMs, CancellationToken cancellationToken)
    {
        GamepadSafety.ValidateHoldMs(holdMs);
        var normalizedX = GamepadSafety.NormalizeAxis(x, "x");
        var normalizedY = GamepadSafety.NormalizeAxis(y, "y");
        lock (_sync)
        {
            var controller = RequireConnected();
            try
            {
                controller.SetAxisValue(Xbox360Axis.RightThumbX, normalizedX);
                controller.SetAxisValue(Xbox360Axis.RightThumbY, normalizedY);
                Wait(holdMs, cancellationToken);
            }
            finally
            {
                controller.SetAxisValue(Xbox360Axis.RightThumbX, 0);
                controller.SetAxisValue(Xbox360Axis.RightThumbY, 0);
                controller.SubmitReport();
            }
        }
    }

    public void ReleaseAll()
    {
        lock (_sync)
        {
            if (!IsConnected || _controller is null) return;
            _controller.ResetReport();
            _controller.SubmitReport();
        }
    }

    public void Disconnect()
    {
        lock (_sync)
        {
            if (_controller is null) return;
            try
            {
                if (IsConnected)
                {
                    _controller.ResetReport();
                    _controller.SubmitReport();
                    _controller.Disconnect();
                }
            }
            finally
            {
                IsConnected = false;
                DisposeTransport();
            }
        }
    }

    public void Dispose()
    {
        lock (_sync)
        {
            if (_disposed) return;
            _disposed = true;
            Disconnect();
        }
    }

    private IXbox360Controller RequireConnected()
    {
        Connect();
        return _controller ?? throw new CaptureException(GamepadErrorCodes.NotConnected, "virtual gamepad is not connected");
    }

    private static void Wait(int holdMs, CancellationToken cancellationToken)
        => Task.Delay(holdMs, cancellationToken).GetAwaiter().GetResult();

    private static Xbox360Button ToNativeButton(GamepadButton button) => button switch
    {
        GamepadButton.A => Xbox360Button.A,
        GamepadButton.B => Xbox360Button.B,
        GamepadButton.X => Xbox360Button.X,
        GamepadButton.Y => Xbox360Button.Y,
        GamepadButton.DpadUp => Xbox360Button.Up,
        GamepadButton.DpadDown => Xbox360Button.Down,
        GamepadButton.DpadLeft => Xbox360Button.Left,
        GamepadButton.DpadRight => Xbox360Button.Right,
        GamepadButton.LeftBumper => Xbox360Button.LeftShoulder,
        GamepadButton.RightBumper => Xbox360Button.RightShoulder,
        GamepadButton.LeftStick => Xbox360Button.LeftThumb,
        GamepadButton.RightStick => Xbox360Button.RightThumb,
        GamepadButton.Menu => Xbox360Button.Start,
        GamepadButton.View => Xbox360Button.Back,
        _ => throw new CaptureException(GamepadErrorCodes.InvalidButton, $"unsupported button '{button}'"),
    };

    private void DisposeTransport()
    {
        _controller = null;
        try { _client?.Dispose(); } catch { }
        _client = null;
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(ViGEmGamepadController));
        }
    }
}
