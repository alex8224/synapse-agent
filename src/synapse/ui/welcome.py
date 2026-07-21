"""Animated welcome screen for the Synapse TUI."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

_BRAILLE_BLANK = "\u2800"
_SHIMMER_FPS = 12.0
_PULSE_SECONDS = 5.0
_RIPPLE_SECONDS = 3.4
_APPEAR_SPREAD = 0.08
_APPEAR_RAMP = 0.5
_BRAILLE_DOTS = (
    ((0, 0, 1), (1, 0, 2), (2, 0, 4), (3, 0, 64)),
    ((0, 1, 8), (1, 1, 16), (2, 1, 32), (3, 1, 128)),
)
_WORD_BITMAPS = {
    "S": ("11110", "10000", "10000", "11110", "00001", "00001", "11110"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "N": ("10001", "11001", "11001", "10101", "10011", "10011", "10001"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
}


def _braille_cell(bitmap: list[str], row: int, column: int) -> str:
    value = 0
    for row_offset, column_offset, bit in (
        dot for column_dots in _BRAILLE_DOTS for dot in column_dots
    ):
        source_row = row * 4 + row_offset
        source_column = column * 2 + column_offset
        if (
            source_row < len(bitmap)
            and source_column < len(bitmap[source_row])
            and bitmap[source_row][source_column] == "1"
        ):
            value |= bit
    return chr(0x2800 + value)


def _braille_glyph(pattern: tuple[str, ...], x_scale: int, y_scale: int) -> tuple[str, ...]:
    bitmap = [
        "".join(pixel * x_scale for pixel in source_row)
        for source_row in pattern
        for _ in range(y_scale)
    ]
    cell_rows = (len(bitmap) + 3) // 4
    cell_columns = (len(bitmap[0]) + 1) // 2
    return tuple(
        "".join(_braille_cell(bitmap, row, column) for column in range(cell_columns))
        for row in range(cell_rows)
    )


def _build_braille_logo(x_scale: int, y_scale: int) -> tuple[str, ...]:
    letters = "SYNAPSE"
    glyphs = {
        letter: _braille_glyph(pattern, x_scale, y_scale)
        for letter, pattern in _WORD_BITMAPS.items()
    }
    return tuple(
        _BRAILLE_BLANK.join(glyphs[letter][letter_row] for letter in letters)
        for letter_row in range(len(next(iter(glyphs.values()))))
    )


_LOGO = _build_braille_logo(x_scale=3, y_scale=4)
_COMPACT_LOGO = _build_braille_logo(x_scale=2, y_scale=2)

def _workspace_label(workspace: str | Path) -> str:
    value = str(workspace or "workspace").rstrip("/\\")
    return Path(value).name or value or "workspace"


def _blend_color(start: str, end: str, amount: float) -> str:
    """Blend theme colors without requiring them to be literal hex values."""
    amount = max(0.0, min(1.0, amount))
    start = start.strip()
    end = end.strip()
    if not (
        start.startswith("#")
        and end.startswith("#")
        and len(start) == 7
        and len(end) == 7
    ):
        return end if amount >= 0.5 else start
    try:
        start_rgb = tuple(int(start[index : index + 2], 16) for index in (1, 3, 5))
        end_rgb = tuple(int(end[index : index + 2], 16) for index in (1, 3, 5))
    except ValueError:
        return end if amount >= 0.5 else start
    rgb = tuple(
        round(left + (right - left) * amount)
        for left, right in zip(start_rgb, end_rgb, strict=True)
    )
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _breathing_intensity(frame: int, column: int, row: int, width: int) -> float:
    """Return a gentle breathing glow with a subtle ripple from the center."""
    elapsed = frame / _SHIMMER_FPS
    breath = 0.5 + 0.5 * math.sin(
        2 * math.pi * elapsed / _PULSE_SECONDS - math.pi / 2
    )
    center_distance = abs(column - (width - 1) / 2)
    ripple_phase = (
        2 * math.pi * elapsed / _RIPPLE_SECONDS
        - center_distance * 0.62
        + row * 0.3
    )
    ripple = 0.5 + 0.5 * math.sin(ripple_phase)
    intensity = max(0.18, min(0.94, 0.26 + 0.54 * breath + 0.14 * ripple))
    alpha = max(0.0, min(1.0, (elapsed - center_distance * _APPEAR_SPREAD) / _APPEAR_RAMP))
    return intensity * alpha


def render_welcome_frame(
    frame: int,
    *,
    workspace: str | Path = "workspace",
    compact: bool = False,
    theme: Any | None = None,
) -> Text:
    """Build one animation frame as theme-aware Rich text."""
    if theme is None:
        from synapse.ui.theme import get_theme

        theme = get_theme()

    fg = str(getattr(theme, "fg", "#e8eaed"))
    dim = str(getattr(theme, "dim", "#9aa0a6"))
    muted = str(getattr(theme, "muted", "#5f6368"))
    accent = str(getattr(theme, "user", "#8ab4f8"))
    green = str(getattr(theme, "green", "#81c995"))

    out = Text(justify="center")
    logo = _COMPACT_LOGO if compact else _LOGO
    logo_width = max(cell_len(line) for line in logo)
    for row, line in enumerate(logo):
        left = max(0, (logo_width - cell_len(line)) // 2)
        for column, char in enumerate(line):
            if char in {" ", _BRAILLE_BLANK}:
                style = muted
            else:
                intensity = _breathing_intensity(
                    frame,
                    left + column,
                    row,
                    logo_width,
                )
                if intensity >= 0.72:
                    color = _blend_color(
                        fg,
                        accent,
                        (intensity - 0.72) / 0.28,
                    )
                    style = f"bold {color}"
                else:
                    style = _blend_color(dim, fg, intensity / 0.72)
            out.append(char, style=style)
        out.append("\n")

    out.append("\n", style=muted)
    out.append("\nLOCAL CODING INTELLIGENCE\n", style=f"bold {green}")
    out.append("Inspect. Plan. Build. Verify.\n", style=dim)
    out.append(f"\n{_workspace_label(workspace)}\n", style=f"bold {fg}")
    out.append("Describe the outcome you want to create.\n", style=dim)
    out.append("@ files   / commands   F2 model   F3 theme", style=muted)
    return out


class WelcomeView(Static):
    """A restrained animated Braille welcome screen for an empty timeline."""

    def __init__(self, workspace: str | Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.workspace = workspace
        self._frame = 0
        self._animate = True

    def on_mount(self) -> None:
        self.refresh_logo()
        self.set_interval(1 / _SHIMMER_FPS, self._advance_frame)

    def on_resize(self) -> None:
        self.refresh_logo()

    def start_animation(self) -> None:
        self._animate = True
        self.refresh_logo()

    def stop_animation(self) -> None:
        self._animate = False

    def _advance_frame(self) -> None:
        if not self._animate:
            return
        self._frame += 1
        self.refresh_logo()

    def refresh_logo(self) -> None:
        compact = bool(self.size.width and self.size.width < 66)
        self.update(
            render_welcome_frame(
                self._frame,
                workspace=self.workspace,
                compact=compact,
            )
        )