"""In-process composition for non-UI runtime consumers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.projects.identity import ensure_project_identity
from synapse.runtime.service import (
    AgentRuntimeService,
    CancelTurnCommand,
    CloseSessionCommand,
    GetSessionQuery,
    LocalAgentRuntimeService,
    OpenSessionCommand,
    SubmitTurnCommand,
)
from synapse.runtime.service.errors import RuntimeServiceError
from synapse.runtime.service.events import RuntimeEvent
from synapse.runtime.sessions import ProjectSharedResources, RuntimeManager
from synapse.runtime.sessions.ref import SessionRef


class LocalProjectRuntimeConsumer:
    """Own one project's manager, local service, and optional catalog handle."""

    def __init__(
        self,
        *,
        settings: Any,
        project_id: str,
        agent_factory: Callable[[str, ProjectSharedResources], Any],
        catalog: ProjectCatalog | None = None,
        max_concurrent_sessions: int | None = None,
        on_status_change: Callable[[Any], None] | None = None,
    ) -> None:
        self.manager = RuntimeManager(
            settings=settings,
            project_id=project_id,
            agent_factory=agent_factory,
            max_concurrent_sessions=(
                max_concurrent_sessions
                if max_concurrent_sessions is not None
                else getattr(settings, "max_concurrent_sessions", 2)
            ),
            on_status_change=on_status_change,
        )
        self.service = LocalAgentRuntimeService(
            lambda requested_project_id: self.manager
            if requested_project_id == project_id
            else None
        )
        self._catalog = catalog
        self._close_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    async def close(self) -> None:
        """Cancel and settle manager work, then release owned catalog resources once."""
        async with self._close_lock:
            if self._cleanup_task is None:
                self._closed = True
                self._cleanup_task = asyncio.create_task(self._cleanup())
            task = self._cleanup_task
        await asyncio.shield(task)

    async def rebind_agent(self, thread_id: str, agent: Any, settings: Any) -> None:
        """Rebind one session without closing its runtime generation."""
        await self.manager.rebind_session_ref(
            SessionRef(self.manager.project_id or "", thread_id), agent, settings
        )

    async def _cleanup(self) -> None:
        first_error: BaseException | None = None
        try:
            await self.manager.shutdown()
        except BaseException as exc:
            first_error = exc
        if self._catalog is not None:
            try:
                self._catalog.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


@dataclass(frozen=True, slots=True)
class ConsumerTurnResult:
    """Pure data returned after one service-owned turn settles."""

    final_text: str
    status: str
    usage: Any
    already_streamed: bool
    turn_id: str = ""
    error_message: str = ""


class ConsumerRuntimeError(RuntimeServiceError):
    """The service stopped watching before the requested turn reached a terminal event."""

    code = "consumer_runtime_error"

    def __init__(self, message: str = "consumer event watch ended before turn completion") -> None:
        super().__init__(message)


async def _invoke_callback(
    callback: Callable[[RuntimeEvent], Any] | None, event: RuntimeEvent
) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def execute_consumer_turn(
    service: AgentRuntimeService,
    session: SessionRef,
    text: str,
    *,
    attachments: tuple[Any, ...] = (),
    on_event: Callable[[RuntimeEvent], Any] | None = None,
    open_session: bool = True,
) -> ConsumerTurnResult:
    """Execute a turn exclusively through the transport-neutral service port."""
    receipt = None
    opened = False
    try:
        after = 0
        if open_session:
            opened_result = await service.open_session(OpenSessionCommand(session))
            opened = True
            after = opened_result.view.latest_sequence
        else:
            after = (await service.get_session(GetSessionQuery(session))).latest_sequence
        watch = service.watch_events(session, after=after)
        async with watch as events:
            receipt = await service.submit_turn(
                SubmitTurnCommand(session=session, text=text, attachments=attachments)
            )
            result = await observe_receipt_turn(
                service, session, receipt, on_event=on_event, events=events
            )
        return result
    except asyncio.CancelledError:
        if opened and receipt is not None:
            try:
                await service.cancel_turn(
                    CancelTurnCommand(session, receipt.turn_id, reason="caller_cancelled")
                )
            except Exception:  # noqa: BLE001 - cancellation cleanup is best effort
                pass
        if opened or receipt is not None:
            try:
                await service.close_session(CloseSessionCommand(session, cancel_active=True))
            except Exception:  # noqa: BLE001 - cancellation cleanup is best effort
                pass
        raise
    except BaseException:
        if opened:
            try:
                await service.close_session(CloseSessionCommand(session, cancel_active=True))
            except Exception:  # noqa: BLE001 - preserve original service error
                pass
        raise


async def observe_receipt_turn(
    service: AgentRuntimeService,
    session: SessionRef,
    receipt: Any,
    *,
    on_event: Callable[[RuntimeEvent], Any] | None = None,
    after: int = 0,
    events: Any | None = None,
) -> ConsumerTurnResult:
    """Observe a receipt, optionally using an already-entered event watch."""
    deltas: list[str] = []
    final_text = ""
    usage = None
    terminal: dict[str, Any] | None = None
    displayed = False
    watch = service.watch_events(session, after=after) if events is None else None
    async with watch if watch is not None else _ExistingWatch(events) as stream:
        async for event in stream:
            if event.turn_id != receipt.turn_id:
                continue
            await _invoke_callback(on_event, event)
            payload = event.payload if isinstance(event.payload, dict) else {}
            if event.kind == "answer_delta" and isinstance(payload.get("text"), str):
                deltas.append(payload["text"])
                displayed = displayed or on_event is not None
            elif event.kind == "answer_completed" and isinstance(payload.get("text"), str):
                final_text = payload["text"]
            elif event.kind == "usage_updated":
                usage = payload
            elif event.kind in {
                "turn_completed",
                "turn_failed",
                "turn_cancelled",
                "turn_waiting_approval",
            }:
                terminal = {**payload, "status": payload.get("status", "completed")}
                if event.kind != "turn_completed":
                    terminal["status"] = event.kind.removeprefix("turn_")
                break
    if terminal is None:
        raise ConsumerRuntimeError("event watch ended before turn reached a terminal state")
    view = await service.get_session(GetSessionQuery(session))
    return ConsumerTurnResult(
        final_text or str(terminal.get("final_text", "")) or "".join(deltas),
        str(terminal.get("status", view.status)),
        usage if usage is not None else view.usage,
        displayed,
        receipt.turn_id,
        str(terminal.get("error") or getattr(view, "last_error", None) or "")[:500],
    )


class _ExistingWatch:
    """Adapt an already-entered async iterator to the observation context."""

    def __init__(self, events: Any) -> None:
        self.events = events

    async def __aenter__(self) -> Any:
        return self.events

    async def __aexit__(self, *exc: Any) -> None:
        return None
def project_identity_for_workspace(
    settings: Any, workspace: Path, *, catalog_enabled: bool = True
) -> tuple[str, ProjectCatalog | None]:
    """Resolve the durable catalog/project.json identity without process probes."""
    catalog: ProjectCatalog | None = None
    catalog_id: str | None = None
    if catalog_enabled:
        try:
            catalog = ProjectCatalog(settings.resolved_catalog_path())
            info = catalog.register_project(workspace, detect_git=False)
            catalog_id = info.project_id
        except Exception:  # noqa: BLE001 - catalog is explicitly best effort
            if catalog is not None:
                catalog.close()
            catalog = None
    try:
        return ensure_project_identity(workspace, catalog_project_id=catalog_id), catalog
    except Exception:
        if catalog is not None:
            catalog.close()
        raise
