"""Extensible bottombar package.

Public layout/registry API lives in ``core``; built-in components live under
``components/`` and are registered via ``DEFAULT_COMPONENT_INSTALLERS``.

Add a component
---------------
1. Create ``components/foo.py`` with ``install(registry, ctx)``.
2. Append ``foo.install`` to ``DEFAULT_COMPONENT_INSTALLERS`` in
   ``components/__init__.py``.
"""

from __future__ import annotations

from synapse.ui.bottombar.components import (
    DEFAULT_COMPONENT_INSTALLERS,
    install_default_components,
    install_default_regions,
)
from synapse.ui.bottombar.context import BottomBarContext
from synapse.ui.bottombar.core import (
    DEFAULT_COL_GAP,
    DEFAULT_REGION_GAP,
    BottomBarAlign,
    BottomBarComponent,
    BottomBarLayout,
    BottomBarRegion,
    BottomBarRegionSpec,
    BottomBarRegistry,
    PackedRegion,
    align_in_width,
    center_in_width,
    display_width,
    join_region_parts,
    layout_from_registry,
    locate_component_span,
    normalize_region_id,
    pack_bottombar_regions,
    pack_layout_from_registry,
    pack_region_list,
    render_packed_line,
    render_region_text,
    truncate_to_width,
)

__all__ = [
    "DEFAULT_COL_GAP",
    "DEFAULT_COMPONENT_INSTALLERS",
    "DEFAULT_REGION_GAP",
    "BottomBarAlign",
    "BottomBarComponent",
    "BottomBarContext",
    "BottomBarLayout",
    "BottomBarRegion",
    "BottomBarRegionSpec",
    "BottomBarRegistry",
    "PackedRegion",
    "align_in_width",
    "center_in_width",
    "display_width",
    "install_default_components",
    "install_default_regions",
    "join_region_parts",
    "layout_from_registry",
    "locate_component_span",
    "normalize_region_id",
    "pack_bottombar_regions",
    "pack_layout_from_registry",
    "pack_region_list",
    "render_packed_line",
    "render_region_text",
    "truncate_to_width",
]
