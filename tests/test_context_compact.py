"""Tests for context-compaction detection (SESSION INTENT / SUMMARY)."""

from __future__ import annotations

from synapse.context_compact import (
    is_context_compact_text,
    is_lc_summarization_message,
    is_stream_meta_summarization,
)
from synapse.transcript import fold_messages_for_ui


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
