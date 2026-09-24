use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::PathBuf;
use portable_pty::{CommandBuilder, NativePtySystem, PtySize, PtySystem};
use tauri::{AppHandle, Emitter};

pub struct TerminalInstance {
    #[allow(dead_code)]
    pub id: u32,
    pub child_pid: Option<u32>,
    pub writer: Box<dyn Write + Send>,
    pub master: Box<dyn portable_pty::MasterPty + Send>,
}

#[derive(Default)]
pub struct TerminalManager {
    next_id: u32,
    instances: HashMap<u32, TerminalInstance>,
}

impl TerminalManager {
    pub fn new() -> Self {
        Self {
            next_id: 1,
            instances: HashMap::new(),
        }
    }

    pub fn create_terminal(
        &mut self,
        app: AppHandle,
        workspace: Option<PathBuf>,
        shell: Option<String>,
        cols: u16,
        rows: u16,
    ) -> Result<u32, String> {
        let id = self.next_id;
        self.next_id += 1;

        let pty_system = NativePtySystem::default();
        let size = PtySize {
            rows: rows.max(5),
            cols: cols.max(10),
            pixel_width: 0,
            pixel_height: 0,
        };

        let pty_pair = pty_system
            .openpty(size)
            .map_err(|e| format!("Failed to open PTY: {}", e))?;

        // Resolve shell executable
        let default_shell = if cfg!(windows) {
            if which_shell("pwsh.exe") {
                "pwsh.exe".to_string()
            } else {
                "powershell.exe".to_string()
            }
        } else {
            std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".to_string())
        };

        let shell_cmd = shell.unwrap_or(default_shell);
        let mut cmd = CommandBuilder::new(&shell_cmd);

        if shell_cmd.contains("powershell") || shell_cmd.contains("pwsh") {
            cmd.args(["-NoLogo"]);
        }

        if let Some(cwd) = workspace {
            if cwd.exists() {
                cmd.cwd(cwd);
            }
        }

        let mut child = pty_pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| format!("Failed to spawn shell '{}': {}", shell_cmd, e))?;
        let child_pid = child.process_id();

        let mut reader = pty_pair
            .master
            .try_clone_reader()
            .map_err(|e| format!("Failed to clone PTY reader: {}", e))?;

        let writer = pty_pair
            .master
            .take_writer()
            .map_err(|e| format!("Failed to take PTY writer: {}", e))?;

        let app_data = app.clone();
        let app_exit = app.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let text = String::from_utf8_lossy(&buf[..n]).to_string();
                        let event_name = format!("terminal-data-{}", id);
                        let _ = app_data.emit(&event_name, text);
                    }
                    Err(_) => break,
                }
            }
        });

        // Wait for child process exit in dedicated thread
        std::thread::spawn(move || {
            let _ = child.wait();
            let exit_event = format!("terminal-exit-{}", id);
            let _ = app_exit.emit(&exit_event, id);
        });

        let instance = TerminalInstance {
            id,
            child_pid,
            writer,
            master: pty_pair.master,
        };

        self.instances.insert(id, instance);
        Ok(id)
    }

    pub fn write_terminal(&mut self, id: u32, data: &str) -> Result<(), String> {
        if let Some(inst) = self.instances.get_mut(&id) {
            inst.writer
                .write_all(data.as_bytes())
                .map_err(|e| format!("Failed to write to terminal: {}", e))?;
            inst.writer
                .flush()
                .map_err(|e| format!("Failed to flush terminal: {}", e))?;
            Ok(())
        } else {
            Err(format!("Terminal session {} not found", id))
        }
    }

    pub fn resize_terminal(&mut self, id: u32, cols: u16, rows: u16) -> Result<(), String> {
        if let Some(inst) = self.instances.get_mut(&id) {
            let size = PtySize {
                rows: rows.max(2),
                cols: cols.max(5),
                pixel_width: 0,
                pixel_height: 0,
            };
            inst.master
                .resize(size)
                .map_err(|e| format!("Failed to resize PTY: {}", e))
        } else {
            Err(format!("Terminal session {} not found", id))
        }
    }

    pub fn close_terminal(&mut self, id: u32) -> Result<(), String> {
        if let Some(inst) = self.instances.remove(&id) {
            if let Some(pid) = inst.child_pid {
                #[cfg(windows)]
                {
                    let _ = std::process::Command::new("taskkill")
                        .args(["/F", "/PID", &pid.to_string(), "/T"])
                        .status();
                }
                #[cfg(not(windows))]
                {
                    let _ = std::process::Command::new("kill")
                        .args(["-9", &pid.to_string()])
                        .status();
                }
            }
            Ok(())
        } else {
            Err(format!("Terminal session {} not found", id))
        }
    }
}

fn which_shell(name: &str) -> bool {
    if let Ok(paths) = std::env::var("PATH") {
        for p in std::env::split_paths(&paths) {
            let full = p.join(name);
            if full.exists() {
                return true;
            }
        }
    }
    false
}
