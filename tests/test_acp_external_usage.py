"""Tests for external ACP (``agy``) quota usage parsing, caching, and RPC wiring."""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType

import pytest

from synapse.acp.external_usage import (
    ExternalUsageCache,
    is_agy_agent,
    parse_agy_usage_json,
)
from synapse.runtime.service.external_agents import ExternalAgentInfo, LocalExternalAgent


def _make_agent(agent_id: str = "agy", command: str = r"C:\bin\agy.exe") -> LocalExternalAgent:
    return LocalExternalAgent(
        info=ExternalAgentInfo(id=agent_id, name="Antigravity CLI", permission_mode="full_access"),
        command=command,
        args=("acp",),
        proxy_env=MappingProxyType({}),
    )


def test_is_agy_agent_matches_id_or_binary_stem() -> None:
    assert is_agy_agent(_make_agent("agy", r"C:\tools\custom.exe"))
    assert is_agy_agent(_make_agent("custom-acp", r"C:\Users\me\AppData\Local\agy\bin\agy.exe"))
    assert not is_agy_agent(_make_agent("claude", r"C:\tools\claude.exe"))


def test_parse_agy_usage_json_extracts_groups_and_buckets() -> None:
    sample = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": 0.9251,
                                "reset_time": "2026-12-01T02:29:59Z",
                            },
                            {
                                "id": "gemini-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 0.7004,
                                "reset_time": "2026-11-24T07:29:59Z",
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "description": "Models within this group: Claude Opus 5.5",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "name": "Weekly Limit Remaining",
                                "window": "weekly",
                                "remaining_fraction": 0.0213,
                                "reset_time": "2026-11-27T03:48:59Z",
                            },
                            {
                                "id": "3p-5h",
                                "name": "Five Hour Limit Remaining",
                                "window": "5h",
                                "remaining_fraction": 1,
                                "reset_time": "2026-11-24T08:19:35Z",
                            },
                        ],
                    },
                ]
            },
        },
    }
    groups = parse_agy_usage_json(json.dumps(sample))
    assert len(groups) == 2
    assert groups[0].name == "Gemini Models"
    assert len(groups[0].buckets) == 2
    assert groups[0].buckets[0].window_minutes == 10080
    assert groups[0].buckets[0].remaining_percent == pytest.approx(92.51)
    assert groups[0].buckets[0].reset_at is not None
    assert groups[0].buckets[1].window_minutes == 300
    assert groups[0].buckets[1].remaining_percent == pytest.approx(70.04)
    assert groups[1].name == "Claude and GPT models"
    assert groups[1].buckets[1].remaining_percent == pytest.approx(100.0)


def test_parse_agy_usage_tsv_fallback() -> None:
    tsv = (
        "Gemini Models\tWeekly Limit Remaining\t93%\t2026-12-01T02:29:59Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t70%\t2026-11-24T07:29:59Z\n"
    )
    groups = parse_agy_usage_json(tsv)
    assert len(groups) == 1
    assert groups[0].name == "Gemini Models"
    assert [b.window_minutes for b in groups[0].buckets] == [10080, 300]
    assert [b.remaining_percent for b in groups[0].buckets] == [93.0, 70.0]


def test_external_usage_cache_respects_ttl_and_force() -> None:
    calls = 0
    now = 1_700_000_000.0

    async def runner(_agent: LocalExternalAgent) -> bytes:
        nonlocal calls
        calls += 1
        return json.dumps(
            {
                "command": {
                    "data": {
                        "groups": [
                            {
                                "name": "Gemini Models",
                                "buckets": [
                                    {
                                        "id": "gemini-5h",
                                        "name": "Five Hour Limit Remaining",
                                        "window": "5h",
                                        "remaining_fraction": 0.8,
                                        "reset_time": "2026-11-24T07:29:59Z",
                                    }
                                ],
                            }
                        ]
                    }
                }
            }
        ).encode("utf-8")

    async def _run() -> None:
        nonlocal now
        cache = ExternalUsageCache(runner=runner, clock=lambda: now, ttl_s=300.0)
        agent = _make_agent()

        first = await cache.fetch(agent)
        assert first.supported is True
        assert calls == 1

        # Within TTL: cached
        now += 60.0
        second = await cache.fetch(agent)
        assert second is first
        assert calls == 1

        # Force bypasses cache
        third = await cache.fetch(agent, force=True)
        assert third.supported is True
        assert calls == 2

        # Non-agy agent reports unsupported without running command
        other = _make_agent("other", r"C:\bin\other.exe")
        unsupported = await cache.fetch(other)
        assert unsupported.supported is False
        assert unsupported.groups == ()
        assert calls == 2

    asyncio.run(_run())
