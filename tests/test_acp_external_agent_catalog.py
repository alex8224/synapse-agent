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


def test_external_agent_catalog_reads_a_per_agent_create_timeout(tmp_path: Path) -> None:
    """`session/new` is the peer's own work, so its budget is configurable."""
    row = {"id": "agy", "name": "Antigravity", "command": sys.executable,
           "create_timeout_s": 120}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [row]}))
    agent = ExternalAgentCatalog.from_state_dir(tmp_path).get("agy")
    assert agent is not None
    assert agent.create_timeout_s == 120.0
    # Absent means "keep the client default", never "no timeout".
    plain = {"id": "other", "name": "Other", "command": sys.executable}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [plain]}))
    default_agent = ExternalAgentCatalog.from_state_dir(tmp_path).get("other")
    assert default_agent is not None
    assert default_agent.create_timeout_s is None


@pytest.mark.parametrize("value", [True, "120", 0, 4.9, 601, None])
def test_external_agent_catalog_rejects_an_unusable_create_timeout(
    tmp_path: Path, value: object
) -> None:
    # `null` is accepted as "default", so only the other values may raise.
    row = {"id": "agy", "name": "Antigravity", "command": sys.executable,
           "create_timeout_s": value}
    (tmp_path / "external-acp-agents.json").write_text(json.dumps({"agents": [row]}))
    if value is None:
        assert ExternalAgentCatalog.from_state_dir(tmp_path).get("agy").create_timeout_s is None
        return
    with pytest.raises(ValueError):
        ExternalAgentCatalog.from_state_dir(tmp_path)


def test_external_agent_catalog_hot_reloads_and_preserves_valid_on_bad_json(
    tmp_path: Path,
) -> None:
    catalog = ExternalAgentCatalog.from_state_dir(tmp_path)
    assert catalog.list() == ()

    config_path = tmp_path / "external-acp-agents.json"
    config_path.write_text(
        json.dumps({"agents": [{"id": "agy", "name": "Antigravity", "command": sys.executable}]})
    )
    assert [a.id for a in catalog.list()] == ["agy"]

    # Temporarily invalid JSON must not wipe the last known good catalog.
    config_path.write_text("{broken json")
    assert [a.id for a in catalog.list()] == ["agy"]

    # Updating to a valid new list takes effect immediately without restart.
    config_path.write_text(
        json.dumps(
            {
                "agents": [
                    {"id": "agy", "name": "Antigravity", "command": sys.executable},
                    {"id": "claude", "name": "Claude ACP", "command": sys.executable},
                ]
            }
        )
    )
    assert [a.id for a in catalog.list()] == ["agy", "claude"]
