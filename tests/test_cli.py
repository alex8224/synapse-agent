"""CLI help and startup error handling tests."""

import ast
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import synapse.cli as cli
from synapse.cli import _bounded_preview_text, _launch_tui, _preview_warning_text, app

runner = CliRunner()


def test_cli_registers_exactly_one_default_callback_ast():
    callbacks = [
        node
        for node in ast.walk(ast.parse(Path(cli.__file__).read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "callback"
    ]
    assert len(callbacks) == 1


def test_cli_registers_exactly_one_tui_command_ast():
    source = Path(cli.__file__).read_text()
    tree = ast.parse(source)
    commands = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "command"
    ]
    assert (
        sum(
            any(isinstance(arg, ast.Constant) and arg.value == "tui" for arg in node.args)
            for node in commands
        )
        == 1
    )


def test_cli_typer_command_names_are_unique():
    names = [command.name for command in app.registered_commands]
    assert len(names) == len(set(names))


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


def test_resolve_launch_target_workspace_only(tmp_path):
    from synapse.cli import _resolve_launch_target

    overrides, thread_id, root = _resolve_launch_target(
        workspace=tmp_path,
        session=None,
        project=None,
        model=None,
        require_approval=False,
        readonly=False,
        debug=False,
    )
    assert overrides["workspace"] == tmp_path
    assert thread_id is None
    assert root == tmp_path.resolve()


def test_resolve_launch_target_session_reference(tmp_path, monkeypatch):
    from synapse.cli import _resolve_launch_target

    calls: dict[str, object] = {}

    class _FakeInfo:
        project_id = "proj-1"
        workspace_path = "/ws/p1"

    class _FakeCatalog:
        def __init__(self, *a, **k):
            del a, k
            calls["catalog"] = True

        def resolve_project(self, ref):
            del ref
            return _FakeInfo()

        def get_project(self, project_id=None):
            del project_id
            return _FakeInfo()

    class _FakeSettings:
        def resolved_catalog_path(self):
            return None

    monkeypatch.setattr("synapse.cli.ProjectCatalog", _FakeCatalog)
    monkeypatch.setattr("synapse.cli.load_settings", lambda **k: _FakeSettings())
    monkeypatch.setattr(
        "synapse.runtime.sessions.resolve_session_ref",
        lambda value, *, catalog=None, verify=False: type("R", (), {"project_id": "proj-1"})(),
    )

    overrides, thread_id, root = _resolve_launch_target(
        workspace=None,
        session="proj-1:thread-9",
        project=None,
        model=None,
        require_approval=False,
        readonly=False,
        debug=False,
    )
    assert overrides["workspace"] == "/ws/p1"
    assert thread_id == "thread-9"
    assert root == Path("/ws/p1").resolve()


def test_resolve_launch_target_no_args_still_has_workspace_key(tmp_path):
    """Regression: _resolve_settings requires the workspace key even when unset."""
    from synapse.cli import _resolve_launch_target

    overrides, thread_id, root = _resolve_launch_target(
        workspace=None,
        session=None,
        project=None,
        model=None,
        require_approval=False,
        readonly=False,
        debug=False,
    )
    assert "workspace" in overrides
    assert overrides["workspace"] is None
    assert thread_id is None
    assert root is None


def test_launch_tui_restarts_for_drawer_project_and_session(monkeypatch, tmp_path):
    calls: list[dict[str, object]] = []

    class _FakeSettings:
        def resolved_catalog_path(self):
            return tmp_path / "catalog.sqlite"

    class _FakeProject:
        workspace_path = str(tmp_path / "target")

    class _FakeCatalog:
        def __init__(self, path):
            calls.append({"catalog_path": path})

        def get_project(self, *, project_id):
            calls.append({"project_id": project_id})
            return _FakeProject()

    results = iter(
        [
            ("switch_project", "project-2", "thread-2"),
            None,
        ]
    )
    tui_calls: list[dict[str, object]] = []

    monkeypatch.setattr("synapse.cli._bootstrap_env", lambda: None)
    monkeypatch.setattr("synapse.cli._resolve_settings", lambda **kwargs: _FakeSettings())
    monkeypatch.setattr("synapse.cli.ProjectCatalog", _FakeCatalog)
    monkeypatch.setattr("synapse.cli.load_settings", lambda **kwargs: _FakeSettings())
    monkeypatch.setattr(
        "synapse.ui.tui.run_tui",
        lambda **kwargs: tui_calls.append(kwargs) or next(results),
    )

    _launch_tui(
        workspace=tmp_path / "source",
        model=None,
        require_approval=False,
        readonly=False,
        thread_id="thread-1",
        debug=False,
    )

    assert [call["project_root"] for call in tui_calls] == [
        (tmp_path / "source").resolve(),
        (tmp_path / "target").resolve(),
    ]
    assert [call["thread_id"] for call in tui_calls] == ["thread-1", "thread-2"]
    assert {call.get("project_id") for call in calls if call.get("project_id")} == {"project-2"}
