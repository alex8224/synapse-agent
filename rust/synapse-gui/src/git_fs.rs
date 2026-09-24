use base64::Engine as _;
use serde::{Deserialize, Serialize};
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
    let mut cmd = Command::new("git");
    cmd.current_dir(workspace);
    cmd.args(args);

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

/// Reveal a file or directory in the system file manager (Explorer / Finder / etc.)
pub fn reveal_in_explorer(target: &Path) -> Result<(), String> {
    if !target.exists() {
        return Err(format!("路径不存在: {}", target.display()));
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut cmd = Command::new("explorer");
        if target.is_dir() {
            cmd.arg(target);
        } else {
            cmd.arg(format!("/select,{}", target.display()));
        }
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        cmd.spawn().map_err(|e| format!("启动文件管理器失败: {}", e))?;
        Ok(())
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg("-R").arg(target).spawn().map_err(|e| format!("启动 Finder 失败: {}", e))?;
        Ok(())
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let dir = if target.is_dir() { target } else { target.parent().unwrap_or(target) };
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
