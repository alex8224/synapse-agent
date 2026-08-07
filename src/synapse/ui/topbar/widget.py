"""Topbar controller and hover-overlay coordination."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.events import Click, Enter, Leave, MouseMove
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from synapse.ui.topbar.core import TopBarRegistry, locate_component_span
from synapse.ui.topbar.git_changes_popover import GitChangesPopover
from synapse.ui.topbar.git_chrome import GitChangedFile, probe_git_changed_files
from synapse.ui.topbar.tool_output_popover import ToolOutputPopover

BRANCH_COMPONENT_ID = "branch"
TOOL_OUTPUT_COMPONENT_ID = "tool_output"
_HIDE_DELAY_S = 0.12


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


class TopBar(Static):
    """Single-line topbar Static with branch-hover change list."""

    class OpenGitExplore(Message):
        """Request the app to open the Git Explore modal."""

        def __init__(self, path: str | None = None) -> None:
            super().__init__()
            self.path = path

    class OpenProjectDrawer(Message):
        """Request the app to open the floating project/session drawer."""

    class ToggleProjectDrawer(Message):
        """Request the app to toggle the floating project/session drawer."""

    def __init__(
        self,
        *,
        registry_provider: Callable[[], TopBarRegistry],
        workspace_provider: Callable[[], Path | str],
        dirty_provider: Callable[[], bool] | None = None,
        tool_output_stats_provider: Callable[[], dict[str, Any]] | None = None,
        usable_width_provider: Callable[[], int] | None = None,
        colors: dict[str, str] | None = None,
        id: str | None = "topbar",
    ) -> None:
        super().__init__(id=id)
        self._registry_provider = registry_provider
        self._workspace_provider = workspace_provider
        self._dirty_provider = dirty_provider
        self._tool_output_stats_provider = tool_output_stats_provider
        self._usable_width_provider = usable_width_provider
        self._colors = colors or {}
        # Independent hover flags: both must be False before hide.
        self._branch_hover = False
        self._popover_hover = False
        self._hide_timer: Timer | None = None
        self._popover: GitChangesPopover | None = None
        self._popover_mounting = False
        self._popover_seq = 0
        self._tool_output_hover = False
        self._tool_output_popover_hover = False
        self._tool_output_hide_timer: Timer | None = None
        self._tool_output_popover: ToolOutputPopover | None = None
        self._tool_output_popover_mounting = False
        self._tool_output_popover_seq = 0
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
        return self._popover_is_attached(self._popover)

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

    @staticmethod
    def _popover_is_attached(pop: Widget | None) -> bool:
        """True when popover is still part of a live screen tree."""
        if pop is None:
            return False
        try:
            if getattr(pop, "is_mounted", False):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            return getattr(pop, "parent", None) is not None
        except Exception:  # noqa: BLE001
            return False

    def _clamp_popover_left(self, left: int, width: int) -> int:
        try:
            screen_w = int(getattr(self.screen.size, "width", 0) or 0)
            if screen_w > 0 and left + width > screen_w:
                return max(0, screen_w - width)
        except Exception:  # noqa: BLE001
            pass
        return left

    def _position_popover(self, pop: GitChangesPopover, left: int) -> None:
        width = pop.measure_width()
        left = self._clamp_popover_left(left, width)
        pop.styles.offset = (left, 1)
        pop.styles.width = width
        pop.styles.layer = "overlay"

    def _purge_screen_popovers(self, *, keep: Widget | None = None) -> None:
        """Drop any leftover GitChangesPopover nodes.

        Textual ``Widget.remove()`` is asynchronous. A second hover can try to
        mount another ``#git-changes-popover`` before the previous node is
        actually gone, which raises ``DuplicateIds``.
        """
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001
            return
        try:
            orphans = list(screen.query(GitChangesPopover))
        except Exception:  # noqa: BLE001
            orphans = []
        for widget in orphans:
            if keep is not None and widget is keep:
                continue
            try:
                widget.remove()
            except Exception:  # noqa: BLE001
                pass

    def _remove_popover(self) -> None:
        pop = self._popover
        self._popover = None
        self._popover_hover = False
        if pop is not None:
            try:
                pop.remove()
            except Exception:  # noqa: BLE001
                pass
        # Also clear any detached-but-still-registered duplicates.
        self._purge_screen_popovers()

    def show_popover(self, *, force_reload: bool = False) -> None:
        if self._popover_mounting:
            return
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

        # Already open and still attached: keep instance and re-position only.
        if self._popover_is_attached(self._popover):
            assert self._popover is not None
            self._position_popover(self._popover, left)
            return

        # Stale handle or async-removed node: drop reference and sweep screen.
        self._popover = None
        self._purge_screen_popovers()

        # Unique id so a dying previous node cannot collide during remove lag.
        self._popover_seq += 1
        pop = GitChangesPopover(
            files,
            color_clean=self._colors.get("clean", "#81c995"),
            color_dirty=self._colors.get("dirty", "#f28b82"),
            color_dim=self._colors.get("dim", "#9aa0a6"),
            color_fg=self._colors.get("fg", "#e8eaed"),
            color_orange=self._colors.get("orange", "#f4b183"),
            color_added=self._colors.get("added", "#81c995"),
            color_deleted=self._colors.get("deleted", "#f28b82"),
            id=f"git-changes-popover-{self._popover_seq}",
        )
        pop._owner = self
        self._position_popover(pop, left)

        self._popover_mounting = True
        try:
            self._popover = pop
            self.screen.mount(pop)
        except Exception:
            self._popover = None
            try:
                pop.remove()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            self._popover_mounting = False

    def _tool_output_span(self) -> tuple[int, int] | None:
        try:
            reg = self._registry_provider()
        except Exception:  # noqa: BLE001
            return None
        return locate_component_span(
            reg, TOOL_OUTPUT_COMPONENT_ID, usable_width=self._usable_width()
        )

    def _pointer_on_tool_output(self, x: int) -> bool:
        span = self._tool_output_span()
        if span is None:
            return False
        start, width = span
        content_x = int(x) - 1  # CSS padding-left
        return width > 0 and start <= content_x < start + width

    def _tool_output_stats(self) -> dict[str, Any]:
        if self._tool_output_stats_provider is None:
            return {}
        try:
            return dict(self._tool_output_stats_provider() or {})
        except Exception:  # noqa: BLE001
            return {}

    def _cancel_tool_output_hide(self) -> None:
        if self._tool_output_hide_timer is not None:
            try:
                self._tool_output_hide_timer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._tool_output_hide_timer = None

    def _remove_tool_output_popover(self) -> None:
        pop = self._tool_output_popover
        self._tool_output_popover = None
        self._tool_output_popover_hover = False
        if pop is not None:
            try:
                pop.remove()
            except Exception:  # noqa: BLE001
                pass

    def _hide_tool_output_popover(self) -> None:
        self._tool_output_hide_timer = None
        if not self._tool_output_hover and not self._tool_output_popover_hover:
            self._remove_tool_output_popover()

    def _schedule_tool_output_hide(self) -> None:
        self._cancel_tool_output_hide()
        self._tool_output_hide_timer = self.set_timer(_HIDE_DELAY_S, self._hide_tool_output_popover)

    def on_tool_output_popover_enter(self) -> None:
        self._tool_output_popover_hover = True
        self._cancel_tool_output_hide()

    def on_tool_output_popover_leave(self) -> None:
        self._tool_output_popover_hover = False
        self._schedule_tool_output_hide()

    def show_tool_output_popover(self) -> None:
        if self._tool_output_popover_mounting:
            return
        stats = self._tool_output_stats()
        if not int(stats.get("transformed", 0) or 0):
            self._remove_tool_output_popover()
            return
        span = self._tool_output_span()
        left = max(0, int(span[0]) + 1) if span is not None else 1
        if self._popover_is_attached(self._tool_output_popover):
            self._remove_tool_output_popover()
        self._tool_output_popover_seq += 1
        pop = ToolOutputPopover(
            stats, id=f"tool-output-popover-{self._tool_output_popover_seq}"
        )
        pop._owner = self
        width = pop.measure_width()
        pop.styles.offset = (self._clamp_popover_left(left, width), 1)
        pop.styles.width = width
        pop.styles.layer = "overlay"
        self._tool_output_popover_mounting = True
        try:
            self._tool_output_popover = pop
            self.screen.mount(pop)
        except Exception:
            self._tool_output_popover = None
            try:
                pop.remove()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            self._tool_output_popover_mounting = False

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
        self._tool_output_hover = False
        self._schedule_tool_output_hide()

    def on_mouse_move(self, event: MouseMove) -> None:
        x = int(event.x)
        on_branch = self._pointer_on_branch(x)
        if on_branch and self._is_dirty():
            if not self._branch_hover or self._popover is None:
                self._branch_hover = True
                self._cancel_hide()
                self.show_popover()
            else:
                self._branch_hover = True
                self._cancel_hide()
        elif self._branch_hover:
            self._branch_hover = False
            self.schedule_hide()

        on_tool_output = self._pointer_on_tool_output(x)
        if on_tool_output:
            if not self._tool_output_hover or self._tool_output_popover is None:
                self._tool_output_hover = True
                self._cancel_tool_output_hide()
                self.show_tool_output_popover()
            else:
                self._tool_output_hover = True
                self._cancel_tool_output_hide()
        elif self._tool_output_hover:
            self._tool_output_hover = False
            self._schedule_tool_output_hide()

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
        self._tool_output_hover = False
        self._tool_output_popover_hover = False
        self._cancel_tool_output_hide()
        self._remove_tool_output_popover()

    def request_explore(self, path: str | None = None) -> None:
        """Dismiss the hover popover and ask the app to open Git Explore."""
        self.dismiss()
        self.post_message(self.OpenGitExplore(path))

    def request_open_drawer(self) -> None:
        """Ask the app to open the project/session drawer."""
        self.dismiss()
        self.post_message(self.ToggleProjectDrawer())

    def on_click(self, event: Click) -> None:
        """Click branch chrome → open Git Explore; workspace chrome → drawer."""
        try:
            x = int(getattr(event, "x", -1))
        except Exception:  # noqa: BLE001
            x = -1
        if x < 0:
            return
        if self._pointer_on_branch(x):
            event.stop()
            self.request_explore(None)
            return
        if self._pointer_on_workspace(x):
            event.stop()
            self.request_open_drawer()

    def _workspace_span(self) -> tuple[int, int] | None:
        """Locate the workspace (leftmost) component span for click handling."""
        try:
            reg = self._registry_provider()
        except Exception:  # noqa: BLE001
            return None
        from synapse.ui.topbar.core import locate_component_span

        return locate_component_span(
            reg, "workspace", usable_width=self._usable_width()
        )

    def _pointer_on_workspace(self, x: int) -> bool:
        span = self._workspace_span()
        if span is None:
            return False
        start, width = span
        if width <= 0:
            return False
        content_x = int(x) - 1
        return start <= content_x < start + width
