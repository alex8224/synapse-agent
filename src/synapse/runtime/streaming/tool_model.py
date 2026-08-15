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
    # Subagent metadata, populated only for top-level ``task`` items. Kept
    # structured (not pre-formatted) so the UI can style and fall back
    # per-field; nested sub-items leave these empty.
    subagent_name: str | None = None
    subagent_model: str | None = None
    subagent_reasoning_effort: str | None = None
    subagent_model_inherited: bool = False
    subagent_reasoning_inherited: bool = False
