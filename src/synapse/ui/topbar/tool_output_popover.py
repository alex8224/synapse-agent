"""Hover card for cumulative tool-output compression metrics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import Vertical
from textual.events import Enter, Leave
from textual.widgets import Static

from synapse.ui.topbar.core import display_width

if TYPE_CHECKING:
    from synapse.ui.topbar.widget import TopBar


def _format_bytes(value: Any) -> str:
    amount = max(0, int(value or 0))
    if amount >= 1024**2:
        return f"{amount / 1024**2:.1f} MiB"
    if amount >= 1024:
        return f"{amount / 1024:.1f} KiB"
    return f"{amount} B"


class ToolOutputPopover(Vertical):
    """Read-only hover summary for cumulative tool-output compression."""

    DEFAULT_CSS = """
    ToolOutputPopover {
        layer: overlay;
        width: auto;
        height: auto;
        padding: 0 1;
        border: solid $theme-border;
        background: $theme-bar;
        color: $theme-fg;
    }
    ToolOutputPopover Static {
        height: 1;
        width: auto;
    }
    """

    def __init__(self, stats: dict[str, Any], *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._stats = dict(stats or {})
        self._owner: TopBar | None = None

    def _lines(self) -> list[str]:
        stats = self._stats
        original = int(stats.get("original_bytes", 0) or 0)
        visible = int(stats.get("visible_bytes", 0) or 0)
        reread = int(stats.get("retrieval_bytes", 0) or 0)
        net = int(stats.get("effective_saved_bytes", 0) or 0)
        ratio = float(stats.get("effective_savings_ratio", 0.0) or 0.0)
        return [
            f"tool input      {_format_bytes(original)}",
            f"model-visible   {_format_bytes(visible)}",
            f"re-read cost    {_format_bytes(reread)}",
            f"net saved       {_format_bytes(net)} ({ratio:.0%})",
            (
                f"{int(stats.get('outputs_considered', 0) or 0)} outputs · "
                f"{int(stats.get('transformed', 0) or 0)} compressed · "
                f"{int(stats.get('skipped', 0) or 0)} skipped"
            ),
        ]

    def compose(self):  # type: ignore[override]
        for line in self._lines():
            yield Static(line)

    def measure_width(self) -> int:
        return max(36, min(52, max(display_width(line) for line in self._lines()) + 4))

    def on_enter(self, event: Enter) -> None:
        event.stop()
        if self._owner is not None:
            self._owner.on_tool_output_popover_enter()

    def on_leave(self, event: Leave) -> None:
        event.stop()
        if self._owner is not None:
            self._owner.on_tool_output_popover_leave()
