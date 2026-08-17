"""Bottombar component: last turn latency/throughput (TTFT + tok/s)."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "turn_stats"
REGION = BottomBarRegion.CENTER
ORDER = 20
PRIORITY = 20  # drop after mode when narrow
MIN_WIDTH = 0


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register the one-line, theme-aware turn latency/throughput label."""

    def render() -> str | Text:
        value = ctx.turn_stats()
        if isinstance(value, Text):
            return value
        return (value or "").strip()

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
