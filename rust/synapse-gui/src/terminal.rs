use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use portable_pty::{CommandBuilder, NativePtySystem, PtySize, PtySystem};
use serde::Serialize;
use tauri::{AppHandle, Emitter};

use crate::agent_title::{self, TitleLookup};

/// Latest agent conversation title resolved for one PTY.
///
/// The value is the render source for the `@` menu entry and for the PTY-only
/// snapshot command; the event stream only announces changes.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct AgentTitleSnapshot {
    pub conversation_id: Option<String>,
    pub title: Option<String>,
}

/// Shared, mutable title state of exactly one PTY.
///
/// The PTY hook poller writes it, `tauri_terminal_agent_title` reads it, and
/// the webview never touches the SQLite file itself.
pub type SharedAgentTitle = Arc<Mutex<AgentTitleSnapshot>>;

const MAX_SCROLLBACK_BYTES: usize = 256 * 1024;

/// Bounded byte ring buffer for PTY output replay across WebView refreshes.
///
/// Tracks a monotonic byte offset (`total_bytes`) so an attaching frontend can
/// deduplicate live `terminal-data-{id}` chunks received while the attach IPC
/// was in flight, and trims dropped prefixes to a valid UTF-8 codepoint start.
#[derive(Debug)]
pub struct OutputRingBuffer {
    buf: VecDeque<u8>,
    total_bytes: u64,
    max_bytes: usize,
}

impl Default for OutputRingBuffer {
    fn default() -> Self {
        Self::with_capacity(MAX_SCROLLBACK_BYTES)
    }
}

impl OutputRingBuffer {
    pub fn with_capacity(max_bytes: usize) -> Self {
        Self {
            buf: VecDeque::with_capacity(max_bytes.min(64 * 1024)),
            total_bytes: 0,
            max_bytes: max_bytes.max(1),
        }
    }

    pub fn push(&mut self, bytes: &[u8]) -> (u64, u64) {
        let start_offset = self.total_bytes;
        self.total_bytes = self.total_bytes.saturating_add(bytes.len() as u64);
        let end_offset = self.total_bytes;

        if bytes.is_empty() {
            return (start_offset, end_offset);
        }

        if bytes.len() >= self.max_bytes {
            self.buf.clear();
            let tail = &bytes[bytes.len() - self.max_bytes..];
            self.buf.extend(tail);
        } else {
            self.buf.extend(bytes);
            while self.buf.len() > self.max_bytes {
                self.buf.pop_front();
            }
        }

        // Never leave a UTF-8 continuation byte (10xxxxxx) at the front.
        while matches!(self.buf.front(), Some(&b) if (b & 0b1100_0000) == 0b1000_0000) {
            self.buf.pop_front();
        }

        (start_offset, end_offset)
    }

