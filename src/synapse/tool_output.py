"""Tool-output transformation pipeline with reversible SQLite storage.

The middleware stores an original result only when its model-visible content is
rewritten.  Transformers are deterministic and keep the original available via
``tool-output://`` references for exact paging or targeted local retrieval.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
import threading
import time
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

_REFERENCE_PREFIX = "tool-output://"
_SEARCH_LINE = re.compile(
    r"^(?P<path>.+?)(?P<separator>[:\-])(?P<line>\d+)(?P=separator)(?P<body>.*)$"
)
_ERROR_LINE = re.compile(r"\b(error|fatal|failed|failure|exception|traceback|critical)\b", re.I)
_WARNING_LINE = re.compile(r"\b(warn|warning|todo|fixme)\b", re.I)
_STACK_LINE = re.compile(r"(^\s+at\s+|^\s*File .+, line \d+|^\s*\d+\s*\||^\s*-->\s+)")
_NUMBER_OR_PATH = re.compile(r"\b\d+\b|(?:[A-Za-z]:)?[/\\][\w./\\-]+")
_LOG_SUMMARY = re.compile(r"\b(passed|failed|skipped|collected|tests? run|exit code)\b", re.I)
_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)


class ContentType(StrEnum):
    SEARCH = "search"
    LOG = "log"
    DIFF = "diff"
    JSON = "json"
    CODE = "code"
    TEXT = "text"


@dataclass(frozen=True)
class Detection:
    content_type: ContentType
    confidence: float


@dataclass(frozen=True)
class TransformContext:
    tool_name: str
    status: str
    query: str = ""


@dataclass(frozen=True)
class TransformResult:
    content: str
    transformer: str
    content_type: ContentType
    critical_total: int
    critical_retained: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolOutputRecord:
    ref: str
    thread_id: str
    checkpoint_ns: str
    tool_call_id: str
    tool_name: str
    status: str
    content: str
    size_bytes: int
    sha256: str
    created_at: str


@dataclass(frozen=True)
class TransformEvent:
    content_type: str
    transformer: str
    outcome: str
    original_bytes: int
    visible_bytes: int
    duration_ms: float
    critical_total: int
    critical_retained: int
    ref_created: bool
    execution_path: str = "python_only"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["saved_bytes"] = max(0, self.original_bytes - self.visible_bytes)
        data["savings_ratio"] = (
            round(1 - self.visible_bytes / self.original_bytes, 4) if self.original_bytes else 0.0
        )
        return data


def content_to_text(content: Any) -> str:
    """Produce stable UTF-8 text for a ToolMessage content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes | bytearray):
        return bytes(content).decode("utf-8", errors="replace")
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(content)


