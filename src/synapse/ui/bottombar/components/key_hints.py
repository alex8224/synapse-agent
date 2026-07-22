"""Bottombar component: contextual key hints (left)."""

from __future__ import annotations

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "key_hints"
REGION = BottomBarRegion.LEFT
ORDER = 10
PRIORITY = 40
MIN_WIDTH = 12


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the contextual key-hint line."""

    def render() -> str:
        if ctx.busy():
            return (ctx.busy_hints() or "").strip()
        return (ctx.idle_hints() or "").strip()

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
