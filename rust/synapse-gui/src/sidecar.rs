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
}

impl ProcessManager {
    pub fn new(workspace: Option<PathBuf>) -> Self {
        Self {
            child: None,
            current_meta: None,
            workspace,
        }
    }

    pub fn current_url(&self) -> Option<String> {
        self.current_meta.as_ref().map(|m| m.url.clone())
    }

    pub fn start(&mut self) -> Result<HostMetadata, String> {
        self.stop();

        let target = resolve_target(self.workspace.as_deref())?;
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

fn resolve_target(workspace: Option<&Path>) -> Result<SpawnTarget, String> {
    let binary_name = if cfg!(windows) {
        "synapse.exe"
    } else {
        "synapse"
    };

    let make_args = |ws: Option<&Path>| -> Vec<String> {
        let mut args = vec![
            "web-console".into(),
            "--port".into(),
            "0".into(),
            "--no-pairing".into(),
            "--auto-register".into(),
        ];
        if let Some(w) = ws {
            args.push("--workspace".into());
            args.push(w.to_string_lossy().into());
        }
        args
    };

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
            let mut args = vec![
                "-m".into(),
                "synapse.web_console.entry".into(),
                "--workspace".into(),
                ws.to_string_lossy().into(),
                "--port".into(),
                "0".into(),
                "--no-pairing".into(),
            ];
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