class ToolOutputRepository:
    """Content-addressed SQLite store for rewritten tool outputs.

    Blobs are zlib-compressed and deduplicated by SHA-256.  References remain
    thread-scoped, so a valid reference cannot be used to read another thread.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _setup(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tool_output_blobs (
                    sha256 TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_output_refs (
                    ref TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    checkpoint_ns TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sha256 TEXT NOT NULL REFERENCES tool_output_blobs(sha256),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_refs_thread
                    ON tool_output_refs(thread_id);
                CREATE TABLE IF NOT EXISTS tool_output_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    ref TEXT,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_events_thread
                    ON tool_output_events(thread_id);
                CREATE TABLE IF NOT EXISTS tool_output_retrieval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    returned_bytes INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_retrieval_events_thread
                    ON tool_output_retrieval_events(thread_id);
                """
            )

    @staticmethod
    def parse_ref(ref: str) -> str | None:
        if not isinstance(ref, str) or not ref.startswith(_REFERENCE_PREFIX):
            return None
        value = ref[len(_REFERENCE_PREFIX) :]
        return value if value and "/" not in value else None

    def put(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        tool_call_id: str = "",
        tool_name: str = "tool",
        status: str = "success",
        content: str,
    ) -> ToolOutputRecord:
        raw = content.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        ref = f"{_REFERENCE_PREFIX}{uuid.uuid4().hex}"
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tool_output_blobs("
                "sha256, content, size_bytes, created_at) VALUES (?, ?, ?, ?)",
                (digest, sqlite3.Binary(zlib.compress(raw, level=6)), len(raw), created),
            )
            conn.execute(
                "INSERT INTO tool_output_refs("
                "ref, thread_id, checkpoint_ns, tool_call_id, tool_name, status, "
                "sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ref, thread_id, checkpoint_ns, tool_call_id, tool_name, status, digest, created),
            )
        return ToolOutputRecord(
            ref,
            thread_id,
            checkpoint_ns,
            tool_call_id,
            tool_name,
            status,
            content,
            len(raw),
            digest,
            created,
        )

    def get(self, ref: str, *, expected_thread_id: str | None = None) -> ToolOutputRecord | None:
        if self.parse_ref(ref) is None:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """SELECT r.ref, r.thread_id, r.checkpoint_ns, r.tool_call_id, r.tool_name,
                          r.status, r.sha256, r.created_at, b.content, b.size_bytes
                   FROM tool_output_refs r JOIN tool_output_blobs b ON b.sha256 = r.sha256
                   WHERE r.ref = ?""",
                (ref,),
            ).fetchone()
        if row is None or (expected_thread_id and row["thread_id"] != expected_thread_id):
            return None
        try:
            raw = zlib.decompress(row["content"])
        except zlib.error:
            return None
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            return None
        return ToolOutputRecord(
            ref=row["ref"],
            thread_id=row["thread_id"],
            checkpoint_ns=row["checkpoint_ns"],
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            content=raw.decode("utf-8", errors="replace"),
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            created_at=row["created_at"],
        )

    def record_event(
        self, thread_id: str, event: TransformEvent, *, ref: str | None = None
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_events(thread_id, ref, event_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    thread_id,
                    ref,
                    json.dumps(event.as_dict(), ensure_ascii=False),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )

    def record_retrieval(
        self,
        *,
        thread_id: str,
        ref: str,
        mode: str,
        returned_bytes: int,
        duration_ms: float,
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_retrieval_events("
                "thread_id, ref, mode, returned_bytes, duration_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    ref,
                    mode,
                    max(0, int(returned_bytes)),
                    max(0.0, float(duration_ms)),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )

    def events(self, *, thread_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent transformation decisions with linked retrieval totals."""
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded_limit = max(1, min(500, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, ref, event_json, created_at "
                f"FROM tool_output_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded_limit),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                event = json.loads(row["event_json"])
                retrieval = 0
                if row["ref"]:
                    retrieval_row = conn.execute(
                        "SELECT COALESCE(SUM(returned_bytes), 0) AS bytes "
                        "FROM tool_output_retrieval_events WHERE ref = ?",
                        (row["ref"],),
                    ).fetchone()
                    retrieval = int(retrieval_row["bytes"])
                result.append(
                    {
                        "id": int(row["id"]),
                        "thread_id": row["thread_id"],
                        "ref": row["ref"],
                        "created_at": row["created_at"],
                        "retrieval_bytes": retrieval,
                        **event,
                    }
                )
        return result

    def stats(self, *, thread_id: str | None = None) -> dict[str, Any]:
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT event_json FROM tool_output_events{where}", params
            ).fetchall()
        with self._lock, self._connection() as conn:
            retrieval_rows = conn.execute(
                f"SELECT returned_bytes FROM tool_output_retrieval_events{where}", params
            ).fetchall()
        events = [json.loads(row["event_json"]) for row in rows]
        retrieval_bytes = sum(int(row["returned_bytes"]) for row in retrieval_rows)
        original = sum(int(item["original_bytes"]) for item in events)
        visible = sum(int(item["visible_bytes"]) for item in events)
        transformed = sum(item["outcome"] == "transformed" for item in events)
        critical_total = sum(int(item["critical_total"]) for item in events)
        critical_retained = sum(int(item["critical_retained"]) for item in events)
        execution_paths: dict[str, int] = {}
        for item in events:
            path = str(item.get("execution_path", "legacy_unknown"))
            execution_paths[path] = execution_paths.get(path, 0) + 1
        return {
            "outputs_considered": len(events),
            "transformed": transformed,
            "original_bytes": original,
            "visible_bytes": visible,
            "saved_bytes": max(0, original - visible),
            "retrieval_bytes": retrieval_bytes,
            "effective_saved_bytes": max(0, original - visible - retrieval_bytes),
            "savings_ratio": round(1 - visible / original, 4) if original else 0.0,
            "effective_savings_ratio": (
                round(max(0, original - visible - retrieval_bytes) / original, 4)
                if original
                else 0.0
            ),
            "critical_retention": round(critical_retained / critical_total, 4)
            if critical_total
            else 1.0,
            "execution_paths": execution_paths,
        }

    def search(
        self,
        ref: str,
        query: str,
        *,
        expected_thread_id: str | None = None,
        max_results: int = 20,
        context_lines: int = 2,
    ) -> list[tuple[int, str]]:
        record = self.get(ref, expected_thread_id=expected_thread_id)
        terms = {item.casefold() for item in _TOKEN.findall(query) if len(item) > 1}
        if record is None or not terms:
            return []
        lines = record.content.splitlines()
        scored = []
        for index, line in enumerate(lines):
            text = line.casefold()
            score = sum(term in text for term in terms) + (4 if _ERROR_LINE.search(line) else 0)
            if score:
                scored.append((score, index))
        selected: list[tuple[int, str]] = []
        seen: set[int] = set()
        for _, index in sorted(scored, reverse=True)[: max(1, min(50, max_results))]:
            start, end = (
                max(0, index - max(0, context_lines)),
                min(len(lines), index + context_lines + 1),
            )
            for line_no in range(start, end):
                if line_no not in seen:
                    seen.add(line_no)
                    selected.append((line_no, lines[line_no]))
        return sorted(selected)


class ToolOutputTransformer(Protocol):
    name: str

    def transform(self, content: str, context: TransformContext) -> TransformResult: ...


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
    sampled = lines[:100]
    log_markers = sum(
        bool(_ERROR_LINE.search(line) or _LOG_SUMMARY.search(line)) for line in sampled
    )
    timestamped = timestamp_count
    if log_markers >= 3 or (
        timestamped >= max(3, len(sampled) // 2) and any(_ERROR_LINE.search(line) for line in lines)
    ):
        return Detection(ContentType.LOG, 0.8)
    code_markers = sum(
        bool(
            re.match(
                r"^\s*(?:async\s+def|def|class|function|func|fn|import|from|use)\b",
                line,
            )
        )
        for line in lines[:200]
    )
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
    if content_type in {ContentType.SEARCH, ContentType.LOG}:
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
    return []


def _result(
    content: str,
    body: str,
    *,
    name: str,
    content_type: ContentType,
    metadata: dict[str, Any] | None = None,
) -> TransformResult:
    critical = _critical_lines(content, content_type)
    return TransformResult(
        body,
        name,
        content_type,
        len(critical),
        sum(item in body for item in critical),
        metadata or {},
    )


class SearchTransformer:
    """Headroom-inspired search parsing, scoring, and bounded selection."""

    name = "search-v1"
    content_types = frozenset({ContentType.SEARCH})

    def __init__(
        self, *, max_files: int = 15, max_matches_per_file: int = 5, max_total_matches: int = 30
    ) -> None:
        self.max_files = max_files
        self.max_matches_per_file = max_matches_per_file
        self.max_total_matches = max_total_matches

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        grouped: dict[str, list[tuple[int, str]]] = {}
        for raw in content.splitlines():
            match = _SEARCH_LINE.match(raw)
            if match:
                grouped.setdefault(match.group("path"), []).append((int(match.group("line")), raw))
        if not grouped:
            return TransformResult(content, self.name, ContentType.SEARCH, 0, 0)
        terms = {term.casefold() for term in _TOKEN.findall(context.query) if len(term) > 2}

        def score(raw: str) -> float:
            value = sum(0.3 for term in terms if term in raw.casefold())
            if _ERROR_LINE.search(raw):
                value += 0.5
            elif _WARNING_LINE.search(raw):
                value += 0.3
            return min(1.0, value)

        files = sorted(
            grouped.items(), key=lambda item: sum(score(raw) for _, raw in item[1]), reverse=True
        )[: self.max_files]
        selected: list[tuple[str, int, str]] = []
        summaries: list[str] = []
        for path, matches in files:
            if len(selected) >= self.max_total_matches:
                summaries.append(f"{path}: {len(matches)} matches omitted")
                continue
            ranked = sorted(matches, key=lambda item: score(item[1]), reverse=True)
            keep = {matches[0], matches[-1], *ranked[: self.max_matches_per_file]}
            shown = sorted(keep)[: self.max_total_matches - len(selected)]
            selected.extend((path, line, raw) for line, raw in shown)
            if len(shown) < len(matches):
                summaries.append(f"{path}: {len(matches) - len(shown)} matches omitted")
        selected.sort(key=lambda item: (item[0], item[1]))
        omitted = sum(int(item.split(": ")[-1].split()[0]) for item in summaries)
        body = "\n".join(
            [
                f"[search results compressed: {len(grouped)} files, {omitted} matches omitted]",
                *(raw for _, _, raw in selected),
                *summaries,
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.SEARCH,
            metadata={
                "files": len(grouped),
                "omitted_matches": omitted,
                "file_summaries": summaries,
            },
        )


class LogTransformer:
    """Headroom-inspired log parsing, warning dedupe, ranking, and context."""

    name = "log-v1"
    content_types = frozenset({ContentType.LOG})

    def __init__(
        self,
        *,
        context_lines: int = 3,
        max_lines: int = 100,
        min_lines_for_compression: int = 50,
        max_warnings: int = 5,
    ) -> None:
        self.context_lines = context_lines
        self.max_lines = max_lines
        self.min_lines_for_compression = min_lines_for_compression
        self.max_warnings = max_warnings

    @staticmethod
    def _normalise(line: str) -> str:
        return _NUMBER_OR_PATH.sub("<value>", line.casefold())

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        if len(lines) < self.min_lines_for_compression:
            return TransformResult(content, self.name, ContentType.LOG, 0, 0)
        errors = [
            i
            for i, line in enumerate(lines)
            if _ERROR_LINE.search(line) or re.search(r"\b[A-Za-z_]+(?:Error|Exception):", line)
        ]
        warnings = [
            i for i, line in enumerate(lines) if _WARNING_LINE.search(line) and i not in errors
        ]
        summaries = [i for i, line in enumerate(lines) if _LOG_SUMMARY.search(line)]
        selected: set[int] = (
            set(range(min(3, len(lines))))
            | set(range(max(0, len(lines) - 3), len(lines)))
            | set(summaries)
        )
        for index in errors:
            selected.update(
                range(
                    max(0, index - self.context_lines),
                    min(len(lines), index + self.context_lines + 1),
                )
            )
        seen: set[str] = set()
        warning_count = 0
        for index in warnings:
            key = self._normalise(lines[index])
            if key in seen or warning_count >= self.max_warnings:
                continue
            seen.add(key)
            warning_count += 1
            selected.add(index)
        for index, line in enumerate(lines):
            if _STACK_LINE.search(line) and any(abs(index - error) <= 20 for error in errors):
                selected.add(index)
        ordered = sorted(selected)
        retained_warning_keys: set[str] = set()
        filtered: list[int] = []
        for index in ordered:
            line = lines[index]
            if _WARNING_LINE.search(line) and index not in errors:
                key = self._normalise(line)
                if key in retained_warning_keys or len(retained_warning_keys) >= self.max_warnings:
                    continue
                retained_warning_keys.add(key)
            filtered.append(index)
        ordered = filtered
        if len(ordered) > self.max_lines:
            priority = sorted(ordered, key=lambda i: (i not in errors, i not in summaries, i))
            ordered = sorted(priority[: self.max_lines])
        omitted = len(lines) - len(ordered)
        stats = {
            "errors": len(errors),
            "warnings": len(warnings),
            "selected": len(ordered),
            "omitted_lines": max(0, omitted),
        }
        summary = (
            f"[log compressed: {max(0, omitted)} lines omitted; "
            f"ERROR={len(errors)} WARN={len(warnings)}]"
        )
        body = "\n".join([summary, *(lines[index] for index in ordered)])
        return _result(content, body, name=self.name, content_type=ContentType.LOG, metadata=stats)


class DiffTransformer:
    name = "diff-v1"
    content_types = frozenset({ContentType.DIFF})

    def __init__(self, *, context_lines: int = 2) -> None:
        self.context_lines = context_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        keep: set[int] = set()
        for index, line in enumerate(lines):
            structural = line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@"))
            changed = line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            if structural:
                keep.add(index)
            if changed:
                keep.update(
                    range(
                        max(0, index - self.context_lines),
                        min(len(lines), index + self.context_lines + 1),
                    )
                )
        ordered = sorted(keep)
        omitted = max(0, len(lines) - len(ordered))
        body = "\n".join(
            [f"[diff compressed: {omitted} context lines omitted]", *(lines[i] for i in ordered)]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.DIFF,
            metadata={"omitted_lines": omitted},
        )


class JsonTransformer:
    name = "json-v1"
    content_types = frozenset({ContentType.JSON})

    def __init__(self, *, max_items: int = 20) -> None:
        self.max_items = max_items

    @staticmethod
    def _score(value: Any, query_terms: set[str]) -> int:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold()
        score = sum(term in text for term in query_terms)
        return (
            score + 5
            if any(word in text for word in ("error", "failed", "fatal", "exception"))
            else score
        )

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return TransformResult(content, self.name, ContentType.JSON, 0, 0)
        query_terms = {term.casefold() for term in _TOKEN.findall(context.query) if len(term) > 1}
        if isinstance(data, list) and len(data) > self.max_items:
            ranked = sorted(
                enumerate(data),
                key=lambda pair: (self._score(pair[1], query_terms), -pair[0]),
                reverse=True,
            )
            indexes = sorted({0, len(data) - 1, *(index for index, _ in ranked[: self.max_items])})
            selected = [data[index] for index in indexes]
            body_data: Any = {
                "_summary": {
                    "original_items": len(data),
                    "shown_items": len(selected),
                    "omitted_items": len(data) - len(selected),
                },
                "items": selected,
            }
        elif isinstance(data, dict) and len(data) > self.max_items:
            ranked_keys = sorted(
                data,
                key=lambda key: (self._score({key: data[key]}, query_terms), key),
                reverse=True,
            )
            selected_keys = ranked_keys[: self.max_items]
            body_data = {
                "_summary": {
                    "original_keys": len(data),
                    "shown_keys": len(selected_keys),
                    "omitted_keys": len(data) - len(selected_keys),
                },
                "values": {key: data[key] for key in selected_keys},
            }
        else:
            return TransformResult(content, self.name, ContentType.JSON, 0, 0)
        body = json.dumps(body_data, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.JSON,
            metadata={"structured": True},
        )


class CodeTransformer:
    name = "code-v1"
    content_types = frozenset({ContentType.CODE})
    _signature = re.compile(
        r"^(?P<indent>\s*)(?:async\s+def|def|class|function|func|fn|export\s+(?:async\s+)?function)\s+[^:{(]+"
    )

    def __init__(self, *, body_lines: int = 3) -> None:
        self.body_lines = body_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        signatures = [index for index, line in enumerate(lines) if self._signature.match(line)]
        if len(signatures) < 3:
            return TransformResult(content, self.name, ContentType.CODE, 0, 0)
        keep: set[int] = set()
        for index, line in enumerate(lines):
            if line.startswith(("import ", "from ", "use ", "package ", "#include")):
                keep.add(index)
        for position, start in enumerate(signatures):
            end = signatures[position + 1] if position + 1 < len(signatures) else len(lines)
            keep.update(range(start, min(end, start + self.body_lines + 1)))
        ordered = sorted(keep)
        omitted = len(lines) - len(ordered)
        body = "\n".join(
            [f"[code compressed: {omitted} body lines omitted]", *(lines[i] for i in ordered)]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.CODE,
            metadata={"omitted_lines": omitted},
        )


class GenericTransformer:
    name = "generic-v1"
    content_types = frozenset({ContentType.TEXT})

    def __init__(
        self, *, head_lines: int = 40, tail_lines: int = 16, max_anchor_lines: int = 12
    ) -> None:
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.max_anchor_lines = max_anchor_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        if len(lines) <= self.head_lines + self.tail_lines:
            return TransformResult(content, self.name, ContentType.TEXT, 0, 0)
        anchors = [
            line for line in lines[self.head_lines : -self.tail_lines] if _ERROR_LINE.search(line)
        ][: self.max_anchor_lines]
        omitted = len(lines) - self.head_lines - self.tail_lines - len(anchors)
        body = "\n".join(
            [
                *lines[: self.head_lines],
                f"...[{max(0, omitted)} lines omitted]...",
                *anchors,
                *lines[-self.tail_lines :],
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.TEXT,
            metadata={"omitted_lines": max(0, omitted)},
        )


def load_transformer_plugins(specs: list[str] | tuple[str, ...]) -> list[ToolOutputTransformer]:
    """Load optional local transformers from ``module:attribute`` specifications."""
    plugins: list[ToolOutputTransformer] = []
    for spec in specs:
        module_name, separator, attribute = str(spec).partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"invalid tool-output transformer plugin: {spec!r}")
        factory = getattr(importlib.import_module(module_name), attribute)
        plugin = factory() if isinstance(factory, type) else factory
        if not callable(getattr(plugin, "transform", None)) or not getattr(plugin, "name", ""):
            raise TypeError(f"invalid tool-output transformer plugin: {spec!r}")
        plugins.append(plugin)
    return plugins


class NativeTransformer:
    """Adapter for the optional prebuilt Apache-2.0 native compression wheel.

    Native code receives only text and returns a compact view. Synapse retains
    responsibility for critical-fact validation, fallback, metrics, and SQLite
    reversible storage in ``ToolOutputTransformPipeline``.
    """

    def __init__(
        self,
        *,
        name: str,
        content_type: ContentType,
        native_transform: Any,
    ) -> None:
        self.name = name
        self.content_types = frozenset({content_type})
        self._content_type = content_type
        self._native_transform = native_transform

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        try:
            payload = self._native_transform(content, context)
            transformed = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(transformed, str):
                raise TypeError("native compressor returned no text content")
            metadata = {
                "native": True,
                **{key: value for key, value in payload.items() if key != "content"},
            }
            return _result(
                content,
                transformed,
                name=str(payload.get("transformer") or self.name),
                content_type=self._content_type,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            return TransformResult(
                content,
                self.name,
                self._content_type,
                0,
                0,
                {"native": True, "fallback": "native_error", "error": type(exc).__name__},
            )


def load_native_transformers(*, enabled: bool = True) -> list[ToolOutputTransformer]:
    """Load native transformers when the optional wheel is installed.

    Import failure is expected on unsupported platforms and leaves the Python
    deterministic transformers active. The package is never built at install
    time by Synapse itself.
    """
    if not enabled:
        return []
    try:
        import synapse_tool_compress_core as native
    except (ImportError, OSError):
        return []

    return [
        NativeTransformer(
            name="headroom-search-v1",
            content_type=ContentType.SEARCH,
            native_transform=lambda content, context: native.compress_search(
                content, query=context.query
            ),
        ),
        NativeTransformer(
            name="headroom-log-v1",
            content_type=ContentType.LOG,
            native_transform=lambda content, _context: native.compress_log(content),
        ),
        NativeTransformer(
            name="headroom-diff-v1",
            content_type=ContentType.DIFF,
            native_transform=lambda content, context: native.compress_diff(content),
        ),
        NativeTransformer(
            name="headroom-smart-crusher-v1",
            content_type=ContentType.JSON,
            native_transform=lambda content, context: native.crush_json(
                content, query=context.query
            ),
        ),
        NativeTransformer(
            name="headroom-code-v1",
            content_type=ContentType.CODE,
            native_transform=lambda content, context: native.compress_code(
                content, context=context.query
            ),
        ),
    ]


class ToolOutputTransformPipeline:
    """Deterministic Headroom-inspired algorithms with optional local plugins."""

    def __init__(
        self,
        *,
        transformers: list[ToolOutputTransformer] | None = None,
        disabled_types: set[str] | frozenset[str] | None = None,
        use_native: bool = True,
    ) -> None:
        builtins: list[ToolOutputTransformer] = [
            SearchTransformer(),
            LogTransformer(),
            DiffTransformer(),
            JsonTransformer(),
            CodeTransformer(),
            GenericTransformer(),
        ]
        self.transformers = [
            *(transformers or []),
            *load_native_transformers(enabled=use_native),
            *builtins,
        ]
        self.disabled_types = frozenset(str(item) for item in (disabled_types or set()))

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        detection = detect_content_type(content)
        if detection.content_type.value in self.disabled_types:
            return TransformResult(
                content,
                "disabled",
                detection.content_type,
                0,
                0,
                {"fallback": "disabled"},
            )
        transformer = next(
            (
                item
                for item in self.transformers
                if detection.content_type in getattr(item, "content_types", set())
            ),
            GenericTransformer(),
        )
        result = transformer.transform(content, context)
        execution_path = "native" if isinstance(transformer, NativeTransformer) else "python_only"
        native_result_is_unsafe_or_unhelpful = isinstance(transformer, NativeTransformer) and (
            result.metadata.get("fallback") == "native_error"
            or result.critical_retained < result.critical_total
            or len(result.content.encode("utf-8")) >= len(content.encode("utf-8"))
        )
        if native_result_is_unsafe_or_unhelpful:
            fallback_transformer = next(
                (
                    item
                    for item in self.transformers
                    if detection.content_type in getattr(item, "content_types", set())
                    and not isinstance(item, NativeTransformer)
                ),
                None,
            )
            if fallback_transformer is not None:
                result = fallback_transformer.transform(content, context)
                execution_path = "python_fallback_after_native"
        result = TransformResult(
            result.content,
            result.transformer,
            result.content_type,
            result.critical_total,
            result.critical_retained,
            {**result.metadata, "execution_path": execution_path},
        )
        if result.critical_retained < result.critical_total or len(
            result.content.encode("utf-8")
        ) >= len(content.encode("utf-8")):
            return TransformResult(
                content,
                "passthrough",
                detection.content_type,
                result.critical_total,
                result.critical_total,
                {"fallback": "unsafe_or_no_savings", "execution_path": execution_path},
            )
        return result
