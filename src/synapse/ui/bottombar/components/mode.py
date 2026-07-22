"""Bottombar component: optional mode tag (center)."""

from __future__ import annotations

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "mode"
REGION = BottomBarRegion.CENTER
ORDER = 10
PRIORITY = 10  # shrink first when narrow
MIN_WIDTH = 0


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the optional mode label (empty string hides it)."""
    registry.register_fn(
        ID,
        lambda: (ctx.mode() or "").strip(),
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
