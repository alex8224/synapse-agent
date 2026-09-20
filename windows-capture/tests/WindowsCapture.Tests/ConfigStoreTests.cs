using System.Text.Json;
using WindowsCapture.Core;
using Xunit;

namespace WindowsCapture.Tests;

public class ConfigStoreTests : IDisposable
{
    private readonly string _directory = Path.Combine(Path.GetTempPath(), "windows-capture-tests", Guid.NewGuid().ToString("N"));
    private string ConfigPath => System.IO.Path.Combine(_directory, "config.json");

    public void Dispose()
    {
        if (Directory.Exists(_directory)) Directory.Delete(_directory, recursive: true);
        GC.SuppressFinalize(this);
    }

    [Fact]
    public void MissingFileLoadsAsEmpty()
    {
        var store = new ConfigStore(ConfigPath);
        var config = store.Load();

        Assert.Null(config.Count);
        Assert.Null(config.LastTarget);
        Assert.Null(store.LastError);
    }

    [Fact]
    public void SavedConfigRoundTrips()
    {
        var store = new ConfigStore(ConfigPath);
        store.Save(new AppConfig
        {
            Count = 5,
            IntervalMs = 300,
            StartDelayMs = 50,
            MaxEdge = 1280,
            OutputDir = @"C:\shots",
            TtlSeconds = 120,
            FrameTimeoutMs = 2000,
            AllowReuse = false,
            LastTarget = new TargetRef { Hwnd = 0x42, Pid = 9, ProcessStartUtc = "2024-01-01T00:00:00.0000000Z", Title = "t" },
        });

        var loaded = new ConfigStore(ConfigPath).Load();

        Assert.Equal(5, loaded.Count);
        Assert.Equal(300, loaded.IntervalMs);
        Assert.Equal(50, loaded.StartDelayMs);
        Assert.Equal(1280, loaded.MaxEdge);
        Assert.Equal(@"C:\shots", loaded.OutputDir);
        Assert.Equal(120, loaded.TtlSeconds);
        Assert.Equal(2000, loaded.FrameTimeoutMs);
        Assert.False(loaded.AllowReuse);
        Assert.Equal(0x42, loaded.LastTarget!.Hwnd);
        Assert.Equal(9, loaded.LastTarget!.Pid);
    }

    [Fact]
    public void CorruptFileDegradesToEmptyAndRecordsTheProblem()
    {
        Directory.CreateDirectory(_directory);
        File.WriteAllText(ConfigPath, "{ this is not json");

        var store = new ConfigStore(ConfigPath);
        var config = store.Load();

        Assert.Null(config.Count);
        Assert.NotNull(store.LastError);
    }

    [Fact]
    public void UnknownFieldsAreIgnored()
    {
        Directory.CreateDirectory(_directory);
        File.WriteAllText(ConfigPath, """{"count":3,"something_else":"ignored"}""");

        var loaded = new ConfigStore(ConfigPath).Load();
        Assert.Equal(3, loaded.Count);
    }

    [Fact]
    public void WriteIsAtomicAndLeavesNoTempFile()
    {
        var store = new ConfigStore(ConfigPath);
        store.Save(new AppConfig { Count = 1 });

        Assert.True(File.Exists(ConfigPath));
        Assert.False(File.Exists(ConfigPath + ".tmp"));
        Assert.Null(store.LastError);
    }

    [Fact]
    public void SavedFileIsValidJson()
    {
        var store = new ConfigStore(ConfigPath);
        store.Save(new AppConfig { Count = 2, LastTarget = new TargetRef { Hwnd = 1, Pid = 2 } });

        using var document = JsonDocument.Parse(File.ReadAllText(ConfigPath));
        Assert.Equal(2, document.RootElement.GetProperty("count").GetInt32());
    }
}
