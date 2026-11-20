//! Host-private conversation title lookup for the Antigravity CLI (`agy`).
//!
//! `agy` records a human readable title for every conversation it starts in
//! `~/.gemini/antigravity-cli/conversation_summaries.db`
//! (`conversation_summaries(conversation_id, title)`). The terminal panel wants
//! to show that title next to a PTY, which is only possible on the Rust side:
//! the database lives outside the webview's world, and exposing an arbitrary
//! path reading IPC would hand the console a filesystem primitive it must never
//! have.
//!
//! This module is therefore deliberately narrow:
//!
//! * The database path is resolved from the home directory only; no caller
//!   supplied path ever reaches SQLite.
//! * The connection is opened read-only and every query is parameterized.
//! * The connection is opened per lookup and dropped immediately, so a running
//!   `agy` can keep writing, and so this crate holds no long lived file handle.
//! * Every failure (missing file, missing directory, missing table, busy or
//!   locked database, garbage file) is reported as `None`. The terminal panel
//!   simply keeps showing the conversation ID fallback.
//!
//! The lookup itself is driven by the terminal PTY poller (`terminal.rs`) on a
//! bounded cadence; this module performs one cheap primary-key lookup per call.

use std::path::PathBuf;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

/// `agy` stores its CLI state under the Gemini home directory.
const ANTIGRAVITY_SUBDIR: &str = "antigravity-cli";
const SUMMARIES_DB_NAME: &str = "conversation_summaries.db";

/// `SQLITE_BUSY` retry budget. `agy` writes while we read, so a transient
/// `database is locked` must not be mistaken for "this conversation has no
/// title". The budget is tiny on purpose: the caller retries later anyway.
const BUSY_TIMEOUT: Duration = Duration::from_millis(250);

const TITLE_QUERY: &str =
    "SELECT title FROM conversation_summaries WHERE conversation_id = ?1 LIMIT 1";

/// Resolve `~/.gemini/antigravity-cli/conversation_summaries.db`.
///
/// `Err` carries a stable reason for tests and diagnostics; it is never logged
/// with the resolved path. Windows uses `USERPROFILE`, every other platform
/// uses `HOME`.
pub(crate) fn summaries_db_path() -> Result<PathBuf, &'static str> {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .filter(|value| !value.is_empty())
        .ok_or("home directory is not set")?;
    Ok(PathBuf::from(home)
        .join(".gemini")
        .join(ANTIGRAVITY_SUBDIR)
        .join(SUMMARIES_DB_NAME))
}

/// Normalize a conversation ID coming from the hook payload.
///
/// `agy` hook payloads carry the ID as an opaque string; an empty or
/// whitespace-only value means "no conversation yet" and must not trigger a
/// query (the PTY starts before the first `SessionStart`).
pub(crate) fn normalize_conversation_id(raw: &str) -> Option<&str> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

