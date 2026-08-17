"""Provider catalog dialog — read-only list of supported model providers."""

from __future__ import annotations

from textual.app import ComposeResult

from synapse.models.providers import known_providers
from synapse.ui.dialogs.base import DialogBase, DialogBody, OptionItem


class ProviderCatalogDialog(DialogBase):
    """List providers that are usable out of the box."""

    _title_icon = "\u25c6"
    _title_keys = "esc close"

    def __init__(self, *, width: int = 92) -> None:
        super().__init__(width=width)

    @property
    def title_text(self) -> str:
        return "Supported Providers"

    def compose_body(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body", DialogBody)
        items: list[OptionItem] = []
        for spec in known_providers():
            meta = f"{spec.wire_api} · env={spec.default_env_key or '-'}"
            items.append(
                OptionItem(
                    key=spec.key,
                    label=f"{spec.key}  {spec.label}",
                    detail=spec.default_base_url,
                    meta=meta,
                    checkable=False,
                )
            )
        items.append(
            OptionItem(
                key="note",
                label="Unlisted providers",
                detail="Any other provider is used as an OpenAI-compatible endpoint",
                meta="openai: + base_url",
                checkable=False,
            )
        )
        body.set_options(items, mark="  ")

    def action_confirm(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)