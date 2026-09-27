//! The user's speech-engine credentials, read straight off disk.
//!
//! The daemon stores a cloud provider's key in ``~/.synapse/stt.json`` and never
//! echoes it back over the wire (see ``synapse.stt.credentials``: the console may
//! *write* a key but never *read* one).  The desktop shell now owns the vendor
//! socket, so it reads that same file itself rather than asking the daemon for the
//! secret -- the key stays in this process and must never reach the webview, an
//! event, a log line or an error message.
//!
//! Path resolution mirrors ``synapse.settings.config_paths.user_config_dir``:
//! ``Path.home() / ".synapse"``, with no environment override.

use std::path::{Path, PathBuf};

use serde_json::Value;

/// Longest accepted key, mirroring ``MAX_API_KEY_CHARS`` in the Python module.
pub const MAX_API_KEY_CHARS: usize = 512;

/// The provider id the cloud engine is registered under (``stt.providers``).
pub const DOUBAO_PROVIDER_ID: &str = "doubao";

const SYNAPSE_DIRNAME: &str = ".synapse";
const STT_FILENAME: &str = "stt.json";

/// ``~`` as Python's ``Path.home()`` resolves it.
///
/// On Windows that is ``USERPROFILE`` and then ``HOMEDRIVE`` + ``HOMEPATH``; on
/// every other platform it is ``HOME``.  Matching Python exactly matters: a
/// mismatch would silently look at a different file than the one the console's
/// settings screen writes.
fn home_dir() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        if let Some(profile) = std::env::var_os("USERPROFILE") {
            if !profile.is_empty() {
                return Some(PathBuf::from(profile));
            }
        }
        let drive = std::env::var_os("HOMEDRIVE")?;
        let home_path = std::env::var_os("HOMEPATH")?;
        if drive.is_empty() || home_path.is_empty() {
            return None;
        }
        let mut combined = PathBuf::from(drive);
        combined.push(home_path);
        Some(combined)
    }
    #[cfg(not(windows))]
    {
        std::env::var_os("HOME")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
    }
}

/// The user-level speech config file (secret-bearing), or ``None`` without a home.
pub fn stt_config_path() -> Option<PathBuf> {
    home_dir().map(|home| home.join(SYNAPSE_DIRNAME).join(STT_FILENAME))
}

/// Why the stored credentials could not be used, in the reader's words.
///
/// Only the path and the parse failure are ever reported -- never a value.
fn read_document(path: &Path) -> Result<Value, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|err| format!("无法读取语音配置文件 {}: {err}", path.display()))?;
    let loaded: Value = serde_json::from_str(&text)
        .map_err(|err| format!("语音配置文件不是合法 JSON {}: {err}", path.display()))?;
    if !loaded.is_object() {
        return Err(format!(
            "语音配置文件必须是一个 JSON 对象: {}",
            path.display()
        ));
    }
    Ok(loaded)
}

/// The stored key for one provider, or ``None``.
///
/// An unreadable file means "no key configured" from the caller's point of view,
/// which is what the Python module does too: the console then reports the provider
/// as unusable with a reason instead of failing every status read until someone
/// edits the file by hand.
pub fn api_key_at(path: &Path, provider_id: &str) -> Option<String> {
    let document = read_document(path).ok()?;
    let providers = document.get("providers")?;
    let entry = providers.get(provider_id)?;
    let value = entry.get("api_key")?;
    let key = value.as_str()?;
    if key.is_empty() || key.chars().count() > MAX_API_KEY_CHARS {
        return None;
    }
    Some(key.to_string())
}

/// The stored key for one provider, read from the real user config file.
pub fn api_key(provider_id: &str) -> Option<String> {
    let path = stt_config_path()?;
    api_key_at(&path, provider_id)
}

/// Whether a usable key is stored, without reading it into the caller's hands.
pub fn configured(provider_id: &str) -> bool {
    api_key(provider_id).is_some()
}

/// The reason a stored key is unusable, for a status view.
///
/// Deliberately separate from [`api_key`]: a status read must be able to explain
/// itself without a secret ever entering the caller.
pub fn config_error() -> Option<String> {
    let path = stt_config_path()?;
    if !path.is_file() {
        return None;
    }
    read_document(&path).err()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A private directory per test, so no test can see another's file.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(label: &str) -> Self {
            let mut path = std::env::temp_dir();
            path.push(format!(
                "synapse-gui-stt-{label}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).expect("temp dir");
            Self(path)
        }

        fn file(&self) -> PathBuf {
            self.0.join(STT_FILENAME)
        }

        fn write(&self, body: &str) {
            std::fs::write(self.file(), body).expect("write config");
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn missing_file_is_no_key_rather_than_an_error() {
        let dir = TempDir::new("missing");
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), None);
    }

    #[test]
    fn reads_the_nested_provider_key() {
        let dir = TempDir::new("nested");
        dir.write(r#"{"providers": {"doubao": {"api_key": "sk-test-value"}}}"#);
        assert_eq!(
            api_key_at(&dir.file(), DOUBAO_PROVIDER_ID).as_deref(),
            Some("sk-test-value")
        );
    }

    #[test]
    fn another_providers_key_is_not_returned() {
        let dir = TempDir::new("other-provider");
        dir.write(r#"{"providers": {"local": {"api_key": "sk-other"}}}"#);
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), None);
    }

    #[test]
    fn malformed_json_is_refused_without_leaking_the_body() {
        let dir = TempDir::new("malformed");
        dir.write(r#"{"providers": {"doubao": {"api_key": "sk-secret""#);
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), None);
        let reason = config_error().unwrap_or_default();
        // config_error() reads the real home; only assert on the direct reader.
        assert!(!reason.contains("sk-secret"));
        let direct = read_document(&dir.file()).expect_err("malformed json is refused");
        assert!(direct.contains("不是合法 JSON"));
        assert!(!direct.contains("sk-secret"));
    }

    #[test]
    fn non_object_json_is_refused() {
        let dir = TempDir::new("array");
        dir.write("[]");
        let err = read_document(&dir.file()).expect_err("an array is not a config");
        assert!(err.contains("JSON 对象"));
    }

    #[test]
    fn empty_and_oversized_keys_are_not_usable() {
        let dir = TempDir::new("bounds");
        dir.write(r#"{"providers": {"doubao": {"api_key": ""}}}"#);
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), None);

        let oversized = "x".repeat(MAX_API_KEY_CHARS + 1);
        dir.write(&format!(
            r#"{{"providers": {{"doubao": {{"api_key": "{oversized}"}}}}}}"#
        ));
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), None);
    }

    #[test]
    fn a_key_at_the_bound_is_still_usable() {
        let dir = TempDir::new("at-bound");
        let key = "y".repeat(MAX_API_KEY_CHARS);
        dir.write(&format!(
            r#"{{"providers": {{"doubao": {{"api_key": "{key}"}}}}}}"#
        ));
        assert_eq!(api_key_at(&dir.file(), DOUBAO_PROVIDER_ID), Some(key));
    }

    #[test]
    fn the_config_path_matches_the_python_layout() {
        let path = stt_config_path().expect("a home directory exists in tests");
        assert!(path.ends_with(Path::new(SYNAPSE_DIRNAME).join(STT_FILENAME)));
    }
}
