fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "minimize_window",
            "toggle_maximize_window",
            "close_window",
            "get_console_url",
            "restart_service",
            "open_path",
            "tauri_git_status",
            "tauri_git_diff",
            "tauri_list_artifacts",
            "tauri_read_artifact",
            "tauri_open_path",
            "tauri_reveal_in_folder",
            "tauri_terminal_create",
            "tauri_terminal_write",
            "tauri_terminal_resize",
            "tauri_terminal_close",
        ]),
    ))
    .expect("failed to run tauri-build");
}
