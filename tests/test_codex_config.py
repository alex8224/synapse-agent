"""Tests for Codex config detection, mapping, and import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synapse.integrations import codex_config as cc


class _FakeSettings:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.models_config_path: str | None = None


def _write_codex_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    codex = home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return codex


def _write_config(codex: Path, text: str) -> None:
    (codex / "config.toml").write_text(text, encoding="utf-8")


def test_scan_detects_config_and_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _write_codex_home(tmp_path / "home", monkeypatch)
    _write_config(codex, 'model = "gpt-5.2-codex"\nmodel_provider = "openai"\n')
    (codex / "auth.json").write_text(json.dumps({"tokens": {"x": 1}}), encoding="utf-8")

    scan = cc.scan_codex_config()
    assert scan.any_config is True
    assert scan.auth_status == "oauth"
    assert scan.any_error is False


def test_scan_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_codex_home(tmp_path / "home", monkeypatch)
    scan = cc.scan_codex_config()
    assert scan.any_config is False
    assert scan.auth_status == "missing"


def test_plan_maps_builtin_custom_and_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _write_codex_home(tmp_path / "home", monkeypatch)
    monkeypatch.setenv("MY_PROXY_KEY", "sk-xyz")
    _write_config(
        codex,
        'model = "gpt-5.2-codex"\n'
        'model_provider = "openai"\n'
        'model_reasoning_effort = "high"\n'
        "\n"
        '[model_providers.openai]\n'
        'name = "OpenAI"\n'
        'base_url = "https://api.openai.com/v1"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
        "\n"
        '[model_providers.myproxy]\n'
        'base_url = "https://myproxy.example.com/v1"\n'
        'env_key = ["MY_PROXY_KEY", "ALT_KEY"]\n'
        'wire_api = "chat"\n'
        'http_headers = { "X-Tenant" = "t1" }\n'
        'query_params = { "api-version" = "2024-06-01" }\n'
        "\n"
        '[profiles.lite]\n'
        'model = "gpt-4.1-mini"\n'
        'model_provider = "myproxy"\n',
    )

    plan = cc.build_import_plan(workspace=tmp_path, target={"models": {}})
    assert len(plan.items) == 2

    default_item = plan.items[0]
    assert default_item.alias == "openai:gpt-5.2-codex"
    assert default_item.payload["model"] == "openai:gpt-5.2-codex"
    # responses is OAuth-only -> downgraded to chat (no wire_api field stored)
    assert "wire_api" not in default_item.payload
    assert default_item.payload["api_key_env"] == "OPENAI_API_KEY"
    assert default_item.payload["reasoning_effort"] == "high"
    assert any("OAuth-only" in w for w in plan.warnings)

    lite = plan.items[1]
    assert lite.alias == "lite"
    assert lite.payload["model"] == "openai:gpt-4.1-mini"
    assert lite.payload["api_key_env"] == "MY_PROXY_KEY"
    assert lite.payload["base_url"] == "https://myproxy.example.com/v1"
    assert lite.payload["headers"] == {"X-Tenant": "t1"}
    assert lite.payload["model_kwargs"] == {
        "default_query": {"api-version": "2024-06-01"}
    }


def test_plan_marks_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex = _write_codex_home(tmp_path / "home", monkeypatch)
    _write_config(codex, 'model = "gpt-5.2-codex"\nmodel_provider = "openai"\n')
    target = {"models": {"openai:gpt-5.2-codex": {"model": "openai:gpt-5.2-codex"}}}
    plan = cc.build_import_plan(workspace=tmp_path, target=target)
    assert plan.items[0].conflict is True


def test_apply_import_writes_and_skips_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _write_codex_home(tmp_path / "home", monkeypatch)
    _write_config(codex, 'model = "gpt-5.2-codex"\nmodel_provider = "openai"\n')

    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = _FakeSettings(workspace)
    plan = cc.build_import_plan(workspace=workspace, target={"models": {}})
    written, skipped = cc.apply_import(settings, plan)
    assert (written, skipped) == (1, 0)

    # Idempotent second run skips the existing alias.
    alias = plan.items[0].alias
    payload = plan.items[0].payload
    plan2 = cc.build_import_plan(workspace=workspace, target={"models": {alias: payload}})
    written2, skipped2 = cc.apply_import(settings, plan2)
    assert written2 == 0
    assert skipped2 == 1


def test_apply_import_replace_overwrites_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = _write_codex_home(tmp_path / "home", monkeypatch)
    _write_config(codex, 'model = "gpt-5.2-codex"\nmodel_provider = "openai"\n')

    workspace = tmp_path / "work"
    workspace.mkdir()
    settings = _FakeSettings(workspace)
    target = {"models": {"openai:gpt-5.2-codex": {"model": "openai:old"}}}
    plan = cc.build_import_plan(workspace=workspace, target=target)
    plan.items[0].action = "replace"
    written, skipped = cc.apply_import(settings, plan)
    assert (written, skipped) == (1, 0)
    from synapse.models.persist import load_models_store

    data = load_models_store(settings)
    assert data["models"]["openai:gpt-5.2-codex"]["model"] == "openai:gpt-5.2-codex"
