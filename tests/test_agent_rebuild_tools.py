"""Injected tools must survive every agent-rebuild path.

A rebind (model switch, MCP attach/reload) builds a brand-new graph. Before this
guard the daemon's collaboration tools were dropped there, so a console session
that had just attached MCP could no longer reach ``contact_session``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def test_mcp_rebinding_forwards_injected_tools(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synapse.runtime.daemon import application as app

    builds: list[dict[str, Any]] = []

    class _Registry:
        def get(self, key: str) -> Any:
            return None

    def _fake_build(settings: Any, **kwargs: Any) -> Any:
        builds.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        'synapse.integrations.mcp_client.get_mcp_pool_registry', lambda: _Registry()
    )
    monkeypatch.setattr(
        'synapse.integrations.mcp_client.load_mcp_server_configs',
        lambda **kwargs: {},
    )
    monkeypatch.setattr(
        app,
        'load_project_settings',
        lambda workspace: SimpleNamespace(mcp_config_path=None, mcp_servers_json=None),
    )
    monkeypatch.setattr(app, 'apply_binding_to_settings', lambda *a, **k: None)
    monkeypatch.setattr(app, 'binding_from_settings', lambda *a, **k: None)
    monkeypatch.setattr(app, '_mcp_server_states', lambda *a, **k: {})
    monkeypatch.setattr('synapse.app.agent.build_coding_agent', _fake_build)

    descriptor = SimpleNamespace(project_id='p1', workspace=tmp_path)
    binding = SimpleNamespace(settings=SimpleNamespace(active_model='m', model='m'))
    common = {
        'descriptor': descriptor,
        'project_settings': SimpleNamespace(mcp_config_path=None),
        'thread_id': 't1',
        'server': None,
        'enabled': None,
        'include_tools': None,
        'binding': binding,
    }

    app.apply_mcp_rebinding(**common)
    assert builds[-1]['extra_tools'] is None

    app.apply_mcp_rebinding(extra_tools=['probe'], **common)
    assert builds[-1]['extra_tools'] == ['probe']


def test_rebuild_coding_agent_reapplies_injected_tools(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock, patch

    from langchain_core.tools import tool

    from synapse.app import agent as agent_mod
    from synapse.settings import Settings

    @tool
    def probe(value: str) -> str:
        """A probe tool."""
        return value

    settings = Settings(
        _env_file=None,
        workspace=tmp_path,
        model='openai:gpt-4.1',
        checkpoint_backend='memory',
        enable_mcp=False,
        enable_subagents=False,
        enable_custom_subagents=False,
        enable_memory=False,
        enable_goals=False,
    )
    monkeypatch.setattr(agent_mod, 'ensure_user_subagents', lambda: [])
    with (
        patch('synapse.models.registry.init_chat_model', return_value=MagicMock(name='model')),
        patch('deepagents.create_deep_agent', return_value=MagicMock(name='agent')) as create,
        patch('deepagents.register_harness_profile', MagicMock()),
        patch('deepagents.HarnessProfile', MagicMock()),
    ):
        agent = agent_mod.build_coding_agent(
            settings, project_root=tmp_path, extra_tools=[probe]
        )
        assert agent._coding_extra_tools == [probe]

        agent_mod.rebuild_coding_agent(settings, agent, project_root=tmp_path)

    assert probe in create.call_args.kwargs['tools']
