use std::fs;
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::file_access::{read_text_detect, write_text_as};

const MAX_READ_LIMIT: usize = 10_000;
const MAX_READ_LINE_CHARS: usize = 1_200;
const MAX_READ_OUTPUT_CHARS: usize = 12_000;

#[derive(Debug, thiserror::Error)]
enum ExactEditError {
    #[error("old_string and new_string must be different")]
    StringsMustDiffer,
    #[error("old_string not found in content (check exact text/whitespace/line endings)")]
    OldStringNotFound,
    #[error("old_string found multiple times; provide more context or set replace_all=true")]
    OldStringFoundMultipleTimes,
}

#[derive(Debug, thiserror::Error)]
enum PatchError {
    #[error("patch is empty or invalid")]
    InvalidPatch,
    #[error("failed to apply patch hunk #{0}: context mismatch")]
    HunkFailed(usize),
}

fn absolute_path(raw: &str) -> PyResult<PathBuf> {
    if raw.trim().is_empty() {
        return Err(PyValueError::new_err("file_path is empty"));
    }
    let path = PathBuf::from(raw);
    if !path.is_absolute() {
        return Err(PyValueError::new_err("file_path must be absolute"));
    }
    Ok(path)
}

fn io_error(action: &str, path: &Path, error: std::io::Error) -> PyErr {
    PyOSError::new_err(format!("failed to {action} '{}': {error}", path.display()))
}

fn truncate_line(text: &str) -> (String, bool) {
    if text.chars().count() <= MAX_READ_LINE_CHARS {
        return (text.to_owned(), false);
    }
    let prefix: String = text.chars().take(MAX_READ_LINE_CHARS).collect();
    (format!("{prefix} [truncated]"), true)
}

fn render_with_budget(lines: impl Iterator<Item = String>) -> (String, bool) {
    let mut output = String::new();
    for line in lines {
        let separator = usize::from(!output.is_empty());
        if output.chars().count() + separator + line.chars().count() > MAX_READ_OUTPUT_CHARS {
            return (output, true);
        }
        if !output.is_empty() {
            output.push('\n');
        }
        output.push_str(&line);
    }
    (output, false)
}

#[pyfunction(signature = (file_path, offset=1, limit=200))]
pub(crate) fn read(
    py: Python<'_>,
    file_path: &str,
    offset: usize,
    limit: usize,
) -> PyResult<Py<PyDict>> {
    if offset == 0 {
        return Err(PyValueError::new_err("offset must be >= 1"));
    }
    if limit == 0 {
        return Err(PyValueError::new_err("limit must be >= 1"));
    }
    let path = absolute_path(file_path)?;
    if !path.exists() {
        return Err(PyValueError::new_err(format!(
            "path does not exist: {}",
            path.display()
        )));
    }

    let result = PyDict::new(py);
    result.set_item("path", path.to_string_lossy().as_ref())?;
    if path.is_dir() {
        let mut entries = fs::read_dir(&path)
            .map_err(|error| io_error("read directory", &path, error))?
            .map(|entry| {
                entry.map(|entry| {
                    let entry_path = entry.path();
                    let mut name = entry.file_name().to_string_lossy().to_string();
                    if entry_path.is_dir() {
                        name.push('/');
                    }
                    name
                })
            })
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| io_error("read directory", &path, error))?;
        entries.sort();
        let total_entries = entries.len();
        let mut line_truncated = false;
        let (output, output_truncated) = render_with_budget(entries.into_iter().map(|entry| {
            let (entry, truncated) = truncate_line(&entry);
            line_truncated |= truncated;
            entry
        }));
        result.set_item("kind", "directory")?;
        result.set_item("output", output)?;
        result.set_item("content", py.None())?;
        result.set_item("truncated", output_truncated || line_truncated)?;
        result.set_item("start_line", py.None())?;
        result.set_item("end_line", py.None())?;
        result.set_item("total_lines", py.None())?;
        result.set_item("total_entries", total_entries)?;
        return Ok(result.unbind());
    }

    let (content, _) =
        read_text_detect(&path).map_err(|error| io_error("read file", &path, error))?;
    let lines = content.split_inclusive('\n').collect::<Vec<_>>();
    let total_lines = lines.len();
    let start = offset.saturating_sub(1).min(total_lines);
    let end = start
        .saturating_add(limit.min(MAX_READ_LIMIT))
        .min(total_lines);
    let selected = lines[start..end].concat();
    let display_lines = lines[start..end]
        .iter()
        .map(|line| line.trim_end_matches(['\r', '\n']))
        .collect::<Vec<_>>();
    let mut line_truncated = false;
    let (output, output_truncated) =
        render_with_budget(display_lines.iter().enumerate().map(|(index, line)| {
            let (line, truncated) = truncate_line(line);
            line_truncated |= truncated;
            format!("{}: {line}", start + index + 1)
        }));
    result.set_item("kind", "file")?;
    result.set_item("output", output)?;
    result.set_item("content", selected)?;
    result.set_item(
        "truncated",
        end < total_lines || output_truncated || line_truncated,
    )?;
    result.set_item("start_line", start + 1)?;
    result.set_item("end_line", end)?;
    result.set_item("total_lines", total_lines)?;
    result.set_item("total_entries", py.None())?;
    Ok(result.unbind())
}