    pub fn snapshot(&self) -> (String, u64) {
        let (front, back) = self.buf.as_slices();
        let mut combined = Vec::with_capacity(front.len() + back.len());
        combined.extend_from_slice(front);
        combined.extend_from_slice(back);
        let text = String::from_utf8_lossy(&combined).into_owned();
        (text, self.total_bytes)
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TerminalDataChunk {
    pub data: String,
    pub start_offset: u64,
    pub end_offset: u64,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TerminalSessionInfo {
    pub pty_id: u32,
    pub workspace: Option<String>,
    pub shell: String,
    pub command: Option<String>,
    pub cols: u16,
    pub rows: u16,
    pub exited: bool,
    pub conversation_id: Option<String>,
    pub title: Option<String>,
    pub last_agent_state: Option<serde_json::Value>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TerminalListResponse {
    pub boot_id: String,
    pub sessions: Vec<TerminalSessionInfo>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TerminalAttachSnapshot {
    pub boot_id: String,
    pub pty_id: u32,
    pub scrollback: String,
    pub end_offset: u64,
    pub cols: u16,
    pub rows: u16,
    pub exited: bool,
    pub conversation_id: Option<String>,
    pub title: Option<String>,
    pub last_agent_state: Option<serde_json::Value>,
}

pub struct TerminalInstance {
    #[allow(dead_code)]
    pub id: u32,
    pub child_pid: Option<u32>,
    pub writer: Box<dyn Write + Send>,
    pub master: Box<dyn portable_pty::MasterPty + Send>,
    pub stop_flag: Arc<AtomicBool>,
    pub exited: Arc<AtomicBool>,
    pub workspace: Option<String>,
    pub shell: String,
    pub command: Option<String>,
    pub cols: u16,
    pub rows: u16,
    pub output_buffer: Arc<Mutex<OutputRingBuffer>>,
    pub last_agent_state: Arc<Mutex<Option<serde_json::Value>>>,
    /// Conversation title state fed by the hook poller.
    pub agent_title: SharedAgentTitle,
}

pub struct TerminalManager {
    boot_id: String,
    next_id: u32,
    instances: HashMap<u32, TerminalInstance>,
}

impl Default for TerminalManager {
    fn default() -> Self {
        Self::new()
    }
}

const HOOK_SCRIPT_NAME: &str = "synapse-cockpit-hook.mjs";

const HOOK_SCRIPT_BODY: &str = r#"// Synapse Cockpit Lifecycle Hook for Antigravity CLI (agy)
// Emits deterministic state (idle / working / done) and extracts clean Markdown from transcript.jsonl
import fs from 'node:fs';
import path from 'node:path';

const stateFile = process.env.SYNAPSE_HOOK_STATE_FILE;
const ptyId = Number(process.env.SYNAPSE_PTY_ID || 0);

if (!stateFile || !ptyId) {
  process.stdout.write('{}\n');
  process.exit(0);
}

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  raw += chunk;
});

process.stdin.on('end', () => {
  try {
    const payload = JSON.parse(raw || '{}');
    const event = String(payload.hookEventName || '');
    // Strict identity: only a real JSON `true` means the turn is over. A missing
    // field, "true" as a string, or 1 must not be coerced into a completion.
    const fullyIdle = payload.fullyIdle === true;
    let state = 'working';
    let detail = '';
    let lastAssistantMarkdown = '';

    if (event === 'SessionStart') {
      state = 'idle';
      detail = '会话已就绪';
    } else if (event === 'PreInvocation') {
      state = 'working';
      detail = '正在思考与推理...';
    } else if (event === 'PostToolUse') {
      state = 'working';
      const toolName = payload.toolCall && payload.toolCall.name ? payload.toolCall.name : '';
      detail = toolName ? `已执行工具: ${toolName}` : '正在执行工具...';
    } else if (event === 'Stop') {
      if (fullyIdle) {
        state = 'done';
        detail = '已完成本轮执行';
        const transcriptPath = payload.transcriptPath ? String(payload.transcriptPath) : '';
        if (transcriptPath) {
          lastAssistantMarkdown = extractLastPlannerResponse(transcriptPath);
        }
      } else {
        // Not the end of the turn: report work in progress and keep the
        // transcript untouched so a later, fully idle Stop can extract it.
        state = 'working';
        detail = '正在收尾...';
      }
    }

    const out = {
      seq: Date.now(),
      ptyId,
      state,
      event,
      detail,
      conversationId: payload.conversationId || null,
      terminationReason: payload.terminationReason || null,
      fullyIdle,
      lastAssistantMarkdown: lastAssistantMarkdown || null,
    };

    fs.mkdirSync(path.dirname(stateFile), { recursive: true });
    const tmpFile = `${stateFile}.tmp`;
    fs.writeFileSync(tmpFile, JSON.stringify(out), 'utf8');
    fs.renameSync(tmpFile, stateFile);
  } catch {
    // Never block or fail the host CLI turn on a hook telemetry error
  }
  process.stdout.write('{}\n');
  process.exit(0);
});

function extractLastPlannerResponse(transcriptPath) {
  try {
    const dir = path.dirname(transcriptPath);
    const fullPath = path.join(dir, 'transcript_full.jsonl');
    const target = fs.existsSync(fullPath) ? fullPath : transcriptPath;
    if (!fs.existsSync(target)) return '';
    const content = fs.readFileSync(target, 'utf8');
    const lines = content.split(/\r?\n/);
    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i].trim();
      if (!line) continue;
      try {
        const row = JSON.parse(line);
        if (
          (row.type === 'PLANNER_RESPONSE' || row.type === 'assistant') &&
          typeof row.content === 'string' &&
          row.content.trim().length > 0
        ) {
          return row.content.trim();
        }
      } catch {}
    }
  } catch {}
  return '';
}
"#;

/// Event name of the agent conversation title channel for one PTY.
pub fn agent_title_event_name(pty_id: u32) -> String {
    format!("terminal-agent-title-{}", pty_id)
}

/// Payload of `terminal-agent-title-{ptyId}`.
///
/// `title` is `null` while the conversation has no stored summary yet (or the
/// summary database is unavailable); the renderer should then fall back to the
/// conversation ID, then to the generic agent label.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AgentTitlePayload {
    pub pty_id: u32,
    pub conversation_id: String,
    pub title: Option<String>,
}

