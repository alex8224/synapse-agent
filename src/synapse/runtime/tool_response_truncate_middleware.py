"""Deterministic tool-RESPONSE truncation middleware.

Why this exists
---------------
Large sessions resend the full history on every turn, and tool responses
(``ToolMessage`` content) are the biggest, lowest-value chunk of that history:
``execute`` logs, ``read_file`` dumps, ``search_files`` hits. The model rarely
needs every old line, only that the tool ran and what it roughly returned.
This middleware clips oversized tool responses in older messages right before
the model call, keeping the recent window intact.

Recoverability
--------------
Synapse already offloads original tool output into ``tool_outputs.sqlite`` and
puts a ``tool-output://`` reference into the visible content (see
``synapse.runtime.tool_output_middleware``). This middleware preserves those
references, so a clipped response is still recoverable via the ``read_tool_result``
tool — truncation reduces tokens without losing access to the data.

Cache-friendly design
---------------------
The provider's prefix cache requires the request token prefix to be stable.
Three properties keep this middleware cache-friendly:

1. **Deterministic transform** - ``_apply`` is a pure function of the message
   list plus the (fixed) configuration. The same history always produces the
   same truncated request, so prewarm requests and real turns share the exact
   same prefix.
2. **Fixed per-message clipping** - truncation of one message depends only on
   that message and the fixed ``max_head_chars`` / ``max_tail_chars`` config,
   never on the position of other messages.
3. **Token-counted keep window** - the keep boundary slides only by the size
   of newly added messages. Do not change the configuration mid-session: that
   would change the clipped form of every message and invalidate the whole
   prefix at once.

Request-only transform: checkpoints are never mutated; each model call
recomputes the truncation from the original history (idempotent).
"""

from __future__ import annotations

import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from synapse.settings import Settings

_DEFAULT_TRUNCATION_TEXT = "...(工具输出已裁剪，原始内容可经 read_tool_result 读取)..."
_REF_RE = re.compile(r"tool-output://[A-Za-z0-9_\-./:]+")


def _msg_tokens(msg: Any) -> int:
    try:
        return max(0, int(count_tokens_approximately([msg])))
    except Exception:  # noqa: BLE001 - counting must never break a turn
        return 0


def _clip_text(text: str, max_head: int, max_tail: int, marker: str) -> str:
    """Clip a long string deterministically.

    - ``max_tail <= 0``: keep the first ``max_head`` chars.
    - ``max_tail > 0``: keep the first ``max_head`` chars and the last
      ``max_tail`` chars, joined by ``marker`` (head+tail).
    """
    if len(text) <= max_head:
        return text
    if max_tail <= 0:
        return text[:max_head] + marker
    return text[:max_head] + marker + text[-max_tail:]


def _clip_tool_response(content: str, max_head: int, max_tail: int, marker: str) -> str:
    """Clip one ToolMessage body, preserving any ``tool-output://`` references.

    References are collected first (stable order) and appended after the
    clipped body so the model can still recover the full output via
    ``read_tool_result``. Deterministic: same content + config -> same result.
    """
    refs = sorted(set(_REF_RE.findall(content)))
    clipped = _clip_text(content, max_head, max_tail, marker)
    if refs and clipped != content:
        clipped = (
            f"{clipped}\n\n[原始输出可用 read_tool_result 读取: {' '.join(refs)}]"
        )
    return clipped


def _fold_tool_response(
    name: str, content: str, fold_head: int, marker: str
) -> str:
    """Collapse one ToolMessage into a short summary.

    - With a ``tool-output://`` reference: a one-line reference summary; the
      full output stays recoverable via ``read_tool_result``.
    - Without a reference: keep a short head anchor (timestamp/logger/module
      clues) and drop the rest; the tool can be re-run to reproduce the output.

    Deterministic: same content -> same summary. Returns ``""`` when the
    message is small enough to keep whole (caller falls back to clipping).
    """
    refs = sorted(set(_REF_RE.findall(content)))
    if refs:
        lines = max(1, content.count("\n") + 1)
        return (
            f"[tool: {name}] 输出约 {lines} 行已省略 | "
            f"完整输出可用 read_tool_result 读取: {' '.join(refs)}"
        )
    if len(content) <= fold_head:
        return ""
    return (
        content[:fold_head]
        + f"...(输出已省略，可重新执行 {name} 获取完整输出)..."
    )


