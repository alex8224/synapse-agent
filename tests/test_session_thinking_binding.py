"""Session-scoped model/reasoning binding: resolution, materialization, policy.

The reasoning level is a *session* axis, exactly like the model.  These tests pin
the rules that keep it one, i.e. the ones whose absence made a session's level
move on its own:

* ``apply_binding_to_settings`` applies a model profile for identity only, so
  opening a session cannot replace the level in force with the profile default;
* ``save_model_binding(create=False)`` materializes a resolution without ever
  creating a row for a session the user never used;
* ``resolve_session_axes`` resets the axes from a pristine baseline before it
  layers the target's binding on, so a switch cannot inherit the previous
  session's level;
* ``reasoning_level_survives_switch`` decides when a model switch may re-seed the
  level from the new model's profile at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from synapse.commands.model import handle_model
from synapse.models.helpers import reasoning_level_survives_switch
from synapse.sessions import (
    ModelBinding,
    SessionStore,
    apply_binding_to_settings,
    resolve_session_axes,
    snapshot_session_axes,
)
from synapse.settings import load_settings
from synapse.settings.config_paths import set_project_reasoning_effort

#: Profile "a" declares high, profile "b" medium but refuses "max": the pair
#: covers both branches of the model-switch policy (keep vs re-seed).
_MODELS = {
    "models": {
        "a": {"model": "openai:demo-a", "thinking": "high"},
        "b": {
            "model": "openai:demo-b",
            "thinking": "medium",
            "thinking_levels": ["off", "low", "medium", "high"],
        },
    }
}


def _models_config(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_MODELS), encoding="utf-8")
    return path


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's own ``~/.synapse`` layers out of the test."""
    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: tmp_path / "nouser" / ".synapse",
    )
    monkeypatch.setattr("synapse.settings.config_paths.executable_config_dirs", lambda: [])


def _settings(
    tmp_path: Path,
    models: Path,
    *,
    active_model: str = "a",
    effort: str = "low",
) -> Any:
    """Settings for a session that is on ``effort`` while its profile says high.

    The level is assigned *after* the load on purpose: loading re-seeds
    ``reasoning_effort`` from the selected profile, so only an explicit
    assignment models "the level this session is actually on".
    """
    settings = load_settings(
        workspace=tmp_path,
        models_config_path=models,
        checkpoint_backend="memory",
        active_model=active_model,
    )
    settings.enable_thinking = True
    settings.reasoning_effort = effort
    return settings


# ---------------------------------------------------------------------------
# Identity, not level: applying a binding must not re-seed the profile default
# ---------------------------------------------------------------------------


def test_applying_a_binding_keeps_the_level_in_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="low")

    changed = apply_binding_to_settings(
        settings,
        ModelBinding(active_model="b", model="openai:demo-b", thinking=None),
    )

    assert changed is True
    assert settings.active_model == "b"
    assert settings.model == "openai:demo-b"
    # Profile "b" declares "medium"; the session's own level is not the profile's
    # to replace, so the level in force survives the identity change.
    assert settings.reasoning_effort == "low"


def test_a_stored_level_wins_over_the_profile_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="low")

    apply_binding_to_settings(
        settings,
        ModelBinding(active_model="a", model="openai:demo-a", thinking="max"),
    )

    assert settings.reasoning_effort == "max"


# ---------------------------------------------------------------------------
# Resolution: baseline first, then the project layer, then the binding
# ---------------------------------------------------------------------------


def test_resolve_resets_the_axes_so_the_previous_session_cannot_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported symptom: switching sessions moved the level on its own."""
    _isolate_config(monkeypatch, tmp_path)
    models = _models_config(tmp_path)
    baseline = snapshot_session_axes(_settings(tmp_path, models, effort="low"))
    # The live object as the previous session left it.
    live = _settings(tmp_path, models, active_model="b", effort="max")

    changed = resolve_session_axes(
        live,
        binding=ModelBinding(active_model="a", model="openai:demo-a", thinking=None),
        baseline=baseline,
        workspace=tmp_path,
    )

    assert changed is True
    assert live.active_model == "a"
    assert live.model == "openai:demo-a"
    # Not "max" (the previous session) and not "high" (profile "a"): the project
    # baseline is what a session with no stored level of its own resolves to.
    assert live.reasoning_effort == "low"


def test_resolve_keeps_the_sessions_own_level_over_the_project_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    models = _models_config(tmp_path)
    set_project_reasoning_effort("low", workspace=tmp_path)
    baseline = snapshot_session_axes(_settings(tmp_path, models, effort="high"))
    live = _settings(tmp_path, models, effort="high")

    resolve_session_axes(
        live,
        binding=ModelBinding(active_model="a", model="openai:demo-a", thinking="max"),
        baseline=baseline,
        workspace=tmp_path,
    )

    assert live.reasoning_effort == "max"


def test_resolve_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second resolution of the same session reports no change to rebuild for."""
    _isolate_config(monkeypatch, tmp_path)
    models = _models_config(tmp_path)
    baseline = snapshot_session_axes(_settings(tmp_path, models, effort="low"))
    binding = ModelBinding(active_model="a", model="openai:demo-a", thinking="max")
    live = _settings(tmp_path, models, effort="low")

    assert resolve_session_axes(live, binding=binding, baseline=baseline, workspace=tmp_path)
    assert not resolve_session_axes(live, binding=binding, baseline=baseline, workspace=tmp_path)


