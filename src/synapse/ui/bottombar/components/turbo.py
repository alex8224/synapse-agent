"""Bottombar component: static TURBO badge next to the model/thinking label."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "turbo"
REGION = BottomBarRegion.LEFT
# Right after the FAST badge (order=12).
ORDER = 13
PRIORITY = 57  # keep when narrow (just below FAST)
MIN_WIDTH = 4

# Headroom-turbo badge palette (cyan pill on a dark bar), mirroring the
# headroom dashboard accent. Fixed on purpose: a status indicator, not a
# theme-tinted surface.
_C_TURBO_BG = "#00d4aa"
_C_TURBO_FG = "#202124"


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the static cyan TURBO badge while turbo mode is active."""

    def render() -> str | Text:
        if not ctx.turbo():
            return ""
        return Text(" TURBO ", style=f"bold {_C_TURBO_FG} on {_C_TURBO_BG}")

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
