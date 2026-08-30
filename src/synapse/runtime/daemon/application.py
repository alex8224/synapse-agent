"""S8 daemon composition root and ordered lifecycle owner."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import signal
from collections.abc import Callable
from typing import Any

from synapse.app.agent import build_coding_agent
from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.daemon.auth import BearerTokenAuthenticator, load_token
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.daemon.lease import DaemonLease
from synapse.runtime.service import (
    CatalogProjectProvider,
    DaemonAuthorizer,
    LocalAgentRuntimeService,
    Principal,
    RuntimeManagerRouter,
    bind_access,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.transport import RuntimeWebSocketServer
from synapse.settings import load_global_settings, load_project_settings


class RuntimeDaemon:
    """Own server, router, catalog, and lease in one reverse-close chain."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        stop_event: asyncio.Event | None = None,
        server_factory: Callable[..., Any] = RuntimeWebSocketServer,
        manager_factory: Callable[[Any], RuntimeManager] | None = None,
        service_factory: Callable[[Any], Any] | None = None,
        settings_factory: Callable[[], Any] | None = None,
        catalog_factory: Callable[[Any], Any] = ProjectCatalog,
        router_factory: Callable[[Any, Callable[[Any], RuntimeManager]], Any]
        | None = None,
        lease_factory: Callable[[Any], Any] | None = None,
        token_loader: Callable[[Any], str] | None = None,
        signal_installer: Callable[[asyncio.Event], Callable[[], None]] | None = None,
        stdout: Any | None = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event if stop_event is not None else asyncio.Event()
        self._server_factory = server_factory
        self._manager_factory_override = manager_factory
        self._service_factory_override = service_factory
        self._settings_factory = settings_factory
        self._catalog_factory = catalog_factory
        self._router_factory = router_factory
        self._lease_factory = lease_factory
        self._token_loader = token_loader
        self._signal_installer = signal_installer
        self._stdout = stdout
        self.settings: Any | None = None
        self.catalog: ProjectCatalog | Any | None = None
        self.lease: DaemonLease | Any | None = None
        self.router: RuntimeManagerRouter | Any | None = None
        self.server: Any | None = None
        self.metadata: dict[str, Any] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_error: BaseException | None = None
        self._signal_restore: Callable[[], None] | None = None
        self._started = False
        self._state = "new"
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task[dict[str, Any]] | None = None
        self._shutdown_requested = False

    @property
    def state(self) -> str:
        """Return the lifecycle state, primarily for diagnostics and tests."""
        return self._state

    def _make_manager(self, descriptor: Any) -> RuntimeManager:
        if self._manager_factory_override is not None:
            return self._manager_factory_override(descriptor)
        project_settings = load_project_settings(descriptor.workspace)
        return RuntimeManager(
            settings=project_settings,
            agent_factory=lambda thread_id, _shared: build_coding_agent(
                project_settings,
                project_root=descriptor.workspace,
                load_mcp=None,
                prompt_cache_key=lambda: thread_id,
                mcp_pool_key=f"{descriptor.project_id}:{thread_id}",
            ),
            max_concurrent_sessions=project_settings.max_concurrency,
            project_id=descriptor.project_id,
        )

    def _make_service(self, principal: Principal) -> Any:
        if self._service_factory_override is not None:
            delegate = self._service_factory_override(principal)
        else:
            delegate = LocalAgentRuntimeService(self.router)
        return bind_access(delegate, principal, DaemonAuthorizer())

    async def start(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._state == "running":
                assert self.metadata is not None
                return self.metadata
            if self._state != "new":
                raise RuntimeError("runtime daemon instance is one-shot")
            self._state = "starting"
            self._start_task = asyncio.create_task(self._start_impl(), name="synapse-runtime-start")
            self._start_task.add_done_callback(_consume_task_exception)
            task = self._start_task
        return await asyncio.shield(task)

    async def _start_impl(self) -> dict[str, Any]:
        try:
            self.settings = (
                self._settings_factory()
                if self._settings_factory is not None
                else load_global_settings()
            )
            self.lease = (
                self._lease_factory(self.config.state_dir)
                if self._lease_factory is not None
                else DaemonLease(self.config.state_dir)
            )
            self.lease.acquire()
            token_path = self.config.token_file or self.config.state_dir / "token"
            token = (
                self._token_loader(token_path)
                if self._token_loader is not None
                else load_token(token_path)
            )
            authenticator = BearerTokenAuthenticator(token)
            self.catalog = self._catalog_factory(self.settings.resolved_catalog_path())
            provider = CatalogProjectProvider(self.catalog)
            self.router = (
                self._router_factory(provider, self._make_manager)
                if self._router_factory is not None
                else RuntimeManagerRouter(provider, self._make_manager)
            )
            self.server = self._server_factory(
                authenticator,
                self._make_service,
                host=self.config.host,
                port=self.config.port,
            )
            await self.server.start()
            addresses = self.server.bound_addresses
            if not addresses:
                raise RuntimeError("runtime daemon did not bind")
            port = int(addresses[0][1])
            self.metadata = self.lease.publish(host=self.config.host, port=port)
            self._signal_restore = (
                self._signal_installer(self.stop_event)
                if self._signal_installer is not None
                else install_signal_handlers(self.stop_event)
            )
            if self._stdout is not None:
                self._stdout.write(_compact_json(self.metadata) + "\n")
                self._stdout.flush()
            async with self._lifecycle_lock:
                stopping = self._shutdown_requested or self._state == "stopping"
                if not stopping:
                    self._state = "running"
                    self._started = True
                    return self.metadata
            cleanup = await self._get_shutdown_task()
            await asyncio.shield(cleanup)
            raise RuntimeError("runtime daemon stopped during startup")
        except BaseException:
            async with self._lifecycle_lock:
                self._state = "stopping"
                cleanup = await self._get_shutdown_task_locked()
            await asyncio.shield(cleanup)
            async with self._lifecycle_lock:
                self._state = "stopped"
                self._started = False
            raise

    async def run(self) -> None:
        await self.start()
        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        """Join one cancellation-independent reverse-order shutdown task."""
        start_task: asyncio.Task[dict[str, Any]] | None = None
        async with self._lifecycle_lock:
            if self._state == "stopped":
                task = self._shutdown_task
            elif self._state == "new":
                self._state = "stopping"
                self._shutdown_requested = True
                task = await self._get_shutdown_task_locked()
            elif self._state == "starting":
                self._state = "stopping"
                self._shutdown_requested = True
                start_task = self._start_task
                # Startup owns creation of the cleanup task.  Creating it here
                # would let cleanup observe a partially initialized resource
                # set and then race with the remainder of startup.
                task = None
            else:
                self._state = "stopping"
                self._shutdown_requested = True
                task = await self._get_shutdown_task_locked()
                start_task = None
        cancelled: asyncio.CancelledError | None = None
        if start_task is not None:
            try:
                await asyncio.shield(start_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except BaseException:
                # Startup owns and reports its own failure; shutdown must still
                # join the cleanup task and report only its first cleanup error.
                pass
            async with self._lifecycle_lock:
                task = self._shutdown_task
        if task is None:
            async with self._lifecycle_lock:
                task = self._shutdown_task
        if task is None:
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        if cancelled is not None:
            raise cancelled
        if self._shutdown_error is not None:
            raise self._shutdown_error

    async def _get_shutdown_task(self) -> asyncio.Task[None]:
        async with self._lifecycle_lock:
            return await self._get_shutdown_task_locked()

    async def _get_shutdown_task_locked(self) -> asyncio.Task[None]:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._finish_shutdown(), name="synapse-runtime-shutdown"
            )
            self._shutdown_task.add_done_callback(_consume_task_exception)
        return self._shutdown_task

    async def _finish_shutdown(self) -> None:
        first_error: BaseException | None = None
        # Signal restoration is deliberately performed before resources: it is
        # installed last during startup and must be removed first.
        if self._signal_restore is not None:
            try:
                self._signal_restore()
            except BaseException as exc:
                first_error = exc
            self._signal_restore = None
        for resource, method in (
            (self.server, "close"),
            (self.router, "shutdown"),
            (self.catalog, "close"),
            (self.lease, "release"),
        ):
            if resource is None:
                continue
            try:
                result = getattr(resource, method)()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        self._shutdown_error = first_error
        self._started = False
        self._state = "stopped"


def _compact_json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def install_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    """Install portable stop handlers and return an idempotent restorer."""
    loop = asyncio.get_running_loop()
    old: dict[signal.Signals, Any] = {}
    installed_loop = False
    loop_installed: list[signal.Signals] = []
    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    try:
        for signum in signals:
            loop.add_signal_handler(signum, stop_event.set)
            loop_installed.append(signum)
        installed_loop = True
    except (NotImplementedError, RuntimeError, ValueError):
        for signum in loop_installed:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signum)
        if loop_installed:
            raise
        try:
            for signum in signals:
                previous = signal.getsignal(signum)
                old[signum] = previous
                signal.signal(signum, lambda _signum, _frame: stop_event.set())
        except BaseException:
            for restored_signum, previous in old.items():
                with contextlib.suppress(BaseException):
                    signal.signal(restored_signum, previous)
            raise
    except BaseException:
        for signum in loop_installed:
            with contextlib.suppress(BaseException):
                loop.remove_signal_handler(signum)
        raise

    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        if installed_loop:
            for signum in loop_installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(signum)
        else:
            for signum, previous in old.items():
                signal.signal(signum, previous)

    return restore


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Prevent an unjoined lifecycle task from producing an event-loop warning."""
    try:
        task.exception()
    except BaseException:
        pass


async def run_daemon(config: DaemonConfig | None = None, **kwargs: Any) -> None:
    """Run a foreground daemon until SIGINT, SIGTERM, or an injected event."""
    daemon = RuntimeDaemon(config or DaemonConfig(), **kwargs)
    await daemon.run()
