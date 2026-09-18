"""Transport client verification for runtime.skills.list (v1 additive)."""

from __future__ import annotations

import asyncio
import json

import pytest

from synapse.runtime.service.skills import (
    ListSkillsQuery,
    SkillEntry,
    SkillListPage,
)
from synapse.runtime.transport import CAPABILITIES, RuntimeWebSocketClient


class _FakeConnection:
    """Minimal wire double: answers the handshake, then one canned result."""

    def __init__(self, result: object) -> None:
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.frames: list[dict[str, object]] = []
        self.closed = False
        self._result = result

    async def send(self, message: str) -> None:
        frame = json.loads(message)
        self.frames.append(frame)
        if frame["method"] == "runtime.protocol.negotiate":
            response: object = {
                "wire_version": "1",
                "supported_versions": ["1"],
                "capabilities": CAPABILITIES,
            }
        else:
            response = self._result
        await self.inbox.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": frame["id"],
                    "meta": {"wire_version": "1"},
                    "result": response,
                }
            )
        )

    async def recv(self) -> str:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True

    def business_frames(self) -> list[dict[str, object]]:
        return [f for f in self.frames if f.get("method") != "runtime.protocol.negotiate"]


def _client(connection: _FakeConnection) -> RuntimeWebSocketClient:
    return RuntimeWebSocketClient(
        "ws://loopback", connect_factory=lambda *a, **k: connection
    )


def test_list_skills_decodes_page_and_sends_query() -> None:
    async def run() -> None:
        raw_page = {
            "skills": [
                {
                    "name": "cua-driver",
                    "description": "Windows 真实桌面自动化",
                    "path": "/path/to/cua-driver/SKILL.md",
                    "source": "repo",
                }
            ]
        }
        connection = _FakeConnection(raw_page)
        client = _client(connection)

        result = await client.list_skills(ListSkillsQuery(project_id="sample-proj"))
        assert isinstance(result, SkillListPage)
        assert len(result.skills) == 1
        skill = result.skills[0]
        assert isinstance(skill, SkillEntry)
        assert skill.name == "cua-driver"
        assert skill.description == "Windows 真实桌面自动化"
        assert skill.path == "/path/to/cua-driver/SKILL.md"
        assert skill.source == "repo"

        frames = connection.business_frames()
        assert len(frames) == 1
        frame = frames[0]
        assert frame["method"] == "runtime.skills.list"
        assert frame["params"] == {"project_id": "sample-proj"}

    asyncio.run(run())


def test_list_skills_sends_empty_params_when_project_id_is_none() -> None:
    async def run() -> None:
        connection = _FakeConnection({"skills": []})
        client = _client(connection)

        result = await client.list_skills(ListSkillsQuery())
        assert isinstance(result, SkillListPage)
        assert result.skills == ()

        frames = connection.business_frames()
        assert len(frames) == 1
        frame = frames[0]
        assert frame["method"] == "runtime.skills.list"
        assert frame["params"] == {}

    asyncio.run(run())


def test_list_skills_rejects_foreign_query() -> None:
    async def run() -> None:
        connection = _FakeConnection({"skills": []})
        client = _client(connection)

        with pytest.raises(ValueError):
            await client.list_skills(object())  # type: ignore[arg-type]

    asyncio.run(run())
