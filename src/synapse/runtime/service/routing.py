"""Thread-safe project routing for in-process runtime managers.

The router keeps the service boundary independent from the project catalog.  A
provider resolves one exact project id to a small immutable descriptor, and a
factory builds the corresponding manager once per published project generation.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from synapse.runtime.sessions.manager import RuntimeManager

__all__ = [
    "CatalogProjectProvider",
    "ManagerFactory",
    "ProjectProvider",
    "RouterClosedError",
    "RuntimeManagerRouter",
    "RuntimeProject",
]

_MAX_PROJECT_ID_BYTES = 256
_MAX_WORKSPACE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class RuntimeProject:
    """The service-safe identity and workspace for one runtime project."""

    project_id: str
    workspace: str


ProjectProvider = Callable[[str], RuntimeProject | None]
ManagerFactory = Callable[[RuntimeProject], RuntimeManager]


class CatalogProjectProvider:
    """Adapt an exact project lookup without importing the catalog package.

    ``lookup`` may be a ``Callable[[str], row | None]`` or a catalog-like
    object exposing ``get_project(project_id=...)``.  The adapter never calls
    ``resolve_project``; names, prefixes, and paths therefore cannot become
    runtime routing keys.
    """

    def __init__(self, lookup: Callable[[str], Any | None] | Any) -> None:
        self._lookup = lookup

    def __call__(self, project_id: str) -> RuntimeProject | None:
        get_project = getattr(self._lookup, "get_project", None)
        row = (
            get_project(project_id=project_id)
            if get_project is not None
            else self._lookup(project_id)
        )
        row_project_id = getattr(row, "project_id", None) if row is not None else None
        workspace = getattr(row, "workspace_path", None) if row is not None else None
        if (
            type(row_project_id) is not str
            or row_project_id != project_id
            or type(workspace) is not str
            or not workspace
            or "\x00" in workspace
        ):
            return None
        return RuntimeProject(project_id=row_project_id, workspace=workspace)


class RouterClosedError(RuntimeError):
    """The runtime manager router has entered shutdown and rejects new work."""


@dataclass(slots=True)
class _BuildState:
    done: threading.Event = field(default_factory=threading.Event)
    manager: RuntimeManager | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _ShutdownState:
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


def _bounded_text(value: object, *, max_bytes: int, allow_empty: bool) -> bool:
    """Validate text without putting the value in an error message."""
    if type(value) is not str:
        return False
    if not allow_empty and not value:
        return False
    if "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


class RuntimeManagerRouter:
    """Resolve immutable project generations to independently built managers.

    ``__call__`` is intentionally synchronous: it is compatible with the
    existing ``LocalAgentRuntimeService`` provider and can be used by callers
    running outside an asyncio loop.  Builds use a per-project event, so a
    blocked build for one project never serializes a different project.

    Invalid project ids and malformed provider descriptors are treated as an
    unknown project and return ``None``.  Once shutdown starts, calls raise
    :class:`RouterClosedError`; the local service maps that typed boundary to
    its stable ``closed`` error.
    """

    def __init__(
        self,
        project_provider: ProjectProvider,
        manager_factory: ManagerFactory,
    ) -> None:
        self._project_provider = project_provider
        self._manager_factory = manager_factory
        self._lock = threading.RLock()
        self._published: dict[str, RuntimeManager] = {}
        # A factory can return a real manager which fails the project identity
        # check.  The synchronous resolver cannot close it, so the router owns
        # it until the shared shutdown completes.  Keying by identity also
        # prevents a manager returned for two rejected generations from being
        # closed twice.
        self._rejected: dict[int, RuntimeManager] = {}
        self._inflight: dict[str, _BuildState] = {}
        self._closing = False
        self._closed = False
        self._shutdown: _ShutdownState | None = None

    def __call__(self, project_id: str) -> RuntimeManager | None:
        """Return the immutable manager generation for one exact project id."""
        with self._lock:
            self._raise_if_closed_locked()
        if not _bounded_text(
            project_id, max_bytes=_MAX_PROJECT_ID_BYTES, allow_empty=False
        ):
            return None
        with self._lock:
            self._raise_if_closed_locked()
            manager = self._published.get(project_id)
            if manager is not None:
                return manager
            state = self._inflight.get(project_id)
            leader = state is None
            if leader:
                state = _BuildState()
                self._inflight[project_id] = state

        if leader:
            return self._build_one(project_id, state)

        # Provider and factory are synchronous by contract.  Waiting here is
        # deliberate: this path is also used by non-async callers and the
        # event has no affinity to an asyncio loop.
        assert state is not None
        state.done.wait()
        with self._lock:
            error = state.error
            manager = state.manager
            closing = self._closing
        if error is not None:
            raise error
        if closing:
            raise RouterClosedError("runtime manager router is closed")
        return manager

    def _build_one(self, project_id: str, state: _BuildState) -> RuntimeManager | None:
        try:
            descriptor = self._project_provider(project_id)
            if descriptor is None:
                result = None
            elif not self._valid_descriptor(descriptor, project_id):
                result = None
            else:
                manager = self._manager_factory(descriptor)
                if not isinstance(manager, RuntimeManager):
                    raise RuntimeError("runtime manager factory returned an invalid object")
                if manager.project_id is None or manager.project_id != project_id:
                    with self._lock:
                        self._rejected[id(manager)] = manager
                    raise RuntimeError("runtime manager factory returned a mismatched project")
                result = manager
        except BaseException as exc:
            with self._lock:
                state.error = exc
                # Existing followers retain this state object and observe the
                # same exception; a later caller is free to start a retry.
                if self._inflight.get(project_id) is state:
                    del self._inflight[project_id]
                state.done.set()
            raise

        with self._lock:
            state.manager = result
            if result is not None:
                # Published generations are never replaced or evicted before
                # router shutdown, even if the catalog changes underneath us.
                self._published[project_id] = result
            if self._inflight.get(project_id) is state:
                del self._inflight[project_id]
            state.done.set()
            closing = self._closing
        if closing:
            raise RouterClosedError("runtime manager router is closed")
        return result

    @staticmethod
    def _valid_descriptor(descriptor: RuntimeProject, project_id: str) -> bool:
        return (
            isinstance(descriptor, RuntimeProject)
            and type(descriptor.project_id) is str
            and descriptor.project_id == project_id
            and _bounded_text(
                descriptor.project_id,
                max_bytes=_MAX_PROJECT_ID_BYTES,
                allow_empty=False,
            )
            and _bounded_text(
                descriptor.workspace,
                max_bytes=_MAX_WORKSPACE_BYTES,
                allow_empty=False,
            )
        )

    def _raise_if_closed_locked(self) -> None:
        if self._closing or self._closed:
            raise RouterClosedError("runtime manager router is closed")

    @property
    def project_ids(self) -> tuple[str, ...]:
        """A thread-safe immutable snapshot of published project ids."""
        with self._lock:
            return tuple(sorted(self._published))

    @property
    def manager_count(self) -> int:
        """Number of currently published manager generations."""
        with self._lock:
            return len(self._published)

    async def shutdown(self) -> None:
        """Close every published manager exactly once and join concurrent calls.

        The internal shutdown work is deliberately detached from the caller's
        cancellation.  A cancelled caller may return with ``CancelledError``;
        the router continues waiting for builds and closing all managers, and a
        later caller joins the same shutdown state.
        """
        with self._lock:
            state = self._shutdown
            starter = state is None
            if starter:
                state = _ShutdownState()
                self._shutdown = state
                self._closing = True
                builds = tuple(self._inflight.values())
            else:
                builds = ()
        assert state is not None
        if starter:
            asyncio.create_task(self._finish_shutdown(state, builds))

        await asyncio.shield(asyncio.to_thread(state.done.wait))
        with self._lock:
            error = state.error
        if error is not None:
            raise error

    async def _finish_shutdown(
        self, state: _ShutdownState, builds: tuple[_BuildState, ...]
    ) -> None:
        first_error: BaseException | None = None
        try:
            if builds:
                try:
                    await asyncio.gather(
                        *(asyncio.to_thread(build.done.wait) for build in builds),
                    )
                except BaseException as exc:
                    # Keep the shutdown task alive long enough to close every
                    # manager already published by the build barrier.  This
                    # is only an internal coordination failure; build errors
                    # remain on their individual single-flight states.
                    first_error = exc
            with self._lock:
                # Builds were snapshotted before closing and cannot be created
                # after ``_closing`` is set.  Therefore this snapshot includes
                # every manager, including rejected factory results, once all
                # snapshotted builds have published or failed.
                managers_by_identity: dict[int, RuntimeManager] = {}
                for manager in self._published.values():
                    managers_by_identity[id(manager)] = manager
                managers_by_identity.update(self._rejected)
                managers = tuple(managers_by_identity.values())
            try:
                results = await asyncio.gather(
                    *(self._close_one(manager) for manager in managers),
                )
            except BaseException as exc:
                # _close_one isolates manager failures.  Reaching this branch
                # means the fanout machinery itself failed, but the finally
                # block still only marks the shared state done after this
                # close attempt has completed.
                results = ()
                if first_error is None:
                    first_error = exc
            if first_error is None:
                for result in results:
                    if isinstance(result, BaseException):
                        first_error = result
                        break
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        finally:
            state.error = first_error
            with self._lock:
                self._closed = True
                self._published.clear()
                self._rejected.clear()
                self._inflight.clear()
            state.done.set()

    @staticmethod
    async def _close_one(manager: RuntimeManager) -> BaseException | None:
        """Attempt one manager without allowing it to abort sibling closes."""
        try:
            await manager.shutdown()
        except BaseException as exc:
            # Shutdown is a best-effort fan-out boundary: one manager failure
            # must not prevent every other owned manager from being attempted.
            return exc
        return None
