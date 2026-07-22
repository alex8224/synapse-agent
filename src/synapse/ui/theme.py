"""UI theme registry: built-ins, layered themes.json, runtime switch.

Config surfaces:

- ``settings.json`` key ``theme`` (active name), loaded via ``Settings.theme``
- ``.coding-agent/themes.json`` (user → project layers)::

      {
        "themes": {
          "my-dark": {
            "extends": "cursor-dark",
            "label": "My Dark",
            "bg": "#0d1117",
            "user": "#58a6ff",
            "top_pad_x": 1,
            "top_gap": 3
          }
        }
      }

Topbar layout metrics (CSS / packing) and optional region bands:

- ``top`` — whole-row ``#topbar`` background via ``$theme-top``
- ``top_pad_x`` — horizontal CSS padding cells (default 1; ``$theme-top-pad-x``)
- ``top_gap`` — cells between left/center/right slots (default 3)
- ``top_left`` / ``top_center`` / ``top_right`` — optional per-region backgrounds
- omit or empty region colors → no bands (default for every built-in theme)

Built-in ``ansi`` inherits the terminal palette with transparent surfaces
(terminal wallpaper / acrylic). Aliases: inherit, terminal, auto.

Runtime:

- ``bootstrap_theme(name, workspace=...)`` at app start
- ``set_theme(name, persist=True)`` from ``/theme``
- ``get_theme()`` for Rich/Textual paint paths
- ``apply_textual_theme(app)`` switches Textual ``App.theme`` (needed for
  transparent / ANSI surfaces; solid palettes use dark/light shells)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from synapse.config_paths import (
    SETTINGS_FILENAME,
    existing_files,
    layered_config_dirs,
    load_json_object,
    load_layered_json,
    project_config_dir,
    user_config_dir,
)

THEMES_FILENAME = "themes.json"
DEFAULT_THEME_NAME = "cursor-dark"

# Palette keys that may appear in themes.json entries (besides name/label/extends).
_PALETTE_KEYS = frozenset(
    {
        "fg",
        "dim",
        "muted",
        "green",
        "orange",
        "bar",
        "bg",
        "top",
        "top_left",
        "top_center",
        "top_right",
        "top_pad_x",
        "top_gap",
        "user",
        "border",
        "border_focus",
        "error",
        "code_theme",
        "rich_user",
        "rich_info_border",
        "rich_ok_border",
        "rich_error",
        "rich_activity",
        "ansi",
        "css_fg",
        "css_dim",
        "css_muted",
        "css_green",
        "css_orange",
        "css_bar",
        "css_user",
        "css_error",
        "css_border",
        "css_border_focus",
    }
)

# Integer layout keys in themes.json (not colors).
_INT_PALETTE_KEYS = frozenset({"top_pad_x", "top_gap"})


@dataclass(frozen=True)
class Theme:
    """One complete UI palette (TUI CSS + Rich text styles).

    For terminal-inherit themes (``ansi``), Rich Text styles use names like
    ``default`` / ``bright_black``, while Textual CSS needs ``ansi_default`` /
    ``transparent``. Optional ``css_*`` fields override the CSS side only.
    """

    name: str
    label: str
    fg: str
    dim: str
    muted: str
    green: str
    orange: str
    bar: str
    bg: str
    top: str
    user: str
    border: str
    border_focus: str
    error: str = "#f28b82"
    code_theme: str = "monokai"
    rich_user: str = "bold cyan"
    rich_info_border: str = "blue"
    rich_ok_border: str = "green"
    rich_error: str = "bold red"
    rich_activity: str = "cyan"
    # When True, surfaces stay transparent and Textual uses native ANSI colors.
    ansi: bool = False
    # Topbar left/center/right band backgrounds (empty = derived defaults).
    # ``none`` / ``off`` / ``transparent`` suppresses that band.
    top_left: str = ""
    top_center: str = ""
    top_right: str = ""
    # Outer horizontal padding of #topbar (cells). CSS: $theme-top-pad-x.
    top_pad_x: int = 1
    # Gap cells between left/center/right slots (packing gap_after).
    top_gap: int = 3
    # CSS-only overrides (empty -> use the matching Rich field above).
    css_fg: str = ""
    css_dim: str = ""
    css_muted: str = ""
    css_green: str = ""
    css_orange: str = ""
    css_bar: str = ""
    css_user: str = ""
    css_error: str = ""
    css_border: str = ""
    css_border_focus: str = ""

    def css_variables(self) -> dict[str, str]:
        """Textual stylesheet variables (names without leading ``$``)."""
        pad = max(0, int(self.top_pad_x or 0))
        return {
            "theme-fg": self.css_fg or self.fg,
            "theme-dim": self.css_dim or self.dim,
            "theme-muted": self.css_muted or self.muted,
            "theme-green": self.css_green or self.green,
            "theme-orange": self.css_orange or self.orange,
            "theme-bar": self.css_bar or self.bar,
            "theme-bg": self.bg,
            "theme-top": self.top,
            "theme-user": self.css_user or self.user,
            "theme-border": self.css_border or self.border,
            "theme-border-focus": self.css_border_focus or self.border_focus,
            "theme-error": self.css_error or self.error,
            "theme-top-pad-x": str(pad),
        }

    @property
    def is_terminal_inherit(self) -> bool:
        """True when chrome should inherit terminal bg (transparent / ANSI)."""
        if self.ansi:
            return True
        bg = (self.bg or "").strip().casefold()
        return bg in {"transparent", "ansi_default", "default"}

    def topbar_region_bands(self) -> dict[str, tuple[str, str]]:
        """Resolved left/center/right ``(fg, bg)`` for optional region bands.

        Empty ``bg`` means no band paint (widget CSS ``$theme-top`` shows).
        Built-ins leave ``top_*`` empty so defaults paint no blocks; set
        ``top_left`` / ``top_center`` / ``top_right`` in themes.json to enable.
        """

        def resolve(explicit: str) -> str:
            key = (explicit or "").strip()
            if not key:
                return ""
            low = key.casefold()
            if low in {"none", "off", "false", "0", "transparent", "inherit", "default"}:
                return ""
            return key

        left_bg = resolve(self.top_left)
        center_bg = resolve(self.top_center)
        right_bg = resolve(self.top_right)
        left_fg = self.fg or ("default" if self.is_terminal_inherit else "")
        center_fg = self.fg or ("default" if self.is_terminal_inherit else "")
        right_fg = self.dim or ("bright_black" if self.is_terminal_inherit else "")

        return {
            "left": (left_fg, left_bg),
            "center": (center_fg, center_bg),
            "right": (right_fg, right_bg),
        }


def _t(
    name: str,
    label: str,
    *,
    fg: str,
    dim: str,
    muted: str,
    green: str,
    orange: str,
    bar: str,
    bg: str,
    top: str,
    user: str,
    border: str,
    border_focus: str,
    error: str = "#f28b82",
    code_theme: str = "monokai",
    rich_user: str = "bold cyan",
    rich_info_border: str = "blue",
    rich_ok_border: str = "green",
    rich_error: str = "bold red",
    rich_activity: str = "cyan",
    ansi: bool = False,
    top_left: str = "",
    top_center: str = "",
    top_right: str = "",
    top_pad_x: int = 1,
    top_gap: int = 3,
    css_fg: str = "",
    css_dim: str = "",
    css_muted: str = "",
    css_green: str = "",
    css_orange: str = "",
    css_bar: str = "",
    css_user: str = "",
    css_error: str = "",
    css_border: str = "",
    css_border_focus: str = "",
) -> Theme:
    return Theme(
        name=name,
        label=label,
        fg=fg,
        dim=dim,
        muted=muted,
        green=green,
        orange=orange,
        bar=bar,
        bg=bg,
        top=top,
        user=user,
        border=border,
        border_focus=border_focus,
        error=error,
        code_theme=code_theme,
        rich_user=rich_user,
        rich_info_border=rich_info_border,
        rich_ok_border=rich_ok_border,
        rich_error=rich_error,
        rich_activity=rich_activity,
        ansi=ansi,
        top_left=top_left,
        top_center=top_center,
        top_right=top_right,
        top_pad_x=max(0, int(top_pad_x or 0)),
        top_gap=max(0, int(top_gap or 0)),
        css_fg=css_fg,
        css_dim=css_dim,
        css_muted=css_muted,
        css_green=css_green,
        css_orange=css_orange,
        css_bar=css_bar,
        css_user=css_user,
        css_error=css_error,
        css_border=css_border,
        css_border_focus=css_border_focus,
    )


# Built-in classic palettes (dark + light + terminal-inherit ansi).
BUILTIN_THEMES: dict[str, Theme] = {
    # Inherit terminal colors; transparent surfaces (acrylic / wallpaper).
    # Rich Text: default / bright_black / green (no ansi_ prefix).
    # CSS: transparent + ansi_* tokens for Textual native ANSI path.
    "ansi": _t(
        "ansi",
        "Terminal (transparent)",
        fg="default",
        dim="bright_black",
        muted="bright_black",
        green="green",
        orange="yellow",
        bar="default",
        bg="transparent",
        top="transparent",
        user="cyan",
        border="bright_black",
        border_focus="cyan",
        error="red",
        code_theme="ansi_dark",
        rich_user="bold cyan",
        rich_info_border="cyan",
        rich_ok_border="green",
        rich_error="bold red",
        rich_activity="cyan",
        ansi=True,
        css_fg="ansi_default",
        css_dim="ansi_bright_black",
        css_muted="ansi_bright_black",
        css_green="ansi_green",
        css_orange="ansi_yellow",
        css_bar="transparent",
        css_user="ansi_cyan",
        css_error="ansi_red",
        css_border="ansi_bright_black",
        css_border_focus="ansi_cyan",
    ),
    "cursor-dark": _t(
        "cursor-dark",
        "Cursor Dark",
        fg="#e8eaed",
        dim="#9aa0a6",
        muted="#5f6368",
        green="#81c995",
        orange="#f4b183",
        bar="#2b2d31",
        bg="#1a1b1e",
        top="#121316",
        user="#8ab4f8",
        border="#3c4043",
        border_focus="#5f6368",
        code_theme="monokai",
    ),
    "github-dark": _t(
        "github-dark",
        "GitHub Dark",
        fg="#e6edf3",
        dim="#8b949e",
        muted="#6e7681",
        green="#3fb950",
        orange="#d29922",
        bar="#21262d",
        bg="#0d1117",
        top="#010409",
        user="#58a6ff",
        border="#30363d",
        border_focus="#8b949e",
        error="#f85149",
        code_theme="github-dark",
        rich_user="bold #58a6ff",
        rich_info_border="#58a6ff",
        rich_ok_border="#3fb950",
        rich_activity="#58a6ff",
    ),
    "dracula": _t(
        "dracula",
        "Dracula",
        fg="#f8f8f2",
        dim="#bd93f9",
        muted="#6272a4",
        green="#50fa7b",
        orange="#ffb86c",
        bar="#44475a",
        bg="#282a36",
        top="#21222c",
        user="#8be9fd",
        border="#6272a4",
        border_focus="#bd93f9",
        error="#ff5555",
        code_theme="dracula",
        rich_user="bold #8be9fd",
        rich_info_border="#bd93f9",
        rich_ok_border="#50fa7b",
        rich_activity="#ff79c6",
    ),
    "nord": _t(
        "nord",
        "Nord",
        fg="#eceff4",
        dim="#d8dee9",
        muted="#4c566a",
        green="#a3be8c",
        orange="#d08770",
        bar="#3b4252",
        bg="#2e3440",
        top="#242933",
        user="#88c0d0",
        border="#4c566a",
        border_focus="#81a1c1",
        error="#bf616a",
        code_theme="nord",
        rich_user="bold #88c0d0",
        rich_info_border="#81a1c1",
        rich_ok_border="#a3be8c",
        rich_activity="#88c0d0",
    ),
    "solarized-dark": _t(
        "solarized-dark",
        "Solarized Dark",
        fg="#93a1a1",
        dim="#839496",
        muted="#586e75",
        green="#859900",
        orange="#cb4b16",
        bar="#073642",
        bg="#002b36",
        top="#001f27",
        user="#268bd2",
        border="#586e75",
        border_focus="#839496",
        error="#dc322f",
        code_theme="solarized-dark",
        rich_user="bold #268bd2",
        rich_info_border="#268bd2",
        rich_ok_border="#859900",
        rich_activity="#2aa198",
    ),
    "solarized-light": _t(
        "solarized-light",
        "Solarized Light",
        fg="#465c63",
        dim="#526b73",
        muted="#657b83",
        green="#859900",
        orange="#cb4b16",
        bar="#eee8d5",
        bg="#fdf6e3",
        top="#eee8d5",
        user="#268bd2",
        border="#93a1a1",
        border_focus="#657b83",
        error="#dc322f",
        code_theme="solarized-light",
        rich_user="bold #268bd2",
        rich_info_border="#268bd2",
        rich_ok_border="#859900",
        rich_activity="#2aa198",
    ),
    "catppuccin-mocha": _t(
        "catppuccin-mocha",
        "Catppuccin Mocha",
        fg="#cdd6f4",
        dim="#a6adc8",
        muted="#6c7086",
        green="#a6e3a1",
        orange="#fab387",
        bar="#313244",
        bg="#1e1e2e",
        top="#181825",
        user="#89b4fa",
        border="#45475a",
        border_focus="#89b4fa",
        error="#f38ba8",
        code_theme="monokai",
        rich_user="bold #89b4fa",
        rich_info_border="#89b4fa",
        rich_ok_border="#a6e3a1",
        rich_activity="#cba6f7",
    ),
    "one-dark": _t(
        "one-dark",
        "One Dark",
        fg="#abb2bf",
        dim="#828997",
        muted="#5c6370",
        green="#98c379",
        orange="#d19a66",
        bar="#2c313c",
        bg="#282c34",
        top="#21252b",
        user="#61afef",
        border="#3e4451",
        border_focus="#61afef",
        error="#e06c75",
        code_theme="one-dark",
        rich_user="bold #61afef",
        rich_info_border="#61afef",
        rich_ok_border="#98c379",
        rich_activity="#c678dd",
    ),
    "github-light": _t(
        "github-light",
        "GitHub Light",
        fg="#1f2328",
        dim="#656d76",
        muted="#6b7280",
        green="#1a7f37",
        orange="#9a6700",
        bar="#d8dee4",
        bg="#f6f8fa",
        top="#eaeef2",
        user="#0969da",
        border="#b8c2cc",
        border_focus="#0969da",
        error="#cf222e",
        code_theme="default",
        rich_user="bold #0969da",
        rich_info_border="#0969da",
        rich_ok_border="#1a7f37",
        rich_activity="#8250df",
    ),
    "one-light": _t(
        "one-light",
        "One Light",
        fg="#383a42",
        dim="#5d616c",
        muted="#737680",
        green="#50a14f",
        orange="#c18401",
        bar="#dedfe2",
        bg="#f5f5f6",
        top="#e8e8ea",
        user="#4078f2",
        border="#b9bbc1",
        border_focus="#4078f2",
        error="#e45649",
        code_theme="default",
        rich_user="bold #4078f2",
        rich_info_border="#4078f2",
        rich_ok_border="#50a14f",
        rich_activity="#a626a4",
    ),
    "gruvbox-light": _t(
        "gruvbox-light",
        "Gruvbox Light",
        fg="#3c3836",
        dim="#6f6258",
        muted="#7c6f64",
        green="#79740e",
        orange="#af3a03",
        bar="#ebdbb2",
        bg="#fbf1c7",
        top="#f2e5bc",
        user="#076678",
        border="#d5c4a1",
        border_focus="#076678",
        error="#cc241d",
        code_theme="default",
        rich_user="bold #076678",
        rich_info_border="#076678",
        rich_ok_border="#79740e",
        rich_activity="#8f3f71",
    ),
    "catppuccin-latte": _t(
        "catppuccin-latte",
        "Catppuccin Latte",
        fg="#4c4f69",
        dim="#5e6178",
        muted="#73778a",
        green="#40a02b",
        orange="#fe640b",
        bar="#e6e9ef",
        bg="#eff1f5",
        top="#dce0e8",
        user="#1e66f5",
        border="#ccd0da",
        border_focus="#1e66f5",
        error="#d20f39",
        code_theme="default",
        rich_user="bold #1e66f5",
        rich_info_border="#1e66f5",
        rich_ok_border="#40a02b",
        rich_activity="#8839ef",
    ),
    "tokyo-night-light": _t(
        "tokyo-night-light",
        "Tokyo Night Light",
        fg="#343b58",
        dim="#4c5168",
        muted="#666d92",
        green="#485e30",
        orange="#965027",
        bar="#c8d3f5",
        bg="#d5d6db",
        top="#c0c2ce",
        user="#2e7de9",
        border="#a8aecb",
        border_focus="#2e7de9",
        error="#8c4351",
        code_theme="default",
        rich_user="bold #2e7de9",
        rich_info_border="#2e7de9",
        rich_ok_border="#485e30",
        rich_activity="#9854f1",
    ),
    "ayu-light": _t(
        "ayu-light",
        "Ayu Light",
        fg="#575f66",
        dim="#5d6875",
        muted="#687582",
        green="#6cbf43",
        orange="#f29718",
        bar="#e3e7eb",
        bg="#f6f8fa",
        top="#e7ebef",
        user="#399ee6",
        border="#c5cbd2",
        border_focus="#399ee6",
        error="#f07178",
        code_theme="default",
        rich_user="bold #399ee6",
        rich_info_border="#399ee6",
        rich_ok_border="#6cbf43",
        rich_activity="#a37acc",
    ),
    "nord-light": _t(
        "nord-light",
        "Nord Light",
        fg="#3b4252",
        dim="#4c566a",
        muted="#60738d",
        green="#719839",
        orange="#c98245",
        bar="#e5e9f0",
        bg="#eceff4",
        top="#d8dee9",
        user="#5e81ac",
        border="#d8dee9",
        border_focus="#5e81ac",
        error="#bf616a",
        code_theme="default",
        rich_user="bold #5e81ac",
        rich_info_border="#5e81ac",
        rich_ok_border="#719839",
        rich_activity="#b48ead",
    ),
}


_active: Theme = BUILTIN_THEMES[DEFAULT_THEME_NAME]
_custom: dict[str, Theme] = {}
_listeners: list[Callable[[Theme], None]] = []
_loaded_workspace: str | None = None


def on_theme_change(callback: Callable[[Theme], None]) -> None:
    """Register a listener invoked after the active theme changes."""
    if callback not in _listeners:
        _listeners.append(callback)


def get_theme() -> Theme:
    return _active


def builtin_theme_names() -> list[str]:
    return list(BUILTIN_THEMES.keys())


def list_theme_names() -> list[str]:
    """Built-ins first (stable order), then custom names sorted."""
    names = list(BUILTIN_THEMES.keys())
    extras = sorted(n for n in _custom if n not in BUILTIN_THEMES)
    return names + extras


def list_themes() -> list[Theme]:
    return [get_theme_by_name(n) for n in list_theme_names()]


def get_theme_by_name(name: str) -> Theme:
    key = (name or "").strip()
    if key in _custom:
        return _custom[key]
    if key in BUILTIN_THEMES:
        return BUILTIN_THEMES[key]
    raise KeyError(f"unknown theme: {name!r}")


def _normalize_color(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _theme_from_dict(
    name: str,
    data: dict[str, Any],
    *,
    catalog: dict[str, Theme],
    stack: list[str] | None = None,
) -> Theme:
    """Build a Theme from a config dict, supporting ``extends``."""
    stack = list(stack or [])
    if name in stack:
        raise ValueError(f"theme extends cycle: {' -> '.join([*stack, name])}")
    stack.append(name)

    extends = str(data.get("extends") or data.get("base") or "").strip()
    if extends:
        if extends in catalog:
            base = catalog[extends]
        elif extends in BUILTIN_THEMES:
            base = BUILTIN_THEMES[extends]
        elif extends in _custom:
            base = _custom[extends]
        else:
            # Resolve peer custom not yet fully built.
            raise KeyError(f"theme {name!r} extends unknown base {extends!r}")
    else:
        base = BUILTIN_THEMES[DEFAULT_THEME_NAME]

    label = str(data.get("label") or data.get("title") or name).strip() or name
    updates: dict[str, Any] = {"name": name, "label": label}
    for key in _PALETTE_KEYS:
        if key not in data:
            continue
        raw = data[key]
        if key == "code_theme":
            val = str(raw).strip() if raw is not None else ""
            if val:
                updates[key] = val
            continue
        if key == "ansi":
            if isinstance(raw, bool):
                updates[key] = raw
            elif raw is not None:
                updates[key] = str(raw).strip().casefold() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            continue
        if key in _INT_PALETTE_KEYS:
            try:
                updates[key] = max(0, int(raw))
            except (TypeError, ValueError):
                pass
            continue
        color = _normalize_color(raw)
        if color is not None:
            updates[key] = color
    return replace(base, **updates)


def _parse_themes_blob(blob: dict[str, Any]) -> dict[str, Theme]:
    """Parse a themes.json root object into name -> Theme."""
    section = blob.get("themes")
    if section is None:
        # Allow flat map of name -> palette (without wrapper).
        section = {
            k: v
            for k, v in blob.items()
            if k not in {"active", "default", "theme"} and isinstance(v, dict)
        }
    if not isinstance(section, dict):
        return {}

    # Multi-pass so extends can refer to peers defined in the same file.
    pending: dict[str, dict[str, Any]] = {
        str(k).strip(): v for k, v in section.items() if str(k).strip() and isinstance(v, dict)
    }
    built: dict[str, Theme] = {}
    # Prefer pure built-in extends first.
    guard = 0
    while pending and guard < 32:
        guard += 1
        progress = False
        for name in list(pending.keys()):
            data = pending[name]
            extends = str(data.get("extends") or data.get("base") or "").strip()
            if (
                extends
                and extends not in BUILTIN_THEMES
                and extends not in built
                and extends in pending
            ):
                continue
            try:
                built[name] = _theme_from_dict(name, data, catalog=built)
            except KeyError:
                continue
            del pending[name]
            progress = True
        if not progress:
            break
    # Last attempt: force-build remaining against defaults / partial catalog.
    for name, data in list(pending.items()):
        try:
            built[name] = _theme_from_dict(name, data, catalog=built)
        except Exception:  # noqa: BLE001
            continue
    return built


def load_custom_themes(workspace: Path | str | None = None) -> dict[str, Theme]:
    """Load layered ``themes.json`` (user → project). Later layers override."""
    merged, _paths = load_layered_json(THEMES_FILENAME, workspace)
    if not merged:
        return {}
    return _parse_themes_blob(merged)


def reload_theme_catalog(workspace: Path | str | None = None) -> dict[str, Theme]:
    """Refresh the in-memory custom catalog from disk."""
    global _custom, _loaded_workspace
    _custom = load_custom_themes(workspace)
    try:
        _loaded_workspace = str(Path(workspace).resolve()) if workspace is not None else None
    except Exception:  # noqa: BLE001
        _loaded_workspace = str(workspace) if workspace is not None else None
    return dict(_custom)


def _notify(theme: Theme) -> None:
    for cb in list(_listeners):
        try:
            cb(theme)
        except Exception:  # noqa: BLE001
            continue


def set_active_theme(theme: Theme) -> Theme:
    """Set the process-wide active theme and notify listeners."""
    global _active
    _active = theme
    _notify(theme)
    return theme


# User-facing aliases that map to the terminal-inherit palette.
_THEME_ALIASES: dict[str, str] = {
    "inherit": "ansi",
    "terminal": "ansi",
    "auto": "ansi",
    "default": "ansi",
    "transparent": "ansi",
}

# Textual App.theme names registered by ensure_textual_themes().
TEXTUAL_THEME_ANSI = "synapse-ansi"
TEXTUAL_THEME_DARK = "synapse-dark"
TEXTUAL_THEME_LIGHT = "synapse-light"


def resolve_theme_name(name: str | None) -> str:
    text = (name or "").strip()
    if not text:
        return DEFAULT_THEME_NAME
    key = text.casefold()
    if key in _THEME_ALIASES:
        return _THEME_ALIASES[key]
    if text in BUILTIN_THEMES or text in _custom:
        return text
    for candidate in list(BUILTIN_THEMES) + list(_custom):
        if candidate.casefold() == key:
            return candidate
    return text


def _is_light_hex(color: str) -> bool:
    """Rough relative-luminance check for solid #rrggbb backgrounds."""
    c = (color or "").strip().lstrip("#")
    if len(c) < 6:
        return False
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return False
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return lum > 0.5


