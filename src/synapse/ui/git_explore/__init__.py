"""Git explore: file list + textual-diff-view (Rich fallback)."""

from __future__ import annotations

from synapse.ui.git_explore.engine import (
    HAS_DIFF_VIEW,
    fallback_renderable,
    make_diff_view,
    status_line,
)
from synapse.ui.git_explore.provider import (
    DIFF_MODES,
    DiffMode,
    DiffPayload,
    language_hint_for_path,
    load_file_diff,
)
from synapse.ui.git_explore.unified import render_unified_diff

__all__ = [
    "DIFF_MODES",
    "DiffMode",
    "DiffPayload",
    "HAS_DIFF_VIEW",
    "fallback_renderable",
    "language_hint_for_path",
    "load_file_diff",
    "make_diff_view",
    "render_unified_diff",
    "status_line",
]
