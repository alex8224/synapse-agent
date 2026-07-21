"""Topbar component: workspace path (left)."""

from __future__ import annotations

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import TopBarRegion, TopBarRegistry

ID = "workspace"
REGION = TopBarRegion.LEFT
ORDER = 10
PRIORITY = 40
MIN_WIDTH = 8


def install(registry: TopBarRegistry, ctx: TopBarContext) -> None:
    """Register the workspace label component."""

    def render() -> str:
        label = (ctx.workspace() or "").strip()
        if not label:
            return ""
        mark = (ctx.workspace_mark or "").strip()
        return f"{mark}  {label}" if mark else label

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
