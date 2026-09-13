"""Bounded project-local failure diagnostics, independent of the UI and root logger.

Raw exception messages, source lines, locals and request/tool payloads are never
written. The UI gets a redacted summary; the file keeps exception types, numeric
error codes and stack locations. Per-process files avoid cross-process rotation
races; writes within a process are serialized and leave no open file handles.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_MAX_LOG_BYTES = 1024 * 1024
_BACKUP_COUNT = 3
_MAX_RECORD_BYTES = 32 * 1024
_WRITE_LOCK = threading.Lock()
_SECRET_FIELD = re.compile(
    r'''(?ix)(["']?(?:authorization|proxy-authorization|x-api-key|api[_-]?key|
    access[_-]?token|refresh[_-]?token|token|password|passwd|secret|cookie|set-cookie)
    ["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\r\n,;}]+)'''
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TOKEN = re.compile(
    r"\b(?:sk-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|xox[baprs]-[\w-]+|"
    r"AKIA[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?)\b"
)


def safe_error_text(text: str, *, limit: int = 1000) -> str:
    """Bound and redact diagnostic text for display, not for persistence."""
    text = text[:16000]
    # An unfinished PEM (including one clipped at the input bound) is sensitive too.
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        "[REDACTED PRIVATE KEY]", text, flags=re.DOTALL,
    )
    text = _SECRET_FIELD.sub(r"\1[REDACTED]", text)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[\w.+/=-]+", "[REDACTED AUTH]", text)
    # A diagnostic rarely needs a URL; dropping it also covers userinfo, signed
    # queries and credentials embedded in paths without a fragile URL key list.
    text = _URL.sub("[REDACTED URL]", text)
    text = _TOKEN.sub("[REDACTED]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def exception_message(error: BaseException) -> str:
    """Keep an existing message compatible, but never lose an empty error's type."""
    try:
        detail = safe_error_text(str(error), limit=2000)
    except Exception:  # A broken exception __str__ must not mask the original failure.
        detail = ""
    return detail or type(error).__name__


def exception_summary(error: BaseException) -> str:
    """A nonempty, bounded TUI summary that always includes the exception type."""
    name = type(error).__name__
    detail = exception_message(error)
    return name if detail == name else f"{name}: {detail}"


@dataclass(frozen=True, slots=True)
class ErrorReport:
    summary: str
    path: Path | None
    log_error: str | None = None


def _stack_locations(error: BaseException) -> str:
    """Walk a bounded cause chain without formatting messages, source or locals."""
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    for _ in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        lines.append(f"{type(current).__name__}: Traceback (locations only)")
        frames: deque[str] = deque(maxlen=24)
        tb = current.__traceback__
        # Retain the innermost frames, with a separate traversal bound.
        for _ in range(256):
            if tb is None:
                break
            code = tb.tb_frame.f_code
            filename = safe_error_text(code.co_filename, limit=240)
            function = safe_error_text(code.co_name, limit=80)
            frames.append(f"  {filename}:{tb.tb_lineno} in {function}")
            tb = tb.tb_next
        lines.extend(frames)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return "\n".join(lines)[:8192]


def _append_record(path: Path, record: dict[str, object]) -> None:
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    if len(data) > _MAX_RECORD_BYTES:
        # Keep the JSON record valid, even for non-ASCII stack locations.
        record["stack"] = str(record.get("stack", ""))[:2000] + "\n[truncated]"
        data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size + len(data) > _MAX_LOG_BYTES:
            for index in range(_BACKUP_COUNT, 0, -1):
                source = path if index == 1 else path.with_suffix(f".log.{index - 1}")
                target = path.with_suffix(f".log.{index}")
                if source.exists():
                    source.replace(target)
        with path.open("ab") as handle:
            handle.write(data)


def record_error(
    workspace: Path | str | None,
    *,
    operation: str,
    thread_id: str = "",
    turn_id: str = "",
    error: BaseException | None = None,
    detail: str = "",
) -> ErrorReport:
    """Report a failure without letting diagnostic I/O become a second failure.

    Creates ``<workspace>/.synapse/logs/errors-<pid>.log`` only on failure.
    A returned path means this record was written successfully. A missing
    workspace or an unwritable directory is reported explicitly to the caller.
    """
    summary = exception_summary(error) if error is not None else (
        safe_error_text(detail) or "TurnFailed: runtime returned no error details"
    )
    if workspace is None:
        return ErrorReport(summary, None, "workspace unavailable")
    try:
        path = Path(workspace).expanduser().resolve() / ".synapse" / "logs"
        path /= f"errors-{os.getpid()}.log"
        # Terminal results contain only text, not an exception/traceback. Keep
        # only a conventional exception class name, never the arbitrary detail.
        match = re.match(r"^([A-Za-z_]\w*(?:Error|Exception))(?::|$)", detail)
        error_type = type(error).__name__ if error is not None else (
            match.group(1) if match else "TurnFailed"
        )
        record: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "pid": os.getpid(),
            "operation": safe_error_text(operation, limit=80),
            "thread_id": safe_error_text(thread_id, limit=128),
            "turn_id": safe_error_text(turn_id, limit=128),
            "error_type": error_type[:80],
            "stack": _stack_locations(error) if error is not None else "",
        }
        if error is not None:
            for name in ("errno", "status_code"):
                value = getattr(error, name, None)
                if type(value) is int:
                    record[name] = value
        _append_record(path, record)
        return ErrorReport(summary, path)
    except Exception as log_error:  # Diagnostics are an explicit best-effort boundary.
        # Do not log the logging failure to stderr/root: it can corrupt the TUI
        # and include sensitive path/exception content. The UI reports its type.
        return ErrorReport(summary, None, type(log_error).__name__)
