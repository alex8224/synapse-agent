"""CLI help and startup error handling tests."""

import pytest
import typer
from typer.testing import CliRunner

from synapse.cli import _bounded_preview_text, _launch_tui, _preview_warning_text, app

runner = CliRunner()


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "coding agent" in result.stdout.lower() or "Coding" in result.stdout


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    # Rich may inject ANSI; strip and also accept plain.
    plain = result.stdout.replace("\x1b[1;36m", "").replace("\x1b[0m", "")
    assert "0.1.0" in result.stdout or "0.1.0" in plain


def test_cli_tui_help():
    result = runner.invoke(app, ["tui", "--help"])
    assert result.exit_code == 0
    assert "tui" in result.stdout.lower() or "Textual" in result.stdout


def test_cli_sessions_help():
    result = runner.invoke(app, ["sessions", "--help"])
    assert result.exit_code == 0
    assert "session" in result.stdout.lower()
    assert "codex-list" in result.stdout
    assert "codex-inspect" in result.stdout
    assert "codex-preview" in result.stdout
    assert "codex-import" in result.stdout


def test_codex_preview_helpers_bound_text_and_explain_known_errors():
    text, truncated = _bounded_preview_text("x" * 12_001)

    assert truncated is True
    assert text.endswith("[message truncated]")
    assert _preview_warning_text("rollout_size_limit") == "历史解压后的大小超过安全上限"
    assert _preview_warning_text("future_warning") == "历史包含暂不支持的记录"


def test_cli_models_help():
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0
    assert "model" in result.stdout.lower()


def test_cli_openai_auth_help():
    result = runner.invoke(app, ["auth", "openai", "--help"])
    assert result.exit_code == 0
    assert "login" in result.stdout
    assert "status" in result.stdout
    assert "logout" in result.stdout


def test_cli_tool_output_help():
    result = runner.invoke(app, ["tool-output", "--help"])
    assert result.exit_code == 0
    assert "stats" in result.stdout
    assert "status" in result.stdout
    assert "events" in result.stdout


def test_cli_tool_output_eval_fixture():
    fixture = __import__("pathlib").Path(__file__).parent / "fixtures" / "tool_output_eval.json"
    result = runner.invoke(app, ["tool-output", "eval", str(fixture)])
    assert result.exit_code == 0
    assert "passed: 3" in result.stdout


def test_cli_mcp_help():
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "mcp" in result.stdout.lower()


def test_launch_tui_reports_invalid_models_json_without_traceback(tmp_path, monkeypatch, capsys):
    models_path = tmp_path / "models.json"
    models_path.write_text('{"models": {"main": {"model": "openai:test"},}', encoding="utf-8")
    monkeypatch.setenv("AGENT_MODELS_CONFIG", str(models_path))
    monkeypatch.delenv("MODELS_JSON", raising=False)
    monkeypatch.setattr("synapse.cli._bootstrap_env", lambda: None)

    with pytest.raises(typer.Exit) as exc_info:
        _launch_tui(
            workspace=tmp_path,
            model=None,
            require_approval=False,
            readonly=False,
            thread_id=None,
            debug=False,
        )

    output = capsys.readouterr().out
    assert getattr(exc_info.value, "exit_code", None) == 1
    assert "Configuration error:" in output
    assert str(models_path) in output
    assert "invalid models config JSON" in output
    assert "MODELS_JSON" in output
