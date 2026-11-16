use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostMetadata {
    pub schema_version: Option<u32>,
    pub host: String,
    pub port: u16,
    pub url: String,
    pub websocket: Option<String>,
    pub project_id: Option<String>,
    pub workspace: Option<String>,
    pub pairing_required: Option<bool>,
}

#[derive(Debug)]
pub enum SpawnTarget {
    Binary { path: PathBuf, args: Vec<String> },
    Python { path: PathBuf, args: Vec<String> },
}

pub struct ProcessManager {
    child: Option<Child>,
    current_meta: Option<HostMetadata>,
    workspace: Option<PathBuf>,
    static_dir: Option<PathBuf>,
}

impl ProcessManager {
    pub fn new(workspace: Option<PathBuf>, static_dir: Option<PathBuf>) -> Self {
        Self {
            child: None,
            current_meta: None,
            workspace,
            static_dir,
        }
    }

    pub fn current_url(&self) -> Option<String> {
        self.current_meta.as_ref().map(|m| m.url.clone())
    }

    /// Start the console host, preferring the port the last launch bound.
    ///
    /// A stable port means a stable origin, and the origin is what scopes the
    /// webview's `localStorage` -- see [`sticky_port_file`].  The remembered
    /// port is tried first (twice, so a restart's still-closing socket does not
    /// cost the reader their preferences); when it cannot be bound the launch
    /// falls back to an ephemeral port and remembers that one instead, so the
    /// GUI always starts.
    pub fn start(&mut self) -> Result<HostMetadata, String> {
        self.stop();

        let port_file = sticky_port_file();
        if let Some(port) = read_sticky_port(&port_file) {
            let mut last_err = String::new();
            for attempt in 0..PREFERRED_PORT_ATTEMPTS {
                if attempt > 0 {
                    std::thread::sleep(PREFERRED_PORT_RETRY_DELAY);
                }
                match self.start_with_port(port) {
                    Ok(meta) => {
                        remember_console_port(&port_file, meta.port);
                        return Ok(meta);
                    }
                    Err(err) => last_err = err,
                }
            }
            eprintln!("控制台端口 {port} 不可用（{last_err}），回退到随机端口");
        }

        let meta = self.start_with_port(0)?;
        remember_console_port(&port_file, meta.port);
        Ok(meta)
    }

    /// One spawn attempt against an explicit port (`0` lets the OS choose).
    fn start_with_port(&mut self, port: u16) -> Result<HostMetadata, String> {
        let target = resolve_target(self.workspace.as_deref(), self.static_dir.as_deref(), port)?;
        let mut cmd = match &target {
            SpawnTarget::Binary { path, args } => {
                let mut c = Command::new(path);
                c.args(args);
                c
            }
            SpawnTarget::Python { path, args } => {
                let mut c = Command::new(path);
                c.args(args);
                c
            }
        };

        if let Some(ref ws) = self.workspace {
            cmd.current_dir(ws);
        } else if let SpawnTarget::Binary { ref path, .. } = target {
            // Without --workspace the host treats cwd as a project. Never let
            // the installed sidecar's binaries directory become that project.
            if let Some(home) = std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))
            {
                cmd.current_dir(home);
            } else if let Some(parent) = path.parent() {
                let fallback = if parent.file_name().is_some_and(|name| name == "binaries") {
                    parent.parent().unwrap_or(parent)
                } else {
                    parent
                };
                cmd.current_dir(fallback);
            }
        }

        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());

        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = cmd.spawn().map_err(|e| format!("启动子进程失败: {e}"))?;

        let stdout = child.stdout.take().ok_or("无法捕获子进程 stdout")?;
        let mut stderr = child.stderr.take();
        let reader = BufReader::new(stdout);

        // Read stdout until the ready JSON line is encountered
        let mut meta: Option<HostMetadata> = None;
        for line_res in reader.lines() {
            let line = line_res.map_err(|e| format!("读取 stdout 失败: {e}"))?;
            let trimmed = line.trim();
            if trimmed.starts_with('{') && trimmed.contains("\"url\"") {
                if let Ok(parsed) = serde_json::from_str::<HostMetadata>(trimmed) {
                    meta = Some(parsed);
                    break;
                }
            }
        }

        let meta = match meta {
            Some(m) => m,
            None => {
                let mut err_msg = String::new();
                if let Some(ref mut err_stream) = stderr {
                    let _ = err_stream.read_to_string(&mut err_msg);
                }
                // Reap the failed attempt so a retry can take the same port.
                let _ = child.kill();
                let _ = child.wait();
                let err_trimmed = err_msg.trim();
                if !err_trimmed.is_empty() {
                    return Err(err_trimmed.to_string());
                }
                return Err("子进程未返回就绪元数据 JSON".to_string());
            }
        };
        self.current_meta = Some(meta.clone());
        self.child = Some(child);

        Ok(meta)
    }

    pub fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let pid = child.id();
            #[cfg(windows)]
            {
                // Kill process tree on Windows
                let _ = Command::new("taskkill")
                    .args(["/F", "/T", "/PID", &pid.to_string()])
                    .creation_flags(0x08000000)
                    .output();
            }
            let _ = child.kill();
            let _ = child.wait();
        }
        self.current_meta = None;
    }

    pub fn restart(&mut self) -> Result<HostMetadata, String> {
        self.stop();
        std::thread::sleep(Duration::from_millis(600));
        self.start()
    }
}

