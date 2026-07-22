"""Fallback unified-diff renderer (Rich Text)."""

from __future__ import annotations

import difflib

from rich.console import Group
from rich.text import Text

from synapse.ui.git_explore.provider import DiffPayload


def render_unified_diff(
    payload: DiffPayload,
    *,
    color_meta: str = "#9aa0a6",
    color_hunk: str = "#8ab4f8",
    color_add: str = "#81c995",
    color_del: str = "#f28b82",
    color_ctx: str = "#e8eaed",
    color_warn: str = "#f4b183",
    context: int = 3,
) -> Group | Text:
    """Render a ``DiffPayload`` as colored unified diff lines."""
    if payload.error:
        return Text(payload.error, style=color_warn)
    if payload.binary:
        return Text(f"binary file: {payload.path}", style=color_meta)

    a_lines = (payload.text_a or "").splitlines(keepends=True)
    b_lines = (payload.text_b or "").splitlines(keepends=True)

    if not a_lines and not b_lines:
        if payload.missing_a and payload.missing_b:
            return Text("file not found on either side", style=color_meta)
        return Text("no content", style=color_meta)

    if payload.mode == "staged":
        from_label = f"HEAD/{payload.path}"
        to_label = f"index/{payload.path}"
    elif payload.mode == "unstaged":
        from_label = f"index/{payload.path}"
        to_label = f"worktree/{payload.path}"
    else:
        from_label = f"HEAD/{payload.path}"
        to_label = f"worktree/{payload.path}"

    rows: list[Text] = []
    if payload.truncated:
        rows.append(Text("… truncated for display …", style=color_warn))

    if payload.missing_a and not payload.missing_b:
        rows.append(Text(f"new file · {payload.mode}", style=color_meta))
    elif payload.missing_b and not payload.missing_a:
        rows.append(Text(f"deleted file · {payload.mode}", style=color_meta))
    else:
        rows.append(Text(f"{payload.mode} · {payload.path}", style=color_meta))

    diff_iter = difflib.unified_diff(
        a_lines,
        b_lines,
        fromfile=from_label,
        tofile=to_label,
        n=max(0, int(context)),
        lineterm="",
    )
    produced = False
    for line in diff_iter:
        produced = True
        raw = line.rstrip("\n")
        if raw.startswith("+++") or raw.startswith("---"):
            rows.append(Text(raw, style=color_meta))
        elif raw.startswith("@@"):
            rows.append(Text(raw, style=color_hunk))
        elif raw.startswith("+"):
            rows.append(Text(raw, style=color_add))
        elif raw.startswith("-"):
            rows.append(Text(raw, style=color_del))
        else:
            rows.append(Text(raw, style=color_ctx))

    if not produced:
        rows.append(Text("no differences in this mode", style=color_meta))

    return Group(*rows)
