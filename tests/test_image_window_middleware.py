"""Tests for producer-scoped image pruning of the model request."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from synapse.runtime.image_window_middleware import (
    IMAGE_PLACEHOLDER,
    SCREENSHOT_PLACEHOLDER,
    build_image_window_middleware,
    image_payload_budget,
    prune_request_images,
)


def _image_block(payload: str) -> dict[str, Any]:
    return {"type": "image", "base64": payload, "mime_type": "image/png"}


def _tool_image(
    name: str,
    tool_call_id: str,
    payload: str = "A" * 100,
    *,
    text: str | None = None,
) -> ToolMessage:
    blocks: list[Any] = []
    if text is not None:
        blocks.append({"type": "text", "text": text})
    blocks.append(_image_block(payload))
    return ToolMessage(content_blocks=blocks, tool_call_id=tool_call_id, name=name)


def _user_image(payload: str = "A" * 100) -> HumanMessage:
    return HumanMessage(content=[{"type": "text", "text": "look"}, _image_block(payload)])


def _has_image(message: Any) -> bool:
    content = message.content
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}
        for block in content
    )


def _texts(message: Any) -> list[str]:
    content = message.content
    if not isinstance(content, list):
        return []
    return [str(b.get("text")) for b in content if isinstance(b, dict) and b.get("type") == "text"]


def test_user_uploads_are_never_pruned() -> None:
    messages = [_user_image("A" * 500) for _ in range(5)]

    assert prune_request_images(messages, payload_budget=1) is None


def test_user_payload_is_reserved_out_of_the_budget() -> None:
    """Uploads are exempt from pruning but still consume the request's budget."""
    upload = _user_image("A" * 300)
    newest_tool = _tool_image("read_file", "c1", payload="A" * 400)
    older_tool = _tool_image("read_file", "c2", payload="A" * 100)

    pruned = prune_request_images(
        [older_tool, upload, newest_tool], tool_ceiling=50, payload_budget=500
    )

    assert pruned is not None
    # 500 budget - 300 reserved = 200 for tool images: only the 100-char one fits.
    assert not _has_image(pruned[2])
    assert _has_image(pruned[0])
    assert _has_image(pruned[1])


def test_only_the_newest_screenshot_is_kept() -> None:
    messages = [
        _tool_image("cdp_take_screenshot", "c1"),
        _tool_image("cdp_take_screenshot", "c2"),
        _tool_image("cdp_take_screenshot", "c3"),
    ]

    pruned = prune_request_images(messages, screenshot_window=1, payload_budget=10**9)

    assert pruned is not None
    assert not _has_image(pruned[0])
    assert not _has_image(pruned[1])
    assert _has_image(pruned[2])
    assert _texts(pruned[0]) == [SCREENSHOT_PLACEHOLDER]


def test_other_tool_images_use_the_global_ceiling() -> None:
    messages = [_tool_image("read_file", f"c{i}") for i in range(4)]

    pruned = prune_request_images(messages, tool_ceiling=2, payload_budget=10**9)

    assert pruned is not None
    assert [_has_image(m) for m in pruned] == [False, False, True, True]
    assert _texts(pruned[0]) == [IMAGE_PLACEHOLDER]


def test_payload_budget_prunes_even_within_the_count_ceiling() -> None:
    messages = [
        _tool_image("read_file", "c1", payload="A" * 400),
        _tool_image("read_file", "c2", payload="A" * 400),
    ]

    pruned = prune_request_images(messages, tool_ceiling=50, payload_budget=500)

    assert pruned is not None
    # Newest is 400 chars; the older one would exceed the 500-char budget.
    assert not _has_image(pruned[0])
    assert _has_image(pruned[1])


def test_pruning_preserves_tool_call_id_identity_and_other_blocks() -> None:
    messages = [
        _tool_image("read_file", "call-1", text="screenshot.png"),
        _tool_image("read_file", "call-2"),
    ]

    pruned = prune_request_images(messages, tool_ceiling=1, payload_budget=10**9)

    assert pruned is not None
    assert pruned[0] is not messages[0]
    assert pruned[0].tool_call_id == "call-1"
    assert pruned[0].name == "read_file"
    assert _texts(pruned[0]) == ["screenshot.png", IMAGE_PLACEHOLDER]


def test_originals_are_not_mutated() -> None:
    original = _tool_image("read_file", "call-1")
    messages = [original, _tool_image("read_file", "call-2")]

    prune_request_images(messages, tool_ceiling=1, payload_budget=10**9)

    assert _has_image(original)
    assert messages[0] is original


def test_placeholder_is_byte_stable_across_repeated_passes() -> None:
    messages = [_tool_image("read_file", "c1"), _tool_image("read_file", "c2")]

    first = prune_request_images(messages, tool_ceiling=1, payload_budget=10**9)
    second = prune_request_images(messages, tool_ceiling=1, payload_budget=10**9)

    assert first is not None and second is not None
    assert first[0].content == second[0].content
    # A pruned message carries no image block, so a second pass over it is a no-op.
    assert prune_request_images(first, tool_ceiling=1, payload_budget=10**9) is None


def test_tool_name_falls_back_to_the_tool_call_index() -> None:
    ai = AIMessage(
        content="",
        tool_calls=[
            {"id": "call-1", "name": "cdp_take_screenshot", "args": {}, "type": "tool_call"}
        ],
    )
    unnamed = ToolMessage(
        content_blocks=[_image_block("A" * 10)], tool_call_id="call-1"
    )
    named = ToolMessage(
        content_blocks=[_image_block("A" * 10)], tool_call_id="call-2", name="read_file"
    )

    pruned = prune_request_images(
        [ai, unnamed, named], screenshot_window=0, tool_ceiling=50, payload_budget=10**9
    )

    assert pruned is not None
    assert _texts(pruned[1]) == [SCREENSHOT_PLACEHOLDER]
    assert _has_image(pruned[2])


def test_no_images_leaves_the_request_untouched() -> None:
    messages = [HumanMessage(content="hi"), AIMessage(content="ok")]

    assert prune_request_images(messages) is None


def test_image_payload_budget_scales_with_the_model_window() -> None:
    assert image_payload_budget(SimpleNamespace(profile={"max_input_tokens": 1_000_000})) == 700_000
    assert image_payload_budget(SimpleNamespace(profile={"max_input_tokens": 370_000})) == 259_000
    assert image_payload_budget(SimpleNamespace(profile=None)) == 1_000_000


class _Request:
    """Minimal ModelRequest stand-in carrying only what the middleware reads."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def override(self, **kwargs: Any) -> _Request:
        return _Request(list(kwargs.get("messages", self.messages)))


def test_wrap_model_call_hands_the_pruned_request_to_the_handler() -> None:
    middleware = build_image_window_middleware(payload_budget=10**9, tool_ceiling=1)
    request = _Request([_tool_image("read_file", "c1"), _tool_image("read_file", "c2")])
    seen: dict[str, Any] = {}

    def handler(req: Any) -> str:
        seen["messages"] = req.messages
        return "done"

    assert middleware.wrap_model_call(request, handler) == "done"
    assert [_has_image(m) for m in seen["messages"]] == [False, True]


def test_wrap_model_call_passes_through_when_nothing_to_prune() -> None:
    middleware = build_image_window_middleware()
    request = _Request([HumanMessage(content="hi")])
    seen: dict[str, Any] = {}

    def handler(req: Any) -> str:
        seen["request"] = req
        return "done"

    middleware.wrap_model_call(request, handler)

    assert seen["request"] is request