fn apply_exact_edit(
    previous: &str,
    old_string: &str,
    new_string: &str,
    replace_all: bool,
) -> Result<(String, usize), ExactEditError> {
    if old_string == new_string {
        return Err(ExactEditError::StringsMustDiffer);
    }
    if old_string.is_empty() {
        return Ok((new_string.to_owned(), 1));
    }

    let candidates = line_ending_candidates(old_string, new_string);
    if replace_all {
        for (old_candidate, new_candidate) in candidates {
            let matches = previous.matches(&old_candidate).count();
            if matches > 0 {
                return Ok((previous.replace(&old_candidate, &new_candidate), matches));
            }
        }
        return Err(ExactEditError::OldStringNotFound);
    }

    let mut ambiguous = false;
    for (old_candidate, new_candidate) in candidates {
        match previous.matches(&old_candidate).count() {
            1 => return Ok((previous.replacen(&old_candidate, &new_candidate, 1), 1)),
            count if count > 1 => ambiguous = true,
            _ => {}
        }
    }
    if ambiguous {
        Err(ExactEditError::OldStringFoundMultipleTimes)
    } else {
        Err(ExactEditError::OldStringNotFound)
    }
}

fn line_ending_candidates(old_string: &str, new_string: &str) -> Vec<(String, String)> {
    let mut candidates = Vec::new();
    push_candidate(
        &mut candidates,
        old_string.to_owned(),
        new_string.to_owned(),
    );
    let old_lf = old_string.replace("\r\n", "\n");
    let new_lf = new_string.replace("\r\n", "\n");
    push_candidate(&mut candidates, old_lf.clone(), new_lf.clone());
    push_candidate(
        &mut candidates,
        old_lf.replace('\n', "\r\n"),
        new_lf.replace('\n', "\r\n"),
    );
    candidates
}

fn push_candidate(candidates: &mut Vec<(String, String)>, old: String, new: String) {
    if !candidates
        .iter()
        .any(|candidate| candidate == &(old.clone(), new.clone()))
    {
        candidates.push((old, new));
    }
}

#[pyfunction(signature = (file_path, old_string, new_string, replace_all=false))]
pub(crate) fn edit(
    py: Python<'_>,
    file_path: &str,
    old_string: &str,
    new_string: &str,
    replace_all: bool,
) -> PyResult<Py<PyDict>> {
    let path = absolute_path(file_path)?;
    if !path.is_file() {
        return Err(PyValueError::new_err(format!(
            "path is not a file: {}",
            path.display()
        )));
    }
    let (content, encoding) =
        read_text_detect(&path).map_err(|error| io_error("read file", &path, error))?;
    let (new_content, replacements) =
        apply_exact_edit(&content, old_string, new_string, replace_all)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
    write_text_as(&path, &new_content, encoding)
        .map_err(|error| io_error("write file", &path, error))?;

    let result = PyDict::new(py);
    result.set_item("path", path.to_string_lossy().as_ref())?;
    result.set_item("replacements", replacements)?;
    Ok(result.unbind())
}

