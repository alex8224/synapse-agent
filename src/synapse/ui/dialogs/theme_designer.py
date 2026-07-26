"""Theme designer dialog — compact HSV picker with live preview."""

from __future__ import annotations

import colorsys
import json
import re
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, Label, Select, Static

from synapse.config_paths import user_config_dir
from synapse.ui.theme import (
    THEMES_FILENAME,
    _theme_from_dict,
    list_theme_names,
    reload_theme_catalog,
    set_theme,
)

_COLOR_FIELDS: list[tuple[str, str]] = [
    ("bg", "Background"),
    ("top", "Top bar"),
    ("bar", "Status bar"),
    ("fg", "Foreground"),
    ("dim", "Dim text"),
    ("muted", "Muted text"),
    ("green", "Success"),
    ("orange", "Warning"),
    ("user", "User accent"),
    ("border", "Border"),
    ("border_focus", "Focus border"),
    ("error", "Error"),
    ("top_left", "Top left"),
    ("top_center", "Top center"),
    ("top_right", "Top right"),
]
_COLOR_LABELS = dict(_COLOR_FIELDS)
_BORDER_STYLES = ("tall", "heavy", "solid", "round", "dashed", "dotted", "double")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_PREVIEW_DEBOUNCE_S = 0.18
_PLANE_WIDTH = 30
_PLANE_HEIGHT = 8


def _normalize_hex(value: str) -> str | None:
    color = (value or "").strip()
    if not _HEX_RE.fullmatch(color):
        return None
    if len(color) == 4:
        color = f"#{color[1] * 2}{color[2] * 2}{color[3] * 2}"
    return color.lower()


