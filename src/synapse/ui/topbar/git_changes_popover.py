"""Branch topbar hover popover: list dirty / untracked files with +A -D."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.text import Text
from textual.containers import Vertical
from textual.events import Click, Enter, Leave, MouseMove
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from synapse.ui.topbar.core import TopBarRegistry, display_width, locate_component_span
from synapse.ui.topbar.git_chrome import (
    GitChangedFile,
    format_changed_file_plain,
    probe_git_changed_files,
    render_changed_file_row,
)

BRANCH_COMPONENT_ID = "branch"
# Close only after the pointer fully leaves both topbar branch and popover.
_HIDE_DELAY_S = 0.12
_MAX_ROWS = 14
_MIN_WIDTH = 36
_MAX_WIDTH = 72


def _widget_is_or_inside(widget: Widget | None, root: Widget | None) -> bool:
    """True if ``widget`` is ``root`` or a descendant of ``root``."""
    if widget is None or root is None:
        return False
    node: Widget | None = widget
    while node is not None:
        if node is root:
            return True
        parent = getattr(node, "parent", None)
        node = parent if isinstance(parent, Widget) else None
    return False


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
        id: str | None = "git-changes-popover",
    ) -> None:
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


class TopBar(Static):
    """Single-line topbar Static with branch-hover change list."""

    class OpenGitExplore(Message):
        """Request the app to open the Git Explore modal."""

        def __init__(self, path: str | None = None) -> None:
            super().__init__()
            self.path = path

    def __init__(
        self,
        *,
        registry_provider: Callable[[], TopBarRegistry],
        workspace_provider: Callable[[], Path | str],
        dirty_provider: Callable[[], bool] | None = None,
        usable_width_provider: Callable[[], int] | None = None,
        colors: dict[str, str] | None = None,
        id: str | None = "topbar",
    ) -> None:
        super().__init__(id=id)
        self._registry_provider = registry_provider
        self._workspace_provider = workspace_provider
        self._dirty_provider = dirty_provider
        self._usable_width_provider = usable_width_provider
        self._colors = colors or {}
        # Independent hover flags: both must be False before hide.
        self._branch_hover = False
        self._popover_hover = False
        self._hide_timer: Timer | None = None
        self._popover: GitChangesPopover | None = None
        self._files_cache: list[GitChangedFile] | None = None
        self._files_cache_key: str | None = None

    def _usable_width(self) -> int:
        if self._usable_width_provider is not None:
            try:
                return max(20, int(self._usable_width_provider() or 0))
            except Exception:  # noqa: BLE001
                pass
        width = max(int(getattr(self.size, "width", 0) or 0), 48)
        # CSS padding: 0 1
        return max(20, width - 2)

    def _is_dirty(self) -> bool:
        if self._dirty_provider is None:
            return True
        try:
            return bool(self._dirty_provider())
        except Exception:  # noqa: BLE001
            return False

    def _branch_span(self) -> tuple[int, int] | None:
        try:
            reg = self._registry_provider()
        except Exception:  # noqa: BLE001
            return None
        return locate_component_span(
            reg, BRANCH_COMPONENT_ID, usable_width=self._usable_width()
        )

    def _pointer_on_branch(self, x: int) -> bool:
        span = self._branch_span()
        if span is None:
            return False
        start, width = span
        if width <= 0:
            return False
        # content x already excludes widget border; CSS padding-left is 1.
        content_x = int(x) - 1
        return start <= content_x < start + width

    def _load_files(self, *, force: bool = False) -> list[GitChangedFile]:
        try:
            ws = Path(self._workspace_provider())
        except Exception:  # noqa: BLE001
            return []
        key = str(ws)
        if not force and self._files_cache is not None and self._files_cache_key == key:
            return self._files_cache
        files = probe_git_changed_files(ws)
        self._files_cache = files
        self._files_cache_key = key
        return files

    def invalidate_files_cache(self) -> None:
        self._files_cache = None
        self._files_cache_key = None

    def is_popover_open(self) -> bool:
        return self._popover is not None

    def on_popover_enter(self) -> None:
        """Popover mouse enter: keep open without faking branch hover."""
        self._popover_hover = True
        self._cancel_hide()

    def on_popover_leave(self) -> None:
        """Popover mouse leave: allow hide if branch is also not hovered."""
        self._popover_hover = False
        self.schedule_hide()

    def keep_open(self) -> None:
        """App-facing cancel of a pending hide (e.g. moving into popover)."""
        self._popover_hover = True
        self._cancel_hide()

    def schedule_hide(self) -> None:
        self._cancel_hide()
        self._hide_timer = self.set_timer(_HIDE_DELAY_S, self._hide_popover)

    def _cancel_hide(self) -> None:
        if self._hide_timer is not None:
            try:
                self._hide_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._hide_timer = None

    def _hide_popover(self) -> None:
        self._hide_timer = None
        # Stay open only while pointer is on branch chrome or the popover itself.
        if self._branch_hover or self._popover_hover:
            return
        self._remove_popover()

    def _remove_popover(self) -> None:
        pop = self._popover
        self._popover = None
        self._popover_hover = False
        if pop is not None:
            try:
                pop.remove()
            except Exception:  # noqa: BLE001
                pass

    def show_popover(self, *, force_reload: bool = False) -> None:
        if not self._is_dirty():
            self.dismiss()
            return
        files = self._load_files(force=force_reload)
        if not files:
            self.dismiss()
            return

        span = self._branch_span()
        left = 1
        if span is not None:
            left = max(0, int(span[0]) + 1)  # + CSS padding

        # Already open: keep instance and re-position only.
        if self._popover is not None:
            width = self._popover.measure_width()
            try:
                screen_w = int(getattr(self.screen.size, "width", 0) or 0)
                if screen_w > 0 and left + width > screen_w:
                    left = max(0, screen_w - width)
            except Exception:  # noqa: BLE001
                pass
            self._popover.styles.offset = (left, 1)
            self._popover.styles.width = width
            return

        pop = GitChangesPopover(
            files,
            color_clean=self._colors.get("clean", "#81c995"),
            color_dirty=self._colors.get("dirty", "#f28b82"),
            color_dim=self._colors.get("dim", "#9aa0a6"),
            color_fg=self._colors.get("fg", "#e8eaed"),
            color_orange=self._colors.get("orange", "#f4b183"),
            color_added=self._colors.get("added", "#81c995"),
            color_deleted=self._colors.get("deleted", "#f28b82"),
        )
        pop._owner = self
        width = pop.measure_width()
        try:
            screen_w = int(getattr(self.screen.size, "width", 0) or 0)
            if screen_w > 0 and left + width > screen_w:
                left = max(0, screen_w - width)
        except Exception:  # noqa: BLE001
            pass

        self._popover = pop
        self.screen.mount(pop)
        pop.styles.offset = (left, 1)
        pop.styles.width = width
        pop.styles.layer = "overlay"

    def on_enter(self, event: Enter) -> None:
        # Enter alone may not carry a stable x on all backends; MouseMove refines.
        try:
            x = int(getattr(event, "x", -1))
        except Exception:  # noqa: BLE001
            x = -1
        if x >= 0 and self._pointer_on_branch(x) and self._is_dirty():
            self._branch_hover = True
            self._cancel_hide()
            self.show_popover()

    def on_leave(self, event: Leave) -> None:
        del event
        self._branch_hover = False
        self.schedule_hide()

    def on_mouse_move(self, event: MouseMove) -> None:
        on_branch = self._pointer_on_branch(int(event.x))
        if on_branch and self._is_dirty():
            if not self._branch_hover or self._popover is None:
                self._branch_hover = True
                self._cancel_hide()
                self.show_popover()
            else:
                self._branch_hover = True
                self._cancel_hide()
        else:
            if self._branch_hover:
                self._branch_hover = False
                self.schedule_hide()

    def dismiss_if_outside(self, widget: Widget | None) -> bool:
        """Dismiss when a click landed outside topbar + popover.

        Returns True if the popover was open and dismissed.
        """
        if self._popover is None:
            return False
        if _widget_is_or_inside(widget, self._popover):
            return False
        if widget is self:
            return False
        self.dismiss()
        return True

    def dismiss(self) -> None:
        self._branch_hover = False
        self._popover_hover = False
        self._cancel_hide()
        self._remove_popover()

    def request_explore(self, path: str | None = None) -> None:
        """Dismiss the hover popover and ask the app to open Git Explore."""
        self.dismiss()
        self.post_message(self.OpenGitExplore(path))

    def on_click(self, event: Click) -> None:
        """Click branch chrome → open Git Explore (works even when clean)."""
        try:
            x = int(getattr(event, "x", -1))
        except Exception:  # noqa: BLE001
            x = -1
        if x < 0 or not self._pointer_on_branch(x):
            return
        event.stop()
        self.request_explore(None)
