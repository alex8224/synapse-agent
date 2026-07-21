"""Topbar component: session title (center)."""

from __future__ import annotations

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import TopBarRegion, TopBarRegistry

ID = "title"
REGION = TopBarRegion.CENTER
ORDER = 10
PRIORITY = 10  # shrink first when narrow
MIN_WIDTH = 4


def install(registry: TopBarRegistry, ctx: TopBarContext) -> None:
    """Register the session title component."""
    registry.register_fn(
        ID,
        lambda: (ctx.title() or "").strip(),
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