def _hex_to_hsv(value: str) -> tuple[float, float, float] | None:
    color = _normalize_hex(value)
    if color is None:
        return None
    red, green, blue = (int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
    return colorsys.rgb_to_hsv(red, green, blue)


def _hsv_to_hex(hue: float, saturation: float, value: float) -> str:
    red, green, blue = colorsys.hsv_to_rgb(
        hue % 1.0,
        max(0.0, min(1.0, saturation)),
        max(0.0, min(1.0, value)),
    )
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _marker_color(value: float) -> str:
    return "black" if value > 0.62 else "white"


class _DesignerColorChanged(Message):
    """A theme color changed, from the picker or a direct programmatic update."""

    def __init__(self, key: str, value: str) -> None:
        super().__init__()
        self.key = key
        self.value = value


class _ColorRoleSelected(Message):
    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class _PickerColorChanged(Message):
    def __init__(self, value: str) -> None:
        super().__init__()
        self.value = value


class _ColorRoleRow(Static):
    """One compact palette role row."""

    def __init__(self, key: str, label: str, value: str) -> None:
        super().__init__("", id=f"role-{key}")
        self.key = key
        self.label = label
        self.value = value
        self._selected = False
        self._paint()

    def set_value(self, value: str) -> None:
        self.value = value
        self._paint()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.set_class(selected, "-selected")
        self._paint()

    def _paint(self) -> None:
        color = _normalize_hex(self.value)
        row = Text("> " if self._selected else "  ")
        if color:
            row.append("●", style=Style(color=color))
        else:
            row.append("○", style="dim")
        row.append(f"  {self.label:<13}")
        row.append(color or "inherit", style="dim")
        self.update(row)


class _ColorRoleList(VerticalScroll):
    """Keyboard and mouse selectable list of theme color roles."""

    can_focus = True
    BINDINGS = [
        Binding("up", "previous", "Previous", show=False),
        Binding("down", "next", "Next", show=False),
        Binding("enter", "choose", "Choose", show=False),
    ]

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(id="color-roles")
        self._rows = [
            _ColorRoleRow(key, label, values.get(key, "")) for key, label in _COLOR_FIELDS
        ]
        self._selected_index = 0

    def compose(self) -> ComposeResult:
        yield from self._rows

    @property
    def selected_key(self) -> str:
        return self._rows[self._selected_index].key

    def on_mount(self) -> None:
        self._sync_selection(emit=False)

    def select_key(self, key: str, *, emit: bool = True) -> None:
        for index, row in enumerate(self._rows):
            if row.key == key:
                self._selected_index = index
                self._sync_selection(emit=emit)
                return

    def update_value(self, key: str, value: str) -> None:
        for row in self._rows:
            if row.key == key:
                row.set_value(value)
                return

    def _sync_selection(self, *, emit: bool = True) -> None:
        for index, row in enumerate(self._rows):
            row.set_selected(index == self._selected_index)
        selected = self._rows[self._selected_index]
        self.scroll_to_widget(selected, animate=False)
        if emit:
            self.post_message(_ColorRoleSelected(selected.key))

    def action_previous(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
            self._sync_selection()

    def action_next(self) -> None:
        if self._selected_index < len(self._rows) - 1:
            self._selected_index += 1
            self._sync_selection()

    def action_choose(self) -> None:
        self.post_message(_ColorRoleSelected(self.selected_key))

    def on_click(self, event: Click) -> None:
        if isinstance(event.widget, _ColorRoleRow):
            self.select_key(event.widget.key)
            self.focus()
            event.stop()


class _ColorPlane(Static):
    """Saturation/value plane for the selected hue."""

    can_focus = True
    BINDINGS = [
        Binding("left", "left", "Less saturation", show=False),
        Binding("right", "right", "More saturation", show=False),
        Binding("up", "up", "Brighter", show=False),
        Binding("down", "down", "Darker", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="color-plane")
        self.hue = 0.0
        self.saturation = 0.0
        self.value = 0.5
        self._paint()

    def set_hsv(self, hue: float, saturation: float, value: float, *, emit: bool = False) -> None:
        self.hue = hue % 1.0
        self.saturation = max(0.0, min(1.0, saturation))
        self.value = max(0.0, min(1.0, value))
        self._paint()
        if emit:
            self._emit_color()

    def _paint(self) -> None:
        selected_x = round(self.saturation * (_PLANE_WIDTH - 1))
        selected_y = round((1.0 - self.value) * (_PLANE_HEIGHT - 1))
        canvas = Text()
        for y in range(_PLANE_HEIGHT):
            value = 1.0 - (y / (_PLANE_HEIGHT - 1))
            for x in range(_PLANE_WIDTH):
                saturation = x / (_PLANE_WIDTH - 1)
                color = _hsv_to_hex(self.hue, saturation, value)
                marker = "◆" if x == selected_x and y == selected_y else " "
                canvas.append(marker, style=Style(color=_marker_color(value), bgcolor=color))
            if y < _PLANE_HEIGHT - 1:
                canvas.append("\n")
        self.update(canvas)

    def _emit_color(self) -> None:
        self.post_message(_PickerColorChanged(_hsv_to_hex(self.hue, self.saturation, self.value)))

    def _move(self, dx: int = 0, dy: int = 0) -> None:
        x = max(0, min(_PLANE_WIDTH - 1, round(self.saturation * (_PLANE_WIDTH - 1)) + dx))
        y = max(0, min(_PLANE_HEIGHT - 1, round((1.0 - self.value) * (_PLANE_HEIGHT - 1)) + dy))
        self.set_hsv(self.hue, x / (_PLANE_WIDTH - 1), 1.0 - y / (_PLANE_HEIGHT - 1), emit=True)

    def action_left(self) -> None:
        self._move(dx=-1)

    def action_right(self) -> None:
        self._move(dx=1)

    def action_up(self) -> None:
        self._move(dy=-1)

    def action_down(self) -> None:
        self._move(dy=1)

    def on_click(self, event: Click) -> None:
        x = int(event.x - self.content_offset.x)
        y = int(event.y - self.content_offset.y)
        if 0 <= x < _PLANE_WIDTH and 0 <= y < _PLANE_HEIGHT:
            self.set_hsv(self.hue, x / (_PLANE_WIDTH - 1), 1.0 - y / (_PLANE_HEIGHT - 1), emit=True)
            self.focus()
            event.stop()


class _HueStrip(Static):
    """Hue selector paired with :class:`_ColorPlane`."""

    can_focus = True
    BINDINGS = [
        Binding("left", "left", "Previous hue", show=False),
        Binding("right", "right", "Next hue", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="hue-strip")
        self.hue = 0.0
        self._paint()

    def set_hue(self, hue: float, *, emit: bool = False) -> None:
        self.hue = hue % 1.0
        self._paint()
        if emit:
            self.post_message(_PickerColorChanged(""))

    def _paint(self) -> None:
        selected_x = round(self.hue * (_PLANE_WIDTH - 1))
        strip = Text()
        for x in range(_PLANE_WIDTH):
            hue = x / _PLANE_WIDTH
            color = _hsv_to_hex(hue, 1.0, 1.0)
            strip.append("◆" if x == selected_x else " ", style=Style(color="black", bgcolor=color))
        self.update(strip)

    def _move(self, delta: int) -> None:
        x = (round(self.hue * (_PLANE_WIDTH - 1)) + delta) % _PLANE_WIDTH
        self.set_hue(x / (_PLANE_WIDTH - 1), emit=True)

    def action_left(self) -> None:
        self._move(-1)

    def action_right(self) -> None:
        self._move(1)

    def on_click(self, event: Click) -> None:
        x = int(event.x - self.content_offset.x)
        if 0 <= x < _PLANE_WIDTH:
            self.set_hue(x / (_PLANE_WIDTH - 1), emit=True)
            self.focus()
            event.stop()


class _ColorReadout(Static):
    def __init__(self) -> None:
        super().__init__("", id="color-readout")

    def set_color(self, label: str, value: str) -> None:
        color = _normalize_hex(value)
        text = Text(f"{label}  ", style="bold")
        if color:
            text.append("  ", style=Style(bgcolor=color))
            text.append(f"  {color}", style="bold")
        else:
            text.append("○  inherited from base", style="dim")
        self.update(text)


class ThemeDesignerDialog(ModalScreen[Any]):
    """Theme editor aligned with the app's standard modal visual language."""

    DEFAULT_CSS = """
    ThemeDesignerDialog {
        align: center middle;
        background: $theme-bg 60%;
    }
    ThemeDesignerDialog > #designer-window {
        layer: overlay;
        width: 78;
        max-width: 96%;
        height: 27;
        max-height: 94%;
        background: $theme-bg;
        border: round $theme-user;
        border-title-color: $theme-fg;
        border-title-background: $theme-top;
        border-title-style: bold;
        border-title-align: left;
        border-subtitle-color: $theme-muted;
        border-subtitle-align: right;
        padding: 0 1;
        layout: vertical;
    }
    ThemeDesignerDialog .section-title {
        height: 1;
        color: $theme-orange;
        text-style: bold;
        margin-top: 1;
    }
    #designer-meta {
        height: 2;
    }
    ThemeDesignerDialog .meta-row {
        height: 1;
        width: 1fr;
        align-vertical: middle;
    }
    ThemeDesignerDialog .meta-label {
        width: 7;
        color: $theme-dim;
        content-align: left middle;
    }
    ThemeDesignerDialog .meta-gap {
        width: 2;
    }
    ThemeDesignerDialog .meta-control {
        width: 1fr;
        height: 1;
        background: $theme-bar;
        color: $theme-fg;
        padding: 0 1;
    }
    ThemeDesignerDialog Input.meta-control:focus {
        background: $theme-top;
        color: $theme-fg;
    }
    ThemeDesignerDialog Select.meta-control > SelectCurrent {
        height: 1;
        min-height: 1;
        background: $theme-bar;
        color: $theme-fg;
        padding: 0;
        border: none;
    }
    ThemeDesignerDialog Select.meta-control:focus > SelectCurrent {
        background: $theme-top;
        border: none;
    }
    #designer-editor {
        height: 1fr;
        min-height: 15;
        margin-top: 1;
        border-top: solid $theme-border;
    }
    #color-roles {
        width: 33;
        min-width: 27;
        height: 1fr;
        background: $theme-bg;
        scrollbar-size: 0 0;
        overflow-x: hidden;
    }
    #color-roles:focus {
        background: $theme-bg;
    }
    _ColorRoleRow {
        width: 1fr;
        height: 1;
        color: $theme-dim;
        padding: 0 1;
    }
    _ColorRoleRow.-selected {
        color: $theme-fg;
        background: $theme-bar;
        text-style: bold;
    }
    #picker-panel {
        width: 1fr;
        min-width: 34;
        height: 1fr;
        padding-left: 2;
        border-left: solid $theme-border;
    }
    #color-readout {
        height: 2;
        color: $theme-fg;
        content-align: left middle;
    }
    #color-plane {
        width: 32;
        height: 10;
        border: tall $theme-border;
        background: $theme-bg;
    }
    #color-plane:focus {
        border: tall $theme-user;
    }
    #hue-title {
        height: 1;
        margin-top: 1;
        color: $theme-muted;
    }
    #hue-strip {
        width: 32;
        height: 3;
        border: tall $theme-border;
        background: $theme-bg;
    }
    #hue-strip:focus {
        border: tall $theme-user;
    }
    #picker-actions {
        height: 1;
        margin-top: 1;
    }
    #inherit-color {
        width: auto;
        color: $theme-user;
        text-style: bold;
    }
    #inherit-color:hover {
        color: $theme-fg;
        background: $theme-bar;
    }
    #picker-help {
        width: 1fr;
        color: $theme-muted;
        content-align: right middle;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
        Binding("delete", "inherit_color", "Use base", show=False),
    ]

    def __init__(self, settings: Any, project_root: Any = None) -> None:
        super().__init__()
        self._settings = settings
        self._project_root = project_root

        import synapse.ui.theme as tm

        current = tm.get_theme()
        self._original = current.name
        self._name = ""
        self._label = ""
        self._extends = current.name if current.name in list_theme_names() else "cursor-dark"
        self._prompt_border = str(getattr(current, "prompt_border", "tall") or "tall")
        if self._prompt_border not in _BORDER_STYLES:
            self._prompt_border = "tall"
        self._code_theme = str(getattr(current, "code_theme", "monokai") or "monokai")
        self._rich_user = str(getattr(current, "rich_user", "") or "")
        self._rich_info_border = str(getattr(current, "rich_info_border", "") or "")
        self._rich_ok_border = str(getattr(current, "rich_ok_border", "") or "")
        self._rich_activity = str(getattr(current, "rich_activity", "") or "")
        self._values = {key: str(getattr(current, key, "") or "") for key, _ in _COLOR_FIELDS}
        self._selected_color_key = _COLOR_FIELDS[0][0]
        self._picker_hsv = _hex_to_hsv(self._values[self._selected_color_key]) or (0.0, 0.0, 0.5)

        self._preview_seq = 0
        self._preview_busy = False
        self._dismiss_started = False
        self._ready = False
        self._last_preview_sig: str | None = None
        self._preview_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        theme_names = list_theme_names()
        with Vertical(id="designer-window"):
            yield Static("Theme details", classes="section-title")
            with Vertical(id="designer-meta"):
                with Horizontal(classes="meta-row"):
                    yield Label("Name", classes="meta-label")
                    yield Input(
                        placeholder="my-theme",
                        id="meta-name",
                        classes="meta-control",
                        compact=True,
                    )
                    yield Static("", classes="meta-gap")
                    yield Label("Label", classes="meta-label")
                    yield Input(
                        placeholder="My Theme",
                        id="meta-label",
                        classes="meta-control",
                        compact=True,
                    )
                with Horizontal(classes="meta-row"):
                    yield Label("Base", classes="meta-label")
                    yield Select(
                        [(name, name) for name in theme_names],
                        value=self._extends,
                        allow_blank=False,
                        id="meta-extends",
                        classes="meta-control",
                        compact=True,
                    )
                    yield Static("", classes="meta-gap")
                    yield Label("Border", classes="meta-label")
                    yield Select(
                        [(style.title(), style) for style in _BORDER_STYLES],
                        value=self._prompt_border,
                        allow_blank=False,
                        id="meta-prompt-border",
                        classes="meta-control",
                        compact=True,
                    )
            with Horizontal(id="designer-editor"):
                yield _ColorRoleList(self._values)
                with Vertical(id="picker-panel"):
                    yield _ColorReadout()
                    yield _ColorPlane()
                    yield Static("Hue", id="hue-title")
                    yield _HueStrip()
                    with Horizontal(id="picker-actions"):
                        yield Static("Use base color", id="inherit-color")
                        yield Static("mouse / arrows", id="picker-help")

    def on_mount(self) -> None:
        window = self.query_one("#designer-window")
        window.border_title = "◈ Theme Designer"
        window.border_subtitle = "Ctrl+S save · Esc cancel"
        self.query_one("#meta-name", Input).value = self._name
        self.query_one("#meta-label", Input).value = self._label
        self._sync_picker_from_selected()
        self.set_focus(self.query_one("#meta-name", Input))
        self.call_after_refresh(self._mark_ready)

    def _mark_ready(self) -> None:
        self._ready = True

    def on_click(self, event: Click) -> None:
        if self._dismiss_started:
            return
        if event.widget is self:
            self.action_cancel()

    @on(_ColorRoleSelected)
    def _on_color_role_selected(self, event: _ColorRoleSelected) -> None:
        self._selected_color_key = event.key
        self._sync_picker_from_selected()

    @on(_PickerColorChanged)
    def _on_picker_color_changed(self, event: _PickerColorChanged) -> None:
        plane = self.query_one("#color-plane", _ColorPlane)
        strip = self.query_one("#hue-strip", _HueStrip)
        if not event.value:
            plane.set_hsv(strip.hue, plane.saturation, plane.value)
            value = _hsv_to_hex(strip.hue, plane.saturation, plane.value)
        else:
            value = event.value
            hsv = _hex_to_hsv(value)
            if hsv is not None:
                strip.set_hue(hsv[0])
                plane.set_hsv(*hsv)
        self._picker_hsv = (plane.hue, plane.saturation, plane.value)
        self._on_color_changed(_DesignerColorChanged(self._selected_color_key, value))

    @on(_DesignerColorChanged)
    def _on_color_changed(self, event: _DesignerColorChanged) -> None:
        self._values[event.key] = event.value
        try:
            self.query_one("#color-roles", _ColorRoleList).update_value(event.key, event.value)
            if event.key == self._selected_color_key:
                self.query_one("#color-readout", _ColorReadout).set_color(
                    _COLOR_LABELS[event.key], event.value
                )
        except Exception:  # noqa: BLE001
            pass
        if _HEX_RE.match((event.value or "").strip()) or not event.value:
            self._schedule_preview()

    def _sync_picker_from_selected(self) -> None:
        value = self._values.get(self._selected_color_key, "")
        hsv = _hex_to_hsv(value)
        if hsv is not None:
            self._picker_hsv = hsv
        hue, saturation, brightness = self._picker_hsv
        try:
            self.query_one("#color-plane", _ColorPlane).set_hsv(hue, saturation, brightness)
            self.query_one("#hue-strip", _HueStrip).set_hue(hue)
            self.query_one("#color-readout", _ColorReadout).set_color(
                _COLOR_LABELS[self._selected_color_key], value
            )
        except Exception:  # noqa: BLE001
            pass

    @on(Input.Changed, "#meta-name")
    def _on_name_changed(self, event: Input.Changed) -> None:
        self._name = event.value.strip()

    @on(Input.Changed, "#meta-label")
    def _on_label_changed(self, event: Input.Changed) -> None:
        self._label = event.value.strip()

    @on(Select.Changed, "#meta-extends")
    def _on_extends_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._extends = event.value
            self._schedule_preview()

    @on(Select.Changed, "#meta-prompt-border")
    def _on_border_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in _BORDER_STYLES:
            self._prompt_border = event.value
            self._schedule_preview()

    @on(Click, "#inherit-color")
    def _on_inherit_clicked(self, event: Click) -> None:
        event.stop()
        self.action_inherit_color()

    def action_inherit_color(self) -> None:
        self._on_color_changed(_DesignerColorChanged(self._selected_color_key, ""))
        self._sync_picker_from_selected()

    def action_cancel(self) -> None:
        if self._dismiss_started:
            return
        self._dismiss_started = True
        self._cancel_preview_timer()
        self._restore_original()
        self.dismiss(None)
        self._defer_host_theme_apply(self._original, persist=False, announce=False)

    def action_save(self) -> None:
        if self._dismiss_started:
            return
        name = self._name or "custom"
        try:
            self._save_theme(name)
        except Exception as exc:  # noqa: BLE001
            try:
                self.query_one("#designer-window").border_subtitle = f"save failed: {exc}"
            except Exception:  # noqa: BLE001
                pass
            return
        self._dismiss_started = True
        self._cancel_preview_timer()
        self.dismiss(("theme", name))
        self._defer_host_theme_apply(name, persist=True, announce=True)

    def _build_theme_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "extends": self._extends or "cursor-dark",
            "label": self._label or self._name or "Custom",
            "prompt_border": self._prompt_border or "tall",
        }
        for key, _ in _COLOR_FIELDS:
            value = (self._values.get(key) or "").strip()
            if value:
                data[key] = value
        if self._code_theme:
            data["code_theme"] = self._code_theme
        for field, value in (
            ("rich_user", self._rich_user),
            ("rich_info_border", self._rich_info_border),
            ("rich_ok_border", self._rich_ok_border),
            ("rich_activity", self._rich_activity),
        ):
            if value:
                data[field] = value
        return data

    def _preview_signature(self, data: dict[str, Any]) -> str:
        items = sorted((str(key), str(value)) for key, value in data.items())
        return repr(items)

    def _cancel_preview_timer(self) -> None:
        timer = getattr(self, "_preview_timer", None)
        self._preview_timer = None
        self._preview_seq = int(getattr(self, "_preview_seq", 0)) + 1
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass

    def _schedule_preview(self) -> None:
        if getattr(self, "_dismiss_started", False) or not getattr(self, "_ready", False):
            return
        self._preview_seq = int(getattr(self, "_preview_seq", 0)) + 1
        sequence = self._preview_seq
        timer = getattr(self, "_preview_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._preview_timer = None

        def fire() -> None:
            self._preview_timer = None
            if getattr(self, "_dismiss_started", False) or sequence != self._preview_seq:
                return
            self._apply_preview()

        try:
            self._preview_timer = self.set_timer(_PREVIEW_DEBOUNCE_S, fire)
        except Exception:  # noqa: BLE001
            if not getattr(self, "_dismiss_started", False) and sequence == self._preview_seq:
                self._apply_preview()

    def _apply_preview(self) -> None:
        if self._dismiss_started or self._preview_busy:
            return
        self._preview_busy = True
        try:
            from synapse.ui.theme import _custom, set_active_theme

            data = self._build_theme_dict()
            signature = self._preview_signature(data)
            if signature == self._last_preview_sig:
                return
            theme = _theme_from_dict("__designer_temp__", data, catalog=dict(_custom))
            set_active_theme(theme)
            self._last_preview_sig = signature
            app = self.app
            if hasattr(app, "refresh_css"):
                app.refresh_css(animate=False)
            if hasattr(app, "_repaint_themed_widgets"):
                app._repaint_themed_widgets()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._preview_busy = False

    def _restore_original(self) -> None:
        try:
            set_theme(
                self._original,
                workspace=self._project_root,
                persist=False,
                reload=False,
            )
        except Exception:  # noqa: BLE001
            pass

    def _defer_host_theme_apply(
        self,
        name: str,
        *,
        persist: bool,
        announce: bool,
    ) -> None:
        """Refresh the host only after ``dismiss`` has queued screen removal."""
        try:
            app = self.app
        except Exception:  # noqa: BLE001
            return

        def apply_to_host() -> None:
            try:
                if hasattr(app, "apply_theme"):
                    app.apply_theme(name, persist=persist, announce=announce)
                elif hasattr(app, "refresh_css"):
                    app.refresh_css(animate=False)
            except Exception as exc:  # noqa: BLE001
                if hasattr(app, "append_event"):
                    app.append_event(f"theme failed: {exc}", "yellow")

        app.call_later(apply_to_host)

    def _save_theme(self, name: str) -> None:
        key = (name or "").strip()
        if not key:
            raise ValueError("theme name is empty")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key):
            raise ValueError("theme name must be [A-Za-z0-9._-]+")

        data = self._build_theme_dict()
        path = user_config_dir() / THEMES_FILENAME
        existing: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:  # noqa: BLE001
                existing = {}

        themes = existing.get("themes", {})
        if not isinstance(themes, dict):
            themes = {}
        themes[key] = data
        existing["themes"] = themes
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        reload_theme_catalog(self._project_root)
        set_theme(key, workspace=self._project_root, persist=True, reload=False)