"""Topbar component: token usage (right)."""

from __future__ import annotations

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import TopBarRegion, TopBarRegistry

ID = "usage"
REGION = TopBarRegion.RIGHT
ORDER = 10
PRIORITY = 60  # keep when narrow
MIN_WIDTH = 8


def install(registry: TopBarRegistry, ctx: TopBarContext) -> None:
    """Register the in/cache/out + occupancy usage component."""
    registry.register_fn(
        ID,
        lambda: (ctx.usage() or "").strip(),
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