impl Drop for ProcessManager {
    fn drop(&mut self) {
        self.stop();
    }
}

pub type SharedProcessManager = Arc<Mutex<ProcessManager>>;

/// Name of the state file that remembers the console port.
pub const STICKY_PORT_FILE_NAME: &str = "gui-console-port.json";

/// How many times one launch tries the remembered port before falling back.
const PREFERRED_PORT_ATTEMPTS: usize = 2;

/// Pause between those attempts: a restart lands here with the previous host
/// only just killed, so its listening socket may still be closing.
const PREFERRED_PORT_RETRY_DELAY: Duration = Duration::from_millis(400);

/// On-disk shape of the remembered port.
#[derive(Debug, Serialize, Deserialize)]
struct StickyPortRecord {
    port: u16,
}

/// The state file that remembers the console port.
///
/// The console keeps its preferences -- the theme, the frosted-glass sliders
/// that drive the window's blur and transparency, the "open with" memory -- in
/// the webview's `localStorage`, and `localStorage` is scoped to an *origin*:
/// scheme, host and **port**.  Binding an ephemeral port therefore handed every
/// launch a fresh, empty origin and silently reset those preferences.  Keeping
/// the port stable keeps the origin stable, so a restart restores what the
/// reader chose.
pub fn sticky_port_file() -> PathBuf {
    #[cfg(windows)]
    {
        if let Some(local) = std::env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty()) {
            return PathBuf::from(local)
                .join("Synapse")
                .join(STICKY_PORT_FILE_NAME);
        }
    }
    if let Some(home) = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .filter(|v| !v.is_empty())
    {
        return PathBuf::from(home)
            .join(".synapse")
            .join(STICKY_PORT_FILE_NAME);
    }
    PathBuf::from(STICKY_PORT_FILE_NAME)
}

/// The remembered port, or `None` when there is no usable memory.
///
/// A missing, malformed or out-of-range file is not an error: the GUI then
/// starts on a fresh ephemeral port, exactly as it did before the port was
/// remembered.
pub fn read_sticky_port(path: &Path) -> Option<u16> {
    let raw = std::fs::read_to_string(path).ok()?;
    let record: StickyPortRecord = serde_json::from_str(&raw).ok()?;
    // `0` means "let the OS choose", never a port worth remembering.
    (record.port != 0).then_some(record.port)
}

