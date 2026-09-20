using System.Diagnostics;
using System.Globalization;
using System.Text;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>Enumerates the top-level windows a user could reasonably want to capture.</summary>
public static class WindowEnumerator
{
    public static IReadOnlyList<WindowInfo> List(TargetRef? selected = null)
    {
        var foreground = Win32.GetForegroundWindow();
        var result = new List<WindowInfo>();

        Win32.EnumWindows((hwnd, _) =>
        {
            try
            {
                if (!IsCapturable(hwnd)) return true;
                result.Add(Describe(hwnd, foreground, selected));
            }
            catch
            {
                // A window can disappear mid-enumeration; skip it rather than aborting the list.
            }
            return true;
        }, IntPtr.Zero);

        return result
            .OrderByDescending(static w => w.IsForeground)
            .ThenByDescending(static w => w.IsSelected)
            .ThenBy(static w => w.Process ?? "", StringComparer.OrdinalIgnoreCase)
            .ThenBy(static w => w.Title, StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    public static WindowInfo? Describe(IntPtr hwnd, TargetRef? selected = null)
    {
        if (hwnd == IntPtr.Zero || !Win32.IsWindow(hwnd)) return null;
        return Describe(hwnd, Win32.GetForegroundWindow(), selected);
    }

    private static bool IsCapturable(IntPtr hwnd)
    {
        if (!Win32.IsWindowVisible(hwnd)) return false;
        if (Win32.GetWindowTextLengthW(hwnd) <= 0) return false;
        if (Win32.GetWindow(hwnd, Win32.GW_OWNER) != IntPtr.Zero) return false;

        var style = Win32.GetWindowLongW(hwnd, Win32.GWL_STYLE);
        if ((style & unchecked((int)Win32.WS_CHILD)) != 0) return false;

        var exStyle = Win32.GetWindowLongW(hwnd, Win32.GWL_EXSTYLE);
        if ((exStyle & unchecked((int)Win32.WS_EX_TOOLWINDOW)) != 0) return false;

        // UWP/immersive windows are "cloaked" when they are not actually on screen.
        if (Win32.DwmGetWindowAttribute(hwnd, Win32.DWMWA_CLOAKED, out var cloaked, sizeof(int)) == 0 && cloaked != 0)
            return false;

        return true;
    }

    private static WindowInfo Describe(IntPtr hwnd, IntPtr foreground, TargetRef? selected)
    {
        var titleLength = Math.Max(1, Win32.GetWindowTextLengthW(hwnd) + 1);
        var title = new StringBuilder(titleLength);
        Win32.GetWindowTextW(hwnd, title, title.Capacity);

        var className = new StringBuilder(256);
        Win32.GetClassNameW(hwnd, className, className.Capacity);

        Win32.GetWindowThreadProcessId(hwnd, out var pid);
        var processId = (int)pid;

        string? processName = null;
        string? startUtc = null;
        try
        {
            using var process = Process.GetProcessById(processId);
            processName = process.ProcessName;
            startUtc = process.StartTime.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture);
        }
        catch
        {
            // Access denied or the process exited; the window is still capturable by HWND.
        }

        var hwndValue = hwnd.ToInt64();
        return new WindowInfo
        {
            Hwnd = "0x" + hwndValue.ToString("X", CultureInfo.InvariantCulture),
            HwndValue = hwndValue,
            Pid = processId,
            Title = title.ToString(),
            Process = processName,
            ProcessStartUtc = startUtc,
            ClassName = className.ToString(),
            Visible = Win32.IsWindowVisible(hwnd),
            Minimized = Win32.IsIconic(hwnd),
            IsForeground = hwnd == foreground,
            IsSelected = selected is not null && selected.Hwnd == hwndValue,
        };
    }
}

/// <summary>Reads a process start time; used by the target-identity re-validation.</summary>
public sealed class Win32ProcessInfo : IProcessInfo
{
    public static readonly Win32ProcessInfo Instance = new();

    public bool TryGetStartUtc(int pid, out DateTime startUtc)
    {
        startUtc = default;
        if (pid <= 0) return false;
        try
        {
            using var process = Process.GetProcessById(pid);
            startUtc = process.StartTime.ToUniversalTime();
            return true;
        }
        catch
        {
            return false;
        }
    }
}
