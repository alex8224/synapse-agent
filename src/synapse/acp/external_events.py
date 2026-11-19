"""Translate peer updates into the existing turn event vocabulary."""

from __future__ import annotations

import json

from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    ContentToolCallContent,
    FileEditToolCallContent,
    TerminalToolCallContent,
    ToolCallProgress,
    Usage,
    UsageUpdate,
)
from acp.schema import ToolCallStart as AcpToolCallStart
from pydantic import BaseModel

from synapse.runtime.service.event_types import (
    EVENT_VERSION,
    PlanEntryPayload,
    PlanPayload,
    TextPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
    UsagePayload,
)
from synapse.runtime.timeline import _basename, tool_category

_MAX_TOOL_PREVIEW_CHARS = 4000

_ACP_KIND_TO_CATEGORY = {
    "read": "read",
    "edit": "edit",
    "delete": "edit",
    "move": "edit",
    "search": "search",
    "execute": "run",
}

_GENERIC_KIND_TOOL_NAMES = {
    "read": "read_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "move": "move_file",
    "search": "search",
    "execute": "execute",
    "think": "think",
    "fetch": "fetch",
}


def _resolve_tool_identity(
    update: AcpToolCallStart | ToolCallProgress,
    *,
    fallback_name: str | None = None,
    fallback_category: str | None = None,
) -> tuple[str, str]:
    raw_title = (update.title or "").strip()
    kind = (getattr(update, "kind", None) or "").strip().lower()
    if raw_title and raw_title.casefold() != "file":
        name = raw_title
    elif kind in _GENERIC_KIND_TOOL_NAMES:
        name = _GENERIC_KIND_TOOL_NAMES[kind]
    elif raw_title:
        name = raw_title
    else:
        name = fallback_name or "External tool"
    name = name[:256]
    category = _ACP_KIND_TO_CATEGORY.get(kind) or fallback_category or tool_category(name)
    return name, category


def _extract_tool_path(update: AcpToolCallStart | ToolCallProgress) -> str | None:
    for loc in getattr(update, "locations", None) or ():
        raw_path = getattr(loc, "path", None)
        if raw_path:
            return _basename(str(raw_path))
    for entry in update.content or ():
        if isinstance(entry, FileEditToolCallContent) and entry.path:
            return _basename(str(entry.path))
    return None


def _truncate_preview(text: str) -> str | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    if len(cleaned) <= _MAX_TOOL_PREVIEW_CHARS:
        return cleaned
    return cleaned[: _MAX_TOOL_PREVIEW_CHARS - 3] + "..."


def _extract_tool_preview(update: AcpToolCallStart | ToolCallProgress) -> str | None:
    parts: list[str] = []
    for entry in update.content or ():
        if isinstance(entry, ContentToolCallContent):
            block = entry.content
            if block.type == "text" and block.text:
                parts.append(block.text)
            elif block.type == "resource_link":
                parts.append(block.title or block.name or block.uri)
            elif block.type == "resource":
                res = block.resource
                text = getattr(res, "text", None)
                uri = getattr(res, "uri", None)
                if text:
                    parts.append(str(text))
                elif uri:
                    parts.append(str(uri))
            else:
                parts.append(f"[{block.type}]")
        elif isinstance(entry, FileEditToolCallContent):
            if entry.new_text:
                parts.append(f"{entry.path}\n{entry.new_text}" if entry.path else entry.new_text)
            elif entry.path:
                parts.append(str(entry.path))
        elif isinstance(entry, TerminalToolCallContent):
            parts.append(f"[terminal: {entry.terminal_id}]")
    if parts:
        return _truncate_preview("\n".join(parts))

    raw_output = update.raw_output
    if raw_output is None:
        return None
    if isinstance(raw_output, str):
        return _truncate_preview(raw_output)
    return _truncate_preview(json.dumps(raw_output, ensure_ascii=False, default=str))


