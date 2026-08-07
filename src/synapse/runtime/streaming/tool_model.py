"""Pure runtime model shared by stream parsers and UI renderers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolItem:
    """One tool invocation tracked across call and result events."""

    id: str
    name: str
    category: str
    label: str
    path: str | None = None
    status: str = "running"
    preview: str | None = None
    error: bool = False
    sub: bool = False
    parent_id: str | None = None
    call_id: str | None = None
