"""Bottombar component: MCP status (right)."""

from __future__ import annotations

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "mcp"
REGION = BottomBarRegion.RIGHT
ORDER = 20
PRIORITY = 55
MIN_WIDTH = 6


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register mcp on/off/err label."""

    def render() -> str:
        return (ctx.mcp() or "").strip()

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
