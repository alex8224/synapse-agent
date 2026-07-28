use std::sync::LazyLock;

use pyo3::prelude::*;
use pyo3::types::PyDict;
use regex::Regex;

use headroom_port::transforms::{
    code_compressor::{CodeAwareCompressor, CodeCompressorConfig},
    diff_compressor::{DiffCompressor, DiffCompressorConfig},
    log_compressor::{LogCompressor, LogCompressorConfig},
    search_compressor::{SearchCompressor, SearchCompressorConfig},
    smart_crusher::{SmartCrusher, SmartCrusherConfig},
};

pub mod headroom_port;

static SEARCH_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?P<path>.+?)(?::|-)(?P<line>\d+)(?::|-)(?P<body>.*)$")
        .expect("valid search regex")
});
static ERROR_LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(error|fatal|failed|failure|exception|traceback|critical)\b")
        .expect("valid error regex")
});
static LOG_SUMMARY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(passed|failed|skipped|collected|tests? run|exit code)\b")
        .expect("valid log summary regex")
});
static TIMESTAMP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\d{4}-\d{2}-\d{2}(?:[ T]|$)").expect("valid timestamp regex"));

fn detect(content: &str) -> (&'static str, f64) {
    let lines: Vec<&str> = content.lines().collect();
    if lines.is_empty() {
        return ("text", 1.0);
    }
    let sampled = &lines[..lines.len().min(100)];
    let search_count = sampled
        .iter()
        .filter(|line| SEARCH_LINE.is_match(line))
        .count();
    let timestamp_count = sampled
        .iter()
        .filter(|line| TIMESTAMP.is_match(line))
        .count();
    if search_count >= 3.max(sampled.len() / 3) && timestamp_count < 3.max(sampled.len() / 2) {
        return (
            "search",
            (search_count as f64 / sampled.len() as f64 + 0.3).min(1.0),
        );
    }
    if lines.iter().take(20).any(|line| {
        line.starts_with("diff --git")
            || line.starts_with("diff --combined")
            || line.starts_with("diff --cc")
            || line.starts_with("--- a/")
            || line.starts_with("@@")
    }) {
        return ("diff", 0.95);
    }
    let log_markers = sampled
        .iter()
        .filter(|line| ERROR_LINE.is_match(line) || LOG_SUMMARY.is_match(line))
        .count();
    if log_markers >= 3
        || (timestamp_count >= 3.max(sampled.len() / 2)
            && lines.iter().any(|line| ERROR_LINE.is_match(line)))
    {
        return ("log", 0.8);
    }
    if serde_json::from_str::<serde_json::Value>(content)
        .is_ok_and(|item| item.is_array() || item.is_object())
    {
        return ("json", 0.9);
    }
    ("text", 0.3)
}

#[pyfunction]
fn detect_content_type(py: Python<'_>, content: &str) -> Py<PyDict> {
    let (content_type, confidence) = detect(content);
    let output = PyDict::new(py);
    output
        .set_item("content_type", content_type)
        .expect("dict write");
    output
        .set_item("confidence", confidence)
        .expect("dict write");
    output.unbind()
}

#[pyfunction(signature = (content, query=None, max_files=15, max_matches_per_file=5, max_total_matches=30))]
fn compress_search(
    py: Python<'_>,
    content: &str,
    query: Option<&str>,
    max_files: usize,
    max_matches_per_file: usize,
    max_total_matches: usize,
) -> Py<PyDict> {
    let config = SearchCompressorConfig {
        max_files,
        max_matches_per_file,
        max_total_matches,
        enable_ccr: false,
        ..Default::default()
    };
    let (result, stats) =
        SearchCompressor::new(config).compress(content, query.unwrap_or_default(), 1.0);
    let omitted_lines = result.matches_omitted();
    let files_affected = result.files_affected;
    let output = PyDict::new(py);
    output
        .set_item("content", result.compressed)
        .expect("dict write");
    output
        .set_item("transformer", "headroom-search-v1")
        .expect("dict write");
    output
        .set_item("content_type", "search")
        .expect("dict write");
    output
        .set_item("omitted_lines", omitted_lines)
        .expect("dict write");
    output
        .set_item("files_affected", files_affected)
        .expect("dict write");
    output
        .set_item("lines_unparsed", stats.lines_unparsed)
        .expect("dict write");
    output.unbind()
}

#[pyfunction(signature = (content, context_lines=3, max_lines=100, min_lines_for_compression=50, max_warnings=5))]
fn compress_log(
    py: Python<'_>,
    content: &str,
    context_lines: usize,
    max_lines: usize,
    min_lines_for_compression: usize,
    max_warnings: usize,
) -> Py<PyDict> {
    let config = LogCompressorConfig {
        error_context_lines: context_lines,
        max_total_lines: max_lines,
        min_lines_for_ccr: min_lines_for_compression,
        max_warnings,
        enable_ccr: false,
        ..Default::default()
    };
    let (result, stats) = LogCompressor::new(config).compress(content, 1.0);
    let omitted_lines = result.lines_omitted();
    let format = result.format_detected.as_str();
    let output = PyDict::new(py);
    output
        .set_item("content", result.compressed)
        .expect("dict write");
    output
        .set_item("transformer", "headroom-log-v1")
        .expect("dict write");
    output.set_item("content_type", "log").expect("dict write");
    output
        .set_item("omitted_lines", omitted_lines)
        .expect("dict write");
    output.set_item("format", format).expect("dict write");
    output
        .set_item("warnings_deduplicated", stats.warnings_dropped_by_dedupe)
        .expect("dict write");
    output.unbind()
}

