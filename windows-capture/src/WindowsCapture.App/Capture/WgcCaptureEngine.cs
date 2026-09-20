using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Windows.Graphics;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using WindowsCapture.Core;
using WinRT;

namespace WindowsCapture.App;

/// <summary>
/// Real Windows Graphics Capture backend.
///
/// A <see cref="GraphicsCaptureItem"/> is created directly for the target HWND (never a desktop
/// region grab, never PrintWindow), driven by a free-threaded
/// <see cref="Direct3D11CaptureFramePool"/> on a D3D11 device. Because WGC taps the window's own
/// composition surface, the captured pixels are the window's own content even while it is fully
/// covered by another window.
///
/// The WGC session and all GPU resources are cached per target and reused across frames and jobs.
/// </summary>
public sealed class WgcCaptureEngine : ICaptureEngine
{
    private readonly object _sync = new();
    private Session? _session;
    private bool _disposed;

    public CapturedFrame Capture(TargetRef target, CapturePlan plan, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);

        Session session;
        lock (_sync)
        {
            if (_session is null || !_session.Matches(target))
            {
                _session?.Dispose();
                _session = new Session(target);
            }
            session = _session;
        }

        try
        {
            return session.Grab(plan, cancellationToken);
        }
        catch (CaptureException ex) when (ex.ErrorCode == ErrorCodes.TargetClosed)
        {
            // The HWND is gone; throw the dead session away so the next job starts clean.
            Invalidate();
            throw;
        }
    }

    public void Invalidate()
    {
        lock (_sync)
        {
            _session?.Dispose();
            _session = null;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        Invalidate();
    }

    // ------------------------------------------------------------------ session

    private sealed class Session : IDisposable
    {
        private readonly TargetRef _target;
        private readonly IntPtr _hwnd;
        private readonly ID3D11Device _d3d;
        private readonly ID3D11DeviceContext _ctx;
        private readonly IDirect3DDevice _device;
        private readonly GraphicsCaptureItem _item;
        private readonly Direct3D11CaptureFramePool _pool;
        private readonly GraphicsCaptureSession _capture;

        private readonly object _sync = new();
        private readonly object _gpuSync = new();
        private readonly ManualResetEventSlim _signal = new(false);

        private ID3D11Texture2D? _staging;
        private int _poolWidth;
        private int _poolHeight;

        private byte[]? _latestPixels;
        private int _latestWidth;
        private int _latestHeight;
        private long _latestSeq;
        private long _latestAtMs;
        private long _consumedSeq;

        private long _encodedSeq = -1;
        private int _encodedMaxEdge = -1;
        private CapturedFrame? _encoded;

        private bool _closed;
        private Exception? _error;
        private bool _disposed;

        public Session(TargetRef target)
        {
            _target = target;
            _hwnd = new IntPtr(target.Hwnd);

            if (!Win32.IsWindow(_hwnd))
                throw new CaptureException(ErrorCodes.TargetNotFound, "the target window does not exist", target.ToString());

            var result = D3D11.D3D11CreateDevice(
                null,
                DriverType.Hardware,
                DeviceCreationFlags.BgraSupport,
                new[] { FeatureLevel.Level_11_1, FeatureLevel.Level_11_0 },
                out var d3d);

            if (result.Failure || d3d is null)
                throw new CaptureException(ErrorCodes.CaptureFailed, "could not create a Direct3D 11 device", result.ToString());

            _d3d = d3d;
            _ctx = _d3d.ImmediateContext;

            using (var dxgiDevice = _d3d.QueryInterface<IDXGIDevice>())
            {
                var hr = Win32.CreateDirect3D11DeviceFromDXGIDevice(dxgiDevice.NativePointer, out var devicePtr);
                if (hr != 0 || devicePtr == IntPtr.Zero)
                    throw new CaptureException(ErrorCodes.CaptureFailed, "could not create the WinRT Direct3D device", $"hr=0x{hr:X8}");
                _device = MarshalInterface<IDirect3DDevice>.FromAbi(devicePtr);
            }

            try
            {
                _item = CreateItem(_hwnd);
            }
            catch (CaptureException)
            {
                _ctx.Dispose();
                _d3d.Dispose();
                throw;
            }

            _poolWidth = _item.Size.Width;
            _poolHeight = _item.Size.Height;

            _pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
                _device, DirectXPixelFormat.B8G8R8A8UIntNormalized, 2, _item.Size);
            _pool.FrameArrived += OnFrameArrived;
            _item.Closed += OnItemClosed;

            _capture = _pool.CreateCaptureSession(_item);
            if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 19041))
            {
                try { _capture.IsCursorCaptureEnabled = false; }
                catch (Exception) { /* cursor flag is cosmetic; never fatal */ }
            }

            _capture.StartCapture();
        }

        public bool Matches(TargetRef target)
        {
            if (_closed) return false;
            return _target.Hwnd == target.Hwnd
                && _target.Pid == target.Pid
                && string.Equals(_target.ProcessStartUtc, target.ProcessStartUtc, StringComparison.Ordinal);
        }

        public CapturedFrame Grab(CapturePlan plan, CancellationToken cancellationToken)
        {
            CheckTargetAlive();

            var deadline = Environment.TickCount64 + plan.FrameTimeoutMs;
            bool fresh;

            while (true)
            {
                lock (_sync)
                {
                    if (_error is not null)
                    {
                        throw new CaptureException(ErrorCodes.CaptureFailed, "the capture session failed", _error.Message);
                    }

                    if (_latestPixels is not null && _latestSeq > _consumedSeq)
                    {
                        _consumedSeq = _latestSeq;
                        fresh = true;
                        break;
                    }
                }

                if (Environment.TickCount64 >= deadline)
                {
                    lock (_sync)
                    {
                        if (_latestPixels is null)
                        {
                            throw new CaptureException(
                                ErrorCodes.NoFrame,
                                "the target window produced no frame",
                                $"waited {plan.FrameTimeoutMs} ms; the window may be minimized, fully occluded by a " +
                                "different top-level window, or not composing");
                        }
                    }

                    fresh = false;
                    break;
                }

                cancellationToken.ThrowIfCancellationRequested();
                _signal.Wait(25);
                _signal.Reset();
            }

            CheckTargetAlive();

            var frame = Encode(plan.MaxEdge);
            return frame with { Source = fresh ? FrameSources.Fresh : FrameSources.Reused };
        }

        private void CheckTargetAlive()
        {
            lock (_sync)
            {
                if (_closed)
                    throw new CaptureException(ErrorCodes.TargetClosed, "the target window was closed", _target.ToString());
            }

            if (!Win32.IsWindow(_hwnd))
                throw new CaptureException(ErrorCodes.TargetClosed, "the target window no longer exists", _target.ToString());

            if (Win32.IsIconic(_hwnd))
                throw new CaptureException(
                    ErrorCodes.TargetMinimized,
                    "the target window is minimized",
                    "Windows Graphics Capture produces no frames for a minimized window; restore it and retry");
        }

        /// <summary>Encodes the newest frame, caching the result so a reused frame is not re-encoded.</summary>
        private CapturedFrame Encode(int maxEdge)
        {
            byte[] pixels;
            int width, height;
            long seq, atMs;

            lock (_sync)
            {
                if (_latestPixels is null)
                    throw new CaptureException(ErrorCodes.NoFrame, "no frame is available yet");

                if (_encoded is not null && _encodedSeq == _latestSeq && _encodedMaxEdge == maxEdge)
                    return _encoded;

                pixels = _latestPixels;
                width = _latestWidth;
                height = _latestHeight;
                seq = _latestSeq;
                atMs = _latestAtMs;
            }

            var png = ImageCodec.EncodePng(pixels, width, height, maxEdge);
            var frame = new CapturedFrame
            {
                Data = png,
                Mime = "image/png",
                Width = maxEdge > 0 && Math.Max(width, height) > maxEdge
                    ? (int)Math.Round(width * Math.Min(1.0, (double)maxEdge / Math.Max(width, height)))
                    : width,
                Height = maxEdge > 0 && Math.Max(width, height) > maxEdge
                    ? (int)Math.Round(height * Math.Min(1.0, (double)maxEdge / Math.Max(width, height)))
                    : height,
                CapturedAtUnixMs = atMs,
                Source = FrameSources.Fresh,
            };

            lock (_sync)
            {
                if (seq == _latestSeq)
                {
                    _encoded = frame;
                    _encodedSeq = seq;
                    _encodedMaxEdge = maxEdge;
                }
            }

            return frame;
        }

        private void OnItemClosed(GraphicsCaptureItem sender, object args)
        {
            lock (_sync) _closed = true;
            _signal.Set();
        }

        private void OnFrameArrived(Direct3D11CaptureFramePool sender, object args)
        {
            try
            {
                using var frame = sender.TryGetNextFrame();
                if (frame is null) return;

                var size = frame.ContentSize;
                if (size.Width <= 0 || size.Height <= 0) return;

                if (size.Width != _poolWidth || size.Height != _poolHeight)
                {
                    // The window was resized; the pool must be told about the new buffer size.
                    try
                    {
                        sender.Recreate(_device, DirectXPixelFormat.B8G8R8A8UIntNormalized, 2, size);
                        _poolWidth = size.Width;
                        _poolHeight = size.Height;
                    }
                    catch (Exception)
                    {
                        // Fall through: the copy below uses the frame's own ContentSize anyway.
                    }
                }

                var pixels = CopyToCpu(frame);
                lock (_sync)
                {
                    _latestPixels = pixels;
                    _latestWidth = size.Width;
                    _latestHeight = size.Height;
                    _latestSeq++;
                    _latestAtMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                }
                _signal.Set();
            }
            catch (Exception ex)
            {
                lock (_sync) _error = ex;
                _signal.Set();
            }
        }

        /// <summary>GPU -&gt; CPU copy of the frame's BGRA surface into a tightly packed buffer.</summary>
        private byte[] CopyToCpu(Direct3D11CaptureFrame frame)
        {
            var surface = frame.Surface;
            var surfaceObject = (IWinRTObject)surface;
            var accessPtr = Win32.QueryInterface(surfaceObject.NativeObject.ThisPtr, Win32.IidDxgiInterfaceAccess);

            try
            {
                var getInterface = Win32.VtblFn<Win32.GetInterfaceFn>(accessPtr, 3);
                var textureIid = Win32.IidD3D11Texture2D;
                Marshal.ThrowExceptionForHR(getInterface(accessPtr, ref textureIid, out var texturePtr));

                using var texture = new ID3D11Texture2D(texturePtr);
                var description = texture.Description;
                var width = (int)description.Width;
                var height = (int)description.Height;
                if (width <= 0 || height <= 0) return Array.Empty<byte>();

                lock (_gpuSync)
                {
                    if (_staging is null
                        || _staging.Description.Width != description.Width
                        || _staging.Description.Height != description.Height)
                    {
                        _staging?.Dispose();
                        _staging = _d3d.CreateTexture2D(new Texture2DDescription
                        {
                            Width = description.Width,
                            Height = description.Height,
                            MipLevels = 1,
                            ArraySize = 1,
                            Format = Format.B8G8R8A8_UNorm,
                            SampleDescription = new SampleDescription(1, 0),
                            Usage = ResourceUsage.Staging,
                            BindFlags = BindFlags.None,
                            CPUAccessFlags = CpuAccessFlags.Read,
                            MiscFlags = ResourceOptionFlags.None,
                        });
                    }

                    _ctx.CopyResource(_staging, texture);
                    var mapped = _ctx.Map(_staging, 0, MapMode.Read, Vortice.Direct3D11.MapFlags.None);
                    try
                    {
                        var buffer = new byte[width * height * 4];
                        var rowBytes = width * 4;
                        for (var y = 0; y < height; y++)
                        {
                            Marshal.Copy(IntPtr.Add(mapped.DataPointer, y * (int)mapped.RowPitch), buffer, y * rowBytes, rowBytes);
                        }
                        return buffer;
                    }
                    finally
                    {
                        _ctx.Unmap(_staging, 0);
                    }
                }
            }
            finally
            {
                Marshal.Release(accessPtr);
            }
        }

        private static GraphicsCaptureItem CreateItem(IntPtr hwnd)
        {
            IObjectReference factory;
            try
            {
                factory = WinRT.ActivationFactory.Get("Windows.Graphics.Capture.GraphicsCaptureItem");
            }
            catch (Exception ex)
            {
                throw new CaptureException(
                    ErrorCodes.CaptureFailed,
                    "Windows Graphics Capture is not available on this system",
                    $"Windows 10 1903 (build 18362) or newer is required: {ex.Message}");
            }

            var interopPtr = Win32.QueryInterface(factory.ThisPtr, Win32.IidGraphicsCaptureItemInterop);
            try
            {
                var interop = (Win32.IGraphicsCaptureItemInterop)Marshal.GetObjectForIUnknown(interopPtr);
                var iid = Win32.IidGraphicsCaptureItem;
                var itemPtr = interop.CreateForWindow(hwnd, ref iid);
                if (itemPtr == IntPtr.Zero)
                {
                    throw new CaptureException(
                        ErrorCodes.TargetNotFound,
                        "Windows Graphics Capture refused to create an item for this window",
                        "protected or elevated windows cannot be captured without elevation");
                }
                return MarshalInspectable<GraphicsCaptureItem>.FromAbi(itemPtr);
            }
            finally
            {
                Marshal.Release(interopPtr);
            }
        }

        public void Dispose()
        {
            if (_disposed) return;
            _disposed = true;

            try { _capture.Dispose(); } catch (Exception) { }
            try { _pool.Dispose(); } catch (Exception) { }
            try { _item.Closed -= OnItemClosed; } catch (Exception) { }
            try { _staging?.Dispose(); } catch (Exception) { }
            try { _ctx.Dispose(); } catch (Exception) { }
            try { _d3d.Dispose(); } catch (Exception) { }
            _signal.Dispose();
        }
    }
}
