"""Extensible bottombar: freeform regions + components.

Reuses the topbar packing engine so layout/priority/flex semantics stay identical.
Regions default to classic left / center / right; custom region ids are allowed.

Built-in chrome paints into a single-line Textual ``Static`` under the prompt.
"""

from __future__ import annotations

# Re-export topbar layout primitives under bottombar names.
from synapse.ui.topbar.core import (
    DEFAULT_COL_GAP,
    DEFAULT_REGION_GAP,
    PackedRegion,
    TopBarAlign as BottomBarAlign,
    TopBarComponent as BottomBarComponent,
    TopBarLayout as BottomBarLayout,
    TopBarRegion as BottomBarRegion,
    TopBarRegionSpec as BottomBarRegionSpec,
    TopBarRegistry as BottomBarRegistry,
    align_in_width,
    center_in_width,
    display_width,
    join_region_parts,
    layout_from_registry,
    locate_component_span,
    normalize_region_id,
    pack_layout_from_registry,
    pack_region_list,
    pack_topbar_regions as pack_bottombar_regions,
    render_packed_line,
    render_region_text,
    truncate_to_width,
)

__all__ = [
    "DEFAULT_COL_GAP",
    "DEFAULT_REGION_GAP",
    "BottomBarAlign",
    "BottomBarComponent",
    "BottomBarLayout",
    "BottomBarRegion",
    "BottomBarRegionSpec",
    "BottomBarRegistry",
    "PackedRegion",
    "align_in_width",
    "center_in_width",
    "display_width",
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
