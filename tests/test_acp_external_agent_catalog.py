from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from synapse.runtime.service.external_agents import ExternalAgentCatalog


def test_external_agent_catalog_is_local_and_public_fields_are_safe(tmp_path: Path) -> None:
    assert not ExternalAgentCatalog.from_state_dir(tmp_path).list()
    row = dict(id="agy", name="Antigravity", command=sys.executable, args=["acp"],
               env={"HTTPS_PROXY": "http://localhost:7990"})
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [row]}))
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    assert [asdict(agent) for agent in catalog.list()] == [
        {"id": "agy", "name": "Antigravity", "permission_mode": "manual"}
    ]
    agent = catalog.get("agy")
    assert agent is not None
    assert agent.args == ("acp",)
    assert "7990" not in repr(agent)
    assert catalog.get("unknown") is None


@pytest.mark.parametrize(
    "override",
    [
        {"command": "relative"},
        {"env": {"OTHER": "value"}},
        {"permission_mode": True},
        {"args": ["acp", "--dangerously-skip-permissions"]},
        {"args": ["acp", "-dangerously-skip-permissions=true"]},
    ],
)
def test_external_agent_catalog_rejects_invalid_launch_config(
    tmp_path: Path, override: dict[str, object]
) -> None:
    row = {"id": "agy", "name": "Antigravity", "command": sys.executable, **override}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [row]}))
    with pytest.raises(ValueError):
        ExternalAgentCatalog.from_state_dir(tmp_path)
