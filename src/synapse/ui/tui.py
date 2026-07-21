"""Textual TUI — Cursor-style agent transcript with tool timeline.

Layout (Grok/Cursor chrome):
  top:     1-line centered: ≡ path · title · ⎇ branch · in/cache/out + ctx
  user:    accent bar ● prompt (multi-line, click expand) · time
  thought: ◆ Thought for Xs  (Ctrl+E expand)
  tools:   ▾ group header + ◆ per-item labels
  answer:  clean Markdown
  footer:  Worked for Xs.
  status:  [activity…]  model · thinking · mcp   (right chrome always on)
  input:   ● Build anything
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click, Enter, Key, Leave
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from synapse.agent import build_coding_agent
from synapse.input_history import InputHistory
from synapse.multimodal import (
    ImageBank,
    compose_user_content,
    find_placeholders,
    provider_from_settings,
    read_clipboard,
)
from synapse.session_recap import SessionRecapController
from synapse.steer import format_steer_message, get_agent_steer_queue
from synapse.ui.steer_widget import SteerQueueWidget
from synapse.ui.stream import extract_last_ai_text, render_math_in_text, stream_agent
from synapse.ui.timeline import (
    TODO_MARK_ACTIVE,
    TODO_MARK_DONE,
    TODO_MARK_PENDING,
    TodoRow,
    ToolItem,
    is_todo_tool,
    parse_todo_preview_lines,
    summarize_items,
)
from synapse.ui.welcome import WelcomeView

_WS_RE = re.compile(r"\s+")
_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Palette slots — kept as module globals so render paths stay cheap.
# Values track ``synapse.ui.theme.get_theme()`` via ``_sync_theme_colors``.
_C_FG = "#e8eaed"
_C_DIM = "#9aa0a6"
_C_MUTED = "#5f6368"
_C_GREEN = "#81c995"
_C_ORANGE = "#f4b183"
_C_BAR = "#2b2d31"
_C_BG = "#1a1b1e"
_C_TOP = "#121316"
_C_USER = "#8ab4f8"
_C_ERROR = "#f28b82"
_C_BORDER = "#3c4043"
_C_BORDER_FOCUS = "#5f6368"
_CODE_THEME = "monokai"


def _sync_theme_colors(theme: object | None = None) -> None:
    """Copy active theme palette into module-level color slots."""
    global _C_FG, _C_DIM, _C_MUTED, _C_GREEN, _C_ORANGE, _C_BAR, _C_BG, _C_TOP
    global _C_USER, _C_ERROR, _C_BORDER, _C_BORDER_FOCUS, _CODE_THEME
    try:
        from synapse.ui.theme import get_theme

        t = theme or get_theme()
    except Exception:  # noqa: BLE001
        return
    _C_FG = str(getattr(t, "fg", _C_FG))
    _C_DIM = str(getattr(t, "dim", _C_DIM))
    _C_MUTED = str(getattr(t, "muted", _C_MUTED))
    _C_GREEN = str(getattr(t, "green", _C_GREEN))
    _C_ORANGE = str(getattr(t, "orange", _C_ORANGE))
    _C_BAR = str(getattr(t, "bar", _C_BAR))
    _C_BG = str(getattr(t, "bg", _C_BG))
    _C_TOP = str(getattr(t, "top", _C_TOP))
    _C_USER = str(getattr(t, "user", _C_USER))
    _C_ERROR = str(getattr(t, "error", _C_ERROR))
    _C_BORDER = str(getattr(t, "border", _C_BORDER))
    _C_BORDER_FOCUS = str(getattr(t, "border_focus", _C_BORDER_FOCUS))
    _CODE_THEME = str(getattr(t, "code_theme", _CODE_THEME) or "monokai")


try:
    from synapse.ui.theme import on_theme_change

    on_theme_change(_sync_theme_colors)
    _sync_theme_colors()
except Exception:  # noqa: BLE001
    pass

# Shared UI marks (not emoji): keep prefixes consistent across chrome.
_MARK_USER = "●"  # user prompt / input
_MARK_INPUT = "›"  # input box placeholder only
_MARK_THOUGHT = "◆"  # reasoning

_USER_PREVIEW_MAX_LINES = 3
_USER_PREVIEW_MIN_COLS = 20

# Live stream must stay cheap: full-body Text/Markdown re-layout freezes the
# Textual event loop (status can still tick, transcript becomes unusable).
_STREAM_TAIL_LINES = 28
_STREAM_TAIL_CHARS = 3500
_MARKDOWN_MAX_CHARS = 24_000
_STREAM_INTERVAL_SMALL = 0.12
_STREAM_INTERVAL_MED = 0.25
_STREAM_INTERVAL_LARGE = 0.40


def _stamp() -> str:
    return datetime.now().strftime("%I:%M %p").lstrip("0")


_FINISHED_RE = re.compile(r"^finished in ([\d.]+)s\b", re.I)


def format_answer_divider(
    width: int,
    *,
    diamond: str = "◇",
    rule_ratio: float = 0.80,
) -> list[str]:
    """Centered thin rule with a diamond between tools and final answer.

    Returns blank / rule / blank. The rule is shorter than the panel and
    space-padded so the diamond sits in the horizontal center.
    """
    usable = max(28, min(int(width or 56), 200))
    gem = diamond or "◇"
    ratio = min(0.95, max(0.3, float(rule_ratio or 0.80)))
    rule_len = max(21, int(usable * ratio))
    if (rule_len - len(gem)) % 2:
        rule_len += 1
    side = max(4, (rule_len - len(gem)) // 2)
    rule = ("─" * side) + gem + ("─" * side)
    pad = max(0, (usable - len(rule)) // 2)
    line = (" " * pad) + rule
    trail = max(0, usable - len(line))
    if trail:
        line = line + (" " * trail)
    return ["", line, ""]


def format_token_count(n: int) -> str:
    """Compact token count for chrome (14K, 1.2M)."""
    n = max(0, int(n or 0))
    if n < 1000:
        return str(n)
    if n < 10_000:
        s = f"{n / 1000:.1f}K"
        return s.replace(".0K", "K")
    if n < 1_000_000:
        return f"{(n + 500) // 1000}K"
    if n < 10_000_000:
        s = f"{n / 1_000_000:.1f}M"
        return s.replace(".0M", "M")
    return f"{(n + 500_000) // 1_000_000}M"


def display_width(text: str) -> int:
    """Terminal cell width (CJK / fullwidth / emoji count as 2)."""
    total = 0
    for ch in text or "":
        o = ord(ch)
        # Wide: CJK, Hangul, fullwidth forms, common emoji blocks.
        if (
            0x1100 <= o <= 0x115F
            or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF
            or 0xFE10 <= o <= 0xFE19
            or 0xFE30 <= o <= 0xFE6F
            or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6
            or 0x1F300 <= o <= 0x1FAFF
        ):
            total += 2
        else:
            total += 1
    return total


def truncate_to_width(text: str, max_w: int) -> str:
    """Truncate ``text`` so its display width fits ``max_w`` cells."""
    raw = text or ""
    max_w = int(max_w or 0)
    if max_w <= 0:
        return ""
    if display_width(raw) <= max_w:
        return raw
    if max_w == 1:
        return "…"
    out: list[str] = []
    used = 0
    limit = max_w - 1  # room for ellipsis
    for ch in raw:
        cw = display_width(ch)
        if used + cw > limit:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def center_in_width(text: str, width: int) -> str:
    """Pad ``text`` so it appears centered within ``width`` terminal cells."""
    body = truncate_to_width(text or "", width)
    w = display_width(body)
    if w >= width:
        return body
    left = (width - w) // 2
    right = width - w - left
    return (" " * left) + body + (" " * right)


# Wide separator between topbar regions (branch / path / title / usage).
_TOPBAR_REGION_GAP = "   ·   "
# Text prefix for git branch (not emoji; terminal-safe branch mark).
_TOPBAR_BRANCH_MARK = "⎇"  # APL upwards vane / branch mark


def format_usage_label(
    *,
    input_tokens: int = 0,
    cache_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    """Token chrome as compact ``in/cache/out`` counts: ``12K/3K/1.2K``."""
    return (
        f"{format_token_count(input_tokens)}/"
        f"{format_token_count(cache_tokens)}/"
        f"{format_token_count(output_tokens)}"
    )


def format_context_occupancy_label(
    *,
    last_input_tokens: int = 0,
    context_window: int | None = None,
) -> str:
    """Last model-call context fill: ``270K/54%`` (tokens + ratio), or ``270K`` without window.

    Uses the final model invocation's returned input size for the turn — not the
    sum of every call in the loop.
    """
    used = max(0, int(last_input_tokens or 0))
    if used <= 0:
        return ""
    used_s = format_token_count(used)
    window: int | None
    try:
        window = int(context_window) if context_window is not None else None
    except (TypeError, ValueError):
        window = None
    if window is not None and window > 0:
        pct = int(round(100.0 * used / window))
        # Cap display so chrome stays short when usage metadata overshoots.
        if pct > 999:
            pct = 999
        return f"{used_s}/{pct}%"
    return used_s


def format_mcp_status_label(
    *,
    enabled: bool,
    servers: list[str] | None = None,
    tools: list[str] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """MCP chrome: ``mcp on`` / ``mcp off`` / ``mcp err`` (no server/tool counts)."""
    if not enabled:
        return "mcp off"
    servers = list(servers or [])
    tools = list(tools or [])
    warnings = list(warnings or [])
    n_s = len(servers)
    n_t = len(tools)
    if warnings and n_s == 0:
        return "mcp err"
    if n_s == 0 and n_t == 0:
        return "mcp off"
    return "mcp on"


def short_model_name(model: str) -> str:
    from synapse.models_registry import short_model_id

    return short_model_id(model)


def model_status_label(settings: object) -> str:
    """Idle status / subtitle: ``deepseek-v4-pro · high``."""
    from synapse.models_registry import format_model_status

    return format_model_status(settings)


def short_workspace_label(path: Path | str, *, max_len: int = 42) -> str:
    """Prefer last two path segments; ellipsize long absolute paths."""
    pth = Path(path)
    parts = [x for x in pth.parts if x not in {"/", "\\"}]
    if len(parts) >= 2:
        label = f"{parts[-2]}/{parts[-1]}"
    else:
        label = pth.name or str(pth)
    if len(label) <= max_len:
        return label
    return "…" + label[-(max_len - 1):]



def todo_kind_style(kind: str) -> str:
    """Color for a checklist row kind."""
    if kind == "done":
        return _C_GREEN
    if kind == "active":
        return _C_ORANGE
    return _C_DIM


def render_todo_row_texts(
    rows: list[TodoRow],
    *,
    indent: str = "       ",
    max_rows: int = 20,
) -> list[Text]:
    """Render structured todo rows as styled Rich Text lines."""
    out: list[Text] = []
    for row in rows[:max_rows]:
        style = todo_kind_style(row.kind)
        line = Text(f"{indent}{row.mark} ", style=style)
        content_style = _C_MUTED if row.kind == "done" else style
        line.append(row.content, style=content_style)
        out.append(line)
    if len(rows) > max_rows:
        out.append(Text(f"{indent}… +{len(rows) - max_rows} more", style=_C_MUTED))
    return out


def render_todo_checklist_from_preview(
    preview: str | None,
    *,
    indent: str = "       ",
    max_rows: int = 20,
) -> list[Text]:
    """Render a stored checklist preview (new marks + legacy ``[x]``)."""
    rows = parse_todo_preview_lines(preview)
    if not rows:
        return []
    return render_todo_row_texts(rows, indent=indent, max_rows=max_rows)


class TodoChecklist(Static):
    """Dedicated checklist widget for ``write_todos`` plans.

    Tool groups reuse the same render helpers so marks stay consistent.
    Mount this widget when a standalone / sticky plan block is needed.
    """

    def __init__(
        self,
        title: str = "Todos",
        *,
        preview: str | None = None,
        rows: list[TodoRow] | None = None,
    ) -> None:
        super().__init__()
        self.title = title or "Todos"
        self.preview = preview
        self.rows = list(rows or [])
        self._render_block()

    def set_data(
        self,
        *,
        title: str | None = None,
        preview: str | None = None,
        rows: list[TodoRow] | None = None,
    ) -> None:
        if title is not None:
            self.title = title
        if preview is not None:
            self.preview = preview
        if rows is not None:
            self.rows = list(rows)
        self._render_block()

    def _render_block(self) -> None:
        lines: list[Text] = [
            Text(f"  {self.title}", style=f"{_C_DIM} on {_C_BAR}"),
        ]
        rows = self.rows or parse_todo_preview_lines(self.preview)
        body = render_todo_row_texts(rows, indent="    ", max_rows=32)
        if body:
            lines.extend(body)
        else:
            lines.append(Text("    (empty plan)", style=_C_MUTED))
        legend = Text("    ", style=_C_MUTED)
        legend.append(f"{TODO_MARK_DONE} done  ", style=_C_GREEN)
        legend.append(f"{TODO_MARK_ACTIVE} doing  ", style=_C_ORANGE)
        legend.append(f"{TODO_MARK_PENDING} todo", style=_C_DIM)
        lines.append(legend)
        lines.append(Text(""))
        self.update(Group(*lines))


def soften_turn_footer(message: str) -> str:
    """CLI dump → Grok-style soft footer for the transcript."""
    text = (message or "").strip()
    m = _FINISHED_RE.match(text)
    if m:
        return f"Worked for {m.group(1)}s."
    return text


def _git_branch(cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    branch = (proc.stdout or "").strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def stream_tail_preview(
    body: str,
    *,
    max_lines: int = _STREAM_TAIL_LINES,
    max_chars: int = _STREAM_TAIL_CHARS,
) -> str:
    """Return only the newest tail of a growing answer for live preview.

    Rendering the full cumulative body on every token is O(n) layout work and
    freezes the main pane long before the final Markdown commit.
    """
    if not body:
        return ""
    text = body
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
        truncated = True
    if len(text) > max_chars:
        text = text[-max_chars:]
        # Drop a likely partial first line after hard char cut.
        nl = text.find("\n")
        if 0 <= nl < 120:
            text = text[nl + 1 :]
        truncated = True
    if truncated:
        return "…\n" + text.lstrip("\n")
    return text


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
    """Map ``n`` turns onto ``height`` minimap rows (proportional, with buckets).

    Returns a list of length ``height``; each entry is the turn indices (0-based)
    that share that row. Empty rows are ``[]``.
    """
    h = max(1, int(height or 1))
    n = max(0, int(n or 0))
    slots: list[list[int]] = [[] for _ in range(h)]
    if n <= 0:
        return slots
    if n == 1:
        slots[0].append(0)
        return slots
    for i in range(n):
        y = int(round(i * (h - 1) / (n - 1)))
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


class UserTurnBlock(Static):
    """User prompt bar; scroll anchor for the turn rail.

    Visual hierarchy:
    - dim mark + bold body (up to 3 preview lines, width-aware)
    - muted meta right-aligned on first line (time / #n / images)
    - click toggles full text when truncated
    - no left accent stripe (kept clean)
    """

    DEFAULT_CSS = """
    UserTurnBlock {
        width: 1fr;
        height: auto;
        margin: 0 0 1 0;
        padding: 0;
    }
    UserTurnBlock.-expanded {
        /* no-op hook for future styling */
    }
    """

    def __init__(
        self,
        text: str,
        *,
        stamp: str | None = None,
        turn_index: int | None = None,
        image_count: int = 0,
    ) -> None:
        super().__init__()
        self.full_text = text or ""
        self.stamp = stamp or _stamp()
        self.turn_index = turn_index
        self.image_count = int(image_count or 0)
        self.collapsed = True  # preview mode when long
        self._truncated = False
        self._render_block()

    def _rail_overlap_cols(self) -> int:
        """Columns covered by the right turn-rail overlay (above log content).

        #log only pads 6 cols, but #turn-rail is ~34 wide — meta painted at the
        true right edge sits under the rail and looks "missing". Reserve that
        overlap so time lands in the visible red-box zone left of the rail.
        """
        rail_w = 34
        try:
            rail = self.app.query_one("#turn-rail")
            rw = int(getattr(rail.size, "width", 0) or 0)
            if rw > 0:
                rail_w = rw
            else:
                rail_w = int(getattr(TurnRail, "RAIL_WIDTH", 34) or 34)
        except Exception:  # noqa: BLE001
            rail_w = int(getattr(TurnRail, "RAIL_WIDTH", 34) or 34)
        # Keep in sync with #log padding-right (turn-rail column budget).
        log_pad_right = 34
        return max(0, rail_w - log_pad_right)

    def _content_width(self) -> int:
        w = int(getattr(self.size, "width", 0) or 0)
        if w <= 0:
            try:
                w = int(getattr(self.app.size, "width", 0) or 0) - 4
            except Exception:  # noqa: BLE001
                w = 72
        usable = w - self._rail_overlap_cols()
        return max(_USER_PREVIEW_MIN_COLS, usable)

    def _render_block(self) -> None:
        width = self._content_width()
        # Always keep a clock; #n / images optional extras.
        stamp = (self.stamp or _stamp() or "").strip() or _stamp()
        meta = format_user_turn_meta(
            stamp=stamp,
            turn_index=self.turn_index,
            image_count=self.image_count,
        ) or stamp
        mark = f" {_MARK_USER}  "
        mark_w = display_width(mark)
        meta_w = display_width(meta)
        gap = 2
        body_w = max(12, width - mark_w - meta_w - gap)

        if self.collapsed:
            lines, truncated = wrap_user_turn_text(
                self.full_text, width=body_w, max_lines=_USER_PREVIEW_MAX_LINES
            )
            full_lines, _ = wrap_user_turn_text(
                self.full_text, width=body_w, max_lines=None
            )
            self._truncated = truncated or len(full_lines) > _USER_PREVIEW_MAX_LINES
        else:
            lines, _ = wrap_user_turn_text(
                self.full_text, width=body_w, max_lines=None
            )
            full_lines = lines
            self._truncated = len(full_lines) > _USER_PREVIEW_MAX_LINES

        bg = f"on {_C_BAR}"
        rows: list[Text] = []
        for i, ln in enumerate(lines):
            row = Text()
            if i == 0:
                row.append(mark, style=f"{_C_DIM} {bg}")
                ln0 = truncate_to_width(ln, body_w)
                row.append(ln0, style=f"bold {_C_FG} {bg}")
                used = mark_w + display_width(ln0)
                # Exact fill so meta sits in the red-box (visible right edge).
                pad = max(gap, width - used - meta_w)
                row.append(" " * pad, style=bg)
                row.append(meta, style=f"{_C_MUTED} {bg}")
            else:
                row.append(" " * mark_w, style=bg)
                row.append(ln, style=f"bold {_C_FG} {bg}")
                pad = max(0, width - mark_w - display_width(ln))
                if pad:
                    row.append(" " * pad, style=bg)
            rows.append(row)

        if self._truncated:
            hint = Text()
            hint.append(" " * mark_w, style=bg)
            label = "click to expand" if self.collapsed else "click to collapse"
            hint.append(label, style=f"{_C_MUTED} {bg}")
            pad = max(0, width - mark_w - display_width(label))
            if pad:
                hint.append(" " * pad, style=bg)
            rows.append(hint)

        self.update(Group(Text(""), *rows, Text("")))

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._render_block()

    def on_click(self, event: Click) -> None:
        event.stop()
        full_w = max(12, self._content_width() - display_width(f" {_MARK_USER}  ") - 14)
        full_lines, _ = wrap_user_turn_text(
            self.full_text, width=full_w, max_lines=None
        )
        if len(full_lines) <= _USER_PREVIEW_MAX_LINES:
            return
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.remove_class("-expanded")
        else:
            self.add_class("-expanded")
        self._render_block()



class TurnRailGap(Static):
    """Empty minimap row (spacing between proportional ticks)."""

    DEFAULT_CSS = """
    TurnRailGap {
        height: 1;
        width: 1fr;
        padding: 0;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__("")


class TurnRailItem(Static):
    """One minimap slot: single turn or a bucket of several turns.

    Width is fixed by the parent rail; bar and preview are both right-aligned.
    """

    def __init__(
        self,
        indices: list[int],
        previews: list[str],
        targets: list[UserTurnBlock],
    ) -> None:
        super().__init__()
        self.indices = [int(i) for i in indices]
        self.previews = list(previews)
        self.targets = list(targets)
        self._cycle = 0
        if len(self.indices) > 1:
            self.add_class("-dense")
        self._show_bar()

    def _bar_glyph(self) -> str:
        n = len(self.indices)
        if n <= 1:
            return _RAIL_BAR
        if n <= 3:
            return _RAIL_BAR_DENSE
        return _RAIL_BAR_HEAVY

    def _show_bar(self) -> None:
        self.remove_class("-hover")
        style = _C_DIM if len(self.indices) > 1 else _C_MUTED
        self.update(Text(self._bar_glyph(), style=style, justify="right"))

    def _show_preview(self) -> None:
        self.add_class("-hover")
        label = format_turn_rail_bucket_label(self.indices, self.previews)
        self.update(Text(label, style=f"{_C_FG} on {_C_BAR}", justify="right"))

    def on_enter(self, event: Enter) -> None:
        event.stop()
        self._show_preview()

    def on_leave(self, event: Leave) -> None:
        event.stop()
        self._show_bar()

    def on_click(self, event: Click) -> None:
        event.stop()
        if not self.targets:
            return
        # Cycle through bucket members on repeated clicks.
        idx = self._cycle % len(self.targets)
        self._cycle = (self._cycle + 1) % len(self.targets)
        target = self.targets[idx]
        app = self.app
        jump = getattr(app, "jump_to_user_turn", None)
        if callable(jump):
            jump(target)


class TurnRail(Vertical):
    """Right-side minimap: all user turns mapped into a fixed viewport height."""

    # Fixed column budget so hover previews never reflow the rail position.
    RAIL_WIDTH = 34

    DEFAULT_CSS = f"""
    TurnRail {{
        dock: right;
        layer: overlay;
        width: {34};
        min-width: {34};
        max-width: {34};
        height: 1fr;
        max-height: 1fr;
        align: right top;
        padding: 1 1;
        margin: 0 0;
        background: transparent;
        overflow-x: hidden;
        overflow-y: hidden;
        scrollbar-size: 0 0;
    }}
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._turns: list[tuple[str, UserTurnBlock]] = []

    def clear_turns(self) -> None:
        self._turns = []
        self.remove_children()

    def set_turns(self, turns: list[tuple[str, UserTurnBlock]]) -> None:
        self._turns = list(turns or [])
        self.relayout()

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self.relayout()

    def _content_height(self) -> int:
        """Rows available for ticks (widget height minus vertical padding)."""
        h = int(getattr(self.size, "height", 0) or 0)
        # padding: 1 1 → two rows reserved
        inner = h - 2
        if inner >= 1:
            return inner
        # Before first layout, estimate from turn count (cap for sanity).
        n = len(self._turns)
        return max(1, min(n if n else 1, 32))

    def relayout(self) -> None:
        """Rebuild proportional tick rows for the current height."""
        turns = self._turns
        height = self._content_height()
        slots = turn_rail_tick_slots(len(turns), height)
        self.remove_children()
        for indices in slots:
            if not indices:
                self.mount(TurnRailGap())
                continue
            previews = [turns[i][0] for i in indices if 0 <= i < len(turns)]
            targets = [turns[i][1] for i in indices if 0 <= i < len(turns)]
            self.mount(TurnRailItem(indices, previews, targets))


class ThoughtBlock(Static):
    """Thought row in the transcript; supports live streaming then seal."""

    def __init__(self, elapsed_s: float, body: str, *, live: bool = False) -> None:
        self.elapsed_s = elapsed_s
        self.body = body or ""
        self.live = bool(live)
        # Expanded while streaming so the growing body is readable.
        self.collapsed = not self.live
        super().__init__()
        self._render_block()

    def update_live(self, elapsed_s: float, body: str) -> None:
        """Refresh in place while tokens are still arriving."""
        self.live = True
        self.collapsed = False
        self.elapsed_s = max(0.0, float(elapsed_s or 0.0))
        self.body = body or ""
        self._render_block()

    def seal(self, elapsed_s: float, body: str) -> None:
        """Finalize this row as a historical ThoughtBlock (no remount)."""
        self.live = False
        self.elapsed_s = max(0.0, float(elapsed_s or 0.0))
        self.body = body or ""
        self.collapsed = True
        self._render_block()

    def _render_block(self) -> None:
        if self.live:
            lines: list[Text | Any] = [
                Text(
                    f"  {_MARK_THOUGHT}  Thinking… {self.elapsed_s:.1f}s",
                    style=f"italic {_C_DIM}",
                )
            ]
            preview = stream_tail_preview(self.body)
            if preview.strip():
                lines.append(Text(preview, style=_C_DIM))
            lines.append(Text(""))
            self.update(Group(*lines))
            return
        lines = [
            Text(f"  {_MARK_THOUGHT}  Thought for {self.elapsed_s:.1f}s", style=_C_DIM)
        ]
        if self.body:
            if self.collapsed:
                preview = " ".join(self.body.split())
                if len(preview) > 160:
                    preview = preview[:159].rstrip() + "..."
                lines.append(Text(f"  {preview}", style=_C_DIM))
            else:
                from rich.markdown import Markdown as RichMarkdown

                lines.append(RichMarkdown(render_math_in_text(self.body), code_theme=_CODE_THEME))
        lines.append(Text(""))
        self.update(Group(*lines))

    def toggle(self) -> None:
        if not self.body or self.live:
            return
        self.collapsed = not self.collapsed
        self._render_block()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()


class AnswerBlock(Static):
    """Assistant answer row; live plain-text tail, then Markdown seal."""

    DEFAULT_CSS = """
    AnswerBlock {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, body: str = "", *, live: bool = False) -> None:
        self.body = body or ""
        self.live = bool(live)
        super().__init__()
        self._render_block()

    def update_live(self, body: str) -> None:
        self.live = True
        self.body = body or ""
        self._render_block()

    def seal(self, body: str) -> None:
        self.live = False
        self.body = body or ""
        self._render_block()

    def _render_block(self) -> None:
        body = self.body or ""
        if self.live:
            preview = stream_tail_preview(body)
            self.update(Text(preview, style=_C_FG) if preview else Text(""))
            return
        if not body.strip():
            self.update(Text(""))
            return
        if len(body) > _MARKDOWN_MAX_CHARS:
            renderable: Any = Text(body, style=_C_FG)
        else:
            renderable = Markdown(render_math_in_text(body), code_theme=_CODE_THEME)
        self.update(Group(renderable, Text("")))


class AnswerDivider(Static):
    """Centered diamond rule between tool batches and the final answer."""

    DEFAULT_CSS = """
    AnswerDivider {
        width: 1fr;
        height: auto;
        padding: 1 0;
        text-align: center;
    }
    """

    def __init__(self, width: int = 56) -> None:
        super().__init__()
        self._width = max(28, int(width or 56))
        self._render_block()

    def on_mount(self) -> None:
        # Re-measure after layout so the diamond is truly panel-centered.
        self.call_after_refresh(self._recenter)

    def on_resize(self) -> None:
        self._recenter()

    def _recenter(self) -> None:
        w = int(getattr(self.size, "width", 0) or 0)
        if w <= 0:
            w = int(getattr(self.container_size, "width", 0) or 0)
        if w >= 20 and abs(w - self._width) >= 2:
            self._width = w
            self._render_block()

    def _render_block(self) -> None:
        rows = format_answer_divider(self._width)
        self.update(Group(*(Text(row, style=_C_MUTED) for row in rows)))


class ToolGroupBlock(Static):
    """A timeline tool group with in-place collapse and preview updates."""

    # Expanded lists past this size become noise; keep the rest behind a count.
    _MAX_EXPANDED_ROWS = 12
    # Light nesting: summary then one-space-deeper details.
    _HEADER_INDENT = "  "
    _ITEM_INDENT = "   "
    _SUB_ITEM_INDENT = "      "  # deeper indent for nested subagent tools
    _MORE_INDENT = "   "
    _TODO_INDENT = "    "

    def __init__(self, summary: str = "tools") -> None:
        self.summary = summary or "tools"
        self.items: list[ToolItem] = []
        # Expand while tools are running so users see rows as they start.
        # Collapse after the batch finishes (close_tool_group).
        self.collapsed = False
        super().__init__()
        self._render_block()

    def _sync_summary_from_items(self, *, running: bool | None = None) -> None:
        """Keep the group header honest as items accumulate."""
        if not self.items:
            return
        if running is None:
            running = any(it.status == "running" for it in self.items)
        self.summary = summarize_items(self.items, running=running)

    def _render_block(self) -> None:
        mark = "▸" if self.collapsed else "▾"
        hi = self._HEADER_INDENT
        lines: list[Text] = [
            Text(f"{hi}{mark}  {self.summary}", style=f"{_C_DIM} on {_C_BAR}")
        ]
        if not self.collapsed:
            visible = self.items
            overflow = 0
            if len(self.items) > self._MAX_EXPANDED_ROWS:
                visible = self.items[: self._MAX_EXPANDED_ROWS]
                overflow = len(self.items) - self._MAX_EXPANDED_ROWS
            for item in visible:
                if item.error:
                    style = "red"
                    bullet = "✗"
                elif item.status == "running":
                    style = _C_ORANGE
                    bullet = "○"
                else:
                    style = _C_GREEN if item.category == "run" else _C_DIM
                    bullet = "✓"
                label = item.label or item.name
                item_indent = self._SUB_ITEM_INDENT if item.sub else self._ITEM_INDENT
                item_style = _C_MUTED if item.sub else style
                if " " in label and item.category in {"read", "edit", "list"}:
                    head, tail = label.split(" ", 1)
                    row = Text(f"{item_indent}{bullet}  {head} ", style=item_style)
                    row.append(tail, style=_C_MUTED if item.sub else _C_ORANGE)
                    lines.append(row)
                else:
                    lines.append(Text(f"{item_indent}{bullet}  {label}", style=item_style))
                # write_todos: always show checklist under the tool row.
                if is_todo_tool(item.name) or str(item.label or "").startswith("Todos "):
                    checklist = render_todo_checklist_from_preview(
                        item.preview, indent=self._TODO_INDENT
                    )
                    if checklist:
                        lines.extend(checklist)
            if overflow:
                lines.append(
                    Text(f"{self._MORE_INDENT}… and {overflow} more", style=_C_MUTED)
                )
        lines.append(Text(""))
        self.update(Group(*lines))

    def set_summary(self, summary: str) -> None:
        # Once items exist, the header is derived from them.  External
        # partial titles (e.g. only the latest batch: "Read 4 files") must
        # not overwrite the aggregate summary.
        if self.items:
            self._sync_summary_from_items()
        else:
            self.summary = summary or "tools"
        self._render_block()

    def add_item(self, item: ToolItem) -> None:
        for existing in self.items:
            if existing.id == item.id:
                # In-place refresh (label/args completed after early start).
                existing.name = item.name
                existing.category = item.category
                existing.label = item.label
                existing.path = item.path
                existing.status = item.status
                existing.preview = item.preview
                existing.error = item.error
                existing.sub = item.sub
                self._sync_summary_from_items()
                self._render_block()
                return
        self.items.append(item)
        # Never leave a stale header like "Read 4 files" after more tools land.
        self._sync_summary_from_items()
        self._render_block()

    def update_item(
        self,
        item_id: str,
        *,
        label: str | None = None,
        path: str | None = None,
        name: str | None = None,
        category: str | None = None,
        status: str | None = None,
        preview: str | None = None,
        error: bool | None = None,
    ) -> None:
        for it in self.items:
            if it.id != item_id:
                continue
            if label is not None:
                it.label = label
            if path is not None:
                it.path = path
            if name is not None:
                it.name = name
            if category is not None:
                it.category = category
            if status is not None:
                it.status = status
            if preview is not None:
                it.preview = preview
            if error is not None:
                it.error = error
            self._sync_summary_from_items()
            self._render_block()
            return

    def update_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        # Keep payload off the main transcript; status color is enough.
        # Still mark the row finished so "…" flips to "◆" immediately.
        self.update_item(item_id, preview=preview, error=error)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = bool(collapsed)
        self._render_block()

    def toggle(self) -> None:
        self.collapsed = not self.collapsed
        self._render_block()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.toggle()


class TextualStreamSink:
    """StreamSink → Cursor-like transcript via CodingAgentApp.

    Supports enhanced tool-item API (preferred) and legacy bulk API.
    """

    def __init__(self, app: CodingAgentApp) -> None:
        self._app = app
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._open_answer: list[str] = []
        self._open_answer_chars = 0
        self._open_reasoning: list[str] = []
        self._open_reasoning_chars = 0
        self._reasoning_open = False
        self._reasoning_started = 0.0
        self._complete_ids: set[str] = set()
        self._complete_texts: set[str] = set()
        self._last_stream_push = 0.0
        # Base interval; adaptive growth is applied in _stream_interval().
        self._min_stream_interval = _STREAM_INTERVAL_SMALL
        self._last_activity_push = 0.0
        self._min_activity_interval = 0.12
        # Subagent status flashes many nested tool events; queue + delay.
        self._sub_activity_interval = 0.25
        self._pending_activity: tuple[str, str, bool] | None = None
        self._last_sub_detail = ""
        # Enhanced tool group state.
        self._group_items: list[ToolItem] = []
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id: str | None = None
        # Legacy fallback counters.
        self._legacy_pending = 0
        self._legacy_names: list[str] = []
        self._legacy_failed = 0

    def _call(self, method: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self._app, method)
        try:
            self._app.call_from_thread(fn, *args, **kwargs)
        except RuntimeError:
            fn(*args, **kwargs)


    def note_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
    ) -> None:
        """Push per-model-call usage to the app topbar (live)."""
        self._call(
            "apply_turn_usage",
            turn_input=int(turn_input or 0),
            turn_output=int(turn_output or 0),
            turn_cache=int(turn_cache or 0),
            last_input=int(last_input or 0),
            last_output=int(last_output or 0),
            last_cache=int(last_cache or 0),
        )


    @staticmethod
    def _norm(text: str) -> str:
        return _WS_RE.sub(" ", (text or "").strip())

    def _stream_interval(self) -> float:
        """Slow down UI pushes as live text grows to protect the event loop."""
        base = self._min_stream_interval
        n = max(self._open_answer_chars, self._open_reasoning_chars)
        if n >= 12_000:
            return max(base, _STREAM_INTERVAL_LARGE)
        if n >= 3_000:
            return max(base, _STREAM_INTERVAL_MED)
        return base

    def _push_stream(
        self,
        kind: str,
        body: str,
        *,
        force: bool = False,
        elapsed_s: float = 0.0,
    ) -> None:
        now = time.monotonic()
        if not force and (now - self._last_stream_push) < self._stream_interval():
            return
        self._last_stream_push = now
        # Tail-only preview keeps layout cheap; commit seals the full body.
        self._call(
            "set_stream",
            kind,
            stream_tail_preview(body),
            elapsed_s=float(elapsed_s or 0.0),
        )

    def _push_activity(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
        min_interval: float | None = None,
    ) -> None:
        """Rate-limit status messages so token streams cannot flood Textual."""
        now = time.monotonic()
        gap = self._min_activity_interval if min_interval is None else float(min_interval)
        if not force and (now - self._last_activity_push) < gap:
            return
        self._last_activity_push = now
        self._call("set_activity", phase, detail, reset_timer)

    def _flush_pending_activity(self, *, force: bool = False) -> None:
        pending = self._pending_activity
        if pending is None:
            return
        phase, detail, reset_timer = pending
        now = time.monotonic()
        if not force and (now - self._last_activity_push) < self._sub_activity_interval:
            return
        if not force and phase == "subagent" and detail == self._last_sub_detail:
            self._pending_activity = None
            return
        self._pending_activity = None
        if phase == "subagent":
            self._last_sub_detail = detail
        self._last_activity_push = now
        self._call("set_activity", phase, detail, reset_timer)

    def _queue_subagent_activity(
        self,
        detail: str,
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        """Coalesce + delay subagent status so nested tools stay readable."""
        text = " ".join((detail or "").split()).strip()
        if not text or text.startswith("ns="):
            text = self._last_sub_detail or "子代理运行中"
        noise = {
            "streaming nested tokens",
            "waiting for model",
        }
        # Heartbeat noise keeps sticky intent if we already have one.
        if text in noise and self._last_sub_detail:
            text = self._last_sub_detail
        elif text in noise:
            text = "子代理运行中"
        self._pending_activity = ("subagent", text, reset_timer)
        now = time.monotonic()
        due = (now - self._last_activity_push) >= self._sub_activity_interval
        if force or due or not self._last_sub_detail:
            self._flush_pending_activity(force=True)

    # -- activity --------------------------------------------------------

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._pending_activity = None
        if phase == "subagent":
            self._last_sub_detail = " ".join((detail or "").split()).strip()
        self._call("set_activity", phase, detail, True)
        self._last_activity_push = time.monotonic()

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        if phase == "subagent":
            self._queue_subagent_activity(detail, reset_timer=reset_timer, force=force)
            return
        if force:
            self._flush_pending_activity(force=True)
        else:
            self._pending_activity = None
        self._push_activity(phase, detail, reset_timer=reset_timer, force=force)

    def activity_stop(self) -> None:
        self._pending_activity = None
        self._last_sub_detail = ""
        self._call("clear_stream")
        self._call("set_activity", "idle", "ready", True)
        self._last_activity_push = time.monotonic()

    # -- reasoning -------------------------------------------------------

    def write_reasoning(self, text: str) -> None:
        if not text:
            return
        # New thought after a completed tool batch must not append into tools.
        # Never seal a still-running group (e.g. parent task/subagent).
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        if not self._reasoning_open:
            self._reasoning_started = time.monotonic()
            self._reasoning_open = True
            self._open_reasoning.clear()
            self._open_reasoning_chars = 0
        self.streamed_reasoning = True
        self._open_reasoning.append(text)
        self._open_reasoning_chars += len(text)
        self.reasoning_buf.append(text)
        elapsed = max(0.0, time.monotonic() - self._reasoning_started)
        # Live preview mounts in #log via set_stream (rate-limit + tail).
        now = time.monotonic()
        if (now - self._last_stream_push) >= self._stream_interval():
            body = "".join(self._open_reasoning)
            self._push_stream("reasoning", body, force=True, elapsed_s=elapsed)
        self._push_activity("thinking", f"{elapsed:.1f}s")

    def close_reasoning(self) -> None:
        if not self._reasoning_open:
            return
        body = "".join(self._open_reasoning).strip()
        self._open_reasoning.clear()
        self._open_reasoning_chars = 0
        self._reasoning_open = False
        elapsed = (
            max(0.0, time.monotonic() - self._reasoning_started)
            if self._reasoning_started
            else 0.0
        )
        # Seal the in-log ThoughtBlock; avoid clear before commit.
        if body:
            self._call("commit_thought", elapsed, body)
        else:
            self._call("clear_stream")

    # -- answer ----------------------------------------------------------

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        if not text:
            return
        if msg_id and msg_id in self._complete_ids:
            return
        # Agent loop: thought → (optional answer) → tools → …  Seal thought first.
        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        self.streamed_answer = True
        self._open_answer.append(text)
        self._open_answer_chars += len(text)
        # Join only when the rate limiter actually allows a UI push.  Building
        # the full string on every token is O(n^2) CPU before layout even runs.
        now = time.monotonic()
        if (now - self._last_stream_push) >= self._stream_interval():
            body = "".join(self._open_answer)
            self._push_stream("answer", body, force=True)
        self._push_activity("writing", f"{self._open_answer_chars}c")

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        body = (text or "").strip()
        if not body:
            return
        key = self._norm(body)
        if msg_id and msg_id in self._complete_ids:
            return
        if key in self._complete_texts:
            return
        # Intermediate assistant messages also sit between thought/tools rounds.
        self.close_reasoning()
        if self._group_open and self._group_header_written:
            if not any(it.status == "running" for it in self._group_items):
                self._finalize_open_group()
        if msg_id:
            self._complete_ids.add(msg_id)
        self._complete_texts.add(key)
        self.streamed_answer = True
        self._open_answer.clear()
        self._open_answer_chars = 0
        self.answer_buf.append(body)
        # Seal the in-log AnswerBlock mounted by set_stream.
        self._call("commit_answer", body)

    def finalize_line(self) -> None:
        self.close_reasoning()
        if self._open_answer:
            body = "".join(self._open_answer).strip()
            self._open_answer.clear()
            self._open_answer_chars = 0
            if body:
                key = self._norm(body)
                if key not in self._complete_texts:
                    self._complete_texts.add(key)
                    self.answer_buf.append(body)
                    self.streamed_answer = True
                    self._call("commit_answer", body)
            else:
                self._call("clear_stream")

    # -- tools: enhanced item API ----------------------------------------

    def _finalize_open_group(self, *, force: bool = False) -> None:
        """Seal the current visual tool group and release sink state."""
        if not self._group_open:
            return
        if not force and any(it.status == "running" for it in self._group_items):
            return
        if self._group_items:
            header = summarize_items(self._group_items, running=False)
            failed = sum(1 for it in self._group_items if it.error)
            if failed:
                header = f"{header}  ({failed} failed)"
            self._call("update_tool_group_header", header)
        if self._group_header_written:
            self._call("close_tool_group")
        self._group_items.clear()
        self._group_open = False
        self._group_header_written = False
        self._expanded_item_id = None

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        """Open a tool group shell; item API fills details right after."""
        del parallel
        self._call("clear_stream")
        # One stream batch == one visual group.  If a previous batch was not
        # closed cleanly, seal it before starting the next header.
        if self._group_open and self._group_items:
            self._finalize_open_group()
        self._group_items.clear()
        self._group_open = True
        self._group_header_written = False
        self._expanded_item_id = None
        self._legacy_pending = len(calls)
        self._legacy_failed = 0
        from synapse.ui.stream import _tool_call_name
        from synapse.ui.timeline import summarize_categories

        self._legacy_names = [_tool_call_name(c) for c in calls]
        summary = summarize_categories(self._legacy_names, running=True)
        self._call("set_activity", "tools", summary, False)

    def tool_item_started(self, item: ToolItem) -> None:
        if not self._group_open:
            self._group_open = True
            self._group_header_written = False
            self._group_items.clear()
            self._expanded_item_id = None
        # Replace same-id early item if args/label improved.
        replaced = False
        for i, existing in enumerate(self._group_items):
            if existing.id == item.id:
                self._group_items[i] = item
                replaced = True
                break
        if not replaced:
            self._group_items.append(item)
        if not self._group_header_written:
            header = summarize_items(self._group_items, running=True)
            # Expand while running so new rows are visible immediately.
            self._call("write_tool_group_header", header, collapsed=False)
            self._group_header_written = True
        else:
            # Refresh header counts as items arrive.
            header = summarize_items(self._group_items, running=True)
            self._call("update_tool_group_header", header)
        self._call("write_tool_item", item)
        self._call("set_activity", "tools", item.label, False)

    def tool_item_updated(self, item: ToolItem) -> None:
        """Refresh label/path after streaming args complete."""
        for i, existing in enumerate(self._group_items):
            if existing.id == item.id:
                self._group_items[i] = item
                break
        header = summarize_items(self._group_items, running=True)
        self._call("update_tool_group_header", header)
        self._call("write_tool_item", item)
        self._call("set_activity", "tools", item.label, False)

    def tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None:
        for it in self._group_items:
            if it.id != item_id:
                continue
            it.status = "error" if error else "ok"
            it.error = error
            # Never wipe a rich todo checklist with a bland tool-result string.
            if preview is not None:
                if is_todo_tool(it.name) and it.preview:
                    # Prefer existing structured checklist if the new preview is weaker.
                    old_rows = parse_todo_preview_lines(it.preview)
                    new_rows = parse_todo_preview_lines(preview)
                    if old_rows and len(new_rows) < len(old_rows):
                        preview = it.preview
                    else:
                        it.preview = preview
                else:
                    it.preview = preview
            break
        # Flip running glyph immediately; keep non-todo payload off transcript.
        self._call(
            "update_tool_item",
            item_id,
            status=("error" if error else "ok"),
            error=error,
            preview=preview,
        )

        still = sum(1 for it in self._group_items if it.status == "running")
        header = summarize_items(self._group_items, running=still > 0)
        self._call("update_tool_group_header", header)
        if still == 0:
            self._call("set_activity", "tools", header, False)

    def tool_group_closed(self, group_id: str) -> None:
        """Close one stream tool batch as its own visual group."""
        del group_id
        self._finalize_open_group(force=True)

    def turn_finished(self) -> None:
        """Seal any leftover open group at end of one turn."""
        self._finalize_open_group(force=True)

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        """Legacy bulk API — used when item events are not emitted."""
        # Nested subagent traffic never paints its own parent timeline groups.
        if sub:
            # Prefer short human status; avoid dumping raw result previews.
            detail = (name or "tool").strip()
            st = (status or "").strip()
            if st.lower().startswith("error"):
                detail = f"{detail} 失败"
            self._queue_subagent_activity(detail, force=True)
            return
        # If we already have items, legacy results are no-ops (item API handles).
        if self._group_items:
            return
        # No open legacy batch → do not invent empty "0 tools" groups.
        if not self._legacy_names and self._legacy_pending <= 0:
            return
        self._legacy_pending = max(0, self._legacy_pending - 1)
        if status.lower().startswith("error"):
            self._legacy_failed += 1
            self._call("append_event", f"✗ {name}  {status}", "red")
        if self._legacy_pending > 0:
            from synapse.ui.timeline import summarize_categories

            live = summarize_categories(self._legacy_names, running=True)
            self._call("set_activity", "tools", live, False)
            return
        from synapse.ui.timeline import summarize_categories

        if not self._legacy_names:
            self._legacy_failed = 0
            return
        summary = summarize_categories(self._legacy_names, running=False)
        if self._legacy_failed:
            summary = f"{summary}  ({self._legacy_failed} failed)"
        self._call("write_tool_group_header", summary, collapsed=True)
        self._call("close_tool_group")
        self._legacy_names.clear()
        self._legacy_failed = 0

    def info(self, message: str) -> None:
        self._call("append_meta", message)


class CodingAgentApp(App[None]):
    """Cursor-like agent transcript."""

    CSS = """
    Screen {
        layout: vertical;
        background: $theme-bg;
        color: $theme-fg;
    }
    #topbar {
        height: 1;
        padding: 0 1;
        color: $theme-dim;
        background: $theme-top;
    }
    #main {
        height: 1fr;
        layout: vertical;
        background: $theme-bg;
        padding: 0 1;
        overflow-y: hidden;
    }
    WelcomeView {
        display: none;
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        content-align: center middle;
        text-align: center;
        background: $theme-bg;
    }
    #main.welcome WelcomeView {
        display: block;
    }
    #main.welcome #log,
    #main.welcome #turn-rail,
    #main.welcome #stream {
        display: none;
    }
    #log {
        width: 1fr;
        height: 1fr;
        background: $theme-bg;
        color: $theme-fg;
        padding: 0 1;
        /* Match #turn-rail width so meta/time is not painted under the overlay. */
        padding-right: 34;
        /* Hide chrome; wheel / keys / programmatic scroll still work. */
        scrollbar-size: 0 0;
        scrollbar-background: $theme-bg;
        scrollbar-color: $theme-bg;
    }
    #turn-rail {
        dock: right;
        layer: overlay;
        width: 34;
        min-width: 34;
        max-width: 34;
        height: 1fr;
        background: transparent;
        scrollbar-size: 0 0;
        overflow-y: hidden;
    }
    #stream {
        /* Legacy fixed slot — live text now mounts in #log in place.
           Keep the node for compat but never reserve vertical space. */
        display: none;
        height: 0;
        max-height: 0;
        padding: 0;
        overflow-y: hidden;
    }
    #stream.active {
        display: none;
    }
    /* Single bottom stack: Textual multi-dock bottom does NOT stack (overlaps). */
    #bottom-chrome {
        dock: bottom;
        height: auto;
        layout: vertical;
        background: $theme-bg;
    }
    #status {
        height: 1;
        padding: 0 2;
        color: $theme-muted;
        background: $theme-bg;
    }
    #status.busy {
        color: $theme-green;
    }
    #steer-queue {
        height: auto;
        max-height: 10;
        margin: 0 1;
    }
    #complete-hint {
        height: auto;
        padding: 0 2;
        color: $theme-muted;
        background: $theme-bg;
    }
    #prompt {
        background: $theme-bg;
        color: $theme-fg;
        border: tall $theme-border;
        padding: 0 1;
        margin: 0 1 1 1;
        height: 3;
    }
    #prompt:focus {
        border: tall $theme-border-focus;
    }
    /* Must be in the app stylesheet: widget DEFAULT_CSS is parsed separately
       and cannot resolve the app's $theme-* variables. */
    TurnRailItem {
        height: 1;
        width: 1fr;
        color: $theme-muted;
        padding: 0 0;
        margin: 0 0 0 0;
        content-align: right middle;
        text-align: right;
    }
    TurnRailItem.-hover {
        color: $theme-fg;
    }
    TurnRailItem.-dense {
        color: $theme-dim;
    }
    AnswerDivider {
        color: $theme-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_log", "Clear", show=False),
        Binding("ctrl+e", "toggle_last_thought", "Expand thought", show=False),
        Binding("ctrl+t", "toggle_last_tools", "Toggle tools", show=False),
        Binding("alt+v", "clipboard_paste", "Paste image", show=False, priority=True),
        # priority: capture ESC even while the prompt Input has focus
        Binding("escape", "cancel_run", "Cancel", show=False, priority=True),
        Binding("up", "history_up", "HistoryUp", show=False, priority=True),
        Binding("down", "history_down", "HistoryDown", show=False, priority=True),
        # Dialog shortcuts (F-keys)
        Binding("f2", "dialog_model", "Model", show=False),
        Binding("f3", "dialog_theme", "Theme", show=False),
        Binding("f4", "dialog_sessions", "Sessions", show=False),
        Binding("f5", "dialog_mcp", "MCP", show=False),
        Binding("f6", "dialog_safety", "Safety", show=False),
    ]

    def action_dialog_model(self) -> None:
        self._open_model_dialog([])

    def action_dialog_theme(self) -> None:
        self._open_theme_dialog()

    def action_dialog_sessions(self) -> None:
        self._open_session_dialog(["switch"])

    def action_dialog_mcp(self) -> None:
        self._open_mcp_dialog()

    def action_dialog_safety(self) -> None:
        self._open_safety_dialog()

    def get_css_variables(self) -> dict[str, str]:
        """Merge Textual defaults with the active theme's ``$theme-*`` palette."""
        variables = super().get_css_variables()
        try:
            from synapse.ui.theme import get_theme

            return {**variables, **get_theme().css_variables()}
        except Exception:  # noqa: BLE001
            return variables

    def __init__(
        self,
        *,
        agent: Any,
        settings: Any,
        thread_id: str,
        env_path: Path | None = None,
        project_root: Path | None = None,
        defer_agent_build: bool = False,
    ) -> None:
        super().__init__()
        self.agent = agent
        self.settings = settings
        self.thread_id = thread_id
        self.env_path = env_path
        self.project_root = project_root or Path.cwd()
        self._defer_agent_build = bool(defer_agent_build and agent is None)
        self._agent_ready = threading.Event()
        self._agent_error: str | None = None
        self._mcp_attaching = False
        self._image_bank = ImageBank()
        # 粘贴截断映射: {占位符: 完整原始文本}
        self._paste_replacements: dict[str, str] = {}
        # 补全下拉菜单当前高亮行索引
        self._complete_active_idx = 0
        # 当前补全会话的基准值（用户原始输入，用于 Tab 循环）
        self._complete_base_value = ""
        if agent is not None:
            self._agent_ready.set()
        self._busy = False
        self._cancel_event = threading.Event()
        self._phase = "idle"
        self._detail = "ready" if agent is not None else "starting"
        self._activity_started = time.monotonic()
        self._spin_i = 0
        self._steer_items: list[str] = []
        self._steer_last_count = 0
        self._steer_listener_bound = False
        self._last_thought_body = ""
        self._last_thought_elapsed = 0.0
        self._thought_expanded = False
        self._last_tool_items: list[ToolItem] = []
        self._last_tool_summary = ""
        self._last_answer_text = ""
        self._live_tool_items: list[ToolItem] = []
        self._live_tool_summary = ""
        self._thought_blocks: list[ThoughtBlock] = []
        self._tool_blocks: list[ToolGroupBlock] = []
        self._live_tool_block: ToolGroupBlock | None = None
        # In-timeline live stream (reasoning / answer), like tool groups.
        self._live_stream_block: ThoughtBlock | AnswerBlock | None = None
        self._live_stream_kind: str | None = None
        self._user_turns: list[UserTurnBlock] = []
        self._in_tool_rail = False
        # After tools run, next final answer gets a ◇ divider above it.
        self._pending_answer_divider = False
        self._session_recap = SessionRecapController(
            enabled=bool(getattr(settings, "session_recap_enabled", True)),
            idle_seconds=float(
                getattr(settings, "session_recap_idle_seconds", 180.0) or 180.0
            ),
            min_turns=int(getattr(settings, "session_recap_min_turns", 3) or 3),
        )
        self._context_tokens = 0
        self._last_out_tokens = 0
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        # Snapshot before a live turn so mid-turn updates stay absolute.
        self._usage_base_input = 0
        self._usage_base_output = 0
        self._usage_base_cache = 0
        self._session_title = ""
        self._complete_applied: str | None = None
        self._complete_cands: list[str] = []
        ws = Path(getattr(settings, "workspace", Path.cwd()) or Path.cwd())
        self._git_branch = _git_branch(ws)
        hist_root = Path(project_root or ws)
        self._input_history = InputHistory.for_project(hist_root)
        self.title = "Synapse"
        self.sub_title = model_status_label(settings)
        self._reload_session_title()

    def _slash_complete_ctx(self):
        from synapse.slash_complete import build_complete_context

        return build_complete_context(self.settings)

    def compose(self) -> ComposeResult:
        from synapse.slash_complete import make_textual_suggester

        yield Static(id="topbar")
        with Vertical(id="main", classes="welcome"):
            yield WelcomeView(self.project_root, id="welcome")
            yield VerticalScroll(id="log")
            # Floating overlay: hover previews must not reflow the transcript.
            yield TurnRail(id="turn-rail")
            yield Static(id="stream")
        with Vertical(id="bottom-chrome"):
            yield SteerQueueWidget(id="steer-queue")
            yield Static("", id="status")
            yield Static("", id="complete-hint")
            yield Input(
                placeholder=f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)",
                id="prompt",
                suggester=make_textual_suggester(
                    self._slash_complete_ctx,
                    workspace=self.project_root,
                ),
            )

    def on_mount(self) -> None:
        # Apply configured theme before first paint of chrome widgets.
        try:
            self.apply_theme(
                getattr(self.settings, "theme", None),
                persist=False,
                announce=False,
            )
        except Exception:  # noqa: BLE001
            pass
        self._refresh_topbar()
        self.set_interval(0.1, self._tick_status)
        log = self.query_one("#log", VerticalScroll)
        # Hide scrollbar chrome; mouse-wheel / keys / scroll_* still work.
        log.show_vertical_scrollbar = False
        log.show_horizontal_scrollbar = False
        self.query_one("#prompt", Input).focus()
        if self._defer_agent_build or self.agent is None:
            self.set_activity("starting", "loading agent…", True)
            self.append_event("starting agent in background…", "dim")
            self._bg_build_agent()
        else:
            self.call_after_refresh(self._restore_session_transcript)

    @work(thread=True, exclusive=True, group="startup")
    def _bg_build_agent(self) -> None:
        """Build agent off the UI thread; attach MCP in a second phase."""
        from synapse.agent import attach_mcp_to_agent, build_coding_agent

        try:
            self.call_from_thread(
                self.set_activity, "starting", "build model/backend…", True
            )
            agent = build_coding_agent(
                self.settings,
                project_root=self.project_root,
                load_mcp=False,
            )
            self.agent = agent
            self._agent_ready.set()
            self.call_from_thread(self._on_agent_ready, False)
        except Exception as exc:  # noqa: BLE001
            self._agent_error = str(exc)
            self._agent_ready.set()
            self.call_from_thread(
                self.append_event,
                f"agent start failed: {exc}",
                "bold red",
            )
            self.call_from_thread(self.set_activity, "idle", "agent failed", True)
            return

        if not bool(getattr(self.settings, "enable_mcp", True)):
            return
        if getattr(agent, "_coding_mcp_attached", False):
            return
        try:
            self._mcp_attaching = True
            self.call_from_thread(
                self.set_activity, "starting", "connecting MCP…", False
            )
            agent2 = attach_mcp_to_agent(
                self.settings,
                agent,
                project_root=self.project_root,
            )
            if self.agent is not agent:
                # Agent was replaced while connecting (e.g. /model switch).
                # Do not clobber the new agent; pool now exists, so a later
                # rebuild reuses MCP tools without reconnecting.
                self.call_from_thread(
                    self.append_event,
                    "MCP connected; current agent unchanged (tools apply on next rebuild)",
                    "dim",
                )
                return
            self.agent = agent2
            if not self._busy:
                self.call_from_thread(self._on_mcp_attached)
            else:
                self.call_from_thread(
                    self.append_event,
                    "MCP tools attached (will apply next turn)",
                    "dim",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.append_event,
                f"MCP attach failed (agent still usable): {exc}",
                "yellow",
            )
        finally:
            self._mcp_attaching = False
            if not self._busy:
                self.call_from_thread(self.set_activity, "idle", "ready", True)

    def _on_agent_ready(self, with_mcp: bool) -> None:
        label = "agent ready" + (" + MCP" if with_mcp else " (MCP pending)")
        self.append_event(label, "dim")
        self.set_activity("idle", "ready", True)
        self._steer_listener_bound = False
        self._bind_steer_queue()
        self._restore_session_transcript(announce=True)

    def _on_mcp_attached(self) -> None:
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        self.append_event(
            f"MCP ready: servers={servers or '-'} tools={len(tools)}",
            "dim",
        )
        for w in warnings:
            self.append_event(f"mcp: {w}", "yellow")
        self.set_activity("idle", "ready", True)

    def _set_complete_hint(self, value: str) -> None:
        from synapse.slash_complete import (
            complete_at_line,
            complete_slash,
        )

        hint = self.query_one("#complete-hint", Static)

        # ---- 先更新补全会话基准值 ----
        # 防御：value 有 @ 但 base_value 没有 → 强制更新
        if self.project_root and "@" in value and "@" not in (self._complete_base_value or ""):
            self._complete_base_value = value
            self._complete_active_idx = 0
        elif not self._complete_base_value:
            # 新会话
            self._complete_base_value = value
            self._complete_active_idx = 0
        elif self._complete_applied and self._complete_applied == value:
            # 补全已应用 → 保持 base_value 不变（Tab / 箭头导航中）
            pass
        elif value.startswith(self._complete_base_value):
            # 用户继续输入更多字符 → 更新 base_value 缩小匹配范围
            self._complete_base_value = value
            self._complete_active_idx = 0
        else:
            # 用户改变了前缀方向 → 重置
            self._complete_base_value = value
            self._complete_active_idx = 0

        # ---- 基于 base_value 计算候选列表 ----
        cands: list[str] = []
        if value.startswith("/"):
            cands = complete_slash(self._complete_base_value or value, self._slash_complete_ctx())
        elif self.project_root and "@" in value:
            cands = complete_at_line(self._complete_base_value or value, self.project_root)

        if not cands:
            hint.update("")
            self._complete_cands = []
            return

        # 多行下拉菜单渲染（最多 6 行）
        max_rows = 6
        active = self._complete_active_idx
        # 确保 active 在有效范围内
        if active >= len(cands):
            active = len(cands) - 1
        # 滚动窗口：尽量让 active 行保持可见
        if active >= max_rows:
            window_start = active - max_rows + 1
            shown = cands[window_start : window_start + max_rows]
            offset = window_start
        else:
            shown = cands[:max_rows]
            offset = 0

        lines: list[str] = []
        for i, c in enumerate(shown):
            idx = offset + i
            # 提取 @/command 尾部用于紧凑显示
            if "@" in c:
                at_pos = c.rfind("@")
                tail = c[at_pos:]
            elif c.startswith("/"):
                tail = c
            else:
                tail = c
            if idx == active:
                lines.append(f"[bold reverse] {tail} [/]")
            else:
                lines.append(f"  {tail}")
        if len(cands) > offset + max_rows:
            lines.append(f"  [dim]...+{len(cands) - offset - max_rows} more[/]")
        elif offset > 0:
            lines.append(f"  [dim]...+{offset} above[/]")
        hint.update("\n".join(lines))

    def _apply_completion(self, line: str) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = line
        prompt.cursor_position = len(line)
        self._complete_applied = line
        self._set_complete_hint(line)

    def action_complete_slash(self) -> None:
        """Accept / cycle slash completions (Tab)."""
        from synapse.slash_complete import (
            complete_at_line,
            complete_slash,
            cycle_at_completion,
            cycle_completion,
        )

        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return
        value = prompt.value or ""

        # --- @ path completion ---
        if self.project_root and "@" in value:
            # ghost 首次接受
            ghost = getattr(prompt, "_suggestion", "") or ""
            if (
                not self._complete_applied
                and ghost
                and ghost != value
                and "@" in ghost
            ):
                cands = complete_at_line(self._complete_base_value or value, self.project_root)
                self._complete_active_idx = 0
                self._apply_completion_candidate(cands, 0)
                return

            # 循环候选
            cands = self._current_completion_cands()
            if cands:
                nxt_idx = (self._complete_active_idx + 1) % len(cands)
                self._apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion ---
        if not value.startswith("/"):
            return
        ctx = self._slash_complete_ctx()

        # ghost 首次接受
        ghost = getattr(prompt, "_suggestion", "") or ""
        if (
            not self._complete_applied
            and ghost
            and ghost.casefold().startswith(value.casefold())
            and ghost != value
        ):
            cands = complete_slash(self._complete_base_value or value, ctx)
            self._complete_active_idx = 0
            self._apply_completion_candidate(cands, 0)
            return

        # 循环候选
        cands = self._current_completion_cands()
        if cands:
            nxt_idx = (self._complete_active_idx + 1) % len(cands)
            self._apply_completion_candidate(cands, nxt_idx)

    def action_complete_slash_prev(self) -> None:
        """Cycle slash completions backwards (Shift+Tab)."""
        from synapse.slash_complete import complete_at_line, complete_slash

        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return
        value = prompt.value or ""

        # --- @ path completion (prev) ---
        if self.project_root and "@" in value:
            cands = self._current_completion_cands()
            if cands:
                nxt_idx = (self._complete_active_idx - 1) % len(cands)
                self._apply_completion_candidate(cands, nxt_idx)
            return

        # --- / command completion (prev) ---
        cands = self._current_completion_cands()
        if cands:
            nxt_idx = (self._complete_active_idx - 1) % len(cands)
            self._apply_completion_candidate(cands, nxt_idx)

    # ------------------------------------------------------------------
    # Intercept Tab/Shift+Tab to run completion before focus switching
    # ------------------------------------------------------------------

    def action_focus_next(self) -> None:
        """Tab: run completion for @/slash, or focus next widget."""
        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus:
            value = prompt.value or ""
            if self.project_root and "@" in value:
                self.action_complete_slash()
                return
            if value.startswith("/"):
                self.action_complete_slash()
                return
        self.screen.focus_next()

    def action_focus_previous(self) -> None:
        """Shift+Tab: run completion (prev) for @/slash, or focus previous widget."""
        prompt = self.query_one("#prompt", Input)
        if prompt.has_focus:
            value = prompt.value or ""
            if self.project_root and "@" in value:
                self.action_complete_slash_prev()
                return
            if value.startswith("/"):
                self.action_complete_slash_prev()
                return
        self.screen.focus_previous()

    def action_show_completions(self) -> None:
        """List available slash completions (Ctrl+Space)."""
        from synapse.slash_complete import complete_slash

        prompt = self.query_one("#prompt", Input)
        value = prompt.value or ""
        if not value.startswith("/"):
            self.append_event("type / to start a slash command", "dim")
            return
        cands = complete_slash(value, self._slash_complete_ctx())
        if not cands and " " in value.rstrip():
            parent = value.rstrip().rsplit(" ", 1)[0] + " "
            cands = complete_slash(parent, self._slash_complete_ctx())
        if not cands:
            self.append_event("no completions", "yellow")
            return
        self.append_event("completions:", "dim")
        for c in cands[:20]:
            mark = "*" if c == value else " "
            self.append_event(f" {mark} {c}", "dim")
        if len(cands) > 20:
            self.append_event(f"  ... +{len(cands) - 20} more", "dim")

    def _set_prompt_value(self, text: str) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.value = text
        prompt.cursor_position = len(text)
        self._set_complete_hint(text)

    def action_history_up(self) -> None:
        """Recall older project input history / navigate completion (up)."""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if self._complete_base_value:
            cands = self._current_completion_cands()
            if cands:
                self._complete_active_idx = (
                    self._complete_active_idx - 1
                    if self._complete_active_idx > 0
                    else len(cands) - 1
                )
                self._apply_completion_candidate(cands, self._complete_active_idx)
                return

        nxt = self._input_history.up(prompt.value or "")
        if nxt is not None:
            self._set_prompt_value(nxt)

    def action_history_down(self) -> None:
        """Recall newer project input history / navigate completion (down)."""
        if isinstance(self.screen, ModalScreen):
            return
        prompt = self.query_one("#prompt", Input)
        if not prompt.has_focus:
            return

        # 补全菜单活跃时：将 up/down 重定向为菜单导航
        if self._complete_base_value:
            cands = self._current_completion_cands()
            if cands:
                self._complete_active_idx = (
                    self._complete_active_idx + 1
                ) % len(cands)
                self._apply_completion_candidate(cands, self._complete_active_idx)
                return

        nxt = self._input_history.down(prompt.value or "")
        if nxt is not None:
            self._set_prompt_value(nxt)

    def _current_completion_cands(self) -> list[str]:
        """Return candidates for the active completion session (always  based on _complete_base_value)."""
        from synapse.slash_complete import complete_at_line, complete_slash

        if self.project_root and "@" in (self._complete_base_value or ""):
            return complete_at_line(self._complete_base_value, self.project_root)
        if (self._complete_base_value or "").startswith("/"):
            ctx = self._slash_complete_ctx()
            base = self._complete_base_value
            cands = complete_slash(base, ctx)
            if len(cands) <= 1 and " " in base.rstrip():
                cands = complete_slash(base.rstrip().rsplit(" ", 1)[0] + " ", ctx)
            return cands
        return []

    def _apply_completion_candidate(self, cands: list[str], idx: int) -> None:
        """Apply the candidate at *idx* and refresh the dropdown."""
        if not cands:
            return
        prompt = self.query_one("#prompt", Input)
        nxt = cands[idx % len(cands)]
        self._complete_cands = cands
        self._complete_active_idx = idx
        prompt.value = nxt
        prompt.cursor_position = len(nxt)
        self._complete_applied = nxt
        self._set_complete_hint(nxt)

    @on(Input.Changed, "#prompt")
    def handle_prompt_changed(self, event: Input.Changed) -> None:
        value = event.value or ""
        # 清理已失效的粘贴占位符映射（用户编辑后占位符被破坏）
        if self._paste_replacements:
            stale = [p for p in self._paste_replacements if p not in value]
            for p in stale:
                del self._paste_replacements[p]
        # 清理 / 命令补全状态（但不影响 @ 补全会话）
        in_at_session = bool(
            self.project_root
            and "@" in value
            and self._complete_base_value
            and "@" in self._complete_base_value
        )
        if not value.startswith("/") and not in_at_session:
            self._complete_applied = None
            self._complete_cands = []
            self._complete_active_idx = 0
            self._complete_base_value = ""
        elif self._complete_applied and not value.casefold().startswith(
            self._complete_applied[: max(1, len(value))].casefold()
        ):
            self._complete_applied = None
            self._complete_active_idx = 0
            self._complete_base_value = ""
        # 清理 @ 补全状态：当 value 不再包含 @ 或不再以已应用的补齐开头
        if self._complete_applied and "@" in self._complete_applied:
            if "@" not in value or not value.startswith(
                self._complete_applied[: max(1, len(value))]
            ):
                self._complete_applied = None
                self._complete_cands = []
                self._complete_active_idx = 0
                self._complete_base_value = ""
        self._set_complete_hint(value)

    def _mcp_snapshot(self) -> tuple[bool, list[str], list[str], list[str]]:
        enabled = bool(getattr(self.settings, "enable_mcp", True))
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        return enabled, servers, tools, warnings

    def _mcp_label(self) -> str:
        enabled, servers, tools, warnings = self._mcp_snapshot()
        return format_mcp_status_label(
            enabled=enabled,
            servers=servers,
            tools=tools,
            warnings=warnings,
        )

    def _reload_session_title(self) -> None:
        """Load human title for the active thread into chrome state."""
        title = ""
        try:
            from synapse.sessions import SessionStore

            info = SessionStore(self.settings.resolved_sessions_path()).get(
                self.thread_id
            )
            if info is not None:
                title = (info.title or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
        self._session_title = title

    def _session_title_label(self, *, max_len: int = 48) -> str:
        title = (self._session_title or "").strip()
        if not title:
            # Compact fallback so middle is never empty.
            tid = str(self.thread_id or "")
            title = tid if len(tid) <= 12 else f"{tid[:8]}…"
        if len(title) <= max_len:
            return title
        return title[: max(0, max_len - 1)] + "…"

    def _context_window_tokens(self) -> int | None:
        """Model context window (tokens) from chat model profile or models.json."""
        agent = getattr(self, "agent", None)
        model = getattr(agent, "_coding_model", None) if agent is not None else None
        profile = getattr(model, "profile", None) if model is not None else None
        if isinstance(profile, dict):
            raw = profile.get("max_input_tokens")
            try:
                n = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n

        reg = getattr(agent, "_coding_model_registry", None) if agent is not None else None
        name = None
        if agent is not None:
            name = getattr(agent, "_coding_model_profile", None)
        if not name:
            name = getattr(self.settings, "active_model", None) or getattr(
                self.settings, "model", None
            )
        if reg is not None and name:
            try:
                prof = reg.get(name)
                win = getattr(prof, "context_window", None)
                if win is not None and int(win) > 0:
                    return int(win)
            except Exception:  # noqa: BLE001
                pass

        try:
            from synapse.models_registry import registry_from_settings

            reg2 = registry_from_settings(self.settings)
            if reg2 is not None:
                prof2 = reg2.get(name)
                win2 = getattr(prof2, "context_window", None)
                if win2 is not None and int(win2) > 0:
                    return int(win2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _usage_right_label(self) -> str:
        """Right chrome: session in/cache/out, then last-turn occupancy.

        Order matters: put totals first so residual clipping never hides out.
        Example: ``2M/1.9M/12K 300K/60%``.
        """
        parts: list[str] = []
        last_in = int(getattr(self, "_context_tokens", 0) or 0)
        # Session totals (always show once any counter is non-zero).
        if self._input_tokens or self._cache_tokens or self._output_tokens:
            parts.append(
                format_usage_label(
                    input_tokens=self._input_tokens,
                    cache_tokens=self._cache_tokens,
                    output_tokens=self._output_tokens,
                )
            )
        elif last_in:
            # Fallback: at least surface last-turn input as in/0/0.
            parts.append(
                format_usage_label(
                    input_tokens=last_in,
                    cache_tokens=0,
                    output_tokens=0,
                )
            )
        occ = format_context_occupancy_label(
            last_input_tokens=last_in,
            context_window=self._context_window_tokens(),
        )
        if occ:
            parts.append(occ)
        # Empty when no usage yet — model lives on the status row, not topbar.
        return " ".join(parts)


    def _begin_turn_usage(self) -> None:
        """Mark session totals baseline for live per-call topbar updates."""
        self._usage_base_input = int(self._input_tokens or 0)
        self._usage_base_output = int(self._output_tokens or 0)
        self._usage_base_cache = int(self._cache_tokens or 0)

    def apply_turn_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
    ) -> None:
        """Apply cumulative-in-turn usage (from stream) onto session chrome.

        ``turn_*`` are totals for the *current* stream/turn so far (not deltas).
        Session display = baseline + turn totals. Occupancy uses last call input.
        """
        self._input_tokens = int(self._usage_base_input or 0) + max(0, int(turn_input or 0))
        self._output_tokens = int(self._usage_base_output or 0) + max(
            0, int(turn_output or 0)
        )
        self._cache_tokens = int(self._usage_base_cache or 0) + max(0, int(turn_cache or 0))
        if last_input or last_output or last_cache:
            self._context_tokens = int(last_input or 0)
            self._last_out_tokens = int(last_output or 0)
        self._refresh_topbar()

    def _apply_restored_usage(self, messages: list[Any] | None) -> None:
        """Hydrate topbar totals from checkpoint messages (session open / switch)."""
        try:
            from synapse.ui.stream import aggregate_usage_from_messages
        except Exception:  # noqa: BLE001
            return
        try:
            agg = aggregate_usage_from_messages(messages)
        except Exception:  # noqa: BLE001
            return
        self._input_tokens = int(agg.get("input_tokens") or 0)
        self._output_tokens = int(agg.get("output_tokens") or 0)
        self._cache_tokens = int(agg.get("cache_tokens") or 0)
        self._context_tokens = int(agg.get("last_input_tokens") or 0)
        self._last_out_tokens = int(agg.get("last_output_tokens") or 0)
        self._usage_base_input = self._input_tokens
        self._usage_base_output = self._output_tokens
        self._usage_base_cache = self._cache_tokens
        self._refresh_topbar()

    def _refresh_topbar(self, tokens: str | None = None) -> None:
        del tokens  # legacy arg; usage is tracked on the app
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        # CSS #topbar padding: 0 1; single row, no wrap.
        # Three regions with spacing: left | centered title | right.
        usable = max(20, width - 2)
        col_gap = 3  # spaces between left/title/right

        workspace = short_workspace_label(self.settings.workspace)
        title = (self._session_title or "").strip() or self._session_title_label(
            max_len=56
        )
        usage = self._usage_right_label()
        branch = (self._git_branch or "").strip()

        left = f"≡  {workspace}"
        right_bits: list[str] = []
        if branch:
            right_bits.append(f"{_TOPBAR_BRANCH_MARK} {branch}")
        if usage:
            right_bits.append(usage)
        # Branch and usage separated with a medium gap (not glued).
        right = "  ·  ".join(right_bits) if right_bits else ""

        left_w = display_width(left)
        right_w = display_width(right)
        # Title sits in the middle band; shrink it first when space is tight.
        mid_budget = usable - left_w - right_w - 2 * col_gap
        if mid_budget < 4:
            # Prefer keeping right chrome; compress left label next.
            right = truncate_to_width(right, max(8, usable // 3))
            right_w = display_width(right)
            left = truncate_to_width(left, max(6, usable // 4))
            left_w = display_width(left)
            mid_budget = max(4, usable - left_w - right_w - 2 * col_gap)
        title = truncate_to_width(title, mid_budget)
        title_w = display_width(title)

        rest = usable - left_w - title_w - right_w
        if rest < 2 * col_gap:
            # Last resort: hard truncate whole line without wrapping.
            body = truncate_to_width(
                f"{left}{' ' * col_gap}{title}{' ' * col_gap}{right}",
                usable,
            )
            line = Text(body, style=_C_DIM)
            self.query_one("#topbar", Static).update(line)
            return

        # Distribute leftover spaces so the title stays visually centered.
        pad_l = rest // 2
        pad_r = rest - pad_l
        # Enforce a minimum gap so regions never collide.
        if pad_l < col_gap:
            shift = col_gap - pad_l
            pad_l += shift
            pad_r = max(col_gap, pad_r - shift)
        if pad_r < col_gap:
            shift = col_gap - pad_r
            pad_r += shift
            pad_l = max(col_gap, pad_l - shift)

        line = Text()
        line.append(left, style=_C_DIM)
        line.append(" " * pad_l, style=_C_MUTED)
        line.append(title, style=_C_DIM)
        line.append(" " * pad_r, style=_C_MUTED)
        line.append(right, style=_C_MUTED)
        self.query_one("#topbar", Static).update(line)

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self._refresh_topbar()
        self._render_status()
        self._refresh_turn_rail()

    # -- status ----------------------------------------------------------

    def set_activity(self, phase: str, detail: str = "", reset_timer: bool = False) -> None:
        detail = detail or ""
        if reset_timer or phase != self._phase:
            self._activity_started = time.monotonic()
        self._phase = phase or "idle"
        self._detail = detail
        busy = self._phase not in {"idle", "ready", ""}
        self.query_one("#status", Static).set_class(busy, "busy")
        if busy:
            self.sub_title = f"{model_status_label(self.settings)} · {self._phase}"
        else:
            self.sub_title = model_status_label(self.settings)
        self._render_status()

    def _resident_status_right(self) -> str:
        """Always-on chrome above the prompt: model · thinking · mcp (+ steer)."""
        base = f"{model_status_label(self.settings)} · {self._mcp_label()}"
        n = len(self._steer_items)
        if n:
            return f"{base} · steer×{n}"
        return base

    def _idle_status_label(self) -> str:
        """Bottom status when idle (alias of resident right chrome)."""
        return self._resident_status_right()

    def _render_status(self) -> None:
        elapsed = max(0.0, time.monotonic() - self._activity_started)
        busy = self._phase not in {"idle", "ready", ""}
        status = self.query_one("#status", Static)
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        # Account for CSS padding (0 2) so right-aligned text is not clipped.
        usable = max(16, width - 4)
        steer_n = len(self._steer_items)
        # Model / thinking / MCP stay on the right whether idle or looping.
        right = self._resident_status_right()
        right_w = display_width(right)
        if right_w > usable:
            right = truncate_to_width(right, usable)
            right_w = display_width(right)
            left = ""
            pad = max(0, usable - right_w)
            status.update(Text((" " * pad) + right, style=_C_MUTED))
            return

        if not busy:
            pad = max(0, usable - right_w)
            status.update(Text((" " * pad) + right, style=_C_MUTED))
            return

        spin = _SPINNER[self._spin_i % len(_SPINNER)]
        detail = f" {self._detail}" if self._detail else ""
        steer_badge = f" · steer×{steer_n}" if steer_n else ""
        # Leave ≥1 space between activity and resident chrome.
        left_budget = max(8, usable - right_w - 1)
        left = f"{spin} {self._phase}{detail}{steer_badge} · {elapsed:.1f}s"
        left = truncate_to_width(left, left_budget)
        left_w = display_width(left)
        pad = max(1, usable - left_w - right_w)
        # Clamp if CJK/width math still overflows.
        overflow = left_w + pad + right_w - usable
        if overflow > 0:
            pad = max(1, pad - overflow)
            still = left_w + pad + right_w - usable
            if still > 0:
                left = truncate_to_width(left, max(4, left_w - still))
                left_w = display_width(left)
                pad = max(1, usable - left_w - right_w)

        line = Text()
        line.append(left, style=_C_GREEN if not steer_n else _C_ORANGE)
        line.append(" " * pad, style=_C_MUTED)
        line.append(right, style=_C_MUTED)
        status.update(line)

    def _bind_steer_queue(self) -> None:
        """Attach live UI listener to the agent steer queue."""
        q = get_agent_steer_queue(self.agent)
        if q is None:
            return
        if self._steer_listener_bound:
            self._on_steer_items_changed(q.peek_items())
            return

        def _on_change(items: list[str]) -> None:
            # Middleware drain may fire from the worker thread.
            try:
                self.call_from_thread(self._on_steer_items_changed, list(items))
            except Exception:  # noqa: BLE001
                self._on_steer_items_changed(list(items))

        q.add_listener(_on_change)
        self._steer_listener_bound = True
        self._on_steer_items_changed(q.peek_items())

    def _on_steer_items_changed(self, items: list[str]) -> None:
        prev = self._steer_last_count
        now = len(items)
        self._steer_items = list(items)
        self._steer_last_count = now
        try:
            self.query_one("#steer-queue", SteerQueueWidget).set_items(self._steer_items)
        except Exception:  # noqa: BLE001
            pass
        self._render_status()
        self._sync_prompt_placeholder()
        # Applied: short status only, no essay in the log.
        if prev > 0 and now == 0 and self._busy:
            self.append_event(f"已注入 {prev} 条引导", "dim")

    def _sync_prompt_placeholder(self) -> None:
        """Prompt copy guides mode: normal vs mid-run queue."""
        try:
            prompt = self.query_one("#prompt", Input)
        except Exception:  # noqa: BLE001
            return
        if self._busy:
            n = len(self._steer_items)
            if n:
                prompt.placeholder = f"{_MARK_INPUT}  继续添加引导（已有 {n}）…"
            else:
                prompt.placeholder = f"{_MARK_INPUT}  输入引导，下轮生效…"
        else:
            prompt.placeholder = f"{_MARK_INPUT}  Build anything  (/ for commands, Tab complete)"

    def drop_steer_at(self, index: int) -> None:
        """UI: remove one pending steer note by index."""
        q = get_agent_steer_queue(self.agent)
        if q is None:
            return
        q.remove_at(int(index))

    def clear_steer_queue(self) -> None:
        """UI: clear all pending steer notes."""
        q = get_agent_steer_queue(self.agent)
        if q is None:
            return
        q.clear()

    def _tick_status(self) -> None:
        if self._phase not in {"idle", "ready", ""}:
            self._spin_i += 1
            self._render_status()
        else:
            self._maybe_show_session_recap()

    # -- stream ----------------------------------------------------------

    def set_stream(self, kind: str, body: str, elapsed_s: float = 0.0) -> None:
        """Mount or update a live block at the end of #log (in place)."""
        text = body or ""
        kind = (kind or "answer").strip() or "answer"
        if self._live_stream_kind and self._live_stream_kind != kind:
            self._live_stream_block = None
            self._live_stream_kind = None
        if not text.strip() and self._live_stream_block is None:
            return
        if kind == "reasoning":
            block = self._live_stream_block
            if not isinstance(block, ThoughtBlock) or self._live_stream_kind != "reasoning":
                block = ThoughtBlock(float(elapsed_s or 0.0), text, live=True)
                self._live_stream_block = block
                self._live_stream_kind = "reasoning"
                self._thought_blocks.append(block)
                self._mount_block(block)
            else:
                block.update_live(float(elapsed_s or 0.0), text)
                self._follow_timeline_if_needed()
            self._in_tool_rail = False
            return
        block = self._live_stream_block
        if not isinstance(block, AnswerBlock) or self._live_stream_kind != "answer":
            if self._pending_answer_divider:
                self._mount_answer_divider()
                self._pending_answer_divider = False
            block = AnswerBlock(text, live=True)
            self._live_stream_block = block
            self._live_stream_kind = "answer"
            self._mount_block(block)
        else:
            block.update_live(text)
            self._follow_timeline_if_needed()

    def clear_stream(self) -> None:
        """Drop unsealed live stream row; legacy #stream stays empty."""
        try:
            stream = self.query_one("#stream", Static)
            stream.update("")
            stream.remove_class("active")
        except Exception:  # noqa: BLE001
            pass
        block = self._live_stream_block
        if block is not None and getattr(block, "live", False):
            try:
                if block.is_attached:
                    block.remove()
            except Exception:  # noqa: BLE001
                pass
            if isinstance(block, ThoughtBlock) and block in self._thought_blocks:
                self._thought_blocks.remove(block)
        self._live_stream_block = None
        self._live_stream_kind = None

    def _follow_timeline_if_needed(self) -> None:
        try:
            timeline = self.query_one("#log", VerticalScroll)
        except Exception:  # noqa: BLE001
            return
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        if follow:
            self.call_after_refresh(self._scroll_timeline)

    # -- transcript writers ----------------------------------------------

    def _show_welcome(self) -> None:
        try:
            self.query_one("#main", Vertical).add_class("welcome")
            self.query_one("#welcome", WelcomeView).start_animation()
        except Exception:  # noqa: BLE001
            pass

    def _dismiss_welcome(self) -> None:
        try:
            self.query_one("#main", Vertical).remove_class("welcome")
            self.query_one("#welcome", WelcomeView).stop_animation()
        except Exception:  # noqa: BLE001
            pass

    def _mount_block(self, block: Any, *, dismiss_welcome: bool = True) -> None:
        if dismiss_welcome:
            self._dismiss_welcome()
        timeline = self.query_one("#log", VerticalScroll)
        follow = timeline.max_scroll_y <= 0 or timeline.scroll_y >= timeline.max_scroll_y - 1
        timeline.mount(block)
        if follow:
            self.call_after_refresh(self._scroll_timeline)

    def _scroll_timeline(self) -> None:
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def append_user(
        self,
        text: str,
        images: list[Any] | None = None,
    ) -> None:
        imgs = list(images or [])
        block = UserTurnBlock(
            text or "",
            stamp=_stamp(),
            turn_index=len(self._user_turns) + 1,
            image_count=len(imgs),
        )
        self._user_turns.append(block)
        self._mount_block(block)
        self._refresh_turn_rail()
        self._in_tool_rail = False
        self._pending_answer_divider = False

    def _refresh_turn_rail(self) -> None:
        """Rebuild right-side turn markers from current user anchors."""
        try:
            rail = self.query_one("#turn-rail", TurnRail)
        except Exception:  # noqa: BLE001
            return
        turns = [
            (format_turn_rail_preview(block.full_text), block)
            for block in self._user_turns
        ]
        rail.set_turns(turns)

    def jump_to_user_turn(self, target: UserTurnBlock) -> None:
        """Scroll the transcript so the selected user turn is at the top."""
        if target is None or not target.is_attached:
            return
        timeline = self.query_one("#log", VerticalScroll)
        try:
            timeline.scroll_to_widget(target, animate=True, top=True)
        except Exception:  # noqa: BLE001
            try:
                timeline.scroll_to_center(target, animate=True)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    #  Alt+V clipboard paste (image or text)
    # ------------------------------------------------------------------
    def action_clipboard_paste(self) -> None:
        try:
            result = read_clipboard()
        except Exception:  # noqa: BLE001
            self.append_event("clipboard read failed", "yellow")
            return

        if result.kind == "empty":
            self.append_event("clipboard empty", "dim")
            return

        if result.kind == "text":
            text = result.text or ""
            prompt = self.query_one("#prompt", Input)
            if len(text) > 200:
                prefix = text[:20].replace("\n", " ").strip()
                placeholder = f"[{prefix}... {len(text)} chars]"
                self._paste_replacements[placeholder] = text
                old = prompt.value or ""
                prompt.value = old + placeholder
                self.append_event(
                    f"pasted text truncated: {len(text)} chars -> placeholder (content preserved)", "dim"
                )
            else:
                old = prompt.value or ""
                prompt.value = old + text
            prompt.focus()
            return

        if result.kind == "image":
            try:
                att = self._image_bank.add_bytes(
                    result.data, mime=result.mime, name=result.name
                )
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"image rejected: {exc}", "yellow")
                return
            self.append_event(
                f"pasted {att.name} -> [image#{att.id}]", "dim"
            )
            prompt = self.query_one("#prompt", Input)
            old = prompt.value or ""
            prompt.value = old + f" [image#{att.id}]"
            prompt.focus()

    # ------------------------------------------------------------------

    def commit_thought(self, elapsed_s: float, body: str) -> None:
        self._last_thought_body = body or ""
        self._last_thought_elapsed = elapsed_s
        self._thought_expanded = False
        live = self._live_stream_block
        if (
            isinstance(live, ThoughtBlock)
            and self._live_stream_kind == "reasoning"
        ):
            live.seal(elapsed_s, body or "")
            self._live_stream_block = None
            self._live_stream_kind = None
            self._follow_timeline_if_needed()
        else:
            block = ThoughtBlock(elapsed_s, body)
            self._thought_blocks.append(block)
            self._mount_block(block)
        self._in_tool_rail = False

    def action_toggle_last_thought(self) -> None:
        """Toggle the most recent ThoughtBlock (supports historical/frozen ones in transcript)."""
        timeline = self.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ThoughtBlock):
                child.toggle()
                self._thought_expanded = not child.collapsed
                return
        # Fallback to tracked list if DOM query yields nothing (e.g. cleared state)
        if self._thought_blocks:
            self._thought_blocks[-1].toggle()
            self._thought_expanded = not self._thought_blocks[-1].collapsed

    def action_toggle_last_tools(self) -> None:
        """Toggle the latest tool group (supports historical/frozen ones after commit).
        Queries live DOM so collapsed state works even for groups from prior turns.
        """
        timeline = self.query_one("#log", VerticalScroll)
        for child in reversed(list(timeline.children)):
            if isinstance(child, ToolGroupBlock):
                child.toggle()
                return
        # Fallback
        if self._tool_blocks:
            self._tool_blocks[-1].toggle()

    def commit_answer(self, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        # Context-compaction summaries are for the model only.
        try:
            from synapse.context_compact import is_context_compact_text

            if is_context_compact_text(body):
                self.append_event("context compacted (hidden)", "dim")
                self.clear_stream()
                return
        except Exception:  # noqa: BLE001
            pass
        self._last_answer_text = body
        self._commit_live_tools_to_log()
        if self._pending_answer_divider:
            self._mount_answer_divider()
            self._pending_answer_divider = False
        # Huge Markdown trees can stall the terminal for seconds.  Prefer
        # plain text once past the soft limit; normal answers stay Markdown.
        if len(body) > _MARKDOWN_MAX_CHARS:
            renderable: Any = Text(body, style=_C_FG)
        else:
            renderable = Markdown(render_math_in_text(body), code_theme=_CODE_THEME)
        self._mount_block(Static(Group(renderable, Text(""))))

    def _mount_answer_divider(self) -> None:
        """Insert centered ◇ rule with vertical spacing before the answer."""
        width = 0
        try:
            log = self.query_one("#log", VerticalScroll)
            width = int(getattr(log.size, "width", 0) or 0)
        except Exception:  # noqa: BLE001
            width = 0
        if width <= 0:
            width = int(getattr(self.size, "width", 0) or 0)
        # Subtract log padding (0 1) so the rule centers in the content box.
        usable = max(28, (width or 56) - 2)
        self._mount_block(AnswerDivider(usable))

    # -- tool group rendering (live panel) --------------------------------

    def _render_live_tools(self) -> None:
        if self._live_tool_block is not None:
            self._live_tool_block.set_summary(self._live_tool_summary or "tools")

    def _tool_details_expanded(self) -> bool:
        """Whether finished tool groups keep detail rows visible (config default: True)."""
        return bool(getattr(self.settings, "tool_details_expanded", True))

    def _commit_live_tools_to_log(self) -> None:
        if self._live_tool_block is None:
            return
        self._last_tool_items = list(self._live_tool_block.items)
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items.clear()
        self._live_tool_summary = ""
        self._live_tool_block = None

    def write_tool_group_header(self, summary: str, collapsed: bool = True) -> None:
        # Never paint empty placeholder groups ("0 tools").
        if (summary or "").strip() in {"", "0 tools", "tools", "Running 0 tools"}:
            if self._live_tool_block is None or not self._live_tool_block.items:
                return
        # A sealed previous group must leave _live_tool_block as None so the
        # next batch always creates a fresh block (never reuses a frozen one).
        if self._live_tool_block is None:
            block = ToolGroupBlock(summary)
            block.collapsed = collapsed
            block._render_block()
            self._live_tool_block = block
            self._tool_blocks.append(block)
            self._mount_block(block)
        else:
            self._live_tool_block.set_summary(summary)
            self._live_tool_block.set_collapsed(collapsed)
        self._live_tool_summary = summary
        self._last_tool_summary = summary

    def update_tool_group_header(self, summary: str) -> None:
        self._live_tool_summary = summary
        self._last_tool_summary = summary
        self._render_live_tools()

    def write_tool_item(self, item: ToolItem) -> None:
        if self._live_tool_block is None:
            self.write_tool_group_header("tools", collapsed=False)
        assert self._live_tool_block is not None
        # Keep live groups expanded while tools are still arriving/running,
        # even when auto-collapse-after-finish is enabled.
        if any(it.status == "running" for it in [*self._live_tool_block.items, item]):
            self._live_tool_block.set_collapsed(False)
        elif self._tool_details_expanded():
            self._live_tool_block.set_collapsed(False)
        self._live_tool_block.add_item(item)
        # Prefer the block's self-derived summary (always matches items).
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)

    def update_tool_item(
        self,
        item_id: str,
        *,
        status: str | None = None,
        preview: str | None = None,
        error: bool | None = None,
        label: str | None = None,
        path: str | None = None,
        name: str | None = None,
        category: str | None = None,
    ) -> None:
        if self._live_tool_block is None:
            return
        self._live_tool_block.update_item(
            item_id,
            status=status,
            preview=preview,
            error=error,
            label=label,
            path=path,
            name=name,
            category=category,
        )
        self._live_tool_summary = self._live_tool_block.summary
        self._last_tool_summary = self._live_tool_block.summary
        self._live_tool_items = list(self._live_tool_block.items)
        self._last_tool_items = list(self._live_tool_items)

    def write_tool_preview(
        self, item_id: str, preview: str, *, error: bool = False
    ) -> None:
        if self._live_tool_block is not None:
            self._live_tool_block.update_preview(item_id, preview, error=error)

    def close_tool_group(self) -> None:
        """Freeze the live tool block so the next batch creates a new group."""
        if self._live_tool_block is not None:
            # Final header from items, not a stale early partial summary.
            self._live_tool_block._sync_summary_from_items(running=False)
            # Default: keep details expanded. Config can auto-collapse finished batches.
            # write_todos checklists always stay expanded for readability.
            has_todo = any(
                (it.name or "").lower() in {"write_todos", "todo_write", "todos"}
                or str(it.label or "").startswith("Todos ")
                for it in self._live_tool_block.items
            )
            keep_open = has_todo or self._tool_details_expanded()
            self._live_tool_block.set_collapsed(not keep_open)
            self._live_tool_summary = self._live_tool_block.summary
            self._last_tool_summary = self._live_tool_block.summary
            self._live_tool_block._render_block()
            # Tools finished → next final answer should show the ◇ rule.
            if self._live_tool_block.items:
                self._pending_answer_divider = True
        self._commit_live_tools_to_log()

    def append_meta(self, message: str) -> None:
        self._commit_live_tools_to_log()
        body = soften_turn_footer(message)
        self._mount_block(Static(Text(f"  {body}", style=_C_MUTED)))

    def append_event(self, message: str, style: str = "dim") -> None:
        self._mount_block(
            Static(Text(f"  {message}", style=style)),
            dismiss_welcome=(style or "dim").lower() != "dim",
        )

    def action_cancel_run(self) -> None:
        """ESC: abort the in-flight agent loop so the user can start a new turn."""
        if isinstance(self.screen, ModalScreen):
            return
        if not self._busy:
            return
        # Idempotent: repeated ESC only re-asserts the cancel flag.
        self._cancel_event.set()
        self.set_activity("idle", "cancelling…", True)
        self.append_event("正在终止当前任务… (Esc)", "yellow")

    def on_key(self, event: Key) -> None:
        # When a modal dialog is open, let it handle keys exclusively.
        if isinstance(self.screen, ModalScreen):
            return
        # Backup path if a child widget swallows Escape before bindings fire.
        if event.key == "escape" and self._busy:
            self.action_cancel_run()
            event.stop()
            event.prevent_default()

    def action_clear_log(self) -> None:
        self.query_one("#log", VerticalScroll).remove_children()
        self.clear_stream()
        self._last_thought_body = ""
        self._last_tool_items.clear()
        self._last_answer_text = ""
        self._live_tool_items.clear()
        self._live_tool_summary = ""
        self._thought_blocks.clear()
        self._tool_blocks.clear()
        self._live_tool_block = None
        self._live_stream_block = None
        self._live_stream_kind = None
        self._user_turns.clear()
        self._in_tool_rail = False
        self._session_recap.reset()
        try:
            self.query_one("#turn-rail", TurnRail).clear_turns()
        except Exception:  # noqa: BLE001
            pass
        self._show_welcome()
        self.set_activity("idle", "ready", True)

    def _reset_session_token_chrome(self) -> None:
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        self._context_tokens = 0
        self._last_out_tokens = 0
        self._usage_base_input = 0
        self._usage_base_output = 0
        self._usage_base_cache = 0

    def _render_restored_tools(
        self,
        tool_calls: list[dict],
        tool_results: list[dict],
    ) -> None:
        """Render a historical tool batch as a collapsed group."""
        from synapse.ui.timeline import (
            build_tool_item,
            extract_todos,
            format_todos_preview,
            is_todo_tool,
            truncate_preview,
        )

        if not tool_calls and not tool_results:
            return
        items: list[ToolItem] = []
        result_by_id = {
            str(r.get("id") or ""): r for r in (tool_results or []) if isinstance(r, dict)
        }
        result_by_name: dict[str, list[dict]] = {}
        for r in tool_results or []:
            if not isinstance(r, dict):
                continue
            result_by_name.setdefault(str(r.get("name") or ""), []).append(r)

        for i, call in enumerate(tool_calls or []):
            if not isinstance(call, dict):
                continue
            cid = str(call.get("id") or f"hist-{i}")
            item = build_tool_item(call, item_id=cid, index=i)
            res = result_by_id.get(cid)
            if res is None:
                bucket = result_by_name.get(str(call.get("name") or ""), [])
                if bucket:
                    res = bucket.pop(0)
            if res is not None:
                content = str(res.get("content") or "")
                status = str(res.get("status") or "ok")
                item.status = "error" if status == "error" else "done"
                item.error = item.status == "error"
                # Prefer checklist from tool args over dumping tool-result JSON.
                if is_todo_tool(item.name):
                    args = call.get("args") if isinstance(call, dict) else {}
                    checklist = format_todos_preview(extract_todos(args))
                    item.preview = checklist or (
                        truncate_preview(content) if content else None
                    )
                else:
                    item.preview = truncate_preview(content) if content else None
            else:
                item.status = "done"
            items.append(item)

        # Orphan results (no matching call) as plain items.
        used_ids = {it.id for it in items}
        for r in tool_results or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "")
            if rid and rid in used_ids:
                continue
            if rid and any(it.id == rid for it in items):
                continue
            fake = {
                "name": r.get("name") or "tool",
                "args": {},
                "id": rid or f"orphan-{len(items)}",
            }
            item = build_tool_item(fake, item_id=str(fake["id"]), index=len(items))
            content = str(r.get("content") or "")
            status = str(r.get("status") or "ok")
            item.status = "error" if status == "error" else "done"
            item.error = item.status == "error"
            item.preview = truncate_preview(content) if content else None
            items.append(item)

        if not items:
            return
        summary = summarize_items(items, running=False)
        self.write_tool_group_header(summary, collapsed=True)
        for it in items:
            self.write_tool_item(it)
        self.close_tool_group()

    def _restore_session_transcript(self, *, announce: bool = True) -> None:
        """Load checkpoint messages for current thread and paint the timeline.

        LLM context is restored by reusing the same ``thread_id`` with the
        LangGraph checkpointer; this method only rebuilds the visual history.
        """
        if self.agent is None:
            return
        from synapse.transcript import fold_messages_for_ui, load_thread_messages

        try:
            messages = load_thread_messages(
                agent=self.agent,
                settings=self.settings,
                thread_id=self.thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            if announce:
                self.append_event(f"restore transcript failed: {exc}", "yellow")
            return

        events = fold_messages_for_ui(messages)
        if not events:
            if announce and messages is not None:
                # Only announce emptiness on explicit /switch restore.
                self.append_event("(empty session transcript)", "dim")
            return

        n_user = n_answer = n_tools = n_thought = 0
        for ev in events:
            kind = ev.kind
            if kind == "user":
                self.append_user(ev.text, images=getattr(ev, "images", None) or None)
                n_user += 1
            elif kind == "thought":
                # Historical thoughts: collapsed, elapsed unknown.
                self.commit_thought(0.0, ev.text)
                n_thought += 1
            elif kind == "tools":
                self._render_restored_tools(ev.tool_calls, ev.tool_results)
                n_tools += 1
            elif kind == "answer":
                self.commit_answer(ev.text)
                n_answer += 1

        # Hydrate session token chrome from AIMessage usage_metadata.
        self._apply_restored_usage(messages)

        if announce:
            self.append_event(
                f"restored transcript: {n_user} user / {n_answer} answers"
                f" / {n_tools} tool groups / {n_thought} thoughts"
                f"  ({len(messages)} msgs)",
                "dim",
            )
        # Jump to bottom after paint.
        self.call_after_refresh(self._scroll_timeline)

    # -- theme -----------------------------------------------------------

    def apply_theme(
        self,
        name: str | None = None,
        *,
        persist: bool = False,
        announce: bool = False,
    ) -> str:
        """Activate a theme at runtime (CSS variables + Rich paint slots)."""
        from synapse.ui.theme import get_theme, set_theme

        theme = set_theme(
            name or getattr(self.settings, "theme", None),
            workspace=self.project_root,
            persist=persist,
            scope="user",
        )
        try:
            self.settings.theme = theme.name
        except Exception:  # noqa: BLE001
            pass
        # refresh_css() calls get_css_variables() which returns the active
        # theme palette; this triggers a full reparse + re-apply of all
        # $theme-* variables across every widget.
        try:
            self.refresh_css(animate=False)
        except Exception:  # noqa: BLE001
            pass
        self._repaint_themed_widgets()
        if announce:
            self.append_event(f"theme: {theme.name} ({theme.label})", "dim")
        return get_theme().name

    def _repaint_themed_widgets(self) -> None:
        """Re-render widgets that baked colors into Rich Text."""
        for cls_name, method in (
            ("WelcomeView", "refresh_logo"),
            ("UserTurnBlock", "_render_block"),
            ("ThoughtBlock", "_render_block"),
            ("ToolGroupBlock", "_render_block"),
            ("TodoChecklist", "_render_block"),
            ("AnswerDivider", "_render_block"),
            ("TurnRailItem", "_show_bar"),
        ):
            try:
                for widget in self.query(cls_name):
                    fn = getattr(widget, method, None)
                    if callable(fn):
                        fn()
            except Exception:  # noqa: BLE001
                continue
        try:
            steer = self.query_one("#steer-queue", SteerQueueWidget)
            paint = getattr(steer, "_paint_block", None)
            if callable(paint):
                paint()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._refresh_topbar()
            self._render_status()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.refresh(layout=False)
        except Exception:  # noqa: BLE001
            pass

    # -- dialogs ----------------------------------------------------------

    def _open_model_dialog(self, _args: list[str]) -> None:
        from synapse.ui.dialogs import ModelPickerDialog

        self.push_screen(
            ModelPickerDialog(self.settings),
            self._on_model_dialog_done,
        )

    def _on_model_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, value = result
        if action == "model":
            self._apply_model_switch(value)
        elif action == "thinking":
            self._apply_thinking_switch(value)

    def _apply_model_switch(self, alias: str) -> None:
        self._switch_model_bg(f"/model {alias}", f"switching model to {alias}")

    def _apply_thinking_switch(self, level: str) -> None:
        self._switch_model_bg(f"/model thinking {level}", f"thinking -> {level}")

    @work(thread=True, exclusive=True, group="model-switch")
    def _switch_model_bg(self, command: str, activity: str) -> None:
        """Run /model rebuild off the UI thread so the TUI stays responsive."""
        from synapse.slash_cmds import handle_slash

        self.call_from_thread(self.set_activity, "switching", activity, True)
        self.call_from_thread(self.append_event, f"{activity} ...", "dim")
        try:
            ok = handle_slash(
                command,
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self.append_event, f"{activity} failed: {exc}", "yellow"
            )
            self.call_from_thread(self.set_activity, "idle", "", True)
            return
        self.call_from_thread(self._apply_ok_result, ok)
        self.call_from_thread(self.set_activity, "idle", "", True)

    def _open_theme_dialog(self) -> None:
        from synapse.ui.dialogs import ThemePickerDialog

        self.push_screen(
            ThemePickerDialog(self.settings, project_root=self.project_root),
            self._on_theme_dialog_done,
        )

    def _on_theme_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, name = result
        if action == "theme":
            try:
                self.apply_theme(str(name), persist=True, announce=True)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme failed: {exc}", "yellow")

    def _open_session_dialog(self, parts: list[str]) -> None:
        mode = "switch"
        if len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            mode = "delete"
        elif len(parts) >= 2 and parts[1].casefold() in {"switch", "sel"}:
            mode = "switch"
        from synapse.ui.dialogs import SessionListDialog

        self.push_screen(
            SessionListDialog(
                self.settings,
                current_thread=self.thread_id,
                mode=mode,
            ),
            self._on_session_dialog_done,
        )

    def _on_session_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, thread_id = result
        if action == "switch":
            self._apply_session_switch(thread_id)
        elif action == "delete":
            self._apply_session_delete(thread_id)

    def _apply_session_switch(self, thread_id: str) -> None:
        from synapse.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                f"/switch {thread_id}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"switch failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _apply_session_delete(self, thread_id: str) -> None:
        from synapse.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                f"/session delete {thread_id}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"delete failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _open_mcp_dialog(self) -> None:
        from synapse.ui.dialogs import McpPanelDialog

        self.push_screen(
            McpPanelDialog(self.settings, project_root=self.project_root),
            self._on_mcp_dialog_done,
        )

    def _on_mcp_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action = result[0] if result else None
        if action == "mcp-toggle":
            server_name = result[1]
            self._apply_mcp_toggle(server_name)
        elif action == "mcp-reload":
            self._apply_mcp_reload()

    def _apply_mcp_toggle(self, server_name: str) -> None:
        from synapse.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                f"/mcp toggle {server_name}",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"MCP toggle failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _apply_mcp_reload(self) -> None:
        from synapse.slash_cmds import handle_slash

        try:
            ok = handle_slash(
                "/mcp reload",
                settings=self.settings,
                agent=self.agent,
                thread_id=self.thread_id,
                project_root=self.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            self.append_event(f"MCP reload failed: {exc}", "yellow")
            return
        self._apply_ok_result(ok)

    def _open_safety_dialog(self) -> None:
        from synapse.ui.dialogs import SafetyPanelDialog

        self.push_screen(
            SafetyPanelDialog(self.settings),
            self._on_safety_dialog_done,
        )

    def _on_safety_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, profile = result
        if action == "safety":
            from synapse.slash_cmds import handle_slash

            try:
                ok = handle_slash(
                    f"/safety {profile}",
                    settings=self.settings,
                    agent=self.agent,
                    thread_id=self.thread_id,
                    project_root=self.project_root,
                )
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"safety switch failed: {exc}", "yellow")
                return
            self._apply_ok_result(ok)

    def _apply_ok_result(self, ok: object) -> None:
        """Apply a SlashResult returned by handle_slash after a dialog pick."""
        agent = getattr(ok, "agent", None)
        if agent is not None:
            self.agent = agent
            self._steer_listener_bound = False
            self._bind_steer_queue()
        thread_id = getattr(ok, "thread_id", None)
        if thread_id is not None and thread_id != self.thread_id:
            self.thread_id = thread_id
            self.action_clear_log()
            self._reset_session_token_chrome()
        if getattr(ok, "clear_log", False):
            self.action_clear_log()
        if agent is not None or getattr(ok, "settings_changed", False):
            self.sub_title = model_status_label(self.settings)
            self._render_status()
        if getattr(ok, "reload_transcript", False):
            self._restore_session_transcript(announce=True)
        theme_name = getattr(ok, "theme_name", None)
        if theme_name:
            try:
                self.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme apply failed: {exc}", "yellow")
        style = "yellow" if getattr(ok, "error", False) else "dim"
        for line in getattr(ok, "lines", []) or []:
            self.append_event(line, style)
        self._reload_session_title()
        self._refresh_topbar()

    # -- input / turn ----------------------------------------------------

    def _handle_slash(self, text: str) -> bool:
        """Handle local slash commands. Return True if consumed."""
        from synapse.slash_cmds import handle_slash

        if self.agent is None:
            low = text.strip().split()[0].casefold() if text.strip() else ""
            if low not in {
                "/quit", "/exit", "/help", "/?", "/clear",
                "/theme", "/model", "/switch", "/safety",
            }:
                self.append_event(
                    "agent still starting — try again in a moment",
                    "yellow",
                )
                return True

        # ---- dialog-capable commands (push ModalScreen) ----
        raw = (text or "").strip()
        parts = raw.split()
        cmd = parts[0].casefold() if parts else ""

        if cmd == "/model" and len(parts) == 1:
            self._open_model_dialog(parts[1:])
            return True
        if cmd == "/model":
            # Args form (/model <alias> [thinking ...]): rebuild in background.
            self._switch_model_bg(raw, f"model {' '.join(parts[1:])}")
            return True
        if cmd == "/switch" and len(parts) == 1:
            self._open_session_dialog(["switch"])
            return True
        if cmd == "/session" and len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            # /session delete (without thread_id) → pick from list
            if len(parts) == 2:
                self._open_session_dialog(parts)
                return True
        if cmd == "/theme" and (len(parts) == 1 or parts[1].casefold() in {"list", "ls"}):
            self._open_theme_dialog()
            return True
        if cmd == "/mcp" and len(parts) == 1:
            self._open_mcp_dialog()
            return True
        if cmd == "/safety" and len(parts) == 1:
            self._open_safety_dialog()
            return True

        prev_thread = self.thread_id
        result = handle_slash(
            text,
            settings=self.settings,
            agent=self.agent,
            thread_id=self.thread_id,
            project_root=self.project_root,
        )
        if not result.handled:
            return False
        if result.exit_requested:
            self.exit()
            return True

        if result.agent is not None:
            self.agent = result.agent
            self._steer_listener_bound = False
            self._bind_steer_queue()

        thread_changed = False
        if result.thread_id is not None and result.thread_id != prev_thread:
            self.thread_id = result.thread_id
            thread_changed = True

        if result.clear_log or thread_changed:
            self.action_clear_log()
            if thread_changed:
                self._reset_session_token_chrome()

        # Title may change via /rename, /switch, /new, first-message bind, etc.
        self._reload_session_title()
        self._refresh_topbar()
        if result.agent is not None or getattr(result, "settings_changed", False):
            self.sub_title = model_status_label(self.settings)
            self._render_status()

        # Restore visual history after switch/new. LLM context follows thread_id
        # via checkpointer; this only rebuilds the transcript chrome.
        if getattr(result, "reload_transcript", False):
            self._restore_session_transcript(announce=True)
            self._refresh_topbar()

        theme_name = getattr(result, "theme_name", None)
        if theme_name:
            try:
                self.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                self.append_event(f"theme apply failed: {exc}", "yellow")

        style = "yellow" if result.error else "dim"
        for line in result.lines:
            self.append_event(line, style)

        # HITL: /approve or /reject resumes the paused graph.
        resume_action = getattr(result, "resume_action", None)
        if resume_action:
            if self.agent is None:
                self.append_event("agent not ready — cannot resume HITL", "yellow")
                return True
            if self._busy:
                self.append_event("still running previous turn…", "yellow")
                return True
            self._busy = True
            self.set_activity("tool", f"HITL {resume_action}", True)
            self.run_resume(
                str(resume_action),
                getattr(result, "resume_message", None),
            )
        return True

    @on(Input.Submitted, "#prompt")
    def handle_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return

        # 将被截断的粘贴占位符替换回完整原始文本
        for placeholder, full_text in list(self._paste_replacements.items()):
            if placeholder in text:
                text = text.replace(placeholder, full_text)
        self._paste_replacements.clear()

        # Parse [image#N] placeholders from text and resolve to attachments.
        ids = find_placeholders(text)
        attachments: list[Any] = []
        if ids:
            seen: set[int] = set()
            for pid in ids:
                if pid in seen:
                    continue
                seen.add(pid)
                att = self._image_bank.items.get(pid)
                if att is not None:
                    attachments.append(att)

        try:
            self._input_history.add(text)
        except Exception:  # noqa: BLE001
            pass
        if self._handle_slash(text):
            self._image_bank.clear()
            return
        if self._busy:
            # Mid-run guidance: queue for next model step (after current tools).
            self._bind_steer_queue()
            q = get_agent_steer_queue(self.agent)
            if q is not None:
                pending = q.push(text)
                if pending:
                    preview = " ".join(text.split())
                    if len(preview) > 60:
                        preview = preview[:59] + "…"
                    self.append_event(
                        f"steer #{pending} queued: {preview}",
                        "cyan",
                    )
                    return
            self.append_event("still running previous turn…", "yellow")
            return
        try:
            from synapse.sessions import SessionStore

            SessionStore(self.settings.resolved_sessions_path()).touch(
                self.thread_id,
                title_hint=text,
                model=str(self.settings.model),
            )
            self._reload_session_title()
            self._refresh_topbar()
        except Exception:  # noqa: BLE001
            pass

        # Snapshot image bank BEFORE clear so run_turn retains data.
        turn_images = list(attachments)
        resolved_ids = {a.id for a in attachments}
        not_found = [f"[image#{pid}]" for pid in ids if pid not in resolved_ids]
        if not_found:
            # Keep bank + restore prompt; do not send a half-image turn.
            self.append_event(
                f"missing images: {' '.join(not_found)} (not sent)",
                "yellow",
            )
            prompt = self.query_one("#prompt", Input)
            prompt.value = text
            prompt.focus()
            return

        self._image_bank.clear()
        display = text

        self.append_user(display, images=turn_images or None)
        self._busy = True
        self._skip_steer_followup = False
        self._cancel_event = threading.Event()
        self._last_tool_items = []
        self._live_tool_items = []
        self._live_tool_summary = ""
        self._live_tool_block = None
        self.clear_stream()
        self.set_activity("thinking", "starting", True)
        self._sync_prompt_placeholder()
        self.run_turn(text, turn_images or None)

    @work(thread=True, exclusive=True)
    def run_turn(self, text: str, attachments: list[Any] | None = None) -> None:
        if not self._agent_ready.wait(timeout=180):
            self.call_from_thread(
                self.append_event,
                "agent start timeout (180s)",
                "bold red",
            )
            return
        if self._agent_error or self.agent is None:
            self.call_from_thread(
                self.append_event,
                f"agent unavailable: {self._agent_error or 'not built'}",
                "bold red",
            )
            return

        self._begin_turn_usage()
        sink = TextualStreamSink(self)
        config = {
            "configurable": {"thread_id": self.thread_id},
            "max_concurrency": self.settings.max_concurrency,
        }
        provider = provider_from_settings(self.settings)
        # None / empty attachments keep plain-string content (legacy path).
        atts = list(attachments or [])
        content = compose_user_content(
            text,
            attachments=atts if atts else None,
            provider=provider,
        )
        payload = {"messages": [{"role": "user", "content": content}]}
        try:
            result = stream_agent(
                self.agent,
                payload,
                config,
                token_stream=self.settings.token_stream,
                prefer_async=True,
                max_concurrency=self.settings.max_concurrency,
                sink=sink,
                cancel_event=self._cancel_event,
            )
            if getattr(result, "cancelled", False):
                self._skip_steer_followup = True
                self.call_from_thread(
                    self.append_event,
                    "已终止（上下文已保留）。可继续输入。",
                    "yellow",
                )
                return
            # Session token totals for chrome: input / cache / output.
            if (
                result.input_tokens
                or result.output_tokens
                or getattr(result, "cache_tokens", 0)
                or result.total_tokens
                or getattr(result, "last_input_tokens", 0)
            ):
                # Idempotent with live note_usage: baseline + turn totals.
                self.call_from_thread(
                    self.apply_turn_usage,
                    turn_input=int(result.input_tokens or 0),
                    turn_output=int(result.output_tokens or 0),
                    turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                    last_input=int(
                        getattr(result, "last_input_tokens", 0)
                        or result.input_tokens
                        or 0
                    ),
                    last_output=int(
                        getattr(result, "last_output_tokens", 0)
                        or result.output_tokens
                        or 0
                    ),
                    last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                )

            if getattr(result, "compact_events", 0):
                self.call_from_thread(
                    self.append_event,
                    f"context compacted ×{result.compact_events}",
                    "dim",
                )

            if not result.streamed_answer:
                answer = result.final_text or extract_last_ai_text(result.state)
                if answer:
                    self.call_from_thread(self.commit_answer, answer)
                elif getattr(result, "interrupted", False):
                    self.call_from_thread(
                        self.append_event,
                        "HITL: use /approve or /reject",
                        "yellow",
                    )
                else:
                    self.call_from_thread(self.append_event, "(empty response)", "dim")
            elif getattr(result, "interrupted", False):
                self.call_from_thread(
                    self.append_event,
                    "HITL: use /approve or /reject",
                    "yellow",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"ERROR: {exc}", "bold red")
        finally:
            self.call_from_thread(self._turn_done)

    @work(thread=True, exclusive=True)
    def run_resume(self, action: str, message: str | None = None) -> None:
        """Resume graph after /approve or /reject."""
        self._begin_turn_usage()
        from synapse.hitl import (
            build_decisions,
            build_resume_payload,
            extract_pending_interrupt,
            format_interrupt_lines,
        )

        sink = TextualStreamSink(self)
        # Allow Esc to abort resume stream as well.
        self._cancel_event = threading.Event()
        config = {
            "configurable": {"thread_id": self.thread_id},
            "max_concurrency": self.settings.max_concurrency,
        }
        try:
            pending = extract_pending_interrupt(self.agent, config)
            if pending is None or (not pending.actions and not pending.raw):
                self.call_from_thread(self.append_event, "no pending approval", "yellow")
                return
            for line in format_interrupt_lines(pending):
                self.call_from_thread(self.append_event, line, "dim")
            decisions = build_decisions(pending, action=action, message=message)
            payload = build_resume_payload(decisions)
            result = stream_agent(
                self.agent,
                payload,
                config,
                token_stream=self.settings.token_stream,
                prefer_async=True,
                max_concurrency=self.settings.max_concurrency,
                sink=sink,
                cancel_event=self._cancel_event,
            )
            if getattr(result, "cancelled", False):
                self._skip_steer_followup = True
                self.call_from_thread(
                    self.append_event,
                    "已终止（上下文已保留）。可继续输入。",
                    "yellow",
                )
                return
            if (
                result.input_tokens
                or result.output_tokens
                or getattr(result, "cache_tokens", 0)
                or result.total_tokens
                or getattr(result, "last_input_tokens", 0)
            ):
                # Idempotent with live note_usage: baseline + turn totals.
                self.call_from_thread(
                    self.apply_turn_usage,
                    turn_input=int(result.input_tokens or 0),
                    turn_output=int(result.output_tokens or 0),
                    turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                    last_input=int(
                        getattr(result, "last_input_tokens", 0)
                        or result.input_tokens
                        or 0
                    ),
                    last_output=int(
                        getattr(result, "last_output_tokens", 0)
                        or result.output_tokens
                        or 0
                    ),
                    last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                )
            if not result.streamed_answer:
                answer = result.final_text or extract_last_ai_text(result.state)
                if answer:
                    self.call_from_thread(self.commit_answer, answer)
            if getattr(result, "interrupted", False):
                self.call_from_thread(
                    self.append_event,
                    "still waiting for approval — /approve or /reject",
                    "yellow",
                )
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.append_event, f"ERROR: {exc}", "bold red")
        finally:
            self.call_from_thread(self._turn_done)

    def _turn_done(self) -> None:
        self._busy = False
        try:
            self._commit_live_tools_to_log()
        except Exception:  # noqa: BLE001
            pass
        self.clear_stream()
        self.set_activity("idle", "ready", True)
        self.query_one("#prompt", Input).focus()
        # If the model finished without another tool/model step, apply leftover
        # guidance as a follow-up turn (unless the run was Esc-cancelled).
        if getattr(self, "_skip_steer_followup", False):
            self._skip_steer_followup = False
            self._note_session_recap_turn()
            return
        # Capture snapshot before steer follow-up may start another busy turn.
        self._note_session_recap_turn()
        self._maybe_followup_steer()

    def _note_session_recap_turn(self) -> None:
        """Remember latest turn facts for idle recap."""
        user_text = ""
        if self._user_turns:
            user_text = getattr(self._user_turns[-1], "full_text", "") or ""
        try:
            self._session_recap.note_turn_done(
                time.monotonic(),
                user_text=user_text,
                tool_summary=self._last_tool_summary or "",
                tool_items=list(self._last_tool_items or []),
                answer_text=self._last_answer_text or "",
                turn_count=len(self._user_turns),
            )
        except Exception:  # noqa: BLE001
            pass

    def _prompt_has_draft(self) -> bool:
        try:
            prompt = self.query_one("#prompt", Input)
            return bool((prompt.value or "").strip())
        except Exception:  # noqa: BLE001
            return False

    def _maybe_show_session_recap(self) -> None:
        """After idle, mount one recap line (no slash command)."""
        if self._busy:
            return
        try:
            line = self._session_recap.try_fire(
                time.monotonic(),
                busy=self._busy,
                draft_nonempty=self._prompt_has_draft(),
            )
        except Exception:  # noqa: BLE001
            return
        if not line:
            return
        self.append_event(line, "dim")

    def _maybe_followup_steer(self) -> None:
        q = get_agent_steer_queue(self.agent)
        if q is None or q.peek_count() <= 0:
            return
        items = q.drain()
        content = format_steer_message(items)
        if not content:
            return
        self.append_event(
            f"applying {len(items)} queued guidance note(s) as follow-up…",
            "cyan",
        )
        self.append_user(f"[steer follow-up] {'; '.join(items)}")
        self._busy = True
        self._skip_steer_followup = False
        self._cancel_event = threading.Event()
        self.clear_stream()
        self.set_activity("thinking", "steer follow-up", True)
        self.run_turn(content, None)

def run_tui(
    *,
    settings: Any,
    thread_id: str | None = None,
    env_path: Path | None = None,
    project_root: Path | None = None,
    cli_model: str | None = None,
) -> None:
    """Launch the Textual app; agent build is deferred off the UI thread by default."""
    root = project_root or Path.cwd()
    tid = thread_id or "pending"
    try:
        from synapse.sessions import (
            SessionStore,
            apply_binding_to_settings,
            binding_from_settings,
            pick_startup_thread_id,
            resolve_startup_binding,
        )

        store = SessionStore(settings.resolved_sessions_path())
        try:
            store.prune_empty(except_ids=set())
        except Exception:  # noqa: BLE001
            pass
        tid, resumed = pick_startup_thread_id(store, thread_id, resume_last=True)
        binding = resolve_startup_binding(
            store, thread_id=tid if resumed else None, cli_model=cli_model
        )
        if binding is not None:
            apply_binding_to_settings(settings, binding)
        bind = binding_from_settings(settings)
        store.set_last_model_binding(bind)
    except Exception:  # noqa: BLE001
        from synapse.sessions import allocate_thread_id

        tid = thread_id or allocate_thread_id()

    defer = bool(getattr(settings, "tui_defer_agent", True))
    agent = None
    if not defer:
        agent = build_coding_agent(
            settings,
            project_root=root,
            load_mcp=bool(settings.enable_mcp)
            and bool(getattr(settings, "mcp_eager", False)),
        )
        if settings.enable_mcp and not getattr(agent, "_coding_mcp_attached", True):
            from synapse.agent import attach_mcp_to_agent

            agent = attach_mcp_to_agent(settings, agent, project_root=root)

    app = CodingAgentApp(
        agent=agent,
        settings=settings,
        thread_id=tid,
        env_path=env_path,
        project_root=root,
        defer_agent_build=defer,
    )
    app.run()