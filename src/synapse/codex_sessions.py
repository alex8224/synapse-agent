"""Read-only discovery of local Codex sessions.

This module intentionally discovers metadata only.  It never resumes Codex,
parses a full conversation, creates a Synapse session, or writes a LangGraph
checkpoint.  Importing is a later phase with separate history and checkpoint
compatibility requirements.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import zstandard

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_DB_ROWS = 5_000
MAX_SCAN_FILES = 500
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_HEAD_BYTES = 2 * 1024 * 1024
MAX_HEAD_RECORDS = 50
MAX_HEADER_LINE_BYTES = 64 * 1024
MAX_TITLE_CHARS = 120

_STATE_DB_RE = re.compile(r"state_(\d+)\.sqlite\Z")
_ROLLOUT_RE = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F-]{36})\.jsonl(?:\.zst)?\Z"
)
_ALLOWED_SOURCES = frozenset(
    {"cli", "vscode", '{"custom":"atlas"}', '{"custom":"chatgpt"}'}
)
_INTERNAL_TITLE_MARKERS = (
    "<environment_context>",
    "<user_instructions>",
    "<developer_instructions>",
    "# agents.md instructions",
)


@dataclass(frozen=True)
class CodexSession:
    """A validated, metadata-only reference to one Codex rollout."""

    native_id: str
    title: str
    cwd: Path
    updated_at: datetime
    source: str
    rollout_path: Path
    fingerprint: str
    discovery: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, str | list[str]]:
        return {
            "native_id": self.native_id,
            "title": self.title,
            "cwd": str(self.cwd),
            "updated_at": self.updated_at.isoformat(),
            "source": self.source,
            "rollout_path": str(self.rollout_path),
            "fingerprint": self.fingerprint,
            "discovery": self.discovery,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CodexScanResult:
    """Scanner result with non-fatal diagnostic warnings."""

    codex_home: Path
    sessions: tuple[CodexSession, ...]
    warnings: tuple[str, ...]
    discovery: str


class CodexSessionScanner:
    """Discover sessions matching one workspace without mutating Codex state."""

    def __init__(self, codex_home: Path | str | None = None) -> None:
        self._codex_home = _resolve_codex_home(codex_home)

    @property
    def codex_home(self) -> Path:
        return self._codex_home

    def scan(
        self,
        workspace: Path | str | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
        include_rollout_fallback: bool = False,
    ) -> CodexScanResult:
        """Return recent readable sessions, optionally scoped to one workspace.

        The highest numbered ``state_N.sqlite`` is preferred. A missing or
        incompatible state DB falls back to bounded ``sessions/**/*.jsonl``
        header inspection. Compressed rollouts use a bounded zstd header reader.
        """
        workspace_input = Path(workspace).expanduser() if workspace is not None else None
        workspace_path = _canonical_path(workspace_input) if workspace_input is not None else None
        workspace_spellings = (
            tuple(dict.fromkeys((str(workspace_path), str(workspace_input))))
            if workspace_path is not None and workspace_input is not None
            else ()
        )
        limit = max(1, min(int(limit), MAX_LIMIT))
        warnings: list[str] = []

        if not self._codex_home.is_dir():
            return CodexScanResult(
                codex_home=self._codex_home,
                sessions=(),
                warnings=(f"Codex home not found: {self._codex_home}",),
                discovery="none",
            )

        sessions_root = self._codex_home / "sessions"
        if sessions_root.is_symlink():
            return CodexScanResult(
                codex_home=self._codex_home,
                sessions=(),
                warnings=("Codex sessions root is a symlink and was ignored",),
                discovery="none",
            )

        state_db = _latest_state_db(self._codex_home)
        if state_db is not None:
            try:
                sessions, state_warnings = _scan_state_db(
                    state_db,
                    self._codex_home,
                    workspace_path,
                    workspace_spellings=workspace_spellings,
                    limit=limit,
                )
                warnings.extend(state_warnings)
            except _UnsupportedStateDb as exc:
                warnings.append(f"state DB ignored: {exc}")
            except sqlite3.Error as exc:
                warnings.append(f"state DB read failed: {type(exc).__name__}")
            else:
                if include_rollout_fallback:
                    fallback_sessions, fallback_warnings = _scan_rollout_headers(
                        self._codex_home,
                        workspace_path,
                        limit=limit,
                    )
                    warnings.extend(fallback_warnings)
                    known_ids = {session.native_id for session in sessions}
                    sessions.extend(
                        session
                        for session in fallback_sessions
                        if session.native_id not in known_ids
                    )
                    sessions.sort(key=lambda session: session.updated_at, reverse=True)
                    sessions = sessions[:limit]
                return CodexScanResult(
                    codex_home=self._codex_home,
                    sessions=tuple(sessions),
                    warnings=tuple(warnings),
                    discovery="state_db",
                )

        sessions, fallback_warnings = _scan_rollout_headers(
            self._codex_home, workspace_path, limit=limit
        )
        warnings.extend(fallback_warnings)
        return CodexScanResult(
            codex_home=self._codex_home,
            sessions=tuple(sessions),
            warnings=tuple(warnings),
            discovery="rollout_headers",
        )

    def inspect(
        self,
        native_id: str,
        *,
        workspace: Path | str | None = None,
        limit: int = MAX_LIMIT,
        include_rollout_fallback: bool = False,
    ) -> CodexSession | None:
        """Look up one native session id, optionally scoped to one workspace."""
        sessions = self.scan(
            workspace,
            limit=limit,
            include_rollout_fallback=include_rollout_fallback,
        ).sessions
        return next((session for session in sessions if session.native_id == native_id), None)


class _UnsupportedStateDb(ValueError):
    pass


def _resolve_codex_home(value: Path | str | None) -> Path:
    raw = value or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    return _canonical_path(Path(raw).expanduser())


def _canonical_path(path: Path) -> Path:
    raw = str(path)
    if os.name == "nt":
        if raw.startswith("\\\\?\\UNC\\"):
            raw = "\\\\" + raw[8:]
        elif raw.startswith("\\\\?\\"):
            raw = raw[4:]
    normalized = Path(raw)
    try:
        return normalized.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(normalized))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(_canonical_path(left))) == os.path.normcase(
        str(_canonical_path(right))
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((str(_canonical_path(path)), str(_canonical_path(root))))
    except ValueError:
        return False
    return _same_path(Path(common), root)


def _latest_state_db(codex_home: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    try:
        children = list(codex_home.iterdir())
    except OSError:
        return None
    for path in children:
        match = _STATE_DB_RE.fullmatch(path.name)
        if not match:
            continue
        try:
            if path.is_file() and not path.is_symlink():
                candidates.append((int(match.group(1)), path))
        except OSError:
            continue
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path).replace(os.sep, '/'))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _scan_state_db(
    state_db: Path,
    codex_home: Path,
    workspace: Path | None,
    *,
    workspace_spellings: tuple[str, ...],
    limit: int,
) -> tuple[list[CodexSession], list[str]]:
    with _readonly_connection(state_db) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(threads)").fetchall()
        }
        required = {"id", "rollout_path", "source", "cwd", "archived"}
        if not required.issubset(columns):
            raise _UnsupportedStateDb("threads table has an unsupported schema")
        updated_column = "updated_at_ms" if "updated_at_ms" in columns else "updated_at"
        if updated_column not in columns:
            raise _UnsupportedStateDb("threads table has no update timestamp")

        title_column = "title" if "title" in columns else "''"
        first_user_column = "first_user_message" if "first_user_message" in columns else "''"
        workspace_filter = ""
        query_params: tuple[object, ...] = ()
        if workspace_spellings:
            placeholders = ", ".join("?" for _ in workspace_spellings)
            workspace_filter = f"\n               AND cwd IN ({placeholders})"
            query_params = workspace_spellings
        query = f"""
            SELECT id, rollout_path, {updated_column}, source, cwd,
                   {title_column}, {first_user_column}
              FROM threads
             WHERE typeof(id) = 'text'
               AND typeof(rollout_path) = 'text'
               AND typeof({updated_column}) = 'integer'
               AND typeof(source) = 'text'
               AND typeof(cwd) = 'text'
               AND typeof(archived) = 'integer'
               AND archived = 0{workspace_filter}
             ORDER BY {updated_column} DESC, id ASC
             LIMIT ?
        """
        rows = connection.execute(query, (*query_params, MAX_DB_ROWS)).fetchall()

    sessions: list[CodexSession] = []
    warnings: list[str] = []
    if len(rows) == MAX_DB_ROWS:
        warnings.append("state DB scan truncated at row limit")
    for row in rows:
        if len(sessions) >= limit:
            break
        native_id, rollout_raw, updated_raw, source, cwd_raw, title, first_user = row
        if not _valid_native_id(native_id) or not _allowed_source(source):
            continue
        if not _text_within(cwd_raw, 16 * 1024) or not _text_within(rollout_raw, 16 * 1024):
            continue
        raw_cwd = Path(cwd_raw)
        if not raw_cwd.is_absolute():
            warnings.append(f"skipped nonabsolute workspace path for {native_id}")
            continue
        cwd = _canonical_path(raw_cwd)
        if workspace is not None and not _same_path(cwd, workspace):
            continue
        rollout_path = _validated_rollout_path(codex_home, rollout_raw, native_id)
        if rollout_path is None:
            warnings.append(f"skipped invalid rollout path for {native_id}")
            continue
        updated_at = _timestamp_from_epoch(updated_raw)
        if updated_at is None:
            continue
        try:
            fingerprint = _fingerprint(native_id, rollout_path)
        except OSError:
            warnings.append(f"skipped unavailable rollout for {native_id}")
            continue
        session = CodexSession(
            native_id=native_id,
            title=_safe_title(title) or _safe_title(first_user) or "(untitled Codex session)",
            cwd=cwd,
            updated_at=updated_at,
            source=_source_label(source),
            rollout_path=rollout_path,
            fingerprint=fingerprint,
            discovery="state_db",
            warnings=(),
        )
        sessions.append(session)
    return sessions, warnings


def _scan_rollout_headers(
    codex_home: Path, workspace: Path | None, *, limit: int
) -> tuple[list[CodexSession], list[str]]:
    sessions_root = codex_home / "sessions"
    warnings: list[str] = []
    if not sessions_root.is_dir():
        return [], warnings

    candidates: list[Path] = []
    for directory, dirs, names in os.walk(sessions_root, followlinks=False):
        dirs.sort(reverse=True)
        for name in sorted(names, reverse=True):
            if len(candidates) >= MAX_SCAN_FILES:
                warnings.append("rollout scan truncated at file limit")
                break
            path = Path(directory) / name
            if _ROLLOUT_RE.fullmatch(name):
                candidates.append(path)
        if len(candidates) >= MAX_SCAN_FILES:
            break

    sessions: list[CodexSession] = []
    for path in sorted(candidates, key=_mtime_ns, reverse=True):
        if len(sessions) >= limit:
            break
        match = _ROLLOUT_RE.fullmatch(path.name)
        if match is None:
            continue
        native_id = match.group(1).lower()
        rollout_path = _validated_rollout_path(codex_home, str(path), native_id)
        if rollout_path is None:
            warnings.append(f"skipped invalid rollout path for {native_id}")
            continue
        try:
            metadata = _read_rollout_head(rollout_path)
        except (OSError, ValueError) as exc:
            warnings.append(f"skipped rollout {native_id}: {exc}")
            continue
        if (
            metadata is None
            or (workspace is not None and not _same_path(metadata["cwd"], workspace))
            or metadata["source"] == "unknown"
        ):
            continue
        try:
            fingerprint = _fingerprint(native_id, rollout_path)
        except OSError:
            warnings.append(f"skipped unavailable rollout for {native_id}")
            continue
        updated_at = datetime.fromtimestamp(_mtime_ns(rollout_path) / 1_000_000_000, tz=UTC)
        sessions.append(
            CodexSession(
                native_id=native_id,
                title=metadata["title"] or "(untitled Codex session)",
                cwd=metadata["cwd"],
                updated_at=updated_at,
                source=metadata["source"],
                rollout_path=rollout_path,
                fingerprint=fingerprint,
                discovery="rollout_headers",
                warnings=(),
            )
        )
    return sessions, warnings


def _read_rollout_head(path: Path) -> dict[str, Any] | None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("file metadata unavailable") from exc
    if size > MAX_ROLLOUT_BYTES:
        raise ValueError("file exceeds size limit")

    metadata: dict[str, Any] = {"cwd": None, "source": "unknown", "title": ""}
    try:
        if path.suffix == ".zst":
            with path.open("rb") as raw:
                with zstandard.ZstdDecompressor().stream_reader(raw) as compressed:
                    records = _rollout_head_lines(compressed)
                    _read_rollout_header_records(records, metadata)
        else:
            with path.open("rb") as raw:
                _read_rollout_header_records(_rollout_head_lines(raw), metadata)
    except zstandard.ZstdError as exc:
        raise ValueError("invalid zstd data") from exc
    return metadata if isinstance(metadata["cwd"], Path) else None


def _rollout_head_lines(stream: Any):
    total = 0
    pending = b""
    records = 0
    discarding = False
    while records < MAX_HEAD_RECORDS:
        chunk = stream.read(MAX_HEADER_LINE_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_HEAD_BYTES:
            raise ValueError("header exceeds size limit")
        if discarding:
            newline = chunk.find(b"\n")
            if newline < 0:
                continue
            records += 1
            yield None
            discarding = False
            chunk = chunk[newline + 1 :]
        pending += chunk
        while b"\n" in pending and records < MAX_HEAD_RECORDS:
            line, pending = pending.split(b"\n", 1)
            records += 1
            if len(line) > MAX_HEADER_LINE_BYTES:
                yield None
            else:
                yield line.decode("utf-8", errors="replace")
        if len(pending) > MAX_HEADER_LINE_BYTES:
            pending = b""
            discarding = True
    if pending and not discarding and records < MAX_HEAD_RECORDS:
        yield pending.decode("utf-8", errors="replace")


def _read_rollout_header_records(records: Any, metadata: dict[str, Any]) -> None:
    for line in records:
        if line is None:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta" and isinstance(payload, dict):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and _text_within(cwd, 16 * 1024):
                raw_cwd = Path(cwd)
                if raw_cwd.is_absolute():
                    metadata["cwd"] = _canonical_path(raw_cwd)
            source = payload.get("source")
            if isinstance(source, str) and _allowed_source(source):
                metadata["source"] = _source_label(source)
            title = payload.get("title")
            if isinstance(title, str):
                metadata["title"] = _safe_title(title)
        elif record_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") == "user_message" and not metadata["title"]:
                message = payload.get("message")
                if isinstance(message, str):
                    metadata["title"] = _safe_title(message)
        if (
            isinstance(metadata["cwd"], Path)
            and metadata["source"] != "unknown"
            and metadata["title"]
        ):
            return


def _validated_rollout_path(codex_home: Path, raw_path: str, native_id: str) -> Path | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = codex_home / candidate
    try:
        resolved = candidate.resolve(strict=True)
        sessions_root = (codex_home / "sessions").resolve(strict=True)
    except OSError:
        return None
    if not _is_under(resolved, sessions_root) or not resolved.is_file():
        return None
    match = _ROLLOUT_RE.fullmatch(resolved.name)
    if match is None or match.group(1).lower() != native_id.lower():
        return None
    return resolved


def _valid_native_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        import uuid

        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _allowed_source(value: object) -> bool:
    return isinstance(value, str) and value.replace(" ", "") in _ALLOWED_SOURCES


def _source_label(value: str) -> str:
    normalized = value.replace(" ", "")
    if normalized == "cli":
        return "cli"
    if normalized == "vscode":
        return "vscode"
    if normalized == '{"custom":"atlas"}':
        return "atlas"
    if normalized == '{"custom":"chatgpt"}':
        return "chatgpt"
    return "unknown"


def _text_within(value: object, maximum: int) -> bool:
    return isinstance(value, str) and len(value.encode("utf-8", errors="ignore")) <= maximum


def _safe_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\x00", " ").split())
    if not text or any(marker in text.casefold() for marker in _INTERNAL_TITLE_MARKERS):
        return ""
    return text[:MAX_TITLE_CHARS]


def _timestamp_from_epoch(value: object) -> datetime | None:
    if not isinstance(value, int):
        return None
    seconds = value / 1000 if abs(value) >= 1_577_836_800_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _fingerprint(native_id: str, rollout_path: Path) -> str:
    stat = rollout_path.stat()
    value = "\0".join(
        (native_id, str(rollout_path), str(stat.st_size), str(stat.st_mtime_ns))
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
