"""Layered user/project config roots and models merge."""

from __future__ import annotations

from synapse.config import Settings, load_settings
from synapse.config_paths import (
    layered_config_dirs,
    load_layered_settings_file,
    project_config_dir,
)
from synapse.models_registry import (
    ModelProfile,
    load_merged_models_registry,
    merge_model_profiles,
)


def test_layered_dirs_order(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "synapse.config_paths.user_config_dir",
        lambda: tmp_path / "home" / ".synapse",
    )
    monkeypatch.setattr(
        "synapse.config_paths.executable_config_dirs",
        lambda: [],
    )
    workspace = tmp_path / "proj"
    workspace.mkdir()
    dirs = layered_config_dirs(workspace, include_exe=False)
    assert dirs[0] == (tmp_path / "home" / ".synapse").resolve()
    assert dirs[-1] == project_config_dir(workspace)


def test_models_merge_user_then_project(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".synapse"
    proj = tmp_path / "proj"
    proj.mkdir()
    user_models = home / "models.json"
    user_models.parent.mkdir(parents=True)
    user_models.write_text(
        """
        {
          "default": "primary",
          "models": {
            "primary": {
              "model": "openai:base-model",
              "api_key": "user-key",
              "base_url": "http://user"
            },
            "shared": {
              "model": "openai:shared",
              "api_key": "shared-key"
            }
          }
        }
        """,
        encoding="utf-8",
    )
    proj_models = proj / ".synapse" / "models.json"
    proj_models.parent.mkdir(parents=True)
    proj_models.write_text(
        """
        {
          "default": "primary",
          "models": {
            "primary": {
              "model": "openai:proj-model",
              "base_url": "http://proj"
            }
          }
        }
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "synapse.config_paths.user_config_dir",
        lambda: home.resolve(),
    )
    monkeypatch.setattr(
        "synapse.config_paths.executable_config_dirs",
        lambda: [],
    )
    monkeypatch.chdir(proj)

    settings = Settings(_env_file=None, workspace=proj.resolve())
    reg = load_merged_models_registry(settings)
    assert reg is not None
    primary = reg.profiles["primary"]
    assert primary.model == "openai:proj-model"
    assert primary.base_url == "http://proj"
    # api_key inherited from user layer when project omits it
    assert primary.api_key == "user-key"
    assert "shared" in reg.profiles

    applied = load_settings(workspace=proj.resolve())
    # load_settings also reads real user home unless patched on config_paths
    # used by load_layered; patch already on user_config_dir
    assert applied.model == "openai:proj-model"
    assert applied.openai_api_key == "user-key"
    assert applied.openai_base_url == "http://proj"


def test_settings_json_layer(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".synapse"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(
        '{"max_concurrency": 3, "token_stream": false}',
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".synapse").mkdir()
    (proj / ".synapse" / "settings.json").write_text(
        '{"max_concurrency": 9}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "synapse.config_paths.user_config_dir",
        lambda: home.resolve(),
    )
    monkeypatch.setattr(
        "synapse.config_paths.executable_config_dirs",
        lambda: [],
    )
    cfg = load_layered_settings_file(proj)
    assert cfg["max_concurrency"] == 9
    assert cfg["token_stream"] is False


def test_merge_profiles_inherit_key():
    base = ModelProfile(name="p", model="openai:a", api_key="k1", base_url="http://a")
    over = ModelProfile(name="p", model="openai:b", base_url="http://b")
    m = merge_model_profiles(base, over)
    assert m.model == "openai:b"
    assert m.api_key == "k1"
    assert m.base_url == "http://b"
