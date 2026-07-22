"""Bottombar component: MCP status (left), colored by state."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import BottomBarRegion, BottomBarRegistry

ID = "mcp"
REGION = BottomBarRegion.LEFT
ORDER = 20
PRIORITY = 55
MIN_WIDTH = 6

# Fallback palette (overridden by active theme when available).
_C_GREEN = "#81c995"
_C_ERROR = "#f28b82"
_C_MUTED = "#5f6368"


def _palette() -> tuple[str, str, str]:
    """Return (green, error, muted) from the active theme when possible."""
    try:
        from synapse.ui.theme import get_theme

        t = get_theme()
        return (
            str(getattr(t, "green", _C_GREEN) or _C_GREEN),
            str(getattr(t, "error", _C_ERROR) or _C_ERROR),
            str(getattr(t, "muted", _C_MUTED) or _C_MUTED),
        )
    except Exception:  # noqa: BLE001
        return _C_GREEN, _C_ERROR, _C_MUTED


def style_for_mcp_label(label: str) -> str:
    """Map ``mcp on`` / ``mcp err`` / ``mcp off`` to a paint color."""
    key = (label or "").strip().lower()
    green, error, muted = _palette()
    if key == "mcp on" or key.endswith(" on"):
        return green
    if key == "mcp err" or "err" in key or "error" in key:
        return error
    return muted


def install(registry: BottomBarRegistry, ctx: BottomBarContext) -> None:
    """Register mcp on/off/err label with state colors."""

    def render() -> str | Text:
        label = (ctx.mcp() or "").strip()
        if not label:
            return ""
        return Text(label, style=style_for_mcp_label(label))

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
