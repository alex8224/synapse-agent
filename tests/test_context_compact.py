"""Tests for context-compaction detection (SESSION INTENT / SUMMARY)."""

from __future__ import annotations

from synapse.runtime.context_compact import (
    _find_summarization_middleware,
    force_compact_via_agent,
    is_context_compact_text,
    is_lc_summarization_message,
    is_stream_meta_summarization,
)
from synapse.sessions.transcript import fold_messages_for_ui


def test_detect_session_intent_summary_block():
    body = """## SESSION INTENT

用户目标是：
1 理解 Claude recap

## SUMMARY

Claude Code recap 结论

## ARTIFACTS

None

## NEXT STEPS

实现 session_recap
"""
    assert is_context_compact_text(body)
    assert not is_context_compact_text("结论：已修好登录。")


def test_detect_wrapper_and_meta():
    assert is_context_compact_text(
        "Here is a summary of the conversation to date:\n\n## SESSION INTENT\nfoo\n## SUMMARY\nbar"
    )
    assert is_stream_meta_summarization({"metadata": {"lc_source": "summarization"}})

    class _Msg:
        additional_kwargs = {"lc_source": "summarization"}
        content = "x"

    assert is_lc_summarization_message(_Msg())


def test_force_compact_uses_agent_async_runtime():
    """Async runtime path triggers SummarizationMiddleware and restores state."""
    from types import SimpleNamespace

    calls: list[str] = []

    middleware = SimpleNamespace(
        name="SummarizationMiddleware",
        _should_summarize=lambda _messages, _tokens: False,
    )
    model_node = SimpleNamespace(bound=SimpleNamespace(middleware=[middleware]))

    class Runtime:
        def run(self, awaitable):
            calls.append("run")
            awaitable.close()
            return {"_summarization_event": {"cutoff": 5}}

    class Agent:
        _coding_async_runtime = Runtime()
        nodes = {"model": model_node}

        async def ainvoke(self, payload, config):
            return {"_summarization_event": {"cutoff": 5}}

        def invoke(self, payload, config):
            raise AssertionError("sync invoke must not be used")

    ok, lines = force_compact_via_agent(Agent(), thread_id="t1")

    assert ok is True
    assert calls == ["run"]
    # _should_summarize must be restored after the call, regardless of success.
    assert middleware._should_summarize([], 0) is False
    assert any("compacted" in line for line in lines)


def test_find_summarization_middleware_in_current_compiled_graph():
    """LangChain 1.3 stores model-call middleware in a compiled closure."""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    agent = create_deep_agent(model=FakeListChatModel(responses=["ok"]))

    middleware = _find_summarization_middleware(agent)

    assert middleware is not None
    assert middleware.name == "SummarizationMiddleware"
    assert callable(middleware._should_summarize)


def test_fold_hides_compact_messages():
    class Human:
        type = "human"
        content = (
            "Here is a summary of the conversation to date:\n\n"
            "## SESSION INTENT\ngoal\n## SUMMARY\nnotes"
        )
        additional_kwargs = {"lc_source": "summarization"}

    class AI:
        type = "ai"
        content = "## SESSION INTENT\ngoal\n\n## SUMMARY\nnotes"
        additional_kwargs = {}
        tool_calls = []

    class RealAI:
        type = "ai"
        content = "已完成 session recap 接入。"
        additional_kwargs = {}
        tool_calls = []

    events = fold_messages_for_ui([Human(), AI(), RealAI()])
    kinds = [e.kind for e in events]
    assert "user" not in kinds
    answers = [e.text for e in events if e.kind == "answer"]
    assert answers == ["已完成 session recap 接入。"]