def theme_kind(theme: Theme | None = None) -> str:
    """Classify a palette as ``ansi``, ``light``, or ``dark``."""
    t = theme or get_theme()
    if t.is_terminal_inherit:
        return "ansi"
    return "light" if _is_light_hex(t.bg) else "dark"


def textual_themes() -> list[Any]:
    """Build Textual Theme objects for ``App.register_theme``."""
    from textual.theme import Theme as TextualTheme

    coding_ansi = TextualTheme(
        name=TEXTUAL_THEME_ANSI,
        primary="ansi_cyan",
        secondary="ansi_blue",
        warning="ansi_yellow",
        error="ansi_red",
        success="ansi_green",
        accent="ansi_cyan",
        foreground="ansi_default",
        background="transparent",
        surface="transparent",
        panel="transparent",
        boost="transparent",
        dark=True,
        ansi=True,
        variables={
            # Built-in ansi-dark paints solid black; keep acrylic/wallpaper.
            "ansi-background": "transparent",
            "ansi-foreground": "ansi_default",
            "border-blurred": "ansi_bright_black",
            "input-cursor-background": "ansi_default",
            "input-cursor-foreground": "ansi_default",
            "input-selection-background": "ansi_bright_blue",
            "input-selection-foreground": "ansi_black",
            "footer-background": "transparent",
        },
    )
    coding_dark = TextualTheme(
        name=TEXTUAL_THEME_DARK,
        primary="#89b4fa",
        secondary="#cba6f7",
        warning="#f4b183",
        error="#f38ba8",
        success="#81c995",
        accent="#89b4fa",
        foreground="#e8eaed",
        background="#1a1b1e",
        surface="#1a1b1e",
        panel="#121316",
        boost="#2b2d31",
        dark=True,
        ansi=False,
    )
    coding_light = TextualTheme(
        name=TEXTUAL_THEME_LIGHT,
        primary="#0969da",
        secondary="#8250df",
        warning="#9a6700",
        error="#cf222e",
        success="#1a7f37",
        accent="#0969da",
        foreground="#1f2328",
        background="#f6f8fa",
        surface="#ffffff",
        panel="#ffffff",
        boost="#eaeef2",
        dark=False,
        ansi=False,
    )
    return [coding_ansi, coding_dark, coding_light]


