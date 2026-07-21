"""Model + thinking picker dialog — invoked by /model."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import (
    DialogBase,
    OptionItem,
    SectionHeader,
)

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "max")


class ModelPickerDialog(DialogBase):
    """Pick a model profile + thinking level.

    dismiss result:
      ("model", alias)       → switch model, use default thinking
      ("thinking", level)    → change thinking only
    """

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self._settings = settings
        try:
            from synapse.models_registry import (
                registry_from_settings,
                settings_thinking_label,
            )

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

    @property
    def title_text(self) -> str:
        return "Select Model"

    def compose_body(self) -> ComposeResult:
        yield SectionHeader("")
        # Actual population happens in on_mount after body is queryable.

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
            items.append(
                OptionItem(
                    key=name,
                    label=name,
                    detail=detail,
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

    def _footer_buttons(self) -> ComposeResult:
        if False:
            yield

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        key = body.selected_key
        if not key:
            self.dismiss(None)
            return
        self._dismiss_with(key)

    def _on_selected(self, key: str | None) -> None:
        if key:
            self._dismiss_with(key)
        else:
            self.dismiss(None)

    def _dismiss_with(self, key: str) -> None:
        if key.startswith("thinking:"):
            self.dismiss(("thinking", key.split(":", 1)[1]))
        else:
            self.dismiss(("model", key))