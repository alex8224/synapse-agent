"""Tool-group and todo transcript widgets."""

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.events import Click, Enter, Leave
from textual.widgets import Static

from synapse.ui.selectable_static import SelectableStatic
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

_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_GREEN = "#81c995"
_DEFAULT_ORANGE = "#f4b183"
_DEFAULT_MUTED = "#5f6368"
_DEFAULT_BAR = "#2b2d31"


def _theme_color(attribute: str, fallback: str) -> str:
    try:
        from synapse.ui.theme import get_theme

        return str(getattr(get_theme(), attribute, fallback))
    except Exception:  # noqa: BLE001
        return fallback


def todo_kind_style(kind: str) -> str:
    """Return the current theme color for a checklist row kind."""
    if kind == "done":
        return _theme_color("green", _DEFAULT_GREEN)
    if kind == "active":
        return _theme_color("orange", _DEFAULT_ORANGE)
    return _theme_color("dim", _DEFAULT_DIM)


def render_todo_row_texts(
    rows: list[TodoRow],
    *,
    indent: str = "       ",
    max_rows: int = 20,
) -> list[Text]:
    """Render structured todo rows as styled Rich Text lines."""
    muted = _theme_color("muted", _DEFAULT_MUTED)
    out: list[Text] = []
    for row in rows[:max_rows]:
        style = todo_kind_style(row.kind)
        line = Text(f"{indent}{row.mark} ", style=style)
        line.append(row.content, style=muted if row.kind == "done" else style)
        out.append(line)
    if len(rows) > max_rows:
        out.append(Text(f"{indent}… +{len(rows) - max_rows} more", style=muted))
    return out


def render_todo_checklist_from_preview(
    preview: str | None,
    *,
    indent: str = "       ",
    max_rows: int = 20,
) -> list[Text]:
    """Render a stored checklist preview, including legacy ``[x]`` entries."""
    rows = parse_todo_preview_lines(preview)
    if not rows:
        return []
    return render_todo_row_texts(rows, indent=indent, max_rows=max_rows)


class TodoChecklist(Static):
    """Dedicated checklist widget for ``write_todos`` plans."""

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
        dim = _theme_color("dim", _DEFAULT_DIM)
        bar = _theme_color("bar", _DEFAULT_BAR)
        muted = _theme_color("muted", _DEFAULT_MUTED)
        green = _theme_color("green", _DEFAULT_GREEN)
        orange = _theme_color("orange", _DEFAULT_ORANGE)
        lines: list[Text] = [Text(f"  {self.title}", style=f"{dim} on {bar}")]
        rows = self.rows or parse_todo_preview_lines(self.preview)
        body = render_todo_row_texts(rows, indent="    ", max_rows=32)
        if body:
            lines.extend(body)
        else:
            lines.append(Text("    (empty plan)", style=muted))
        legend = Text("    ", style=muted)
        legend.append(f"{TODO_MARK_DONE} done  ", style=green)
        legend.append(f"{TODO_MARK_ACTIVE} doing  ", style=orange)
        legend.append(f"{TODO_MARK_PENDING} todo", style=dim)
        lines.append(legend)
        lines.append(Text(""))
        self.update(Group(*lines))


