use base64::Engine as _;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitFileChangeView {
    pub path: String,
    pub index_status: String,
    pub worktree_status: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_list_artifacts_current_dir() {
        let cwd = std::env::current_dir().unwrap();
        let page = list_artifacts(&cwd, None).expect("should list workspace dir");
        assert!(page.total > 0, "workspace directory should not be empty");
        assert!(page.entries.iter().any(|e| e.name == "Cargo.toml"));
    }

    /// A fresh, empty workspace directory private to one test.
    fn temp_workspace(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("synapse-gui-git-fs-{name}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn test_list_artifacts_walks_into_gitignored_build_output() {
        let dir = temp_workspace("nested");
        fs::create_dir_all(dir.join("rust/gui/target/debug")).unwrap();
        fs::write(dir.join("rust/gui/target/debug/app.exe"), b"bin").unwrap();
        // A `.gitignore` hiding the build output must not hide it from the
        // desktop tree: the native surface lists what is on disk.
        fs::write(dir.join(".gitignore"), "**/target/\n").unwrap();

        let top = list_artifacts(&dir, None).unwrap();
        assert!(
            top.entries.iter().any(|e| e.path == "rust"),
            "the workspace root must list the source tree"
        );

        let gui = list_artifacts(&dir, Some("rust/gui")).unwrap();
        assert!(
            gui.entries.iter().any(|e| e.path == "rust/gui/target"),
            "a gitignored build directory must still be listed"
        );

        let target = list_artifacts(&dir, Some("rust/gui/target")).unwrap();
        assert_eq!(target.entries.len(), 1);
        assert_eq!(target.entries[0].path, "rust/gui/target/debug");
        assert!(target.entries[0].is_dir);

        let debug = list_artifacts(&dir, Some("rust/gui/target/debug")).unwrap();
        assert_eq!(debug.entries.len(), 1);
        assert_eq!(debug.entries[0].path, "rust/gui/target/debug/app.exe");
        assert!(!debug.entries[0].is_dir);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_stat_and_chunked_read_stay_bounded() {
        let dir = temp_workspace("chunked");
        let payload: Vec<u8> = (0..(MAX_ARTIFACT_CHUNK_BYTES as usize * 2 + 7))
            .map(|index| (index % 251) as u8)
            .collect();
        fs::write(dir.join("big.bin"), &payload).unwrap();

        let stat = stat_artifact(&dir, "big.bin").expect("stat must succeed");
        assert!(!stat.is_dir);
        assert_eq!(stat.size, payload.len() as u64);
        assert!(stat.revision.is_some(), "a file carries a read fingerprint");

        // An oversized request is clamped instead of pulling the whole file.
        let first = read_artifact_chunk(&dir, "big.bin", 0, u64::MAX).unwrap();
        assert_eq!(first.byte_length, MAX_ARTIFACT_CHUNK_BYTES);
        assert_eq!(first.next_offset, MAX_ARTIFACT_CHUNK_BYTES);
        assert!(!first.eof);
        assert_eq!(first.revision, stat.revision);
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(&first.data_base64)
            .unwrap();
        assert_eq!(decoded.len(), first.byte_length as usize);
        assert_eq!(decoded[..], payload[..first.byte_length as usize]);

        // Walking the file chunk by chunk ends exactly at its size, and no chunk
        // ever exceeds the cap.
        let mut offset = 0u64;
        let mut seen = 0usize;
        loop {
            let chunk = read_artifact_chunk(&dir, "big.bin", offset, u64::MAX).unwrap();
            assert_eq!(chunk.offset, offset);
            assert!(chunk.byte_length <= MAX_ARTIFACT_CHUNK_BYTES);
            seen += chunk.byte_length as usize;
            if chunk.eof {
                assert_eq!(chunk.next_offset, payload.len() as u64);
                break;
            }
            assert!(chunk.next_offset > offset, "a read must advance");
            offset = chunk.next_offset;
        }
        assert_eq!(seen, payload.len());

        // Past the end is an error, never a silent empty chunk.
        assert!(read_artifact_chunk(&dir, "big.bin", payload.len() as u64 + 1, 16).is_err());
        // A directory is not a readable artifact.
        assert!(read_artifact_chunk(&dir, ".", 0, 16).is_err());

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_artifact_paths_cannot_escape_the_workspace() {
        let dir = temp_workspace("escape");
        assert!(stat_artifact(&dir, "../outside.txt").is_err());
        assert!(read_artifact_chunk(&dir, "..\\outside.txt", 0, 16).is_err());
        assert!(list_artifacts(&dir, Some("a/../../b")).is_err());
        // A segment carrying a drive prefix would make `PathBuf::push` replace the
        // whole path, so it is refused instead of re-rooting the read.
        assert!(stat_artifact(&dir, "a/C:/outside.txt").is_err());
        assert!(read_artifact_chunk(&dir, "C:outside.txt", 0, 16).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_a_name_with_spaces_round_trips_untrimmed() {
        let dir = temp_workspace("spaces");
        fs::write(dir.join(" padded.txt"), b"padded").unwrap();

        let listed = list_artifacts(&dir, None).unwrap();
        let entry = listed
            .entries
            .iter()
            .find(|e| e.name == " padded.txt")
            .expect("the listing keeps the name verbatim");

        // The echoed path is the listed path: the caller compares the two (the
        // image loader refuses a chunk whose path is not the one it asked for).
        let stat = stat_artifact(&dir, &entry.path).expect("stat must resolve it");
        assert_eq!(stat.path, entry.path);
        assert_eq!(stat.size, 6);
        let chunk = read_artifact_chunk(&dir, &entry.path, 0, 16).unwrap();
        assert_eq!(chunk.path, entry.path);
        assert!(chunk.eof);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_git_status_current_repo() {
        let mut repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        repo.pop();
        repo.pop(); // root of synapse git repo
        let st = get_git_status(&repo).expect("git status should succeed on synapse repo");
        assert!(st.branch.is_some(), "should detect git branch");
    }

    /// Run one git command in a test repository, panicking on failure.
    fn git_in(dir: &Path, args: &[&str]) -> String {
        let output = Command::new("git")
            .current_dir(dir)
            .args(args)
            .output()
            .expect("git must be runnable");
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8_lossy(&output.stdout).to_string()
    }

    /// A throwaway repository with three commits, a tag, a rename and a stash,
    /// so the history readers are exercised without depending on this checkout.
    fn seeded_repo(name: &str) -> PathBuf {
        let dir = temp_workspace(name);
        git_in(&dir, &["init", "-b", "main"]);
        git_in(&dir, &["config", "user.name", "Test"]);
        git_in(&dir, &["config", "user.email", "test@example.com"]);
        fs::write(dir.join("first.txt"), "one\n").unwrap();
        git_in(&dir, &["add", "."]);
        git_in(&dir, &["commit", "-m", "first commit"]);
        fs::write(dir.join("first.txt"), "one\ntwo\n").unwrap();
        fs::write(dir.join("second.txt"), "second\n").unwrap();
        git_in(&dir, &["add", "."]);
        git_in(&dir, &["commit", "-m", "second commit"]);
        git_in(&dir, &["tag", "v1"]);
        git_in(&dir, &["mv", "second.txt", "renamed.txt"]);
        git_in(&dir, &["commit", "-m", "rename second"]);
        fs::write(dir.join("first.txt"), "one\ntwo\nthree\n").unwrap();
        git_in(&dir, &["stash", "push", "-m", "work in progress"]);
        dir
    }

    #[test]
    fn test_refs_read_branches_tags_stashes_and_worktrees() {
        let dir = seeded_repo("refs");
        let refs = get_git_refs(&dir).expect("refs must read");
        assert_eq!(refs.current.as_deref(), Some("main"));
        assert!(!refs.truncated);

        let main = refs
            .branches
            .iter()
            .find(|branch| branch.name == "main")
            .expect("the local branch must be listed");
        assert!(main.is_head);
        assert_eq!(main.kind, "local");
        assert_eq!(main.tip_subject, "rename second");
        assert_eq!(main.tip_sha.len(), 7);
        // The upstream is unset here, so no ahead/behind is claimed.
        assert!(main.upstream.is_none());
        assert_eq!((main.ahead, main.behind), (0, 0));

        let tag = refs
            .tags
            .iter()
            .find(|tag| tag.name == "v1")
            .expect("the tag must be listed");
        assert!(!tag.annotated, "`git tag v1` is a lightweight tag");
        // The tag was made before the rename commit, so it points at the commit
        // before the branch tip -- a tag is not the tip by definition.
        let log = get_git_log(&dir, None, 5, 0, false, None).unwrap();
        assert_eq!(tag.target_sha, log.commits[1].short_sha);
        assert_ne!(tag.target_sha, main.tip_sha);

        assert_eq!(refs.stashes.len(), 1);
        assert_eq!(refs.stashes[0].index, 0);
        assert_eq!(refs.stashes[0].name, "stash@{0}");
        assert!(refs.stashes[0].message.contains("work in progress"));

        assert_eq!(refs.worktrees.len(), 1);
        assert!(refs.worktrees[0].is_main);
        assert_eq!(refs.worktrees[0].branch.as_deref(), Some("main"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_log_pages_and_reports_more() {
        let dir = seeded_repo("log");
        let page = get_git_log(&dir, None, 2, 0, false, None).expect("log must read");
        assert_eq!(page.rev, "HEAD");
        assert_eq!(page.commits.len(), 2);
        assert!(
            page.more,
            "a third commit exists, so the page is not the end"
        );
        assert_eq!(page.commits[0].subject, "rename second");
        assert_eq!(page.commits[0].parents.len(), 1);

        let whole = get_git_log(&dir, None, 5, 0, false, None).unwrap();
        assert_eq!(whole.commits.len(), 3);
        assert!(!whole.more);

        // A page past the end is empty, not an error.
        let past_end = get_git_log(&dir, None, 5, 9, false, None).unwrap();
        assert!(past_end.commits.is_empty());

        // A path filter keeps only the commits that touched that path.
        let filtered = get_git_log(&dir, None, 5, 0, false, Some("first.txt")).unwrap();
        assert_eq!(filtered.commits.len(), 2);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_commit_detail_lists_files_and_one_diff() {
        let dir = seeded_repo("detail");
        let log = get_git_log(&dir, None, 5, 0, false, None).unwrap();
        let head_sha = log.commits[0].sha.clone();

        let detail = get_git_commit(&dir, &head_sha, None).expect("detail must read");
        assert_eq!(detail.sha, head_sha);
        assert_eq!(detail.subject, "rename second");
        assert!(
            detail.diff.is_none(),
            "no path was asked for, so no diff is read"
        );
        assert_eq!(detail.files.len(), 1);
        let renamed = &detail.files[0];
        assert_eq!(renamed.status, "R100");
        assert_eq!(renamed.path, "renamed.txt");
        assert_eq!(renamed.old_path.as_deref(), Some("second.txt"));
        assert_eq!(renamed.insertions, Some(0));
        assert_eq!(renamed.deletions, Some(0));
        assert!(!renamed.binary);

        // The first commit has no parent: it is compared against the empty tree.
        let root_sha = log.commits.last().unwrap().sha.clone();
        let root = get_git_commit(&dir, &root_sha, Some("first.txt")).unwrap();
        assert!(root.parents.is_empty());
        assert_eq!(root.files.len(), 1);
        assert_eq!(root.files[0].status, "A");
        assert_eq!(root.files[0].insertions, Some(1));
        let diff = root.diff.expect("the asked-for path's diff must be read");
        assert!(!diff.empty);
        assert!(diff.text.contains("+one"));
        assert!(!diff.binary);
        assert!(!diff.truncated);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_stash_diff_is_read_without_applying_it() {
        let dir = seeded_repo("stash");
        let diff = get_git_stash_diff(&dir, 0).expect("the stash must be readable");
        assert_eq!(diff.path, "stash@{0}");
        assert!(!diff.empty);
        assert!(diff.text.contains("+three"));
        assert!(!diff.binary);
        // Reading it must not have popped it: the entry is still there.
        assert_eq!(get_git_refs(&dir).unwrap().stashes.len(), 1);
        // A stash that does not exist is an error, never a fabricated empty diff.
        assert!(get_git_stash_diff(&dir, 7).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_an_unborn_repository_has_an_empty_log() {
        let dir = temp_workspace("unborn");
        git_in(&dir, &["init", "-b", "main"]);
        let log =
            get_git_log(&dir, None, 10, 0, false, None).expect("an unborn HEAD is not a failure");
        assert!(log.commits.is_empty());
        assert!(!log.more);
        // A revision that does not exist is still reported.
        assert!(get_git_log(&dir, Some("nope"), 10, 0, false, None).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_revisions_and_paths_cannot_smuggle_options() {
        for bad in [
            "",
            "--upload-pack=x",
            "-n",
            "HEAD~1",
            "a..b",
            "a@{1}",
            "/main",
            ".hidden",
            "main.",
            "a b",
            "a;b",
            "refs/heads/../x",
        ] {
            assert!(validate_git_rev(bad).is_err(), "{bad} must be refused");
        }
        for good in [
            "main",
            "HEAD",
            "v1",
            "feature/right-panel",
            "abc1234",
            "origin/main",
            "release-1.0",
        ] {
            assert!(validate_git_rev(good).is_ok(), "{good} must be accepted");
        }
        for bad in [
            "",
            "/abs.txt",
            "..\\win.txt",
            "a/../../b",
            "-x",
            "a/C:/x.txt",
            "C:x.txt",
            "a//b",
            "a/./b",
        ] {
            assert!(validate_git_path(bad).is_err(), "{bad} must be refused");
        }
        for good in ["a.txt", "src/synapse/app/agent.py", "a b/c.txt"] {
            assert!(validate_git_path(good).is_ok(), "{good} must be accepted");
        }
    }

    #[test]
    fn test_numstat_and_name_status_parsers() {
        let numstat = "40\t0\ttests/a.py\0".to_string()
            + "0\t0\t\0old/a.rs\0new/a.rs\0"
            + "-\t-\tassets/logo.png\0";
        let counts = parse_numstat(&numstat);
        assert_eq!(counts.get("tests/a.py"), Some(&(Some(40), Some(0), false)));
        assert_eq!(counts.get("new/a.rs"), Some(&(Some(0), Some(0), false)));
        assert_eq!(counts.get("assets/logo.png"), Some(&(None, None, true)));
        let status = "M\0tests/a.py\0R100\0old/a.rs\0new/a.rs\0A\0added.txt\0";
        let records = parse_name_status(status);
        assert_eq!(records.len(), 3);
        assert_eq!(records[0].0, "M");
        assert_eq!(records[0].1, "tests/a.py");
        assert!(records[0].2.is_none());
        assert_eq!(records[1].0, "R100");
        assert_eq!(records[1].1, "new/a.rs");
        assert_eq!(records[1].2.as_deref(), Some("old/a.rs"));
        assert_eq!(records[2].0, "A");
    }

    #[test]
    fn test_cap_text_stays_on_a_character_boundary() {
        let mut text = "中文中文".to_string();
        assert!(
            cap_text(&mut text, 7),
            "7 bytes lands inside the third character"
        );
        assert_eq!(text, "中文");
        let mut fits = "abc".to_string();
        assert!(!cap_text(&mut fits, 3));
        assert_eq!(fits, "abc");
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitStatusView {
    pub branch: Option<String>,
    pub upstream: Option<String>,
    pub ahead: u32,
    pub behind: u32,
    pub dirty: bool,
    pub files: Vec<GitFileChangeView>,
    pub truncated: bool,
    pub insertions: Option<u32>,
    pub deletions: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitDiffView {
    pub path: String,
    pub text: String,
    pub binary: bool,
    pub truncated: bool,
    pub empty: bool,
}

/// Commits one `get_git_log` page returns at most.
pub const MAX_GIT_LOG_PAGE: u32 = 200;
/// Branches (and, separately, tags) one refs read reports before it truncates.
pub const MAX_GIT_REFS: usize = 200;
/// Changed files one commit detail reports before it truncates.
pub const MAX_GIT_COMMIT_FILES: usize = 200;
/// One historical diff (a commit's file, or a stash) is capped here.
pub const MAX_GIT_DIFF_BYTES: usize = 256 * 1024;
/// The empty tree: what a root commit (one with no parent) is compared against,
/// so the detail reader never has to special-case a repository's first commit.
const EMPTY_TREE: &str = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
/// The field separator byte every reader below splits on. The two escape
/// spellings are not interchangeable: `for-each-ref` expands `%1f` but leaves
/// `%x1f` literal, while `log`, `show` and `stash list` expand `%x1f` but leave
/// `%1f` literal. Both spellings are therefore pinned in the format constants
/// rather than here.
const US: char = '\u{1f}';
/// `git for-each-ref`: ref name, short name, tip sha, upstream, upstream
/// tracking, date, subject, current-branch marker, symref, peeled target, kind.
const REF_FORMAT: &str = "--format=%(refname)%1f%(refname:short)%1f%(objectname:short)%1f%(upstream:short)%1f%(upstream:track)%1f%(authordate:iso-strict)%1f%(subject)%1f%(HEAD)%1f%(symref)%1f%(*objectname:short)%1f%(objecttype)";
/// `git log`: full sha, short sha, author, author date, parents, subject.
const LOG_FORMAT: &str = "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%P%x1f%s";
/// `git show -s`: the log fields plus the body last, so a body that contains the
/// separator cannot shift the fields before it.
const COMMIT_FORMAT: &str = "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%P%x1f%s%x1f%b";
/// `git stash list`: the reflog selector (`stash@{0}`) and the message. It is the
/// log family, so it needs `%x1f`.
const STASH_FORMAT: &str = "--format=%gd%x1f%s";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitBranchView {
    pub name: String,
    /// `local` or `remote`.
    pub kind: String,
    pub tip_sha: String,
    pub upstream: Option<String>,
    pub ahead: u32,
    pub behind: u32,
    pub tip_date: String,
    pub tip_subject: String,
    pub is_head: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitTagView {
    pub name: String,
    /// The commit the tag points at. For an annotated tag this is the peeled
    /// target, never the tag object itself.
    pub target_sha: String,
    pub annotated: bool,
    pub date: String,
    pub subject: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitStashView {
    pub index: u32,
    pub name: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitWorktreeView {
    pub path: String,
    pub head_sha: String,
    pub branch: Option<String>,
    pub is_main: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitRefsView {
    pub current: Option<String>,
    pub branches: Vec<GitBranchView>,
    pub tags: Vec<GitTagView>,
    pub stashes: Vec<GitStashView>,
    pub worktrees: Vec<GitWorktreeView>,
    pub truncated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitCommitView {
    pub sha: String,
    pub short_sha: String,
    pub parents: Vec<String>,
    pub author: String,
    pub authored_at: String,
    pub subject: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitLogView {
    /// The revision this page was read for (`HEAD` when none was asked for).
    pub rev: String,
    pub commits: Vec<GitCommitView>,
    /// True when the page is not the end of the history.
    pub more: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitCommitFileView {
    pub path: String,
    /// git's own status letter (`A`, `M`, `D`, `R100`, `C75`, …), verbatim.
    pub status: String,
    /// Set only for a rename or a copy: the path the file came from.
    pub old_path: Option<String>,
    pub insertions: Option<u32>,
    pub deletions: Option<u32>,
    /// A binary change carries no line counts, exactly as git reports it.
    pub binary: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GitCommitDetailView {
    pub sha: String,
    pub short_sha: String,
    pub parents: Vec<String>,
    pub author: String,
    pub authored_at: String,
    pub subject: String,
    pub body: String,
    pub files: Vec<GitCommitFileView>,
    pub insertions: u32,
    pub deletions: u32,
    pub truncated: bool,
    /// One file's diff inside this commit, present only when a path was asked
    /// for. The commit is always compared against its first parent (a root
    /// commit against the empty tree), so a merge commit reports what it brought
    /// in rather than a combined diff.
    pub diff: Option<GitDiffView>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactEntry {
    pub path: String,
    pub name: String,
    pub is_dir: bool,
    pub size: u64,
    pub modified_at: Option<String>,
    /// Stat fingerprint (`size:mtime_ns`) the file surfaces use to fence paged
    /// reads and to invalidate cached previews.
    pub revision: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactPage {
    pub entries: Vec<ArtifactEntry>,
    pub path: String,
    pub total: usize,
}

/// Largest byte range one `read_artifact_chunk` call returns.
pub const MAX_ARTIFACT_CHUNK_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactStat {
    pub path: String,
    pub is_dir: bool,
    pub size: u64,
    pub modified_at: Option<String>,
    pub revision: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactChunk {
    pub path: String,
    pub offset: u64,
    pub data_base64: String,
    pub byte_length: u64,
    pub next_offset: u64,
    pub eof: bool,
    pub size: u64,
    pub modified_at: Option<String>,
    pub revision: Option<String>,
}

/// Helper to run git commands with suppressed window on Windows
fn run_git_cmd(workspace: &Path, args: &[&str]) -> Result<String, String> {
    run_git_cmd_with(workspace, args, false)
}

/// Read-only git: `GIT_OPTIONAL_LOCKS=0` is what makes "read-only" true rather
/// than aspirational -- it is the switch that stops git from refreshing and
/// rewriting the index while it answers. Every history read goes through here;
/// nothing on this surface stages, commits, checks out or moves anything.
fn run_git_cmd_ro(workspace: &Path, args: &[&str]) -> Result<String, String> {
    run_git_cmd_with(workspace, args, true)
}

fn run_git_cmd_with(
    workspace: &Path,
    args: &[&str],
    optional_locks_off: bool,
) -> Result<String, String> {
    let mut cmd = Command::new("git");
    cmd.current_dir(workspace);
    cmd.args(args);
    if optional_locks_off {
        cmd.env("GIT_OPTIONAL_LOCKS", "0");
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW
        cmd.creation_flags(0x08000000);
    }

    let output = cmd.output().map_err(|e| format!("执行 git 失败: {}", e))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "git 命令返回错误 ({}): {}",
            output.status,
            stderr.trim()
        ));
    }

    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

/// Fetch git status using git CLI in the workspace
pub fn get_git_status(workspace: &Path) -> Result<GitStatusView, String> {
    let output = run_git_cmd(workspace, &["status", "--porcelain=v1", "--branch"])?;

    let mut branch = None;
    let mut upstream = None;
    let mut ahead = 0;
    let mut behind = 0;
    let mut files = Vec::new();

    for line in output.lines() {
        if line.starts_with("## ") {
            let header = &line[3..];
            if header.starts_with("No commits yet on ") {
                branch = Some(header["No commits yet on ".len()..].trim().to_string());
                continue;
            }
            if header == "HEAD (no branch)" || header.starts_with("HEAD detached") {
                branch = Some("HEAD (detached)".to_string());
                continue;
            }

            // Pattern: branch...upstream [ahead X, behind Y]
            let (branch_part, rest) = if let Some(idx) = header.find("...") {
                let b = &header[..idx];
                let rem = &header[idx + 3..];
                (b, Some(rem))
            } else if let Some(idx) = header.find(' ') {
                let b = &header[..idx];
                let rem = &header[idx..];
                (b, Some(rem))
            } else {
                (header.trim(), None)
            };

            branch = Some(branch_part.trim().to_string());

            if let Some(rem) = rest {
                let rem = rem.trim();
                let u_name = if let Some(bracket_idx) = rem.find('[') {
                    let u = rem[..bracket_idx].trim();
                    let bracket_content = &rem[bracket_idx + 1..rem.len().saturating_sub(1)];

                    for part in bracket_content.split(',') {
                        let part = part.trim();
                        if let Some(val) = part.strip_prefix("ahead ") {
                            ahead = val.trim().parse::<u32>().unwrap_or(0);
                        } else if let Some(val) = part.strip_prefix("behind ") {
                            behind = val.trim().parse::<u32>().unwrap_or(0);
                        }
                    }
                    u
                } else {
                    rem
                };

                if !u_name.is_empty() {
                    upstream = Some(u_name.to_string());
                }
            }
        } else if line.len() >= 3 {
            let index_status = line[0..1].to_string();
            let worktree_status = line[1..2].to_string();
            let raw_path = &line[3..];
            let path = if let Some(arrow_idx) = raw_path.find(" -> ") {
                raw_path[arrow_idx + 4..].trim().to_string()
            } else {
                raw_path.trim().to_string()
            };

            files.push(GitFileChangeView {
                path,
                index_status,
                worktree_status,
            });
        }
    }

    // Calculate line counts via git diff --numstat
    let mut insertions = 0u32;
    let mut deletions = 0u32;
    let mut has_stats = false;

    if let Ok(numstat) = run_git_cmd(workspace, &["diff", "HEAD", "--numstat"]) {
        has_stats = true;
        for line in numstat.lines() {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 2 {
                if let (Ok(ins), Ok(del)) = (parts[0].parse::<u32>(), parts[1].parse::<u32>()) {
                    insertions += ins;
                    deletions += del;
                }
            }
        }
    }

    let dirty = !files.is_empty();

    Ok(GitStatusView {
        branch,
        upstream,
        ahead,
        behind,
        dirty,
        files,
        truncated: false,
        insertions: if has_stats { Some(insertions) } else { None },
        deletions: if has_stats { Some(deletions) } else { None },
    })
}

/// Open a file or directory using the operating system's default application
pub fn open_path(target: &Path) -> Result<(), String> {
    if !target.exists() {
        return Err(format!("路径不存在: {}", target.display()));
    }
    open::that(target).map_err(|e| format!("打开失败: {}", e))
}

/// Open a file in VS Code using CLI or protocol handler
pub fn open_in_vscode(target: &Path) -> Result<(), String> {
    let target_str = target.to_string_lossy().to_string();

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // 1. Try `code` in PATH
        let mut cmd = Command::new("code");
        cmd.arg(&target_str);
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        if cmd.spawn().is_ok() {
            return Ok(());
        }

        // 2. Try known VS Code install locations on Windows
        let mut candidates = Vec::new();
        if let Ok(local) = std::env::var("LOCALAPPDATA") {
            candidates.push(PathBuf::from(local).join(r"Programs\Microsoft VS Code\Code.exe"));
        }
        if let Ok(pf) = std::env::var("ProgramFiles") {
            candidates.push(PathBuf::from(pf).join(r"Microsoft VS Code\Code.exe"));
        }
        if let Ok(pf86) = std::env::var("ProgramFiles(x86)") {
            candidates.push(PathBuf::from(pf86).join(r"Microsoft VS Code\Code.exe"));
        }
        for candidate in candidates {
            if candidate.exists() {
                let mut cmd = Command::new(&candidate);
                cmd.arg(&target_str);
                cmd.creation_flags(0x08000000);
                if cmd.spawn().is_ok() {
                    return Ok(());
                }
            }
        }
    }

    #[cfg(not(windows))]
    {
        let mut cmd = Command::new("code");
        cmd.arg(&target_str);
        if cmd.spawn().is_ok() {
            return Ok(());
        }
    }

    // 3. Fall back to protocol handler: vscode://file/<path>
    let url = if target_str.starts_with('/') {
        format!("vscode://file{}", target_str)
    } else {
        format!("vscode://file/{}", target_str.replace('\\', "/"))
    };
    open::that(&url).map_err(|e| format!("打开 VS Code 失败: {}", e))
}

/// Reveal a file or directory in the system file manager (Explorer / Finder / etc.)
pub fn reveal_in_explorer(target: &Path) -> Result<(), String> {
    let target_buf: PathBuf = if !target.exists() {
        target.parent().filter(|p| p.exists()).map(|p| p.to_path_buf()).ok_or_else(|| {
            format!("路径不存在: {}", target.display())
        })?
    } else {
        target.to_path_buf()
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut cmd = Command::new("explorer");
        if target_buf.is_dir() {
            cmd.arg(&target_buf);
        } else {
            cmd.arg(format!("/select,{}", target_buf.display()));
        }
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        cmd.spawn().map_err(|e| format!("启动文件管理器失败: {}", e))?;
        Ok(())
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg("-R").arg(&target_buf).spawn().map_err(|e| format!("启动 Finder 失败: {}", e))?;
        Ok(())
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let dir = if target_buf.is_dir() { &target_buf } else { target_buf.parent().unwrap_or(&target_buf) };
        open::that(dir).map_err(|e| format!("打开目录失败: {}", e))
    }
}

/// Fetch unified diff for a single path
pub fn get_git_diff(workspace: &Path, file_path: &str) -> Result<GitDiffView, String> {
    // Try git diff HEAD -- <path> first
    let text = match run_git_cmd(workspace, &["diff", "HEAD", "--", file_path]) {
        Ok(res) if !res.is_empty() => res,
        _ => {
            // Try git diff -- <path> for unstaged changes
            match run_git_cmd(workspace, &["diff", "--", file_path]) {
                Ok(res) if !res.is_empty() => res,
                _ => {
                    // Check if it is an untracked file, create unified addition diff if readable
                    let target = workspace.join(file_path);
                    if target.is_file() {
                        if let Ok(content) = fs::read_to_string(&target) {
                            let mut buf = format!(
                                "--- /dev/null\n+++ b/{}\n@@ -0,0 +1,{} @@\n",
                                file_path,
                                content.lines().count()
                            );
                            for line in content.lines() {
                                buf.push('+');
                                buf.push_str(line);
                                buf.push('\n');
                            }
                            buf
                        } else {
                            String::new()
                        }
                    } else {
                        String::new()
                    }
                }
            }
        }
    };

    let binary = text.contains("Binary files ") || text.contains("GIT binary patch");
    let empty = text.is_empty();

    Ok(GitDiffView {
        path: file_path.to_string(),
        text,
        binary,
        truncated: false,
        empty,
    })
}

// ---------------------------------------------------------------------------
// Branch / tag / stash / worktree inventory, history, and commit detail.
//
// All of it is read-only, bounded, and argument-validated: a revision or a path
// arriving from the webview is never handed to git unexamined, because both land
// in argv positions where a leading `-` would be read as an option.
// ---------------------------------------------------------------------------

/// A revision the history readers accept: a branch, a tag, or a sha.
///
/// Deliberately stricter than git's own revision syntax. `HEAD~1`, `a..b` and
/// `@{upstream}` are all valid revisions, but this surface only ever asks for one
/// ref at a time, and refusing them here is what makes a leading `-` (an option
/// injection) impossible rather than merely unlikely.
pub fn validate_git_rev(rev: &str) -> Result<(), String> {
    if rev.is_empty() {
        return Err("revision 不能为空".to_string());
    }
    if rev.len() > 255 {
        return Err(format!("revision 过长: {} 字节", rev.len()));
    }
    if rev.starts_with('-') || rev.starts_with('.') || rev.starts_with('/') {
        return Err(format!("revision 不能以 '-' '.' '/' 开头: {rev}"));
    }
    if rev.ends_with('.') || rev.ends_with('/') || rev.contains("..") || rev.contains("@{") {
        return Err(format!("revision 不是合法引用: {rev}"));
    }
    if !rev
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "._/-@".contains(c))
    {
        return Err(format!("revision 只允许字母、数字与 . _ / - @ : {rev}"));
    }
    Ok(())
}

/// A workspace-relative POSIX path, or an error.
///
/// Same rule as the artifact surface: no drive prefix, no backslash, no empty,
/// `.` or `..` segment -- and no leading `-`, which a pathspec position would
/// otherwise read as an option.
pub fn validate_git_path(path: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err("路径不能为空".to_string());
    }
    if path.len() > 4096 {
        return Err("路径过长".to_string());
    }
    if path.starts_with('-') {
        return Err("路径不能以 '-' 开头".to_string());
    }
    if path.starts_with('/') || path.contains('\\') || path.contains(':') {
        return Err("路径必须是工作区相对的 POSIX 路径".to_string());
    }
    if path
        .split('/')
        .any(|segment| segment.is_empty() || segment == "." || segment == "..")
    {
        return Err("路径不能包含空段或父目录段".to_string());
    }
    Ok(())
}

/// Cap a string at `cap` bytes on a character boundary; reports whether it cut.
fn cap_text(text: &mut String, cap: usize) -> bool {
    if text.len() <= cap {
        return false;
    }
    let mut end = cap;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text.truncate(end);
    true
}

/// `%(upstream:track)` (`[ahead 1, behind 2]`, or empty) as counts.
fn parse_track(track: &str) -> (u32, u32) {
    let mut ahead = 0;
    let mut behind = 0;
    let inner = track.trim().trim_start_matches('[').trim_end_matches(']');
    for part in inner.split(',') {
        let part = part.trim();
        if let Some(value) = part.strip_prefix("ahead ") {
            ahead = value.trim().parse::<u32>().unwrap_or(0);
        } else if let Some(value) = part.strip_prefix("behind ") {
            behind = value.trim().parse::<u32>().unwrap_or(0);
        }
    }
    (ahead, behind)
}

/// One numstat record's `(added, removed, binary)`.
fn numstat_counts(added: &str, removed: &str) -> (Option<u32>, Option<u32>, bool) {
    if added == "-" || removed == "-" {
        return (None, None, true);
    }
    (
        added.parse::<u32>().ok(),
        removed.parse::<u32>().ok(),
        false,
    )
}

/// Parse `git diff --numstat -z` into `path -> (added, removed, binary)`.
///
/// With `-z` a single-path record is one NUL-terminated token holding
/// `added\tremoved\tpath`, while a rename or a copy is a token holding
/// `added\tremoved\t` followed by two more tokens (the old, then the new path).
/// A binary change reports `-` for both columns and carries no line counts.
fn parse_numstat(raw: &str) -> HashMap<String, (Option<u32>, Option<u32>, bool)> {
    let mut counts = HashMap::new();
    let mut tokens = raw.split('\0').filter(|token| !token.is_empty());
    while let Some(token) = tokens.next() {
        let mut parts = token.splitn(3, '\t');
        let added = parts.next().unwrap_or("");
        let removed = parts.next().unwrap_or("");
        let rest = parts.next().unwrap_or("");
        if rest.is_empty() {
            // A rename or a copy: the next two tokens are the old and new path.
            match (tokens.next(), tokens.next()) {
                (Some(_old), Some(new)) => {
                    counts.insert(new.to_string(), numstat_counts(added, removed));
                }
                _ => break,
            }
            continue;
        }
        counts.insert(rest.to_string(), numstat_counts(added, removed));
    }
    counts
}

/// Parse `git diff --name-status -z` into `(status, path, old_path)` records.
///
/// A rename or a copy is `status\0old\0new`; everything else is `status\0path`.
fn parse_name_status(raw: &str) -> Vec<(String, String, Option<String>)> {
    let mut records = Vec::new();
    let mut tokens = raw.split('\0').filter(|token| !token.is_empty());
    while let Some(status) = tokens.next() {
        let status = status.to_string();
        if status.starts_with('R') || status.starts_with('C') {
            match (tokens.next(), tokens.next()) {
                (Some(old), Some(new)) => {
                    records.push((status, new.to_string(), Some(old.to_string())));
                }
                _ => break,
            }
            continue;
        }
        match tokens.next() {
            Some(path) => records.push((status, path.to_string(), None)),
            None => break,
        }
    }
    records
}

/// The stash entries of one workspace (`git stash list`).
fn read_stashes(workspace: &Path) -> Result<Vec<GitStashView>, String> {
    let raw = run_git_cmd_ro(workspace, &["stash", "list", STASH_FORMAT])?;
    let mut stashes = Vec::new();
    for line in raw.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let mut parts = line.splitn(2, US);
        let name = parts.next().unwrap_or("").trim().to_string();
        let message = parts.next().unwrap_or("").to_string();
        let index = name
            .strip_prefix("stash@{")
            .and_then(|rest| rest.strip_suffix('}'))
            .and_then(|digits| digits.parse::<u32>().ok())
            .unwrap_or(0);
        stashes.push(GitStashView {
            index,
            name,
            message,
        });
    }
    Ok(stashes)
}

/// The worktrees of one repository (`git worktree list --porcelain`).
///
/// The paths are absolute and are reported as such: this surface exists for the
/// desktop shell, which already reads the reader's own filesystem.
fn read_worktrees(workspace: &Path) -> Result<Vec<GitWorktreeView>, String> {
    let raw = run_git_cmd_ro(workspace, &["worktree", "list", "--porcelain"])?;
    // One blank-line separated block per worktree: `worktree <path>`,
    // `HEAD <sha>`, then `branch <ref>` or `detached`.
    let mut worktrees: Vec<GitWorktreeView> = Vec::new();
    let mut current: Option<(Option<String>, String, Option<String>)> = None;
    let flush = |current: &mut Option<(Option<String>, String, Option<String>)>,
                 worktrees: &mut Vec<GitWorktreeView>| {
        if let Some((Some(path), head_sha, branch)) = current.take() {
            let is_main = worktrees.is_empty();
            worktrees.push(GitWorktreeView {
                path,
                head_sha,
                branch,
                is_main,
            });
        }
    };
    for line in raw.lines() {
        if line.trim().is_empty() {
            flush(&mut current, &mut worktrees);
            continue;
        }
        let entry = current.get_or_insert_with(|| (None, String::new(), None));
        if let Some(value) = line.strip_prefix("worktree ") {
            entry.0 = Some(value.trim().to_string());
        } else if let Some(value) = line.strip_prefix("HEAD ") {
            entry.1 = value.trim().to_string();
        } else if let Some(value) = line.strip_prefix("branch ") {
            let value = value.trim();
            entry.2 = Some(
                value
                    .strip_prefix("refs/heads/")
                    .unwrap_or(value)
                    .to_string(),
            );
        }
    }
    flush(&mut current, &mut worktrees);
    Ok(worktrees)
}

/// Branches, tags, stashes and worktrees of one workspace.
///
/// Three bounded read-only calls: one `for-each-ref` covers branches and tags
/// together (with `%(upstream:track)` answering ahead/behind without a
/// `rev-list` per branch), one `stash list`, one `worktree list`.
pub fn get_git_refs(workspace: &Path) -> Result<GitRefsView, String> {
    let raw = run_git_cmd_ro(
        workspace,
        &[
            "for-each-ref",
            "--sort=-committerdate",
            REF_FORMAT,
            "refs/heads",
            "refs/remotes",
            "refs/tags",
        ],
    )?;

    let mut branches: Vec<GitBranchView> = Vec::new();
    let mut tags: Vec<GitTagView> = Vec::new();
    let mut truncated = false;

    for line in raw.lines() {
        let fields: Vec<&str> = line.split(US).collect();
        if fields.len() < 11 {
            continue;
        }
        let refname = fields[0];
        let short = fields[1];
        let object_short = fields[2];
        let upstream = fields[3];
        let (ahead, behind) = parse_track(fields[4]);
        let date = fields[5];
        let subject = fields[6];
        let head = fields[7];
        let symref = fields[8];
        let peeled = fields[9];
        let object_kind = fields[10];
        // `refs/remotes/origin/HEAD` is a symbolic alias of another ref, not a
        // branch of its own; listing it would duplicate `origin/main`.
        if !symref.is_empty() {
            continue;
        }
        if refname.starts_with("refs/tags/") {
            if tags.len() >= MAX_GIT_REFS {
                truncated = true;
                continue;
            }
            let annotated = object_kind == "tag";
            tags.push(GitTagView {
                name: short.to_string(),
                // An annotated tag's own object is a tag object; the commit it
                // points at is the peeled one.
                target_sha: if annotated && !peeled.is_empty() {
                    peeled
                } else {
                    object_short
                }
                .to_string(),
                annotated,
                date: date.to_string(),
                subject: subject.to_string(),
            });
            continue;
        }
        let is_local = refname.starts_with("refs/heads/");
        if !is_local && !refname.starts_with("refs/remotes/") {
            continue;
        }
        if branches.len() >= MAX_GIT_REFS {
            truncated = true;
            continue;
        }
        branches.push(GitBranchView {
            name: short.to_string(),
            kind: if is_local { "local" } else { "remote" }.to_string(),
            tip_sha: object_short.to_string(),
            upstream: if upstream.is_empty() {
                None
            } else {
                Some(upstream.to_string())
            },
            ahead,
            behind,
            tip_date: date.to_string(),
            tip_subject: subject.to_string(),
            is_head: head.trim() == "*",
        });
    }

    let current = branches
        .iter()
        .find(|branch| branch.is_head)
        .map(|branch| branch.name.clone());

    Ok(GitRefsView {
        current,
        stashes: read_stashes(workspace)?,
        worktrees: read_worktrees(workspace)?,
        branches,
        tags,
        truncated,
    })
}

/// One page of a revision's history, newest first.
///
/// `limit + 1` commits are asked for, so "there is more" is an answer rather
/// than a guess; the extra one is dropped before returning.
pub fn get_git_log(
    workspace: &Path,
    rev: Option<&str>,
    limit: u32,
    skip: u32,
    first_parent: bool,
    path: Option<&str>,
) -> Result<GitLogView, String> {
    let rev = rev.unwrap_or("HEAD");
    validate_git_rev(rev)?;
    let page = match limit {
        0 => MAX_GIT_LOG_PAGE,
        value => value.min(MAX_GIT_LOG_PAGE),
    };
    let mut args: Vec<String> = vec![
        "log".to_string(),
        "--no-color".to_string(),
        LOG_FORMAT.to_string(),
        "-n".to_string(),
        (page + 1).to_string(),
        "--skip".to_string(),
        skip.to_string(),
    ];
    if first_parent {
        args.push("--first-parent".to_string());
    }
    args.push(rev.to_string());
    if let Some(path) = path {
        validate_git_path(path)?;
        args.push("--".to_string());
        args.push(path.to_string());
    }
    let borrowed: Vec<&str> = args.iter().map(String::as_str).collect();
    let raw = match run_git_cmd_ro(workspace, &borrowed) {
        Ok(raw) => raw,
        Err(error) => {
            // A repository with no commit yet has no history to show, which is an
            // empty log rather than a failure -- but only when it was `HEAD`, the
            // default, that could not be read. A revision the caller named still
            // reports itself, so a typo is never silently an empty history.
            let unborn = rev == "HEAD"
                && run_git_cmd_ro(workspace, &["rev-parse", "--verify", "HEAD"]).is_err();
            if !unborn {
                return Err(error);
            }
            return Ok(GitLogView {
                rev: rev.to_string(),
                commits: Vec::new(),
                more: false,
            });
        }
    };

    let mut commits: Vec<GitCommitView> = Vec::new();
    for line in raw.lines() {
        if line.trim().is_empty() {
            continue;
        }
        // `splitn` keeps a subject that happens to contain the separator intact.
        let mut fields = line.splitn(6, US);
        let sha = fields.next().unwrap_or("").to_string();
        let short_sha = fields.next().unwrap_or("").to_string();
        let author = fields.next().unwrap_or("").to_string();
        let authored_at = fields.next().unwrap_or("").to_string();
        let parents = fields.next().unwrap_or("");
        let subject = fields.next().unwrap_or("").to_string();
        if sha.is_empty() {
            continue;
        }
        commits.push(GitCommitView {
            sha,
            short_sha,
            parents: parents.split_whitespace().map(str::to_string).collect(),
            author,
            authored_at,
            subject,
        });
    }
    let more = commits.len() > page as usize;
    commits.truncate(page as usize);
    Ok(GitLogView {
        rev: rev.to_string(),
        commits,
        more,
    })
}

/// One path's diff inside one commit, bounded and binary-safe.
fn git_commit_file_diff(
    workspace: &Path,
    parent: &str,
    sha: &str,
    path: &str,
) -> Result<GitDiffView, String> {
    let raw = run_git_cmd_ro(
        workspace,
        &["diff", "--no-color", "--unified=3", parent, sha, "--", path],
    )?;
    if raw.contains('\u{0}') || (raw.contains("Binary files ") && raw.contains(" differ")) {
        return Ok(GitDiffView {
            path: path.to_string(),
            text: String::new(),
            binary: true,
            truncated: false,
            empty: false,
        });
    }
    let mut text = raw;
    let truncated = cap_text(&mut text, MAX_GIT_DIFF_BYTES);
    Ok(GitDiffView {
        path: path.to_string(),
        empty: text.trim().is_empty(),
        text,
        binary: false,
        truncated,
    })
}

/// One commit: its metadata, the files it changed against its first parent, and
/// -- when a path is asked for -- that file's diff inside the commit.
pub fn get_git_commit(
    workspace: &Path,
    sha: &str,
    path: Option<&str>,
) -> Result<GitCommitDetailView, String> {
    validate_git_rev(sha)?;
    if let Some(path) = path {
        validate_git_path(path)?;
    }
    let raw = run_git_cmd_ro(workspace, &["show", "-s", COMMIT_FORMAT, sha])?;
    let mut fields = raw.splitn(7, US);
    let full_sha = fields.next().unwrap_or("").trim().to_string();
    let short_sha = fields.next().unwrap_or("").to_string();
    let author = fields.next().unwrap_or("").to_string();
    let authored_at = fields.next().unwrap_or("").to_string();
    let parents: Vec<String> = fields
        .next()
        .unwrap_or("")
        .split_whitespace()
        .map(str::to_string)
        .collect();
    let subject = fields.next().unwrap_or("").to_string();
    let body = fields.next().unwrap_or("").trim().to_string();
    if full_sha.is_empty() {
        return Err(format!("无法读取提交: {sha}"));
    }

    // A root commit has no parent to compare against, so it is compared against
    // the empty tree; a merge commit is compared against its first parent, which
    // is what "what did this commit bring in" means for a history list.
    let parent = parents
        .first()
        .cloned()
        .unwrap_or_else(|| EMPTY_TREE.to_string());

    let name_status = run_git_cmd_ro(
        workspace,
        &["diff", "--name-status", "-M", "-z", parent.as_str(), sha],
    )?;
    let numstat = run_git_cmd_ro(
        workspace,
        &["diff", "--numstat", "-M", "-z", parent.as_str(), sha],
    )
    .unwrap_or_default();
    let counts = parse_numstat(&numstat);

    let mut files: Vec<GitCommitFileView> = Vec::new();
    let mut truncated = false;
    let mut insertions = 0u32;
    let mut deletions = 0u32;
    for (status, file_path, old_path) in parse_name_status(&name_status) {
        if files.len() >= MAX_GIT_COMMIT_FILES {
            truncated = true;
            break;
        }
        let (added, removed, binary) = counts
            .get(&file_path)
            .copied()
            .unwrap_or((None, None, false));
        insertions += added.unwrap_or(0);
        deletions += removed.unwrap_or(0);
        files.push(GitCommitFileView {
            path: file_path,
            status,
            old_path,
            insertions: added,
            deletions: removed,
            binary,
        });
    }

    let diff = match path {
        Some(path) => Some(git_commit_file_diff(workspace, &parent, sha, path)?),
        None => None,
    };

    Ok(GitCommitDetailView {
        sha: full_sha,
        short_sha,
        parents,
        author,
        authored_at,
        subject,
        body,
        files,
        insertions,
        deletions,
        truncated,
        diff,
    })
}

/// One stash's diff, read-only: `git stash show -p` never applies or drops it.
pub fn get_git_stash_diff(workspace: &Path, index: u32) -> Result<GitDiffView, String> {
    let spec = format!("stash@{{{index}}}");
    let raw = run_git_cmd_ro(
        workspace,
        &[
            "stash",
            "show",
            "-p",
            "--no-color",
            "--unified=3",
            spec.as_str(),
        ],
    )?;
    if raw.contains('\u{0}') || (raw.contains("Binary files ") && raw.contains(" differ")) {
        return Ok(GitDiffView {
            path: spec,
            text: String::new(),
            binary: true,
            truncated: false,
            empty: false,
        });
    }
    let mut text = raw;
    let truncated = cap_text(&mut text, MAX_GIT_DIFF_BYTES);
    Ok(GitDiffView {
        path: spec,
        empty: text.trim().is_empty(),
        text,
        binary: false,
        truncated,
    })
}

/// List artifacts in workspace directory
pub fn list_artifacts(workspace: &Path, subpath: Option<&str>) -> Result<ArtifactPage, String> {
    let rel_sub = subpath.unwrap_or("").trim_start_matches(['/', '\\']);
    let target_dir = resolve_artifact_path(workspace, rel_sub)?;

    if !target_dir.exists() {
        return Err(format!("目录不存在: {}", target_dir.display()));
    }

    let mut entries = Vec::new();
    let read_dir = fs::read_dir(&target_dir).map_err(|e| format!("无法读取目录: {}", e))?;

    for entry_res in read_dir {
        if let Ok(entry) = entry_res {
            let file_name = entry.file_name().to_string_lossy().to_string();
            // Skip typical noise like .git
            if file_name == ".git" {
                continue;
            }

            let path_buf = entry.path();
            // `fs::metadata` (not `DirEntry::metadata`) so a symlinked directory
            // is listed as the directory it points at, exactly as before.
            let metadata = fs::metadata(&path_buf).ok();
            let is_dir = metadata.as_ref().map(|m| m.is_dir()).unwrap_or(false);
            let size = match metadata.as_ref() {
                Some(m) if !is_dir => m.len(),
                _ => 0,
            };

            let rel_path = path_buf
                .strip_prefix(workspace)
                .map(|p| p.to_string_lossy().replace('\\', "/"))
                .unwrap_or_else(|_| file_name.clone());

            entries.push(ArtifactEntry {
                path: rel_path,
                name: file_name,
                is_dir,
                size,
                modified_at: metadata.as_ref().and_then(modified_at_of),
                revision: metadata.as_ref().and_then(revision_of),
            });
        }
    }

    // Sort: directories first, then alphabetical by name
    entries.sort_by(|a, b| {
        b.is_dir
            .cmp(&a.is_dir)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
    });

    let total = entries.len();
    Ok(ArtifactPage {
        entries,
        // Mirror the RPC's canonical root (`.`), so a caller that feeds the
        // echoed path back in lands on the same directory either way.
        path: if rel_sub.is_empty() {
            ".".to_string()
        } else {
            rel_sub.to_string()
        },
        total,
    })
}

/// Stat fingerprint of one entry (`size:mtime_ns`).
fn revision_of(metadata: &fs::Metadata) -> Option<String> {
    let modified_ns = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| duration.as_nanos())?;
    Some(format!("{}:{}", metadata.len(), modified_ns))
}

/// Last modification time in whole seconds since the Unix epoch.
fn modified_at_of(metadata: &fs::Metadata) -> Option<String> {
    metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|duration| format!("{}s", duration.as_secs()))
}

/// Resolve a workspace-relative artifact path.
///
/// The desktop file surfaces only ever send a path a listing produced, but a
/// transcript reference can carry anything the model wrote, so a `..` segment is
/// refused instead of being joined into the workspace blindly.
fn resolve_artifact_path(workspace: &Path, path: &str) -> Result<PathBuf, String> {
    // Only the leading separators are stripped: a real file name may begin or end
    // with a space, and trimming it would resolve a different path than the one a
    // listing reported (and than the one the caller gets echoed back).
    let trimmed = path.trim_start_matches(['/', '\\']);
    if trimmed.is_empty() || trimmed == "." {
        return Ok(workspace.to_path_buf());
    }
    if Path::new(trimmed).is_absolute() {
        return Err(format!("路径必须是工作区内的相对路径: {}", path));
    }
    let mut resolved = workspace.to_path_buf();
    for segment in trimmed.split(['/', '\\']) {
        match segment {
            "" | "." => {}
            ".." => return Err(format!("路径不能离开工作区: {}", path)),
            name => {
                // `PathBuf::push` replaces the whole buffer when the pushed
                // component carries a prefix (`C:`), so a segment that is not one
                // plain name is refused rather than silently re-rooting the path.
                if !is_plain_path_segment(name) {
                    return Err(format!("路径必须是工作区内的相对路径: {}", path));
                }
                resolved.push(name);
            }
        }
    }
    Ok(resolved)
}

/// Whether `segment` is exactly one ordinary path component.
///
/// A Windows drive (`C:`), a rooted segment (`\x`) or anything else carrying a
/// prefix fails here; on Unix a colon is a legal file-name character, so such a
/// name still passes.
fn is_plain_path_segment(segment: &str) -> bool {
    let mut components = Path::new(segment).components();
    matches!(components.next(), Some(Component::Normal(_))) && components.next().is_none()
}

/// Stat one artifact inside the workspace.
pub fn stat_artifact(workspace: &Path, path: &str) -> Result<ArtifactStat, String> {
    let target = resolve_artifact_path(workspace, path)?;
    let metadata = fs::metadata(&target).map_err(|e| format!("无法读取文件信息: {}", e))?;
    let is_dir = metadata.is_dir();
    Ok(ArtifactStat {
        path: path.trim_start_matches(['/', '\\']).to_string(),
        is_dir,
        size: if is_dir { 0 } else { metadata.len() },
        modified_at: modified_at_of(&metadata),
        revision: revision_of(&metadata),
    })
}

/// Read one bounded byte range of a workspace artifact.
///
/// `limit` is clamped to `MAX_ARTIFACT_CHUNK_BYTES`, so a single call can never
/// pull a whole build artifact (a multi-hundred-megabyte binary under `target/`,
/// say) into memory; the caller advances with `next_offset` until `eof`.
pub fn read_artifact_chunk(
    workspace: &Path,
    path: &str,
    offset: u64,
    limit: u64,
) -> Result<ArtifactChunk, String> {
    let target = resolve_artifact_path(workspace, path)?;
    let metadata = fs::metadata(&target).map_err(|e| format!("无法读取文件信息: {}", e))?;
    if !metadata.is_file() {
        return Err(format!("不是文件: {}", target.display()));
    }
    let size = metadata.len();
    if offset > size {
        return Err(format!("读取偏移超出文件末尾: {} > {}", offset, size));
    }
    let bound = limit.clamp(1, MAX_ARTIFACT_CHUNK_BYTES).min(size - offset);
    let mut bytes = vec![0u8; bound as usize];
    if !bytes.is_empty() {
        use std::io::{Read, Seek, SeekFrom};
        let mut file = fs::File::open(&target).map_err(|e| format!("读取文件失败: {}", e))?;
        file.seek(SeekFrom::Start(offset))
            .map_err(|e| format!("读取文件失败: {}", e))?;
        file.read_exact(&mut bytes)
            .map_err(|e| format!("读取文件失败: {}", e))?;
    }
    let next_offset = offset + bound;
    Ok(ArtifactChunk {
        path: path.trim_start_matches(['/', '\\']).to_string(),
        offset,
        data_base64: base64::engine::general_purpose::STANDARD.encode(&bytes),
        byte_length: bound,
        next_offset,
        eof: next_offset >= size,
        size,
        modified_at: modified_at_of(&metadata),
        revision: revision_of(&metadata),
    })
}
