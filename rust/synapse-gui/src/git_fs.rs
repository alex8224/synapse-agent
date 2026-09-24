use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;
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
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactPage {
    pub entries: Vec<ArtifactEntry>,
    pub path: String,
    pub total: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactContent {
    pub path: String,
    pub content: String,
    pub size: u64,
    pub is_binary: bool,
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
    let target_dir = if rel_sub.is_empty() || rel_sub == "." {
        workspace.to_path_buf()
    } else {
        workspace.join(rel_sub)
    };

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
            let is_dir = path_buf.is_dir();
            let size = if is_dir {
                0
            } else {
                entry.metadata().map(|m| m.len()).unwrap_or(0)
            };

            let modified_at = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|time| {
                    let duration = time.duration_since(std::time::UNIX_EPOCH).ok()?;
                    Some(format!("{}s", duration.as_secs()))
                });

            let rel_path = path_buf
                .strip_prefix(workspace)
                .map(|p| p.to_string_lossy().replace('\\', "/"))
                .unwrap_or_else(|_| file_name.clone());

            entries.push(ArtifactEntry {
                path: rel_path,
                name: file_name,
                is_dir,
                size,
                modified_at,
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
        path: rel_sub.to_string(),
        total,
    })
}

/// Read text artifact content
pub fn read_artifact(workspace: &Path, file_path: &str) -> Result<ArtifactContent, String> {
    let clean_path = file_path.trim_start_matches(['/', '\\']);
    let target = workspace.join(clean_path);

    if !target.exists() || !target.is_file() {
        return Err(format!("文件不存在: {}", target.display()));
    }

    let bytes = fs::read(&target).map_err(|e| format!("读取文件失败: {}", e))?;
    let size = bytes.len() as u64;

    // Check binary heuristic
    let is_binary = bytes.iter().take(1024).any(|&b| b == 0);
    let content = if is_binary {
        "[二进制文件内容]".to_string()
    } else {
        String::from_utf8_lossy(&bytes).to_string()
    };

    Ok(ArtifactContent {
        path: clean_path.to_string(),
        content,
        size,
        is_binary,
    })
}
