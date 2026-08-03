"""Bottombar component: static FAST badge next to the model/thinking label."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "fast"
REGION = BottomBarRegion.LEFT
# Right after the model · thinking label (model order=10), before Codex usage.
ORDER = 12
PRIORITY = 58  # keep when narrow (just below model)
MIN_WIDTH = 4

# Codex Fast badge palette (brand yellow pill on a dark bar). Fixed on purpose:
# the badge is a status indicator, not a theme-tinted surface.
_C_FAST_BG = "#ffd60a"
_C_FAST_FG = "#202124"


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the static yellow FAST badge while Codex Fast tier is active."""

    def render() -> str | Text:
        if not ctx.fast_mode():
            return ""
        # Leading/trailing spaces widen the yellow pill so the label breathes.
        return Text(" FAST ", style=f"bold {_C_FAST_FG} on {_C_FAST_BG}")

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
