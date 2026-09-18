"""UI-independent transcript, summary, and catalog projection for one session."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from synapse.runtime.agent_loop import TurnContext, TurnResult, TurnStatus
from synapse.runtime.streaming import (
    ToolFinishedPayload,
    ToolItemPayload,
    TurnEvent,
    TurnEventKind,
)
from synapse.sessions.transcript import UiTranscriptEvent, fold_messages_for_ui
from synapse.sessions.transcript_projection import TranscriptUsage


def _latest_checkpoint_id(context: TurnContext) -> str | None:
    """Read the thread's newest checkpoint id for the projection watermark.

    Best-effort: the projection remains correct without this (a later restore
    reconciles against the checkpoint anyway); the id only avoids needless
    rebuilds on the next open.
    """
    settings = getattr(context, "settings", None)
    path = getattr(settings, "checkpoint_path", None)
    if not path or not getattr(context, "thread_id", None):
        return None
    try:
        from synapse.sessions.transcript import latest_checkpoint_id_from_sqlite_file

        return latest_checkpoint_id_from_sqlite_file(path, context.thread_id)
    except Exception:  # noqa: BLE001 - persistence must never fail on the watermark
        return None


def _request_message_metadata(request: Any) -> Any:
    """Return the frozen request's user-message ``additional_kwargs``."""
    payload = getattr(request, "payload", None)
    if not isinstance(payload, Mapping):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, (list, tuple)) or not messages:
        return None
    last = messages[-1]
    if not isinstance(last, Mapping):
        return None
    return last.get("additional_kwargs")


def _durable_attachment_refs(request: Any) -> tuple[Any, ...]:
    """Durable attachment refs for one settled turn, read from message metadata.

    The frozen request payload is the exact user message that reaches the
    checkpoint, so reading the refs here keeps the append path identical to a
    later projection rebuild (which re-derives them from the checkpoint message).
    Falls back to ``TurnRequest.attachment_refs`` for requests whose payload
    carries no metadata (legacy or directly constructed).
    """
    try:
        from synapse.content.multimodal import extract_attachment_refs

        refs = extract_attachment_refs(_request_message_metadata(request))
    except Exception:  # noqa: BLE001 - persistence must never fail on metadata
        refs = []
    if refs:
        return tuple(refs)
    return tuple(getattr(request, "attachment_refs", ()) or ())


def _schedule_subagent_checkpoint_gc(context: TurnContext) -> None:
    """Reclaim this thread's finished subagent namespaces (best-effort).

    Subagent subgraphs keep their state under ``checkpoint_ns = "tools:<id>"``
    and nothing reads it back once the turn completed, so without this sweep
    those namespaces grow without bound (measured: 83% of a 14 GB checkpoint
    store).  The sweep itself re-checks that nothing is suspended.
    """
    settings = getattr(context, "settings", None)
    path = getattr(settings, "checkpoint_path", None)
    thread_id = getattr(context, "thread_id", None)
    if not path or not thread_id:
        return
    try:
        from synapse.sessions.checkpoint_gc import schedule_subagent_checkpoint_gc

        schedule_subagent_checkpoint_gc(path, thread_id)
    except Exception:  # noqa: BLE001 - persistence must never fail on the sweep
        return


def _checkpoint_messages(context: TurnContext) -> list[Any]:
    """The thread's full message list, or ``[]`` when none can be read.

    Best-effort by design: a settlement must never fail because the checkpoint is
    unavailable (memory backend, closed agent, an unexpected saver shape).
    """
    agent = getattr(context, "agent", None)
    thread_id = getattr(context, "thread_id", None)
    get_state = getattr(agent, "get_state", None)
    if not thread_id or not callable(get_state):
        return []
    try:
        snapshot = get_state({"configurable": {"thread_id": thread_id}})
        values = getattr(snapshot, "values", None)
        messages = values.get("messages") if isinstance(values, dict) else None
    except Exception:  # noqa: BLE001 - an unreadable checkpoint degrades to the result
        return []
    return list(messages) if isinstance(messages, (list, tuple)) else []


