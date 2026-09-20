using System.Reflection;
using WindowsCapture.Core;

namespace WindowsCapture.App;

/// <summary>UI actions the RPC layer may need to trigger. Implemented by the WPF <see cref="App"/>.</summary>
public interface IHostUi
{
    bool IsVisible { get; }
    void ShowWindow();
    void RequestShutdown();
    void SetStatus(string message);
    void RefreshWindows();
    void NotifyJobsChanged();
}

/// <summary>
/// Owns everything the process needs to serve both the GUI and the RPC clients: the config file,
/// the single <see cref="JobService"/> (so only one capture can run at a time), the real WGC engine
/// and the named pipe server. Implements <see cref="IRpcHost"/> so the protocol layer stays
/// platform independent.
/// </summary>
public sealed class HostService : IRpcHost, IDisposable
{
    private readonly IHostUi _ui;
    private readonly ConfigStore _configStore;
    private readonly WgcCaptureEngine _engine;
    private readonly IGamepadController _gamepad;
    private readonly JobService _jobs;
    private readonly RpcDispatcher _dispatcher;
    private readonly object _sync = new();

    private AppConfig _config;
    private TargetRef? _selected;
    private RpcServer? _server;
    private bool _disposed;

    public HostService(IHostUi ui, string pipeName, string configPath, IGamepadControllerFactory? gamepadFactory = null)
    {
        _ui = ui;
        PipeName = pipeName;
        ConfigPath = configPath;
        _configStore = new ConfigStore(configPath);
        _config = _configStore.Load();

        _engine = new WgcCaptureEngine();
        Limits = new Limits();
        _jobs = new JobService(_engine, Limits, SystemClock.Instance, Win32ProcessInfo.Instance);
        _gamepad = (gamepadFactory ?? new ViGEmGamepadControllerFactory()).Create();
        _dispatcher = new RpcDispatcher(this);

        Version = typeof(HostService).Assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
            ?? typeof(HostService).Assembly.GetName().Version?.ToString()
            ?? "0.0.0";
    }

    public string Version { get; }
    public string PipeName { get; }
    public int ProcessId => Environment.ProcessId;
    public string? ConfigPath { get; }
    public string? ConfigError => _configStore.LastError;
    public Limits Limits { get; }
    public JobService Jobs => _jobs;
    public IGamepadController Gamepad => _gamepad;
    public long NowMs => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    public bool UiVisible => _ui.IsVisible;
    public void ShowUi() => _ui.ShowWindow();
    public void RequestShutdown() => _ui.RequestShutdown();
    public void SetStatus(string message) => _ui.SetStatus(message);
    public void RefreshWindows() => _ui.RefreshWindows();
    public void NotifyJobsChanged() => _ui.NotifyJobsChanged();

    public AppConfig Config
    {
        get { lock (_sync) return _config; }
    }

    public TargetRef? SelectedTarget
    {
        get { lock (_sync) return _selected; }
    }

    public void SetSelectedTarget(TargetRef? target)
    {
        lock (_sync)
        {
            _selected = target;
            if (target is not null) _config = _config with { LastTarget = target };
        }

        _ui.RefreshWindows();
    }

    public void SaveConfig(AppConfig config)
    {
        lock (_sync) _config = config;
        _configStore.Save(config);
    }

    /// <summary>Loads the persisted "last target" so the GUI starts with something selected.</summary>
    public void AdoptPersistedTarget()
    {
        lock (_sync) _selected = _config.LastTarget;
    }

    public IReadOnlyList<WindowInfo> ListWindows() => WindowEnumerator.List(SelectedTarget);

    public Task<CliInvocationResult> InvokeCliAsync(IReadOnlyList<string> args, CancellationToken cancellationToken)
        => CliRunner.ExecuteAsync(args, this, cancellationToken);

    public void StartServer()
    {
        _server = new RpcServer(PipeName, _dispatcher, Limits);
        _server.Start();
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _server?.Dispose();
        _gamepad.Dispose();
        _jobs.Dispose();
    }
}
