"""Startup path helpers: deferred MCP + agent build flags."""

from __future__ import annotations

from synapse.agent import resolve_load_mcp
from synapse.config import Settings


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
