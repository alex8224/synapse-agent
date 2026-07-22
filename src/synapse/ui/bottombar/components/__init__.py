"""Built-in bottombar components.

Extension pattern
-----------------
1. Add ``components/my_widget.py`` with::

       ID = "my_widget"

       def install(registry, ctx) -> None:
           registry.register_fn(ID, render, region=..., order=..., priority=...)

2. Append ``my_widget.install`` to :data:`DEFAULT_COMPONENT_INSTALLERS` below.

No other bottombar core files need to change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from synapse.ui.bottombar.components import key_hints, mcp, mode, model, thread
from synapse.ui.bottombar.core import (
    DEFAULT_COL_GAP,
    BottomBarAlign,
    BottomBarRegion,
    BottomBarRegistry,
)

if TYPE_CHECKING:
    from synapse.ui.bottombar.context import BottomBarContext

ComponentInstaller = Callable[[BottomBarRegistry, "BottomBarContext"], None]

# Order here is install order only (layout uses each component's region/order).
DEFAULT_COMPONENT_INSTALLERS: list[ComponentInstaller] = [
    key_hints.install,
    mode.install,
    model.install,
    mcp.install,
    thread.install,
]


def install_default_regions(registry: BottomBarRegistry) -> None:
    """Ensure classic left / center / right region slots exist."""
    registry.register_region(
        BottomBarRegion.LEFT.value,
        order=10,
        flex=1,
        align=BottomBarAlign.LEFT,
        priority=40,
        gap_after=DEFAULT_COL_GAP,
    )
    registry.register_region(
        BottomBarRegion.CENTER.value,
        order=20,
        flex=0,
        align=BottomBarAlign.CENTER,
        priority=10,
        min_width=0,
        gap_after=DEFAULT_COL_GAP,
    )
    registry.register_region(
        BottomBarRegion.RIGHT.value,
        order=30,
        flex=0,
        align=BottomBarAlign.RIGHT,
        priority=50,
        gap_after=0,
    )


def install_default_components(
    registry: BottomBarRegistry,
    ctx: "BottomBarContext | None" = None,
    /,
    *,
    busy: Callable[[], bool] | None = None,
    thread: Callable[[], str] | None = None,
    mode: Callable[[], str] | None = None,
    idle_hints: Callable[[], str] | None = None,
    busy_hints: Callable[[], str] | None = None,
    model: Callable[[], str] | None = None,
    mcp: Callable[[], str] | None = None,
    installers: list[ComponentInstaller] | None = None,
) -> None:
    """Install default regions + component modules.

    Prefer passing :class:`BottomBarContext`. Keyword providers remain for tests.
    """
    from synapse.ui.bottombar.context import BottomBarContext as _Ctx

    def _empty() -> str:
        return ""

    if ctx is None:
        if busy is None:
            raise TypeError(
                "install_default_components requires ctx= or at least busy="
            )
        ctx = _Ctx(
            busy=busy,
            thread=thread or _empty,
            mode=mode or _empty,
            idle_hints=idle_hints
            or (lambda: "Tab complete · / commands · Esc cancel · F2 model · F4 sessions"),
            busy_hints=busy_hints
            or (lambda: "Esc cancel · Enter queue guidance"),
            model=model or _empty,
            mcp=mcp or _empty,
        )
    else:
        ctx = _Ctx(
            busy=ctx.busy,
            thread=thread or ctx.thread,
            mode=mode or ctx.mode,
            idle_hints=idle_hints or ctx.idle_hints,
            busy_hints=busy_hints or ctx.busy_hints,
            model=model or ctx.model,
            mcp=mcp or ctx.mcp,
        )

    install_default_regions(registry)
    for install in installers if installers is not None else DEFAULT_COMPONENT_INSTALLERS:
        install(registry, ctx)


__all__ = [
    "DEFAULT_COMPONENT_INSTALLERS",
    "ComponentInstaller",
    "install_default_components",
    "install_default_regions",
]
