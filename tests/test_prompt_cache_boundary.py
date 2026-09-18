"""Tests for the prompt-cache boundary middleware."""

from __future__ import annotations

import asyncio

from langchain_core.messages import SystemMessage

from synapse.runtime.prompt_cache_boundary_middleware import (
    build_prompt_cache_boundary_middleware,
)

STABLE = "STABLE PROMPT\n"
REST = "\n\nDYNAMIC PROMPT\n"


class _Request:
    def __init__(self, system_message: SystemMessage) -> None:
        self.system_message = system_message

    def override(self, **changes):  # noqa: ANN003
        return _Request(changes.get("system_message", self.system_message))


async def _passthrough(current):  # noqa: ANN001, ANN202
    return current


def _blocks(message: SystemMessage) -> list[dict]:
    return [block for block in message.content_blocks if isinstance(block, dict)]


def _run(middleware, text: str) -> SystemMessage:  # noqa: ANN001
    request = _Request(SystemMessage(content=text))
    updated = middleware.wrap_model_call(request, lambda current: current)
    return updated.system_message


def test_splits_and_tags_the_stable_half() -> None:
    message = _run(build_prompt_cache_boundary_middleware(STABLE), STABLE + REST)
    blocks = _blocks(message)

    assert [block["text"] for block in blocks] == [STABLE, REST]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_split_preserves_the_original_text() -> None:
    message = _run(build_prompt_cache_boundary_middleware(STABLE), STABLE + REST)
    blocks = _blocks(message)

    assert "".join(block["text"] for block in blocks) == STABLE + REST


def test_second_pass_is_idempotent() -> None:
    middleware = build_prompt_cache_boundary_middleware(STABLE)
    first = _run(middleware, STABLE + REST)
    request = _Request(first)

    second = middleware.wrap_model_call(request, lambda current: current)

    assert second.system_message is first


def test_noop_when_prefix_is_absent() -> None:
    message = _run(build_prompt_cache_boundary_middleware(STABLE), "OTHER PROMPT")
    blocks = _blocks(message)

    assert [block.get("text") for block in blocks] == ["OTHER PROMPT"]
    assert all("cache_control" not in block for block in blocks)


def test_noop_when_nothing_follows_the_prefix() -> None:
    message = _run(build_prompt_cache_boundary_middleware(STABLE), STABLE)
    blocks = _blocks(message)

    assert [block.get("text") for block in blocks] == [STABLE]
    assert all("cache_control" not in block for block in blocks)


def test_noop_with_empty_prefix() -> None:
    message = _run(build_prompt_cache_boundary_middleware(""), STABLE + REST)
    blocks = _blocks(message)

    assert [block.get("text") for block in blocks] == [STABLE + REST]


def test_async_hook_splits_the_stable_half() -> None:
    middleware = build_prompt_cache_boundary_middleware(STABLE)

    async def run() -> SystemMessage:
        request = _Request(SystemMessage(content=STABLE + REST))
        updated = await middleware.awrap_model_call(request, _passthrough)
        return updated.system_message

    assert [block.get("text") for block in _blocks(asyncio.run(run()))] == [STABLE, REST]
