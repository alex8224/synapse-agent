#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod git_fs;
mod sidecar;
mod terminal;

use sidecar::{ProcessManager, SharedProcessManager};
use terminal::TerminalManager;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WebviewWindow, WindowEvent,
};

#[derive(Clone)]
struct AppState {
    proc_mgr: SharedProcessManager,
    terminal_mgr: Arc<Mutex<TerminalManager>>,
}

/// Resolve the workspace one command operates on.
///
/// An explicit argument always wins; otherwise the auto-detection the GUI has
/// always used applies, and `.` stays the last resort.
fn command_workspace(workspace: Option<String>) -> PathBuf {
    workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Run one blocking command body on the async blocking pool and await it.
///
/// The chat transcript and the embedded terminal share this process, and Tauri
/// executes a non-async command on the main thread, so a body that waits on a
/// PTY write, a `git` subprocess or a file read freezes the whole window.
/// Awaiting such a body inline inside an `async` command would starve the async
/// runtime just as badly, so the body is handed to `spawn_blocking` and runs on
/// a dedicated thread while the command itself stays `async`.
///
/// Ordering: bodies that touch the same terminal never overlap, because the
/// caller awaits each IPC before sending the next keystroke and every body
/// holds the same `TerminalManager` mutex.
async fn run_blocking<T, F>(label: &'static str, task: F) -> Result<T, String>
where
    F: FnOnce() -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    tauri::async_runtime::spawn_blocking(task)
        .await
        // A panic in one body fails that command instead of the window.
        .map_err(|err| format!("{label}失败: {err}"))?
}

#[tauri::command]
async fn tauri_open_path(workspace: Option<String>, path: String) -> Result<(), String> {
    // `open::that` waits for the launcher process it spawns, so this blocks.
    run_blocking("打开路径", move || {
        let ws = command_workspace(workspace);
        let target = if std::path::Path::new(&path).is_absolute() {
            PathBuf::from(path)
        } else {
            ws.join(path)
        };
        git_fs::open_path(&target)
    })
    .await
}

#[tauri::command]
async fn tauri_reveal_in_folder(workspace: Option<String>, path: String) -> Result<(), String> {
    // The Windows path waits on `explorer`; the other platforms spawn a helper.
    run_blocking("在文件管理器中显示", move || {
        let ws = command_workspace(workspace);
        let target = if std::path::Path::new(&path).is_absolute() {
            PathBuf::from(path)
        } else {
            ws.join(path)
        };
        git_fs::reveal_in_explorer(&target)
    })
    .await
}

#[tauri::command]
async fn tauri_git_status(workspace: Option<String>) -> Result<git_fs::GitStatusView, String> {
    // `git status --porcelain` plus `git diff --numstat` shell out twice.
    run_blocking("读取 git 状态", move || {
        git_fs::get_git_status(&command_workspace(workspace))
    })
    .await
}

#[tauri::command]
async fn tauri_git_diff(
    workspace: Option<String>,
    path: String,
) -> Result<git_fs::GitDiffView, String> {
    run_blocking("读取 git 差异", move || {
        git_fs::get_git_diff(&command_workspace(workspace), &path)
    })
    .await
}

#[tauri::command]
async fn tauri_list_artifacts(
    workspace: Option<String>,
    subpath: Option<String>,
) -> Result<git_fs::ArtifactPage, String> {
    // A workspace listing walks the directory tree on disk.
    run_blocking("列出文件", move || {
        git_fs::list_artifacts(&command_workspace(workspace), subpath.as_deref())
    })
    .await
}

#[tauri::command]
async fn tauri_stat_artifact(
    workspace: Option<String>,
    path: String,
) -> Result<git_fs::ArtifactStat, String> {
    run_blocking("读取文件信息", move || {
        git_fs::stat_artifact(&command_workspace(workspace), &path)
    })
    .await
}

#[tauri::command]
async fn tauri_read_artifact(
    workspace: Option<String>,
    path: String,
    offset: Option<u64>,
    limit: Option<u64>,
) -> Result<git_fs::ArtifactChunk, String> {
    // A chunk is up to 1 MiB off disk, so it never runs on the UI thread.
    run_blocking("读取文件内容", move || {
        git_fs::read_artifact_chunk(
            &command_workspace(workspace),
            &path,
            offset.unwrap_or(0),
            limit.unwrap_or(git_fs::MAX_ARTIFACT_CHUNK_BYTES),
        )
    })
    .await
}

#[tauri::command]
fn minimize_window(window: WebviewWindow) {
    let _ = window.minimize();
}

#[tauri::command]
fn toggle_maximize_window(window: WebviewWindow) {
    if let Ok(is_max) = window.is_maximized() {
        if is_max {
            let _ = window.unmaximize();
        } else {
            let _ = window.maximize();
        }
    }
}

#[tauri::command]
fn close_window(window: WebviewWindow) {
    let _ = window.hide();
}

#[tauri::command]
fn open_path(path: String) -> Result<(), String> {
    open::that(&path).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_console_url(state: tauri::State<AppState>) -> Result<String, String> {
    let mgr = state.proc_mgr.lock().map_err(|e| e.to_string())?;
    mgr.current_url().ok_or_else(|| "控制台未就绪".into())
}

#[tauri::command]
fn restart_service(app: AppHandle, state: tauri::State<AppState>) -> Result<String, String> {
    do_restart_service(&app, &state)
}

fn do_restart_service(app: &AppHandle, state: &AppState) -> Result<String, String> {
    let url = {
        let mut mgr = state.proc_mgr.lock().map_err(|e| e.to_string())?;
        let meta = mgr.restart()?;
        meta.url
    };

    if let Some(window) = app.get_webview_window("main") {
        let eval_script = format!("window.location.replace('{}');", url);
        let _ = window.eval(&eval_script);
        let _ = window.show();
        let _ = window.set_focus();
    }
    Ok(url)
}

fn resolve_workspace() -> Option<PathBuf> {
    // Check CLI argument --workspace <path>
    let args: Vec<String> = std::env::args().collect();
    for i in 0..args.len() {
        if args[i] == "--workspace" && i + 1 < args.len() {
            let p = PathBuf::from(&args[i + 1]);
            if p.exists() {
                return Some(p);
            }
        }
    }

    // Check current_dir if it is not an install directory
    if let Ok(cwd) = std::env::current_dir() {
        let is_install_dir =
            cwd.join("synapse-gui.exe").exists() || cwd.join("uninstall.exe").exists();
        if !is_install_dir
            && (cwd.join("src").join("synapse").exists()
                || cwd.join("pyproject.toml").exists()
                || cwd.join(".synapse").exists()
                || cwd.join(".git").exists())
        {
            return Some(cwd);
        }
    }

    // Check current_exe parent hierarchies for source checkout root
    if let Ok(exe) = std::env::current_exe() {
        let mut cur = exe.parent();
        while let Some(dir) = cur {
            if dir.join("src").join("synapse").exists() || dir.join("pyproject.toml").exists() {
                return Some(dir.to_path_buf());
            }
            cur = dir.parent();
        }
    }

    None
}

fn resolve_static_dir() -> Option<PathBuf> {
    // Check CLI argument --static-dir <path> or -s <path>
    let args: Vec<String> = std::env::args().collect();
    for i in 0..args.len() {
        if (args[i] == "--static-dir" || args[i] == "-s") && i + 1 < args.len() {
            let p = PathBuf::from(&args[i + 1]);
            if p.exists() {
                return Some(p);
            }
        }
    }
    None
}


#[tauri::command]
async fn tauri_terminal_create(
    app: AppHandle,
    state: tauri::State<'_, AppState>,
    workspace: Option<String>,
    shell: Option<String>,
    cols: Option<u16>,
    rows: Option<u16>,
) -> Result<u32, String> {
    // The shared manager is cloned out of the borrowed state so the blocking
    // body owns everything it touches (`State` itself is not `'static`).
    let terminal_mgr = state.terminal_mgr.clone();
    let ws = workspace.map(PathBuf::from).or_else(resolve_workspace);
    run_blocking("创建终端", move || {
        let mut mgr = terminal_mgr.lock().map_err(|e| e.to_string())?;
        mgr.create_terminal(app, ws, shell, cols.unwrap_or(80), rows.unwrap_or(24))
    })
    .await
}

#[tauri::command]
async fn tauri_terminal_write(
    state: tauri::State<'_, AppState>,
    id: u32,
    data: String,
) -> Result<(), String> {
    // Writes for one terminal are serialised by the caller: it awaits each IPC
    // before sending the next keystroke, and every body takes the same mutex,
    // so the shell still sees the bytes in the order they were typed.
    let terminal_mgr = state.terminal_mgr.clone();
    run_blocking("终端写入", move || {
        let mut mgr = terminal_mgr.lock().map_err(|e| e.to_string())?;
        mgr.write_terminal(id, &data)
    })
    .await
}

#[tauri::command]
async fn tauri_terminal_resize(
    state: tauri::State<'_, AppState>,
    id: u32,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let terminal_mgr = state.terminal_mgr.clone();
    run_blocking("终端调整大小", move || {
        let mut mgr = terminal_mgr.lock().map_err(|e| e.to_string())?;
        mgr.resize_terminal(id, cols, rows)
    })
    .await
}

#[tauri::command]
async fn tauri_terminal_close(state: tauri::State<'_, AppState>, id: u32) -> Result<(), String> {
    // Closing kills the child process tree, which is a blocking syscall.
    let terminal_mgr = state.terminal_mgr.clone();
    run_blocking("关闭终端", move || {
        let mut mgr = terminal_mgr.lock().map_err(|e| e.to_string())?;
        mgr.close_terminal(id)
    })
    .await
}

fn main() {
    let workspace = resolve_workspace();
    let static_dir = resolve_static_dir();
    let proc_mgr = Arc::new(Mutex::new(ProcessManager::new(workspace, static_dir)));
    let state = AppState {
        proc_mgr: proc_mgr.clone(),
        terminal_mgr: Arc::new(Mutex::new(TerminalManager::new())),
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            } else if let Some(splash) = app.get_webview_window("splash") {
                let _ = splash.show();
                let _ = splash.set_focus();
            }
        }))
        .manage(state.clone())
        .invoke_handler(tauri::generate_handler![
            minimize_window,
            toggle_maximize_window,
            close_window,
            get_console_url,
            restart_service,
            open_path,
            tauri_git_status,
            tauri_git_diff,
            tauri_list_artifacts,
            tauri_stat_artifact,
            tauri_read_artifact,
            tauri_open_path,
            tauri_reveal_in_folder,
            tauri_terminal_create,
            tauri_terminal_write,
            tauri_terminal_resize,
            tauri_terminal_close
        ])
        .setup(move |app| {
            let handle = app.handle().clone();

            // Apply native vibrancy effect to the compact splash window on macOS
            if let Some(_splash) = app.get_webview_window("splash") {
                #[cfg(target_os = "windows")]
                {
                }
                #[cfg(target_os = "macos")]
                {
                    let _ = window_vibrancy::apply_vibrancy(
                        &_splash,
                        window_vibrancy::NSVisualEffectMaterial::HudWindow,
                        None,
                        None,
                    );
                }
            }

            // Set up system tray menu
            let show_item = MenuItem::with_id(
                &handle,
                "show_hide",
                "显示 / 隐藏控制台",
                true,
                None::<&str>,
            )?;
            let restart_item =
                MenuItem::with_id(&handle, "restart", "重启后台服务", true, None::<&str>)?;
            let copy_item =
                MenuItem::with_id(&handle, "copy_url", "复制访问地址", true, None::<&str>)?;
            let logs_item =
                MenuItem::with_id(&handle, "open_logs", "打开日志目录", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(&handle)?;
            let quit_item = MenuItem::with_id(&handle, "quit", "退出 Synapse", true, None::<&str>)?;

            let menu = Menu::with_items(
                &handle,
                &[
                    &show_item,
                    &restart_item,
                    &copy_item,
                    &logs_item,
                    &sep,
                    &quit_item,
                ],
            )?;

            let tray_icon = tauri::image::Image::from_bytes(include_bytes!("../icons/32x32.png"))
                .or_else(|_| tauri::image::Image::from_bytes(include_bytes!("../icons/icon.png")))
                .ok()
                .or_else(|| app.default_window_icon().cloned());

            let mut tray_builder = TrayIconBuilder::new()
                .tooltip("Synapse 控制台")
                .menu(&menu)
                .show_menu_on_left_click(false);

            if let Some(icon) = tray_icon {
                tray_builder = tray_builder.icon(icon);
            }

            let _tray = tray_builder
                .on_menu_event(move |app, event| {
                    let id = event.id().as_ref();
                    match id {
                        "show_hide" => {
                            if let Some(window) = app.get_webview_window("main") {
                                if window.is_visible().unwrap_or(false) {
                                    let _ = window.hide();
                                } else {
                                    let _ = window.show();
                                    let _ = window.set_focus();
                                }
                            }
                        }
                        "restart" => {
                            let state = app.state::<AppState>().inner().clone();
                            let app_handle = app.app_handle().clone();
                            std::thread::spawn(move || {
                                let _ = do_restart_service(&app_handle, &state);
                            });
                        }
                        "copy_url" => {
                            let url_opt = {
                                let mgr_arc = app.state::<AppState>().proc_mgr.clone();
                                mgr_arc.lock().ok().and_then(|m| m.current_url())
                            };
                            if let Some(url) = url_opt {
                                #[cfg(windows)]
                                {
                                    let _ = std::process::Command::new("powershell")
                                        .args([
                                            "-NoProfile",
                                            "-Command",
                                            &format!("Set-Clipboard -Value '{}'", url),
                                        ])
                                        .output();
                                }
                            }
                        }
                        "open_logs" => {
                            let log_dir = dirs_or_local();
                            let _ = open::that(log_dir);
                        }
                        "quit" => {
                            let mgr_arc = {
                                let state = app.state::<AppState>();
                                state.proc_mgr.clone()
                            };
                            if let Ok(mut mgr) = mgr_arc.lock() {
                                mgr.stop();
                            }
                            app.exit(0);
                        }
                        _ => {}
                    }
                })
                .on_tray_icon_event(move |tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            if window.is_visible().unwrap_or(false) {
                                let _ = window.hide();
                            } else {
                                let _ = window.show();
                                let _ = window.set_focus();
                            }
                        }
                    }
                })
                .build(&handle)?;

            // Background task: start sidecar and navigate window
            let bg_handle = handle.clone();
            let bg_proc = proc_mgr.clone();
            std::thread::spawn(move || {
                let start_res = {
                    let mut mgr = bg_proc.lock().unwrap();
                    mgr.start()
                };

                match start_res {
                    Ok(meta) => {
                        if let Some(window) = bg_handle.get_webview_window("main") {
                            let eval_script = format!("window.location.replace('{}');", meta.url);
                            let _ = window.eval(&eval_script);
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                        if let Some(splash) = bg_handle.get_webview_window("splash") {
                            let _ = splash.close();
                        }
                    }
                    Err(err) => {
                        eprintln!("Synapse GUI 启动后台失败: {err}");
                        if let Some(splash) = bg_handle.get_webview_window("splash") {
                            let msg_json = serde_json::to_string(&err).unwrap_or_default();
                            let script = format!(
                                "const el = document.querySelector('.sub'); if (el) {{ el.textContent = '启动失败: ' + {}; el.style.color = '#ef4444'; }}",
                                msg_json
                            );
                            let _ = splash.eval(&script);
                        }
                    }
                }
            });

            // Prevent window close from killing app - hide to tray instead
            if let Some(window) = app.get_webview_window("main") {
                let w_clone = window.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = w_clone.hide();
                    }
                });
            }

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("运行 Synapse GUI 失败");
}

