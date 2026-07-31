"""In-process monitor for DAG subagent runs.

The TUI cannot rely only on LangGraph subgraph streaming for DAG subagents:
they run inside the model middleware before the parent ``task`` ToolMessage is
painted. This module gives the DAG scheduler a small, UI-neutral place to
publish live subagent state.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

MONITOR_CONFIG_KEY = "subagent_monitor_id"

_REGISTRY_LOCK = threading.RLock()
_REGISTRY: dict[str, SubagentMonitor] = {}


def get_subagent_monitor(monitor_id: str | None) -> SubagentMonitor | None:
    if not monitor_id:
        return None
    with _REGISTRY_LOCK:
        return _REGISTRY.get(str(monitor_id))


def register_subagent_monitor(monitor: SubagentMonitor) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY[monitor.monitor_id] = monitor


def unregister_subagent_monitor(monitor_id: str | None) -> None:
    if not monitor_id:
        return
    with _REGISTRY_LOCK:
        _REGISTRY.pop(str(monitor_id), None)


@dataclass
class SubagentEvent:
    kind: str
    title: str
    body: str = ""
    status: str = "ok"
    elapsed_s: float = 0.0
    event_id: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class SubagentRun:
    call_id: str
    task_id: str
    subagent_type: str
    description: str
    status: str = "running"
    wave: int | None = None
    depends_on: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    events: list[SubagentEvent] = field(default_factory=list)

    @property
    def elapsed_s(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)


class SubagentMonitor:
    """Thread-safe live state for one TUI session."""

    def __init__(self) -> None:
        self.monitor_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._runs: dict[str, SubagentRun] = {}
        self._order: list[str] = []
        self._revision = 0
        register_subagent_monitor(self)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def reset(self) -> None:
        with self._lock:
            self._runs.clear()
            self._order.clear()
            self._revision += 1

    def snapshot(self) -> tuple[int, list[SubagentRun]]:
        with self._lock:
            runs = [
                SubagentRun(
                    call_id=run.call_id,
                    task_id=run.task_id,
                    subagent_type=run.subagent_type,
                    description=run.description,
                    status=run.status,
                    wave=run.wave,
                    depends_on=list(run.depends_on),
                    started_at=run.started_at,
                    ended_at=run.ended_at,
                    events=list(run.events),
                )
                for key in self._order
                if (run := self._runs.get(key)) is not None
            ]
            return self._revision, runs

    def start_task(
        self,
        *,
        call_id: str,
        task_id: str,
        subagent_type: str,
        description: str,
        wave: int | None = None,
        depends_on: list[str] | None = None,
        status: str = "running",
    ) -> None:
        key = call_id or task_id
        if not key:
            return
        next_status = status if status in {"pending", "running"} else "running"
        with self._lock:
            run = self._runs.get(key)
            if run is None:
                run = SubagentRun(
                    call_id=call_id,
                    task_id=task_id,
                    subagent_type=subagent_type,
                    description=description,
                    status=next_status,
                    wave=wave,
                    depends_on=list(depends_on or []),
                )
                self._runs[key] = run
                self._order.append(key)
            else:
                if run.status == "pending" and next_status == "running":
                    run.started_at = time.time()
                run.status = next_status
                run.wave = wave
                run.depends_on = list(depends_on or [])
            self._revision += 1

    def finish_task(self, call_id: str, output: str, *, error: bool = False) -> None:
        key = call_id
        with self._lock:
            run = self._runs.get(key)
            if run is None:
                return
            run.status = "error" if error else "ok"
            run.ended_at = time.time()
            run.events.append(
                SubagentEvent(
                    kind="answer",
                    title="Final response",
                    body=output,
                    status=run.status,
                    elapsed_s=run.elapsed_s,
                )
            )
            self._revision += 1

    def add_event(
        self,
        call_id: str,
        *,
        kind: str,
        title: str,
        body: str = "",
        status: str = "ok",
        event_id: str = "",
    ) -> None:
        with self._lock:
            run = self._runs.get(call_id)
            if run is None:
                return
            if event_id:
                for event in run.events:
                    if event.kind == kind and event.event_id == event_id:
                        event.title = title
                        event.body = body
                        event.status = status
                        event.timestamp = time.time()
                        self._revision += 1
                        return
            run.events.append(
                SubagentEvent(
                    kind=kind,
                    title=title,
                    body=body,
                    status=status,
                    event_id=event_id,
                )
            )
            self._revision += 1

    def extend_events(self, call_id: str, events: list[SubagentEvent]) -> None:
        if not events:
            return
        with self._lock:
            run = self._runs.get(call_id)
            if run is None:
                return
            existing = {
                (event.kind, event.title, event.body, event.status)
                for event in run.events
            }
            existing_by_id = {
                (event.kind, event.event_id): event
                for event in run.events
                if event.event_id
            }
            changed = False
            for event in events:
                if event.event_id:
                    current = existing_by_id.get((event.kind, event.event_id))
                    if current is not None:
                        if (
                            current.title != event.title
                            or current.body != event.body
                            or current.status != event.status
                        ):
                            current.title = event.title
                            current.body = event.body
                            current.status = event.status
                            current.timestamp = time.time()
                            changed = True
                        continue
                    existing_by_id[(event.kind, event.event_id)] = event
                key = (event.kind, event.title, event.body, event.status)
                if key in existing:
                    continue
                run.events.append(event)
                existing.add(key)
                changed = True
            if changed:
                self._revision += 1


def monitor_from_config(config: Mapping[str, Any] | None) -> SubagentMonitor | None:
    configurable = (config or {}).get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get(MONITOR_CONFIG_KEY)
    return get_subagent_monitor(str(value)) if value else None


def _short_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _extract_tool_args(input_str: str, kwargs: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = kwargs.get("inputs")
    if isinstance(inputs, Mapping):
        return inputs
    if not input_str:
        return {}
    try:
        parsed = json.loads(input_str)
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _tool_intent(args: Mapping[str, Any]) -> str:
    for key in ("intent", "purpose", "reason"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _tool_title(name: str, args: Mapping[str, Any]) -> str:
    intent = _tool_intent(args)
    return f"{name} · {intent}" if intent else name


def _message_tool_calls(message: Any) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None) or []
    return [call for call in calls if isinstance(call, dict)]


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "") or ""
    if isinstance(content, str):
        return _short_text(content, limit=1600)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return _short_text("".join(parts), limit=1600)
    return _short_text(content, limit=1600)


def _message_type(message: Any) -> str:
    return str(
        getattr(message, "type", "")
        or getattr(message, "__class__", type(message)).__name__
    ).lower()


def _messages_from_output(output: Any) -> list[Any]:
    if output is None:
        return []
    if isinstance(output, Mapping):
        messages = output.get("messages")
        if isinstance(messages, list):
            return list(messages)
        return []
    messages = _llm_result_messages(output)
    if messages:
        return messages
    if _message_type(output) in {"ai", "aimessage", "tool", "toolmessage"}:
        return [output]
    if hasattr(output, "tool_calls") or hasattr(output, "content"):
        return [output]
    return []


def events_from_messages(
    messages: list[Any],
    *,
    tool_titles: dict[str, str] | None = None,
    tool_title_queue: dict[str, list[tuple[str, str]]] | None = None,
) -> list[SubagentEvent]:
    """Build monitor events from a completed subagent message history."""
    events: list[SubagentEvent] = []
    titles = tool_titles if tool_titles is not None else {}
    title_queue = tool_title_queue if tool_title_queue is not None else {}

    def upsert(event: SubagentEvent) -> None:
        if event.event_id:
            for existing in events:
                if existing.kind == event.kind and existing.event_id == event.event_id:
                    existing.title = event.title
                    existing.body = event.body
                    existing.status = event.status
                    existing.timestamp = event.timestamp
                    return
        events.append(event)

    for message in messages or []:
        calls = _message_tool_calls(message)
        text = _message_text(message)
        msg_type = _message_type(message)
        if calls:
            if text:
                events.append(
                    SubagentEvent(
                        kind="model",
                        title="Model thought",
                        body=text,
                        status="ok",
                    )
                )
            for call in calls:
                name = str(call.get("name") or "tool")
                args = call.get("args") or {}
                if not isinstance(args, Mapping):
                    args = {}
                title = _tool_title(name, args)
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                event_id = call_id or f"{name}:{len(events)}:{title}"
                if call_id:
                    titles[call_id] = title
                title_queue.setdefault(name, []).append((title, event_id))
                upsert(
                    SubagentEvent(
                        kind="tool",
                        title=title,
                        body="",
                        status="running",
                        event_id=event_id,
                    )
                )
            continue
        if msg_type in {"tool", "toolmessage"}:
            tool_call_id = str(getattr(message, "tool_call_id", "") or "")
            name = str(getattr(message, "name", "") or "tool")
            title = titles.get(tool_call_id, name)
            event_id = tool_call_id or f"{name}:{title}"
            upsert(
                SubagentEvent(
                    kind="tool",
                    title=title,
                    body="",
                    status="ok",
                    event_id=event_id,
                )
            )
            continue
    return events


def _llm_result_messages(response: Any) -> list[Any]:
    messages: list[Any] = []
    for generation_group in getattr(response, "generations", None) or []:
        if not isinstance(generation_group, list):
            continue
        for generation in generation_group:
            message = getattr(generation, "message", None)
            if message is not None:
                messages.append(message)
    return messages


class SubagentStreamEventRecorder:
    """Record live subagent stream events into the monitor."""

    def __init__(self, monitor: SubagentMonitor, call_id: str) -> None:
        self._monitor = monitor
        self._call_id = call_id
        self._tool_titles: dict[str, str] = {}
        self._tool_title_queue: dict[str, list[tuple[str, str]]] = {}
        self._tool_event_ids: dict[str, str] = {}
        self.final_output: Any = None

    def record(self, event: Mapping[str, Any]) -> None:
        event_name = str(event.get("event") or "")
        data = event.get("data") or {}
        if not isinstance(data, Mapping):
            data = {}
        if event_name in {"on_chat_model_end", "on_llm_end"}:
            self._record_messages(_messages_from_output(data.get("output")))
            return
        if event_name == "on_tool_start":
            self._record_tool_start(event, data)
            return
        if event_name == "on_tool_end":
            self._record_tool_end(event, data)
            return
        if event_name == "on_tool_error":
            self._record_tool_error(event, data)
            return
        if event_name == "on_chain_end":
            output = data.get("output")
            if output is not None:
                self.final_output = output

    def _record_messages(self, messages: list[Any]) -> None:
        self._monitor.extend_events(
            self._call_id,
            events_from_messages(
                messages,
                tool_titles=self._tool_titles,
                tool_title_queue=self._tool_title_queue,
            ),
        )

    def _record_tool_start(
        self,
        event: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> None:
        name = str(event.get("name") or "tool")
        run_id = str(event.get("run_id") or "")
        queued_titles = self._tool_title_queue.get(name) or []
        planned_title = ""
        planned_event_id = ""
        if queued_titles:
            planned_title, planned_event_id = queued_titles.pop(0)
        if planned_title:
            if run_id:
                self._tool_titles[run_id] = planned_title
                self._tool_event_ids[run_id] = planned_event_id
            return
        raw_input = data.get("input")
        args = raw_input if isinstance(raw_input, Mapping) else {}
        title = _tool_title(name, args)
        event_id = run_id or f"{name}:{title}"
        if run_id:
            self._tool_titles[run_id] = title
            self._tool_event_ids[run_id] = event_id
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="running",
            event_id=event_id,
        )

    def _record_tool_end(
        self,
        event: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> None:
        run_id = str(event.get("run_id") or "")
        name = str(event.get("name") or "tool")
        title = self._tool_titles.pop(run_id, name) if run_id else name
        event_id = (
            self._tool_event_ids.pop(run_id, run_id)
            if run_id
            else f"{name}:{title}"
        )
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="ok",
            event_id=event_id,
        )

    def _record_tool_error(
        self,
        event: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> None:
        run_id = str(event.get("run_id") or "")
        name = str(event.get("name") or "tool")
        title = self._tool_titles.pop(run_id, name) if run_id else name
        event_id = (
            self._tool_event_ids.pop(run_id, run_id)
            if run_id
            else f"{name}:{title}"
        )
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="error",
            event_id=event_id,
        )


class SubagentMonitorCallback(BaseCallbackHandler):
    """LangChain callback handler that mirrors child tool/model events."""

    run_inline = True

    def __init__(self, monitor: SubagentMonitor, call_id: str) -> None:
        super().__init__()
        self._monitor = monitor
        self._call_id = call_id
        self._tool_titles: dict[str, str] = {}
        self._tool_title_queue: dict[str, list[tuple[str, str]]] = {}
        self._tool_event_ids: dict[str, str] = {}

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        self._monitor.add_event(
            self._call_id,
            kind="thought",
            title="Model call",
            body="waiting for model",
            status="running",
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for message in _llm_result_messages(response):
            for call in _message_tool_calls(message):
                name = str(call.get("name") or "tool")
                args = call.get("args") or {}
                if not isinstance(args, Mapping):
                    args = {}
                title = _tool_title(name, args)
                call_id = str(call.get("id") or call.get("tool_call_id") or "")
                queued = self._tool_title_queue.get(name) or []
                event_id = call_id or f"{name}:{len(queued)}:{title}"
                if call_id:
                    self._tool_titles[call_id] = title
                self._tool_title_queue.setdefault(name, []).append((title, event_id))
                self._monitor.add_event(
                    self._call_id,
                    kind="tool",
                    title=title,
                    body="",
                    status="running",
                    event_id=event_id,
                )

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        name = str(
            kwargs.get("name")
            or serialized.get("name")
            or serialized.get("id")
            or "tool"
        )
        explicit_id = str(kwargs.get("tool_call_id") or "")
        queued_titles = self._tool_title_queue.get(name) or []
        run_id = str(kwargs.get("run_id") or kwargs.get("tool_call_id") or "")
        queued_title = ""
        queued_event_id = ""
        if queued_titles:
            queued_title, queued_event_id = queued_titles.pop(0)
        planned_title = self._tool_titles.get(explicit_id) or queued_title
        planned_event_id = explicit_id or queued_event_id
        if not planned_event_id and planned_title:
            planned_event_id = f"{name}:{planned_title}"
        if planned_title:
            if run_id:
                self._tool_titles[run_id] = planned_title
                self._tool_event_ids[run_id] = planned_event_id
            return
        args = _extract_tool_args(input_str, kwargs)
        title = _tool_title(name, args)
        event_id = run_id or explicit_id or f"{name}:{title}"
        if run_id:
            self._tool_titles[run_id] = title
            self._tool_event_ids[run_id] = event_id
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="running",
            event_id=event_id,
        )

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id") or kwargs.get("tool_call_id") or "")
        name = str(kwargs.get("name") or "tool")
        title = self._tool_titles.pop(run_id, name) if run_id else name
        event_id = (
            self._tool_event_ids.pop(run_id, run_id)
            if run_id
            else f"{name}:{title}"
        )
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="ok",
            event_id=event_id,
        )

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id") or kwargs.get("tool_call_id") or "")
        name = str(kwargs.get("name") or "tool")
        title = self._tool_titles.pop(run_id, name) if run_id else name
        event_id = (
            self._tool_event_ids.pop(run_id, run_id)
            if run_id
            else f"{name}:{title}"
        )
        self._monitor.add_event(
            self._call_id,
            kind="tool",
            title=title,
            body="",
            status="error",
            event_id=event_id,
        )
