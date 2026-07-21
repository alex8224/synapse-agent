"""Streaming UI helpers for CLI output.

Supports:
- token-level streaming (`messages` mode)
- reasoning / thinking stream (DeepSeek etc.)
- intermediate assistant messages between tool rounds
- concurrent multi-tool progress + subagent heartbeat
- compact tool results (params on call; status only on return)

Rendering is pluggable via ``StreamSink``:
- default: ``RichStreamSink`` (CLI)
- TUI: ``synapse.ui.tui.TextualStreamSink``
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.markdown import Markdown as _Markdown
from rich.markdown import MarkdownElement, TableElement as _TableElement
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich import box

from synapse.context_compact import (
    is_context_compact_text,
    is_lc_summarization_message,
    is_stream_meta_summarization,
)
from synapse.pathing import summarize_tool_result
from synapse.ui.sink import StreamSink, sink_supports_tool_items
from synapse.ui.timeline import (
    build_tool_item,
    content_to_text,
    is_error_status,
    is_todo_tool,
    item_label,
    match_tool_result,
    truncate_preview,
)

# soft_wrap keeps long lines readable; force_terminal helps Windows color.
# highlight=False avoids over-styling plain identifiers in non-markdown UI.
console = Console(highlight=False, soft_wrap=True, emoji=False)


def _theme():
    try:
        from synapse.ui.theme import get_theme

        return get_theme()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# LaTeX math → Unicode art (powered by TeXicode)
# ---------------------------------------------------------------------------

_LATEX_MATH_RE = re.compile(
    r"\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\)|\\begin\{.*?\}.*?\\end\{.*?\}",
    re.DOTALL,
)


def _replace_latex(match: re.Match) -> str:
    """Render a single LaTeX math block as Unicode art via TeXicode."""
    tex_block = match.group(0)
    if tex_block.startswith("$$"):
        clean = tex_block[2:-2]
        ctx = "md_block"
    elif tex_block.startswith("\\["):
        clean = tex_block[2:-2]
        ctx = "md_block"
    elif tex_block.startswith("\\("):
        clean = tex_block[2:-2]
        ctx = "md_inline"
    elif tex_block.startswith("\\begin"):
        clean = tex_block
        ctx = "md_block"
    else:
        clean = tex_block[1:-1]
        ctx = "md_inline"
    try:
        from texicode.pipeline import render_tex

        return render_tex(clean, False, False, ctx, {"fonts": "normal"})
    except Exception:
        return tex_block


def render_math_in_text(text: str) -> str:
    """Replace $$...$$ / $...$ / \\[...\\] / \\(...\\) with Unicode math art."""
    return _LATEX_MATH_RE.sub(_replace_latex, text)


# ---------------------------------------------------------------------------
# Rich Markdown rendering
# ---------------------------------------------------------------------------


class _FullTableElement(_TableElement):
    """Rich table element with full rounded borders instead of just a header line."""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        table = Table(
            box=box.ROUNDED,
            pad_edge=False,
            style="markdown.table.border",
            show_edge=True,
            show_lines=True,  # draw grid lines between body rows/cells
            collapse_padding=True,
        )

        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading)

        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)

        yield table


class _FullBorderMarkdown(_Markdown):
    """Rich Markdown that renders tables with full rounded borders."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **_Markdown.elements,
        "table_open": _FullTableElement,
    }


def render_markdown(text: str) -> _Markdown:
    """Build a Rich Markdown renderable for assistant answers."""
    theme = _theme()
    code_theme = getattr(theme, "code_theme", None) or "monokai"
    # Render LaTeX math blocks before passing to Rich Markdown.
    text = render_math_in_text(text)
    return _FullBorderMarkdown(
        text or "(empty response)",
        code_theme=code_theme,
        hyperlinks=True,
    )


def print_markdown(text: str) -> None:
    """Print markdown body without a panel."""
    console.print(render_markdown(text))


def print_banner(workspace: str, model: str, require_approval: bool) -> None:
    approval = "ON" if require_approval else "OFF (auto-pass)"
    theme = _theme()
    border = getattr(theme, "rich_info_border", None) or "blue"
    console.print(
        Panel.fit(
            f"[bold]Coding Agent[/bold]\n"
            f"workspace: [cyan]{workspace}[/cyan]\n"
            f"model: [green]{model}[/green]\n"
            f"approval: [yellow]{approval}[/yellow]\n"
            f"backend: LocalShell · parallel tools · token/reasoning stream",
            border_style=border,
        )
    )


def print_user(text: str) -> None:
    theme = _theme()
    style = getattr(theme, "rich_user", None) or "bold cyan"
    console.print(Text(f"You: {text}", style=style))


def print_error(message: str) -> None:
    theme = _theme()
    style = getattr(theme, "rich_error", None) or "bold red"
    # Keep the ERROR: prefix visible even if theme uses hex colors.
    if " " in style and not style.startswith("bold "):
        console.print(f"[{style}]ERROR:[/{style}] {message}")
    else:
        console.print(f"[{style}]ERROR:[/{style}] {message}")


def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def print_final(text: str) -> None:
    """Print the final assistant answer with markdown rendering."""
    theme = _theme()
    border = getattr(theme, "rich_ok_border", None) or "green"
    console.print()
    console.print(
        Panel(
            render_markdown(text),
            title="Assistant",
            border_style=border,
            padding=(0, 1),
        )
    )


def _looks_like_middleware_update(data: Any) -> bool:
    """True when an updates payload is only middleware jump metadata.

    LangGraph emits maps like ``{"SkillsMiddleware.before_agent": None, ...}``
    when hooks return no state patch. These must never become the answer body.
    """
    if not isinstance(data, dict) or not data:
        return False
    if "messages" in data:
        return False
    keys = [str(k) for k in data]
    hook_markers = (".before_agent", ".after_agent", ".before_model", ".after_model")
    hookish = sum(1 for k in keys if any(m in k for m in hook_markers))
    if hookish >= max(1, len(keys) // 2):
        return True
    # All values empty/None and no known agent state channels.
    state_keys = {"messages", "files", "todos", "structured_response", "jump_to"}
    if any(k in state_keys for k in data):
        return False
    return all(v is None or v == {} or v == [] for v in data.values())


def extract_last_ai_text(result: dict[str, Any] | Any) -> str:
    """Best-effort extraction of the final assistant message text.

    Only reads a real ``messages`` channel. Never stringifies middleware jump
    maps or other non-state updates (that used to leak into the TUI as the
    assistant answer).
    """
    if not isinstance(result, dict) or not result:
        return ""
    if _looks_like_middleware_update(result):
        return ""
    if "messages" not in result:
        return ""
    messages = result.get("messages") or []
    if not messages:
        return ""
    for msg in reversed(messages):
        if not _is_ai_message(msg):
            continue
        text = _normalize_content(getattr(msg, "content", "")).strip()
        if text:
            return text
    last = messages[-1]
    content = getattr(last, "content", None)
    if content is None:
        return ""
    text = content if isinstance(content, str) else _normalize_content(content)
    return text.strip()


def _normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = str(block.get("type") or "")
                if btype in {"reasoning", "thinking"}:
                    continue  # handled separately
                if btype == "text" or "text" in block:
                    parts.append(str(block.get("text", "")))
            else:
                text = getattr(block, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content)


def _extract_reasoning(msg: Any) -> str:
    """Extract model reasoning / thinking text from common provider fields."""
    parts: list[str] = []

    ak = getattr(msg, "additional_kwargs", None) or {}
    if isinstance(ak, dict):
        for key in ("reasoning_content", "reasoning", "thinking", "thought"):
            val = ak.get(key)
            if val:
                parts.append(str(val))

    rm = getattr(msg, "response_metadata", None) or {}
    if isinstance(rm, dict):
        for key in ("reasoning_content", "reasoning", "thinking"):
            val = rm.get(key)
            if val:
                parts.append(str(val))

    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if btype in {"reasoning", "thinking"}:
                parts.append(str(block.get("text") or block.get("reasoning") or ""))

    for key in ("reasoning_content", "reasoning"):
        val = getattr(msg, key, None)
        if val:
            parts.append(str(val))

    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "".join(out)


def _shorten(text: str, limit: int = 160) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _format_tool_args(args: Any) -> str:
    return _shorten(repr(args), 240)


@dataclass
class StreamResult:
    state: dict[str, Any] = field(default_factory=dict)
    final_text: str = ""
    tool_calls: int = 0
    elapsed_s: float = 0.0
    streamed_answer: bool = False
    reasoning_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0  # cache hit / cache_read tokens
    total_tokens: int = 0
    # Last model-call usage in this turn (not summed). Topbar occupancy uses these.
    last_input_tokens: int = 0
    last_output_tokens: int = 0
    last_cache_tokens: int = 0
    cancelled: bool = False  # user abort (ESC / cancel_event)
    interrupted: bool = False  # graph paused for HITL approval
    compact_events: int = 0  # context-compaction summaries hidden from UI


class _ActivityLine:
    """Animated status with heartbeat so long waits never look frozen."""

    _LABELS = {
        "thinking": "thinking",
        "tool": "running tools",
        "subagent": "running subagent",
        "model": "waiting for model",
        "stream": "streaming",
        "reasoning": "reasoning",
        "done": "done",
    }

    def __init__(self) -> None:
        self._phase = "thinking"
        self._detail = "waiting for model"
        self._started_at = time.time()
        self._live: Live | None = None
        self._stop_hb = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._spinner = Spinner(
            "line",
            text=Text(self._format_text(), style="cyan"),
            style="bold cyan",
            speed=1.2,
        )

    def _format_text(self) -> str:
        label = self._LABELS.get(self._phase, self._phase)
        elapsed = max(0.0, time.time() - self._started_at)
        base = f"{label} — {self._detail}" if self._detail else label
        return f"{base}  ({elapsed:0.0f}s)"

    def _apply_text(self) -> None:
        with self._lock:
            self._spinner.update(text=Text(self._format_text(), style="cyan"))

    def _heartbeat(self) -> None:
        while not self._stop_hb.wait(0.08):
            self._apply_text()
            live = self._live
            if live is not None:
                try:
                    live.refresh()
                except Exception:  # noqa: BLE001
                    pass

    def start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._phase = phase
        self._detail = detail
        self._started_at = time.time()
        self._apply_text()
        if self._live is None:
            self._live = Live(
                self._spinner,
                console=console,
                refresh_per_second=16,
                transient=True,
                auto_refresh=True,
            )
            self._live.start()
        if self._hb_thread is None or not self._hb_thread.is_alive():
            self._stop_hb.clear()
            self._hb_thread = threading.Thread(
                target=self._heartbeat, name="activity-heartbeat", daemon=True
            )
            self._hb_thread.start()

    def update(self, phase: str, detail: str = "", *, reset_timer: bool = False) -> None:
        if detail.startswith("node="):
            if self._live is None:
                self.start(phase, "working")
            else:
                self._apply_text()
            return
        if phase == self._phase and detail == self._detail and not reset_timer:
            self._apply_text()
            return
        self._phase = phase
        self._detail = detail
        if reset_timer:
            self._started_at = time.time()
        self._apply_text()
        if self._live is None:
            self.start(phase, detail)

    def stop(self) -> None:
        self._stop_hb.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=0.3)
            self._hb_thread = None
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None