/// Remember `port` for the next launch.
pub fn write_sticky_port(path: &Path, port: u16) -> Result<(), String> {
    if let Some(parent) = path.parent().filter(|dir| !dir.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("创建 {} 失败: {e}", parent.display()))?;
    }
    let body = serde_json::to_string(&StickyPortRecord { port })
        .map_err(|e| format!("序列化控制台端口失败: {e}"))?;
    std::fs::write(path, body).map_err(|e| format!("写入 {} 失败: {e}", path.display()))
}

/// Remember a bound port, reporting (never propagating) a write failure: an
/// unwritable state file must not stop the console from starting.
fn remember_console_port(path: &Path, port: u16) {
    if let Err(err) = write_sticky_port(path, port) {
        eprintln!("记住控制台端口 {port} 失败: {err}");
    }
}

/// The console host's argv for one port (`0` asks the OS for a free one).
fn console_args(port: u16) -> Vec<String> {
    vec![
        "web-console".into(),
        "--port".into(),
        port.to_string(),
        "--no-pairing".into(),
        "--auto-register".into(),
    ]
}

/// The source-checkout argv: the module entry point instead of the script.
fn python_console_args(port: u16, workspace: &Path) -> Vec<String> {
    vec![
        "-m".into(),
        "synapse.web_console.entry".into(),
        "--workspace".into(),
        workspace.to_string_lossy().into(),
        "--port".into(),
        port.to_string(),
        "--no-pairing".into(),
    ]
}

