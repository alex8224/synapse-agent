"""Model + thinking picker dialog — invoked by /model."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.ui.dialogs.base import (
    DialogBase,
    OptionItem,
)

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "max")


class ModelPickerDialog(DialogBase):
    """Pick a model profile + thinking level.

    Space marks a pending row (model and thinking sections are independent);
    Enter commits the marked combination (``confirm`` is overridden to save
    pending targets). No separate ``s`` key.

    dismiss result:
      ("model", alias)                 → switch model only
      ("thinking", level)              → change thinking only
      ("both", alias, level)           → switch model AND thinking together
      None                             → no change / cancelled
    """

    _title_icon = "◆"
    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("space", "toggle_selection", "Select", show=False, priority=True),
    ]

    _title_keys = "\u2191\u2193 move \u00b7 space select \u00b7 enter save \u00b7 esc"

    def __init__(self, settings: Any) -> None:
        super().__init__()
        # Deferred: synapse.models.registry pulls in langchain.chat_models
        # (~1.3s); the picker is only opened on demand via /model.
        from synapse.models.registry import registry_from_settings, settings_thinking_label

        self._settings = settings
        try:
            reg = registry_from_settings(settings)
            current_model = getattr(settings, "active_model", None) or getattr(
                reg, "default", None
            )
            current_think = settings_thinking_label(settings) or getattr(
                settings, "reasoning_effort", "high"
            )
            model_names = list(reg.list_names())
            allowed_think = list(reg.allowed_thinking_levels(current_model or ""))
            if not allowed_think:
                allowed_think = list(THINKING_LEVELS)
        except Exception:  # noqa: BLE001
            reg = None
            current_model = None
            current_think = "high"
            model_names = []
            allowed_think = list(THINKING_LEVELS)

        self._reg = reg
        self._current_model = current_model
        self._current_think = current_think
        self._model_names = model_names
        self._allowed_think = allowed_think
        self._model_count = len(model_names)
        # Independent pending targets: space marks one model and one thinking
        # level; Enter commits the marked combination. None = keep current value
        # (that section is not part of the commit).
        self._pending_model: str | None = None
        self._pending_think: str | None = None

    @property
    def title_text(self) -> str:
        return "Select Model"

    def compose_body(self) -> ComposeResult:
        # Population happens in on_mount after body is queryable.
        return
        yield  # pragma: no cover

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        reg = self._reg
        current = self._current_model
        items: list[OptionItem] = []
        # --- Model section ---
        for name in self._model_names:
            detail = ""
            if reg is not None:
                try:
                    p = reg.get(name)
                    detail = str(p.model or "")
                except Exception:  # noqa: BLE001
                    pass
            # Keep one line: alias as label, provider model as trailing meta.
            items.append(
                OptionItem(
                    key=name,
                    label=name,
                    meta=detail,
                    selected=(name == current),
                )
            )
        self._model_count = len(items)

        # --- Thinking section ---
        current_think = self._current_think or "high"
        for level in self._allowed_think:
            items.append(
                OptionItem(
                    key=f"thinking:{level}",
                    label=level,
                    selected=(level == current_think),
                )
            )
        body.set_options(items[: self._model_count], mark="  ")

        # Mount thinking section header + items manually.
        think_items = items[self._model_count :]
        if think_items:
            body.append_section("Thinking")
            body.append_options(think_items, mark="  ")

    def action_toggle_selection(self) -> None:
        """Space: toggle the highlighted row as a pending save target.

        Model and thinking sections are independent: space marks/unmarks one
        row inside the section the cursor is on. The dialog stays open; Enter
        commits the marked combination.
        """
        body = self.query_one("#dialog-body")
        key = body.selected_key
        if not key:
            return
        if key.startswith("thinking:"):
            if key == self._pending_think:
                self._pending_think = None
            else:
                self._pending_think = key
        else:
            if key == self._pending_model:
                self._pending_model = None
            else:
                self._pending_model = key
        self._sync_markers()

    def _sync_markers(self) -> None:
        """Reflect the ● markers per section: pending target or current value.

        Each section always shows one marker: the space-pinned pending row if
        the user marked one, otherwise the current value. This keeps the two
        sections independent and never hides the current configuration.
        """
        body = self.query_one("#dialog-body")
        keys: set[str] = set()
        if self._pending_model:
            keys.add(self._pending_model)
        elif self._current_model:
            keys.add(self._current_model)
        if self._pending_think:
            keys.add(self._pending_think)
        elif self._current_think or "high":
            keys.add(f"thinking:{self._current_think or 'high'}")
        body.set_selected_marker(keys or None)

    def _row_clicked(self, key: str) -> None:
        """Mouse click: toggle the row's pending state (like space).

        The click already moved the cursor; the dialog stays open, only Enter
        commits. Overrides DialogBase._row_clicked (no instant dismiss)."""
        self.action_toggle_selection()

    def _on_apply(self) -> None:
        if self._pending_model and self._pending_think:
            self.dismiss(
                (
                    "both",
                    self._pending_model,
                    self._pending_think.removeprefix("thinking:"),
                )
            )
        elif self._pending_model:
            self.dismiss(("model", self._pending_model))
        elif self._pending_think:
            self.dismiss(("thinking", self._pending_think.removeprefix("thinking:")))
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        """Enter: commit the marked combination (model + thinking)."""
        self._on_apply()