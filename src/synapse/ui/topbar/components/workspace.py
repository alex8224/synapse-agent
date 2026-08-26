"""Topbar component: workspace path (left)."""

from __future__ import annotations

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import (
    TopBarRegion,
    TopBarRegistry,
    _elide_tail,
    display_width,
)

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

    def render_for_width(width: int) -> str:
        """Adapt to available width: keep the full label when it fits, otherwise
        elide to the trailing (meaningful) path segment."""
        label = (ctx.workspace() or "").strip()
        if not label:
            return ""
        mark = (ctx.workspace_mark or "").strip()
        head = f"{mark}  {label}" if mark else label
        max_w = max(0, int(width))
        if display_width(head) <= max_w:
            return head
        return _elide_tail(head, max_w)

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
        render_for_width=render_for_width,
    )
