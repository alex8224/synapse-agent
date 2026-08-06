"""Clickable inline status bar for the current turn's subagents."""

from __future__ import annotations

from textual.events import Click
from textual.widgets import Static


class SubagentStatusBar(Static):
    """Clickable inline status bar for the current turn's subagents."""

    def on_click(self, event: Click) -> None:
        event.stop()
        event.prevent_default()
        monitor = getattr(self.app, "_subagent_monitor", None)
        if monitor is None:
            return
        _, runs = monitor.snapshot()
        if not runs:
            return
        opener = getattr(self.app, "_open_subagent_monitor", None)
        if callable(opener):
            opener()
