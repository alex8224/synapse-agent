"""Animated welcome screen for the Synapse TUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

_BRAILLE_BLANK = "\u2800"
_SHIMMER_FPS = 12.0
_REVEAL_DURATION = 3.0
_VISIBLE_DURATION = 3.0
_ERASE_DURATION = 3.0
_HIDDEN_DURATION = 0.8
_CYCLE_SECONDS = (
    _REVEAL_DURATION + _VISIBLE_DURATION + _ERASE_DURATION + _HIDDEN_DURATION
)
_LEFT_COL_MASK = 0x4D  # braille left-column bits: 1+2+4+64
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


def _logo_phase(elapsed: float) -> tuple[str, float]:
    """Return the cycle phase and its elapsed time in seconds."""
    cycle_elapsed = elapsed % _CYCLE_SECONDS
    reveal_end = _REVEAL_DURATION
    visible_end = reveal_end + _VISIBLE_DURATION
    erase_end = visible_end + _ERASE_DURATION
    if cycle_elapsed < reveal_end:
        return "revealing", cycle_elapsed
    if cycle_elapsed < visible_end:
        return "visible", cycle_elapsed - reveal_end
    if cycle_elapsed < erase_end:
        return "erasing", cycle_elapsed - visible_end
    return "hidden", cycle_elapsed - erase_end


def _dot_char(full_value: int, fraction: float) -> str:
    """Build a Braille glyph with only a subset of dots active.

    ``fraction`` 0→1 reveals left-column first, then right-column.
    ``fraction >= 1`` returns the full glyph.
    """
    if full_value == 0:
        return _BRAILLE_BLANK
    if fraction >= 1.0:
        return chr(0x2800 + full_value)
    if fraction < 0.5:
        partial = full_value & _LEFT_COL_MASK
    else:
        partial = full_value
    return chr(0x2800 + partial) if partial else _BRAILLE_BLANK


def _dot_char_erase(full_value: int, fraction: float) -> str:
    """Build a Braille glyph erasing right-column then left-column.

    ``fraction`` 0→1 removes right-column first, then everything.
    """
    if full_value == 0:
        return _BRAILLE_BLANK
    if fraction >= 1.0:
        return _BRAILLE_BLANK
    if fraction < 0.5:
        partial = full_value & _LEFT_COL_MASK
    else:
        partial = 0
    return chr(0x2800 + partial) if partial else _BRAILLE_BLANK


def _logo_style(
    frame: int,
    column: int,
    row: int,
    width: int,
    full_value: int,
    *,
    muted: str,
    fg: str,
    total_rows: int = 7,
) -> tuple[str, str]:
    """Return ``(braille_char, style)`` for one animation frame."""
    elapsed = frame / _SHIMMER_FPS
    phase, phase_elapsed = _logo_phase(elapsed)

    if phase == "hidden":
        return _dot_char(full_value, 0), muted
    if phase == "revealing":
        reveal_column = (phase_elapsed / _REVEAL_DURATION) * width
        if column > reveal_column:
            return _dot_char(full_value, 0), muted
        # within-cell reveal: left column then right
        local = max(0.0, min(1.0, reveal_column - column + 0.35))
        return _dot_char(full_value, local), fg
    if phase == "erasing":
        column_norm = column / max(1, width - 1)
        position = row + column_norm * 0.4
        progress = (phase_elapsed / _ERASE_DURATION) * (total_rows + 0.4)
        local = max(0.0, min(1.0, (progress - position) / 0.6))
        return _dot_char_erase(full_value, local), fg
    return _dot_char(full_value, 1.0), fg


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
    green = str(getattr(theme, "green", "#81c995"))

    out = Text(justify="center")
    logo = _COMPACT_LOGO if compact else _LOGO
    logo_width = max(cell_len(line) for line in logo)
    logo_rows = len(logo)
    for row, line in enumerate(logo):
        left = max(0, (logo_width - cell_len(line)) // 2)
        for column, char in enumerate(line):
            if char in {" ", _BRAILLE_BLANK}:
                out.append(char, style=muted)
            else:
                anim_char, style = _logo_style(
                    frame,
                    left + column,
                    row,
                    logo_width,
                    ord(char) - 0x2800,
                    muted=muted,
                    fg=fg,
                    total_rows=logo_rows,
                )
                out.append(anim_char, style=style)
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