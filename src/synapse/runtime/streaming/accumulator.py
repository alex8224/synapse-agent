"""Runtime-owned turn state independent from any renderer."""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.streaming.events import (
    EVENT_VERSION,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
    UsagePayload,
)
from synapse.runtime.streaming.protocol import AgentEventSink, NullEventSink

_WS_RE = re.compile(r"\s+")


@dataclass
class TurnAccumulator:
    """Accumulate semantic turn state and publish ordered events.

    The object is the source of truth for streamed text and usage. Renderers may
    be absent, detached, or lossy without changing the final turn result.
    """

    thread_id: str
    event_sink: AgentEventSink = field(default_factory=NullEventSink)
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    answer_buf: list[str] = field(default_factory=list)
    reasoning_buf: list[str] = field(default_factory=list)
    open_answer: list[str] = field(default_factory=list)
    open_reasoning: list[str] = field(default_factory=list)
    streamed_answer: bool = False
    streamed_reasoning: bool = False
    tool_calls: int = 0
    usage: UsagePayload = field(default_factory=UsagePayload)
    terminal_status: str | None = None
    terminal_event: TurnEvent | None = None
    _sequence: int = 0
    _complete_ids: set[str] = field(default_factory=set)
    _complete_texts: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def normalize_text(text: str) -> str:
        return _WS_RE.sub(" ", (text or "").strip())

    @property
    def sequence(self) -> int:
        return self._sequence

    def emit(self, kind: TurnEventKind, payload: object) -> TurnEvent:
        with self._lock:
            self._sequence += 1
            event = TurnEvent(
                version=EVENT_VERSION,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                sequence=self._sequence,
                kind=kind,
                payload=payload,
            )
        try:
            self.event_sink.emit(event)
        except Exception:  # noqa: BLE001 - observers cannot fail the turn
            pass
        return event

    def answer_delta(self, text: str, message_id: str | None) -> None:
        if not text or (message_id and message_id in self._complete_ids):
            return
        self.open_answer.append(text)
        self.streamed_answer = True

    def answer_completed(self, text: str, message_id: str | None) -> bool:
        body = (text or "").strip()
        if not body:
            return False
        key = self.normalize_text(body)
        if (message_id and message_id in self._complete_ids) or key in self._complete_texts:
            self.open_answer.clear()
            return False
        if message_id:
            self._complete_ids.add(message_id)
        self._complete_texts.add(key)
        self.open_answer.clear()
        self.answer_buf.append(body)
        self.streamed_answer = True
        return True

    def finalize_answer(self) -> str:
        body = "".join(self.open_answer).strip()
        self.open_answer.clear()
        if body:
            self.answer_completed(body, None)
        return body

    def reasoning_delta(self, text: str) -> None:
        if not text:
            return
        self.open_reasoning.append(text)
        self.reasoning_buf.append(text)
        self.streamed_reasoning = True

    def close_reasoning(self) -> str:
        body = "".join(self.open_reasoning).strip()
        self.open_reasoning.clear()
        return body

    def clear_leaked_answer(self) -> None:
        self.open_answer.clear()
        self.answer_buf.clear()
        self.streamed_answer = False

    def note_tool_batch(self, count: int) -> None:
        self.tool_calls += max(0, int(count))

    def note_usage(self, usage: UsagePayload) -> None:
        self.usage = usage

    def terminate(self, payload: TurnTerminalPayload) -> TurnEvent:
        """Emit one mutually exclusive terminal event for this turn."""
        with self._lock:
            existing = self.terminal_event
            if existing is not None:
                return existing
            kind = {
                "completed": TurnEventKind.TURN_COMPLETED,
                "cancelled": TurnEventKind.TURN_CANCELLED,
                "waiting_approval": TurnEventKind.TURN_WAITING_APPROVAL,
                "failed": TurnEventKind.TURN_FAILED,
            }.get(payload.status)
            if kind is None:
                raise ValueError(f"unsupported terminal status: {payload.status!r}")
            self._sequence += 1
            event = TurnEvent(
                version=EVENT_VERSION,
                thread_id=self.thread_id,
                turn_id=self.turn_id,
                sequence=self._sequence,
                kind=kind,
                payload=payload,
            )
            self.terminal_status = payload.status
            self.terminal_event = event
        try:
            self.event_sink.emit(event)
        except Exception:  # noqa: BLE001 - observers cannot fail the turn
            pass
        return event

    @property
    def final_answer_text(self) -> str:
        self.finalize_answer()
        return "".join(self.answer_buf).strip()

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning_buf).strip()

    @classmethod
    def thread_id_from_config(cls, config: dict[str, Any] | None) -> str:
        configurable = (config or {}).get("configurable") or {}
        return str(configurable.get("thread_id") or "")
