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
_NUMBERED_SOURCE_LINE = re.compile(r"^(?P<indent>\s*)\d+(?:\.\d+)?\t(?P<body>.*)$")
_CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".php",
        ".py",
        ".pyi",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
    }
)

# Process-local UI hook. Persistence remains in ToolOutputRepository; the hook
# only lets an active UI refresh its in-memory metrics promptly.
_metrics_notifier: Any | None = None
_metrics_notifier_lock = threading.RLock()


def set_metrics_notifier(notifier: Any | None) -> None:
    """Install a best-effort callback invoked after metrics-changing writes."""
    global _metrics_notifier
    with _metrics_notifier_lock:
        _metrics_notifier = notifier


def clear_metrics_notifier() -> None:
    """Remove the active process-local metrics callback."""
    set_metrics_notifier(None)


def notify_metrics_changed(thread_id: str) -> None:
    """Notify the active UI without allowing observer failures to affect tools."""
    with _metrics_notifier_lock:
        notifier = _metrics_notifier
    if callable(notifier):
        try:
            notifier(thread_id)
        except Exception:  # noqa: BLE001
            pass


class ContentType(StrEnum):
    SEARCH = "search"
    LOG = "log"
    DIFF = "diff"
    GIT_SUMMARY = "git-summary"
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
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    file_suffix: str = ""