def build_tool_response_truncate_middleware(settings: Settings):
    """Build the tool-response truncation middleware for the given settings.

    Disabled by default (``enable_tool_response_truncate=False``). When enabled
    the config is captured once at build time and never changes for the agent's
    lifetime, which keeps the request prefix stable for provider caching.
    """
    enabled = bool(getattr(settings, "enable_tool_response_truncate", False))
    tools = set(getattr(settings, "tool_response_truncate_tools", None) or [])
    fold_enabled = bool(getattr(settings, "tool_response_truncate_fold_enabled", True))
    fold_head = max(
        1,
        int(getattr(settings, "tool_response_truncate_fold_head_chars", 300) or 300),
    )
    max_head = max(1, int(getattr(settings, "tool_response_truncate_max_head_chars", 2000) or 2000))
    max_tail = max(0, int(getattr(settings, "tool_response_truncate_max_tail_chars", 0) or 0))
    keep_tokens = max(
        1,
        int(getattr(settings, "tool_response_truncate_keep_tokens", 40000) or 40000),
    )
    marker = _DEFAULT_TRUNCATION_TEXT

    def _keep_start_index(messages: list[Any]) -> int:
        """Index of the first message that may be truncated.

        Messages at ``index >= keep_start`` are preserved untouched. The keep
        window is counted from the tail in tokens so its edge moves only by the
        size of newly added messages.
        """
        if not messages:
            return 0
        kept = 0
        for i in range(len(messages) - 1, -1, -1):
            tokens = _msg_tokens(messages[i])
            if kept + tokens > keep_tokens:
                return i + 1
            kept += tokens
        return 0

    def _apply(request: Any) -> Any:
        messages = list(getattr(request, "messages", None) or [])
        if not enabled or not tools or not messages:
            return request
        keep_start = _keep_start_index(messages)
        out: list[Any] = []
        modified = False
        for i, msg in enumerate(messages):
            if (
                i < keep_start
                and isinstance(msg, ToolMessage)
                and isinstance(msg.content, str)
            ):
                # Resolve the tool name from the matching AIMessage if possible.
                name = _tool_name_for(msg, messages)
                if name not in tools:
                    out.append(msg)
                    continue
                if fold_enabled:
                    # Fold regardless of reference: with a ref it is fully
                    # recoverable; without one, keep a head anchor.
                    folded = _fold_tool_response(name, msg.content, fold_head, marker)
                    new_content = folded or _clip_tool_response(
                        msg.content, max_head, max_tail, marker
                    )
                else:
                    new_content = _clip_tool_response(msg.content, max_head, max_tail, marker)
                if new_content == msg.content:
                    out.append(msg)
                    continue
                new_msg = msg.model_copy()
                new_msg.content = new_content
                out.append(new_msg)
                modified = True
            else:
                out.append(msg)
        if modified:
            return request.override(messages=out)
        return request

    def wrap_model_call(self, request, handler):  # noqa: ANN001, ARG001
        return handler(_apply(request))

    async def awrap_model_call(self, request, handler):  # noqa: ANN001, ARG001
        return await handler(_apply(request))

    return type(
        "ToolResponseTruncateMiddleware",
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "wrap_model_call": wrap_model_call,
            "awrap_model_call": awrap_model_call,
        },
    )()


def _tool_name_for(tool_msg: ToolMessage, messages: list[Any]) -> str:
    """Best-effort map of a ToolMessage back to its tool name.

    Uses the nearest preceding AIMessage that made a call with the same
    ``tool_call_id``. Falls back to an empty string (not in the allow-list).
    """
    call_id = getattr(tool_msg, "tool_call_id", None)
    if not call_id:
        return ""
    idx = next((i for i, m in enumerate(messages) if m is tool_msg), None)
    if idx is None:
        return ""
    for msg in reversed(messages[:idx]):
        if isinstance(msg, AIMessage):
            for call in getattr(msg, "tool_calls", None) or []:
                cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
                if cid == call_id:
                    return call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
    return ""