class ExternalTurnEvents:
    """One ordered projection per turn; no peer paths or raw arguments are trusted."""

    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._sequence = 0
        self._tools: dict[str, str] = {}
        self._titles: dict[str, str] = {}
        self._categories: dict[str, str] = {}
        self._paths: dict[str, str | None] = {}
        self._previews: dict[str, str] = {}
        self._finished: set[str] = set()
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_tokens: int = 0
        self.context_size: int | None = None

    def project(self, update: BaseModel) -> tuple[TurnEvent, ...]:
        if isinstance(update, (AgentMessageChunk, AgentThoughtChunk)):
            if update.content.type != "text" or not update.content.text:
                return ()
            kind = (
                TurnEventKind.ANSWER_DELTA
                if isinstance(update, AgentMessageChunk)
                else TurnEventKind.REASONING_DELTA
            )
            return (self._event(kind, TextPayload(update.content.text)),)
        if isinstance(update, AgentPlanUpdate):
            entries = tuple(
                PlanEntryPayload(entry.content[:2048], entry.priority, entry.status)
                for entry in update.entries[:100]
            )
            return (self._event(TurnEventKind.PLAN_UPDATED, PlanPayload(self.turn_id, entries)),)
        if isinstance(update, UsageUpdate):
            used = max(0, int(update.used))
            size = max(0, int(update.size)) or None
            if used > self.input_tokens:
                self.input_tokens = used
            if size is not None:
                self.context_size = size
            payload = UsagePayload(
                turn_input=self.input_tokens,
                turn_output=self.output_tokens,
                turn_cache=self.cache_tokens,
                last_input=used,
                last_output=self.output_tokens,
                last_cache=self.cache_tokens,
                context_size=self.context_size,
            )
            return (self._event(TurnEventKind.USAGE_UPDATED, payload),)
        if isinstance(update, (AcpToolCallStart, ToolCallProgress)):
            return self._tool(update)
        return ()

    def project_prompt_usage(self, usage: Usage | None) -> TurnEvent | None:
        """Project final token counts from ``PromptResponse.usage`` into ``USAGE_UPDATED``."""
        if usage is None:
            return None
        inp = max(0, int(usage.input_tokens or 0))
        out = max(0, int(usage.output_tokens or 0))
        cache = max(0, int(usage.cached_read_tokens or 0)) + max(
            0, int(usage.cached_write_tokens or 0)
        )
        if inp == 0 and out == 0 and cache == 0:
            return None
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_tokens += cache
        payload = UsagePayload(
            turn_input=self.input_tokens,
            turn_output=self.output_tokens,
            turn_cache=self.cache_tokens,
            last_input=inp,
            last_output=out,
            last_cache=cache,
            context_size=self.context_size,
        )
        return self._event(TurnEventKind.USAGE_UPDATED, payload)

    def _tool(self, update: AcpToolCallStart | ToolCallProgress) -> tuple[TurnEvent, ...]:
        peer_id = update.tool_call_id
        if peer_id in self._finished:
            return ()
        events: list[TurnEvent] = []
        preview = _extract_tool_preview(update)
        if preview is not None:
            self._previews[peer_id] = preview
        extracted_path = _extract_tool_path(update)
        item_id = self._tools.get(peer_id)
        if item_id is None:
            if len(self._tools) >= 256:
                return ()
            item_id = f"acp:{self.turn_id}:{len(self._tools)}"
            self._tools[peer_id] = item_id
            name, category = _resolve_tool_identity(update)
            self._titles[peer_id] = name
            self._categories[peer_id] = category
            self._paths[peer_id] = extracted_path
            payload = ToolItemPayload(
                item_id=item_id,
                call_id=item_id,
                name=name,
                category=category,
                label=name,
                path=extracted_path,
                status="running",
                preview=preview,
                error=False,
                sub=False,
                parent_id=None,
            )
            events.append(self._event(TurnEventKind.TOOL_STARTED, payload))
        else:
            prev_name = self._titles.get(peer_id) or "External tool"
            prev_category = self._categories.get(peer_id) or "other"
            prev_path = self._paths.get(peer_id)
            name, category = _resolve_tool_identity(
                update, fallback_name=prev_name, fallback_category=prev_category
            )
            path = extracted_path or prev_path
            changed = (
                preview is not None
                or name != prev_name
                or category != prev_category
                or path != prev_path
            )
            self._titles[peer_id] = name
            self._categories[peer_id] = category
            self._paths[peer_id] = path
            if update.status not in ("completed", "failed") and changed:
                payload_update = ToolItemPayload(
                    item_id=item_id,
                    call_id=item_id,
                    name=name,
                    category=category,
                    label=name,
                    path=path,
                    status="running",
                    preview=self._previews.get(peer_id),
                    error=False,
                    sub=False,
                    parent_id=None,
                )
                events.append(self._event(TurnEventKind.TOOL_UPDATED, payload_update))
        if update.status in ("completed", "failed"):
            self._finished.add(peer_id)
            final_preview = preview or self._previews.pop(peer_id, None)
            self._titles.pop(peer_id, None)
            self._categories.pop(peer_id, None)
            self._paths.pop(peer_id, None)
            payload_done = ToolFinishedPayload(
                item_id, update.status, preview=final_preview, error=update.status == "failed"
            )
            events.append(self._event(TurnEventKind.TOOL_FINISHED, payload_done))
        return tuple(events)

    def _event(self, kind: TurnEventKind, payload: object) -> TurnEvent:
        self._sequence += 1
        return TurnEvent(EVENT_VERSION, self.thread_id, self.turn_id, self._sequence, kind, payload)

    @property
    def has_running_tools(self) -> bool:
        """True while at least one started tool has not reported completion/failure."""
        return len(self._finished) < len(self._tools)

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence
