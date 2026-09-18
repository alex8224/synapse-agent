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


def test_cli_web_console_forwards_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "synapse.web_console.entry.main",
        lambda argv: calls.append(argv) or 0,
    )
    result = runner.invoke(app, ["web-console", "--port", "8123", "--no-pairing"])

    assert result.exit_code == 0
    assert calls == [["--port", "8123", "--no-pairing"]]


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


def _cli_settings(tmp_path, monkeypatch):
    """Point the CLI at a temporary project with a real state directory."""
    from types import SimpleNamespace

    state = tmp_path / ".synapse"
    state.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(
        workspace=tmp_path,
        checkpoint_path=state / "checkpoints.sqlite",
        resolved_sessions_path=lambda: state / "sessions.sqlite",
    )
    monkeypatch.setattr("synapse.cli.load_settings", lambda **_: settings)
    return settings


def _seed_cli_history(settings, thread_id: str) -> None:
    """Give one thread a conversation in the stores a delete must clear."""
    import sqlite3

    with sqlite3.connect(settings.checkpoint_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints (thread_id TEXT NOT NULL, "
            "checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, "
            "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, '', 'ckpt')", (thread_id,)
        )
    with sqlite3.connect(settings.resolved_sessions_path().parent / "search-index.sqlite") as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (thread_id TEXT NOT NULL, "
            "seq INTEGER NOT NULL, PRIMARY KEY (thread_id, seq))"
        )
        conn.execute("INSERT INTO messages VALUES (?, 0)", (thread_id,))


def _count(path, table: str, thread_id: str) -> int:
    import sqlite3

    with sqlite3.connect(path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
        )


def test_cli_sessions_delete_erases_the_conversation(tmp_path, monkeypatch):
    """The CLI delete is the same delete: the conversation goes with the row."""
    from synapse.sessions.store import SessionStore

    settings = _cli_settings(tmp_path, monkeypatch)
    store = SessionStore(settings.resolved_sessions_path())
    store.ensure("t1", title="Session one")
    store.close()
    _seed_cli_history(settings, "t1")

    result = runner.invoke(app, ["sessions", "delete", "t1"])

    assert result.exit_code == 0
    assert _count(settings.checkpoint_path, "checkpoints", "t1") == 0
    assert (
        _count(settings.resolved_sessions_path().parent / "search-index.sqlite", "messages", "t1")
        == 0
    )
    store = SessionStore(settings.resolved_sessions_path())
    assert store.get("t1") is None
    store.close()


def test_cli_sessions_purge_lists_orphans_and_only_erases_with_apply(tmp_path, monkeypatch):
    """The sweep is a dry run by default, because it is irreversible."""
    from synapse.sessions.store import SessionStore

    settings = _cli_settings(tmp_path, monkeypatch)
    store = SessionStore(settings.resolved_sessions_path())
    store.ensure("live", title="Live session")
    store.close()
    _seed_cli_history(settings, "live")
    # The leftover of a delete that removed only the row: history, no metadata.
    _seed_cli_history(settings, "orphan")

    dry = runner.invoke(app, ["sessions", "purge"])

    assert dry.exit_code == 0
    assert "orphan" in dry.stdout
    assert "live" not in dry.stdout
    # Nothing was erased, and the live session was never a candidate.
    assert _count(settings.checkpoint_path, "checkpoints", "orphan") == 1
    assert _count(settings.checkpoint_path, "checkpoints", "live") == 1

    applied = runner.invoke(app, ["sessions", "purge", "--apply"])

    assert applied.exit_code == 0
    assert _count(settings.checkpoint_path, "checkpoints", "orphan") == 0
    assert _count(settings.checkpoint_path, "checkpoints", "live") == 1


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


def test_resolve_launch_target_model_forwarding_into_settings(tmp_path, monkeypatch):
    """Regression: _resolve_launch_target must not leak ``active_model``.

    ``_resolve_launch_target(model=...)`` returned an ``overrides`` dict that
    contained ``active_model``, which ``_resolve_settings`` does not accept;
    ``synapse tui --model <alias>`` therefore crashed with a TypeError on
    ``_resolve_settings(**overrides)``.  The alias forwarding belongs to
    ``_resolve_settings``, which maps ``model`` to both ``model`` and
    ``active_model`` overrides.
    """
    from synapse.cli import _resolve_launch_target, _resolve_settings

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "synapse.cli.load_settings",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    overrides, thread_id, root = _resolve_launch_target(
        workspace=tmp_path,
        session=None,
        project=None,
        model="deepseek-v4-flash",
        require_approval=False,
        readonly=False,
        debug=False,
    )
    # The overrides dict must stay a subset of _resolve_settings' parameters.
    settings = _resolve_settings(**overrides)
    assert settings is not None
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["active_model"] == "deepseek-v4-flash"
    assert thread_id is None
    assert root == tmp_path.resolve()


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