def _turn_state_messages(context: TurnContext, result: TurnResult) -> list[Any]:
    """The settled turn's own messages, read from the checkpoint when possible.

    ``TurnResult.state`` is accumulated from the stream's per-node *updates*, so
    its ``messages`` key only holds the last node's delta: the AI message that
    requested a tool is overwritten long before the turn settles, and a projection
    built from it loses every tool row -- the calls are visible in the live stream
    and gone after a reload.  The checkpointer keeps the whole conversation, so the
    turn's slice of it (from its own user message on) is preferred; a checkpoint
    that cannot be read degrades to ``result.state`` instead of failing.
    """
    messages = _checkpoint_messages(context)
    if messages:
        from synapse.sessions.transcript import turn_start_indexes

        starts = turn_start_indexes(messages)
        if starts:
            return messages[starts[-1] :]
    return list(result.state.get("messages") or [])


@dataclass(frozen=True, slots=True)
class SessionPersistence:
    """Persist one frozen turn without consulting widgets or mutable app state."""

    transcript_projection: Any
    summary_store: Any
    project_catalog: Any | None = None
    workspace: Any | None = None
    summary_mode: str = "local"
    summary_max_chars: int = 600
    catalog_enabled: bool = True

    def persist(
        self,
        context: TurnContext,
        result: TurnResult,
        *,
        turn_events: list[TurnEvent] | None = None,
    ) -> None:
        if result.status not in {
            TurnStatus.COMPLETED,
            TurnStatus.WAITING_APPROVAL,
            TurnStatus.CANCELLED,
            TurnStatus.FAILED,
        }:
            return
        resume = bool(context.request.resume)
        user_text = "" if resume else context.request.input
        # Durable attachment metadata travels with the frozen request's user
        # message metadata (never a process-global map), so a settled turn can
        # persist the opaque ids even when the in-memory attachment objects are
        # long gone.  Reading it from the same message the checkpoint stores keeps
        # this projection identical to a later checkpoint-driven rebuild.
        attachment_refs = () if resume else _durable_attachment_refs(context.request)
        events = self._events(
            user_text,
            result,
            state_messages=_turn_state_messages(context, result),
            turn_events=turn_events,
            attachments=attachment_refs,
        )
        if resume:
            # The original user turn is already projected. Resume may still run
            # the model after approval/rejection, so persist usage only.
            events = []
        usage = TranscriptUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_tokens=result.cache_tokens,
            last_input_tokens=result.last_input_tokens,
            last_output_tokens=result.last_output_tokens,
            last_cache_tokens=result.last_cache_tokens,
        )
        self.transcript_projection.append_turn(
            context.thread_id,
            events,
            usage=usage,
            turn_id=result.turn_id,
            source_checkpoint_id=_latest_checkpoint_id(context),
        )
        if not resume:
            self._persist_summary(context.thread_id, user_text, result, events)
            self._project_catalog(context.thread_id)
        if result.status is TurnStatus.COMPLETED:
            _schedule_subagent_checkpoint_gc(context)

    @staticmethod
    def _events(
        user_text: str,
        result: TurnResult,
        *,
        state_messages: list[Any] | None = None,
        turn_events: list[TurnEvent] | None = None,
        attachments: tuple[Any, ...] = (),
    ) -> list[UiTranscriptEvent]:
        """The visible events of one settled turn.

        ``state_messages`` are the turn's own checkpoint messages when they could
        be read (see ``_turn_state_messages``); ``result.state`` is only the
        fallback, because its ``messages`` key holds the stream's last node delta.
        """
        events: list[UiTranscriptEvent] = []
        if user_text or attachments:
            events.append(
                UiTranscriptEvent(
                    kind="user",
                    text=user_text,
                    attachments=[dict(item) for item in attachments],
                    turn_id=result.turn_id,
                    elapsed_s=max(0.0, result.elapsed_s),
                )
            )
        source = (
            state_messages
            if state_messages is not None
            else list(result.state.get("messages") or [])
        )
        state_events = fold_messages_for_ui(list(source))
        # The checkpoint's own order is the order the reader watched: each step's
        # reasoning, then the batch it asked for.  Keeping only the batches and
        # prefixing the turn's aggregated reasoning put every call of the turn after
        # every thought of the turn, so a reload re-ordered the whole turn.
        ordered = [event for event in state_events if event.kind in {"thought", "tools"}]
        tool_events = [event for event in ordered if event.kind == "tools"]
        if not tool_events and turn_events:
            tool_event = _tools_from_turn_events(turn_events)
            if tool_event is not None:
                tool_events.append(tool_event)
                ordered.append(tool_event)
        if tool_events and turn_events:
            _annotate_tool_calls_with_subagent_snapshots(tool_events, turn_events)
        if result.reasoning_text and not any(
            event.kind == "thought" for event in ordered
        ):
            # A checkpoint that kept no reasoning at all: the live accumulator is
            # then the only record of it, and it belongs ahead of the batches.
            events.append(UiTranscriptEvent(kind="thought", text=result.reasoning_text))
        events.extend(ordered)
        answer_text = result.final_text or _last_answer_text(state_events)
        if answer_text:
            events.append(UiTranscriptEvent(kind="answer", text=answer_text))
        if result.changes:
            # The turn's own change list, last of all: it is the turn's outcome, and it
            # is what the console paints as its change cards (a reload included).
            # The runtime turn id travels with it so the row names the turn it belongs
            # to on its own: the console matches a later revert to that turn, and the
            # live event and the reloaded row then agree on which turn this is.
            events.append(
                UiTranscriptEvent(
                    kind="changes",
                    changes=[asdict(change) for change in result.changes],
                    changes_total=result.changes_total,
                    turn_id=result.turn_id,
                )
            )
        return events

    def _persist_summary(
        self,
        thread_id: str,
        user_text: str,
        result: TurnResult,
        events: list[UiTranscriptEvent],
    ) -> None:
        if self.summary_mode == "off" or not user_text:
            return
        from synapse.sessions.summary import persist_local_summary

        tool_count = sum(len(event.tool_calls) for event in events if event.kind == "tools")
        tool_summary = f"{tool_count} tool call(s)" if tool_count else ""
        persist_local_summary(
            self.summary_store,
            thread_id,
            user_text=user_text,
            tool_summary=tool_summary,
            answer_text=result.final_text or _last_answer_text(events),
            max_chars=self.summary_max_chars,
        )

    def _project_catalog(self, thread_id: str) -> None:
        if not self.catalog_enabled or self.project_catalog is None:
            return
        info = self.summary_store.get(thread_id)
        if info is None:
            return
        self.project_catalog.upsert_session(
            self.workspace,
            thread_id=info.thread_id,
            title=info.title,
            model=info.model or info.active_model,
            summary=info.summary,
            updated_at=info.updated_at,
            created_at=info.created_at,
            tags=info.tags,
        )


