"""Textual widgets for the transcript turn-rail minimap."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.events import Click, Enter, Leave
from textual.widgets import Static

from synapse.ui.turn_rail import format_turn_rail_bucket_label, turn_rail_tick_slots
from synapse.ui.user_turn_block import UserTurnBlock

_RAIL_BAR = "───"
_RAIL_BAR_DENSE = "━━━"
_RAIL_BAR_HEAVY = "▓▓▓"
_DEFAULT_FG = "#e8eaed"
_DEFAULT_DIM = "#9aa0a6"
_DEFAULT_MUTED = "#5f6368"
_DEFAULT_BAR = "#2b2d31"


def _theme_color(attribute: str, fallback: str) -> str:
    try:
        from synapse.ui.theme import get_theme

        return str(getattr(get_theme(), attribute, fallback))
    except Exception:  # noqa: BLE001
        return fallback


class TurnRailGap(Static):
    """Empty minimap row between proportional ticks."""

    DEFAULT_CSS = """
    TurnRailGap {
        height: 1;
        width: 1fr;
        padding: 0;
        margin: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__("")


class TurnRailItem(Static):
    """One minimap slot for a user turn or a bucket of turns."""

    def __init__(
        self,
        indices: list[int],
        previews: list[str],
        targets: list[UserTurnBlock],
    ) -> None:
        super().__init__()
        self.indices = [int(index) for index in indices]
        self.previews = list(previews)
        self.targets = list(targets)
        self._cycle = 0
        if len(self.indices) > 1:
            self.add_class("-dense")
        self._show_bar()

    def set_data(
        self,
        indices: list[int],
        previews: list[str],
        targets: list[UserTurnBlock],
    ) -> None:
        """Reuse this fixed rail slot for another turn bucket."""
        self.indices = [int(index) for index in indices]
        self.previews = list(previews)
        self.targets = list(targets)
        self._cycle = 0
        self.set_class(len(self.indices) > 1, "-dense")
        self._show_bar()

    def _bar_glyph(self) -> str:
        count = len(self.indices)
        if count == 0:
            return ""
        if count <= 1:
            return _RAIL_BAR
        if count <= 3:
            return _RAIL_BAR_DENSE
        return _RAIL_BAR_HEAVY

    def _show_bar(self) -> None:
        self.remove_class("-hover")
        attribute = "dim" if len(self.indices) > 1 else "muted"
        fallback = _DEFAULT_DIM if len(self.indices) > 1 else _DEFAULT_MUTED
        self.update(
            Text(self._bar_glyph(), style=_theme_color(attribute, fallback), justify="right")
        )

    def _show_preview(self) -> None:
        self.add_class("-hover")
        label = format_turn_rail_bucket_label(self.indices, self.previews)
        fg = _theme_color("fg", _DEFAULT_FG)
        bar = _theme_color("bar", _DEFAULT_BAR)
        self.update(Text(label, style=f"{fg} on {bar}", justify="right"))

    def on_enter(self, event: Enter) -> None:
        event.stop()
        self._show_preview()

    def on_leave(self, event: Leave) -> None:
        event.stop()
        self._show_bar()

    def on_click(self, event: Click) -> None:
        event.stop()
        if not self.targets:
            return
        index = self._cycle % len(self.targets)
        self._cycle = (self._cycle + 1) % len(self.targets)
        jump = getattr(self.app, "jump_to_user_turn", None)
        if callable(jump):
            jump(self.targets[index])


class TurnRail(Vertical):
    """Right-side minimap mapping all user turns to a fixed viewport height."""

    RAIL_WIDTH = 34

    DEFAULT_CSS = """
    TurnRail {
        dock: right;
        layer: overlay;
        width: 34;
        min-width: 34;
        max-width: 34;
        height: 1fr;
        max-height: 1fr;
        align: right top;
        padding: 1 1;
        margin: 0 0;
        background: transparent;
        overflow-x: hidden;
        overflow-y: hidden;
        scrollbar-size: 0 0;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._turns: list[tuple[str, UserTurnBlock]] = []
        self._slots: list[TurnRailItem] = []

    def clear_turns(self) -> None:
        self._turns = []
        for slot in self._slots:
            slot.set_data([], [], [])

    def set_turns(self, turns: list[tuple[str, UserTurnBlock]]) -> None:
        self._turns = list(turns or [])
        self.relayout()

    def on_mount(self) -> None:
        self.relayout()

    def on_resize(self, event: object) -> None:  # noqa: ANN001
        del event
        self.relayout()

    def _content_height(self) -> int:
        height = int(getattr(self.size, "height", 0) or 0) - 2
        if height >= 1:
            return height
        count = len(self._turns)
        return max(1, min(count if count else 1, 32))

    def relayout(self) -> None:
        if not self.is_attached:
            return
        turns = self._turns
        slots = turn_rail_tick_slots(len(turns), self._content_height())
        while len(self._slots) < len(slots):
            slot = TurnRailItem([], [], [])
            self._slots.append(slot)
            self.mount(slot)
        for index, slot in enumerate(self._slots):
            active = index < len(slots)
            slot.display = active
            if not active:
                slot.set_data([], [], [])
                continue
            indices = slots[index]
            previews = [turns[index][0] for index in indices if 0 <= index < len(turns)]
            targets = [turns[index][1] for index in indices if 0 <= index < len(turns)]
            slot.set_data(indices, previews, targets)


__all__ = ["TurnRail", "TurnRailGap", "TurnRailItem"]
