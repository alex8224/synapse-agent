"""Producer-scoped image windows applied to the model request.

Screenshot-heavy sessions dominate the wire payload: every image block carries
its full base64 payload on every request, and a payload-billed endpoint charges
that payload as text (measured on the local relay at ~2.4 chars/token, where
1.79M base64 chars read as ~0.75M input tokens).

The standard remedy for long-running GUI agents is to prune images by
*producer*, at request-assembly time: keep the newest viewport, replace older
images with a deterministic placeholder. Three controls, each owning a different
failure mode:

``SCREENSHOT_WINDOW``
    Screenshot/GUI tool results keep only the newest N image-bearing messages.
    The current viewport is evidence; obsolete viewports are a tax.
``TOOL_IMAGE_CEILING``
    Every other tool-produced image (e.g. ``read_file`` of a mockup) shares a
    generous global ceiling.
``IMAGE_PAYLOAD_BUDGET_CHARS``
    Cumulative inline payload kept per request, so a handful of very large
    images cannot blow the window on a payload-billed endpoint.

User-uploaded images are never pruned: "compare these twelve screenshots" is a
legitimate workload that a global "keep one image" rule would destroy.

The pass is non-mutating — it only rewrites the request handed to the model, so
session state, checkpoints and the transcript keep the original bytes. It is
idempotent (a pruned message no longer carries an image block) and emits a
byte-stable placeholder, so the prompt-cache prefix survives.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

SCREENSHOT_WINDOW = 1
"""Newest screenshot-bearing messages kept verbatim."""

TOOL_IMAGE_CEILING = 50
"""Newest other tool-produced image messages kept verbatim."""

IMAGE_PAYLOAD_FRACTION = 0.35
"""Share of the model window a request's inline image payload may occupy."""

IMAGE_PAYLOAD_BUDGET_CHARS = 1_000_000
"""Fallback inline-payload budget when the model exposes no window size."""

SCREENSHOT_PLACEHOLDER = "[previous screenshot removed to save context]"
IMAGE_PLACEHOLDER = "[previous image removed to save context]"

_SCREENSHOT_TOOL_MARKERS = ("screenshot", "screen_shot", "screencap", "cdp_", "browser")
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_IMAGE_URL_KEYS = ("image_url", "url", "base64")


def _image_blocks(message: Any) -> list[Mapping[str, Any]]:
    """Return the message's image content blocks (empty for text-only messages)."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, Mapping) and str(block.get("type") or "") in _IMAGE_BLOCK_TYPES
    ]


def _block_payload_chars(block: Mapping[str, Any]) -> int:
    """Length of the inline payload a block puts on the wire."""
    value: Any = ""
    for key in _IMAGE_URL_KEYS:
        if block.get(key):
            value = block[key]
            break
    if isinstance(value, Mapping):
        value = value.get("url") or ""
    return len(str(value))


def _tool_name_index(messages: Sequence[Any]) -> dict[str, str]:
    """Map ``tool_call_id`` to tool name so results without ``name`` still classify."""
    index: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = call.get("id")
            name = call.get("name")
            if call_id and name:
                index[str(call_id)] = str(name)
    return index


def _is_screenshot_tool(name: str | None) -> bool:
    if not name:
        return False
    lowered = name.casefold()
    return any(marker in lowered for marker in _SCREENSHOT_TOOL_MARKERS)


def _image_class(message: Any, tool_names: Mapping[str, str]) -> str:
    """Classify an image-bearing message as ``user``, ``screenshot`` or ``tool``."""
    if isinstance(message, HumanMessage):
        return "user"
    name = getattr(message, "name", None)
    if not name and isinstance(message, ToolMessage):
        name = tool_names.get(str(getattr(message, "tool_call_id", "") or ""))
    return "screenshot" if _is_screenshot_tool(name) else "tool"


def _prune_message(message: Any, placeholder: str) -> Any | None:
    """Copy *message* with its image blocks replaced by *placeholder*.

    The message identity, ``tool_call_id`` and every non-image block are
    preserved so the assistant's tool call keeps a matching result.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    kept = [
        block
        for block in content
        if not (isinstance(block, Mapping) and str(block.get("type") or "") in _IMAGE_BLOCK_TYPES)
    ]
    if len(kept) == len(content):
        return None
    kept.append({"type": "text", "text": placeholder})
    pruned = message.model_copy()
    pruned.content = kept
    return pruned