/// How often the poller re-reads the summary of the current conversation.
///
/// The conversation ID arrives with the 160 ms hook poll, but the title is
/// refined by `agy` afterwards and a summary row may not exist on the first
/// turn, so a bounded refresh keeps a late title from being lost. The lookup is
/// one primary-key read on a read-only connection, and it is skipped entirely
/// while no conversation ID is known.
const AGENT_TITLE_REFRESH: Duration = Duration::from_secs(2);

/// Send `agent_title_event_name(pty_id)` if the tool-reported title changed.
///
/// De-duplication lives here so the 160 ms hook poll and the slower title
/// refresh can both call it: a repeated conversation ID and title never emits
/// twice, while a title that first appears later (`None` -> `Some`) does.
fn update_agent_title(
    app: &AppHandle,
    snapshot: &SharedAgentTitle,
    pty_id: u32,
    conversation_id: &str,
    lookup: TitleLookup,
) {
    let Some(conversation_id) = agent_title::normalize_conversation_id(conversation_id) else {
        return;
    };
    let title = lookup(conversation_id);

    // A poisoned lock only means a previous holder panicked; the payload is a
    // cheap value, so recover it rather than dropping every later title.
    let mut current = match snapshot.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };

    let next = AgentTitleSnapshot {
        conversation_id: Some(conversation_id.to_string()),
        title,
    };
    if *current == next {
        return;
    }
    *current = next.clone();
    drop(current);

    let payload = AgentTitlePayload {
        pty_id,
        conversation_id: next.conversation_id.unwrap_or_default(),
        title: next.title,
    };
    let _ = app.emit(&agent_title_event_name(pty_id), payload);
}

/// Ensure `~/.gemini/config/synapse-cockpit-hook.mjs` and `~/.gemini/config/hooks.json`
/// have the `synapse-cockpit-bridge` lifecycle hook registered so `agy` CLI sessions
/// spawned inside Synapse PTYs report deterministic `idle`/`working`/`done` states.
fn ensure_agy_hooks_installed() {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from);
    let Some(home_dir) = home else { return };

    let config_dir = home_dir.join(".gemini").join("config");
    if std::fs::create_dir_all(&config_dir).is_err() {
        return;
    }

    let script_path = config_dir.join(HOOK_SCRIPT_NAME);
    let _ = std::fs::write(&script_path, HOOK_SCRIPT_BODY);

    let hooks_json_path = config_dir.join("hooks.json");
    let mut root: serde_json::Value = if hooks_json_path.exists() {
        std::fs::read_to_string(&hooks_json_path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_else(|| serde_json::json!({}))
    } else {
        serde_json::json!({})
    };

    let Some(obj) = root.as_object_mut() else {
        return;
    };

    let script_str = script_path.to_string_lossy().replace('\\', "/");
    let cmd_str = format!("node \"{}\"", script_str);

    obj.insert(
        "synapse-cockpit-bridge".to_string(),
        serde_json::json!({
            "enabled": true,
            "SessionStart": [
                { "type": "command", "command": cmd_str, "timeout": 5 }
            ],
            "PreInvocation": [
                { "type": "command", "command": cmd_str, "timeout": 5 }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        { "type": "command", "command": cmd_str, "timeout": 5 }
                    ]
                }
            ],
            "Stop": [
                { "type": "command", "command": cmd_str, "timeout": 5 }
            ]
        }),
    );

    if let Ok(formatted) = serde_json::to_string_pretty(&root) {
        let _ = std::fs::write(&hooks_json_path, formatted);
    }
}

fn build_startup_command_with_prompt(startup_cmd: &str, has_initial_prompt: bool, is_windows_ps: bool) -> String {
    if !has_initial_prompt {
        return startup_cmd.to_string();
    }
    let trimmed = startup_cmd.trim();
    let lower = trimmed.to_lowercase();
    let env_ref = if is_windows_ps {
        "\"$env:SYNAPSE_AGENT_PROMPT\""
    } else {
        "\"$SYNAPSE_AGENT_PROMPT\""
    };

    if lower == "agy" || lower.starts_with("agy ") || lower.ends_with("\\agy.exe") || lower.ends_with("/agy") {
        format!("{} -i {}", trimmed, env_ref)
    } else if lower == "claude"
        || lower.starts_with("claude ")
        || lower == "codex"
        || lower.starts_with("codex ")
    {
        format!("{} {}", trimmed, env_ref)
    } else if trimmed.is_empty() {
        if is_windows_ps {
            "Invoke-Expression $env:SYNAPSE_AGENT_PROMPT".to_string()
        } else {
            "eval \"$SYNAPSE_AGENT_PROMPT\"".to_string()
        }
    } else {
        format!("{} {}", trimmed, env_ref)
    }
}

fn generate_boot_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{}-{}", std::process::id(), nanos)
}