def ensure_textual_themes(app: Any) -> None:
    """Register synapse-* Textual themes on an App instance (idempotent)."""
    for th in textual_themes():
        try:
            app.register_theme(th)
        except Exception:  # noqa: BLE001
            pass


def apply_textual_theme(app: Any, theme: Theme | None = None) -> str:
    """Switch ``App.theme`` so Textual surfaces match the active palette.

    Returns the Textual theme name applied (or attempted).
    """
    pal = theme or get_theme()
    kind = theme_kind(pal)
    if kind == "ansi":
        name = TEXTUAL_THEME_ANSI
    elif kind == "light":
        name = TEXTUAL_THEME_LIGHT
    else:
        name = TEXTUAL_THEME_DARK
    ensure_textual_themes(app)
    try:
        app.theme = name
        return name
    except Exception:  # noqa: BLE001
        fallback = {
            TEXTUAL_THEME_ANSI: "ansi-dark",
            TEXTUAL_THEME_DARK: "textual-dark",
            TEXTUAL_THEME_LIGHT: "textual-light",
        }.get(name, "textual-dark")
        try:
            app.theme = fallback
        except Exception:  # noqa: BLE001
            pass
        return fallback


def set_theme(
    name: str | None,
    *,
    workspace: Path | str | None = None,
    persist: bool = False,
    scope: str = "user",
    reload: bool = True,
) -> Theme:
    """Activate a theme by name. Optionally persist to settings.json."""
    if reload:
        reload_theme_catalog(workspace)
    key = resolve_theme_name(name)
    try:
        theme = get_theme_by_name(key)
    except KeyError as exc:
        raise KeyError(
            f"unknown theme: {key!r}. available: {', '.join(list_theme_names())}"
        ) from exc
    set_active_theme(theme)
    if persist:
        persist_theme_preference(theme.name, workspace=workspace, scope=scope)
    return theme