class ToolGroupBlock(SelectableStatic):
    """A timeline tool group with in-place collapse and preview updates."""

    _MAX_EXPANDED_ROWS = 12
    _HEADER_INDENT = "  "
    _ITEM_INDENT = "   "
    _SUB_ITEM_INDENT = "      "
    _MORE_INDENT = "   "
    _TODO_INDENT = "    "

    def __init__(self, summary: str = "tools") -> None:
        self.summary = summary or "tools"
        self.items: list[ToolItem] = []
        self.collapsed = False
        super().__init__()
        self._render_block()

    def _sync_summary_from_items(self, *, running: bool | None = None) -> None:
        """Keep the group header honest as items accumulate."""
        if not self.items:
            return
        if running is None:
            running = any(item.status == "running" for item in self.items)
        self.summary = summarize_items(self.items, running=running)

    def _render_block(self) -> None:
        fg = _theme_color("fg", _DEFAULT_FG)
        bar = _theme_color("bar", _DEFAULT_BAR)
        green = _theme_color("green", _DEFAULT_GREEN)
        orange = _theme_color("orange", _DEFAULT_ORANGE)
        muted = _theme_color("muted", _DEFAULT_MUTED)
        mark = "▸" if self.collapsed else "▾"
        lines: list[Text] = [
            Text(f"{self._HEADER_INDENT}{mark}  {self.summary}", style=f"{fg} on {bar}")
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
                    style = orange
                    bullet = "○"
                else:
                    style = green
                    bullet = "✓"
                label = item.label or item.name
                indent = self._SUB_ITEM_INDENT if item.sub else self._ITEM_INDENT
                if " " in label and item.category in {"read", "edit", "list"}:
                    head, tail = label.split(" ", 1)
                    row = Text(f"{indent}{bullet}  {head} ", style=style)
                    row.append(tail, style=orange)
                    lines.append(row)
                else:
                    lines.append(Text(f"{indent}{bullet}  {label}", style=style))
                if is_todo_tool(item.name) or str(item.label or "").startswith("Todos "):
                    lines.extend(
                        render_todo_checklist_from_preview(item.preview, indent=self._TODO_INDENT)
                    )
            if overflow:
                lines.append(Text(f"{self._MORE_INDENT}… and {overflow} more", style=muted))
        lines.append(Text(""))
        self.update(Group(*lines))

    def set_summary(self, summary: str) -> None:
        if self.items:
            self._sync_summary_from_items()
        else:
            self.summary = summary or "tools"
        self._render_block()

    def add_item(self, item: ToolItem) -> None:
        for existing in self.items:
            if existing.id == item.id:
                existing.name = item.name
                existing.category = item.category
                existing.label = item.label
                existing.path = item.path
                existing.status = item.status
                existing.preview = item.preview
                existing.error = item.error
                existing.sub = item.sub
                existing.parent_id = item.parent_id
                existing.call_id = item.call_id
                self._sync_summary_from_items()
                self._render_block()
                return
        if item.sub and not item.parent_id:
            return
        if item.parent_id:
            insert_at = next(
                (
                    index + 1
                    for index, existing in reversed(list(enumerate(self.items)))
                    if existing.id == item.parent_id or existing.parent_id == item.parent_id
                ),
                len(self.items),
            )
            self.items.insert(insert_at, item)
        else:
            self.items.append(item)
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
        for item in self.items:
            if item.id != item_id:
                continue
            if label is not None:
                item.label = label
            if path is not None:
                item.path = path
            if name is not None:
                item.name = name
            if category is not None:
                item.category = category
            if status is not None:
                item.status = status
            if preview is not None:
                item.preview = preview
            if error is not None:
                item.error = error
            self._sync_summary_from_items()
            self._render_block()
            return

    def update_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        self.update_item(item_id, preview=preview, error=error)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = bool(collapsed)
        self._render_block()

    def toggle(self) -> None:
        self.collapsed = not self.collapsed
        self._render_block()

    def selectable_text(self) -> str:
        mark = "▸" if self.collapsed else "▾"
        lines = [f"{mark}  {self.summary}"]
        if not self.collapsed:
            for item in self.items:
                label = item.label or item.name
                status = "err" if item.error else item.status
                lines.append(f"  {label} [{status}]")
        return "\n".join(lines)

    def on_enter(self, event: Enter) -> None:
        event.stop()
        self.add_class("-hover")

    def on_leave(self, event: Leave) -> None:
        event.stop()
        self.remove_class("-hover")

    def on_click(self, event: Click) -> None:
        if getattr(event, "chain", 1) != 1:
            return
        event.stop()
        self.toggle()


__all__ = [
    "TodoChecklist",
    "ToolGroupBlock",
    "render_todo_checklist_from_preview",
    "render_todo_row_texts",
    "todo_kind_style",
]
