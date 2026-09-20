using System.Globalization;
using System.Windows;
using WindowsCapture.Core;

namespace WindowsCapture.App;

public partial class SettingsWindow : Window
{
    private readonly HostService _host;

    public SettingsWindow(HostService host)
    {
        _host = host;
        InitializeComponent();

        var config = _host.Config;
        CountBox.Text = (config.Count ?? CaptureDefaults.Count).ToString(CultureInfo.InvariantCulture);
        IntervalBox.Text = (config.IntervalMs ?? CaptureDefaults.IntervalMs).ToString(CultureInfo.InvariantCulture);
        StartDelayBox.Text = (config.StartDelayMs ?? CaptureDefaults.StartDelayMs).ToString(CultureInfo.InvariantCulture);
        MaxEdgeBox.Text = (config.MaxEdge ?? CaptureDefaults.MaxEdge).ToString(CultureInfo.InvariantCulture);
        TtlBox.Text = (config.TtlSeconds ?? _host.Limits.DefaultTtlSeconds).ToString(CultureInfo.InvariantCulture);
        FrameTimeoutBox.Text = (config.FrameTimeoutMs ?? _host.Limits.DefaultFrameTimeoutMs).ToString(CultureInfo.InvariantCulture);
        OutputDirBox.Text = config.OutputDir ?? "";
        AllowReuseBox.IsChecked = config.AllowReuse ?? CaptureDefaults.AllowReuse;

        var limits = _host.Limits;
        RuntimeText.Text =
            $"Config file : {_host.ConfigPath}\n" +
            $"RPC pipe    : {_host.PipeName} (named pipe, current user only)\n" +
            $"Limits      : count <= {limits.MaxFramesPerJob}, interval <= {limits.MaxIntervalMs} ms, " +
            $"max_edge <= {limits.MaxEdge}, ttl <= {limits.MaxTtlSeconds} s, " +
            $"frame timeout <= {limits.MaxFrameTimeoutMs} ms, chunk <= {limits.MaxChunkBytes} B, " +
            $"jobs <= {limits.MaxJobs}, result <= {limits.MaxJobResultBytes} B";
    }

    private void OnSaveClick(object sender, RoutedEventArgs e)
    {
        try
        {
            var limits = _host.Limits;
            var config = _host.Config with
            {
                Count = Parse(CountBox.Text, "count"),
                IntervalMs = Parse(IntervalBox.Text, "interval_ms"),
                StartDelayMs = Parse(StartDelayBox.Text, "start_delay_ms"),
                MaxEdge = Parse(MaxEdgeBox.Text, "max_edge"),
                TtlSeconds = Parse(TtlBox.Text, "ttl_seconds"),
                FrameTimeoutMs = Parse(FrameTimeoutBox.Text, "frame_timeout_ms"),
                OutputDir = string.IsNullOrWhiteSpace(OutputDirBox.Text) ? null : OutputDirBox.Text.Trim(),
                AllowReuse = AllowReuseBox.IsChecked,
            };

            // Reuse the same validation the capture path uses so the UI cannot save nonsense.
            _ = CapturePlanner.Resolve(new CaptureRequest { Target = _host.SelectedTarget ?? new TargetRef { Hwnd = 1 } }, config, limits);

            _host.SaveConfig(config);
            DialogResult = true;
            Close();
        }
        catch (CaptureException ex)
        {
            MessageBox.Show(this, ex.Message, "windows-capture", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private static int? Parse(string text, string field)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        if (!int.TryParse(text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value))
            throw new CaptureException(ErrorCodes.InvalidParams, $"'{field}' must be an integer");
        return value;
    }
}
