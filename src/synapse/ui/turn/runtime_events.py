"""Project service runtime events directly into the Textual turn renderer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from synapse.runtime.service.events import RuntimeEvent
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer


def project_runtime_event(renderer: TextualTurnEventRenderer, event: RuntimeEvent) -> bool:
    """Dispatch one service event without constructing a legacy turn event.

    The renderer owns the session/turn fence and sequence cursor.  Returning a
    boolean makes unknown or stale events harmless to callers that want a
    diagnostic counter, while keeping this adapter free of UI state.
    """
    return renderer.render_runtime_event(event)


# Short alias useful for callback registration.
render_runtime_event = project_runtime_event


def _payload(event: RuntimeEvent) -> Mapping[str, Any]:
    return event.payload if isinstance(event.payload, Mapping) else {}
