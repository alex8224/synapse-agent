use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use globset::{Glob, GlobSet, GlobSetBuilder};
use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{BinaryDetection, Searcher, SearcherBuilder, Sink, SinkContext, SinkMatch};
use ignore::WalkBuilder;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

const MAX_CONTEXT_LINES: usize = 10;
const MAX_MATCH_TEXT_CHARS: usize = 4_000;

/// Directory names pruned from every search walk, matching the model-facing
/// contract that `glob`/`grep` automatically exclude build artifacts and
/// caches. These are enforced here instead of relying on `.gitignore`, which
/// can be incomplete (and can never exclude `.git` itself).
const EXCLUDED_DIR_NAMES: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    ".jj",
    "node_modules",
    ".node_modules",
    "target",
    ".venv",
    "__pycache__",
];

/// Returns true when an entry should be pruned from the walk.
fn is_excluded(entry: &ignore::DirEntry) -> bool {
    // Keep the walk root; only prune nested entries below it.
    if entry.depth() == 0 {
        return false;
    }
    let name = entry.file_name().to_str().unwrap_or("");
    if entry.file_type().is_some_and(|kind| kind.is_dir()) {
        return EXCLUDED_DIR_NAMES.contains(&name);
    }
    // Files: skip compiled Python bytecode explicitly listed in the contract.
    name.ends_with(".pyc")
}

#[derive(Debug)]
struct SearchError(String);

impl std::fmt::Display for SearchError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

fn compile_glob(pattern: &str) -> Result<GlobSet, SearchError> {
    let mut builder = GlobSetBuilder::new();
    let glob = Glob::new(pattern)
        .map_err(|error| SearchError(format!("invalid glob pattern: {error}")))?;
    builder.add(glob);
    builder
        .build()
        .map_err(|error| SearchError(format!("invalid glob pattern: {error}")))
}

fn build_walker(base_path: &Path) -> ignore::Walk {
    WalkBuilder::new(base_path)
        .hidden(false)
        .require_git(false)
        .git_ignore(true)
        .git_global(true)
        .git_exclude(true)
        .ignore(true)
        .follow_links(false)
        .filter_entry(|entry| !is_excluded(entry))
        .build()
}

