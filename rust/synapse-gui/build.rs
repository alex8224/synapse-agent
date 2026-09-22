fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "minimize_window",
                "toggle_maximize_window",
                "close_window",
                "get_console_url",
                "restart_service",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