fn kill_process_tree(pid: u32) {
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

impl TerminalManager {
    pub fn new() -> Self {
        ensure_agy_hooks_installed();
        Self {
            boot_id: generate_boot_id(),
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
        command: Option<String>,
        initial_prompt: Option<String>,
    ) -> Result<u32, String> {
        ensure_agy_hooks_installed();

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

        // Set deterministic hook state file and PTY ID in child environment
        let hook_dir = std::env::temp_dir().join("synapse-pty-hooks");
        let _ = std::fs::create_dir_all(&hook_dir);
        let hook_state_file = hook_dir.join(format!("pty-{}.json", id));
        let _ = std::fs::remove_file(&hook_state_file);

        cmd.env("SYNAPSE_PTY_ID", id.to_string());
        cmd.env(
            "SYNAPSE_HOOK_STATE_FILE",
            hook_state_file.to_string_lossy().to_string(),
        );

        let clean_prompt = initial_prompt
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(|s| s.to_string());

        if let Some(ref prompt_val) = clean_prompt {
            cmd.env("SYNAPSE_AGENT_PROMPT", prompt_val);
        }

        let trimmed_command = command
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty());

        let is_ps = shell_cmd.contains("powershell") || shell_cmd.contains("pwsh");
        if is_ps {
            if let Some(startup_cmd) = trimmed_command {
                let effective = build_startup_command_with_prompt(
                    startup_cmd,
                    clean_prompt.is_some(),
                    true,
                );
                cmd.args(["-NoLogo", "-NoExit", "-Command", &effective]);
            } else if clean_prompt.is_some() {
                cmd.args([
                    "-NoLogo",
                    "-NoExit",
                    "-Command",
                    "Invoke-Expression $env:SYNAPSE_AGENT_PROMPT",
                ]);
            } else {
                cmd.args(["-NoLogo"]);
            }
        } else if let Some(startup_cmd) = trimmed_command {
            if cfg!(windows) && shell_cmd.contains("cmd") {
                cmd.args(["/K", startup_cmd]);
            } else {
                let effective = build_startup_command_with_prompt(
                    startup_cmd,
                    clean_prompt.is_some(),
                    false,
                );
                let wrapped = format!("{}; exec {}", effective, shell_cmd);
                cmd.args(["-i", "-c", &wrapped]);
            }
        }

        let workspace_str = workspace
            .as_ref()
            .map(|p| p.to_string_lossy().to_string());
        let saved_command = trimmed_command.map(str::to_string);
        let saved_shell = shell_cmd.clone();

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

        let stop_flag = Arc::new(AtomicBool::new(false));
        let stop_for_hook = stop_flag.clone();
        let stop_for_exit = stop_flag.clone();
        let exited_flag = Arc::new(AtomicBool::new(false));
        let exited_for_wait = exited_flag.clone();

        let output_buffer = Arc::new(Mutex::new(OutputRingBuffer::default()));
        let output_for_reader = output_buffer.clone();
        let last_agent_state: Arc<Mutex<Option<serde_json::Value>>> = Arc::new(Mutex::new(None));
        let state_for_hook = last_agent_state.clone();

        // Title state outlives the poller: the snapshot command reads it after
        // the fact, so a renderer that mounted late (or remounted) can still
        // recover the current title without a replay of the event stream.
        let agent_title: SharedAgentTitle = Arc::new(Mutex::new(AgentTitleSnapshot::default()));

        let app_data = app.clone();
        let app_exit = app.clone();
        let app_hook = app.clone();

        // Stream PTY stdout/stderr bytes to frontend xterm and record into bounded ring buffer
        std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => {
                        let (start_offset, end_offset) = match output_for_reader.lock() {
                            Ok(mut guard) => guard.push(&buf[..n]),
                            Err(poisoned) => poisoned.into_inner().push(&buf[..n]),
                        };
                        let text = String::from_utf8_lossy(&buf[..n]).to_string();
                        let event_name = format!("terminal-data-{}", id);
                        let _ = app_data.emit(
                            &event_name,
                            TerminalDataChunk {
                                data: text,
                                start_offset,
                                end_offset,
                            },
                        );
                    }
                    Err(_) => break,
                }
            }
        });

        // Poll deterministic hook state file written by `synapse-cockpit-hook.mjs`.
        //
        // The same loop resolves the `agy` conversation title: the hook payload
        // `conversationId` is looked up in the local summary database, and the
        // result is emitted on `terminal-agent-title-{id}` only when it changes.
        // The database read is throttled to `AGENT_TITLE_REFRESH` so the 160 ms
        // terminal cadence never turns into a per-tick SQLite query; the title
        // also stays refreshable after the conversation ID stops changing, so a
        // summary written later (`null` -> title) still reaches the renderer.
        let watched_file = hook_state_file.clone();
        let title_snapshot = agent_title.clone();
        std::thread::spawn(move || {
            let mut last_seq: u64 = 0;
            let mut last_title_check: Option<std::time::Instant> = None;
            let mut observed_conversation_id: Option<String> = None;
            let event_name = format!("terminal-agent-state-{}", id);
            while !stop_for_hook.load(Ordering::Relaxed) {
                if let Ok(raw) = std::fs::read_to_string(&watched_file) {
                    if let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) {
                        let seq = val.get("seq").and_then(|v| v.as_u64()).unwrap_or(0);
                        if seq > last_seq {
                            last_seq = seq;
                            let conversation_id = val
                                .get("conversationId")
                                .and_then(|v| v.as_str())
                                .and_then(agent_title::normalize_conversation_id)
                                .map(str::to_string);
                            let conversation_changed = conversation_id != observed_conversation_id;
                            observed_conversation_id = conversation_id;
                            match state_for_hook.lock() {
                                Ok(mut guard) => *guard = Some(val.clone()),
                                Err(poisoned) => *poisoned.into_inner() = Some(val.clone()),
                            }
                            let _ = app_hook.emit(&event_name, val);

                            if conversation_changed {
                                // A new conversation is resolved immediately so the
                                // `@` menu does not wait for the refresh window.
                                last_title_check = Some(std::time::Instant::now());
                                if let Some(ref cid) = observed_conversation_id {
                                    update_agent_title(
                                        &app_hook,
                                        &title_snapshot,
                                        id,
                                        cid,
                                        agent_title::lookup_default_title,
                                    );
                                }
                            }
                        }
                    }
                }

                if let Some(ref cid) = observed_conversation_id {
                    let due = last_title_check
                        .map(|at| at.elapsed() >= AGENT_TITLE_REFRESH)
                        .unwrap_or(true);
                    if due {
                        last_title_check = Some(std::time::Instant::now());
                        update_agent_title(
                            &app_hook,
                            &title_snapshot,
                            id,
                            cid,
                            agent_title::lookup_default_title,
                        );
                    }
                }

                std::thread::sleep(Duration::from_millis(160));
            }
            let _ = std::fs::remove_file(&watched_file);
        });

        // Wait for child process exit in dedicated thread
        std::thread::spawn(move || {
            let _ = child.wait();
            exited_for_wait.store(true, Ordering::Relaxed);
            stop_for_exit.store(true, Ordering::Relaxed);
            let exit_event = format!("terminal-exit-{}", id);
            let _ = app_exit.emit(&exit_event, id);
        });

        let instance = TerminalInstance {
            id,
            child_pid,
            writer,
            master: pty_pair.master,
            stop_flag,
            exited: exited_flag,
            workspace: workspace_str,
            shell: saved_shell,
            command: saved_command,
            cols: size.cols,
            rows: size.rows,
            output_buffer,
            last_agent_state,
            agent_title,
        };

        self.instances.insert(id, instance);
        Ok(id)
    }

    /// Current conversation title of one PTY (PTY-scoped, read-only).
    ///
    /// Returns `Err` for an unknown PTY so the frontend can distinguish "closed"
    /// from "no title yet". This is the snapshot half of the
    /// `terminal-agent-title-{id}` event contract: listen first, then snapshot,
    /// and keep the newer value.
    pub fn agent_title_snapshot(&self, id: u32) -> Result<AgentTitleSnapshot, String> {
        let inst = self
            .instances
            .get(&id)
            .ok_or_else(|| format!("Terminal session {} not found", id))?;
        let snapshot = match inst.agent_title.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        };
        Ok(snapshot)
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
            inst.cols = size.cols;
            inst.rows = size.rows;
            inst.master
                .resize(size)
                .map_err(|e| format!("Failed to resize PTY: {}", e))
        } else {
            Err(format!("Terminal session {} not found", id))
        }
    }

    pub fn list_terminals(&self) -> TerminalListResponse {
        let mut sessions: Vec<TerminalSessionInfo> = self
            .instances
            .values()
            .map(|inst| {
                let title_snapshot = match inst.agent_title.lock() {
                    Ok(guard) => guard.clone(),
                    Err(poisoned) => poisoned.into_inner().clone(),
                };
                let last_agent_state = match inst.last_agent_state.lock() {
                    Ok(guard) => guard.clone(),
                    Err(poisoned) => poisoned.into_inner().clone(),
                };
                TerminalSessionInfo {
                    pty_id: inst.id,
                    workspace: inst.workspace.clone(),
                    shell: inst.shell.clone(),
                    command: inst.command.clone(),
                    cols: inst.cols,
                    rows: inst.rows,
                    exited: inst.exited.load(Ordering::Relaxed),
                    conversation_id: title_snapshot.conversation_id,
                    title: title_snapshot.title,
                    last_agent_state,
                }
            })
            .collect();
        sessions.sort_by_key(|s| s.pty_id);
        TerminalListResponse {
            boot_id: self.boot_id.clone(),
            sessions,
        }
    }

    pub fn attach_terminal(&self, id: u32) -> Result<TerminalAttachSnapshot, String> {
        let inst = self
            .instances
            .get(&id)
            .ok_or_else(|| format!("Terminal session {} not found", id))?;
        let (scrollback, end_offset) = match inst.output_buffer.lock() {
            Ok(guard) => guard.snapshot(),
            Err(poisoned) => poisoned.into_inner().snapshot(),
        };
        let title_snapshot = match inst.agent_title.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        };
        let last_agent_state = match inst.last_agent_state.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        };
        Ok(TerminalAttachSnapshot {
            boot_id: self.boot_id.clone(),
            pty_id: inst.id,
            scrollback,
            end_offset,
            cols: inst.cols,
            rows: inst.rows,
            exited: inst.exited.load(Ordering::Relaxed),
            conversation_id: title_snapshot.conversation_id,
            title: title_snapshot.title,
            last_agent_state,
        })
    }

    pub fn close_terminal(&mut self, id: u32) -> Result<(), String> {
        if let Some(inst) = self.instances.remove(&id) {
            inst.stop_flag.store(true, Ordering::Relaxed);
            if let Some(pid) = inst.child_pid {
                kill_process_tree(pid);
            }
            Ok(())
        } else {
            Err(format!("Terminal session {} not found", id))
        }
    }

    pub fn close_all(&mut self) {
        let ids: Vec<u32> = self.instances.keys().copied().collect();
        for id in ids {
            if let Some(inst) = self.instances.remove(&id) {
                inst.stop_flag.store(true, Ordering::Relaxed);
                if let Some(pid) = inst.child_pid {
                    kill_process_tree(pid);
                }
            }
        }
    }
}

