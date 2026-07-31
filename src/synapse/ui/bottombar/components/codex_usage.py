"""Bottombar component: Codex OAuth usage between model and MCP."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "codex_usage"
REGION = BottomBarRegion.LEFT
ORDER = 15
PRIORITY = 45
MIN_WIDTH = 0


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the one-line, theme-aware Codex usage label."""

    def render() -> str | Text:
        value = ctx.codex_usage()
        if isinstance(value, Text):
            return value
        return (value or "").strip()

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
