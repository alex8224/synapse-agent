"""Tests for the cache-friendly tool-RESPONSE truncation middleware."""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from synapse.runtime.tool_response_truncate_middleware import (
    build_tool_response_truncate_middleware,
)
from synapse.settings import Settings


class _FakeRequest:
    """Minimal ModelRequest stand-in exposing messages + immutable override."""

    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **overrides):
        req = _FakeRequest(self.messages)
        for key, value in overrides.items():
            setattr(req, key, value)
        return req


def _tool_msg(name: str, content: str, call_id: str = "call_1") -> ToolMessage:
    """A ToolMessage preceded by the AIMessage that invoked the tool."""
    return ToolMessage(content=content, tool_call_id=call_id)


def _ai_msg(name: str, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": {}}],
    )


def _turn(name: str, content: str, call_id: str = "call_1") -> list:
    return [_ai_msg(name, call_id), _tool_msg(name, content, call_id)]


def _apply(middleware, messages):
    seen: list[object] = []

    def handler(request):
        seen.append(request)
        return "ok"

    middleware.wrap_model_call(_FakeRequest(messages), handler)
    return seen[0]


def _settings(**overrides) -> Settings:
    """Build Settings via model_copy: fields with validation_alias do not
    accept field-name kwargs in __init__, but model_copy(update=...) does."""
    base = {
        "enable_tool_response_truncate": True,
        "tool_response_truncate_keep_tokens": 2000,
        "tool_response_truncate_max_head_chars": 500,
        "tool_response_truncate_max_tail_chars": 0,
    }
    base.update(overrides)
    return Settings().model_copy(update=base)


def test_disabled_by_default_returns_original_request():
    mw = build_tool_response_truncate_middleware(Settings())
    msgs = _turn("execute", "x" * 10000)
    seen = _apply(mw, msgs)
    assert seen.messages == msgs


def test_keeps_recent_window_untouched():
    mw = build_tool_response_truncate_middleware(
        _settings(tool_response_truncate_keep_tokens=200)
    )
    old = _turn("execute", "A" * 3000, "call_old1") + _turn("execute", "B" * 3000, "call_old2")
    recent = _turn("execute", "C" * 10, "call_new")
    seen = _apply(mw, old + recent)

    # Old responses are folded (head anchor kept), recent ones untouched.
    assert seen.messages[1].content.startswith("A" * 300)
    assert "省略" in seen.messages[1].content
    assert seen.messages[-1].content == "C" * 10


def test_head_tail_clip_preserves_tail():
    mw = build_tool_response_truncate_middleware(
        _settings(
            tool_response_truncate_max_head_chars=100,
            tool_response_truncate_max_tail_chars=40,
            tool_response_truncate_keep_tokens=100,
            tool_response_truncate_fold_enabled=False,
        )
    )
    content = "H" * 1000 + "T" * 1000
    seen = _apply(mw, _turn("execute", content))
    clipped = seen.messages[1].content
    assert clipped.startswith("H" * 100)
    assert clipped.endswith("T" * 40)


def test_tool_allowlist_only():
    mw = build_tool_response_truncate_middleware(
        _settings(tool_response_truncate_keep_tokens=100)
    )
    msgs = _turn("execute", "x" * 3000, "call_1") + _turn("write_file", "y" * 3000, "call_2")
    seen = _apply(mw, msgs)
    # execute is in the allow-list -> folded; write_file is not -> kept.
    assert "省略" in seen.messages[1].content
    assert seen.messages[3].content == "y" * 3000


def test_fold_mode_collapses_responses_with_ref():
    """Fold mode: any out-of-window allow-listed response with a reference is
    collapsed to a one-line summary regardless of size."""
    mw = build_tool_response_truncate_middleware(
        _settings(tool_response_truncate_keep_tokens=100)
    )
    content = "[tool output transformed]\nref: tool-output://call_1_abc123\n" + "z" * 3000
    seen = _apply(mw, _turn("execute", content))
    clipped = seen.messages[1].content
    assert clipped.startswith("[tool: execute] 输出约")
    assert "tool-output://call_1_abc123" in clipped
    assert "read_tool_result" in clipped


def test_fold_collapses_no_ref_with_head_anchor():
    """Without a reference the response is still folded, keeping a head anchor."""
    mw = build_tool_response_truncate_middleware(
        _settings(
            tool_response_truncate_keep_tokens=100,
            tool_response_truncate_fold_head_chars=200,
        )
    )
    content = "2026-07-30 WARN [keycloak] (thread) " + "q" * 3000
    seen = _apply(mw, _turn("execute", content))
    clipped = seen.messages[1].content
    assert clipped.startswith("2026-07-30 WARN")
    assert "重新执行 execute" in clipped
    # Head anchor is limited to fold_head_chars.
    assert len(clipped) < 400


def test_fold_keeps_small_no_ref_messages_whole():
    """Messages shorter than the head anchor have no anchor to keep; the
    clip fallback leaves them whole (they are small anyway)."""
    mw = build_tool_response_truncate_middleware(
        _settings(tool_response_truncate_keep_tokens=100)
    )
    seen = _apply(mw, _turn("execute", "short output"))
    assert seen.messages[1].content == "short output"


def test_clip_mode_when_fold_disabled():
    mw = build_tool_response_truncate_middleware(
        _settings(
            tool_response_truncate_keep_tokens=100,
            tool_response_truncate_fold_enabled=False,
        )
    )
    content = "z" * 3000 + "\nref: tool-output://call_1_abc123"
    seen = _apply(mw, _turn("execute", content))
    clipped = seen.messages[1].content
    assert clipped.startswith("z" * 500)
    assert "工具输出已裁剪" in clipped
    assert "tool-output://call_1_abc123" in clipped


def test_original_messages_are_not_mutated():
    mw = build_tool_response_truncate_middleware(_settings())
    msgs = _turn("execute", "z" * 3000)
    original = msgs[1]
    _apply(mw, msgs)
    assert original.content == "z" * 3000


def test_deterministic_across_calls():
    """Same history + same config -> byte-identical truncated request (cache prefix)."""
    mw = build_tool_response_truncate_middleware(_settings())
    msgs = _turn("execute", "d" * 3000, "call_1") + _turn("execute", "e" * 3000, "call_2")
    seen1 = _apply(mw, msgs)
    seen2 = _apply(mw, msgs)
    s1 = [m.model_dump() for m in seen1.messages]
    s2 = [m.model_dump() for m in seen2.messages]
    assert s1 == s2


def test_cache_prefix_stability_as_session_grows():
    """Clipped form of old responses must not change when new turns arrive.

    Simulates the real cache property: a response's clipped form is a stable
    function of the message itself, so only newly added messages (not the
    already-clipped history) invalidate the provider prefix cache.
    """
    mw = build_tool_response_truncate_middleware(_settings())
    history = _turn("execute", "H" * 3000, "call_old1") + _turn("execute", "H" * 3000, "call_old2")
    turn1 = history + _turn("execute", "t1", "call_t1")
    turn2 = history + _turn("execute", "t1", "call_t1") + _turn("execute", "t2", "call_t2")
    seen1 = _apply(mw, turn1)
    seen2 = _apply(mw, turn2)
    # Old clipped responses keep the identical clipped prefix across turns.
    # History layout: [AI, Tool, AI, Tool]; responses are at index 1 and 3.
    for idx in (1, 3):
        assert seen1.messages[idx].content.startswith("H" * 500)
        assert seen2.messages[idx].content.startswith("H" * 500)