/// Command-side mirror of [`AgentTitlePayload`] with Tauri's default
/// snake_case argument naming, so `invoke('tauri_terminal_agent_title', { ptyId })`
/// works while the event payload stays camelCase.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct AgentTitleSnapshotDto {
    pub pty_id: u32,
    pub conversation_id: Option<String>,
    pub title: Option<String>,
}

impl AgentTitleSnapshotDto {
    pub fn new(pty_id: u32, snapshot: AgentTitleSnapshot) -> Self {
        Self {
            pty_id,
            conversation_id: snapshot.conversation_id,
            title: snapshot.title,
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Stand-in for the SQLite lookup so the title state machine can be tested
    /// without an `agy` installation. The real query is covered by
    /// `crate::agent_title::tests`.
    fn fixed_title(conversation_id: &str) -> Option<String> {
        match conversation_id {
            "conv-known" => Some("Refine the cockpit".to_string()),
            _ => None,
        }
    }

    #[test]
    fn event_name_is_pty_scoped() {
        assert_eq!(agent_title_event_name(7), "terminal-agent-title-7");
    }

    #[test]
    fn payload_serializes_camel_case_with_null_title() {
        let payload = AgentTitlePayload {
            pty_id: 3,
            conversation_id: "conv-known".to_string(),
            title: None,
        };
        let json = serde_json::to_value(&payload).expect("serialize payload");
        assert_eq!(
            json,
            serde_json::json!({
                "ptyId": 3,
                "conversationId": "conv-known",
                "title": null
            })
        );

        let with_title = AgentTitlePayload {
            pty_id: 3,
            conversation_id: "conv-known".to_string(),
            title: Some("Refine the cockpit".to_string()),
        };
        let json = serde_json::to_value(&with_title).expect("serialize payload");
        assert_eq!(json["title"], serde_json::json!("Refine the cockpit"));
    }

    #[test]
    fn snapshot_command_dto_keeps_snake_case_fields() {
        let dto = AgentTitleSnapshotDto::new(
            9,
            AgentTitleSnapshot {
                conversation_id: Some("conv-known".to_string()),
                title: Some("Refine the cockpit".to_string()),
            },
        );
        let json = serde_json::to_value(&dto).expect("serialize dto");
        assert_eq!(
            json,
            serde_json::json!({
                "pty_id": 9,
                "conversation_id": "conv-known",
                "title": "Refine the cockpit"
            })
        );
    }

    /// The poller must not query SQLite for a PTY without a conversation ID.
    #[test]
    fn blank_conversation_id_skips_the_lookup() {
        assert_eq!(agent_title::normalize_conversation_id(""), None);
        assert_eq!(agent_title::normalize_conversation_id("   "), None);
        assert_eq!(
            agent_title::normalize_conversation_id("  conv-known "),
            Some("conv-known")
        );
    }

    #[test]
    fn known_conversation_resolves_title_and_unknown_stays_null() {
        assert_eq!(
            fixed_title("conv-known").as_deref(),
            Some("Refine the cockpit")
        );
        assert_eq!(fixed_title("conv-unknown"), None);
    }

    fn snapshot_of(
        snapshot: &SharedAgentTitle,
        conversation_id: Option<&str>,
        title: Option<&str>,
    ) -> AgentTitleSnapshot {
        let mut guard = snapshot.lock().expect("lock snapshot");
        *guard = AgentTitleSnapshot {
            conversation_id: conversation_id.map(str::to_string),
            title: title.map(str::to_string),
        };
        guard.clone()
    }

    #[test]
    fn snapshot_round_trips_through_shared_state() {
        let shared: SharedAgentTitle = Arc::new(Mutex::new(AgentTitleSnapshot::default()));
        let initial = snapshot_of(&shared, None, None);
        assert_eq!(initial, AgentTitleSnapshot::default());

        let filled = snapshot_of(&shared, Some("conv-known"), Some("Refine the cockpit"));
        assert_eq!(filled.conversation_id.as_deref(), Some("conv-known"));
        assert_eq!(filled.title.as_deref(), Some("Refine the cockpit"));
    }

    /// A poisoned lock must not lose the payload: the poller recovers it and
    /// keeps emitting later titles.
    #[test]
    fn poisoned_snapshot_lock_is_recovered() {
        let shared: SharedAgentTitle = Arc::new(Mutex::new(AgentTitleSnapshot::default()));
        let poisoner = shared.clone();
        let _ = std::thread::spawn(move || {
            let _guard = poisoner.lock().expect("lock before poisoning");
            panic!("poison the snapshot lock");
        })
        .join();

        assert!(shared.is_poisoned());
        let recovered = match shared.lock() {
            Ok(guard) => guard.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        };
        assert_eq!(recovered, AgentTitleSnapshot::default());
    }

    #[test]
    fn ring_buffer_trims_to_valid_utf8_boundary_and_tracks_offsets() {
        let mut ring = OutputRingBuffer::with_capacity(8);
        // "ab" (2 bytes) + "中文" (6 bytes: 3 + 3) = 8 bytes
        let (s1, e1) = ring.push("ab中文".as_bytes());
        assert_eq!((s1, e1), (0, 8));
        assert_eq!(ring.snapshot(), ("ab中文".to_string(), 8));

        // Push 2 more bytes ("cd") -> capacity 8 drops "ab" (2 bytes), keeping "中文cd"
        let (s2, e2) = ring.push(b"cd");
        assert_eq!((s2, e2), (8, 10));
        assert_eq!(ring.snapshot(), ("中文cd".to_string(), 10));

        // Push 1 more byte ("e") -> dropping 1 byte cuts inside '中', must advance past continuation bytes
        let (s3, e3) = ring.push(b"e");
        assert_eq!((s3, e3), (10, 11));
        assert_eq!(ring.snapshot(), ("文cde".to_string(), 11));
    }

    #[test]
    fn ring_buffer_handles_chunk_larger_than_capacity() {
        let mut ring = OutputRingBuffer::with_capacity(5);
        // "a" (1) + "你好" (6) -> tail 5 bytes cuts inside '你', leaving "好" (3 bytes)
        let (s, e) = ring.push("a你好".as_bytes());
        assert_eq!((s, e), (0, 7));
        assert_eq!(ring.snapshot(), ("好".to_string(), 7));
    }

    #[test]
    fn terminal_manager_list_has_non_empty_boot_id() {
        let mgr = TerminalManager::new();
        let list = mgr.list_terminals();
        assert!(!list.boot_id.is_empty());
        assert!(list.sessions.is_empty());
    }
}
