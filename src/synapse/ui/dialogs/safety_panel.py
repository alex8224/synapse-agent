"""Safety profile picker — invoked by /safety."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader

PROFILES = {
    "dev-autopass": "All tool calls pass automatically",
    "dev-approve": "Each tool call requires confirmation",
    "readonly": "Read-only mode, all writes blocked",
}


class SafetyPanelDialog(DialogBase):
    """Pick a safety profile."""

    _title_icon = "◇"

    def __init__(self, settings: Any) -> None:
        super().__init__()
        self._settings = settings
        self._current = getattr(settings, "safety_profile", "dev-autopass")

    @property
    def title_text(self) -> str:
        return "Safety Profile"

    def compose_body(self) -> ComposeResult:
        yield SectionHeader("Profile")
        items: list[OptionItem] = []
        for key, desc in PROFILES.items():
            items.append(
                OptionItem(
                    key=key,
                    label=key,
                    detail=desc,
                    selected=(key == self._current),
                )
            )
        self._items = items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="  ")

    def _on_apply(self) -> None:
        body = self.query_one("#dialog-body")
        key = body.selected_key
        if key:
            self.dismiss(("safety", key))
        else:
            self.dismiss(None)

    def _on_selected(self, key: str | None) -> None:
        if key:
            self._on_apply()
        else:
            self.dismiss(None)