class _StreamPrinter:
    """Owns console layout for reasoning + assistant text.

    Design (low overhead):
    - During tokens: **buffer only** + lightweight activity status.
      No Rich Live, no per-token Markdown re-render (avoids flicker/cost).
    - On commit: print permanent Markdown **once**.
    - Dedup by msg_id + normalized text so final content is not repeated.
    """

    def __init__(self, activity: _ActivityLine) -> None:
        self.activity = activity
        self.reasoning_open = False
        self.answer_open = False
        self.streamed_answer = False
        self.streamed_reasoning = False
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self._printed_complete_texts: set[str] = set()
        self._token_streamed_msg_ids: set[str] = set()
        self._open_msg_id: str | None = None
        self._open_answer_parts: list[str] = []
        self._open_reasoning_parts: list[str] = []
        self._markdown_rendered_ids: set[str] = set()
        self._last_committed_answer = ""
        self._reasoning_committed_norms: set[str] = set()
        self._last_status_at = 0.0
        self._status_interval = 0.35

    def _stop_activity(self) -> None:
        self.activity.stop()

    @staticmethod
    def _norm_text(text: str) -> str:
        return " ".join((text or "").split())

    def _answer_group(self, text: str):
        from rich.console import Group

        body = text if text.strip() else "…"
        return Group(
            Text("assistant:", style="bold green"),
            render_markdown(body),
        )

    def _reasoning_group(self, text: str):
        from rich.console import Group

        body = text if text.strip() else "…"
        return Group(
            Text("reasoning:", style="dim italic"),
            render_markdown(body),
        )

    def _status(self, phase: str, detail: str) -> None:
        """Throttle activity-line updates so streaming stays cheap."""
        now = time.time()
        if (now - self._last_status_at) < self._status_interval:
            return
        self._last_status_at = now
        try:
            self.activity.update(phase, detail)
        except Exception:  # noqa: BLE001
            pass

    def close_reasoning(self) -> None:
        """Seal reasoning buffer and commit permanent markdown once."""
        if not self.reasoning_open and not self._open_reasoning_parts:
            return
        text = "".join(self._open_reasoning_parts).strip()
        self.reasoning_open = False
        self._open_reasoning_parts = []
        if not text:
            return

        norm = self._norm_text(text)
        if norm and norm in self._reasoning_committed_norms:
            return

        self._stop_activity()
        console.print()
        console.print(self._reasoning_group(text))
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass
        if norm:
            self._reasoning_committed_norms.add(norm)
        self.streamed_reasoning = True

    def close_answer(self) -> None:
        """Seal token buffer flag; content is committed via flush/complete."""
        self.answer_open = False
        if self._open_msg_id:
            self._token_streamed_msg_ids.add(self._open_msg_id)

    def write_reasoning(self, text: str) -> None:
        """Buffer reasoning tokens; render Markdown only on close."""
        if not text:
            return
        if self._open_answer_parts:
            self.flush_buffered_answer()
        self.close_answer()
        if not self.reasoning_open:
            self.reasoning_open = True
            self.streamed_reasoning = True
            self._open_reasoning_parts = []
        self._open_reasoning_parts.append(text)
        self.reasoning_buf.append(text)
        n = sum(len(p) for p in self._open_reasoning_parts)
        self._status("thinking", f"reasoning {n}c")

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        """Buffer answer tokens; render Markdown only on complete/flush."""
        if not text:
            return
        if msg_id and msg_id in self._markdown_rendered_ids:
            return
        self.close_reasoning()
        if not self.answer_open:
            if (
                msg_id
                and msg_id in self._token_streamed_msg_ids
                and self._last_committed_answer
            ):
                return
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and self._open_msg_id and msg_id != self._open_msg_id:
            self.flush_buffered_answer()
            if msg_id in self._markdown_rendered_ids:
                return
            self.answer_open = True
            self._open_answer_parts = []
            self._open_msg_id = msg_id
        elif msg_id and not self._open_msg_id:
            self._open_msg_id = msg_id

        self._open_answer_parts.append(text)
        if msg_id:
            self._token_streamed_msg_ids.add(msg_id)
        self.streamed_answer = True
        n = sum(len(p) for p in self._open_answer_parts)
        self._status("model", f"composing {n}c")

    def _print_markdown_answer(self, text: str, *, msg_id: str | None = None) -> None:
        """Commit one assistant message as permanent Markdown (exactly once)."""
        text = text.strip()
        if not text:
            return

        norm = self._norm_text(text)
        if msg_id and msg_id in self._markdown_rendered_ids:
            self.answer_open = False
            self._open_answer_parts = []
            self._open_msg_id = None
            return
        if norm and (
            norm in self._printed_complete_texts
            or norm == self._norm_text(self._last_committed_answer)
        ):
            self.answer_open = False
            self._open_answer_parts = []
            self._open_msg_id = None
            self.streamed_answer = True
            if msg_id:
                self._markdown_rendered_ids.add(msg_id)
            return

        self._stop_activity()
        self.close_reasoning()
        self.answer_open = False
        self._open_answer_parts = []
        self._open_msg_id = None

        console.print()
        console.print(self._answer_group(text))
        try:
            console.file.flush()
        except Exception:  # noqa: BLE001
            pass

        self.answer_buf.append(text)
        self._last_committed_answer = text
        if norm:
            self._printed_complete_texts.add(norm)
        self.streamed_answer = True
        if msg_id:
            self._markdown_rendered_ids.add(msg_id)
            self._token_streamed_msg_ids.add(msg_id)

    def write_answer_complete(
        self,
        text: str,
        *,
        msg_id: str | None = None,
    ) -> None:
        """Complete an assistant message; commit permanent markdown once."""
        text = text.strip()
        if not text:
            return
        self._print_markdown_answer(text, msg_id=msg_id)

    def flush_buffered_answer(self) -> None:
        """Flush token buffer when tools or reasoning interrupt."""
        buffered = "".join(self._open_answer_parts).strip()
        msg_id = self._open_msg_id
        self._open_answer_parts = []
        self.answer_open = False
        self._open_msg_id = None
        if buffered:
            self._print_markdown_answer(buffered, msg_id=msg_id)

    def finalize_line(self) -> None:
        self.close_reasoning()
        self.flush_buffered_answer()


class RichStreamSink:
    """CLI StreamSink backed by Rich Live + console printing."""

    def __init__(self) -> None:
        self._activity = _ActivityLine()
        self._printer = _StreamPrinter(self._activity)

    @property
    def streamed_answer(self) -> bool:
        return self._printer.streamed_answer

    @streamed_answer.setter
    def streamed_answer(self, value: bool) -> None:
        self._printer.streamed_answer = value

    @property
    def answer_buf(self) -> list[str]:
        return self._printer.answer_buf

    @property
    def reasoning_buf(self) -> list[str]:
        return self._printer.reasoning_buf

    @property
    def streamed_reasoning(self) -> bool:
        return self._printer.streamed_reasoning

    @streamed_reasoning.setter
    def streamed_reasoning(self, value: bool) -> None:
        self._printer.streamed_reasoning = value

    def activity_start(self, phase: str = "thinking", detail: str = "waiting for model") -> None:
        self._activity.start(phase, detail)

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
    ) -> None:
        self._activity.update(phase, detail, reset_timer=reset_timer)

    def activity_stop(self) -> None:
        self._activity.stop()

    def write_reasoning(self, text: str) -> None:
        self._printer.write_reasoning(text)

    def close_reasoning(self) -> None:
        self._printer.close_reasoning()

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self._printer.write_answer_token(text, msg_id=msg_id)

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        self._printer.write_answer_complete(text, msg_id=msg_id)

    def finalize_line(self) -> None:
        self._printer.finalize_line()

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        if parallel:
            console.print(
                f"[bold magenta]→ tools x{len(calls)} (parallel)[/bold magenta]"
            )
        else:
            console.print("[bold magenta]→ tool[/bold magenta]")
        for call in calls:
            name = _tool_call_name(call)
            args = _tool_call_args(call)
            console.print(
                f"  [yellow]{name}[/yellow] "
                f"[dim]{_format_tool_args(args)}[/dim]"
            )

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        prefix = "sub" if sub else ""
        style = "red" if status.lower().startswith("error") else "green"
        console.print()
        console.print(
            f"[dim]←{prefix}[/dim] [yellow]{name}[/yellow] "
            f"[{style}]{status}[/{style}]"
        )

    def info(self, message: str) -> None:
        print_info(message)

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
        """Optional live token chrome (TUI overrides)."""
        del turn_input, turn_output, turn_cache, last_input, last_output, last_cache