# ---------------------------------------------------------------------------
# Model switch: the level is kept unless the new model refuses it
# ---------------------------------------------------------------------------


def test_level_survives_a_switch_the_new_model_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="low")

    assert reasoning_level_survives_switch(settings, ["off", "low", "medium", "high"])


def test_level_is_reseeded_when_the_new_model_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="max")

    assert not reasoning_level_survives_switch(settings, ["off", "low", "medium", "high"])


def test_thinking_off_survives_every_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling thinking is a choice; a profile must not switch it back on."""
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="low")
    settings.enable_thinking = False

    assert reasoning_level_survives_switch(settings, ["off", "low"])


def test_model_switch_command_keeps_the_session_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/model <alias>`` is also how unrelated rebuilds run: it must not reseed.

    Saving the subagent configuration rebuilds the graph through
    ``/model <same-alias>``; before the policy existed that silently reset the
    session's level to the model profile's default and persisted it.
    """
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="low")
    persisted: list[Any] = []

    result = handle_model(
        ["b"],
        settings=settings,
        agent=object(),
        project_root=tmp_path,
        thread_id="t1",
        apply_thinking_inplace=lambda *args: False,
        rebuild_agent=lambda *args, **kwargs: "AGENT",
        persist_model_binding=lambda current, thread_id: persisted.append((current, thread_id)),
        mcp_attach_pending=lambda current: False,
    )

    assert result.error is False
    assert settings.active_model == "b"
    assert settings.reasoning_effort == "low"
    assert persisted and persisted[0][1] == "t1"


def test_model_switch_command_reseeds_a_level_the_new_model_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    settings = _settings(tmp_path, _models_config(tmp_path), effort="max")

    handle_model(
        ["b"],
        settings=settings,
        agent=object(),
        project_root=tmp_path,
        thread_id="t1",
        apply_thinking_inplace=lambda *args: False,
        rebuild_agent=lambda *args, **kwargs: "AGENT",
        persist_model_binding=lambda current, thread_id: None,
        mcp_attach_pending=lambda current: False,
    )

    assert settings.reasoning_effort == "medium"


# ---------------------------------------------------------------------------
# Materialization: record the resolved axes, never create a session
# ---------------------------------------------------------------------------


def test_materializing_never_creates_a_session(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        wrote = store.save_model_binding(
            "never-used",
            ModelBinding(active_model="a", model="openai:demo-a", thinking="low"),
            also_last=False,
            create=False,
        )

        assert wrote is False
        assert store.get("never-used") is None
        # ``also_last`` is the global last-used preference; nothing happened here,
        # so it must stay empty too.
        assert store.get_last_model_binding().has_data() is False


def test_materializing_fills_the_level_of_an_existing_row(tmp_path: Path) -> None:
    with SessionStore(tmp_path / "sessions.sqlite") as store:
        store.ensure("t1", model="openai:demo-a")
        assert store.get("t1").thinking is None

        wrote = store.save_model_binding(
            "t1",
            ModelBinding(active_model="a", model="openai:demo-a", thinking="low"),
            also_last=False,
            create=False,
        )

        assert wrote is True
        binding = store.get_model_binding("t1")
        assert (binding.active_model, binding.model, binding.thinking) == (
            "a",
            "openai:demo-a",
            "low",
        )


# ---------------------------------------------------------------------------
# The TUI switch path: resolve, rebuild, materialize
# ---------------------------------------------------------------------------


def test_session_switch_resolves_from_the_baseline_and_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synapse.commands import slash_cmds

    _isolate_config(monkeypatch, tmp_path)
    models = _models_config(tmp_path)
    baseline = snapshot_session_axes(_settings(tmp_path, models, effort="low"))
    live = _settings(tmp_path, models, active_model="b", effort="max")

    with SessionStore(live.resolved_sessions_path()) as store:
        store.ensure("target", model="openai:demo-a", active_model="a")

    monkeypatch.setattr(slash_cmds, "_rebuild_agent", lambda *args, **kwargs: "AGENT")
    agent, notes = slash_cmds._restore_thread_model(
        settings=live,
        agent=object(),
        project_root=tmp_path,
        thread_id="target",
        baseline=baseline,
    )

    assert agent == "AGENT"
    assert live.active_model == "a"
    assert live.reasoning_effort == "low"
    assert notes == ["restored model: a · low"]
    with SessionStore(live.resolved_sessions_path()) as store:
        # The row now describes what the session runs with, so the next switch
        # resolves to the same answer instead of a fresh guess at the defaults.
        assert store.get_model_binding("target").thinking == "low"