fn resolve_target(
    workspace: Option<&Path>,
    static_dir: Option<&Path>,
    port: u16,
) -> Result<SpawnTarget, String> {
    let resolve_static = |ws: Option<&Path>| -> Option<PathBuf> {
        if let Some(s) = static_dir {
            if s.exists() {
                return Some(s.to_path_buf());
            }
        }
        if let Some(w) = ws {
            let default_dist = w.join("web").join("dist");
            if default_dist.exists() {
                return Some(default_dist);
            }
        }
        None
    };

    let binary_name = if cfg!(windows) {
        "synapse.exe"
    } else {
        "synapse"
    };

    let make_args = |ws: Option<&Path>| -> Vec<String> {
        let mut args = console_args(port);
        if let Some(w) = ws {
            args.push("--workspace".into());
            args.push(w.to_string_lossy().into());
        }
        if let Some(s) = resolve_static(ws) {
            args.push("--static-dir".into());
            args.push(s.to_string_lossy().into());
        }
        args
    };

    // 1. In source checkout with .venv, prioritize local development environment
    if let Some(ws) = workspace {
        let python_rel = if cfg!(windows) {
            PathBuf::from(".venv").join("Scripts").join("python.exe")
        } else {
            PathBuf::from(".venv").join("bin").join("python")
        };
        let venv_python = ws.join(python_rel);
        if venv_python.exists() {
            let mut args = python_console_args(port, ws);
            if let Some(s) = resolve_static(Some(ws)) {
                args.push("--static-dir".into());
                args.push(s.to_string_lossy().into());
            }
            return Ok(SpawnTarget::Python {
                path: venv_python,
                args,
            });
        }
    }

    // 1. Check beside the GUI executable or in subdirectories
    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            // First check binaries/ subdirectory (standard installed / bundle layout)
            let candidate_binaries = exe_dir.join("binaries").join(binary_name);
            if candidate_binaries.exists() {
                return Ok(SpawnTarget::Binary {
                    path: candidate_binaries,
                    args: make_args(workspace),
                });
            }

            let candidate_res_binaries =
                exe_dir.join("resources").join("binaries").join(binary_name);
            if candidate_res_binaries.exists() {
                return Ok(SpawnTarget::Binary {
                    path: candidate_res_binaries,
                    args: make_args(workspace),
                });
            }

            let candidate_res = exe_dir.join("resources").join(binary_name);
            if candidate_res.exists() {
                return Ok(SpawnTarget::Binary {
                    path: candidate_res,
                    args: make_args(workspace),
                });
            }

            let candidate = exe_dir.join(binary_name);
            if candidate.exists() {
                return Ok(SpawnTarget::Binary {
                    path: candidate,
                    args: make_args(workspace),
                });
            }
        }
    }

    // 2. Check workspace dist/ or .venv directory (if a workspace is provided)
    if let Some(ws) = workspace {
        let dist_binary = ws.join("dist").join(binary_name);
        if dist_binary.exists() {
            return Ok(SpawnTarget::Binary {
                path: dist_binary,
                args: make_args(Some(ws)),
            });
        }

        let python_rel = if cfg!(windows) {
            PathBuf::from(".venv").join("Scripts").join("python.exe")
        } else {
            PathBuf::from(".venv").join("bin").join("python")
        };
        let venv_python = ws.join(python_rel);
        if venv_python.exists() {
            let mut args = python_console_args(port, ws);
            let static_dir = ws.join("web").join("dist");
            if static_dir.exists() {
                args.push("--static-dir".into());
                args.push(static_dir.to_string_lossy().into());
            }
            return Ok(SpawnTarget::Python {
                path: venv_python,
                args,
            });
        }
    }

    Err("未找到 synapse 二进制制品或 .venv 环境".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    /// A unique path under the OS temp dir: the tests never touch the real
    /// state file, and a leftover from an earlier run cannot leak into them.
    fn temp_dir(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!(
            "synapse-gui-sidecar-{}-{tag}-{nanos}",
            std::process::id()
        ))
    }

    /// The value right after `--port`, which is what the host parses.
    fn port_flag_value(args: &[String]) -> Option<String> {
        let index = args.iter().position(|arg| arg == "--port")?;
        args.get(index + 1).cloned()
    }

    /// The memory has to sit next to the console's logs, not in the cwd: the
    /// installed GUI runs from its bundle directory.
    #[test]
    fn the_state_file_is_named_and_located_for_the_console() {
        assert!(sticky_port_file().ends_with(STICKY_PORT_FILE_NAME));
    }

    #[test]
    fn a_remembered_port_round_trips_and_creates_its_directory() {
        let dir = temp_dir("round-trip");
        let path = dir.join("nested").join(STICKY_PORT_FILE_NAME);
        assert_eq!(
            read_sticky_port(&path),
            None,
            "a missing file means no memory"
        );

        write_sticky_port(&path, 51_234).expect("the port must be written");
        assert!(path.exists(), "the write must create the parent directory");
        assert_eq!(read_sticky_port(&path), Some(51_234));

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A hand-edited, truncated or pre-`u16` file must degrade to "no memory"
    /// instead of failing the launch.
    #[test]
    fn an_unusable_state_file_reads_as_no_memory() {
        let dir = temp_dir("garbage");
        std::fs::create_dir_all(&dir).expect("the temp directory must be creatable");
        let path = dir.join(STICKY_PORT_FILE_NAME);

        for body in [
            "",
            "not json",
            "{}",
            "{\"port\":0}",
            "{\"port\":70000}",
            "{\"port\":\"8080\"}",
        ] {
            std::fs::write(&path, body).expect("the fixture must be written");
            assert_eq!(read_sticky_port(&path), None, "unusable fixture: {body:?}");
        }

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Both spawn flavours must carry the remembered port; only the very first
    /// launch (and the fallback) may still ask for `0`.
    #[test]
    fn both_spawn_flavours_carry_the_remembered_port() {
        let binary = console_args(51_234);
        assert_eq!(binary.first().map(String::as_str), Some("web-console"));
        assert_eq!(port_flag_value(&binary), Some("51234".to_string()));

        let python = python_console_args(51_234, Path::new("workspace"));
        assert_eq!(port_flag_value(&python), Some("51234".to_string()));

        assert_eq!(port_flag_value(&console_args(0)), Some("0".to_string()));
    }
}
