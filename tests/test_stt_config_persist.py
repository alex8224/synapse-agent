"""Tests for the speech settings' persist-and-apply path.

The console's engine switch is only useful if it does two things at once: survive a
restart (the file) and take effect in the running daemon (the live object).  Both
are pinned here, together with the refusal that keeps a hand-edited file from being
silently overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.runtime import stt_config_persist
from synapse.runtime.stt_config_persist import load_stt_config, save_stt_config


@pytest.fixture()
def user_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the helper at a temporary user config directory."""
    monkeypatch.setattr(stt_config_persist, "user_config_dir", lambda: tmp_path)
    return tmp_path


def _settings(engine: str = "browser", model_dir: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(stt_engine=engine, stt_model_dir=model_dir)


def test_the_choice_is_written_and_applied_to_the_live_settings(user_dir: Path) -> None:
    settings = _settings()
    path = save_stt_config(settings, engine="local", model_dir="/models/stt")

    assert path == user_dir / "settings.json"
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["stt_engine"] == "local"
    assert written["stt_model_dir"] == str(Path("/models/stt"))
    # The daemon reads `manager.settings` per request, so this is what makes the
    # change effective without a restart.
    assert settings.stt_engine == "local"
    assert settings.stt_model_dir == Path("/models/stt")


def test_other_keys_are_preserved(user_dir: Path) -> None:
    path = user_dir / "settings.json"
    path.write_text(json.dumps({"theme": "fluent-dark", "stt_engine": "browser"}), encoding="utf-8")
    save_stt_config(_settings(), engine="local", model_dir=None)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["theme"] == "fluent-dark"
    assert written["stt_engine"] == "local"


def test_clearing_the_model_dir_removes_the_key(user_dir: Path) -> None:
    settings = _settings("local", Path("/models/stt"))
    save_stt_config(settings, engine="local", model_dir=None)
    written = json.loads((user_dir / "settings.json").read_text(encoding="utf-8"))
    assert "stt_model_dir" not in written, "an empty field means the engine's default"
    assert settings.stt_model_dir is None


def test_a_corrupt_file_is_refused_and_left_alone(user_dir: Path) -> None:
    path = user_dir / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        save_stt_config(_settings(), engine="local", model_dir=None)
    assert "refusing to overwrite" in str(raised.value)
    assert path.read_text(encoding="utf-8") == "{not json"
    # The live settings must not move either: a write that did not happen cannot
    # leave the daemon on an engine that would not survive a restart.
    assert _settings().stt_engine == "browser"


def test_a_non_object_file_is_refused(user_dir: Path) -> None:
    (user_dir / "settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        save_stt_config(_settings(), engine="local", model_dir=None)
    assert "JSON object" in str(raised.value)


def test_an_unknown_engine_is_refused_before_anything_is_written(user_dir: Path) -> None:
    with pytest.raises(ValueError) as raised:
        save_stt_config(_settings(), engine="voice", model_dir=None)
    assert "unknown speech engine" in str(raised.value)
    assert not (user_dir / "settings.json").exists()


def test_load_normalizes_what_it_reports() -> None:
    assert load_stt_config(_settings()) == {"engine": "browser", "model_dir": None}
    assert load_stt_config(_settings("local", Path("/models/stt"))) == {
        "engine": "local",
        "model_dir": str(Path("/models/stt")),
    }
    # A value from a newer build (or a typo) reads as the engine the runtime would
    # actually fall back to, so the UI cannot claim a mode nothing will run.
    assert load_stt_config(_settings("quantum"))["engine"] == "browser"
