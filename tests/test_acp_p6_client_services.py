"""ACP P6 Client service gateway tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from acp.schema import (
    CreateTerminalResponse,
    FileSystemCapabilities,
    ReadTextFileResponse,
    TerminalOutputResponse,
    WriteTextFileResponse,
)

from synapse.acp.client_services import (
    ACPClientScope,
    ACPClientServiceError,
    ClientServiceGateway,
    build_client_service_tools,
)


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.released: list[str] = []

    async def read_text_file(self, *args: Any, **kwargs: Any) -> ReadTextFileResponse:
        self.calls.append(("read", args, kwargs))
        return ReadTextFileResponse(content="hello")

    async def write_text_file(self, *args: Any, **kwargs: Any) -> WriteTextFileResponse:
        self.calls.append(("write", args, kwargs))
        return WriteTextFileResponse()

    async def create_terminal(self, *args: Any, **kwargs: Any) -> CreateTerminalResponse:
        self.calls.append(("create", args, kwargs))
        return CreateTerminalResponse(terminal_id="term-1")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> TerminalOutputResponse:
        self.calls.append(("output", args, kwargs))
        return TerminalOutputResponse(output="ok", truncated=False)

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(("wait", args, kwargs))
        return object()

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("kill", args, kwargs))

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        self.calls.append(("release", (session_id, terminal_id), kwargs))
        self.released.append(terminal_id)


def test_filesystem_capability_and_scope_are_enforced(tmp_path: Path) -> None:
    async def run() -> None:
        client = _Client()
        caps = type(
            "Caps",
            (),
            {
                "fs": FileSystemCapabilities(read_text_file=True, write_text_file=True),
                "terminal": False,
            },
        )()
        gateway = ClientServiceGateway(
            client,
            caps,
            ACPClientScope("sess-1", tmp_path, (tmp_path / "extra",)),
        )
        await gateway.read_text_file("file.txt", line=2, limit=10)
        await gateway.write_text_file("file.txt", "new")
        with pytest.raises(ACPClientServiceError, match="outside"):
            await gateway.read_text_file(str(tmp_path.parent / "secret.txt"))
        assert [item[0] for item in client.calls] == ["read", "write"]

    asyncio.run(run())


def test_client_tools_are_capability_gated() -> None:
    class Gateway:
        def _has_fs(self, name: str) -> bool:
            return name == "read_text_file"

        def _has_terminal(self) -> bool:
            return False

    assert [item.name for item in build_client_service_tools(Gateway())] == [
        "client_read_text_file"
    ]
    assert build_client_service_tools(None) == []


def test_terminal_registry_is_session_scoped_and_released_on_close(tmp_path: Path) -> None:
    async def run() -> None:
        client = _Client()
        caps = type("Caps", (), {"fs": None, "terminal": True})()
        gateway = ClientServiceGateway(client, caps, ACPClientScope("sess-1", tmp_path))
        created = await gateway.create_terminal("python", args=["-V"])
        assert created.terminal_id == "term-1"
        assert (await gateway.terminal_output("term-1")).output == "ok"
        with pytest.raises(ACPClientServiceError, match="does not belong"):
            await gateway.terminal_output("other")
        await gateway.close()
        assert client.released == ["term-1"]
        with pytest.raises(ACPClientServiceError, match="closed"):
            await gateway.kill_terminal("term-1")

    asyncio.run(run())


def test_client_rpc_failure_is_normalized_to_stable_error(tmp_path: Path) -> None:
    async def run() -> None:
        class FailingClient:
            async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("transport closed")

        gateway = ClientServiceGateway(
            FailingClient(),
            type("Caps", (), {"fs": FileSystemCapabilities(read_text_file=True)})(),
            ACPClientScope("sess-1", tmp_path),
        )
        with pytest.raises(ACPClientServiceError, match="read_text_file failed"):
            await gateway.read_text_file("file.txt")

    asyncio.run(run())


def test_client_rpc_cancellation_is_not_swallowed(tmp_path: Path) -> None:
    async def run() -> None:
        class CancellingClient:
            async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
                raise asyncio.CancelledError()

        gateway = ClientServiceGateway(
            CancellingClient(),
            type("Caps", (), {"fs": FileSystemCapabilities(read_text_file=True)})(),
            ACPClientScope("sess-1", tmp_path),
        )
        with pytest.raises(asyncio.CancelledError):
            await gateway.read_text_file("file.txt")

    asyncio.run(run())


def test_closed_gateway_returns_stable_disconnect_error(tmp_path: Path) -> None:
    async def run() -> None:
        client = _Client()
        gateway = ClientServiceGateway(
            client,
            type("Caps", (), {"fs": FileSystemCapabilities(read_text_file=True)})(),
            ACPClientScope("sess-1", tmp_path),
        )
        await gateway.close()
        with pytest.raises(ACPClientServiceError, match="closed"):
            await gateway.read_text_file("file.txt")

    asyncio.run(run())


def test_read_text_file_is_client_backed_for_unsaved_buffers(tmp_path: Path) -> None:
    async def run() -> None:
        client = _Client()
        gateway = ClientServiceGateway(
            client,
            type("Caps", (), {"fs": FileSystemCapabilities(read_text_file=True)})(),
            ACPClientScope("sess-1", tmp_path),
        )
        # The path does not exist on disk: content must come from the Client,
        # which is what makes unsaved editor buffers readable.
        response = await gateway.read_text_file("unsaved-buffer.txt")
        assert response.content == "hello"
        assert not (tmp_path / "unsaved-buffer.txt").exists()

    asyncio.run(run())


def test_terminals_are_isolated_across_sessions(tmp_path: Path) -> None:
    async def run() -> None:
        client = _Client()
        caps = type("Caps", (), {"fs": None, "terminal": True})()
        first = ClientServiceGateway(client, caps, ACPClientScope("sess-1", tmp_path))
        second = ClientServiceGateway(client, caps, ACPClientScope("sess-2", tmp_path))

        created = await first.create_terminal("python")
        # The same terminal id must not be addressable from another session.
        with pytest.raises(ACPClientServiceError, match="does not belong"):
            await second.terminal_output(created.terminal_id)
        # The owning session still sees it.
        assert (await first.terminal_output(created.terminal_id)).output == "ok"
        await first.close()
        await second.close()

    asyncio.run(run())


def test_terminal_creation_during_close_releases_handle(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowClient(_Client):
            async def create_terminal(self, *args: Any, **kwargs: Any) -> CreateTerminalResponse:
                started.set()
                await release.wait()
                return await super().create_terminal(*args, **kwargs)

        client = SlowClient()
        gateway = ClientServiceGateway(
            client,
            type("Caps", (), {"fs": None, "terminal": True})(),
            ACPClientScope("sess-1", tmp_path),
        )
        task = asyncio.create_task(gateway.create_terminal("python"))
        await started.wait()
        await gateway.close()
        release.set()
        with pytest.raises(ACPClientServiceError, match="closed during terminal creation"):
            await task
        assert "term-1" in client.released

    asyncio.run(run())


@pytest.mark.parametrize(
    ("read", "write", "terminal", "expected"),
    [
        (True, False, False, ["client_read_text_file"]),
        (
            True,
            True,
            False,
            ["client_read_text_file", "client_write_text_file"],
        ),
        (
            False,
            False,
            True,
            [
                "client_create_terminal",
                "client_terminal_output",
                "client_wait_for_terminal_exit",
                "client_kill_terminal",
                "client_release_terminal",
            ],
        ),
        (
            True,
            True,
            True,
            [
                "client_read_text_file",
                "client_write_text_file",
                "client_create_terminal",
                "client_terminal_output",
                "client_wait_for_terminal_exit",
                "client_kill_terminal",
                "client_release_terminal",
            ],
        ),
    ],
)
def test_client_tool_combinations(
    read: bool, write: bool, terminal: bool, expected: list[str]
) -> None:
    class Gateway:
        def _has_fs(self, name: str) -> bool:
            return (name == "read_text_file" and read) or (
                name == "write_text_file" and write
            )

        def _has_terminal(self) -> bool:
            return terminal

    assert [item.name for item in build_client_service_tools(Gateway())] == expected
