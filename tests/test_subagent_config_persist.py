"""Tests for TUI-managed subagent model config persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.runtime.subagent_config_persist import (
    load_subagent_config,
    save_subagent_config,
)
from synapse.settings import load_settings
from synapse.ui.dialogs.subagent_config import _GLOBAL_KEY, _config_value, _set_config_value


class _FakeSettings:
    """Minimal Settings-like object with the subagent fields."""

    def __init__(self) -> None:
        self.subagent_default_model: str | None = None
        self.subagent_default_reasoning_effort: str | None = None
        self.subagent_model_overrides: dict[str, str] = {}
        self.subagent_reasoning_effort_overrides: dict[str, str] = {}


def _empty_config() -> dict:
    return {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }


def test_load_subagent_config_normalizes() -> None:
    s = _FakeSettings()
    s.subagent_default_model = "global:m"
    s.subagent_model_overrides = {"tester": "algo:1"}
    assert load_subagent_config(s) == {
        "default_model": "global:m",
        "default_reasoning_effort": None,
        "model_overrides": {"tester": "algo:1"},
        "reasoning_effort_overrides": {},
    }


def test_load_subagent_config_folds_inherit_to_none() -> None:
    """Raw ``"inherit"`` from env / hand-edited files must not leak into the
    TUI as a concrete value."""
    s = _FakeSettings()
    s.subagent_default_model = "inherit"
    s.subagent_default_reasoning_effort = "inherit"
    s.subagent_model_overrides = {"tester": "inherit"}
    s.subagent_reasoning_effort_overrides = {"reviewer": "inherit"}
    assert load_subagent_config(s) == {
        "default_model": None,
        "default_reasoning_effort": None,
        "model_overrides": {},
        "reasoning_effort_overrides": {},
    }


def test_save_subagent_config_merges_and_preserves_other_keys(
    tmp_path: Path, monkeypatch
) -> None:
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text(json.dumps({"theme": "github-dark"}), encoding="utf-8")
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    config = _empty_config()
    config["model_overrides"] = {"tester": "algo:1"}
    config["reasoning_effort_overrides"] = {"reviewer": "medium"}

    path = save_subagent_config(s, config)
    assert path == settings_file
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["theme"] == "github-dark"
    assert data["subagent_model_overrides"] == {"tester": "algo:1"}
    assert data["subagent_reasoning_effort_overrides"] == {"reviewer": "medium"}
    # Settings object is synced in memory for the running process.
    assert s.subagent_model_overrides == {"tester": "algo:1"}


def test_save_subagent_config_removes_emptied_keys(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "theme": "x",
                "subagent_default_model": "old:m",
                "subagent_model_overrides": {"tester": "algo:1"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    save_subagent_config(s, _empty_config())
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data == {"theme": "x"}


def test_save_then_load_settings_roundtrip(tmp_path: Path, monkeypatch) -> None:
    """The layered loader must restore persisted keys on the next load."""
    import synapse.settings.config_paths as paths_mod
    from synapse.runtime import subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(json.dumps({"theme": "x"}), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "user_config_dir", lambda: cfg_dir)
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = load_settings()
    config = _empty_config()
    config["default_model"] = "global:m"
    config["default_reasoning_effort"] = "high"
    config["model_overrides"] = {"tester": "algo:1"}
    save_subagent_config(s, config)

    s2 = load_settings()
    assert s2.subagent_default_model == "global:m"
    assert s2.subagent_default_reasoning_effort == "high"
    assert s2.subagent_model_overrides == {"tester": "algo:1"}


def test_save_subagent_config_refuses_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    config = _empty_config()
    config["default_model"] = "global:m"
    with pytest.raises(ValueError, match="refusing to overwrite"):
        save_subagent_config(s, config)
    # The broken file must be left untouched.
    assert settings_file.read_text(encoding="utf-8") == "{ not json"


def test_save_subagent_config_refuses_non_object_root(tmp_path: Path, monkeypatch) -> None:
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    with pytest.raises(ValueError, match="JSON object"):
        save_subagent_config(s, _empty_config())
    assert settings_file.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_save_subagent_config_writes_atomically_without_tmp_leftover(
    tmp_path: Path, monkeypatch
) -> None:
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text(json.dumps({"theme": "x"}), encoding="utf-8")
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    config = _empty_config()
    config["default_model"] = "global:m"
    save_subagent_config(s, config)
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["subagent_default_model"] == "global:m"
    assert data["theme"] == "x"
    assert list(cfg_dir.glob("*.tmp")) == []


def test_save_subagent_config_normalizes_inherit_away(tmp_path: Path, monkeypatch) -> None:
    """"inherit" means "unset this layer": it must not be persisted anywhere."""
    import synapse.runtime.subagent_config_persist as persist_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    settings_file = cfg_dir / "settings.json"
    settings_file.write_text(json.dumps({"theme": "x"}), encoding="utf-8")
    monkeypatch.setattr(persist_mod, "user_config_dir", lambda: cfg_dir)

    s = _FakeSettings()
    config = _empty_config()
    config["default_model"] = "inherit"
    config["default_reasoning_effort"] = "inherit"
    config["model_overrides"] = {"tester": "inherit"}
    config["reasoning_effort_overrides"] = {"reviewer": "inherit"}
    save_subagent_config(s, config)
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data == {"theme": "x"}
    # In-memory sync mirrors the file: defaults None, overrides cleared.
    assert s.subagent_default_model is None
    assert s.subagent_default_reasoning_effort is None
    assert s.subagent_model_overrides == {}
    assert s.subagent_reasoning_effort_overrides == {}


def test_load_settings_rejects_invalid_reasoning_from_layered_json(
    tmp_path: Path, monkeypatch
) -> None:
    """Layered settings.json bypasses Pydantic validators via model_copy; the
    merge path must still reject invalid reasoning levels."""
    import synapse.settings.config_paths as paths_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"subagent_default_reasoning_effort": "turbo"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "user_config_dir", lambda: cfg_dir)

    with pytest.raises(ValueError, match="reasoning_effort"):
        load_settings()


def test_load_settings_rejects_invalid_reasoning_override_from_layered_json(
    tmp_path: Path, monkeypatch
) -> None:
    import synapse.settings.config_paths as paths_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"subagent_reasoning_effort_overrides": {"tester": "turbo"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "user_config_dir", lambda: cfg_dir)

    with pytest.raises(ValueError, match="invalid reasoning_effort"):
        load_settings()


def test_load_settings_rejects_malformed_reasoning_override_shape(
    tmp_path: Path, monkeypatch
) -> None:
    """A list value must raise a stable ValueError, not a TypeError."""
    import synapse.settings.config_paths as paths_mod

    cfg_dir = tmp_path / ".synapse"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"subagent_reasoning_effort_overrides": {"tester": ["high"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths_mod, "user_config_dir", lambda: cfg_dir)

    with pytest.raises(ValueError, match="invalid reasoning_effort"):
        load_settings()


# --------------------------------------------------------------------------- #
# config value routing helpers (dialog <-> config)
# --------------------------------------------------------------------------- #


def test_config_value_routing_global_and_per_name() -> None:
    config = _empty_config()
    assert _config_value(config, "model", "tester") is None

    _set_config_value(config, "model", "tester", "algo:1")
    _set_config_value(config, "reasoning", "tester", "high")
    _set_config_value(config, "model", _GLOBAL_KEY, "global:m")
    _set_config_value(config, "reasoning", _GLOBAL_KEY, "medium")

    assert config["model_overrides"] == {"tester": "algo:1"}
    assert config["reasoning_effort_overrides"] == {"tester": "high"}
    assert config["default_model"] == "global:m"
    assert config["default_reasoning_effort"] == "medium"

    # Per-name read falls back to the default.
    assert _config_value(config, "model", "reviewer") == "global:m"
    assert _config_value(config, "reasoning", "reviewer") == "medium"
    # Explicit override wins over the default.
    assert _config_value(config, "model", "tester") == "algo:1"


def test_config_value_clearing_removes_entries() -> None:
    config = _empty_config()
    _set_config_value(config, "model", "tester", "algo:1")
    _set_config_value(config, "model", "tester", None)
    _set_config_value(config, "model", _GLOBAL_KEY, "global:m")
    _set_config_value(config, "model", _GLOBAL_KEY, None)
    assert config["model_overrides"] == {}
    assert config["default_model"] is None