#[pyfunction(signature = (content, context_lines=2))]
fn compress_diff(py: Python<'_>, content: &str, context_lines: usize) -> Py<PyDict> {
    let config = DiffCompressorConfig {
        max_context_lines: context_lines,
        enable_ccr: false,
        ..Default::default()
    };
    let result = DiffCompressor::new(config).compress(content, "");
    let output = PyDict::new(py);
    output
        .set_item("content", result.compressed)
        .expect("dict write");
    output
        .set_item("transformer", "headroom-diff-v1")
        .expect("dict write");
    output.set_item("content_type", "diff").expect("dict write");
    output
        .set_item(
            "omitted_lines",
            result
                .original_line_count
                .saturating_sub(result.compressed_line_count),
        )
        .expect("dict write");
    output
        .set_item("files_affected", result.files_affected)
        .expect("dict write");
    output
        .set_item("hunks_kept", result.hunks_kept)
        .expect("dict write");
    output.unbind()
}

#[pyfunction(signature = (content, query="", bias=1.0))]
fn crush_json(py: Python<'_>, content: &str, query: &str, bias: f64) -> Py<PyDict> {
    let result = SmartCrusher::new(SmartCrusherConfig::default()).crush(content, query, bias);
    let output = PyDict::new(py);
    output
        .set_item("content", result.compressed)
        .expect("dict write");
    output
        .set_item("transformer", "headroom-smart-crusher-v1")
        .expect("dict write");
    output.set_item("content_type", "json").expect("dict write");
    output
        .set_item("was_modified", result.was_modified)
        .expect("dict write");
    output
        .set_item("strategy", result.strategy)
        .expect("dict write");
    output.unbind()
}

#[pyfunction(signature = (content, language=None, context=""))]
fn compress_code(
    py: Python<'_>,
    content: &str,
    language: Option<&str>,
    context: &str,
) -> Py<PyDict> {
    let result = CodeAwareCompressor::new(CodeCompressorConfig::default())
        .compress_with(content, language, context);
    let output = PyDict::new(py);
    output
        .set_item("content", result.compressed)
        .expect("dict write");
    output
        .set_item("transformer", "headroom-code-v1")
        .expect("dict write");
    output.set_item("content_type", "code").expect("dict write");
    output
        .set_item("language", result.language.value())
        .expect("dict write");
    output
        .set_item("syntax_valid", result.syntax_valid)
        .expect("dict write");
    output
        .set_item("compressed_bodies", result.compressed_bodies)
        .expect("dict write");
    output.unbind()
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(detect_content_type, module)?)?;
    module.add_function(wrap_pyfunction!(compress_search, module)?)?;
    module.add_function(wrap_pyfunction!(compress_log, module)?)?;
    module.add_function(wrap_pyfunction!(compress_diff, module)?)?;
    module.add_function(wrap_pyfunction!(crush_json, module)?)?;
    module.add_function(wrap_pyfunction!(compress_code, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_timestamped_log_before_search() {
        let content = (0..8)
            .map(|n| format!("2026-01-01 INFO item={n}"))
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(detect(&content).0, "text");
        let error_log = format!("{content}\n2026-01-01 ERROR boom\n2026-01-01 FAILED done");
        assert_eq!(detect(&error_log).0, "log");
    }

    #[test]
    fn search_keeps_relevant_ripgrep_context_with_a_global_budget() {
        let content = "src/a.py-40-before\nsrc/a.py:42:def process():\nsrc/a.py-43-after\nsrc/b.py:1: TODO later\nsrc/b.py:2: plain";
        let config = SearchCompressorConfig {
            max_matches_per_file: 3,
            max_total_matches: 4,
            enable_ccr: false,
            ..Default::default()
        };
        let (result, _) = SearchCompressor::new(config).compress(content, "process todo", 1.0);
        assert!(result.compressed.contains("src/a.py:42:def process():"));
        assert!(result.compressed.contains("TODO later"));
        assert!(result.compressed.contains("src/a.py:40:before"));
        assert!(result.compressed.contains("src/a.py:43:after"));
        assert!(result.matches_omitted() <= 1);
    }

    #[test]
    fn log_deduplicates_normalised_warnings_and_keeps_error() {
        let mut lines = (0..20)
            .map(|n| format!("WARN retry attempt={n} path=/tmp/{n}"))
            .collect::<Vec<_>>();
        lines.extend([
            "FATAL DATABASE_DEADLOCK".to_string(),
            "  File store.py, line 42".to_string(),
            "DatabaseError: deadlock".to_string(),
        ]);
        lines.extend((0..50).map(|n| format!("INFO done={n}")));
        let config = LogCompressorConfig {
            max_warnings: 2,
            min_lines_for_ccr: 20,
            enable_ccr: false,
            ..Default::default()
        };
        let (result, stats) = LogCompressor::new(config).compress(&lines.join("\n"), 1.0);
        assert!(result.compressed.contains("DATABASE_DEADLOCK"));
        assert!(result.compressed.contains("DatabaseError: deadlock"));
        assert!(stats.warnings_dropped_by_dedupe > 0);
    }

    #[test]
    fn diff_keeps_headers_and_changes() {
        let content =
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n context";
        let config = DiffCompressorConfig {
            max_context_lines: 0,
            enable_ccr: false,
            min_lines_for_ccr: 0,
            ..Default::default()
        };
        let result = DiffCompressor::new(config).compress(content, "");
        assert!(result.compressed.contains("-old"));
        assert!(result.compressed.contains("+new"));
    }
}
