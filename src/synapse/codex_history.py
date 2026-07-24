"""Pure, fail-closed projection of Codex rollouts into visible text snapshots.

This module deliberately does not reconstruct Codex runtime state.  It produces
only the effective human and assistant text of completed turns.  Callers must
check ``CodexTextSnapshot.importable`` before using its messages for any future
import workflow.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

PROJECTION_KIND = "codex_visible_text_v1"
PARSER_VERSION = 1
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_ROLLOUT_LINE_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_USER_MESSAGE_BEGIN = "## My request for Codex:"
_INTERNAL_USER_MARKERS = (
    "<environment_context>",
    "<user_instructions>",
    "<developer_instructions>",
    "# agents.md instructions",
)


@dataclass(frozen=True)
class CodexProjectionWarning:
    """A sanitized diagnostic that never includes source text or raw payloads."""

    code: str
    line_number: int | None = None


@dataclass(frozen=True)
class CodexVisibleMessage:
    """One stable visible message in the projected snapshot."""

    source_id: str
    turn_id: str
    role: str
    text: str


@dataclass(frozen=True)
class CodexTextSnapshot:
    """Immutable result of the ``codex_visible_text_v1`` projection."""

    projection_kind: str
    parser_version: int
    messages: tuple[CodexVisibleMessage, ...]
    warnings: tuple[CodexProjectionWarning, ...]
    importable: bool


@dataclass
class _Turn:
    turn_id: str
    messages: list[CodexVisibleMessage]
    state: str


class CodexHistoryProjector:
    """Project a Codex JSONL rollout without side effects.

    Normal rollout text is accepted only from ``event_msg.user_message`` and
    ``event_msg.agent_message``.  Bare ``response_item`` records are ignored
    because Codex writes them for model context and they may duplicate UI text.
    A ``compacted.replacement_history`` record is the sole exception: it becomes
    the complete new baseline after strict message-only validation.
    """

    def project_path(self, path: Path | str) -> CodexTextSnapshot:
        """Read one bounded UTF-8 JSONL or JSONL.zst rollout snapshot."""
        rollout_path = Path(path)
        try:
            lines = _rollout_lines(rollout_path)
            return self.project_lines(lines)
        except _RolloutReadError as exc:
            return _rejected_snapshot((CodexProjectionWarning(exc.code),))
        except OSError:
            return _rejected_snapshot((CodexProjectionWarning("rollout_read_failed"),))
        except UnicodeDecodeError:
            return _rejected_snapshot((CodexProjectionWarning("rollout_not_utf8"),))

    def project_lines(self, lines: Iterable[str]) -> CodexTextSnapshot:
        """Project JSONL records from a caller-owned text stream or iterable."""
        state = _ProjectionState()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                state.reject("invalid_json", line_number)
                break
            if not isinstance(record, dict):
                state.reject("invalid_record", line_number)
                break
            state.handle(record, line_number)
            if state.rejected:
                break
        return state.finish()


class _RolloutReadError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code


def _rollout_lines(path: Path) -> Iterator[str]:
    if path.suffix == ".zst":
        try:
            with path.open("rb") as raw:
                with zstandard.ZstdDecompressor().stream_reader(raw) as compressed:
                    yield from _decode_rollout_lines(compressed)
        except zstandard.ZstdError as exc:
            raise _RolloutReadError("rollout_zstd_invalid") from exc
        return
    with path.open("rb") as raw:
        yield from _decode_rollout_lines(raw)


def _decode_rollout_lines(stream: Any) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    total_bytes = 0
    while chunk := stream.read(_READ_CHUNK_BYTES):
        total_bytes += len(chunk)
        if total_bytes > MAX_ROLLOUT_BYTES:
            raise _RolloutReadError("rollout_size_limit")
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            if len(line.encode()) > MAX_ROLLOUT_LINE_BYTES:
                raise _RolloutReadError("rollout_line_limit")
            yield line
        if len(pending.encode()) > MAX_ROLLOUT_LINE_BYTES:
            raise _RolloutReadError("rollout_line_limit")
    pending += decoder.decode(b"", final=True)
    if pending:
        if len(pending.encode()) > MAX_ROLLOUT_LINE_BYTES:
            raise _RolloutReadError("rollout_line_limit")
        yield pending

class _ProjectionState:
    def __init__(self) -> None:
        self.completed_turns: list[_Turn] = []
        self.current_turn: _Turn | None = None
        self.next_implicit_turn = 0
        self.warnings: list[CodexProjectionWarning] = []
        self.rejected = False

    def reject(self, code: str, line_number: int | None = None) -> None:
        self.warnings.append(CodexProjectionWarning(code, line_number))
        self.rejected = True

    def warn(self, code: str, line_number: int | None = None) -> None:
        self.warnings.append(CodexProjectionWarning(code, line_number))

    def handle(self, record: dict[str, Any], line_number: int) -> None:
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "event_msg":
            if not isinstance(payload, dict):
                self.reject("invalid_event_payload", line_number)
                return
            self.handle_event(payload, line_number)
        elif record_type == "compacted":
            self.handle_compacted(payload, line_number)
        elif record_type in {
            "session_meta",
            "turn_context",
            "world_state",
            "response_item",
            "inter_agent_communication",
            "inter_agent_communication_metadata",
        }:
            return
        else:
            self.warn("ignored_record_type", line_number)

    def handle_event(self, event: dict[str, Any], line_number: int) -> None:
        event_type = event.get("type")
        if event_type in {"task_started", "turn_started"}:
            turn_id = event.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                self.reject("invalid_turn_started", line_number)
                return
            if self.current_turn is not None:
                self.warn("superseded_unfinished_turn", line_number)
            self.current_turn = _Turn(turn_id, [], "open")
        elif event_type in {"task_complete", "turn_complete"}:
            self.complete_turn(event.get("turn_id"), line_number)
        elif event_type == "turn_aborted":
            self.abort_turn(event.get("turn_id"), line_number)
        elif event_type == "thread_rolled_back":
            self.rollback(event.get("num_turns"), line_number)
        elif event_type == "user_message":
            self.append_message("user", event.get("message"), line_number)
        elif event_type == "agent_message":
            self.append_message("assistant", event.get("message"), line_number)
        elif event_type in {
            "agent_reasoning",
            "agent_reasoning_raw_content",
            "context_compacted",
            "error",
            "warning",
        }:
            return
        else:
            self.warn("ignored_event_type", line_number)

    def append_message(self, role: str, text: Any, line_number: int) -> None:
        if not isinstance(text, str):
            self.reject("invalid_visible_message", line_number)
            return
        if role == "user":
            text = _visible_user_text(text)
            if text is None:
                self.reject("internal_user_message", line_number)
                return
        if not text:
            return
        turn = self._ensure_turn()
        position = len(turn.messages)
        source_id = _source_id(turn.turn_id, position, role, text)
        turn.messages.append(CodexVisibleMessage(source_id, turn.turn_id, role, text))

    def complete_turn(self, turn_id: Any, line_number: int) -> None:
        if self.current_turn is None:
            self.warn("unmatched_turn_complete", line_number)
            return
        if isinstance(turn_id, str) and turn_id and turn_id != self.current_turn.turn_id:
            self.warn("unmatched_turn_complete", line_number)
            return
        self.current_turn.state = "complete"
        self.completed_turns.append(self.current_turn)
        self.current_turn = None

    def abort_turn(self, turn_id: Any, line_number: int) -> None:
        if self.current_turn is not None and (
            not isinstance(turn_id, str) or not turn_id or turn_id == self.current_turn.turn_id
        ):
            self.current_turn = None
            self.warn("aborted_turn_omitted", line_number)
            return
        if isinstance(turn_id, str) and turn_id:
            for index, turn in enumerate(self.completed_turns):
                if turn.turn_id == turn_id:
                    del self.completed_turns[index]
                    self.warn("aborted_turn_omitted", line_number)
                    return
        self.warn("unmatched_turn_aborted", line_number)

    def rollback(self, num_turns: Any, line_number: int) -> None:
        if not isinstance(num_turns, int) or isinstance(num_turns, bool) or num_turns < 0:
            self.reject("invalid_rollback", line_number)
            return
        if self.current_turn is not None:
            self.warn("unfinished_turn_omitted", line_number)
            self.current_turn = None
        if num_turns > len(self.completed_turns):
            self.reject("rollback_exceeds_completed_turns", line_number)
            return
        if num_turns:
            del self.completed_turns[-num_turns:]

    def handle_compacted(self, payload: Any, line_number: int) -> None:
        if not isinstance(payload, dict):
            self.reject("invalid_compaction_payload", line_number)
            return
        replacement_history = payload.get("replacement_history")
        if replacement_history is None:
            self.reject("legacy_compaction_unsupported", line_number)
            return
        messages = _parse_replacement_history(replacement_history, line_number, self)
        if messages is None:
            return
        self.completed_turns = [_Turn("replacement_history", messages, "complete")]
        self.current_turn = None

    def _ensure_turn(self) -> _Turn:
        if self.current_turn is None:
            turn_id = f"implicit-{self.next_implicit_turn}"
            self.next_implicit_turn += 1
            self.current_turn = _Turn(turn_id, [], "implicit")
        return self.current_turn

    def finish(self) -> CodexTextSnapshot:
        if self.rejected:
            return _rejected_snapshot(tuple(self.warnings))
        if self.current_turn is not None:
            self.warn("unfinished_turn_omitted")
        messages = tuple(message for turn in self.completed_turns for message in turn.messages)
        return CodexTextSnapshot(
            projection_kind=PROJECTION_KIND,
            parser_version=PARSER_VERSION,
            messages=messages,
            warnings=tuple(self.warnings),
            importable=True,
        )


def _parse_replacement_history(
    replacement_history: Any, line_number: int, state: _ProjectionState
) -> list[CodexVisibleMessage] | None:
    if not isinstance(replacement_history, list):
        state.reject("invalid_replacement_history", line_number)
        return None
    messages: list[CodexVisibleMessage] = []
    for index, item in enumerate(replacement_history):
        if not isinstance(item, dict) or item.get("type") != "message":
            state.reject("unsupported_replacement_item", line_number)
            return None
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, list):
            state.reject("unsupported_replacement_item", line_number)
            return None
        text_parts: list[str] = []
        for content_item in content:
            if not isinstance(content_item, dict):
                state.reject("unsupported_replacement_content", line_number)
                return None
            content_type = content_item.get("type")
            expected_type = "input_text" if role == "user" else "output_text"
            text = content_item.get("text")
            if content_type != expected_type or not isinstance(text, str):
                state.reject("unsupported_replacement_content", line_number)
                return None
            if text:
                text_parts.append(text)
        text = "\n".join(text_parts)
        if not text:
            continue
        source_id = _source_id("replacement_history", index, role, text)
        messages.append(CodexVisibleMessage(source_id, "replacement_history", role, text))
    return messages


def _visible_user_text(text: str) -> str | None:
    marker_index = text.find(_USER_MESSAGE_BEGIN)
    visible = text[marker_index + len(_USER_MESSAGE_BEGIN) :] if marker_index >= 0 else text
    visible = visible.strip()
    if any(marker in visible.casefold() for marker in _INTERNAL_USER_MARKERS):
        return None
    return visible


def _source_id(turn_id: str, position: int, role: str, text: str) -> str:
    digest = hashlib.sha256(
        f"{PARSER_VERSION}\0{turn_id}\0{position}\0{role}\0{text}".encode()
    ).hexdigest()
    return f"codex-visible-v1-{digest[:24]}"


def _rejected_snapshot(
    warnings: tuple[CodexProjectionWarning, ...],
) -> CodexTextSnapshot:
    return CodexTextSnapshot(
        projection_kind=PROJECTION_KIND,
        parser_version=PARSER_VERSION,
        messages=(),
        warnings=warnings,
        importable=False,
    )
