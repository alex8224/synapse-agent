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


def _suffix_truncate(value: str, limit: int) -> str:
    """Bound a suffix field so custom long names cannot blow up the row."""
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def format_subagent_suffix(item: ToolItem) -> str:
    """Return the muted subagent metadata suffix, or ``""`` when not applicable.

    Only top-level ``task`` items with a known ``subagent_name`` get a
    suffix. Missing model/effort degrade to a bare ``[name]`` instead of
    ``None``/``?`` placeholders; each axis falling back to the main agent is
    marked ``(inherit)`` so the shown value is not mistaken for a pinned one.
    """
    if item.sub or item.category != "task" or not item.subagent_name:
        return ""
    parts = [_suffix_truncate(item.subagent_name, 32)]
    if item.subagent_model:
        model = _suffix_truncate(item.subagent_model, 48)
        if item.subagent_model_inherited:
            model = f"{model} (inherit)"
        parts.append(model)
    if item.subagent_reasoning_effort:
        effort = _suffix_truncate(item.subagent_reasoning_effort, 24)
        if item.subagent_reasoning_inherited:
            effort = f"{effort} (inherit)"
        parts.append(effort)
    return f"[{' · '.join(parts)}]"


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

    # Top-level tool rows kept before folding the oldest completed ones.
    _MAX_EXPANDED_ROWS = 12
    # Nested calls shown per subagent; older calls fold into "... and N earlier".
    _MAX_SUB_ROWS = 3
    _HEADER_INDENT = "  "
    _ITEM_INDENT = "   "
    _SUB_ITEM_INDENT = "      "
    _TODO_INDENT = "    "
    _PHASE_LABELS = {"thinking": "thinking", "answering": "answering"}

    def __init__(self, summary: str = "tools") -> None:
        self.summary = summary or "tools"
        self.items: list[ToolItem] = []
        self.collapsed = False
        self._phases: dict[str, str] = {}
        super().__init__()
        self._render_block()

    def _sync_summary_from_items(self, *, running: bool | None = None) -> None:
        """Keep the group header honest as items accumulate."""
        if not self.items:
            return
        if running is None:
            running = any(item.status == "running" for item in self.items)
        self.summary = summarize_items(self.items, running=running)

    def _grouped_items(self) -> list[tuple[ToolItem, list[ToolItem]]]:
        """Return ordered ``(parent, sub_items)`` groups.

        Sub-items are attached to their ``parent_id`` owner rather than their
        flat list position, so concurrent subagents never bleed into each
        other. Orphan sub-items (unknown parent) are dropped instead of being
        misattributed to the first or last group.
        """
        subs_by_parent: dict[str, list[ToolItem]] = {}
        for item in self.items:
            if item.sub and item.parent_id:
                subs_by_parent.setdefault(item.parent_id, []).append(item)
        groups: list[tuple[ToolItem, list[ToolItem]]] = []
        for item in self.items:
            if item.sub:
                continue
            groups.append((item, subs_by_parent.get(item.id, [])))
        return groups

    def _select_visible_groups(
        self, groups: list[tuple[ToolItem, list[ToolItem]]]
    ) -> tuple[list[tuple[ToolItem, list[ToolItem]]], int]:
        """Keep live activity visible when the group overflows.

        Fold the *oldest completed* top-level rows into the "... and N
        earlier" line instead of hiding the newest ones. Running and errored
        rows (including any still-running nested call) are kept up to the cap,
        preferring the newest live rows.
        """

        def live(group: tuple[ToolItem, list[ToolItem]]) -> bool:
            parent, subs = group
            return bool(
                parent.error
                or parent.status == "running"
                or any(s.error or s.status == "running" for s in subs)
            )

        cap = self._MAX_EXPANDED_ROWS
        if len(groups) <= cap:
            return groups, 0

        order = {id(parent): i for i, (parent, _) in enumerate(groups)}
        live_groups = [g for g in groups if live(g)]
        done_groups = [g for g in groups if not live(g)]

        visible = list(live_groups)
        if len(visible) > cap:
            visible = visible[-cap:]
        else:
            room = cap - len(visible)
            if room > 0 and done_groups:
                visible = visible + done_groups[-room:]

        visible.sort(key=lambda g: order[id(g[0])])
        return visible, len(groups) - len(visible)

    def _visible_subs(self, subs: list[ToolItem]) -> tuple[list[ToolItem], int]:
        """Keep live nested calls visible, then the newest completed ones.

        Mirrors the top-level policy: running/errored sub-calls are never
        folded into "... and N earlier"; the remaining slots go to the most
        recent completed calls.
        """
        if len(subs) <= self._MAX_SUB_ROWS:
            return list(subs), 0

        order = {id(sub): i for i, sub in enumerate(subs)}
        live = [sub for sub in subs if sub.error or sub.status == "running"]
        done = [sub for sub in subs if not sub.error and sub.status != "running"]

        visible = list(live)
        if len(visible) > self._MAX_SUB_ROWS:
            visible = visible[-self._MAX_SUB_ROWS :]
        else:
            room = self._MAX_SUB_ROWS - len(visible)
            if room > 0 and done:
                visible = visible + done[-room:]

        visible.sort(key=lambda sub: order[id(sub)])
        return visible, len(subs) - len(visible)

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
            groups = self._grouped_items()
            visible_groups, overflow = self._select_visible_groups(groups)
            if overflow:
                lines.append(
                    Text(f"{self._ITEM_INDENT}… and {overflow} earlier", style=muted)
                )

            def render_item(item: ToolItem, indent: str) -> None:
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
                suffix = format_subagent_suffix(item)
                suffix_text = f"  {suffix}" if suffix else ""
                if " " in label and item.category in {"read", "edit", "list"}:
                    head, tail = label.split(" ", 1)
                    row = Text(f"{indent}{bullet}  {head} ", style=style)
                    row.append(tail, style=orange)
                    if suffix_text:
                        row.append(suffix_text, style=muted)
                    lines.append(row)
                else:
                    row = Text(f"{indent}{bullet}  {label}", style=style)
                    if suffix_text:
                        row.append(suffix_text, style=muted)
                    lines.append(row)
                if is_todo_tool(item.name) or str(item.label or "").startswith("Todos "):
                    lines.extend(
                        render_todo_checklist_from_preview(
                            item.preview, indent=self._TODO_INDENT
                        )
                    )

            for parent, subs in visible_groups:
                render_item(parent, self._ITEM_INDENT)
                phase = self._phases.get(parent.id)
                if phase and parent.status == "running":
                    label = self._PHASE_LABELS.get(phase, phase)
                    lines.append(
                        Text(
                            f"{self._SUB_ITEM_INDENT}◈ {label}…",
                            style=f"italic {muted}",
                        )
                    )
                visible_subs, sub_overflow = self._visible_subs(subs)
                if sub_overflow:
                    lines.append(
                        Text(
                            f"{self._SUB_ITEM_INDENT}… and {sub_overflow} earlier",
                            style=muted,
                        )
                    )
                for sub in visible_subs:
                    render_item(sub, self._SUB_ITEM_INDENT)
        lines.append(Text(""))
        self.update(Group(*lines))

    def set_summary(self, summary: str, *, render: bool = True) -> None:
        if self.items:
            self._sync_summary_from_items()
        else:
            self.summary = summary or "tools"
        if render:
            self._render_block()

    def set_subagent_phase(
        self, parent_id: str, phase: str | None, *, render: bool = True
    ) -> None:
        """Set or clear a subagent row's transient thinking/answering stage.

        No-op when the stage is unchanged so high-frequency token streams only
        trigger a re-render on actual transitions.
        """
        current = self._phases.get(parent_id)
        if phase is None:
            if parent_id not in self._phases:
                return
            self._phases.pop(parent_id, None)
        else:
            if current == phase:
                return
            self._phases[parent_id] = phase
        if render:
            self._render_block()

    def add_item(self, item: ToolItem, *, render: bool = True) -> None:
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
                existing.subagent_name = item.subagent_name
                existing.subagent_model = item.subagent_model
                existing.subagent_reasoning_effort = item.subagent_reasoning_effort
                existing.subagent_model_inherited = item.subagent_model_inherited
                existing.subagent_reasoning_inherited = item.subagent_reasoning_inherited
                self._sync_summary_from_items()
                if render:
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
        if render:
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
        render: bool = True,
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
            if render:
                self._render_block()
            return

    def update_preview(self, item_id: str, preview: str, *, error: bool = False) -> None:
        self.update_item(item_id, preview=preview, error=error)

    def set_collapsed(self, collapsed: bool, *, render: bool = True) -> None:
        self.collapsed = bool(collapsed)
        if render:
            self._render_block()

    def flush(self) -> None:
        """Render pending item/summary/collapsed mutations in one pass.

        Used by the replay batch path, where ``render=False`` accumulates tool
        writes and a single ``flush`` seals the final state.
        """
        self._render_block()

    def toggle(self) -> None:
        self.collapsed = not self.collapsed
        self._render_block()

    def selectable_text(self) -> str:
        mark = "▸" if self.collapsed else "▾"
        lines = [f"{mark}  {self.summary}"]
        if not self.collapsed:
            visible_groups, overflow = self._select_visible_groups(self._grouped_items())
            if overflow:
                lines.append(f"  … and {overflow} earlier")
            for parent, subs in visible_groups:
                parent_status = "err" if parent.error else (parent.status or "done")
                suffix = format_subagent_suffix(parent)
                suffix_text = f"  {suffix}" if suffix else ""
                lines.append(
                    f"  {parent.label or parent.name}{suffix_text} [{parent_status}]"
                )
                visible_subs, sub_overflow = self._visible_subs(subs)
                if sub_overflow:
                    lines.append(f"    … and {sub_overflow} earlier")
                for sub in visible_subs:
                    sub_status = "err" if sub.error else (sub.status or "done")
                    lines.append(f"    {sub.label or sub.name} [{sub_status}]")
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
    "format_subagent_suffix",
    "render_todo_checklist_from_preview",
    "render_todo_row_texts",
    "todo_kind_style",
]
