using System.ComponentModel;
using System.Globalization;
using System.Windows;
using System.Windows.Threading;
using WindowsCapture.Core;

namespace WindowsCapture.App;

public partial class MainWindow : Window
{
    private readonly HostService _host;
    private readonly DispatcherTimer _timer;
    private WindowInfo? _selected;

    /// <summary>Set by <see cref="App"/> when a real shutdown is in progress.</summary>
    public bool AllowClose { get; set; }

    public MainWindow(HostService host)
    {
        _host = host;
        InitializeComponent();

        var config = _host.Config;
        CountBox.Text = (config.Count ?? CaptureDefaults.Count).ToString(CultureInfo.InvariantCulture);
        IntervalBox.Text = (config.IntervalMs ?? CaptureDefaults.IntervalMs).ToString(CultureInfo.InvariantCulture);
        StartDelayBox.Text = (config.StartDelayMs ?? CaptureDefaults.StartDelayMs).ToString(CultureInfo.InvariantCulture);
        MaxEdgeBox.Text = (config.MaxEdge ?? CaptureDefaults.MaxEdge).ToString(CultureInfo.InvariantCulture);
        OutputDirBox.Text = config.OutputDir ?? "";

        _timer = new DispatcherTimer(TimeSpan.FromMilliseconds(400), DispatcherPriority.Background, OnTick, Dispatcher);
        _timer.Start();

        Loaded += (_, _) => RefreshWindows();
        UpdateSelectedTargetText();
    }

    // ---------------------------------------------------------------- UI plumbing

    public void SetStatus(string message) => StatusText.Text = message;

    public void RefreshWindows()
    {
        _selected = null;
        WindowList.ItemsSource = _host.ListWindows();
        UpdateSelectedTargetText();
    }

    public void RefreshJobs()
    {
        var active = _host.Jobs.ActiveJobId;
        if (active is null) return;

        try
        {
            var job = _host.Jobs.GetJob(active);
            JobText.Text = $"job {job.JobId}: {job.State} {job.Captured}/{job.Requested} frames, " +
                           $"{job.ResultBytes} bytes, expires at {job.ExpiresAt}";
        }
        catch (CaptureException)
        {
            JobText.Text = "";
        }
    }

    private void OnTick(object? sender, EventArgs e)
    {
        try
        {
            var jobs = _host.Jobs.ListJobs();
            var active = jobs.FirstOrDefault(static j => j.State is "queued" or "running");
            if (active is not null)
            {
                JobText.Text = $"job {active.JobId}: {active.State} {active.Captured}/{active.Requested} frames, " +
                               $"{active.ResultBytes} bytes";
                return;
            }

            var last = jobs.FirstOrDefault();
            JobText.Text = last is null
                ? "no jobs yet"
                : $"last job {last.JobId}: {last.State}, {last.Captured}/{last.Requested} frames, " +
                  $"{last.ResultBytes} bytes, expires at {last.ExpiresAt}";

            if (last is { State: "failed", Error: not null })
                JobText.Text += $" ({last.Error.Code}: {last.Error.Message})";
        }
        catch (Exception)
        {
            // Status display must never take the UI down.
        }
    }

    private void OnRefreshClick(object sender, RoutedEventArgs e) => RefreshWindows();

    private void OnWindowSelectionChanged(object sender, RoutedEventArgs e)
    {
        _selected = WindowList.SelectedItem as WindowInfo;
        if (_selected is null) return;

        _host.SetSelectedTarget(new TargetRef
        {
            Hwnd = _selected.HwndValue,
            Pid = _selected.Pid,
            ProcessStartUtc = _selected.ProcessStartUtc,
            Title = _selected.Title,
        });
        UpdateSelectedTargetText();
    }

    private void UpdateSelectedTargetText()
    {
        var target = _host.SelectedTarget;
        SelectedTargetText.Text = target is null
            ? "none selected - pick a row below"
            : $"{target.HwndHex}  pid {target.Pid}  \"{target.Title}\"";
    }

    private void OnStartClick(object sender, RoutedEventArgs e)
    {
        var target = _host.SelectedTarget;
        if (target is null)
        {
            MessageBox.Show(this, "Select a target window first.", "windows-capture",
                MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }

        var request = new CaptureRequest
        {
            Target = target,
            Count = ParseInt(CountBox.Text, "count"),
            IntervalMs = ParseInt(IntervalBox.Text, "interval_ms"),
            StartDelayMs = ParseInt(StartDelayBox.Text, "start_delay_ms"),
            MaxEdge = ParseInt(MaxEdgeBox.Text, "max_edge"),
            OutputDir = string.IsNullOrWhiteSpace(OutputDirBox.Text) ? null : OutputDirBox.Text.Trim(),
        };

        try
        {
            var job = _host.Jobs.Start(request, _host.Config);
            SetStatus($"started {job.JobId} for {job.Target}");
        }
        catch (CaptureException ex)
        {
            MessageBox.Show(this, $"{ex.ErrorCode}: {ex.Message}", "windows-capture",
                MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private static int? ParseInt(string text, string field)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        if (!int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value))
            throw new CaptureException(ErrorCodes.InvalidParams, $"'{field}' must be an integer");
        return value;
    }

    private void OnCancelClick(object sender, RoutedEventArgs e)
    {
        var active = _host.Jobs.ActiveJobId;
        if (active is null)
        {
            SetStatus("nothing to cancel");
            return;
        }

        try
        {
            var job = _host.Jobs.Cancel(active);
            SetStatus($"job {job.JobId} is {job.State}");
        }
        catch (CaptureException ex)
        {
            SetStatus($"cancel failed: {ex.ErrorCode}: {ex.Message}");
        }
    }

    private void OnSettingsClick(object sender, RoutedEventArgs e)
    {
        var dialog = new SettingsWindow(_host) { Owner = this };
        if (dialog.ShowDialog() == true)
        {
            var config = _host.Config;
            CountBox.Text = (config.Count ?? CaptureDefaults.Count).ToString(CultureInfo.InvariantCulture);
            IntervalBox.Text = (config.IntervalMs ?? CaptureDefaults.IntervalMs).ToString(CultureInfo.InvariantCulture);
            StartDelayBox.Text = (config.StartDelayMs ?? CaptureDefaults.StartDelayMs).ToString(CultureInfo.InvariantCulture);
            MaxEdgeBox.Text = (config.MaxEdge ?? CaptureDefaults.MaxEdge).ToString(CultureInfo.InvariantCulture);
            OutputDirBox.Text = config.OutputDir ?? "";
            SetStatus("settings saved to " + _host.ConfigPath);
        }
    }

    private void OnExitClick(object sender, RoutedEventArgs e)
    {
        AllowClose = true;
        Application.Current.Shutdown();
    }

    private void OnWindowClosing(object? sender, CancelEventArgs e)
    {
        if (AllowClose) return;

        // Hide instead of exiting: the RPC server keeps serving clients.
        e.Cancel = true;
        Hide();
        SetStatus("Window hidden - the RPC server is still running. Use --show-ui (or relaunch) to " +
                  "bring this window back, and --exit to release everything.");
    }
}
