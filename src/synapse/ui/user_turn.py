"""Pure formatting helpers for user transcript turns."""
from __future__ import annotations

import re

from synapse.ui.topbar import display_width, truncate_to_width

_WS_RE = re.compile(r"\s+")
_USER_PREVIEW_MAX_LINES = 3

def wrap_user_turn_text(
    text: str,
    *,
    width: int,
    max_lines: int | None = _USER_PREVIEW_MAX_LINES,
) -> tuple[list[str], bool]:
    """Word-wrap user prompt for the transcript bar.

    Returns ``(lines, truncated)``. When ``max_lines`` is None, never truncates.
    Prefers breaks at spaces; falls back to display-width chunks (CJK-safe).
    """
    width = max(8, int(width or 8))
    raw = _WS_RE.sub(" ", (text or "").strip())
    if not raw:
        return [""], False

    lines: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        acc = ""
        last_space_acc_len = -1
        j = i
        while j < n:
            ch = raw[j]
            trial = acc + ch
            if display_width(trial) > width:
                break
            acc = trial
            if ch == " ":
                last_space_acc_len = len(acc)
            j += 1
        if not acc:
            # Single character wider than width (rare); force one cell.
            acc = raw[i]
            j = i + 1
        elif j < n and last_space_acc_len > 0:
            # Break at last space inside this line.
            acc = acc[:last_space_acc_len].rstrip()
            j = i + last_space_acc_len
            # skip the space
            if j < n and raw[j] == " ":
                j += 1
        lines.append(acc)
        i = j

    if max_lines is None or len(lines) <= max_lines:
        return lines, False
    kept = list(lines[: max(1, int(max_lines))])
    last = kept[-1]
    kept[-1] = truncate_to_width(last, max(4, width))
    if not kept[-1].endswith("…"):
        kept[-1] = truncate_to_width(kept[-1], max(4, width - 1)).rstrip("…") + "…"
    return kept, True


def format_user_turn_meta(
    *,
    stamp: str,
    turn_index: int | None = None,
    image_count: int = 0,
    expanded: bool = False,
    truncated: bool = False,
) -> str:
    """Right-side meta: optional #n, img count, time; expand hint is separate."""
    bits: list[str] = []
    if turn_index is not None and int(turn_index) > 0:
        bits.append(f"#{int(turn_index)}")
    if image_count and int(image_count) > 0:
        bits.append(f"img×{int(image_count)}")
    if stamp:
        bits.append(stamp)
    return " · ".join(bits)
