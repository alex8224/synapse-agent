"""Host context passed into bottombar component installers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text

# Providers may return plain text or pre-styled Rich Text.
LabelFn = Callable[[], str]
RichLabelFn = Callable[[], str | Text]
BoolFn = Callable[[], bool]


@dataclass(slots=True)
class BottomBarContext:
    """Data sources for built-in (and custom) bottombar components.

    App/host fills these callables; each component module only reads what it needs.
    """

    # True while the agent run is active (steer mode / cancelable).
    busy: BoolFn
    # Optional short session / thread label (left, with model/mcp).
    thread: LabelFn
    # Optional extra mode tag (e.g. "safe", "steer×2"); empty hides the piece.
    mode: LabelFn
    # Idle key-hint line (right).
    idle_hints: LabelFn
    # Busy key-hint line (right).
    busy_hints: LabelFn
    # Model id + thinking level (left; e.g. ``haha-grok-4.5 · max``).
    model: LabelFn
    # MCP chrome (left; ``mcp on`` / ``mcp off`` / ``mcp err``).
    mcp: LabelFn