fn relative_path(path: &Path, base_path: &Path) -> String {
    path.strip_prefix(base_path)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn truncate_text(text: &str) -> String {
    let text = text.trim_end_matches(['\r', '\n']);
    if text.chars().count() <= MAX_MATCH_TEXT_CHARS {
        return text.to_owned();
    }
    let prefix: String = text.chars().take(MAX_MATCH_TEXT_CHARS).collect();
    format!(
        "{prefix} [truncated {} chars]",
        text.chars().count().saturating_sub(MAX_MATCH_TEXT_CHARS)
    )
}

struct FileSearchResult {
    total_matches: usize,
    lines: Vec<(usize, String)>,
}

struct MatchSink {
    result: FileSearchResult,
    context_lines: usize,
}

impl Sink for MatchSink {
    type Error = std::io::Error;

    fn matched(&mut self, _: &Searcher, mat: &SinkMatch<'_>) -> Result<bool, Self::Error> {
        self.result.total_matches = self.result.total_matches.saturating_add(1);
        self.result.lines.push((
            mat.line_number().unwrap_or(0) as usize,
            truncate_text(&String::from_utf8_lossy(mat.bytes())),
        ));
        Ok(true)
    }

    fn context(&mut self, _: &Searcher, context: &SinkContext<'_>) -> Result<bool, Self::Error> {
        if self.context_lines > 0 {
            self.result.lines.push((
                context.line_number().unwrap_or(0) as usize,
                truncate_text(&String::from_utf8_lossy(context.bytes())),
            ));
        }
        Ok(true)
    }
}

fn search_file(
    path: &Path,
    matcher: &RegexMatcher,
    context_lines: usize,
) -> Result<FileSearchResult, SearchError> {
    let mut searcher = SearcherBuilder::new()
        .before_context(context_lines)
        .after_context(context_lines)
        .binary_detection(BinaryDetection::quit(b'\x00'))
        .build();
    let mut sink = MatchSink {
        result: FileSearchResult {
            total_matches: 0,
            lines: Vec::new(),
        },
        context_lines,
    };
    searcher
        .search_path(matcher, path, &mut sink)
        .map_err(|error| SearchError(format!("failed to search '{}': {error}", path.display())))?;
    Ok(sink.result)
}

#[pyfunction(signature = (base_path, pattern, include_glob=None, max_results=1000, context_lines=0, case_insensitive=false))]
pub(crate) fn grep(
    py: Python<'_>,
    base_path: &str,
    pattern: &str,
    include_glob: Option<&str>,
    max_results: usize,
    context_lines: usize,
    case_insensitive: bool,
) -> PyResult<Py<PyDict>> {
    if pattern.is_empty() {
        return Err(PyValueError::new_err("pattern must not be empty"));
    }
    let base_path = PathBuf::from(base_path);
    if !base_path.is_absolute() {
        return Err(PyValueError::new_err("base_path must be absolute"));
    }
    if !base_path.exists() {
        return Err(PyValueError::new_err(format!(
            "base_path does not exist: {}",
            base_path.display()
        )));
    }
    let matcher = RegexMatcherBuilder::new()
        .case_insensitive(case_insensitive)
        .build(pattern)
        .map_err(|error| PyValueError::new_err(format!("invalid regex pattern: {error}")))?;
    let include = include_glob
        .map(compile_glob)
        .transpose()
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    let max_results = max_results.max(1);
    let context_lines = context_lines.min(MAX_CONTEXT_LINES);
    let matches = PyList::empty(py);
    let mut total_matches = 0usize;
    let mut truncated = false;

    for entry in build_walker(&base_path) {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let path = entry.path();
        if !entry.file_type().is_some_and(|kind| kind.is_file()) {
            continue;
        }
        let relative = relative_path(path, &base_path);
        if include
            .as_ref()
            .is_some_and(|glob| !glob.is_match(&relative))
        {
            continue;
        }
        let file_result = match search_file(path, &matcher, context_lines) {
            Ok(file_result) => file_result,
            Err(_) => continue,
        };
        total_matches = total_matches.saturating_add(file_result.total_matches);
        for (line, text) in file_result.lines {
            if matches.len() >= max_results {
                truncated = true;
                continue;
            }
            let item = PyDict::new(py);
            item.set_item("path", relative.as_str())?;
            item.set_item("line", line)?;
            item.set_item("text", text)?;
            matches.append(item)?;
        }
        if total_matches > max_results || matches.len() > max_results {
            truncated = true;
        }
        // Stop walking once we have collected enough matches. Without this the
        // walker keeps searching every remaining file just to count matches,
        // which dominates cost on large trees with a common pattern.
        if matches.len() >= max_results {
            truncated = true;
            break;
        }
    }

    let output = PyDict::new(py);
    output.set_item("matches", matches)?;
    output.set_item("total_matches", total_matches)?;
    output.set_item("truncated", truncated)?;
    output.set_item("context_lines", context_lines)?;
    Ok(output.unbind())
}

#[pyfunction(signature = (base_path, pattern))]
pub(crate) fn glob(py: Python<'_>, base_path: &str, pattern: &str) -> PyResult<Py<PyDict>> {
    if pattern.is_empty() {
        return Err(PyValueError::new_err("pattern must not be empty"));
    }
    let base_path = PathBuf::from(base_path);
    if !base_path.is_absolute() {
        return Err(PyValueError::new_err("base_path must be absolute"));
    }
    if !base_path.is_dir() {
        return Err(PyValueError::new_err(format!(
            "base_path is not a directory: {}",
            base_path.display()
        )));
    }
    let matcher =
        compile_glob(pattern).map_err(|error| PyValueError::new_err(error.to_string()))?;
    let matches = PyList::empty(py);

    for entry in build_walker(&base_path) {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let path = entry.path();
        if path == base_path {
            continue;
        }
        let relative = relative_path(path, &base_path);
        if !matcher.is_match(&relative) {
            continue;
        }
        let item = PyDict::new(py);
        item.set_item("path", relative.as_str())?;
        item.set_item(
            "is_dir",
            entry.file_type().is_some_and(|kind| kind.is_dir()),
        )?;
        if let Ok(metadata) = fs::metadata(path) {
            item.set_item("size", metadata.len())?;
            if let Ok(modified) = metadata.modified() {
                if let Ok(seconds) = modified.duration_since(UNIX_EPOCH) {
                    item.set_item("modified_at_unix", seconds.as_secs_f64())?;
                }
            }
        }
        matches.append(item)?;
    }

    let output = PyDict::new(py);
    output.set_item("matches", matches)?;
    Ok(output.unbind())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn documented_regex_examples_are_supported() {
        for pattern in [r"def\s+stream_agent", r"TODO|FIXME", r"config\.json"] {
            RegexMatcherBuilder::new()
                .build(pattern)
                .unwrap_or_else(|error| panic!("documented pattern {pattern:?} failed: {error}"));
        }
    }

    #[test]
    fn truncates_match_text_at_character_boundary() {
        let text = "中".repeat(MAX_MATCH_TEXT_CHARS + 1);
        let result = truncate_text(&text);
        assert!(result.starts_with(&"中".repeat(MAX_MATCH_TEXT_CHARS)));
        assert!(result.contains("[truncated 1 chars]"));
    }
}
