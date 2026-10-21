"""Session lookup tools stay inside the current project's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from synapse.tools.session_tools import build_session_tools


def _tools(tmp_path: Path) -> dict[str, Any]:
    built = build_session_tools(
        sessions_path=tmp_path / "sessions.sqlite",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
    )
    return {tool.name: tool for tool in built}


def test_read_session_names_the_cross_project_route(tmp_path: Path) -> None:
    """A global reference must point at contact_session, not a bare miss."""
    out = _tools(tmp_path)["read_session"].invoke({"thread_id": "pid-1:tid-1"})

    assert "会话未找到: pid-1:tid-1" in out
    assert "跨项目全局引用" in out
    assert "contact_session" in out


def test_read_session_keeps_the_plain_miss_hint(tmp_path: Path) -> None:
    """A plain local id keeps the original recovery hint."""
    out = _tools(tmp_path)["read_session"].invoke({"thread_id": "missing"})

    assert "search_session" in out
    assert "contact_session" not in out


def test_search_session_states_its_project_scope(tmp_path: Path) -> None:
    """The tool description must not promise cross-project search."""
    description = _tools(tmp_path)["search_session"].description

    assert "当前项目" in description
    assert "contact_session" in description