def bootstrap_theme(
    name: str | None = None,
    *,
    workspace: Path | str | None = None,
) -> Theme:
    """Load customs + activate ``name`` (fallback: default). Never raises on bad name."""
    reload_theme_catalog(workspace)
    key = resolve_theme_name(name)
    try:
        return set_theme(key, workspace=workspace, persist=False, reload=False)
    except KeyError:
        return set_theme(DEFAULT_THEME_NAME, workspace=workspace, persist=False, reload=False)


def themes_config_paths(workspace: Path | str | None = None) -> list[Path]:
    return existing_files(layered_config_dirs(workspace), THEMES_FILENAME)


def persist_theme_preference(
    name: str,
    *,
    workspace: Path | str | None = None,
    scope: str = "user",
) -> Path:
    """Write ``theme`` into user or project ``settings.json`` (merge, keep other keys)."""
    key = resolve_theme_name(name)
    if scope == "project":
        target_dir = project_config_dir(workspace)
    else:
        target_dir = user_config_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / SETTINGS_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = load_json_object(path)
        except Exception:  # noqa: BLE001
            data = {}
    data["theme"] = key
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def format_theme_list_lines(*, active: str | None = None) -> list[str]:
    """Human-readable theme catalog for ``/theme``."""
    current = (active or get_theme().name).strip() or DEFAULT_THEME_NAME
    lines = [f"theme: {current}", "available:"]
    for theme in list_themes():
        mark = "*" if theme.name == current else " "
        tone = theme_kind(theme)
        if theme.name in _custom and theme.name not in BUILTIN_THEMES:
            kind = f"custom/{tone}"
        elif theme.name in _custom:
            kind = f"override/{tone}"
        else:
            kind = f"built-in/{tone}"
        lines.append(f"  {mark} {theme.name:20} {theme.label}  ({kind})")
    lines.append("usage: /theme <name>")
    lines.append("       /theme list")
    lines.append("       /theme ansi|inherit  (terminal transparent)")
    lines.append("config: settings.json theme + optional themes.json")
    return lines


def theme_field_names() -> list[str]:
    return [f.name for f in fields(Theme)]


__all__ = [
    "BUILTIN_THEMES",
    "DEFAULT_THEME_NAME",
    "TEXTUAL_THEME_ANSI",
    "TEXTUAL_THEME_DARK",
    "TEXTUAL_THEME_LIGHT",
    "THEMES_FILENAME",
    "Theme",
    "apply_textual_theme",
    "bootstrap_theme",
    "builtin_theme_names",
    "ensure_textual_themes",
    "format_theme_list_lines",
    "get_theme",
    "get_theme_by_name",
    "list_theme_names",
    "list_themes",
    "load_custom_themes",
    "on_theme_change",
    "persist_theme_preference",
    "reload_theme_catalog",
    "resolve_theme_name",
    "set_active_theme",
    "set_theme",
    "textual_themes",
    "theme_field_names",
    "theme_kind",
    "themes_config_paths",
]
