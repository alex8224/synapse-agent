"""Shared command result contract."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SlashResult:
    """Outcome of a slash command."""

    handled: bool = False
    lines: list[str] = field(default_factory=list)
    error: bool = False
    # Short one-line confirmation for the bottom status bar (never transcript).
    notice: str | None = None
    exit_requested: bool = False
    clear_log: bool = False
    reload_transcript: bool = False
    thread_id: str | None = None
    agent: Any | None = None
    settings_changed: bool = False
    # TUI should attach MCP after applying the rebuilt agent, without blocking
    # the slash command/model switch worker.
    # Rich Markdown text rendered directly into the transcript (#log).  When set,
    # the TUI renders it as a Markdown block instead of plain lines.
    markdown: str | None = None
    mcp_attach_pending: bool = False
    # UI theme switch (TUI should re-apply CSS / palette).
    theme_name: str | None = None
    # HITL: UI should resume the paused graph with this decision.
    resume_action: str | None = None  # "approve" | "reject"
    resume_message: str | None = None
    # Goal pause is a runtime action too: stop the current graph turn after
    # the durable goal state has been changed to paused.
    cancel_active_turn: bool = False
    candidate_settings: Any | None = None