def _chunk_text(msg_chunk: Any) -> str:
    content = getattr(msg_chunk, "content", None)
    if content is None and isinstance(msg_chunk, dict):
        content = msg_chunk.get("content")
    return _normalize_content(content)


def _is_tool_message(msg: Any) -> bool:
    """Detect tool result messages.

    LangChain ToolMessage.type is the short string ``\"tool\"`` (not ``toolmessage``).
    """
    type_name = (getattr(msg, "type", None) or "").lower()
    if type_name == "tool":
        return True
    cls_name = msg.__class__.__name__.lower()
    return cls_name == "toolmessage" or (
        "tool" in cls_name and "message" in cls_name
    )


def _is_ai_message(msg: Any) -> bool:
    type_name = (getattr(msg, "type", None) or "").lower()
    if type_name in {"ai", "aimessage", "aimessagechunk"}:
        return True
    cls_name = msg.__class__.__name__.lower()
    return cls_name in {"aimessage", "aimessagechunk"}


def _reasoning_token_count(msg: Any) -> int | None:
    usage = getattr(msg, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        details = getattr(usage, "output_token_details", None)
        if details is not None:
            val = getattr(details, "reasoning", None)
            return int(val) if val is not None else None
        return None
    details = usage.get("output_token_details") or {}
    if isinstance(details, dict) and details.get("reasoning") is not None:
        try:
            return int(details["reasoning"])
        except (TypeError, ValueError):
            return None
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _cache_tokens_from_details(details: Any) -> int:
    """Best-effort cache-hit tokens from provider detail objects/dicts."""
    if details is None:
        return 0
    keys = (
        "cache_read",
        "cache_read_tokens",
        "cache_hit",
        "cache_hit_tokens",
        "cached",
        "cached_tokens",
    )
    if isinstance(details, dict):
        for key in keys:
            if details.get(key) is not None:
                return _as_int(details.get(key))
        return 0
    for key in keys:
        val = getattr(details, key, None)
        if val is not None:
            return _as_int(val)
    return 0


def _extract_cache_tokens(msg: Any, usage: Any) -> int:
    """Extract cache-hit tokens from usage_metadata / response_metadata."""
    if usage is not None:
        if isinstance(usage, dict):
            cache = _cache_tokens_from_details(usage.get("input_token_details"))
            if cache:
                return cache
            cache = _cache_tokens_from_details(usage.get("input_tokens_details"))
            if cache:
                return cache
            for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
                if usage.get(key) is not None:
                    return _as_int(usage.get(key))
        else:
            cache = _cache_tokens_from_details(
                getattr(usage, "input_token_details", None)
            )
            if cache:
                return cache
            for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
                val = getattr(usage, key, None)
                if val is not None:
                    return _as_int(val)

    meta = getattr(msg, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        return 0
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if not isinstance(token_usage, dict):
        return 0
    details = token_usage.get("prompt_tokens_details") or token_usage.get(
        "input_tokens_details"
    )
    cache = _cache_tokens_from_details(details)
    if cache:
        return cache
    for key in ("cache_read_tokens", "cached_tokens", "cache_tokens"):
        if token_usage.get(key) is not None:
            return _as_int(token_usage.get(key))
    return 0


def _extract_usage(msg: Any) -> dict[str, int]:
    """Extract token usage from AIMessage usage_metadata (OpenAI-compatible format)."""
    empty: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_tokens": 0,
    }

    usage = getattr(msg, "usage_metadata", None)
    if usage is None:
        cache = _extract_cache_tokens(msg, None)
        if cache:
            empty["cache_tokens"] = cache
        return empty

    if not isinstance(usage, dict):
        return {
            "input_tokens": _as_int(getattr(usage, "input_tokens", 0)),
            "output_tokens": _as_int(getattr(usage, "output_tokens", 0)),
            "total_tokens": _as_int(getattr(usage, "total_tokens", 0)),
            "cache_tokens": _extract_cache_tokens(msg, usage),
        }

    return {
        "input_tokens": _as_int(usage.get("input_tokens", 0)),
        "output_tokens": _as_int(usage.get("output_tokens", 0)),
        "total_tokens": _as_int(usage.get("total_tokens", 0)),
        "cache_tokens": _extract_cache_tokens(msg, usage),
    }



def aggregate_usage_from_messages(messages: list[Any] | None) -> dict[str, int]:
    """Sum usage_metadata across AI messages; track last call values.

    Used when restoring a thread so the topbar can show historical totals
    without waiting for a new live turn.
    """
    total_in = 0
    total_out = 0
    total_cache = 0
    last_in = 0
    last_out = 0
    last_cache = 0
    seen: set[str] = set()
    for msg in messages or []:
        if not _is_ai_message(msg):
            continue
        msg_id = getattr(msg, "id", None)
        key = f"usage:{msg_id if msg_id else id(msg)}"
        if key in seen:
            continue
        u = _extract_usage(msg)
        if not (
            u.get("input_tokens")
            or u.get("output_tokens")
            or u.get("cache_tokens")
        ):
            continue
        seen.add(key)
        total_in += int(u.get("input_tokens") or 0)
        total_out += int(u.get("output_tokens") or 0)
        total_cache += int(u.get("cache_tokens") or 0)
        last_in = int(u.get("input_tokens") or 0)
        last_out = int(u.get("output_tokens") or 0)
        last_cache = int(u.get("cache_tokens") or 0)
    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_tokens": total_cache,
        "last_input_tokens": last_in,
        "last_output_tokens": last_out,
        "last_cache_tokens": last_cache,
    }



def _tool_call_name(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("name") or "?")
    return str(getattr(call, "name", "?"))


def _tool_call_args(call: Any) -> Any:
    if isinstance(call, dict):
        return call.get("args")
    return getattr(call, "args", {})


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or call.get("tool_call_id") or "")
    return str(getattr(call, "id", None) or getattr(call, "tool_call_id", None) or "")


def human_tool_label(call: Any) -> str:
    """Prefer model intent (via item_label) over raw tool name/args."""
    name = _tool_call_name(call)
    args = _tool_call_args(call)
    label = item_label(name, args)
    return " ".join(str(label or name).split()).strip() or name


