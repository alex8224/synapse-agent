#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod git_fs;
mod sidecar;

use sidecar::{ProcessManager, SharedProcessManager};
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
}

#[tauri::command]
fn tauri_open_path(workspace: Option<String>, path: String) -> Result<(), String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    let target = if std::path::Path::new(&path).is_absolute() {
        PathBuf::from(path)
    } else {
        ws.join(path)
    };
    git_fs::open_path(&target)
}

#[tauri::command]
fn tauri_reveal_in_folder(workspace: Option<String>, path: String) -> Result<(), String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    let target = if std::path::Path::new(&path).is_absolute() {
        PathBuf::from(path)
    } else {
        ws.join(path)
    };
    git_fs::reveal_in_explorer(&target)
}

#[tauri::command]
fn tauri_git_status(workspace: Option<String>) -> Result<git_fs::GitStatusView, String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    git_fs::get_git_status(&ws)
}

#[tauri::command]
fn tauri_git_diff(workspace: Option<String>, path: String) -> Result<git_fs::GitDiffView, String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    git_fs::get_git_diff(&ws, &path)
}

#[tauri::command]
fn tauri_list_artifacts(
    workspace: Option<String>,
    subpath: Option<String>,
) -> Result<git_fs::ArtifactPage, String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    git_fs::list_artifacts(&ws, subpath.as_deref())
}

#[tauri::command]
fn tauri_read_artifact(
    workspace: Option<String>,
    path: String,
) -> Result<git_fs::ArtifactContent, String> {
    let ws = workspace
        .map(PathBuf::from)
        .or_else(resolve_workspace)
        .unwrap_or_else(|| PathBuf::from("."));
    git_fs::read_artifact(&ws, &path)
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

fn main() {
    let workspace = resolve_workspace();
    let proc_mgr = Arc::new(Mutex::new(ProcessManager::new(workspace)));
    let state = AppState {
        proc_mgr: proc_mgr.clone(),
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
            tauri_read_artifact,
            tauri_open_path,
            tauri_reveal_in_folder
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
