using System.Text.Json;

namespace WindowsCapture.Core;

/// <summary>
/// Loads/saves <see cref="AppConfig"/> as JSON. A missing or corrupt file degrades to
/// <see cref="AppConfig.Empty"/> rather than failing the process, and writes are atomic
/// (temp file + replace) so a crash cannot leave a half-written config behind.
/// </summary>
public sealed class ConfigStore
{
    private readonly object _sync = new();
    private readonly string _path;

    public ConfigStore(string path) => _path = path;

    public string Path => _path;

    /// <summary>Last load/save problem, surfaced in <c>get_state</c> for troubleshooting.</summary>
    public string? LastError { get; private set; }

    public AppConfig Load()
    {
        lock (_sync)
        {
            try
            {
                if (!File.Exists(_path))
                {
                    LastError = null;
                    return AppConfig.Empty;
                }

                var json = File.ReadAllText(_path);
                if (string.IsNullOrWhiteSpace(json))
                {
                    LastError = null;
                    return AppConfig.Empty;
                }

                var config = JsonSerializer.Deserialize<AppConfig>(json, Json.Options);
                LastError = null;
                return config ?? AppConfig.Empty;
            }
            catch (Exception ex)
            {
                // A corrupt config must never stop the tool from running.
                LastError = $"could not read {_path}: {ex.Message}";
                return AppConfig.Empty;
            }
        }
    }

    public void Save(AppConfig config)
    {
        lock (_sync)
        {
            try
            {
                var dir = System.IO.Path.GetDirectoryName(_path);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

                var json = JsonSerializer.Serialize(config, Json.IndentedOptions);
                var temp = _path + ".tmp";
                File.WriteAllText(temp, json);
                File.Move(temp, _path, overwrite: true);
                LastError = null;
            }
            catch (Exception ex)
            {
                LastError = $"could not write {_path}: {ex.Message}";
            }
        }
    }
}