fn apply_unified_diff(content: &str, patch: &str) -> Result<(String, usize), PatchError> {
    let line_ending = if content.contains("\r\n") {
        "\r\n"
    } else {
        "\n"
    };
    let had_trailing_newline = content.ends_with('\n');
    let normalized = content.replace("\r\n", "\n");
    let mut lines = if normalized.is_empty() {
        Vec::new()
    } else {
        normalized
            .trim_end_matches('\n')
            .split('\n')
            .map(str::to_owned)
            .collect::<Vec<_>>()
    };
    let patch_lines = patch.lines().collect::<Vec<_>>();
    let mut hunk_count = 0usize;
    let mut index = 0usize;

    while index < patch_lines.len() {
        if !patch_lines[index].starts_with("@@") {
            index += 1;
            continue;
        }
        hunk_count += 1;
        let mut patch_index = index + 1;
        let mut old_lines = Vec::new();
        let mut new_lines = Vec::new();
        while patch_index < patch_lines.len() && !patch_lines[patch_index].starts_with("@@") {
            let line = patch_lines[patch_index];
            if let Some(value) = line.strip_prefix(' ') {
                old_lines.push(value);
                new_lines.push(value);
            } else if let Some(value) = line.strip_prefix('-') {
                old_lines.push(value);
            } else if let Some(value) = line.strip_prefix('+') {
                new_lines.push(value);
            }
            patch_index += 1;
        }

        let position = if old_lines.is_empty() {
            Some(0)
        } else if old_lines.len() <= lines.len() {
            (0..=lines.len() - old_lines.len()).find(|start| {
                lines[*start..*start + old_lines.len()]
                    .iter()
                    .map(String::as_str)
                    .eq(old_lines.iter().copied())
            })
        } else {
            None
        };
        let Some(position) = position else {
            return Err(PatchError::HunkFailed(hunk_count));
        };
        lines.splice(
            position..position + old_lines.len(),
            new_lines.into_iter().map(str::to_owned),
        );
        index = patch_index;
    }

    if hunk_count == 0 {
        return Err(PatchError::InvalidPatch);
    }
    let mut output = lines.join(line_ending);
    if had_trailing_newline && !output.is_empty() {
        output.push_str(line_ending);
    }
    Ok((output, hunk_count))
}

#[pyfunction(signature = (file_path, patch))]
pub(crate) fn patch(py: Python<'_>, file_path: &str, patch: &str) -> PyResult<Py<PyDict>> {
    let path = absolute_path(file_path)?;
    if !path.is_file() {
        return Err(PyValueError::new_err(format!(
            "path is not a file: {}",
            path.display()
        )));
    }
    let (content, encoding) =
        read_text_detect(&path).map_err(|error| io_error("read file", &path, error))?;
    let (new_content, hunks_applied) = apply_unified_diff(&content, patch)
        .map_err(|error| PyValueError::new_err(error.to_string()))?;
    write_text_as(&path, &new_content, encoding)
        .map_err(|error| io_error("write file", &path, error))?;

    let result = PyDict::new(py);
    result.set_item("path", path.to_string_lossy().as_ref())?;
    result.set_item("hunks_applied", hunks_applied)?;
    Ok(result.unbind())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_edit_accepts_crlf_candidates() {
        let (edited, count) =
            apply_exact_edit("a\r\nb\r\n", "a\nb", "x\ny", false).expect("CRLF edit should apply");
        assert_eq!(edited, "x\r\ny\r\n");
        assert_eq!(count, 1);
    }

    #[test]
    fn exact_edit_rejects_ambiguous_match() {
        let error = apply_exact_edit("a a", "a", "b", false).expect_err("must be ambiguous");
        assert!(matches!(error, ExactEditError::OldStringFoundMultipleTimes));
    }

    #[test]
    fn unified_diff_inserts_into_empty_file() {
        let (content, hunks) =
            apply_unified_diff("", "@@ -0,0 +1 @@\n+new\n").expect("insertion should apply");
        assert_eq!(content, "new");
        assert_eq!(hunks, 1);
    }

    #[test]
    fn unified_diff_preserves_crlf_and_trailing_newline() {
        let (content, _) =
            apply_unified_diff("old\r\nkeep\r\n", "@@ -1,2 +1,2 @@\n-old\n+new\n keep\n")
                .expect("patch should apply");
        assert_eq!(content, "new\r\nkeep\r\n");
    }
}
