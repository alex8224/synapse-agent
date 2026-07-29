"""Thread-scoped turn and model-call correlation for compression diagnostics."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InteractionPosition:
    turn_id: str
    turn_index: int
    model_call_index: int


@dataclass
class _ThreadPosition:
    user_fingerprint: str = ""
    turn_id: str = ""
    turn_index: int = 0
    model_call_index: int = 0


_lock = threading.RLock()
_positions: dict[str, _ThreadPosition] = {}


def user_fingerprint(content: Any) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()[:16]


def begin_model_call(
    thread_id: str,
    current_user_content: Any,
    *,
    turn_index_hint: int = 0,
    model_call_index_hint: int = 0,
) -> InteractionPosition:
    """Advance a thread position using deterministic persisted-position hints."""
    fingerprint = user_fingerprint(current_user_content)
    with _lock:
        state = _positions.setdefault(thread_id, _ThreadPosition())
        hinted_turn = max(0, int(turn_index_hint or 0))
        new_turn = (
            not state.turn_id
            or state.user_fingerprint != fingerprint
            or (hinted_turn and state.turn_index != hinted_turn)
        )
        if new_turn:
            state.turn_index = hinted_turn or state.turn_index + 1
            state.model_call_index = max(0, int(model_call_index_hint or 0))
            state.user_fingerprint = fingerprint
            seed = f"{thread_id}:{state.turn_index}:{fingerprint}"
            state.turn_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        else:
            state.model_call_index = max(
                state.model_call_index, max(0, int(model_call_index_hint or 0))
            )
        state.model_call_index += 1
        return InteractionPosition(
            turn_id=state.turn_id,
            turn_index=state.turn_index,
            model_call_index=state.model_call_index,
        )


def current_position(thread_id: str) -> InteractionPosition:
    with _lock:
        state = _positions.get(thread_id) or _ThreadPosition()
        return InteractionPosition(
            turn_id=state.turn_id,
            turn_index=state.turn_index,
            model_call_index=state.model_call_index,
        )


def clear_interaction_positions() -> None:
    """Reset process-local positions for tests and clean agent restarts."""
    with _lock:
        _positions.clear()
