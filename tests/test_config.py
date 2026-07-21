"""Unit tests for config and safety helpers."""

from __future__ import annotations

from pathlib import Path

from synapse.config import Settings, bootstrap_project_env, load_settings
from synapse.safety import build_interrupt_on, check_command


def test_default_approval_is_off(monkeypatch):
    monkeypatch.delenv("AGENT_REQUIRE_APPROVAL", raising=False)
    monkeypatch.delenv("AGENT_AUTO_APPROVE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.require_approval is False
    assert settings.auto_approve is True
    assert build_interrupt_on(require_approval=settings.require_approval) is None


def test_tool_details_expanded_default_and_env(monkeypatch):
    monkeypatch.delenv("AGENT_TOOL_DETAILS_EXPANDED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.tool_details_expanded is True

    monkeypatch.setenv("AGENT_TOOL_DETAILS_EXPANDED", "false")
    settings_off = Settings(_env_file=None)
    assert settings_off.tool_details_expanded is False


def test_models_config_discovered_beside_exe(tmp_path, monkeypatch):
    """Portable layout: models.json under exe-adjacent .synapse/."""
    from synapse.config_paths import models_config_paths

    monkeypatch.chdir(tmp_path)
    exe_root = tmp_path / "dist"
    models = exe_root / ".synapse" / "models.json"
    models.parent.mkdir(parents=True)
    models.write_text(
        '{"default":"p","models":{"p":{"model":"openai:x","api_key":"k","base_url":"http://x"}}}',
        encoding="utf-8",
    )

    workspace = tmp_path / "work"
    workspace.mkdir()

    import synapse.config_paths as cfgp

    monkeypatch.setattr(cfgp, "user_config_dir", lambda: tmp_path / "nouser" / ".synapse")
    monkeypatch.setattr(cfgp, "executable_config_dirs", lambda: [exe_root.resolve()])
    found = models_config_paths(workspace)
    assert any(p.resolve() == models.resolve() for p in found)


def test_interrupt_on_when_enabled():
    cfg = build_interrupt_on(require_approval=True)
    assert cfg is not None
    assert cfg["execute"] is True


def test_blacklist_blocks_rm_rf_root():
    verdict = check_command("rm -rf /")
    assert verdict.allowed is False


def test_blacklist_allows_pytest():
    verdict = check_command("uv run pytest -q")
    assert verdict.allowed is True


def test_load_settings_workspace_override(tmp_path: Path):
    settings = load_settings(workspace=tmp_path)
    assert settings.workspace == tmp_path.resolve()


def test_dotenv_wins_over_stale_process_env(tmp_path: Path, monkeypatch):
    """Project .env key must beat a stale system/user OPENAI_API_KEY."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=from-dotenv-key-123456\n"
        "OPENAI_BASE_URL=http://127.0.0.1:3000/v1\n"
        "MODEL=openai:demo-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "stale-system-key-fb5f")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("MODEL", "openai:stale-model")
    # Prevent global models.json from interfering with this test.
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(tmp_path / "nonexistent.json"))
    monkeypatch.chdir(tmp_path)

    loaded = bootstrap_project_env(tmp_path)
    assert loaded == env_file

    settings = load_settings(workspace=tmp_path, checkpoint_backend="memory")
    assert settings.openai_api_key == "from-dotenv-key-123456"
    assert settings.openai_base_url == "http://127.0.0.1:3000/v1"
    assert settings.model == "openai:demo-model"
    assert "from-dotenv" in settings.mask_openai_key() or "len=" in settings.mask_openai_key()
