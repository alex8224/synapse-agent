"""Tests for compaction tuning of the built-in summarization middleware."""

from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from synapse.models.rust_openai import RustOpenAIChatModel
from synapse.runtime.summarization_tuning import (
    KEEP_MESSAGES,
    SUMMARY_TRIM_TOKENS,
    apply_compaction_tuning,
)


def _agent_exposing(middleware: object) -> SimpleNamespace:
    """Minimal compiled-agent shape that `_find_summarization_middleware` walks."""
    node = SimpleNamespace(bound=SimpleNamespace(middleware=[middleware]))
    return SimpleNamespace(nodes={"model": node})


def _middleware() -> SummarizationMiddleware:
    model = RustOpenAIChatModel(model="gemini-3.8-flash-high")
    model.profile = {"max_input_tokens": 1_000_000}
    return SummarizationMiddleware(
        model=model,
        trigger=("fraction", 0.85),
        keep=("fraction", 0.10),
    )


def test_apply_compaction_tuning_sets_keep_and_trim() -> None:
    middleware = _middleware()

    notes = apply_compaction_tuning(_agent_exposing(middleware))

    assert middleware.keep == ("messages", KEEP_MESSAGES)
    assert middleware.trim_tokens_to_summarize == SUMMARY_TRIM_TOKENS
    assert any("keep=" in note for note in notes)
    assert any("trim_tokens_to_summarize=" in note for note in notes)


def test_apply_compaction_tuning_targets_deepagents_wrapper_helper() -> None:
    """The production middleware keeps its settings on the wrapped helper."""
    helper = SimpleNamespace(keep=("fraction", 0.10), trim_tokens_to_summarize=None)
    middleware = SimpleNamespace(name="SummarizationMiddleware", _lc_helper=helper)

    notes = apply_compaction_tuning(_agent_exposing(middleware))

    assert helper.keep == ("messages", KEEP_MESSAGES)
    assert helper.trim_tokens_to_summarize == SUMMARY_TRIM_TOKENS
    assert any("keep=" in note for note in notes)


def test_apply_compaction_tuning_reports_missing_middleware() -> None:
    notes = apply_compaction_tuning(SimpleNamespace(nodes={}))

    assert len(notes) == 1
    assert "skipped" in notes[0]


def _screenshot_heavy_history(count: int) -> list[object]:
    """History whose token cost the estimator cannot see.

    ``count_tokens_approximately`` charges a flat 85 tokens per image and skips
    the base64 payload, so a fraction-based keep window sees a tiny context and
    keeps almost everything.
    """
    payload = "A" * 20_000
    messages: list[object] = []
    for i in range(count):
        messages.append(
            HumanMessage(
                content=[
                    {"type": "text", "text": f"step {i}"},
                    {"type": "image", "base64": payload, "mime_type": "image/png"},
                ]
            )
        )
        messages.append(AIMessage(content=f"ok {i}"))
    return messages


def test_keep_window_stops_depending_on_the_token_estimator() -> None:
    middleware = _middleware()
    messages = _screenshot_heavy_history(20)

    # Fraction-based keep: the estimator sees only a few thousand tokens, so the
    # keep window covers the whole history and compaction does nothing.
    assert middleware._determine_cutoff_index(messages) == 0

    apply_compaction_tuning(_agent_exposing(middleware))

    cutoff = middleware._determine_cutoff_index(messages)
    assert cutoff == len(messages) - KEEP_MESSAGES
