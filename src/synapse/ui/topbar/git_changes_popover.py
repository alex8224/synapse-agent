"""Git working-tree changes hover popover."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Vertical
from textual.events import Click, Enter, Leave
from textual.widgets import Static

from synapse.ui.topbar.core import display_width
from synapse.ui.topbar.git_chrome import (
    GitChangedFile,
    format_changed_file_plain,
    render_changed_file_row,
)

if TYPE_CHECKING:
    from synapse.ui.topbar.widget import TopBar

_MAX_ROWS = 14
_MIN_WIDTH = 36
_MAX_WIDTH = 72


class PopoverFileRow(Static):
    """One clickable changed-file row inside the hover popover."""

    def __init__(self, item: GitChangedFile, row_text: Text) -> None:
        super().__init__(row_text)
        self.item = item


class GitChangesPopover(Vertical):
    """Overlay list of changed files under the branch chrome."""

    DEFAULT_CSS = """
    GitChangesPopover {
        layer: overlay;
        width: auto;
        height: auto;
        max-height: 16;
        padding: 0 1;
        border: solid $theme-border;
        background: $theme-bar;
        color: $theme-fg;
        overflow-x: hidden;
        overflow-y: auto;
        /* Quiet 1-cell rail: track blends into bar; thumb is muted border. */
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-background: $theme-bar;
        scrollbar-color: $theme-border;
        scrollbar-background-hover: $theme-bar;
        scrollbar-color-hover: $theme-muted;
        scrollbar-background-active: $theme-bar;
        scrollbar-color-active: $theme-dim;
        scrollbar-corner-color: $theme-bar;
    }
    GitChangesPopover #git-changes-title {
        height: 1;
        color: $theme-dim;
        text-style: bold;
    }
    GitChangesPopover #git-changes-body {
        height: auto;
        max-height: 14;
        overflow-y: auto;
        overflow-x: hidden;
        layout: vertical;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-background: $theme-bar;
        scrollbar-color: $theme-border;
        scrollbar-background-hover: $theme-bar;
        scrollbar-color-hover: $theme-muted;
        scrollbar-background-active: $theme-bar;
        scrollbar-color-active: $theme-dim;
        scrollbar-corner-color: $theme-bar;
    }
    GitChangesPopover PopoverFileRow {
        height: 1;
        width: 1fr;
    }
    GitChangesPopover PopoverFileRow:hover {
        text-style: bold;
    }
    """

    def __init__(
        self,
        files: list[GitChangedFile],
        *,
        color_clean: str = "#81c995",
        color_dirty: str = "#f28b82",
        color_dim: str = "#9aa0a6",
        color_fg: str = "#e8eaed",
        color_orange: str = "#f4b183",
        color_added: str = "#81c995",
        color_deleted: str = "#f28b82",
        id: str | None = None,
    ) -> None:
        # Callers that mount onto Screen should pass a unique id; fixed default
        # ids collide when Textual remove() is still draining the previous node.
        super().__init__(id=id)
        self.files = list(files or [])
        self._colors = {
            "clean": color_clean,
            "dirty": color_dirty,
            "dim": color_dim,
            "fg": color_fg,
            "orange": color_orange,
            "added": color_added,
            "deleted": color_deleted,
        }
        self._owner: TopBar | None = None

    def compose(self):  # type: ignore[override]
        n = len(self.files)
        title = f"changed files ({n})  · click to explore" if n else "no changes"
        yield Static(title, id="git-changes-title")
        with Vertical(id="git-changes-body"):
            if not self.files:
                yield Static(Text("working tree clean", style=self._colors["dim"]))
            else:
                shown = self.files[:_MAX_ROWS]
                path_w = max(12, min(48, max((len(f.path) for f in shown), default=12)))
                for item in shown:
                    row_text = render_changed_file_row(
                        item,
                        path_width=path_w,
                        color_status_m=self._colors["orange"],
                        color_status_a=self._colors["added"],
                        color_status_d=self._colors["deleted"],
                        color_status_u=self._colors["dim"],
                        color_path=self._colors["fg"],
                        color_added=self._colors["added"],
                        color_deleted=self._colors["deleted"],
                        color_muted=self._colors["dim"],
                    )
                    yield PopoverFileRow(item, row_text)
                extra = len(self.files) - len(shown)
                if extra > 0:
                    yield Static(Text(f"... +{extra} more", style=self._colors["dim"]))

    def measure_width(self) -> int:
        """Preferred content width in cells."""
        if not self.files:
            return _MIN_WIDTH
        plain_rows = [format_changed_file_plain(f) for f in self.files[:_MAX_ROWS]]
        body = max((display_width(r) for r in plain_rows), default=_MIN_WIDTH)
        title_w = display_width(f"changed files ({len(self.files)})  · click to explore")
        # border + padding ~ 4 cells
        return max(_MIN_WIDTH, min(_MAX_WIDTH, max(body, title_w) + 4))

    def on_enter(self, event: Enter) -> None:
        event.stop()
        if self._owner is not None:
            self._owner.on_popover_enter()

    def on_leave(self, event: Leave) -> None:
        event.stop()
        if self._owner is not None:
            self._owner.on_popover_leave()

    def on_click(self, event: Click) -> None:
        """Click a file row (or the popover) to open Git Explore."""
        event.stop()
        path: str | None = None
        widget = event.widget
        if isinstance(widget, PopoverFileRow):
            path = widget.item.path
        elif self.files:
            # Title / empty area → open explore without a focused path.
            path = None
        if self._owner is not None:
            self._owner.request_explore(path)
