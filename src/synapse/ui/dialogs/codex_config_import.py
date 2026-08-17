"""Codex config import dialog — detect, preview, and confirm the import plan.

Runs the detection/planning synchronously on open (files are small and local),
shows one row per planned profile, and lets the user toggle add/skip (Space)
or replace (C for an existing alias). ``y`` executes and dismisses with
``("imported", plan)`` so the controller persists, hot-reloads and reopens the
manager.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.integrations.codex_config import (
    ImportPlan,
    ImportPlanItem,
    build_import_plan,
    scan_codex_config,
)
from synapse.ui.dialogs.base import DialogBase, DialogBody, OptionItem

_HINT = "up/down move · space toggle · c replace · y execute · esc cancel"


class CodexConfigImportDialog(DialogBase):
    """Preview the Codex import plan and confirm execution."""

    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("y", "execute", "Execute", show=False),
        Binding("c", "toggle_replace", "Replace", show=False),
        Binding("space", "toggle_skip", "Toggle", show=False, priority=True),
    ]
    _title_icon = "\u25c6"
    _title_keys = _HINT

    def __init__(self, settings: Any, *, width: int = 96) -> None:
        super().__init__(width=width)
        self._settings = settings
        self._plan: ImportPlan | None = None
        self._scan_error: str | None = None

    @property
    def title_text(self) -> str:
        return "Import Codex Config"

    def compose_body(self) -> ComposeResult:
        return
        yield  # pragma: no cover

    def _build_plan(self) -> None:
        workspace = getattr(self._settings, "workspace", None)
        from synapse.models.persist import load_models_store, models_store_path

        target = load_models_store(self._settings)
        try:
            self._plan = build_import_plan(
                workspace,
                target,
                target_path=models_store_path(self._settings),
            )
        except ValueError as exc:
            self._scan_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            self._scan_error = f"planning failed: {exc}"

    def on_mount(self) -> None:
        super().on_mount()
        self._build_plan()
        self._refresh()

    def _refresh(self) -> None:
        body = self.query_one("#dialog-body", DialogBody)
        if self._scan_error:
            body.set_options(
                [OptionItem(key="", label=self._scan_error, detail="", checkable=False)],
                mark="  ",
            )
            self._sync_hint("esc close")
            return

        plan = self._plan
        if plan is None:
            body.set_options(
                [OptionItem(key="", label="no plan", detail="", checkable=False)],
                mark="  ",
            )
            self._sync_hint("esc close")
            return

        scan = scan_codex_config(getattr(self._settings, "workspace", None))
        items: list[OptionItem] = []
        for item in plan.items:
            if item.action == "replace":
                action = "replace"
            elif item.action == "skip":
                action = "skip"
            else:
                action = "add"
            meta = f"{action} ({plan.target_path.name})"
            if item.conflict and item.action == "skip":
                meta = "skip (conflict) · c to replace"
            items.append(
                OptionItem(
                    key=item.alias,
                    label=str(item.alias),
                    detail=f"{item.model}  <- {item.source}",
                    meta=meta,
                    checked=item.action != "skip",
                )
            )
        header = f"detected: {scan.user_config.describe()} | {scan.project_config.describe()}"
        body.set_options(
            [OptionItem(key="@header", label=header, detail="", checkable=False)],
            mark="  ",
        )
        body.append_options(items, mark="  ")
        body.append_options(
            [
                OptionItem(
                    key="@auth",
                    label=_auth_line(scan.auth_status),
                    detail="",
                    checkable=False,
                )
            ],
            mark="  ",
        )
        warnings = plan.warnings[:4]
        for warning in warnings:
            body.append_options(
                [OptionItem(key="@warn", label=f"warn: {warning}", detail="", checkable=False)],
                mark="  ",
            )
        if len(plan.warnings) > 4:
            body.append_options(
                [
                    OptionItem(
                        key="@warn",
                        label=f"... and {len(plan.warnings) - 4} more warnings",
                        detail="",
                        checkable=False,
                    )
                ],
                mark="  ",
            )
        self._sync_hint(_HINT)

    def _sync_hint(self, hint: str | None = None) -> None:
        try:
            self.query_one("#dialog-window").border_subtitle = hint or _HINT
        except Exception:  # noqa: BLE001
            pass

    def _selected_item(self) -> ImportPlanItem | None:
        body = self.query_one("#dialog-body", DialogBody)
        key = body.selected_key
        if not key or key.startswith("@"):
            return None
        for item in self._plan.items if self._plan else []:
            if item.alias == key:
                return item
        return None

    def _row_clicked(self, key: str) -> None:
        # Clicks move the cursor; execution stays on the y key.
        return

    def action_toggle_skip(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        item.action = "add" if item.action == "skip" else "skip"
        self._refresh()

    def action_toggle_replace(self) -> None:
        item = self._selected_item()
        if item is None or not item.conflict:
            return
        order = {"add": "replace", "replace": "skip", "skip": "add"}
        item.action = order.get(item.action, "replace")
        self._refresh()

    def action_execute(self) -> None:
        if self._plan is None:
            return
        self.dismiss(("imported", self._plan))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _auth_line(status: str) -> str:
    if status == "oauth":
        return "auth.json: OAuth grant available (use auth=openai_oauth for it)"
    if status == "api-key":
        return "auth.json: plaintext API key present - not copied; reference it via api_key_env"
    if status == "unreadable":
        return "auth.json: unreadable"
    return "auth.json: none"