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
    align_in_width,
    center_in_width,
    display_width,
    join_region_parts,
    layout_from_registry,
    locate_component_span,
    normalize_region_id,
    pack_layout_from_registry,
    pack_region_list,
    render_packed_line,
    render_region_text,
    truncate_to_width,
)
from synapse.ui.topbar.core import (
    TopBarAlign as BottomBarAlign,
)
from synapse.ui.topbar.core import (
    TopBarComponent as BottomBarComponent,
)
from synapse.ui.topbar.core import (
    TopBarLayout as BottomBarLayout,
)
from synapse.ui.topbar.core import (
    TopBarRegion as BottomBarRegion,
)
from synapse.ui.topbar.core import (
    TopBarRegionSpec as BottomBarRegionSpec,
)
from synapse.ui.topbar.core import (
    TopBarRegistry as BottomBarRegistry,
)
from synapse.ui.topbar.core import (
    pack_topbar_regions as pack_bottombar_regions,
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
