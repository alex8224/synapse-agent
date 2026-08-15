"""Smoke tests for the Subagent Models TUI dialogs (headless Textual Pilot)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from synapse.settings import load_settings
from synapse.ui.dialogs import SubagentEditDialog, SubagentModelsDialog
from synapse.ui.dialogs.base import DialogBody
from synapse.ui.dialogs.subagent_config import _GLOBAL_KEY, _set_config_value
from synapse.ui.theme import bootstrap_theme


class _FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str):
        return SimpleNamespace(name=name, model=f"provider:{name}")


def _settings_with_config(tmp_path: Path):
    settings = load_settings()
    settings.subagent_default_model = None
    settings.subagent_default_reasoning_effort = None
    settings.subagent_model_overrides = {"tester": "algo:1"}
    settings.subagent_reasoning_effort_overrides = {"reviewer": "medium"}
    settings.workspace = tmp_path
    settings.custom_agents_dirs = []
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


def test_subagent_models_dialog_mounts_and_lists(tmp_path: Path) -> None:
    settings = _settings_with_config(tmp_path)
    dialog = SubagentModelsDialog(settings, registry=_FakeRegistry(["algo:1", "algo:2"]))

    async def exercise() -> None:
        app = _host_for(dialog)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            keys = body._option_keys
            assert _GLOBAL_KEY in keys
            assert "planner" in keys
            assert "researcher" in keys
            assert "reviewer" in keys
            assert "tester" in keys

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_subagent_models_dialog_shows_effective_summary(tmp_path: Path) -> None:
    settings = _settings_with_config(tmp_path)
    dialog = SubagentModelsDialog(settings, registry=_FakeRegistry(["algo:1"]))

    async def exercise() -> None:
        app = _host_for(dialog)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            row = next(r for r in body._rows if r.item.key == "tester")
            # tester has a model override but no reasoning override.
            assert "algo:1" in row.render().plain
            assert "inherit" in row.render().plain

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_subagent_edit_dialog_mounts_all_sections(tmp_path: Path) -> None:
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {"tester": "algo:1"},
        "reasoning_effort_overrides": {},
    }
    dialog = SubagentEditDialog(settings, "tester", config, _FakeRegistry(["algo:1", "algo:2"]))

    async def exercise() -> None:
        app = _host_for(dialog)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            keys = body._option_keys
            assert "model:inherit" in keys
            assert "model:algo:1" in keys
            assert "model:algo:2" in keys
            assert "reasoning:inherit" in keys
            for level in ("off", "minimal", "low", "medium", "high", "max"):
                assert f"reasoning:{level}" in keys

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_subagent_edit_dialog_reasoning_defaults_to_inherit(tmp_path: Path) -> None:
    """A role without an explicit reasoning override must show inherit, never
    ``"off"`` (which would silently disable thinking on save)."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    dialog = SubagentEditDialog(settings, "tester", config, _FakeRegistry(["algo:1"]))
    assert dialog._selected_model is None
    assert dialog._selected_reasoning is None


def test_subagent_edit_global_row_reads_existing_defaults(tmp_path: Path) -> None:
    """Editing the global row must show the existing defaults, not inherit."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": "global:m",
        "default_reasoning_effort": "high",
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    dialog = SubagentEditDialog(settings, _GLOBAL_KEY, config, _FakeRegistry(["global:m"]))
    assert dialog._selected_model == "global:m"
    assert dialog._selected_reasoning == "high"


def test_subagent_edit_shows_ad_hoc_model_not_in_registry(tmp_path: Path) -> None:
    """A model value that is not a registry alias (e.g. hand-edited settings)
    must still appear as a selectable, pre-selected option."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": "openai:gpt-4.1",
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    dialog = SubagentEditDialog(settings, _GLOBAL_KEY, config, _FakeRegistry(["algo:1"]))

    async def exercise() -> None:
        app = _host_for(dialog)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            assert "model:openai:gpt-4.1" in body._option_keys
            row = next(r for r in body._rows if r.item.key == "model:openai:gpt-4.1")
            assert row.item.selected

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_subagent_edit_normalizes_explicit_inherit(tmp_path: Path) -> None:
    """Raw ``"inherit"`` values are folded to None so the dialog shows a single
    selected inherit row (no duplicate model keys, no unselected reasoning)."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": "inherit",
        "default_reasoning_effort": "inherit",
        "model_overrides": {"tester": "inherit"},
        "reasoning_effort_overrides": {"tester": "inherit"},
    }
    dialog = SubagentEditDialog(settings, "tester", config, _FakeRegistry(["algo:1"]))
    assert dialog._selected_model is None
    assert dialog._selected_reasoning is None
    global_dialog = SubagentEditDialog(
        settings, _GLOBAL_KEY, config, _FakeRegistry(["algo:1"])
    )
    assert global_dialog._selected_model is None
    assert global_dialog._selected_reasoning is None

    async def exercise() -> None:
        app = _host_for(dialog)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            body = dialog.query_one("#dialog-body", DialogBody)
            assert body._option_keys.count("model:inherit") == 1
            model_inherit = next(
                r for r in body._rows if r.item.key == "model:inherit"
            )
            assert model_inherit.item.selected
            reasoning_inherit = next(
                r for r in body._rows if r.item.key == "reasoning:inherit"
            )
            assert reasoning_inherit.item.selected

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


def test_subagent_edit_global_untouched_model_keeps_default(tmp_path: Path) -> None:
    """Saving the global row without touching the model keeps the default."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": "global:m",
        "default_reasoning_effort": "high",
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    results: list[object] = []
    dialog = SubagentEditDialog(settings, _GLOBAL_KEY, config, _FakeRegistry(["global:m"]))

    async def exercise() -> None:
        app = _host_for(dialog, results.append)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Row order: model:inherit, model:global:m, reasoning:inherit,
            # reasoning:off, ... Select the same model then reasoning:inherit.
            await pilot.press("down", "enter")  # model:global:m
            await pilot.press("down", "down", "enter")  # reasoning:inherit
            await pilot.pause()

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    assert results == [("edited", config)]
    # Model default preserved; reasoning reset to inherit (no override).
    assert config["default_model"] == "global:m"
    assert config["default_reasoning_effort"] == "inherit"


def test_subagent_edit_only_model_change_keeps_reasoning_inherit(tmp_path: Path) -> None:
    """Selecting a model without touching reasoning writes the model override
    and records ``inherit`` (persisted as "no override"), never a fallback."""
    settings = _settings_with_config(tmp_path)
    config = {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    results: list[object] = []
    dialog = SubagentEditDialog(settings, "tester", config, _FakeRegistry(["algo:1", "algo:2"]))

    async def exercise() -> None:
        app = _host_for(dialog, results.append)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Row order: model:inherit, model:algo:1, model:algo:2,
            # reasoning:inherit, reasoning:off, ...
            await pilot.press("down", "enter")  # model:algo:1
            await pilot.press("down", "down", "enter")  # reasoning:inherit
            await pilot.pause()

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))
    assert results == [("edited", config)]
    assert config["model_overrides"] == {"tester": "algo:1"}
    assert config["reasoning_effort_overrides"] == {"tester": "inherit"}


def test_subagent_edit_global_routes_to_defaults(tmp_path: Path) -> None:
    config = {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }
    _set_config_value(config, "model", _GLOBAL_KEY, "algo:1")
    _set_config_value(config, "reasoning", _GLOBAL_KEY, "high")
    assert config["default_model"] == "algo:1"
    assert config["default_reasoning_effort"] == "high"
    assert config["model_overrides"] == {}
    assert config["reasoning_effort_overrides"] == {}
