"""Extensible topbar package.

Public layout/registry API lives in ``core``; built-in components live under
``components/`` and are registered via ``DEFAULT_COMPONENT_INSTALLERS``.

Add a component
---------------
1. Create ``components/foo.py`` with ``install(registry, ctx)``.
2. Append ``foo.install`` to ``DEFAULT_COMPONENT_INSTALLERS`` in
   ``components/__init__.py``.
"""

from __future__ import annotations

from synapse.ui.topbar.components import (
    DEFAULT_COMPONENT_INSTALLERS,
    install_default_components,
    install_default_regions,
)
from synapse.ui.topbar.context import TopBarContext
from synapse.ui.topbar.core import (
    DEFAULT_COL_GAP,
    DEFAULT_REGION_GAP,
    PackedRegion,
    TopBarAlign,
    TopBarComponent,
    TopBarLayout,
    TopBarRegion,
    TopBarRegionSpec,
    TopBarRegistry,
    align_in_width,
    center_in_width,
    display_width,
    join_region_parts,
    layout_from_registry,
    locate_component_span,
    normalize_region_id,
    pack_layout_from_registry,
    pack_region_list,
    pack_topbar_regions,
    render_packed_line,
    render_region_text,
    truncate_to_width,
)
from synapse.ui.topbar.git_changes_popover import GitChangesPopover, TopBar
from synapse.ui.topbar.git_chrome import (
    GitBranchChrome,
    GitChangedFile,
    format_branch_chrome_plain,
    format_changed_file_plain,
    probe_git_branch_chrome,
    probe_git_changed_files,
    render_branch_chrome,
    render_changed_file_row,
)

__all__ = [
    "DEFAULT_COL_GAP",
    "DEFAULT_COMPONENT_INSTALLERS",
    "DEFAULT_REGION_GAP",
    "GitBranchChrome",
    "GitChangedFile",
    "GitChangesPopover",
    "PackedRegion",
    "TopBar",
    "TopBarAlign",
    "TopBarComponent",
    "TopBarContext",
    "TopBarLayout",
    "TopBarRegion",
    "TopBarRegionSpec",
    "TopBarRegistry",
    "align_in_width",
    "center_in_width",
    "display_width",
    "format_branch_chrome_plain",
    "format_changed_file_plain",
    "install_default_components",
    "install_default_regions",
    "join_region_parts",
    "layout_from_registry",
    "locate_component_span",
    "normalize_region_id",
    "pack_layout_from_registry",
    "pack_region_list",
    "pack_topbar_regions",
    "probe_git_branch_chrome",
    "probe_git_changed_files",
    "render_branch_chrome",
    "render_changed_file_row",
    "render_packed_line",
    "render_region_text",
    "truncate_to_width",
]
