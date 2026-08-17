"""Smoke tests for the model manager TUI dialogs (headless Textual Pilot)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from synapse.settings import load_settings
from synapse.ui.dialogs import (
    CodexConfigImportDialog,
    ModelFormDialog,
    ModelManagerDialog,
    ProviderCatalogDialog,
)
from synapse.ui.dialogs.base import DialogBody
from synapse.ui.theme import bootstrap_theme


def _settings(tmp_path: Path):
    settings = load_settings()
    settings.workspace = tmp_path
    settings.models_config_path = None
    return settings


def _host_for(screen, on_result=None):
    from textual.app import App

    bootstrap_theme("github-dark")

    class Host(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            from synapse.ui.theme import get_theme

            return {**super().get_css_variables(), **get_theme().css_variables()}

        def on_mount(self) -> None:
            self.push_screen(screen, on_result)

    return Host()


def test_model_manager_mounts_empty_store(tmp_path: Path) -> None:
    dialog = ModelManagerDialog(_settings(tmp_path))

    async def exercise() -> None:
        async with _host_for(dialog).run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            assert body._option_keys == []

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_model_form_create_mounts_fields(tmp_path: Path) -> None:
    dialog = ModelFormDialog(_settings(tmp_path))

    async def exercise() -> None:
        async with _host_for(dialog).run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            from textual.widgets import Input, Select

            assert dialog.query_one("#f-alias", Input).value == ""
            assert dialog.query_one("#f-provider", Select).value == "openai"
            assert dialog.query_one("#f-model", Input).value == ""
            # Screen CSS must constrain the window so it stays a centered modal.
            win = dialog.query_one("#form-window")
            assert win.styles.width and win.styles.width.value == 72
            assert dialog.query_one("#advanced").styles.display == "none"

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_model_form_save_returns_add_payload(tmp_path: Path) -> None:
    results: list = []
    dialog = ModelFormDialog(_settings(tmp_path))

    async def exercise() -> None:
        async with _host_for(dialog, results.append).run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            from textual.widgets import Input

            dialog.query_one("#f-alias", Input).value = "mini"
            dialog.query_one("#f-model", Input).value = "gpt-4.1"
            dialog.query_one("#f-key", Input).value = "sk-test"
            dialog.action_save()
            await pilot.pause()

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    assert results and results[0][0] == "add"
    _, alias, payload = results[0]
    assert alias == "mini"
    assert payload["model"] == "openai:gpt-4.1"
    assert payload["api_key"] == "sk-test"
    assert "provider" not in payload
    assert "wire_api" not in payload
    assert "api_key_env" not in payload


def test_model_form_edit_prefills_and_has_no_alias_field(tmp_path: Path) -> None:
    dialog = ModelFormDialog(
        _settings(tmp_path),
        alias="mini",
        initial={
            "model": "anthropic:claude-sonnet-4-5",
            "api_key_env": "ANTHROPIC_API_KEY",
            "base_url": "https://api.anthropic.com",
        },
    )

    async def exercise() -> None:
        async with _host_for(dialog).run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            from textual.widgets import Input, Select

            assert dialog.query_one("#f-provider", Select).value == "anthropic"
            assert dialog.query_one("#f-model", Input).value == "claude-sonnet-4-5"
            # Edit mode composes no alias input.
            assert not dialog.query("#f-alias")

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_provider_catalog_mounts(tmp_path: Path) -> None:
    dialog = ProviderCatalogDialog()

    async def exercise() -> None:
        async with _host_for(dialog).run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            assert "openai" in body._option_keys
            assert "anthropic" in body._option_keys

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_codex_import_mounts_without_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    dialog = CodexConfigImportDialog(_settings(tmp_path))

    async def exercise() -> None:
        async with _host_for(dialog).run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            assert body._rows, "dialog should show a diagnostic row"

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
