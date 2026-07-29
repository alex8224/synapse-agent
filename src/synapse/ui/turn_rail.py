"""Pure helpers for the TUI turn-rail minimap."""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")
_RAIL_PREVIEW_MAX = 28
_RAIL_BAR = "───"
_RAIL_BAR_DENSE = "━━━"
_RAIL_BAR_HEAVY = "▓▓▓"

def format_turn_rail_preview(
    text: str,
    *,
    max_len: int = _RAIL_PREVIEW_MAX,
) -> str:
    """Single-line user-turn preview for the right rail (ellipsis when long)."""
    one = _WS_RE.sub(" ", (text or "").strip())
    if not one:
        return "(empty)"
    limit = max(8, int(max_len or _RAIL_PREVIEW_MAX))
    if len(one) > limit:
        return one[: limit - 1].rstrip() + "…"
    return one


def turn_rail_tick_slots(n: int, height: int) -> list[list[int]]:
    """Map ``n`` turns onto ``height`` minimap rows.

    When ``n <= height`` the turns are packed tightly and centered vertically
    so the mouse need not travel far.  When ``n > height`` the rows are filled
    proportionally with bucket merging (same as before).
    """
    h = max(1, int(height or 1))
    n = max(0, int(n or 0))
    slots: list[list[int]] = [[] for _ in range(h)]
    if n <= 0:
        return slots
    if n <= h:
        # Compact, centered placement.
        start = (h - n) // 2
        for i in range(n):
            slots[start + i].append(i)
        return slots
    # n > h — proportional bucket merging.
    for i in range(n):
        y = i * h // n
        y = min(h - 1, max(0, y))
        slots[y].append(i)
    return slots


def format_turn_rail_bucket_label(
    indices: list[int],
    previews: list[str],
    *,
    max_len: int = _RAIL_PREVIEW_MAX,
) -> str:
    """Hover label for a minimap slot (single turn or merged bucket)."""
    if not indices:
        return ""
    if len(indices) == 1:
        return previews[0] if previews else f"#{indices[0] + 1}"
    first = indices[0] + 1
    last = indices[-1] + 1
    head = previews[0] if previews else ""
    prefix = f"#{first}-{last} "
    room = max(6, int(max_len or _RAIL_PREVIEW_MAX) - len(prefix))
    if len(head) > room:
        head = head[: max(0, room - 1)].rstrip() + "…"
    return f"{prefix}{head}" if head else f"#{first}-{last}"
