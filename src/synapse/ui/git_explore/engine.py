"""Diff engine: textual-diff-view primary, Rich unified fallback."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from synapse.ui.git_explore.provider import DiffPayload
from synapse.ui.git_explore.unified import render_unified_diff

try:
    from textual_diff_view import DiffView as _DiffView

    HAS_DIFF_VIEW = True
except Exception:  # noqa: BLE001
    _DiffView = None  # type: ignore[assignment,misc]
    HAS_DIFF_VIEW = False


def make_diff_view(
    payload: DiffPayload,
    *,
    split: bool = True,
    annotations: bool = True,
    colors: dict[str, str] | None = None,
    widget_id: str = "ge-diff-view",
) -> Any:
    """Build a mountable widget for ``payload``.

    Prefer ``textual_diff_view.DiffView`` when available and content is text.
    Otherwise return a Rich-backed ``Static``-ready renderable wrapped as payload
    metadata for the caller to place in a Static (or a prebuilt Static).
    """
    colors = colors or {}
    if payload.error or payload.binary or not HAS_DIFF_VIEW or _DiffView is None:
        return None

    # Paths are labels only; content comes from strings.
    path_a = f"a/{payload.path}"
    path_b = f"b/{payload.path}"
    if payload.mode == "staged":
        path_a = f"HEAD/{payload.path}"
        path_b = f"index/{payload.path}"
    elif payload.mode == "unstaged":
        path_a = f"index/{payload.path}"
        path_b = f"worktree/{payload.path}"
    else:
        path_a = f"HEAD/{payload.path}"
        path_b = f"worktree/{payload.path}"

    return _DiffView(
        path_a,
        path_b,
        payload.text_a or "",
        payload.text_b or "",
        split=bool(split),
        annotations=bool(annotations),
        auto_split=False,
        wrap=False,
        id=widget_id,
    )


def fallback_renderable(payload: DiffPayload, *, colors: dict[str, str] | None = None) -> Any:
    """Rich Group/Text for cases where DiffView is unavailable or unsuitable."""
    colors = colors or {}
    return render_unified_diff(
        payload,
        color_meta=colors.get("dim", "#9aa0a6"),
        color_hunk=colors.get("hunk", "#8ab4f8"),
        color_add=colors.get("added", "#81c995"),
        color_del=colors.get("deleted", "#f28b82"),
        color_ctx=colors.get("fg", "#e8eaed"),
        color_warn=colors.get("orange", "#f4b183"),
    )


def status_line(*, split: bool, annotations: bool, engine: str) -> Text:
    layout = "split" if split else "unified"
    ann = "ann:on" if annotations else "ann:off"
    return Text(f"{layout} · {ann} · {engine}", style="dim")
