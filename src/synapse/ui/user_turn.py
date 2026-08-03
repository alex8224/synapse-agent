"""Pure formatting helpers for user transcript turns."""
from __future__ import annotations

import re

from synapse.ui.topbar import display_width, truncate_to_width

_WS_RE = re.compile(r"\s+")
_USER_PREVIEW_MAX_LINES = 3

# Render cap for user turns: pasted content is display-only trivia, so the
# transcript never paints more than this many characters of a plain message.
RENDER_MAX_CHARS = 250

# When the render source carries ``[...N chars]`` paste placeholders, the text
# around them is kept as-is (user-typed content); only a safety ceiling is
# applied so pathological cases still stay cheap.
RENDER_WITH_PLACEHOLDER_MAX = 4096

_PASTE_PLACEHOLDER_RE = re.compile(r"\.\.\.\s*\d+\+?\s*chars\]")


def has_paste_placeholder(text: str) -> bool:
    """True when ``text`` contains a ``[...N chars]`` paste placeholder."""
    return bool(_PASTE_PLACEHOLDER_RE.search(text or ""))


def compress_paste_placeholder(placeholder: str, *, max_chars: int = RENDER_MAX_CHARS) -> str:
    """Cap the reported size inside a ``[prefix... N chars]`` placeholder.

    The bracket shape and prefix are preserved; only a huge ``N`` is replaced
    with ``max_chars+`` so the label itself never renders a long number.
    Unrecognised input is returned unchanged.
    """
    m = re.match(r"^(.*?\.\.\.\s*)(\d+)(\s*chars\])$", placeholder or "")
    if not m:
        return placeholder
    n = int(m.group(2))
    if n <= max_chars:
        return placeholder
    return f"{m.group(1)}{max_chars}+{m.group(3)}"


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
    early_exit = False
    # Incrementally track the display width of the current line so each
    # character is measured exactly once (avoids O(width^2) per line, which
    # dominates for very large pasted prompts).
    acc_width = 0
    while i < n:
        acc = ""
        acc_width = 0
        last_space_acc_len = -1
        j = i
        while j < n:
            ch = raw[j]
            ch_w = display_width(ch)
            if acc_width + ch_w > width:
                break
            acc += ch
            acc_width += ch_w
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
        # Preview mode: once the line cap is reached there is still more
        # input, so stop wrapping here. The cost becomes O(max_lines * width)
        # instead of O(len(text) * width) for huge pastes.
        if max_lines is not None and len(lines) >= max_lines and i < n:
            early_exit = True
            break

    if max_lines is None or not early_exit:
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
