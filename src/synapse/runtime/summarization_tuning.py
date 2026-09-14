"""Tune the built-in summarization middleware for Synapse's transports.

deepagents wires ``SummarizationMiddleware`` with fraction-based thresholds
derived from the model profile (``compute_summarization_defaults``), so both the
pre-emptive trigger and the keep window are expressed as a fraction of
``max_input_tokens`` and evaluated with ``count_tokens_approximately``.

That estimator must not be the *decision* input for the keep window:

1. It charges a flat 85 tokens per image and ignores inline ``data:`` payloads.
   A provider that bills the payload as text undercounts by an order of
   magnitude — measured on the local relay at ~2.4 chars/token, where 1.79M
   base64 chars read as ~0.1M tokens instead of ~0.75M. The keep window then
   retains ~90% of the real context, so compaction barely shrinks anything.
2. It undercounts CJK text (roughly 4x).

The pre-emptive trigger is already exact: langchain consults the provider's own
``usage_metadata.total_tokens`` as soon as ``response_metadata["model_provider"]``
is present (see ``synapse.models.rust_openai``). The keep window has no such
fallback, so this module switches it to a message-count policy, which removes
estimation from the decision entirely. ``_derive_overflow_clip_threshold_tokens``
in deepagents' overflow clip accepts the message form, so the whole pipeline
stays consistent.

``trim_tokens_to_summarize`` is bounded here because deepagents passes ``None``
(meaning "summarize everything evicted"), which lets the summary request itself
exceed the model window and fail the turn.
"""

from __future__ import annotations

from typing import Any

from synapse.runtime.context_compact import _find_summarization_middleware

KEEP_MESSAGES = 20
"""Recent messages retained verbatim after compaction.

langchain's own default (``_DEFAULT_MESSAGES_TO_KEEP``). A message count is used
instead of a token fraction so the keep window does not depend on an estimator
that mis-prices inline media.
"""

SUMMARY_TRIM_TOKENS = 32_000
"""Cap on the evicted history handed to the summarizer, in estimator units.

Bounded so a summary request can never exceed the model window; the full history
is still offloaded to ``/conversation_history/<thread>.md`` before summarization.
"""


def apply_compaction_tuning(agent: Any) -> list[str]:
    """Apply Synapse's compaction policy to the built-in summarization middleware.

    Args:
        agent: The compiled deep agent returned by ``create_deep_agent``.

    Returns:
        Human-readable notes describing what was applied or skipped, for logging.
    """
    middleware = _find_summarization_middleware(agent)
    if middleware is None:
        return ["compaction tuning skipped: summarization middleware unavailable"]

    # deepagents wraps langchain's middleware in `_lc_helper`; a bare langchain
    # instance keeps the settings on itself.
    helper = getattr(middleware, "_lc_helper", None) or middleware

    helper.keep = ("messages", KEEP_MESSAGES)
    helper.trim_tokens_to_summarize = SUMMARY_TRIM_TOKENS
    return [
        f"compaction keep=('messages', {KEEP_MESSAGES})",
        f"compaction trim_tokens_to_summarize={SUMMARY_TRIM_TOKENS}",
    ]
