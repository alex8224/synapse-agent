"""Git Explore dialog — file list + DiffView (opened from topbar branch)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from synapse.ui.git_explore import (
    DIFF_MODES,
    HAS_DIFF_VIEW,
    DiffMode,
    DiffPayload,
    fallback_renderable,
    load_file_diff,
    make_diff_view,
)
from synapse.ui.topbar.git_chrome import (
    GitChangedFile,
    probe_git_changed_files,
)


class ExploreFileRow(Static):
    """One selectable changed-file row (theme-safe styles for ANSI)."""

    def __init__(self, item: GitChangedFile, *, selected: bool = False) -> None:
        self.item = item
        self._selected = selected
        # Build content before Static init so no active App is required.
        super().__init__(self._build_text())
        if selected:
            self.add_class("-selected")

    def set_selected(self, on: bool) -> None:
        self._selected = on
        if on:
            self.add_class("-selected")
        else:
            self.remove_class("-selected")
        try:
            self.update(self._build_text())
        except Exception:  # noqa: BLE001
            pass

    def _build_text(self) -> Text:
        """Render status + path + stats with named styles (ANSI-safe)."""
        item = self.item
        status = (item.status or "M")[:1]
        path = (item.path or "").replace("\\", "/")
        if len(path) > 34:
            path = "…" + path[-33:]

        if item.is_untracked or status == "?":
            st_style = "dim"
        elif status == "A":
            st_style = "bold green"
        elif status == "D":
            st_style = "bold red"
        else:
            st_style = "bold yellow"

        out = Text()
        out.append(f"{status} ", style=st_style)
        out.append(path, style="default")
        if item.is_untracked:
            out.append("  ?", style="dim")
        elif item.lines_added or item.lines_deleted:
            out.append(" ")
            if item.lines_added:
                out.append(f"+{item.lines_added}", style="green")
            if item.lines_deleted:
                if item.lines_added:
                    out.append(" ")
                out.append(f"-{item.lines_deleted}", style="red")
        elif status == "D":
            out.append("  del", style="red")
        elif status == "A":
            out.append("  new", style="green")
        return out


class GitExploreScreen(ModalScreen[None]):
    """Near-full-screen git changes explorer."""

    class OpenRequested(Message):
        def __init__(self, path: str | None = None) -> None:
            super().__init__()
            self.path = path

    DEFAULT_CSS = """
    GitExploreScreen {
        align: center middle;
        background: $theme-bg 60%;
    }
    GitExploreScreen > #ge-window {
        width: 96%;
        height: 92%;
        max-width: 160;
        max-height: 48;
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
    #ge-header {
        height: 1;
        width: 1fr;
        padding: 0 1;
        color: $theme-dim;
        background: $theme-bar;
    }
    #ge-body {
        height: 1fr;
        width: 1fr;
        layout: horizontal;
    }
    #ge-file-list {
        width: 42;
        min-width: 28;
        max-width: 52;
        height: 1fr;
        border-right: solid $theme-border;
        padding: 0 0;
        overflow-y: auto;
        overflow-x: hidden;
        background: $theme-bg;
        scrollbar-size: 1 1;
    }
    #ge-file-list ExploreFileRow {
        height: 1;
        width: 1fr;
        padding: 0 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    #ge-file-list ExploreFileRow.-selected {
        background: $theme-bar;
        text-style: bold;
    }
    #ge-diff-scroll {
        width: 1fr;
        height: 1fr;
        padding: 0 0;
        overflow-y: auto;
        overflow-x: auto;
        background: $theme-bg;
        scrollbar-size: 1 1;
    }
    #ge-diff-scroll DiffView {
        width: 1fr;
        height: auto;
        min-height: 1fr;
    }
    #ge-diff-body {
        height: auto;
        width: auto;
        color: $theme-fg;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("q", "close", "Close", show=False, priority=True),
        Binding("up", "file_prev", "Prev", show=False, priority=True),
        Binding("k", "file_prev", "Prev", show=False, priority=True),
        Binding("down", "file_next", "Next", show=False, priority=True),
        Binding("j", "file_next", "Next", show=False, priority=True),
        Binding("left", "mode_prev", "Mode-", show=False, priority=True),
        Binding("right", "mode_next", "Mode+", show=False, priority=True),
        Binding("1", "mode_working", "Working", show=False, priority=True),
        Binding("2", "mode_staged", "Staged", show=False, priority=True),
        Binding("3", "mode_unstaged", "Unstaged", show=False, priority=True),
        Binding("r", "reload", "Reload", show=False, priority=True),
        Binding("u", "toggle_split", "Split", show=False, priority=True),
        Binding("space", "toggle_split", "Split", show=False, priority=True),
        Binding("a", "toggle_annotations", "Annot", show=False, priority=True),
        Binding("pageup", "diff_page_up", "PgUp", show=False, priority=True),
        Binding("pagedown", "diff_page_down", "PgDn", show=False, priority=True),
    ]

    def __init__(
        self,
        workspace: Path | str,
        *,
        initial_path: str | None = None,
        branch_label: str | None = None,
        colors: dict[str, str] | None = None,
        split: bool = True,
        annotations: bool = True,
    ) -> None:
        super().__init__()
        self._workspace = Path(workspace)
        self._initial_path = (initial_path or "").strip() or None
        self._branch_label = (branch_label or "").strip()
        self._colors = colors or {}
        self._mode: DiffMode = "working"
        self._files: list[GitChangedFile] = []
        self._selected_idx: int = 0
        self._load_token: int = 0
        self._split = bool(split)
        self._annotations = bool(annotations)
        self._last_payload: DiffPayload | None = None
        # Keep this distinct from Textual MessagePump._closed.
        self._dismiss_started: bool = False

    @property
    def title_text(self) -> str:
        branch = self._branch_label or "git"
        n = len(self._files)
        return f"Git Explore · {branch} · {n} file{'s' if n != 1 else ''}"

    def compose(self) -> ComposeResult:
        with Vertical(id="ge-window"):
            yield Static("", id="ge-header")
            with Horizontal(id="ge-body"):
                yield VerticalScroll(id="ge-file-list")
                with VerticalScroll(id="ge-diff-scroll"):
                    yield Static("select a file", id="ge-diff-body")

    def on_mount(self) -> None:
        win = self.query_one("#ge-window")
        win.border_title = f"◆ {self.title_text}"
        win.border_subtitle = (
            "j/k files · ←/→ mode · u split/unified · a ann · r reload · esc"
        )
        self._reload_files()
        self.set_focus(self.query_one("#ge-file-list"))

    def _mode_tabs(self) -> str:
        parts: list[str] = []
        for m in DIFF_MODES:
            if m == self._mode:
                parts.append(f"[{m}]")
            else:
                parts.append(f" {m} ")
        return " ".join(parts)

    def _view_flags_text(self) -> Text:
        out = Text()
        if self._split:
            out.append("[split]", style="bold cyan")
            out.append(" unified", style="dim")
        else:
            out.append(" split", style="dim")
            out.append("[unified]", style="bold cyan")
        out.append("  ")
        if self._annotations:
            out.append("[ann]", style="bold")
        else:
            out.append("ann", style="dim")
        engine = "diff-view" if HAS_DIFF_VIEW else "fallback"
        out.append(f"  ·  {engine}", style="dim")
        return out

    def _paint_header(self) -> None:
        header = self.query_one("#ge-header", Static)
        n = len(self._files)
        line = Text()
        line.append(self._mode_tabs())
        line.append("  ·  ")
        if n == 0:
            line.append("working tree clean", style="dim")
        else:
            item = self._files[self._selected_idx] if self._files else None
            focus = item.path if item else ""
            line.append(f"{n} changed")
            if focus:
                line.append("  ·  ")
                line.append(focus, style="bold")
        line.append("  ·  ")
        line.append_text(self._view_flags_text())
        header.update(line)

    def _reload_files(self) -> None:
        self._files = probe_git_changed_files(self._workspace, limit=200)
        if self._initial_path:
            for i, f in enumerate(self._files):
                if f.path == self._initial_path:
                    self._selected_idx = i
                    break
            else:
                self._selected_idx = 0
            self._initial_path = None
        else:
            self._selected_idx = min(self._selected_idx, max(0, len(self._files) - 1))
        self._mount_file_rows()
        self._paint_header()
        self._update_title()
        if self._files:
            self._request_diff()
        else:
            self._last_payload = None
            self._show_fallback(Text("working tree clean", style="dim"))

    def _update_title(self) -> None:
        try:
            win = self.query_one("#ge-window")
            win.border_title = f"◆ {self.title_text}"
        except Exception:  # noqa: BLE001
            pass

    def _alive(self) -> bool:
        """False after dismiss/unmount starts; blocks late mounts/workers."""
        if self._dismiss_started:
            return False
        try:
            return bool(self.is_attached)
        except Exception:  # noqa: BLE001
            return False

    def _release_heavy_state(self) -> None:
        """Drop file texts / lists held on the screen instance."""
        self._last_payload = None
        self._files = []
        self._selected_idx = 0
        self._load_token += 1  # invalidate in-flight apply callbacks

    def _clear_diff_view_caches(self, widget: Any) -> None:
        """Best-effort wipe of DiffView internal highlighted line caches."""
        if widget is None:
            return
        for attr, empty in (
            ("code_original", ""),
            ("code_modified", ""),
            ("_highlighted_code_lines", None),
            ("_grouped_opcodes", None),
        ):
            try:
                setattr(widget, attr, empty)
            except Exception:  # noqa: BLE001
                pass
        for attr in (
            "_number_styles",
            "_annotation_styles",
            "_line_styles",
            "_edge_styles",
        ):
            try:
                bucket = getattr(widget, attr, None)
                if isinstance(bucket, dict):
                    bucket.clear()
            except Exception:  # noqa: BLE001
                pass

    def _begin_close(self) -> None:
        """Invalidate late work and release payloads before Textual removes the screen."""
        if self._dismiss_started:
            return
        self._dismiss_started = True
        self._release_heavy_state()

    def dismiss(self, result: Any = None) -> AwaitComplete:
        self._begin_close()
        return super().dismiss(result)

    def on_unmount(self) -> None:
        """Final safety net when the screen leaves the stack."""
        self._begin_close()

    def _mount_file_rows(self) -> None:
        if not self._alive():
            return
        self.run_worker(
            self._replace_file_rows(),
            exclusive=True,
            group="ge-file-rows",
        )

    async def _replace_file_rows(self) -> None:
        if not self._alive():
            return
        panel = self.query_one("#ge-file-list", VerticalScroll)
        await panel.remove_children()
        if not self._alive():
            return
        if not self._files:
            await panel.mount(Static(Text("no changes", style="dim")))
            return
        rows = [
            ExploreFileRow(item, selected=(i == self._selected_idx))
            for i, item in enumerate(self._files)
        ]
        if rows and self._alive():
            await panel.mount_all(rows)

    def _sync_selection(self) -> None:
        if not self._alive():
            return
        panel = self.query_one("#ge-file-list", VerticalScroll)
        rows = list(panel.query(ExploreFileRow))
        for i, row in enumerate(rows):
            row.set_selected(i == self._selected_idx)
        if 0 <= self._selected_idx < len(rows):
            try:
                panel.scroll_to_widget(rows[self._selected_idx], animate=False)
            except Exception:  # noqa: BLE001
                pass
        self._paint_header()
        self._request_diff()

    def _diff_host(self) -> VerticalScroll:
        return self.query_one("#ge-diff-scroll", VerticalScroll)

    async def _replace_diff_children(self, *widgets: Any) -> None:
        if not self._alive():
            # Drop unmounted DiffView caches if we still hold them.
            for w in widgets:
                self._clear_diff_view_caches(w)
            return
        host = self._diff_host()
        # Clear caches on outgoing children before remove (helps GC).
        for child in list(host.children):
            self._clear_diff_view_caches(child)
        await host.remove_children()
        if not self._alive():
            for w in widgets:
                self._clear_diff_view_caches(w)
            return
        if widgets:
            await host.mount(*widgets)
        try:
            host.scroll_home(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def _show_fallback(self, content: Any) -> None:
        if not self._alive():
            return
        host = self._diff_host()
        try:
            body = host.query_one("#ge-diff-body", Static)
        except Exception:  # noqa: BLE001
            body = None
        if body is not None and len(host.children) == 1:
            body.update(content)
            try:
                host.scroll_home(animate=False)
            except Exception:  # noqa: BLE001
                pass
            return
        self.run_worker(
            self._replace_diff_children(Static(content, id="ge-diff-body")),
            exclusive=True,
            group="ge-diff-mount",
        )

    def _show_diff_view(self, view: Any) -> None:
        if not self._alive():
            self._clear_diff_view_caches(view)
            return
        self.run_worker(
            self._replace_diff_children(view),
            exclusive=True,
            group="ge-diff-mount",
        )

    def _mount_payload(self, payload: DiffPayload) -> None:
        if not self._alive():
            return
        # Keep only the active file payload; previous one is replaced (not stacked).
        self._last_payload = payload
        if payload.error or payload.binary:
            self._show_fallback(fallback_renderable(payload, colors=self._colors))
            return
        view = make_diff_view(
            payload,
            split=self._split,
            annotations=self._annotations,
            colors=self._colors,
        )
        if view is None:
            self._show_fallback(fallback_renderable(payload, colors=self._colors))
            return
        self._show_diff_view(view)

    def _request_diff(self) -> None:
        if not self._alive() or not self._files:
            return
        idx = self._selected_idx
        if not (0 <= idx < len(self._files)):
            return
        item = self._files[idx]
        self._load_token += 1
        token = self._load_token
        # Drop previous payload early when switching files to free memory sooner.
        self._last_payload = None
        self._show_fallback(Text(f"loading {item.path}…", style="dim"))
        self._load_diff_worker(
            token,
            item.path,
            item.is_untracked,
            self._mode,
        )

    @work(thread=True, exclusive=True, group="git-explore-diff")
    def _load_diff_worker(
        self,
        token: int,
        path: str,
        is_untracked: bool,
        mode: DiffMode,
    ) -> None:
        if self._dismiss_started or token != self._load_token:
            return
        try:
            payload = load_file_diff(
                self._workspace,
                path,
                mode=mode,
                is_untracked=is_untracked,
            )
        except Exception as exc:  # noqa: BLE001
            payload = DiffPayload(
                path=path,
                text_a="",
                text_b="",
                mode=mode,
                error=f"diff failed: {exc}",
            )
        if self._dismiss_started or token != self._load_token:
            return
        try:
            self.app.call_from_thread(self._apply_payload, token, payload)
        except Exception:  # noqa: BLE001
            # App/screen already gone — drop payload reference.
            del payload

    def _apply_payload(self, token: int, payload: DiffPayload) -> None:
        if self._dismiss_started or token != self._load_token or not self._alive():
            return
        try:
            self._mount_payload(payload)
        except Exception as exc:  # noqa: BLE001
            if not self._alive():
                return
            self._show_fallback(
                Text(
                    f"diff failed: {exc}",
                    style=self._colors.get("orange", "#f4b183"),
                )
            )

    def _set_mode(self, mode: DiffMode) -> None:
        if not self._alive() or mode == self._mode:
            return
        self._mode = mode
        self._paint_header()
        self._request_diff()

    def _remount_last_payload(self) -> None:
        if not self._alive():
            return
        if self._last_payload is None:
            self._request_diff()
            return
        self._paint_header()
        self._mount_payload(self._last_payload)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_file_prev(self) -> None:
        if not self._files:
            return
        if self._selected_idx > 0:
            self._selected_idx -= 1
            self._sync_selection()

    def action_file_next(self) -> None:
        if not self._files:
            return
        if self._selected_idx < len(self._files) - 1:
            self._selected_idx += 1
            self._sync_selection()

    def action_mode_prev(self) -> None:
        i = DIFF_MODES.index(self._mode)
        self._set_mode(DIFF_MODES[(i - 1) % len(DIFF_MODES)])

    def action_mode_next(self) -> None:
        i = DIFF_MODES.index(self._mode)
        self._set_mode(DIFF_MODES[(i + 1) % len(DIFF_MODES)])

    def action_mode_working(self) -> None:
        self._set_mode("working")

    def action_mode_staged(self) -> None:
        self._set_mode("staged")

    def action_mode_unstaged(self) -> None:
        self._set_mode("unstaged")

    def action_reload(self) -> None:
        self._reload_files()

    def action_toggle_split(self) -> None:
        self._split = not self._split
        self._remount_last_payload()

    def action_toggle_annotations(self) -> None:
        self._annotations = not self._annotations
        self._remount_last_payload()

    def action_diff_page_up(self) -> None:
        try:
            self._diff_host().scroll_page_up(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def action_diff_page_down(self) -> None:
        try:
            self._diff_host().scroll_page_down(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def on_click(self, event: Click) -> None:
        widget = event.widget
        if isinstance(widget, ExploreFileRow):
            panel = self.query_one("#ge-file-list", VerticalScroll)
            rows = list(panel.query(ExploreFileRow))
            try:
                idx = rows.index(widget)
            except ValueError:
                return
            if idx != self._selected_idx:
                self._selected_idx = idx
                self._sync_selection()
            event.stop()
            return
        try:
            win = self.query_one("#ge-window")
        except Exception:  # noqa: BLE001
            return
        node = widget
        while node is not None:
            if node is win:
                return
            node = getattr(node, "parent", None)
        if widget is self:
            self.dismiss(None)