def human_nested_tools_detail(calls: list[Any], *, limit: int = 5) -> str:
    """Status text for concurrent nested tool calls."""
    labels: list[str] = []
    for call in calls[: max(1, limit)]:
        labels.append(human_tool_label(call))
    more = len(calls) - len(labels)
    text = " · ".join(labels)
    if more > 0:
        text = f"{text} · +{more}"
    return text


def checkpointer_supports_async(checkpointer: Any) -> bool:
    """Whether a LangGraph checkpointer is safe for agent.astream.

    Sync ``SqliteSaver`` raises RuntimeError under async graph methods.
    """
    if checkpointer is None:
        return True
    cls = type(checkpointer)
    name = cls.__name__
    module = cls.__module__ or ""
    if name == "SqliteSaver" and ".aio" not in module:
        return False
    if name.startswith("Async") and "Saver" in name:
        return True
    # MemorySaver and most modern savers expose aget_tuple.
    if callable(getattr(checkpointer, "aget_tuple", None)):
        return True
    if callable(getattr(checkpointer, "aget", None)):
        return True
    return True


def _bound_async_loop(agent: Any) -> asyncio.AbstractEventLoop | None:
    """Event loop bound to AsyncSqliteSaver / agent async runtime, if any."""
    runtime = getattr(agent, "_coding_async_runtime", None)
    if runtime is not None:
        loop = getattr(runtime, "loop", None)
        if loop is not None:
            try:
                if loop.is_running():
                    return loop
            except Exception:  # noqa: BLE001
                pass
    cp = getattr(agent, "_coding_checkpointer", None)
    loop = getattr(cp, "loop", None) if cp is not None else None
    if loop is not None:
        try:
            if loop.is_running():
                return loop
        except Exception:  # noqa: BLE001
            pass
    return None


def _is_sync_only_checkpointer_error(exc: BaseException) -> bool:
    """True for SqliteSaver/async mismatch errors that should fall back to sync stream."""
    msg = str(exc).lower()
    if "does not support async" in msg:
        return True
    if "asyncsqlitesaver" in msg and "aiosqlite" in msg:
        return True
    if "sqlitesaver" in msg and "async" in msg:
        return True
    return False


def _normalize_stream_item(item: Any) -> tuple[str, Any, tuple[str, ...]]:
    ns: tuple[str, ...] = ()

    if isinstance(item, dict) and "type" in item and "data" in item:
        mode = str(item.get("type") or "updates")
        data = item.get("data")
        raw_ns = item.get("ns") or item.get("namespace") or ()
        if raw_ns:
            ns = tuple(str(x) for x in raw_ns)
        return mode, data, ns

    if isinstance(item, tuple):
        if len(item) == 3:
            maybe_ns, mode, data = item
            if isinstance(maybe_ns, (tuple, list)):
                return str(mode), data, tuple(str(x) for x in maybe_ns)
            return str(maybe_ns), mode, ()
        if len(item) == 2:
            a, b = item
            if isinstance(a, str) and a in {
                "messages",
                "updates",
                "values",
                "custom",
                "events",
                "debug",
            }:
                return a, b, ()
            if isinstance(a, (tuple, list)):
                return "updates", b, tuple(str(x) for x in a)
            return str(a), b, ()

    return "updates", item, ()


def _iter_stream_events(
    agent,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool,
    prefer_async: bool,
    subgraphs: bool,
    cancel_event: threading.Event | None = None,
) -> Iterator[tuple[str, Any, tuple[str, ...]]]:
    modes: list[str] = ["updates"]
    if token_stream:
        modes = ["messages", "updates"]

    def _put_norm(q: queue.Queue[Any], item: Any) -> None:
        q.put(_normalize_stream_item(item))

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if prefer_async and hasattr(agent, "astream"):
        q: queue.Queue[Any] = queue.Queue()
        error_box: list[BaseException] = []
        done_box: list[bool] = []

        async def _astream_once(**kwargs: Any):
            async for item in agent.astream(payload, config=config, **kwargs):
                if _cancelled():
                    break
                _put_norm(q, item)

        async def _produce() -> None:
            kwargs: dict[str, Any] = {
                "stream_mode": modes,
                "subgraphs": subgraphs,
            }
            try:
                await _astream_once(version="v2", **kwargs)
            except TypeError:
                try:
                    await _astream_once(**kwargs)
                except TypeError:
                    await _astream_once(stream_mode=modes)
            except asyncio.CancelledError:
                return
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
            finally:
                q.put(None)

        async def _main() -> None:
            prod = asyncio.create_task(_produce())
            if cancel_event is None:
                await prod
                return
            # Poll cancel so ESC can interrupt long model/tool waits.
            while not prod.done():
                if cancel_event.is_set():
                    prod.cancel()
                    try:
                        await prod
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    # Ensure consumer unblocks even if finally was skipped.
                    try:
                        q.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        q.put(None)
                    return
                await asyncio.sleep(0.05)
            await prod

        bound_loop = _bound_async_loop(agent)
        worker_thread: threading.Thread | None = None
        bound_future: Any | None = None

        if bound_loop is not None and bound_loop.is_running():
            # AsyncSqliteSaver path: schedule on the checkpointer's loop.
            try:
                bound_future = asyncio.run_coroutine_threadsafe(_main(), bound_loop)
            except BaseException as exc:  # noqa: BLE001
                error_box.append(exc)
                q.put(None)
        else:
            # MemorySaver / no bound loop: dedicated worker + asyncio.run.
            def _runner() -> None:
                try:
                    asyncio.run(_main())
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)
                    try:
                        q.put_nowait(None)
                    except Exception:  # noqa: BLE001
                        q.put(None)
                finally:
                    done_box.append(True)

            worker_thread = threading.Thread(
                target=_runner, name="agent-astream", daemon=True
            )
            worker_thread.start()

        while True:
            if _cancelled():
                # Unblock promptly; producer task is being cancelled in parallel.
                try:
                    item = q.get(timeout=0.15)
                except queue.Empty:
                    yield "__cancelled__", None, ()
                    break
                if item is None:
                    yield "__cancelled__", None, ()
                    break
                yield item
                continue
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                # If bound future finished without sentinel, stop.
                if bound_future is not None and bound_future.done() and q.empty():
                    break
                yield "__heartbeat__", None, ()
                continue
            if item is None:
                if _cancelled():
                    yield "__cancelled__", None, ()
                break
            yield item

        if worker_thread is not None:
            worker_thread.join(timeout=1.5)
        if bound_future is not None:
            try:
                bound_future.result(timeout=1.5)
            except Exception as exc:  # noqa: BLE001
                if not error_box and not _cancelled():
                    error_box.append(exc)
        if error_box:
            err = error_box[0]
            # Cancellation-induced errors are expected; ignore soft failures.
            if _cancelled() or isinstance(err, asyncio.CancelledError):
                return
            # Fall through to sync stream when:
            # - TypeError: astream kwargs (version/subgraphs) not supported
            # - sync-only checkpointer used under astream (SqliteSaver)
            # Other runtime/API failures must still surface.
            if isinstance(err, TypeError) or _is_sync_only_checkpointer_error(err):
                pass
            else:
                raise err
        else:
            return

    def _sync_iter(**kwargs: Any):
        return agent.stream(payload, config=config, **kwargs)

    sync_errors: list[BaseException] = []
    for attempt in (
        {"stream_mode": modes, "subgraphs": subgraphs, "version": "v2"},
        {"stream_mode": modes, "subgraphs": subgraphs},
        {"stream_mode": modes, "version": "v2"},
        {"stream_mode": modes},
        {"stream_mode": "updates"},
    ):
        try:
            for item in _sync_iter(**attempt):
                if _cancelled():
                    yield "__cancelled__", None, ()
                    return
                yield _normalize_stream_item(item)
            return
        except TypeError as exc:
            sync_errors.append(exc)
            continue
        except asyncio.CancelledError:
            if _cancelled():
                yield "__cancelled__", None, ()
                return
            raise
    if sync_errors:
        raise sync_errors[-1]


