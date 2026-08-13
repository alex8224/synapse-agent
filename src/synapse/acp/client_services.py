"""Capability-aware ACP Client filesystem and terminal gateway."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp.schema import (
    CreateTerminalResponse,
    ReadTextFileResponse,
    TerminalOutputResponse,
    WriteTextFileResponse,
)


class ACPClientServiceError(RuntimeError):
    """Client service is unavailable or the request violates session scope."""


@dataclass(frozen=True, slots=True)
class ACPClientScope:
    session_id: str
    cwd: Path
    additional_directories: tuple[Path, ...] = ()

    def authorize(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.cwd / path
        resolved = path.resolve()
        roots = (self.cwd, *self.additional_directories)
        if not any(_is_relative_to(resolved, root.resolve()) for root in roots):
            raise ACPClientServiceError("path is outside the ACP session scope")
        return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class ClientServiceGateway:
    """Route filesystem and terminal operations to one ACP Client connection."""

    def __init__(self, connection: Any, capabilities: Any, scope: ACPClientScope) -> None:
        self._connection = connection
        self._capabilities = capabilities
        self.scope = scope
        self._terminals: set[str] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    def _require_open(self) -> None:
        if self._closed or self._connection is None:
            raise ACPClientServiceError("ACP Client connection is closed")

    def _has_fs(self, name: str) -> bool:
        fs = getattr(self._capabilities, "fs", None)
        return bool(getattr(fs, name, False))

    def _has_terminal(self) -> bool:
        return bool(getattr(self._capabilities, "terminal", False))

    async def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call one Client RPC and normalize failures to a stable tool error.

        Cancellation must keep propagating so prompt cancel/disconnect semantics
        stay intact; every other Client-side failure is collapsed into
        ``ACPClientServiceError`` so the tool layer never leaks raw transport
        exceptions to the model.
        """
        try:
            return await getattr(self._connection, method)(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except ACPClientServiceError:
            raise
        except Exception as exc:
            raise ACPClientServiceError(f"ACP Client {method} failed: {exc}") from exc

    async def read_text_file(
        self, path: str, *, line: int | None = None, limit: int | None = None
    ) -> ReadTextFileResponse:
        self._require_open()
        if not self._has_fs("read_text_file"):
            raise ACPClientServiceError("Client does not provide fs/read_text_file")
        resolved = self.scope.authorize(path)
        return await self._invoke(
            "read_text_file",
            self.scope.session_id,
            str(resolved),
            line=line,
            limit=limit,
        )

    async def write_text_file(self, path: str, content: str) -> WriteTextFileResponse:
        self._require_open()
        if not self._has_fs("write_text_file"):
            raise ACPClientServiceError("Client does not provide fs/write_text_file")
        resolved = self.scope.authorize(path)
        return await self._invoke(
            "write_text_file",
            self.scope.session_id,
            str(resolved),
            content,
        )

    async def create_terminal(
        self,
        command: str,
        *,
        args: list[str] | None = None,
        env: list[Any] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
    ) -> CreateTerminalResponse:
        self._require_open()
        if not self._has_terminal():
            raise ACPClientServiceError("Client does not provide terminal services")
        resolved_cwd = self.scope.authorize(cwd) if cwd else self.scope.cwd
        response = await self._invoke(
            "create_terminal",
            self.scope.session_id,
            command,
            args=args,
            env=env,
            cwd=str(resolved_cwd),
            output_byte_limit=output_byte_limit,
        )
        terminal_id = str(response.terminal_id)
        async with self._lock:
            if self._closed:
                with contextlib.suppress(Exception):
                    await self._connection.release_terminal(
                        self.scope.session_id, terminal_id
                    )
                raise ACPClientServiceError("gateway closed during terminal creation")
            self._terminals.add(terminal_id)
        return response

    async def terminal_output(self, terminal_id: str) -> TerminalOutputResponse:
        self._require_terminal(terminal_id)
        return await self._invoke(
            "terminal_output", self.scope.session_id, terminal_id
        )

    async def wait_for_terminal_exit(self, terminal_id: str) -> Any:
        self._require_terminal(terminal_id)
        return await self._invoke(
            "wait_for_terminal_exit", self.scope.session_id, terminal_id
        )

    async def kill_terminal(self, terminal_id: str) -> None:
        self._require_terminal(terminal_id)
        await self._invoke("kill_terminal", self.scope.session_id, terminal_id)

    async def release_terminal(self, terminal_id: str) -> None:
        self._require_terminal(terminal_id)
        await self._invoke("release_terminal", self.scope.session_id, terminal_id)
        async with self._lock:
            self._terminals.discard(terminal_id)

    def _require_terminal(self, terminal_id: str) -> None:
        self._require_open()
        if not self._has_terminal():
            raise ACPClientServiceError("Client does not provide terminal services")
        if terminal_id not in self._terminals:
            raise ACPClientServiceError("terminal does not belong to this ACP session")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            terminals = tuple(self._terminals)
            self._terminals.clear()
        if self._connection is None:
            return
        await asyncio.gather(
            *(
                self._connection.release_terminal(self.scope.session_id, terminal_id)
                for terminal_id in terminals
            ),
            return_exceptions=True,
        )


def build_client_service_tools(gateway: ClientServiceGateway | None) -> list[Any]:
    """Build session-local tools backed by ACP Client reverse RPCs.

    Tools are only exposed when the negotiated capability exists.  The normal
    local backend remains authoritative for sessions without a corresponding
    Client capability.
    """
    if gateway is None:
        return []
    from langchain_core.tools import tool

    tools: list[Any] = []
    if gateway._has_fs("read_text_file"):  # noqa: SLF001 - same adapter boundary

        @tool("client_read_text_file")
        async def client_read_text_file(path: str, line: int = 1, limit: int = 2000) -> str:
            """Read a text file from the ACP Client editor/workspace."""
            response = await gateway.read_text_file(path, line=line, limit=limit)
            return str(response.content)

        tools.append(client_read_text_file)
    if gateway._has_fs("write_text_file"):  # noqa: SLF001 - same adapter boundary

        @tool("client_write_text_file")
        async def client_write_text_file(path: str, content: str) -> str:
            """Write a text file through the ACP Client."""
            await gateway.write_text_file(path, content)
            return f"Wrote {path}."

        tools.append(client_write_text_file)
    if gateway._has_terminal():

        @tool("client_create_terminal")
        async def client_create_terminal(command: str, cwd: str | None = None) -> str:
            """Create a terminal through the ACP Client."""
            response = await gateway.create_terminal(command, cwd=cwd)
            return str(response.terminal_id)

        @tool("client_terminal_output")
        async def client_terminal_output(terminal_id: str) -> str:
            """Read bounded output from an ACP Client terminal."""
            response = await gateway.terminal_output(terminal_id)
            return str(response.output)

        @tool("client_wait_for_terminal_exit")
        async def client_wait_for_terminal_exit(terminal_id: str) -> str:
            """Wait for an ACP Client terminal to exit."""
            response = await gateway.wait_for_terminal_exit(terminal_id)
            return str(response)

        @tool("client_kill_terminal")
        async def client_kill_terminal(terminal_id: str) -> str:
            """Kill an ACP Client terminal."""
            await gateway.kill_terminal(terminal_id)
            return f"Killed terminal {terminal_id}."

        @tool("client_release_terminal")
        async def client_release_terminal(terminal_id: str) -> str:
            """Release an ACP Client terminal and its resources."""
            await gateway.release_terminal(terminal_id)
            return f"Released terminal {terminal_id}."

        tools.extend(
            [
                client_create_terminal,
                client_terminal_output,
                client_wait_for_terminal_exit,
                client_kill_terminal,
                client_release_terminal,
            ]
        )
    return tools