def _last_answer_text(events: list[UiTranscriptEvent]) -> str:
    for event in reversed(events):
        if event.kind == "answer" and event.text:
            return event.text
    return ""


def _tools_from_turn_events(events: list[TurnEvent]) -> UiTranscriptEvent | None:
    """Build compact restorable tools when the final graph state omits messages."""
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload
        if event.kind in {TurnEventKind.TOOL_STARTED, TurnEventKind.TOOL_UPDATED} and isinstance(
            payload, ToolItemPayload
        ):
            item_id = payload.item_id or payload.call_id or f"tool-{len(calls) + 1}"
            calls[item_id] = {
                "id": item_id,
                "name": payload.name,
                "args": _subagent_args(payload, intent=payload.label),
            }
            results[item_id] = {
                "id": item_id,
                "name": payload.name,
                "content": payload.preview or "",
                "status": payload.status,
            }
        elif event.kind is TurnEventKind.TOOL_FINISHED and isinstance(
            payload, ToolFinishedPayload
        ):
            result = results.get(payload.item_id)
            if result is not None:
                result["content"] = payload.preview or result["content"]
                result["status"] = "error" if payload.error else payload.status
    if not calls:
        return None
    return UiTranscriptEvent(
        kind="tools",
        tool_calls=list(calls.values()),
        tool_results=list(results.values()),
    )