fn dirs_or_local() -> PathBuf {
    #[cfg(windows)]
    {
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            let p = PathBuf::from(local).join("Synapse").join("web-console");
            if p.exists() {
                return p;
            }
        }
    }
    if let Ok(home) = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")) {
        return PathBuf::from(home).join(".synapse");
    }
    PathBuf::from(".")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::ThreadId;

    /// The blocking body must not run on the caller's thread: that hop is what
    /// keeps one slow `git` call or PTY write from freezing chat rendering.
    #[test]
    fn blocking_bodies_run_off_the_calling_thread() {
        let caller: ThreadId = std::thread::current().id();
        let worker = tauri::async_runtime::block_on(run_blocking("测试", || {
            Ok(std::thread::current().id())
        }))
        .expect("blocking task must succeed");
        assert_ne!(caller, worker);
    }

    /// The IPC contract of every offloaded command is unchanged: the body's own
    /// `Ok` value and `Err` string still reach the caller verbatim.
    #[test]
    fn blocking_results_and_errors_round_trip_unchanged() {
        let value = tauri::async_runtime::block_on(run_blocking("测试", || Ok(7u32)))
            .expect("blocking task must succeed");
        assert_eq!(value, 7);

        let err = tauri::async_runtime::block_on(run_blocking("测试", || {
            Err::<u32, String>("终端会话 1 不存在".to_string())
        }))
        .expect_err("the body error must be reported");
        assert_eq!(err, "终端会话 1 不存在");
    }

    /// A panicking body must fail that one command: the caller still receives an
    /// error string instead of a response that never arrives.
    #[test]
    fn a_panicking_body_becomes_an_error_string() {
        let err = tauri::async_runtime::block_on(run_blocking::<u32, _>("读取文件", || {
            panic!("boom")
        }))
        .expect_err("a panicking body must be reported as an error");
        assert!(err.starts_with("读取文件"), "unexpected error: {err}");
    }

    /// An explicit workspace argument is used as given, exactly like before the
    /// commands became async.
    #[test]
    fn an_explicit_workspace_is_used_verbatim() {
        let resolved = command_workspace(Some("a/b".into()));
        assert_eq!(resolved, PathBuf::from("a/b"));
        assert!(resolved.is_relative(), "the argument must not be resolved");
    }
}
