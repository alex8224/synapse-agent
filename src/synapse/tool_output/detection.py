"""Content classification rules for tool-output transformation."""
from __future__ import annotations

import json
import re
from typing import Any

from synapse.tool_output.models import ContentType, Detection

_SEARCH_LINE = re.compile(
    r"^(?P<path>.+?)(?P<separator>[:\-])(?P<line>\d+)(?P=separator)(?P<body>.*)$"
)
_ERROR_LINE = re.compile(
    r"\b(error|fatal|failed|failure|exception|traceback|critical)\b", re.I
)
_LOG_SUMMARY = re.compile(r"\b(passed|failed|skipped|collected|tests? run|exit code)\b", re.I)
_NUMBERED_SOURCE_LINE = re.compile(r"^(?P<indent>\s*)\d+(?:\.\d+)?\t(?P<body>.*)$")
_CODE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
        ".kt", ".php", ".py", ".pyi", ".rb", ".rs", ".scala", ".sh", ".swift", ".ts",
        ".tsx",
    }
)

def _strip_numbered_source_lines(content: str) -> tuple[str, int, int]:
    """Return a detection-only view without read_file's cat-n line prefixes."""
    normalized: list[str] = []
    numbered = 0
    non_empty = 0
    for line in content.splitlines():
        if line.strip():
            non_empty += 1
        match = _NUMBERED_SOURCE_LINE.match(line)
        if match:
            numbered += 1
            normalized.append(match.group("indent") + match.group("body"))
        else:
            normalized.append(line)
    return "\n".join(normalized), numbered, non_empty


def _code_marker_count(content: str) -> int:
    return sum(
        bool(
            re.match(
                r"^\s*(?:async\s+def|def|class|function|func|fn|import|from|use)\b",
                line,
            )
        )
        for line in content.splitlines()[:200]
    )


def _diff_bloat_metadata(content: str) -> dict[str, Any]:
    total_lines = 0
    change_lines = 0
    context_lines = 0
    in_hunk = False
    for line in content.splitlines():
        total_lines += 1
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith("diff --git"):
            in_hunk = False
            continue
        if not in_hunk or line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            change_lines += 1
        elif line.startswith(" "):
            context_lines += 1
    denominator = context_lines + change_lines
    context_ratio = context_lines / denominator if denominator else 0.0
    normal_context_ratio = 0.6
    bloat_score = (
        max(0.0, min(1.0, (context_ratio - normal_context_ratio) / (1 - normal_context_ratio)))
        if denominator
        else 0.0
    )
    return {
        "total_lines": total_lines,
        "change_lines": change_lines,
        "context_lines": context_lines,
        "context_ratio": round(context_ratio, 4),
        "bloat_score": round(bloat_score, 4),
        "dense_diff": bool(total_lines >= 50 and context_ratio <= normal_context_ratio),
    }


def detect_content_type(content: str) -> Detection:
    lines = content.splitlines()
    if not lines:
        return Detection(ContentType.TEXT, 1.0)
    search_count = sum(bool(_SEARCH_LINE.match(line)) for line in lines[:100])
    timestamp_count = sum(
        bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:[ T]|$)", line)) for line in lines[:100]
    )
    enough_search_lines = search_count >= max(3, len(lines[:100]) // 3)
    mostly_timestamped = timestamp_count >= max(3, len(lines[:100]) // 2)
    if enough_search_lines and not mostly_timestamped:
        return Detection(
            ContentType.SEARCH, min(1.0, search_count / max(1, len(lines[:100])) + 0.3)
        )
    if any(line.startswith(("diff --git", "--- a/", "+++ b/", "@@")) for line in lines[:20]):
        return Detection(ContentType.DIFF, 0.95)
    git_summary_markers = sum(
        bool(
            re.match(
                r"^(?:Merge made by the .+ strategy\.|(?:create|delete) mode \d+ |"
                r"rename .+ => .+|\s*\d+ files? changed(?:,|$)|\s*\d+ insertions?\(\+\)|"
                r"\s*\d+ deletions?\(-\)| .+\s+\|\s+\d+\s+[+\-]+$)",
                line,
            )
        )
        for line in lines[:200]
    )
    if git_summary_markers >= 2:
        return Detection(ContentType.GIT_SUMMARY, min(0.95, 0.45 + git_summary_markers / 20))
    sampled = lines[:100]
    log_markers = sum(
        bool(_ERROR_LINE.search(line) or _LOG_SUMMARY.search(line)) for line in sampled
    )
    timestamped = timestamp_count
    if log_markers >= 3 or (
        timestamped >= max(3, len(sampled) // 2) and any(_ERROR_LINE.search(line) for line in lines)
    ):
        return Detection(ContentType.LOG, 0.8)
    code_markers = _code_marker_count(content)
    if code_markers >= 3:
        return Detection(ContentType.CODE, min(0.95, 0.4 + code_markers / max(1, len(lines[:200]))))
    try:
        parsed = json.loads(content)
        if isinstance(parsed, (list, dict)):
            return Detection(ContentType.JSON, 0.9)
    except (ValueError, TypeError):
        pass
    return Detection(ContentType.TEXT, 0.3)


def _critical_lines(content: str, content_type: ContentType) -> list[str]:
    lines = content.splitlines()
    if content_type is ContentType.LOG:
        return [
            line
            for line in lines
            if _ERROR_LINE.search(line) or re.search(r"\b[A-Za-z_]+(?:Error|Exception):", line)
        ]
    if content_type is ContentType.DIFF:
        return [
            line
            for line in lines
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
    if content_type is ContentType.GIT_SUMMARY:
        return [
            line
            for line in lines
            if re.match(
                r"^(?:Merge made by the .+ strategy\.|\s*\d+ files? changed(?:,|$)|"
                r"\s*\d+ insertions?\(\+\)|\s*\d+ deletions?\(-\))",
                line,
            )
        ]
    return []
