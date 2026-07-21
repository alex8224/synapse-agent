"""CLI help smoke tests."""

from typer.testing import CliRunner

from synapse.cli import app

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


def test_cli_models_help():
    result = runner.invoke(app, ["models", "--help"])
    assert result.exit_code == 0
    assert "model" in result.stdout.lower()


def test_cli_mcp_help():
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "mcp" in result.stdout.lower()
