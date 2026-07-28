"""Topbar component: effective tool-output compression savings."""

from __future__ import annotations

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import TopBarRegistry

ID = "tool_output"
REGION = "tool_output"
ORDER = 10
PRIORITY = 45
MIN_WIDTH = 8


def install(registry: TopBarRegistry, ctx: TopBarContext) -> None:
    """Register the compact tool-output savings label in its own region."""
    registry.register_fn(
        ID,
        lambda: ctx.tool_output() or "",
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