def stream_agent(
    agent,
    payload: Any,
    config: dict[str, Any],
    *,
    token_stream: bool = True,
    prefer_async: bool = True,
    max_concurrency: int = 8,
    subgraphs: bool = True,
    sink: StreamSink | None = None,
    cancel_event: threading.Event | None = None,
) -> StreamResult:
    """Stream agent with reasoning + answer tokens and tool/subagent progress.

    Args:
        payload: User message dict or LangGraph ``Command`` (HITL resume).
        sink: Optional UI consumer. Defaults to Rich CLI sink.
    """
    # Sync-only SqliteSaver cannot astream. AsyncSqliteSaver + process runtime can.
    if prefer_async:
        cp = getattr(agent, "_coding_checkpointer", None)
        if not checkpointer_supports_async(cp):
            prefer_async = False

    started = time.time()
    final: dict[str, Any] = {}
    printed_ids: set[str] = set()
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    last_input_tokens = 0
    last_output_tokens = 0
    last_cache_tokens = 0
    _usage_seen: set[str] = set()  # dedupe usage from repeated messages
    sink = sink or RichStreamSink()
    active_tools: list[str] = []
    use_tool_items = sink_supports_tool_items(sink)
    pending_tool_items: list[Any] = []
    tool_group_seq = 0
    # Nested subagent: tool name / call id -> human intent label.
    sub_tool_labels: dict[str, str] = {}

    run_config = dict(config or {})
    run_config.setdefault("max_concurrency", max_concurrency)
    if "configurable" in (config or {}):
        run_config["configurable"] = dict(config["configurable"])

    sink.activity_start("thinking", "waiting for model")
    cancelled = False
    compact_announced = False
    suppress_msg_ids: set[str] = set()
    compact_events = 0

    def _note_compact() -> None:
        nonlocal compact_announced, compact_events
        compact_events += 1
        if compact_announced:
            return
        compact_announced = True
        try:
            sink.info("context compacted (hidden)")
        except Exception:  # noqa: BLE001
            pass

    def _drop_leaked_stream() -> None:
        for name in ("clear_stream", "close_stream", "finalize_line"):
            fn = getattr(sink, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                break
        for attr in ("answer_buf", "_open_answer"):
            buf = getattr(sink, attr, None)
            if isinstance(buf, list):
                buf.clear()
        if hasattr(sink, "streamed_answer"):
            try:
                sink.streamed_answer = False
            except Exception:  # noqa: BLE001
                pass

    def _mark_cancelled() -> None:
        nonlocal cancelled
        cancelled = True
        # Best-effort: close open tool rows so the timeline does not stick on "running".
        if use_tool_items and pending_tool_items:
            for item in list(pending_tool_items):
                try:
                    item.status = "error"
                    item.error = True
                    sink.tool_item_finished(
                        item.id,
                        status="cancelled",
                        preview="cancelled",
                        error=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            pending_tool_items.clear()
            try:
                sink.tool_group_closed(f"g{tool_group_seq}")
            except Exception:  # noqa: BLE001
                pass

    try:
        for mode, chunk, ns in _iter_stream_events(
            agent,
            payload,
            run_config,
            token_stream=token_stream,
            prefer_async=prefer_async,
            subgraphs=subgraphs,
            cancel_event=cancel_event,
        ):
            if mode == "__cancelled__" or (
                cancel_event is not None and cancel_event.is_set() and mode == "__heartbeat__"
            ):
                _mark_cancelled()
                break
            if cancel_event is not None and cancel_event.is_set():
                _mark_cancelled()
                break
            if mode == "__heartbeat__":
                if active_tools:
                    phase = "subagent" if "task" in active_tools else "tool"
                    if phase == "subagent":
                        # Keep sticky intent; sink coalesces/delays subagent text.
                        sink.activity_update("subagent", "子代理运行中")
                    else:
                        label = ", ".join(active_tools[:3])
                        sink.activity_update(phase, f"{label} still running")
                else:
                    sink.activity_update("model", "waiting for model")
                continue

            in_sub = bool(ns)

            if mode == "messages":
                msg_chunk = chunk
                meta: dict[str, Any] = {}
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    msg_chunk, meta = chunk[0], chunk[1] or {}

                node = ""
                if isinstance(meta, dict):
                    node = str(
                        meta.get("langgraph_node") or meta.get("checkpoint_ns") or ""
                    )
                if node and any(x in node for x in ("tools", "tool")):
                    continue

                # Nested summarization invoke must not stream SESSION INTENT into TUI.
                if is_stream_meta_summarization(meta) or is_lc_summarization_message(
                    msg_chunk
                ):
                    mid = getattr(msg_chunk, "id", None)
                    if mid is not None:
                        suppress_msg_ids.add(str(mid))
                    _note_compact()
                    _drop_leaked_stream()
                    continue

                mid = getattr(msg_chunk, "id", None)
                if mid is not None and str(mid) in suppress_msg_ids:
                    continue

                if in_sub:
                    # Nested token stream is high-frequency; keep sticky intent.
                    continue

                reasoning_delta = _extract_reasoning(msg_chunk)
                if reasoning_delta:
                    sink.activity_update("reasoning", "model thinking")
                    sink.write_reasoning(reasoning_delta)

                # Content tokens first — same chunk may also carry tool_call_chunks.
                text = _chunk_text(msg_chunk)
                msg_id = getattr(msg_chunk, "id", None)
                if msg_id is not None:
                    msg_id = str(msg_id)
                if text:
                    if is_context_compact_text(text):
                        if msg_id:
                            suppress_msg_ids.add(msg_id)
                        _note_compact()
                        _drop_leaked_stream()
                        continue
                    sink.write_answer_token(text, msg_id=msg_id)

                tool_call_chunks = getattr(msg_chunk, "tool_call_chunks", None) or []
                if tool_call_chunks:
                    sink.finalize_line()
                    sink.activity_update("tool", "model requested tool call(s)")
                continue

            if mode != "updates" or not isinstance(chunk, dict):
                continue

            # Middleware-only jump maps (all Nones) are not agent state.
            if _looks_like_middleware_update(chunk):
                sink.activity_update("model", "working")
                continue

            if chunk and all(isinstance(v, dict) for v in chunk.values()):
                node_items = list(chunk.items())
            else:
                node_items = [("graph" if not in_sub else "subagent", chunk)]

            for _node_name, update in node_items:
                if not isinstance(update, dict):
                    continue
                if _looks_like_middleware_update(update):
                    sink.activity_update("model", "working")
                    continue
                if not in_sub:
                    final.update(update)
                messages = update.get("messages") or []
                if not messages:
                    sink.activity_update("model", "working")
                    continue

                for msg in messages:
                    msg_id = getattr(msg, "id", None) or id(msg)
                    dedupe_key = f"{'/'.join(ns)}:{msg_id}"
                    if dedupe_key in printed_ids:
                        continue
                    printed_ids.add(dedupe_key)

                    if _is_tool_message(msg):
                        name = getattr(msg, "name", "tool")
                        raw_content = getattr(msg, "content", "")
                        status = summarize_tool_result(raw_content, limit=100)
                        sink.finalize_line()
                        # Nested subgraph tool traffic must not paint the parent
                        # timeline and must not reset status to idle mid-task.
                        if in_sub:
                            tool_call_id = str(
                                getattr(msg, "tool_call_id", None)
                                or getattr(msg, "id", None)
                                or ""
                            )
                            label = (
                                (tool_call_id and sub_tool_labels.get(tool_call_id))
                                or sub_tool_labels.get(str(name))
                                or str(name)
                            )
                            body = content_to_text(raw_content)
                            err = is_error_status(status, body)
                            detail = f"{label} 失败" if err else label
                            try:
                                sink.activity_update("subagent", detail, force=True)
                            except TypeError:
                                sink.activity_update("subagent", detail)
                            # Also finish the nested tool item in the timeline.
                            if use_tool_items:
                                item = match_tool_result(pending_tool_items, str(name))
                                preview = truncate_preview(raw_content)
                                if item is not None:
                                    if is_todo_tool(item.name) and item.preview:
                                        preview = item.preview
                                    item.status = "error" if err else "ok"
                                    item.error = err
                                    item.preview = preview
                                    sink.tool_item_finished(
                                        item.id,
                                        status=item.status,
                                        preview=preview,
                                        error=err,
                                    )
                                    try:
                                        pending_tool_items.remove(item)
                                    except ValueError:
                                        pass
                            continue
                        sink.activity_stop()
                        if use_tool_items:
                            item = match_tool_result(pending_tool_items, str(name))
                            preview = truncate_preview(raw_content)
                            err = is_error_status(status, content_to_text(raw_content))
                            if item is not None:
                                # Keep checklist from tool args; result is usually a short ack.
                                if is_todo_tool(item.name) and item.preview:
                                    preview = item.preview
                                item.status = "error" if err else "ok"
                                item.error = err
                                item.preview = preview
                                sink.tool_item_finished(
                                    item.id,
                                    status=status,
                                    preview=preview,
                                    error=err,
                                )
                                try:
                                    pending_tool_items.remove(item)
                                except ValueError:
                                    pass
                            # Unmatched parent results are ignored under the item
                            # API — never invent empty "0 tools" groups.
                            if not pending_tool_items:
                                sink.tool_group_closed(f"g{tool_group_seq}")
                                # Multi-round agent loop: after a tool batch the
                                # model may think / speak again.
                                sink.streamed_reasoning = False
                        else:
                            sink.tool_result(name, status, sub=False)
                        if name in active_tools:
                            try:
                                active_tools.remove(name)
                            except ValueError:
                                pass
                        sink.activity_start("model", "waiting for model")
                        continue

                    if not _is_ai_message(msg):
                        # Hide summarization HumanMessage wrappers if state-emitted.
                        if is_lc_summarization_message(msg) or is_context_compact_text(
                            _normalize_content(getattr(msg, "content", ""))
                        ):
                            _note_compact()
                        continue

                    # Accumulate token usage (dedupe by msg id).
                    usage_key = f"usage:{msg_id if msg_id else id(msg)}"
                    if usage_key not in _usage_seen:
                        u = _extract_usage(msg)
                        input_tokens += u["input_tokens"]
                        output_tokens += u["output_tokens"]
                        cache_tokens += u.get("cache_tokens", 0)
                        # Occupancy chrome: keep the latest call's raw return values.
                        if (
                            u["input_tokens"]
                            or u["output_tokens"]
                            or u.get("cache_tokens")
                        ):
                            last_input_tokens = int(u["input_tokens"] or 0)
                            last_output_tokens = int(u["output_tokens"] or 0)
                            last_cache_tokens = int(u.get("cache_tokens", 0) or 0)
                            _usage_seen.add(usage_key)
                            note = getattr(sink, "note_usage", None)
                            if callable(note):
                                try:
                                    note(
                                        turn_input=input_tokens,
                                        turn_output=output_tokens,
                                        turn_cache=cache_tokens,
                                        last_input=last_input_tokens,
                                        last_output=last_output_tokens,
                                        last_cache=last_cache_tokens,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass

                    reasoning = _extract_reasoning(msg)
                    text = _normalize_content(getattr(msg, "content", "")).strip()
                    calls = getattr(msg, "tool_calls", None) or []
                    msg_id = getattr(msg, "id", None)
                    if msg_id is not None:
                        msg_id = str(msg_id)

                    if is_lc_summarization_message(msg) or is_context_compact_text(text):
                        if msg_id:
                            suppress_msg_ids.add(msg_id)
                        _note_compact()
                        _drop_leaked_stream()
                        continue

                    if in_sub:
                        if calls:
                            for call in calls:
                                label = human_tool_label(call)
                                cid = _tool_call_id(call)
                                n = _tool_call_name(call)
                                if cid:
                                    sub_tool_labels[cid] = label
                                if n:
                                    sub_tool_labels[n] = label
                            detail = human_nested_tools_detail(list(calls), limit=5)
                            try:
                                sink.activity_update("subagent", detail, force=True)
                            except TypeError:
                                sink.activity_update("subagent", detail)
                            # Emit nested tool items under the current task group
                            # so the user can see subagent internal progress.
                            if use_tool_items:
                                gid = f"g{tool_group_seq}"
                                for idx, call in enumerate(calls):
                                    item = build_tool_item(
                                        call,
                                        item_id=f"{gid}-sub-{idx}",
                                        index=idx,
                                        sub=True,
                                    )
                                    pending_tool_items.append(item)
                                    sink.tool_item_started(item)
                        # Nested free-text: keep last tool intent sticky.
                        continue

                    r_tokens = _reasoning_token_count(msg)
                    if reasoning and not sink.streamed_reasoning:
                        sink.write_reasoning(reasoning)
                        sink.close_reasoning()
                    elif reasoning and sink.streamed_reasoning:
                        sink.close_reasoning()
                    elif r_tokens and r_tokens > 0 and not sink.streamed_reasoning:
                        sink.write_reasoning(
                            f"(reasoning text not exposed by gateway; "
                            f"~{r_tokens} reasoning tokens)\n"
                        )
                        sink.close_reasoning()

                    # Always surface complete AI content once per message.
                    # Intermediate (content + tool_calls) and final answers both print.
                    if text:
                        sink.write_answer_complete(text, msg_id=msg_id)

                    if calls:
                        sink.finalize_line()
                        sink.activity_stop()
                        names = [_tool_call_name(c) for c in calls]
                        for n in names:
                            active_tools.append(n)
                            tool_calls += 1
                        sink.tool_calls_started(calls, parallel=len(calls) > 1)
                        if use_tool_items:
                            tool_group_seq += 1
                            gid = f"g{tool_group_seq}"
                            for idx, call in enumerate(calls):
                                item = build_tool_item(
                                    call,
                                    item_id=f"{gid}-{idx}",
                                    index=idx,
                                    sub=in_sub,
                                )
                                pending_tool_items.append(item)
                                sink.tool_item_started(item)
                        if any(n == "task" for n in names):
                            sink.activity_start(
                                "subagent",
                                "task (this can take a while; progress may be sparse)",
                            )
                        else:
                            sink.activity_start(
                                "tool",
                                f"{', '.join(names[:5])}"
                                + ("…" if len(names) > 5 else ""),
                            )
                    elif text:
                        sink.activity_update("model", "composing answer")
                    else:
                        sink.activity_update("model", "working")
    finally:
        sink.finalize_line()
        sink.activity_stop()
        # Seal any leftover open tool group (e.g. incomplete batch).
        finish_turn = getattr(sink, "turn_finished", None)
        if callable(finish_turn):
            finish_turn()

    # Prefer last AI message text; answer_buf holds already-rendered answers.
    complete = extract_last_ai_text(final)
    if not complete and not cancelled:
        # Stream updates may only have carried middleware jumps; recover from
        # checkpointer state so we do not show empty/garbled answers.
        try:
            get_state = getattr(agent, "get_state", None)
            if callable(get_state):
                snap = get_state(run_config)
                values = getattr(snap, "values", None)
                if isinstance(values, dict):
                    recovered = extract_last_ai_text(values)
                    if recovered:
                        complete = recovered
                        if "messages" in values and not final.get("messages"):
                            final["messages"] = values.get("messages")
        except Exception:  # noqa: BLE001
            pass
    buffered = "".join(sink.answer_buf).strip()
    final_text = complete or buffered

    interrupted = False
    if not cancelled:
        try:
            from synapse.hitl import (
                extract_pending_interrupt,
                format_interrupt_lines,
                has_pending_interrupt,
            )

            interrupted = has_pending_interrupt(agent, run_config)
            if interrupted:
                pending = extract_pending_interrupt(agent, run_config)
                if pending is not None:
                    for line in format_interrupt_lines(pending):
                        sink.info(line)
        except Exception:  # noqa: BLE001
            interrupted = False

    result = StreamResult(
        state=final,
        final_text=final_text if not interrupted else (final_text or ""),
        tool_calls=tool_calls,
        elapsed_s=time.time() - started,
        streamed_answer=sink.streamed_answer,
        reasoning_text="".join(sink.reasoning_buf).strip(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        total_tokens=input_tokens + output_tokens,
        last_input_tokens=last_input_tokens,
        last_output_tokens=last_output_tokens,
        last_cache_tokens=last_cache_tokens,
        cancelled=cancelled,
        interrupted=interrupted,
        compact_events=compact_events,
    )
    if cancelled:
        # Preserve multi-turn continuity: seal open tool_calls / pending next.
        try:
            from synapse.cancel_repair import repair_thread_after_cancel

            for note in repair_thread_after_cancel(agent, run_config)[:5]:
                sink.info(f"cancel-seal: {note}")
        except Exception as exc:  # noqa: BLE001
            sink.info(f"cancel-seal failed: {exc}")
        sink.info(f"cancelled in {result.elapsed_s:.1f}s | tools={result.tool_calls}")
    elif interrupted:
        sink.info(
            f"paused for approval in {result.elapsed_s:.1f}s | "
            f"tools={result.tool_calls} — /approve or /reject"
        )
    elif result.tool_calls or result.elapsed_s >= 0.5:
        token_info = ""
        if result.total_tokens or result.cache_tokens:
            token_info = (
                f" | tokens: {result.total_tokens} "
                f"(in={result.input_tokens} cache={result.cache_tokens} "
                f"out={result.output_tokens})"
            )
        sink.info(
            f"finished in {result.elapsed_s:.1f}s | tools={result.tool_calls} | "
            f"token_stream={'on' if token_stream else 'off'}"
            + (
                f" | reasoning={len(result.reasoning_text)}c"
                if result.reasoning_text
                else ""
            )
            + token_info
        )
    return result
