"""Durable session-scoped storage for full tool results.

Only a bounded preview is sent back to the model. The original result is appended
to a per-session JSONL journal and can be retrieved by its opaque reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REFERENCE_PREFIX = "tool-result://"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ToolResultRecord:
    """A complete persisted tool result and its stable lookup reference."""

    ref: str
    event_id: str
    thread_id: str
    checkpoint_ns: str
    tool_call_id: str
    tool_name: str
    status: str
    content: str
    size_bytes: int
    sha256: str
    created_at: str


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _safe_component(value: str, *, fallback: str) -> str:
    """Return a filesystem-safe opaque directory component."""
    text = str(value or "").strip()
    if not text:
        return fallback
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text)
    return safe[:128] or fallback


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


class ToolResultStore:
    """Append-only JSONL journal, isolated by LangGraph thread id.

    Writes are synchronized per process. Each journal record is one JSON object,
    written with a single append operation after its complete byte representation
    has been generated.
    """

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.Lock] = {}

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    def _thread_dir(self, thread_id: str) -> Path:
        return self.root / _safe_component(thread_id, fallback="unknown-thread")

    def _journal_path(self, thread_id: str) -> Path:
        return self._thread_dir(thread_id) / "tool-results.jsonl"

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        key = str(path)
        with cls._locks_guard:
            lock = cls._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._locks[key] = lock
            return lock

    @staticmethod
    def make_ref(thread_id: str, event_id: str) -> str:
        return f"{_REFERENCE_PREFIX}{thread_id}/{event_id}"

    @staticmethod
    def parse_ref(ref: str) -> tuple[str, str] | None:
        if not isinstance(ref, str) or not ref.startswith(_REFERENCE_PREFIX):
            return None
        body = ref[len(_REFERENCE_PREFIX) :]
        thread_id, sep, event_id = body.partition("/")
        if not sep or not thread_id or not event_id or "/" in event_id:
            return None
        return thread_id, event_id

    def append(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str = "",
        tool_call_id: str,
        tool_name: str,
        status: str,
        content: Any,
    ) -> ToolResultRecord:
        """Persist one original tool result before its model-visible replacement."""
        thread = str(thread_id or "unknown-thread")
        text = content_to_text(content)
        raw = text.encode("utf-8")
        event_id = f"tr_{uuid.uuid4().hex}"
        record = ToolResultRecord(
            ref=self.make_ref(thread, event_id),
            event_id=event_id,
            thread_id=thread,
            checkpoint_ns=str(checkpoint_ns or ""),
            tool_call_id=str(tool_call_id or ""),
            tool_name=str(tool_name or "tool"),
            status="error" if str(status).casefold() == "error" else "success",
            content=text,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            created_at=_utcnow(),
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": record.event_id,
            "thread_id": record.thread_id,
            "checkpoint_ns": record.checkpoint_ns,
            "tool_call_id": record.tool_call_id,
            "tool_name": record.tool_name,
            "status": record.status,
            "created_at": record.created_at,
            "size_bytes": record.size_bytes,
            "sha256": record.sha256,
            "content": record.content,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        line = encoded.encode("utf-8")
        path = self._journal_path(thread)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(path):
            with path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def get(self, ref: str, *, expected_thread_id: str | None = None) -> ToolResultRecord | None:
        """Load one record by opaque reference without accepting arbitrary paths."""
        parsed = self.parse_ref(ref)
        if parsed is None:
            return None
        thread_id, event_id = parsed
        if expected_thread_id and thread_id != expected_thread_id:
            return None
        path = self._journal_path(thread_id)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("event_id") != event_id:
                        continue
                    content = content_to_text(data.get("content", ""))
                    raw = content.encode("utf-8")
                    if data.get("sha256") != hashlib.sha256(raw).hexdigest():
                        return None
                    return ToolResultRecord(
                        ref=self.make_ref(thread_id, event_id),
                        event_id=event_id,
                        thread_id=thread_id,
                        checkpoint_ns=str(data.get("checkpoint_ns") or ""),
                        tool_call_id=str(data.get("tool_call_id") or ""),
                        tool_name=str(data.get("tool_name") or "tool"),
                        status=str(data.get("status") or "success"),
                        content=content,
                        size_bytes=len(raw),
                        sha256=str(data.get("sha256") or ""),
                        created_at=str(data.get("created_at") or ""),
                    )
        except OSError:
            return None
        return None


def build_model_preview(
    record: ToolResultRecord,
    *,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Return a bounded, model-readable reference preserving diagnostic context."""
    text = record.content
    head = max(0, head_chars)
    tail = max(0, tail_chars)
    if len(text) <= head + tail:
        excerpt = text
    elif tail:
        omitted = len(text) - head - tail
        excerpt = f"{text[:head]}\n\n...[{omitted} chars omitted]...\n\n{text[-tail:]}"
    else:
        excerpt = f"{text[:head]}\n\n...[{len(text) - head} chars omitted]..."
    return (
        f"[tool result archived]\n"
        f"tool: {record.tool_name}\n"
        f"status: {record.status}\n"
        f"bytes: {record.size_bytes}\n"
        f"ref: {record.ref}\n"
        f"sha256: {record.sha256[:16]}\n"
        f"preview:\n{excerpt}\n\n"
        f"Use read_tool_result(ref=..., offset=..., limit=...) for more detail."
    )