def _subagent_args(payload: ToolItemPayload, *, intent: str) -> dict[str, Any]:
    """Persisted tool-call args including the subagent metadata snapshot.

    Restored transcripts rebuild ``ToolItem`` via ``build_tool_item``, which
    rehydrates ``subagent_name`` from ``subagent_type`` and the model/effort
    from these ``subagent_*`` keys when no live config map is available.
    """
    args: dict[str, Any] = {"intent": intent}
    if payload.subagent_name:
        args["subagent_type"] = payload.subagent_name
    if payload.subagent_model:
        args["subagent_model"] = payload.subagent_model
    if payload.subagent_reasoning_effort:
        args["subagent_reasoning_effort"] = payload.subagent_reasoning_effort
    args["subagent_model_inherited"] = payload.subagent_model_inherited
    args["subagent_reasoning_inherited"] = payload.subagent_reasoning_inherited
    return args


class RuntimeProjectPersistence:
    """Neutral per-project persistence owner for runtime-executed sessions.

    The TUI wires ``SessionPersistence`` through its own controller/app
    resources; headless/daemon sessions executed through
    :class:`synapse.runtime.consumer.LocalProjectRuntimeConsumer` or the
    runtime daemon previously had no ``persist_result`` on their
    ``RuntimeManager``, so completed turns never reached ``transcript.sqlite``
    or the session-metadata store.  This binder reuses the same neutral domain
    logic (``SessionPersistence`` + ``TranscriptProjection`` + ``SessionStore``)
    without importing any UI module, and owns exactly one project's lazy
    resources that are closed once after the owning manager settles.

    Policy:
    - resources open lazily on the first settled turn (never on construction);
    - a missing/unresolvable ``resolved_sessions_path`` or a
      ``checkpoint_backend == "memory"`` configuration disables persistence
      (no file is created and ``persist()`` is a bounded no-op);
    - ``close()`` is idempotent and safe to call from any thread.
    """

    def __init__(
        self,
        settings: Any,
        *,
        project_catalog: Any | None = None,
        workspace: Any | None = None,
    ) -> None:
        self._settings = settings
        self._project_catalog = project_catalog
        self._workspace = workspace if workspace is not None else getattr(
            settings, "workspace", None
        )
        self._store: Any = None
        self._projection: Any = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        """True when a durable project path exists and checkpoint backend is sqlite."""
        if str(getattr(self._settings, "checkpoint_backend", "sqlite") or "sqlite") == "memory":
            return False
        resolver = getattr(self._settings, "resolved_sessions_path", None)
        if not callable(resolver):
            return False
        try:
            path = resolver()
        except Exception:
            return False
        return bool(path)

    def _activate(self) -> None:
        if self._closed:
            raise RuntimeError("project persistence is closed")
        if self._store is not None:
            return
        from synapse.sessions.store import SessionStore
        from synapse.sessions.transcript_projection import (
            TranscriptProjection,
            default_transcript_projection_path,
        )

        path = self._settings.resolved_sessions_path()
        self._store = SessionStore(path)
        self._projection = TranscriptProjection(
            default_transcript_projection_path(path)
        )

    def load_usage(self, thread_id: str) -> Any | None:
        """Durable cumulative usage for one session, or ``None`` when unknown.

        A ``SessionRuntime`` keeps its own in-memory totals, and those start at
        zero for every process: without this seed, restarting the daemon silently
        resets a session's reported usage to the turns that process happened to
        run, while the TUI keeps showing the session's real total (it reads the
        same projection directly).  Seeding from here makes both agree.

        Degrades to ``None`` — never raises — because a session must still open
        when the projection is unreadable or was never written; the runtime then
        simply starts from zero, which is the pre-existing behaviour.
        """
        if not self.enabled:
            return None
        with self._lock:
            self._activate()
            projection = self._projection
        if projection is None:
            return None
        try:
            return projection.load_usage(thread_id)
        except Exception:  # noqa: BLE001 - a missing/corrupt row is just "no seed"
            return None

    def persist_result(self, context: Any, result: Any) -> None:
        """``RuntimeManager.persist_result`` binding for one settled turn."""
        if not self.enabled:
            return
        status = getattr(result, "status", None)
        allowed = {
            "completed",
            "waiting_approval",
            "cancelled",
            "failed",
        }
        raw = getattr(status, "value", None) if status is not None else None
        if raw not in allowed and str(status) not in allowed:
            return
        with self._lock:
            self._activate()
            store = self._store
            projection = self._projection
        thread_id = str(getattr(context, "thread_id", "") or "")
        if not thread_id:
            return
        resume = bool(getattr(getattr(context, "request", None), "resume", False))
        request = getattr(context, "request", None)
        user_text = "" if resume else str(getattr(request, "input", "") or "")
        settings = getattr(context, "settings", None) or self._settings
        try:
            model = str(getattr(settings, "model", "") or "") or None
            active_model = str(getattr(settings, "active_model", "") or "") or None
            thinking = str(getattr(settings, "thinking", "") or "") or None
            store.touch(
                thread_id,
                title_hint=user_text or None,
                model=model,
                active_model=active_model,
                thinking=thinking,
            )
            SessionPersistence(
                transcript_projection=projection,
                summary_store=store,
                project_catalog=self._project_catalog,
                workspace=self._workspace,
                summary_mode=str(getattr(settings, "session_summary_mode", "local")),
                summary_max_chars=int(
                    getattr(settings, "session_summary_max_chars", 600) or 600
                ),
                catalog_enabled=bool(getattr(settings, "project_catalog_enabled", True)),
            ).persist(context, result)
        except Exception:
            raise

    def close(self) -> None:
        """Idempotently close the lazily opened store and projection."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            store, projection = self._store, self._projection
            self._store = None
            self._projection = None
        first_error: BaseException | None = None
        for resource in (projection, store):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:  # noqa: BLE001 - report first close failure
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError("project persistence close failed") from first_error


def _annotate_tool_calls_with_subagent_snapshots(
    tool_events: list[UiTranscriptEvent],
    turn_events: list[TurnEvent],
) -> None:
    """Backfill subagent metadata snapshots onto persisted tool calls.

    The state-message path (``fold_messages_for_ui``) keeps the original task
    call args (``intent``/``subagent_type``) but not the resolved model/effort;
    match each call by its tool-call id against the runtime ``ToolItemPayload``
    events and copy the snapshot so history restores show the exact config used
    that turn.
    """
    by_call_id: dict[str, ToolItemPayload] = {}
    for event in turn_events or []:
        payload = event.payload
        if event.kind in {TurnEventKind.TOOL_STARTED, TurnEventKind.TOOL_UPDATED} and isinstance(
            payload, ToolItemPayload
        ):
            if payload.call_id:
                by_call_id[payload.call_id] = payload
    if not by_call_id:
        return
    for event in tool_events:
        for call in event.tool_calls or []:
            payload = by_call_id.get(str(call.get("id") or ""))
            if payload is None:
                continue
            args = call.setdefault("args", {})
            args.update(
                {
                    key: value
                    for key, value in _subagent_args(
                        payload, intent=str(args.get("intent") or payload.label or "")
                    ).items()
                }
            )