@dataclass(frozen=True)
class CompressionStageEvent:
    """One observable stage in a tool-output compression decision."""

    phase: str
    algorithm: str
    applied: bool
    reason_code: str
    input_bytes: int = 0
    output_bytes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformResult:
    content: str
    transformer: str
    content_type: ContentType
    critical_total: int
    critical_retained: int
    metadata: dict[str, Any] = field(default_factory=dict)
    stages: tuple[CompressionStageEvent, ...] = ()


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
class ModelRequestCompressionEvent:
    """One model call's compression and provider-safety accounting."""

    request_id: str
    provider: str
    api_style: str
    auth_mode: str
    model: str
    input_tokens_before: int
    input_tokens_after: int
    provider_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int = 0
    output_tokens: int = 0
    tool_output_saved_tokens: int = 0
    prompt_saved_tokens: int = 0
    summarization_saved_tokens: int = 0
    total_saved_tokens: int = 0
    candidate_blocks: int = 0
    transformed_blocks: int = 0
    protected_tokens_by_reason: dict[str, int] = field(default_factory=dict)
    content_breakdown: dict[str, int] = field(default_factory=dict)
    opportunity_tokens_by_reason: dict[str, int] = field(default_factory=dict)
    turn_id: str = ""
    turn_index: int = 0
    model_call_index: int = 0
    live_zone_plan: list[dict[str, Any]] = field(default_factory=list)
    live_zone_tokens: dict[str, int] = field(default_factory=dict)
    wire_fingerprints: dict[str, Any] = field(default_factory=dict)
    cache_diagnostics: dict[str, Any] = field(default_factory=dict)
    tool_schema_profiles: list[dict[str, Any]] = field(default_factory=list)
    token_count_method: str = "langchain_approximate"
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        whole_denominator = self.input_tokens_after + self.total_saved_tokens
        new_input = self.uncached_input_tokens + self.cache_write_tokens
        data["whole_request_savings_ratio"] = (
            round(self.total_saved_tokens / whole_denominator, 4) if whole_denominator else 0.0
        )
        data["new_input_savings_ratio"] = (
            round(self.total_saved_tokens / (new_input + self.total_saved_tokens), 4)
            if new_input > 0
            else 0.0
        )
        return data


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
    estimated_original_tokens: int = 0
    estimated_visible_tokens: int = 0
    decision: str = "transformed"
    reason_code: str = "compressed"
    reason_detail: str = ""
    eligible: bool = True
    source_kind: str = "tool-output"
    detection_confidence: float = 0.0
    threshold_bytes: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    status: str = "success"
    checkpoint_ns: str = ""
    message_id: str = ""
    algorithm_output_bytes: int = 0
    algorithm_output_tokens: int = 0
    token_count_method: str = "langchain_approximate"
    content_sha256: str = ""
    stages: tuple[CompressionStageEvent, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["saved_bytes"] = max(0, self.original_bytes - self.visible_bytes)
        data["estimated_saved_tokens"] = max(
            0, self.estimated_original_tokens - self.estimated_visible_tokens
        )
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
                CREATE TABLE IF NOT EXISTS tool_output_model_reuse_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    estimated_avoided_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_output_model_reuse_thread
                    ON tool_output_model_reuse_events(thread_id);
                CREATE TABLE IF NOT EXISTS model_request_compression_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    thread_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_request_compression_thread
                    ON model_request_compression_events(thread_id, id);
                CREATE TABLE IF NOT EXISTS interaction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_interaction_events_thread
                    ON interaction_events(thread_id, id);
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
        notify_metrics_changed(thread_id)

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
        notify_metrics_changed(thread_id)

    def record_model_reuse(self, *, thread_id: str, estimated_avoided_tokens: int) -> None:
        """Record estimated token savings when transformed outputs re-enter a model call."""
        avoided = max(0, int(estimated_avoided_tokens or 0))
        if not thread_id or avoided <= 0:
            return
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO tool_output_model_reuse_events("
                "thread_id, estimated_avoided_tokens, created_at) VALUES (?, ?, ?)",
                (thread_id, avoided, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            )
        notify_metrics_changed(thread_id)

    def record_interaction(self, *, thread_id: str, event: dict[str, Any]) -> None:
        """Persist one model/tool interaction independently of compression eligibility."""
        if not thread_id:
            return
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO interaction_events(thread_id, event_json, created_at) "
                "VALUES (?, ?, ?)",
                (thread_id, json.dumps(event, ensure_ascii=False), created),
            )
        notify_metrics_changed(thread_id)

    def interaction_events(
        self, *, thread_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded = max(1, min(5000, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                f"FROM interaction_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def latest_request_position(self, *, thread_id: str) -> tuple[int, int]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT event_json FROM model_request_compression_events "
                "WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if row is None:
            return 0, 0
        event = json.loads(row["event_json"])
        return int(event.get("turn_index", 0) or 0), int(
            event.get("model_call_index", 0) or 0
        )

    def record_model_request(
        self, *, thread_id: str, event: ModelRequestCompressionEvent
    ) -> None:
        """Persist one completed model-call compression accounting event."""
        if not thread_id or not event.request_id:
            return
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO model_request_compression_events("
                "request_id, thread_id, event_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    event.request_id,
                    thread_id,
                    json.dumps(event.as_dict(), ensure_ascii=False),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
        notify_metrics_changed(thread_id)

    def model_request_events(
        self, *, thread_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return recent model request compression accounting events."""
        where, params = (" WHERE thread_id = ?", (thread_id,)) if thread_id else ("", ())
        bounded = max(1, min(500, int(limit)))
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                f"FROM model_request_compression_events{where} ORDER BY id DESC LIMIT ?",
                (*params, bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in rows
        ]

    def estimated_active_saved_tokens(self, *, thread_id: str) -> int:
        """Sum estimated savings of transformed tool outputs currently in graph state."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT event_json FROM tool_output_events "
                "WHERE thread_id = ? AND ref IS NOT NULL",
                (thread_id,),
            ).fetchall()
        return sum(
            max(
                0,
                int(json.loads(row["event_json"]).get("estimated_saved_tokens", 0) or 0),
            )
            for row in rows
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

    def export_diagnostics(self, *, thread_id: str) -> dict[str, Any]:
        """Return complete compression diagnostics for one thread without output blobs."""
        with self._lock, self._connection() as conn:
            tool_rows = conn.execute(
                "SELECT id, thread_id, ref, event_json, created_at "
                "FROM tool_output_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            request_rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                "FROM model_request_compression_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            retrieval_rows = conn.execute(
                "SELECT id, thread_id, ref, mode, returned_bytes, duration_ms, created_at "
                "FROM tool_output_retrieval_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            reuse_rows = conn.execute(
                "SELECT id, thread_id, estimated_avoided_tokens, created_at "
                "FROM tool_output_model_reuse_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
            interaction_rows = conn.execute(
                "SELECT id, thread_id, event_json, created_at "
                "FROM interaction_events WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()

        tool_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "ref": row["ref"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in tool_rows
        ]
        request_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in request_rows
        ]
        retrieval_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "ref": row["ref"],
                "mode": row["mode"],
                "returned_bytes": int(row["returned_bytes"]),
                "duration_ms": float(row["duration_ms"]),
                "created_at": row["created_at"],
            }
            for row in retrieval_rows
        ]
        model_reuse_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "estimated_avoided_tokens": int(row["estimated_avoided_tokens"]),
                "created_at": row["created_at"],
            }
            for row in reuse_rows
        ]
        interaction_events = [
            {
                "id": int(row["id"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                **json.loads(row["event_json"]),
            }
            for row in interaction_rows
        ]
        return {
            "summary": self.stats(thread_id=thread_id),
            "model_request_events": request_events,
            "interaction_events": interaction_events,
            "tool_output_events": tool_events,
            "retrieval_events": retrieval_events,
            "model_reuse_events": model_reuse_events,
        }

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
            reuse_rows = conn.execute(
                f"SELECT estimated_avoided_tokens FROM tool_output_model_reuse_events{where}",
                params,
            ).fetchall()
            request_rows = conn.execute(
                f"SELECT event_json FROM model_request_compression_events{where}", params
            ).fetchall()
            interaction_rows = conn.execute(
                f"SELECT event_json FROM interaction_events{where}", params
            ).fetchall()
        events = [json.loads(row["event_json"]) for row in rows]
        request_events = [json.loads(row["event_json"]) for row in request_rows]
        interaction_events = [json.loads(row["event_json"]) for row in interaction_rows]
        turn_ids = {str(item.get("turn_id") or "") for item in request_events}
        turn_ids.discard("")
        tool_calls = [item for item in interaction_events if item.get("event_type") == "tool_call"]
        live_zone_tokens: dict[str, int] = {}
        schema_tokens_by_tool: dict[str, int] = {}
        cache_bust_suspected = 0
        for request_event in request_events:
            for key, value in dict(request_event.get("live_zone_tokens") or {}).items():
                live_zone_tokens[str(key)] = live_zone_tokens.get(str(key), 0) + int(value or 0)
            if (request_event.get("cache_diagnostics") or {}).get("cache_bust_suspected"):
                cache_bust_suspected += 1
            for profile in request_event.get("tool_schema_profiles") or []:
                name = str(profile.get("tool_name") or "unknown")
                schema_tokens_by_tool[name] = schema_tokens_by_tool.get(name, 0) + int(
                    profile.get("estimated_tokens", 0) or 0
                )
        retrieval_bytes = sum(int(row["returned_bytes"]) for row in retrieval_rows)
        estimated_reused_tokens = sum(
            int(row["estimated_avoided_tokens"]) for row in reuse_rows
        )
        original = sum(int(item["original_bytes"]) for item in events)
        visible = sum(int(item["visible_bytes"]) for item in events)
        transformed = sum(item["outcome"] == "transformed" for item in events)
        estimated_original_tokens = sum(
            int(item.get("estimated_original_tokens", 0) or 0) for item in events
        )
        estimated_visible_tokens = sum(
            int(item.get("estimated_visible_tokens", 0) or 0) for item in events
        )
        estimated_saved_tokens = max(
            0, estimated_original_tokens - estimated_visible_tokens
        )
        critical_total = sum(int(item["critical_total"]) for item in events)
        critical_retained = sum(int(item["critical_retained"]) for item in events)
        execution_paths: dict[str, int] = {}
        decisions: dict[str, int] = {}
        reasons: dict[str, int] = {}
        tokens_by_reason: dict[str, int] = {}
        bytes_by_reason: dict[str, int] = {}
        request_input_before = sum(
            int(item.get("input_tokens_before", 0) or 0) for item in request_events
        )
        request_input_after = sum(
            int(item.get("input_tokens_after", 0) or 0) for item in request_events
        )
        request_saved_tokens = sum(
            int(item.get("total_saved_tokens", 0) or 0) for item in request_events
        )
        provider_input_tokens = sum(
            int(item.get("provider_input_tokens", 0) or 0) for item in request_events
        )
        cache_read_tokens = sum(
            int(item.get("cache_read_tokens", 0) or 0) for item in request_events
        )
        cache_write_tokens = sum(
            int(item.get("cache_write_tokens", 0) or 0) for item in request_events
        )
        uncached_input_tokens = sum(
            int(item.get("uncached_input_tokens", 0) or 0) for item in request_events
        )
        request_output_tokens = sum(
            int(item.get("output_tokens", 0) or 0) for item in request_events
        )
        content_breakdown: dict[str, int] = {}
        opportunities: dict[str, int] = {}
        protected_breakdown: dict[str, int] = {}
        for request_event in request_events:
            for key, value in dict(request_event.get("content_breakdown") or {}).items():
                content_breakdown[str(key)] = content_breakdown.get(str(key), 0) + int(
                    value or 0
                )
            for key, value in dict(
                request_event.get("opportunity_tokens_by_reason") or {}
            ).items():
                opportunities[str(key)] = opportunities.get(str(key), 0) + int(value or 0)
            for key, value in dict(
                request_event.get("protected_tokens_by_reason") or {}
            ).items():
                protected_breakdown[str(key)] = protected_breakdown.get(str(key), 0) + int(
                    value or 0
                )
        for item in events:
            path = str(item.get("execution_path", "legacy_unknown"))
            execution_paths[path] = execution_paths.get(path, 0) + 1
            decision = str(
                item.get("decision")
                or ("transformed" if item.get("outcome") == "transformed" else "fallback")
            )
            reason = str(
                item.get("reason_code")
                or ("compressed" if decision == "transformed" else "legacy_passthrough")
            )
            decisions[decision] = decisions.get(decision, 0) + 1
            reasons[reason] = reasons.get(reason, 0) + 1
            if decision != "transformed":
                tokens_by_reason[reason] = tokens_by_reason.get(reason, 0) + int(
                    item.get("estimated_original_tokens", 0) or 0
                )
                bytes_by_reason[reason] = bytes_by_reason.get(reason, 0) + int(
                    item.get("original_bytes", 0) or 0
                )
        return {
            "outputs_considered": len(events),
            "transformed": transformed,
            "original_bytes": original,
            "visible_bytes": visible,
            "saved_bytes": max(0, original - visible),
            "estimated_original_tokens": estimated_original_tokens,
            "estimated_visible_tokens": estimated_visible_tokens,
            "estimated_saved_tokens": estimated_saved_tokens,
            "estimated_reused_tokens": estimated_reused_tokens,
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
            "decisions": decisions,
            "reasons": reasons,
            "tokens_by_reason": tokens_by_reason,
            "bytes_by_reason": bytes_by_reason,
            "skipped": decisions.get("skipped", 0),
            "fallback": decisions.get("fallback", 0),
            "model_requests": len(request_events),
            "turns": len(turn_ids),
            "tool_calls": len(tool_calls),
            "compression_managed_tool_calls": sum(
                bool(item.get("compression_managed")) for item in tool_calls
            ),
            "live_zone_tokens": live_zone_tokens,
            "cache_bust_suspected_requests": cache_bust_suspected,
            "schema_tokens_by_tool": schema_tokens_by_tool,
            "top_schema_tools": sorted(
                schema_tokens_by_tool.items(), key=lambda item: item[1], reverse=True
            )[:10],
            "request_input_tokens_before": request_input_before,
            "request_input_tokens_after": request_input_after,
            "request_saved_tokens": request_saved_tokens,
            "provider_input_tokens": provider_input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "request_output_tokens": request_output_tokens,
            "whole_request_savings_ratio": (
                round(request_saved_tokens / request_input_before, 4)
                if request_input_before
                else 0.0
            ),
            "new_input_savings_ratio": (
                round(
                    request_saved_tokens
                    / (uncached_input_tokens + cache_write_tokens + request_saved_tokens),
                    4,
                )
                if uncached_input_tokens + cache_write_tokens > 0
                else 0.0
            ),
            "content_breakdown": content_breakdown,
            "opportunity_tokens_by_reason": opportunities,
            "protected_tokens_by_reason": protected_breakdown,
            "top_opportunities": sorted(
                opportunities.items(), key=lambda item: item[1], reverse=True
            )[:10],
            "top_protected_sources": sorted(
                protected_breakdown.items(), key=lambda item: item[1], reverse=True
            )[:10],
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


class GitSummaryTransformer:
    """Keep Git operation status and a bounded, representative file-stat view."""

    name = "git-summary-v1"
    content_types = frozenset({ContentType.GIT_SUMMARY})

    def __init__(
        self, *, head_lines: int = 8, tail_lines: int = 8, max_file_entries: int = 30
    ) -> None:
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.max_file_entries = max_file_entries

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        file_entries = [
            index
            for index, line in enumerate(lines)
            if re.match(
                r"^(?:\s+.+\s+\|\s+\d+\s+[+\-]+$|(?:create|delete) mode \d+ |rename )", line
            )
        ]
        if len(file_entries) <= self.max_file_entries:
            return TransformResult(content, self.name, ContentType.GIT_SUMMARY, 0, 0)
        selected = set(range(min(self.head_lines, len(lines))))
        selected.update(range(max(0, len(lines) - self.tail_lines), len(lines)))
        selected.update(
            index
            for index, line in enumerate(lines)
            if re.match(
                r"^\s*\d+ files? changed|^\s*\d+ (?:insertions?\(\+\)|deletions?\(-\))",
                line,
            )
        )
        selected.update(file_entries[: self.max_file_entries // 2])
        selected.update(file_entries[-(self.max_file_entries - self.max_file_entries // 2) :])
        ordered = sorted(selected)
        omitted_entries = len(file_entries) - len(set(file_entries) & selected)
        omitted_lines = len(lines) - len(ordered)
        body = "\n".join(
            [
                "[git summary compressed: "
                f"{omitted_entries} file entries and {omitted_lines} lines omitted]",
                *(lines[index] for index in ordered),
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.GIT_SUMMARY,
            metadata={"file_entries": len(file_entries), "omitted_file_entries": omitted_entries},
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

    def compress_native_diff(content: str, context: TransformContext) -> dict[str, Any]:
        try:
            result = native.compress_diff(content, context=context.query)
        except TypeError as exc:
            if "unexpected keyword argument 'context'" not in str(exc):
                raise
            result = native.compress_diff(content)
            result["context_supported"] = False
        else:
            result["context_supported"] = True
        return result

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
            native_transform=compress_native_diff,
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
            GitSummaryTransformer(),
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
        original_bytes = len(content.encode("utf-8"))
        detection_started = time.perf_counter()
        raw_detection = detect_content_type(content)
        detection = raw_detection
        detection_view = content
        numbered_lines = 0
        non_empty_lines = 0
        normalized_code_markers = 0
        suffix_is_code = context.file_suffix.casefold() in _CODE_SUFFIXES
        numbered_source_hint = bool(
            suffix_is_code
            and numbered_lines
            and numbered_lines >= max(1, non_empty_lines // 2)
        )
        if context.tool_name == "read_file":
            normalized, numbered_lines, non_empty_lines = _strip_numbered_source_lines(content)
            normalized_code_markers = _code_marker_count(normalized)
            numbered_source_hint = bool(
                suffix_is_code
                and numbered_lines
                and numbered_lines >= max(1, non_empty_lines // 2)
            )
            if numbered_lines and normalized_code_markers >= 3:
                detection_view = normalized
                detection = detect_content_type(normalized)
            if numbered_source_hint:
                detection_view = normalized
                detection = Detection(ContentType.CODE, max(0.8, detection.confidence))
        detection_metadata = {
            "content_type": detection.content_type.value,
            "confidence": detection.confidence,
            "raw_content_type": raw_detection.content_type.value,
            "raw_confidence": raw_detection.confidence,
            "tool_name": context.tool_name,
            "file_suffix": context.file_suffix,
            "numbered_lines": numbered_lines,
            "non_empty_lines": non_empty_lines,
            "normalized_code_markers": normalized_code_markers,
            "numbered_source_hint": numbered_source_hint,
            "classification_conflict": raw_detection.content_type is not detection.content_type,
        }
        if detection.content_type is ContentType.DIFF:
            detection_metadata.update(_diff_bloat_metadata(content))
        stages: list[CompressionStageEvent] = [
            CompressionStageEvent(
                phase="detect",
                algorithm="tool-aware-content-detector-v2",
                applied=True,
                reason_code="classified",
                input_bytes=original_bytes,
                output_bytes=len(detection_view.encode("utf-8")),
                duration_ms=(time.perf_counter() - detection_started) * 1000,
                metadata=detection_metadata,
            )
        ]
        if (
            context.tool_name == "read_file"
            and context.status == "success"
            and suffix_is_code
            and detection.content_type is ContentType.CODE
        ):
            stages.append(
                CompressionStageEvent(
                    phase="eligibility",
                    algorithm="fresh-read-source-policy-v1",
                    applied=False,
                    reason_code="fresh_read_source_protected",
                    input_bytes=original_bytes,
                    output_bytes=original_bytes,
                    metadata={
                        "file_path": context.file_path,
                        "file_suffix": context.file_suffix,
                        "normalized_code_markers": normalized_code_markers,
                    },
                )
            )
            return TransformResult(
                content,
                "fresh-read-source-policy-v1",
                ContentType.CODE,
                0,
                0,
                {
                    "fallback": "fresh_read_source_protected",
                    "detection_confidence": detection.confidence,
                    **detection_metadata,
                },
                tuple(stages),
            )
        if detection.content_type.value in self.disabled_types:
            stages.append(
                CompressionStageEvent(
                    phase="eligibility",
                    algorithm="disabled-types-policy",
                    applied=False,
                    reason_code="disabled_content_type",
                    input_bytes=original_bytes,
                    output_bytes=original_bytes,
                )
            )
            return TransformResult(
                content,
                "disabled",
                detection.content_type,
                0,
                0,
                {"fallback": "disabled", "detection_confidence": detection.confidence},
                tuple(stages),
            )
        transformer = next(
            (
                item
                for item in self.transformers
                if detection.content_type in getattr(item, "content_types", set())
            ),
            GenericTransformer(),
        )
        transform_started = time.perf_counter()
        result = transformer.transform(content, context)
        result_bytes = len(result.content.encode("utf-8"))
        native = isinstance(transformer, NativeTransformer)
        native_reason = str(result.metadata.get("fallback") or "compressed")
        stages.append(
            CompressionStageEvent(
                phase="native-transform" if native else "transform",
                algorithm=str(getattr(transformer, "name", result.transformer)),
                applied=result.content != content,
                reason_code=native_reason,
                input_bytes=original_bytes,
                output_bytes=result_bytes,
                duration_ms=(time.perf_counter() - transform_started) * 1000,
                metadata=dict(result.metadata),
            )
        )
        execution_path = "native" if native else "python_only"
        native_result_is_unsafe_or_unhelpful = native and (
            result.metadata.get("fallback") == "native_error"
            or result.critical_retained < result.critical_total
            or result_bytes >= original_bytes
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
                fallback_started = time.perf_counter()
                result = fallback_transformer.transform(content, context)
                result_bytes = len(result.content.encode("utf-8"))
                stages.append(
                    CompressionStageEvent(
                        phase="python-fallback",
                        algorithm=str(getattr(fallback_transformer, "name", result.transformer)),
                        applied=result.content != content,
                        reason_code="python_fallback_used",
                        input_bytes=original_bytes,
                        output_bytes=result_bytes,
                        duration_ms=(time.perf_counter() - fallback_started) * 1000,
                        metadata=dict(result.metadata),
                    )
                )
                execution_path = "python_fallback_after_native"
        result = TransformResult(
            result.content,
            result.transformer,
            result.content_type,
            result.critical_total,
            result.critical_retained,
            {
                **detection_metadata,
                **result.metadata,
                "execution_path": execution_path,
                "detection_confidence": detection.confidence,
            },
            tuple(stages),
        )
        if result.critical_retained < result.critical_total:
            stages.append(
                CompressionStageEvent(
                    phase="critical-guard",
                    algorithm="critical-retention-v1",
                    applied=False,
                    reason_code="critical_content_lost",
                    input_bytes=original_bytes,
                    output_bytes=result_bytes,
                    metadata={
                        "critical_total": result.critical_total,
                        "critical_retained": result.critical_retained,
                    },
                )
            )
            return TransformResult(
                content,
                "passthrough",
                detection.content_type,
                result.critical_total,
                result.critical_total,
                {
                    "fallback": "critical_content_lost",
                    "execution_path": execution_path,
                    "detection_confidence": detection.confidence,
                },
                tuple(stages),
            )
        stages.append(
            CompressionStageEvent(
                phase="critical-guard",
                algorithm="critical-retention-v1",
                applied=True,
                reason_code="accepted",
                input_bytes=original_bytes,
                output_bytes=result_bytes,
                metadata={
                    "critical_total": result.critical_total,
                    "critical_retained": result.critical_retained,
                },
            )
        )
        if result_bytes >= original_bytes:
            stages.append(
                CompressionStageEvent(
                    phase="byte-guard",
                    algorithm="non-increase-v1",
                    applied=False,
                    reason_code="no_byte_savings",
                    input_bytes=original_bytes,
                    output_bytes=result_bytes,
                )
            )
            return TransformResult(
                content,
                "passthrough",
                detection.content_type,
                result.critical_total,
                result.critical_total,
                {
                    "fallback": "no_byte_savings",
                    "execution_path": execution_path,
                    "detection_confidence": detection.confidence,
                },
                tuple(stages),
            )
        stages.append(
            CompressionStageEvent(
                phase="byte-guard",
                algorithm="non-increase-v1",
                applied=True,
                reason_code="accepted",
                input_bytes=original_bytes,
                output_bytes=result_bytes,
            )
        )
        return TransformResult(
            result.content,
            result.transformer,
            result.content_type,
            result.critical_total,
            result.critical_retained,
            result.metadata,
            tuple(stages),
        )