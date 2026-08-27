"""Startup path helpers: deferred MCP + agent build flags."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.app.agent import resolve_load_mcp
from synapse.config import Settings
from synapse.ui.tui import CodingAgentApp


def _settings(**kwargs) -> Settings:
    # Avoid process env / layered files overriding explicit flags in unit tests.
    return Settings.model_construct(**kwargs)


def test_resolve_load_mcp_default_deferred():
    s = _settings(enable_mcp=True, mcp_eager=False)
    assert resolve_load_mcp(s, None) is False
    assert resolve_load_mcp(s, True) is True
    assert resolve_load_mcp(s, False) is False


def test_resolve_load_mcp_eager():
    s = _settings(enable_mcp=True, mcp_eager=True)
    assert resolve_load_mcp(s, None) is True


def test_resolve_load_mcp_disabled():
    s = _settings(enable_mcp=False, mcp_eager=True)
    assert resolve_load_mcp(s, True) is False
    assert resolve_load_mcp(s, None) is False


def test_settings_tui_defer_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_MCP_EAGER", raising=False)
    monkeypatch.delenv("AGENT_TUI_DEFER_AGENT", raising=False)
    s = Settings(_env_file=None)
    # Defaults may be overridden by ~/.synapse/settings.json; assert field exists.
    assert hasattr(s, "mcp_eager")
    assert hasattr(s, "tui_defer_agent")
    assert Settings.model_fields["mcp_eager"].default is False
    assert Settings.model_fields["tui_defer_agent"].default is True


def test_tui_mount_schedules_git_chrome_refresh(monkeypatch) -> None:
    """A failed constructor probe must be retried before the first turn."""
    calls: list[str] = []
    log = SimpleNamespace(show_vertical_scrollbar=True, show_horizontal_scrollbar=True)
    prompt = SimpleNamespace(focus=lambda: calls.append("focus"))
    lifecycle = SimpleNamespace(should_build_on_mount=lambda: False)
    host = SimpleNamespace(
        settings=SimpleNamespace(theme="cursor-dark"),
        _lifecycle=lifecycle,
        apply_theme=lambda *args, **kwargs: calls.append("theme"),
        _refresh_git_chrome=lambda: calls.append("git"),
        _reload_tool_output_stats=lambda: calls.append("tool-output"),
        _on_tool_output_metrics_changed=lambda thread_id: None,
        _refresh_bottombar=lambda: None,
        _refresh_codex_usage=lambda: None,
        _tick_status=lambda: None,
        _check_history_edge=lambda: None,
        _mark_first_frame=lambda: None,
        _restore_session_transcript=lambda: None,
        set_interval=lambda *args, **kwargs: None,
        query_one=lambda selector, *args: log if selector == "#log" else prompt,
        call_after_refresh=lambda callback: None,
    )
    monkeypatch.setattr("synapse.ui.tui.set_metrics_notifier", lambda callback: None)

    CodingAgentApp.on_mount(host)

    assert calls[:3] == ["theme", "git", "tool-output"]
