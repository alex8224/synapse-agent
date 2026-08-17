"""State and handle models for a UI-independent agent turn."""

from __future__ import annotations

import concurrent.futures
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from synapse.runtime.cancellation import mark_cancel_event

if TYPE_CHECKING:
    from synapse.runtime.agent_loop.request import TurnRequest


class TurnStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class CancelToken:
    """Thread-safe and idempotent cancellation signal for one turn."""

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event or threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "user") -> bool:
        """Set cancellation once and return whether this call changed state."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = str(reason or "user")
            # ``stream_agent`` accepts a plain threading.Event for compatibility.
            # Attach the origin before setting it so the parser can distinguish
            # user ESC from lifecycle/goal cancellation without changing that API.
            mark_cancel_event(self._event, self._reason)
            self._event.set()
            return True


@dataclass(frozen=True, slots=True)
class TurnContext:
    """All inputs frozen before one graph run starts."""

    thread_id: str
    agent: Any
    settings: Any
    request: TurnRequest
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError("thread_id is required")
        if self.request.thread_id != self.thread_id:
            raise ValueError("TurnRequest.thread_id must match TurnContext.thread_id")
        if self.agent is None:
            raise ValueError("agent is required")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Runtime-owned outcome independent from any renderer/subscriber."""

    turn_id: str
    thread_id: str
    status: TurnStatus
    state: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    reasoning_text: str = ""
    tool_calls: int = 0
    elapsed_s: float = 0.0
    streamed_answer: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    total_tokens: int = 0
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_tokens: int = 0
    last_output_tokens_per_second: float | None = None
    last_ttft_s: float | None = None
    last_rate_basis: str = "end_to_end"
    model_calls: int = 0
    compact_events: int = 0
    cancel_reason: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.status is TurnStatus.CANCELLED

    @property
    def interrupted(self) -> bool:
        return self.status is TurnStatus.WAITING_APPROVAL

    @property
    def failed(self) -> bool:
        return self.status is TurnStatus.FAILED


@dataclass(frozen=True, slots=True)
class TurnHandle:
    """Thread-safe handle returned when a turn is submitted to AsyncRuntime."""

    turn_id: str
    future: concurrent.futures.Future[TurnResult]
    cancel_token: CancelToken

    def cancel(self, reason: str = "user") -> bool:
        return self.cancel_token.cancel(reason)

    def done(self) -> bool:
        return self.future.done()

    def result(self, timeout: float | None = None) -> TurnResult:
        return self.future.result(timeout=timeout)