def prune_request_images(
    messages: Sequence[Any],
    *,
    screenshot_window: int = SCREENSHOT_WINDOW,
    tool_ceiling: int = TOOL_IMAGE_CEILING,
    payload_budget: int = IMAGE_PAYLOAD_BUDGET_CHARS,
) -> list[Any] | None:
    """Return *messages* with out-of-budget images replaced, or ``None`` if unchanged.

    Walks newest to oldest so the most recent evidence is what survives. User
    uploads are never pruned, but their payload is reserved out of the budget so
    the request's total inline payload stays bounded either way.
    """
    tool_names = _tool_name_index(messages)
    blocks_by_index = {index: _image_blocks(m) for index, m in enumerate(messages)}
    classes = {
        index: _image_class(messages[index], tool_names)
        for index, blocks in blocks_by_index.items()
        if blocks
    }
    payloads = {
        index: sum(_block_payload_chars(block) for block in blocks)
        for index, blocks in blocks_by_index.items()
        if blocks
    }
    reserved = sum(
        payloads[index] for index, kind in classes.items() if kind == "user"
    )
    available = max(0, payload_budget - reserved)

    kept_screenshots = 0
    kept_tool_images = 0
    used_payload = 0
    replacements: dict[int, str] = {}

    for index in sorted(classes, reverse=True):
        kind = classes[index]
        if kind == "user":
            continue
        payload = payloads[index]
        if kind == "screenshot":
            within_count = kept_screenshots < screenshot_window
        else:
            within_count = kept_tool_images < tool_ceiling
        if within_count and used_payload + payload <= available:
            if kind == "screenshot":
                kept_screenshots += 1
            else:
                kept_tool_images += 1
            used_payload += payload
            continue

        replacements[index] = (
            SCREENSHOT_PLACEHOLDER if kind == "screenshot" else IMAGE_PLACEHOLDER
        )

    if not replacements:
        return None

    pruned_messages: list[Any] = []
    for index, message in enumerate(messages):
        placeholder = replacements.get(index)
        pruned = _prune_message(message, placeholder) if placeholder else None
        pruned_messages.append(pruned if pruned is not None else message)
    return pruned_messages


def image_payload_budget(model: Any) -> int:
    """Inline-payload budget for *model*, scaled by its context window when known."""
    profile = getattr(model, "profile", None)
    limit = profile.get("max_input_tokens") if isinstance(profile, Mapping) else None
    if isinstance(limit, int) and limit > 0:
        # 2 chars/token is deliberately below the ~2.4 measured on a payload-billed
        # relay, so the budget errs toward pruning.
        return max(200_000, round(limit * IMAGE_PAYLOAD_FRACTION * 2))
    return IMAGE_PAYLOAD_BUDGET_CHARS


def build_image_window_middleware(
    *,
    model: Any = None,
    screenshot_window: int = SCREENSHOT_WINDOW,
    tool_ceiling: int = TOOL_IMAGE_CEILING,
    payload_budget: int | None = None,
) -> AgentMiddleware:
    """Build the request-level image pruning middleware."""
    budget = payload_budget if payload_budget is not None else image_payload_budget(model)

    def _rewrite(request: Any) -> Any:
        messages = list(getattr(request, "messages", None) or [])
        pruned = prune_request_images(
            messages,
            screenshot_window=screenshot_window,
            tool_ceiling=tool_ceiling,
            payload_budget=budget,
        )
        if pruned is None:
            return request
        return request.override(messages=pruned)

    class _ImageWindowMiddleware(AgentMiddleware):
        state_schema = AgentState

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(_rewrite(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(_rewrite(request))

    return _ImageWindowMiddleware()
