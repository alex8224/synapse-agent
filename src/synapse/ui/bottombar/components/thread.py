"""Bottombar component: short thread / session id (right)."""

from __future__ import annotations

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "thread"
REGION = BottomBarRegion.RIGHT
ORDER = 30
PRIORITY = 20  # drop before model/mcp when narrow
MIN_WIDTH = 4


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the short thread label on the right."""

    def render() -> str:
        label = (ctx.thread() or "").strip()
        return label

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
