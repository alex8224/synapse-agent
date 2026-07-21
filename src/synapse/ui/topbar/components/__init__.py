"""Built-in topbar components.

Extension pattern
-----------------
1. Add ``components/my_widget.py`` with::

       ID = "my_widget"

       def install(registry, ctx) -> None:
           registry.register_fn(ID, render, region=..., order=..., priority=...)

2. Append ``my_widget.install`` to :data:`DEFAULT_COMPONENT_INSTALLERS` below.

No other topbar core files need to change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from synapse.ui.topbar.components import branch, title, usage, workspace
from synapse.ui.topbar.core import (
    DEFAULT_COL_GAP,
    TopBarAlign,
    TopBarRegion,
    TopBarRegistry,
)

if TYPE_CHECKING:
    from synapse.ui.topbar.context import TopBarContext

ComponentInstaller = Callable[[TopBarRegistry, "TopBarContext"], None]

# Order here is install order only (layout uses each component's region/order).
# To add a component: create a module, then append its ``install`` here.
DEFAULT_COMPONENT_INSTALLERS: list[ComponentInstaller] = [
    workspace.install,
    title.install,
    branch.install,
    usage.install,
]


def install_default_regions(registry: TopBarRegistry) -> None:
    """Ensure classic left / center / right region slots exist."""
    registry.register_region(
        TopBarRegion.LEFT.value,
        order=10,
        flex=0,
        align=TopBarAlign.LEFT,
        priority=40,
        gap_after=DEFAULT_COL_GAP,
    )
    registry.register_region(
        TopBarRegion.CENTER.value,
        order=20,
        flex=1,
        align=TopBarAlign.CENTER,
        priority=10,
        min_width=4,
        gap_after=DEFAULT_COL_GAP,
    )
    registry.register_region(
        TopBarRegion.RIGHT.value,
        order=30,
        flex=0,
        align=TopBarAlign.RIGHT,
        priority=50,
        gap_after=0,
    )


def install_default_components(
    registry: TopBarRegistry,
    ctx: TopBarContext | None = None,
    /,
    *,
    workspace: Callable[[], str] | None = None,
    title: Callable[[], str] | None = None,
    branch: Callable | None = None,
    usage: Callable[[], str] | None = None,
    workspace_mark: str = "≡",
    branch_mark: str = "⎇",
    installers: list[ComponentInstaller] | None = None,
) -> None:
    """Install default regions + component modules.

    Prefer passing :class:`TopBarContext`. Keyword providers remain for tests
    and call sites that have not migrated yet.
    """
    from synapse.ui.topbar.context import TopBarContext as _Ctx

    if ctx is None:
        if workspace is None or title is None or branch is None or usage is None:
            raise TypeError(
                "install_default_components requires ctx= or workspace/title/branch/usage="
            )
        ctx = _Ctx(
            workspace=workspace,
            title=title,
            branch=branch,
            usage=usage,
            workspace_mark=workspace_mark,
            branch_mark=branch_mark,
        )

    install_default_regions(registry)
    for install in installers or DEFAULT_COMPONENT_INSTALLERS:
        install(registry, ctx)


__all__ = [
    "DEFAULT_COMPONENT_INSTALLERS",
    "ComponentInstaller",
    "install_default_components",
    "install_default_regions",
]
