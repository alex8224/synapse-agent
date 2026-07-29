"""Subagent monitor dialog for live DAG subagent runs."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from synapse.subagent_monitor import SubagentMonitor, SubagentRun
from synapse.ui.stream import render_markdown


def _status_label(status: str) -> tuple[str, str]:
    value = (status or "running").lower()
    if value == "ok":
        return "ok", "green"
    if value == "error":
        return "error", "red"
    return "running", "yellow"


def _short(text: str, *, limit: int = 42) -> str:
    value = " ".join((text or "").split())
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "..."
    return value


def _depends_label(depends_on: list[str]) -> str:
    value = ", ".join(str(item).strip() for item in depends_on if str(item).strip())
    return _short(value, limit=22) if value else "-"


class SubagentRunRow(Static):
    """One row in the left run list."""

    def __init__(self, run: SubagentRun, *, selected: bool = False) -> None:
        self.run = run
        self._selected = selected
        super().__init__(self._build_text())
        if selected:
            self.add_class("-selected")

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self.set_class(self._selected, "-selected")
        self.update(self._build_text())

    def update_run(self, run: SubagentRun) -> None:
        self.run = run
        self.update(self._build_text())

    def _build_text(self) -> Text:
        status, style = _status_label(self.run.status)
        title = self.run.task_id or self.run.call_id or self.run.subagent_type
        wave = f"w{self.run.wave}" if self.run.wave is not None else "-"
        depends = _depends_label(self.run.depends_on)
        mark = ">" if self._selected else " "
        text = Text(f"{mark} {title}", style="bold" if self._selected else "")
        text.append(f"  {self.run.subagent_type}", style="dim")
        text.append(f"  {wave}", style="dim")
        text.append(f"  dep:{depends}", style="dim")
        text.append(f"  {status}", style=style)
        return text

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(SubagentMonitorDialog.SelectRun(self.run.call_id))


class SubagentMonitorDialog(ModalScreen[None]):
    """Near-full-screen live viewer for current turn subagents."""

    class SelectRun(Message):
        def __init__(self, call_id: str) -> None:
            super().__init__()
            self.call_id = call_id

    DEFAULT_CSS = """
    SubagentMonitorDialog {
        align: center middle;
        background: $theme-bg 60%;
    }
    SubagentMonitorDialog > #sa-window {
        width: 94%;
        height: 88%;
        max-width: 150;
        max-height: 46;
        background: $theme-bg;
        border: round $theme-user;
        border-title-color: $theme-fg;
        border-title-background: $theme-top;
        border-title-style: bold;
        border-title-align: left;
        border-subtitle-color: $theme-muted;
        border-subtitle-align: right;
        layout: vertical;
        padding: 0;
    }
    #sa-body {
        height: 1fr;
        width: 1fr;
        layout: horizontal;
    }
    #sa-list {
        width: 54;
        min-width: 30;
        max-width: 72;
        height: 1fr;
        border-right: solid $theme-border;
        padding: 0 0;
        overflow-y: auto;
        overflow-x: hidden;
        background: $theme-bg;
        scrollbar-size: 1 1;
    }
    #sa-list SubagentRunRow {
        height: 1;
        width: 1fr;
        padding: 0 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    #sa-list SubagentRunRow.-selected {
        background: $theme-bar;
        text-style: bold;
    }
    #sa-detail-scroll {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        overflow-y: auto;
        overflow-x: auto;
        background: $theme-bg;
        scrollbar-size: 1 1;
    }
    #sa-detail {
        width: 1fr;
        height: auto;
        color: $theme-fg;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False, priority=True),
        Binding("up", "previous", "Prev", show=False, priority=True),
        Binding("k", "previous", "Prev", show=False, priority=True),
        Binding("down", "next", "Next", show=False, priority=True),
        Binding("j", "next", "Next", show=False, priority=True),
        Binding("r", "refresh", "Refresh", show=False, priority=True),
    ]

    def __init__(self, monitor: SubagentMonitor) -> None:
        super().__init__()
        self._monitor = monitor
        self._revision = -1
        self._runs: list[SubagentRun] = []
        self._selected_idx = 0
        self._rows: list[SubagentRunRow] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="sa-window"):
            with Horizontal(id="sa-body"):
                yield VerticalScroll(id="sa-list")
                with VerticalScroll(id="sa-detail-scroll"):
                    yield Static("", id="sa-detail")

    def on_mount(self) -> None:
        win = self.query_one("#sa-window")
        win.border_title = "Subagents"
        win.border_subtitle = "j/k select · r refresh · esc"
        self._refresh(force=True)
        self.set_focus(self.query_one("#sa-list"))
        self.set_interval(0.35, self._refresh)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_refresh(self) -> None:
        self._refresh(force=True)

    def action_previous(self) -> None:
        if not self._runs:
            return
        self._selected_idx = max(0, self._selected_idx - 1)
        self._render_selection()

    def action_next(self) -> None:
        if not self._runs:
            return
        self._selected_idx = min(len(self._runs) - 1, self._selected_idx + 1)
        self._render_selection()

    def on_subagent_monitor_dialog_select_run(self, event: SelectRun) -> None:
        for idx, run in enumerate(self._runs):
            if run.call_id == event.call_id:
                self._selected_idx = idx
                self._render_selection()
                return

    def _refresh(self, *, force: bool = False) -> None:
        revision, runs = self._monitor.snapshot()
        if not force and revision == self._revision:
            self._tick_detail()
            return
        self._revision = revision
        previous_call = (
            self._runs[self._selected_idx].call_id
            if 0 <= self._selected_idx < len(self._runs)
            else ""
        )
        self._runs = runs
        if previous_call:
            for idx, run in enumerate(self._runs):
                if run.call_id == previous_call:
                    self._selected_idx = idx
                    break
        if self._selected_idx >= len(self._runs):
            self._selected_idx = max(0, len(self._runs) - 1)
        self._render_list()
        self._render_detail()

    def _render_list(self) -> None:
        list_view = self.query_one("#sa-list", VerticalScroll)
        list_view.remove_children()
        self._rows = []
        if not self._runs:
            list_view.mount(Static(Text("  no subagents for current turn", style="dim")))
            return
        for idx, run in enumerate(self._runs):
            row = SubagentRunRow(run, selected=idx == self._selected_idx)
            self._rows.append(row)
            list_view.mount(row)

    def _render_selection(self) -> None:
        for idx, row in enumerate(self._rows):
            row.set_selected(idx == self._selected_idx)
        self._render_detail()

    def _tick_detail(self) -> None:
        if not self._runs:
            return
        run = self._runs[self._selected_idx]
        if run.status == "running":
            self._render_detail()

    def _render_detail(self) -> None:
        detail = self.query_one("#sa-detail", Static)
        if not self._runs:
            detail.update(Text("No subagents yet.", style="dim"))
            return
        run = self._runs[self._selected_idx]
        status, status_style = _status_label(run.status)
        header = Text()
        header.append(f"{run.task_id or run.call_id}", style="bold")
        header.append(f"  {run.subagent_type}", style="dim")
        header.append(f"  {status}", style=status_style)
        header.append(f"  {run.elapsed_s:.1f}s", style="dim")
        rows: list[Any] = [header, Text("")]
        if run.depends_on:
            rows.append(Text(f"depends_on: {', '.join(run.depends_on)}", style="dim"))
            rows.append(Text(""))
        if run.description:
            rows.append(Text("  ◆  Task", style="dim"))
            rows.append(render_markdown(_short(run.description, limit=1400)))
            rows.append(Text(""))
        for event in run.events:
            rows.extend(self._render_event(event))
        detail.update(Group(*rows))

    def _render_event(self, event: Any) -> list[Any]:
        status = str(getattr(event, "status", "ok") or "ok")
        kind = str(getattr(event, "kind", "") or "")
        title = str(getattr(event, "title", "") or kind or "event")
        body = str(getattr(event, "body", "") or "")
        if kind == "answer":
            return [Text("  ◆  Answer", style="green"), render_markdown(body), Text("")]
        if kind == "tool":
            if status == "error":
                mark, style = "x", "red"
            elif status == "running":
                mark, style = "o", "yellow"
            else:
                mark, style = "v", "green"
            rows: list[Any] = [Text(f"  {mark}  {title}", style=style)]
            if body:
                rows.append(Text(_short(body, limit=1000), style="dim"))
            rows.append(Text(""))
            return rows
        label = "Thought" if kind == "thought" else title
        rows = [Text(f"  ◆  {label}", style="dim")]
        if body:
            rows.append(render_markdown(_short(body, limit=1400)))
        rows.append(Text(""))
        return rows
