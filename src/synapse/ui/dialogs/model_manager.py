"""Model manager dialog — list profiles and dispatch add/edit/delete/default.

Read-only itself: it collects the current store and returns an operation via
``dismiss`` for the controller to persist and hot-reload. Operations:

    ("open-form", alias, payload)  open the form (alias None => create)
    ("delete", alias, None)        remove one profile (deleting default repoints it)
    ("set-default", alias, None)   make a profile the default
    ("open-import", None, None)    open the Codex import dialog
    ("open-providers", None, None) open the provider catalog
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.ui.dialogs.base import DialogBase, DialogBody, OptionItem

_KEYS_HINT = "up/down move · a add · e edit · d del · s default · i import · p providers · esc"


class ModelManagerDialog(DialogBase):
    """List model profiles and dispatch CRUD operations."""

    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("a", "add_model", "Add", show=False),
        Binding("e", "edit_model", "Edit", show=False),
        Binding("d", "delete_model", "Delete", show=False),
        Binding("s", "set_default", "Default", show=False),
        Binding("i", "import_codex", "Codex import", show=False),
        Binding("p", "providers", "Providers", show=False),
    ]
    _title_keys = _KEYS_HINT
    _title_icon = "\u25c6"

    def __init__(self, settings: Any, *, width: int = 78) -> None:
        super().__init__(width=width)
        self._settings = settings
        self._pending_delete: str | None = None

    @property
    def title_text(self) -> str:
        return "Model Manager"

    def compose_body(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    def _load_rows(self) -> tuple[dict[str, Any], str]:
        from synapse.models.persist import load_models_store

        data = load_models_store(self._settings)
        models = data.get("models") or {}
        default = data.get("default") or next(iter(models), "")
        return models, str(default)

    def on_mount(self) -> None:
        super().on_mount()
        self._refresh()

    def _refresh(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        try:
            models, default = self._load_rows()
        except Exception as exc:  # noqa: BLE001
            body.set_options(
                [OptionItem(key="", label=f"cannot load models: {exc}", detail="")],
                mark="  ",
            )
            return
        items: list[OptionItem] = []
        for alias in sorted(models):
            profile = models[alias]
            model = str(profile.get("model") or "")
            provider = str(profile.get("provider") or "")
            base_url = str(profile.get("base_url") or "")
            meta = provider
            if base_url:
                meta = f"{provider}  {base_url}"
            items.append(
                OptionItem(
                    key=str(alias),
                    label=str(alias),
                    detail=model,
                    meta=meta,
                    selected=(str(alias) == default),
                    checkable=False,
                )
            )
        body.set_options(items, mark="  ")
        self._sync_hint()

    def _sync_hint(self) -> None:
        try:
            win = self.query_one("#dialog-window")
            if self._pending_delete:
                win.border_subtitle = f"press d again to delete {self._pending_delete}"
            else:
                win.border_subtitle = _KEYS_HINT
        except Exception:  # noqa: BLE001
            pass

    def _selected_alias(self) -> str | None:
        body = self.query_one("#dialog-body", DialogBody)
        key = body.selected_key
        return key if key else None

    def _row_clicked(self, key: str) -> None:
        # Row clicks only move the cursor here; they must not dismiss the
        # manager like DialogBase's default enter/click behavior.
        self._clear_pending_delete()

    def _clear_pending_delete(self) -> None:
        if self._pending_delete:
            self._pending_delete = None
            self._sync_hint()

    def action_add_model(self) -> None:
        self._clear_pending_delete()
        self.dismiss(("open-form", None, None))

    def action_edit_model(self) -> None:
        self._clear_pending_delete()
        alias = self._selected_alias()
        if alias is None:
            return
        data = self._load_rows()[0]
        if alias not in data:
            return
        self.dismiss(("open-form", alias, dict(data[alias])))

    def action_delete_model(self) -> None:
        alias = self._selected_alias()
        if alias is None:
            return
        if self._pending_delete == alias:
            self.dismiss(("delete", alias, None))
            return
        self._pending_delete = alias
        self._sync_hint()

    def action_set_default(self) -> None:
        self._clear_pending_delete()
        alias = self._selected_alias()
        if alias is None:
            return
        self.dismiss(("set-default", alias, None))

    def action_import_codex(self) -> None:
        self._clear_pending_delete()
        self.dismiss(("open-import", None, None))

    def action_providers(self) -> None:
        self._clear_pending_delete()
        self.dismiss(("open-providers", None, None))

    def action_confirm(self) -> None:
        # Enter on a profile row is inert here; Esc closes the dialog.
        self._clear_pending_delete()

    def action_cancel(self) -> None:
        self.dismiss(None)