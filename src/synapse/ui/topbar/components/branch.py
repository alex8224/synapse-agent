"""Topbar component: git branch chrome (left, after workspace)."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import TopBarRegion, TopBarRegistry

ID = "branch"
REGION = TopBarRegion.LEFT
ORDER = 20  # immediately right of workspace
PRIORITY = 50
MIN_WIDTH = 6


def install(registry: TopBarRegistry, ctx: TopBarContext) -> None:
    """Register the git branch status component.

    ``ctx.branch`` may return plain text or a pre-styled ``rich.text.Text``
    (dirty/ahead/behind colors). Plain strings get the default branch mark.
    """

    def render() -> str | Text:
        raw = ctx.branch()
        if isinstance(raw, Text):
            return raw if raw.plain.strip() else ""
        name = str(raw or "").strip()
        if not name:
            return ""
        mark = (ctx.branch_mark or "").strip()
        return f"{mark} {name}" if mark else name

    registry.register_fn(
        ID,
        render,
        region=REGION,
        order=ORDER,
        priority=PRIORITY,
        min_width=MIN_WIDTH,
    )
