using System.IO;
using System.Windows;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>
/// The WPF application object. It owns the <see cref="HostService"/> (single job service shared by
/// the GUI and the RPC server) and implements <see cref="IHostUi"/> so RPC calls can drive the UI
/// from any thread.
///
/// Closing the window hides it and keeps the RPC server alive; only an explicit exit releases
/// everything.
/// </summary>
public sealed class App : Application, IHostUi
{
    private readonly CliOptions _options;
    private HostService? _host;
    private MainWindow? _window;

    public App(CliOptions options) => _options = options;

    public HostService Host => _host!;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        var pipeName = _options.PipeName ?? PipeClient.DefaultPipeName();
        var configPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "WindowsCapture",
            "config.json");

        _host = new HostService(this, pipeName, configPath);
        _host.AdoptPersistedTarget();
        _host.StartServer();

        _window = new MainWindow(_host);
        MainWindow = _window;

        if (!_options.Hidden)
        {
            _window.Show();
            _window.Activate();
        }

        _host.SetStatus(_options.Hidden
            ? $"Running hidden. RPC pipe: {pipeName}"
            : $"RPC pipe: {pipeName}");
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try { _host?.Dispose(); } catch (Exception) { }
        base.OnExit(e);
    }

    // ---------------------------------------------------------------- IHostUi

    public bool IsVisible
    {
        get
        {
            var window = _window;
            if (window is null || Dispatcher.HasShutdownStarted) return false;
            return Dispatcher.Invoke(() => window.IsVisible);
        }
    }

    public void ShowWindow()
    {
        if (Dispatcher.HasShutdownStarted) return;
        Dispatcher.Invoke(() =>
        {
            if (_window is null) return;
            if (!_window.IsVisible) _window.Show();
            if (_window.WindowState == WindowState.Minimized) _window.WindowState = WindowState.Normal;
            _window.Activate();
        });
    }

    public void RequestShutdown()
    {
        if (Dispatcher.HasShutdownStarted) return;
        Dispatcher.InvokeAsync(() =>
        {
            if (_window is not null) _window.AllowClose = true;
            Shutdown();
        });
    }

    public void SetStatus(string message)
    {
        if (Dispatcher.HasShutdownStarted) return;
        Dispatcher.InvokeAsync(() => _window?.SetStatus(message));
    }

    public void RefreshWindows()
    {
        if (Dispatcher.HasShutdownStarted) return;
        Dispatcher.InvokeAsync(() => _window?.RefreshWindows());
    }

    public void NotifyJobsChanged()
    {
        if (Dispatcher.HasShutdownStarted) return;
        Dispatcher.InvokeAsync(() => _window?.RefreshJobs());
    }
}