/// Read the stored title for one conversation ID.
///
/// Returns `Ok(None)` for "no row" and for every degraded condition listed in
/// the module docs, so callers can treat `None` as "keep the current fallback".
pub(crate) fn lookup_title(db_path: &std::path::Path, conversation_id: &str) -> Option<String> {
    let conversation_id = normalize_conversation_id(conversation_id)?;

    // `SQLITE_OPEN_NO_MUTEX` is correct here: the connection is created and
    // dropped inside this function and never shared between threads.
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = Connection::open_with_flags(db_path, flags).ok()?;
    let _ = conn.busy_timeout(BUSY_TIMEOUT);

    let title = conn
        .query_row(TITLE_QUERY, [conversation_id], |row| {
            row.get::<_, Option<String>>(0)
        })
        .ok()??;

    let trimmed = title.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Convenience wrapper that resolves the default database path first.
pub(crate) fn lookup_default_title(conversation_id: &str) -> Option<String> {
    let db_path = summaries_db_path().ok()?;
    lookup_title(&db_path, conversation_id)
}

/// Injection point for the PTY poller: maps a conversation ID to a title.
///
/// Production passes [`lookup_default_title`]; tests pass a closure so the
/// poller and the emit de-duplication can be exercised without a real `agy`
/// installation.
pub(crate) type TitleLookup = fn(&str) -> Option<String>;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    /// Build a temporary directory without pulling in a temp-file crate; the
    /// guard removes the tree on drop so a failing test leaves nothing behind.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(label: &str) -> Self {
            let mut dir = std::env::temp_dir();
            let unique = format!(
                "synapse-agent-title-{}-{}-{}",
                label,
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_nanos())
                    .unwrap_or(0)
            );
            dir.push(unique);
            std::fs::create_dir_all(&dir).expect("create temp dir");
            Self(dir)
        }

        fn join(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// Create a database shaped exactly like the subset of the `agy` schema we
    /// read, plus one row.
    fn seed_db(path: &Path, rows: &[(&str, &str)]) {
        let conn = Connection::open(path).expect("open writable");
        conn.execute_batch(
            "CREATE TABLE conversation_summaries (
                 conversation_id TEXT PRIMARY KEY,
                 title TEXT
             );",
        )
        .expect("create table");
        for (id, title) in rows {
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, title) VALUES (?1, ?2)",
                [*id, *title],
            )
            .expect("insert row");
        }
    }

    #[test]
    fn normalize_rejects_blank_ids() {
        assert_eq!(normalize_conversation_id("  abc-123  "), Some("abc-123"));
        assert_eq!(normalize_conversation_id(""), None);
        assert_eq!(normalize_conversation_id("   "), None);
    }

    #[test]
    fn reads_exact_conversation_title() {
        let dir = TempDir::new("hit");
        let db = dir.join("conversation_summaries.db");
        seed_db(
            &db,
            &[("conv-a", "Fix the PTY bridge"), ("conv-b", "另一个会话")],
        );

        assert_eq!(
            lookup_title(&db, "conv-a").as_deref(),
            Some("Fix the PTY bridge")
        );
        assert_eq!(lookup_title(&db, "conv-b").as_deref(), Some("另一个会话"));
    }

    #[test]
    fn exact_match_only_never_substring() {
        let dir = TempDir::new("exact");
        let db = dir.join("conversation_summaries.db");
        seed_db(&db, &[("conv-a", "Alpha")]);

        assert_eq!(lookup_title(&db, "conv"), None);
        assert_eq!(lookup_title(&db, "conv-a-suffix"), None);
        assert_eq!(lookup_title(&db, "CONV-A"), None);
    }

    #[test]
    fn parameterized_query_is_injection_safe() {
        let dir = TempDir::new("inject");
        let db = dir.join("conversation_summaries.db");
        seed_db(&db, &[("conv-a", "Alpha")]);

        // A quote-laden "ID" must be bound as a value, not spliced into SQL.
        assert_eq!(lookup_title(&db, "conv-a' OR '1'='1"), None);
        assert_eq!(
            lookup_title(&db, "'; DROP TABLE conversation_summaries; --"),
            None
        );
        // The table still answers after the injection attempts.
        assert_eq!(lookup_title(&db, "conv-a").as_deref(), Some("Alpha"));
    }

    #[test]
    fn blank_title_is_treated_as_missing() {
        let dir = TempDir::new("blank");
        let db = dir.join("conversation_summaries.db");
        seed_db(&db, &[("conv-empty", "   ")]);

        assert_eq!(lookup_title(&db, "conv-empty"), None);
    }

    #[test]
    fn mismatched_schema_degrades_to_none() {
        let dir = TempDir::new("schema");
        let db = dir.join("conversation_summaries.db");
        let conn = Connection::open(&db).expect("open writable");
        conn.execute_batch("CREATE TABLE unrelated (id TEXT);")
            .expect("create table");
        drop(conn);

        assert_eq!(lookup_title(&db, "conv-a"), None);
    }

    #[test]
    fn missing_file_degrades_to_none() {
        let dir = TempDir::new("missing");
        let db = dir.join("conversation_summaries.db");
        assert!(!db.exists());

        assert_eq!(lookup_title(&db, "conv-a"), None);
    }

    #[test]
    fn missing_directory_degrades_to_none() {
        let dir = TempDir::new("nodir");
        let db = dir.join("nested").join("conversation_summaries.db");

        assert_eq!(lookup_title(&db, "conv-a"), None);
    }

    #[test]
    fn garbage_file_degrades_to_none() {
        let dir = TempDir::new("garbage");
        let db = dir.join("conversation_summaries.db");
        std::fs::write(&db, b"this is not a sqlite database").expect("write garbage");

        assert_eq!(lookup_title(&db, "conv-a"), None);
    }

    #[test]
    fn read_only_connection_does_not_mutate_the_database() {
        let dir = TempDir::new("readonly");
        let db = dir.join("conversation_summaries.db");
        seed_db(&db, &[("conv-a", "Alpha")]);
        let before = std::fs::metadata(&db).expect("stat").len();

        assert_eq!(lookup_title(&db, "conv-a").as_deref(), Some("Alpha"));
        let after = std::fs::metadata(&db).expect("stat").len();
        assert_eq!(before, after);
    }

    #[test]
    fn summaries_db_path_points_into_the_agy_state_dir() {
        let path = summaries_db_path().expect("home is set in tests");
        assert!(path.ends_with(
            Path::new(".gemini")
                .join(ANTIGRAVITY_SUBDIR)
                .join(SUMMARIES_DB_NAME)
        ));
    }
}
