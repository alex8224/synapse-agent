"""Bottombar component: model id + thinking level (right)."""

from __future__ import annotations

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "model"
REGION = BottomBarRegion.RIGHT
ORDER = 10
PRIORITY = 60  # keep when narrow
MIN_WIDTH = 8


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register model · thinking label."""

    def render() -> str:
        return (ctx.model() or "").strip()

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
