using System.Runtime.InteropServices;
using System.Text;

namespace WindowsCapture.App;

/// <summary>Raw Win32 / COM declarations used by the enumerator and the capture engine.</summary>
internal static class Win32
{
    // ---------------------------------------------------------------- window enumeration

    internal delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    internal static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern int GetWindowTextLengthW(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    internal static extern int GetClassNameW(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll")]
    internal static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    internal static extern bool IsWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    internal static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    internal static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    internal static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll", SetLastError = true)]
    internal static extern int GetWindowLongW(IntPtr hWnd, int nIndex);

    [DllImport("dwmapi.dll")]
    internal static extern int DwmGetWindowAttribute(IntPtr hwnd, int dwAttribute, out int pvAttribute, int cbAttribute);

    internal const uint GW_OWNER = 4;
    internal const int GWL_STYLE = -16;
    internal const int GWL_EXSTYLE = -20;
    internal const long WS_CHILD = 0x40000000L;
    internal const long WS_EX_TOOLWINDOW = 0x00000080L;
    internal const int DWMWA_CLOAKED = 14;

    // ---------------------------------------------------------------- graphics interop

    [DllImport("d3d11.dll", ExactSpelling = true)]
    internal static extern int CreateDirect3D11DeviceFromDXGIDevice(IntPtr dxgiDevice, out IntPtr graphicsDevice);

    /// <summary>
    /// <c>IGraphicsCaptureItemInterop</c> (3628E81B-...). Only ever obtained from the
    /// <c>GraphicsCaptureItem</c> activation factory via QueryInterface, so it is a plain COM
    /// interface and can be used through the built-in COM interop.
    /// </summary>
    [ComImport, Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IGraphicsCaptureItemInterop
    {
        IntPtr CreateForWindow([In] IntPtr window, [In] ref Guid iid);
        IntPtr CreateForMonitor([In] IntPtr monitor, [In] ref Guid iid);
    }

    /// <summary><c>IDirect3DDxgiInterfaceAccess</c> (A9B3D012-...) implemented by WinRT surfaces.</summary>
    [ComImport, Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IDirect3DDxgiInterfaceAccess
    {
        IntPtr GetInterface([In] ref Guid iid);
    }

    internal static readonly Guid IidGraphicsCaptureItemInterop = new("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356");

    /// <summary><c>IGraphicsCaptureItem</c> - the interface returned by CreateForWindow.</summary>
    internal static readonly Guid IidGraphicsCaptureItem = new("79C3F95B-31F7-4EC2-A464-632EF5D30760");

    internal static readonly Guid IidDxgiInterfaceAccess = new("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1");
    internal static readonly Guid IidD3D11Texture2D = new("6F15AAF2-D208-4E89-9AB4-489535D34F9C");

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    internal delegate int GetInterfaceFn(IntPtr thisPtr, ref Guid iid, out IntPtr result);

    internal static IntPtr QueryInterface(IntPtr p, Guid iid)
    {
        Marshal.ThrowExceptionForHR(Marshal.QueryInterface(p, ref iid, out var result));
        return result;
    }

    internal static TDelegate VtblFn<TDelegate>(IntPtr p, int slot) where TDelegate : Delegate
        => Marshal.GetDelegateForFunctionPointer<TDelegate>(
            Marshal.ReadIntPtr(Marshal.ReadIntPtr(p), slot * IntPtr.Size));

    // ---------------------------------------------------------------- console

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AttachConsole(int dwProcessId);

    /// <summary>
    /// A WinExe has no console. Attaching to the parent console makes <c>--help</c>/<c>--rpc</c>
    /// output visible in a terminal while leaving piped/redirected stdout untouched.
    /// </summary>
    internal static void AttachParentConsole()
    {
        try { AttachConsole(-1); }
        catch { /* no parent console: piped stdout still works */ }
    }
}
