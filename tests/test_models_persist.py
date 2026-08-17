"""Tests for models.json CRUD persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.models.persist import (
    ModelsStoreError,
    add_profile,
    delete_profile,
    load_models_store,
    models_store_path,
    set_default,
    update_profile,
    validate_models_data,
)


class _FakeSettings:
    """Settings-like object pointing CRUD at an isolated workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.models_config_path: str | None = None


def _payload(model: str = "openai:gpt-4.1-mini", **extra: object) -> dict:
    data: dict[str, object] = {"model": model, "provider": "openai", **extra}
    return data


def test_models_store_path_uses_project_layer(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    assert models_store_path(s) == (tmp_path / ".synapse" / "models.json").resolve()


def test_add_profile_creates_store_and_sets_default(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    path = add_profile(s, "mini", _payload())
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["default"] == "mini"
    assert data["models"]["mini"]["model"] == "openai:gpt-4.1-mini"
    # settings path is refreshed so a live reload sees the new file
    assert str(s.models_config_path) == str(path)


def test_add_profile_filters_unknown_fields(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    payload = _payload(
        headers={"X-T": "1"},
        bogus="drop-me",
        empty="",
        provider="openai",
        wire_api="chat",
    )
    add_profile(s, "mini", payload)
    data = load_models_store(s)
    assert data["models"]["mini"]["headers"] == {"X-T": "1"}
    assert "bogus" not in data["models"]["mini"]
    assert "empty" not in data["models"]["mini"]
    assert "provider" not in data["models"]["mini"]
    assert "wire_api" not in data["models"]["mini"]


def test_add_profile_rejects_duplicate_alias(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "mini", _payload())
    with pytest.raises(ModelsStoreError):
        add_profile(s, "mini", _payload(model="openai:other"))


def test_add_profile_requires_model(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    with pytest.raises(ModelsStoreError):
        add_profile(s, "bad", {"provider": "openai"})


def test_update_profile_merges_and_deletes_keys(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "mini", _payload(base_url="https://a.example/v1"))
    update_profile(s, "mini", {"base_url": None, "reasoning_effort": "high"})
    data = load_models_store(s)
    assert "base_url" not in data["models"]["mini"]
    assert data["models"]["mini"]["reasoning_effort"] == "high"


def test_update_profile_refuses_required_key_removal(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "mini", _payload())
    with pytest.raises(ModelsStoreError):
        update_profile(s, "mini", {"model": None})


def test_delete_profile_repoints_default(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "a", _payload(model="openai:a"))
    add_profile(s, "b", _payload(model="openai:b"))
    set_default(s, "a")
    delete_profile(s, "a")
    data = load_models_store(s)
    assert data["default"] == "b"
    assert "a" not in data["models"]


def test_delete_last_profile_refused(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "only", _payload())
    with pytest.raises(ModelsStoreError):
        delete_profile(s, "only")


def test_set_default_requires_existing_alias(tmp_path: Path) -> None:
    s = _FakeSettings(tmp_path)
    add_profile(s, "mini", _payload())
    with pytest.raises(ModelsStoreError):
        set_default(s, "nope")


def test_validate_models_data_rejects_empty_or_bad_default() -> None:
    with pytest.raises(ModelsStoreError):
        validate_models_data({"models": {}})
    with pytest.raises(ModelsStoreError):
        validate_models_data(
            {
                "models": {"a": {"model": "openai:a"}},
                "default": "missing",
            }
        )
