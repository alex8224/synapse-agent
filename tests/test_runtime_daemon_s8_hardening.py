from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.config import DaemonConfig, ensure_directory


class _Resource:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def close(self) -> None:
        self.calls.append(self.name)

    async def shutdown(self) -> None:
        self.calls.append(self.name)

    def release(self) -> None:
        self.calls.append(self.name)


def test_shutdown_before_start_is_terminal() -> None:
    async def run() -> None:
        daemon = RuntimeDaemon(DaemonConfig(state_dir=Path(".s8-hardening-state")))
        await daemon.shutdown()
        assert daemon.state == "stopped"
        with pytest.raises(RuntimeError):
            await daemon.start()

    asyncio.run(run())


def test_concurrent_shutdown_joins_same_cleanup_and_preserves_first_error() -> None:
    async def run() -> None:
        calls: list[str] = []

        class Failure(_Resource):
            async def close(self) -> None:
                self.calls.append(self.name)
                raise RuntimeError("secret failure")

        daemon = RuntimeDaemon(DaemonConfig(state_dir=Path(".s8-hardening-state")))
        daemon.server = Failure("server", calls)
        daemon.router = _Resource("router", calls)
        daemon.catalog = _Resource("catalog", calls)
        daemon.lease = _Resource("lease", calls)
        first, second = await asyncio.gather(
            daemon.shutdown(), daemon.shutdown(), return_exceptions=True
        )
        assert type(first) is RuntimeError
        assert type(second) is RuntimeError
        assert first is second
        assert calls == ["server", "router", "catalog", "lease"]

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["settings", "token", "catalog", "router", "server"])
def test_startup_failure_rolls_back_resources_and_is_one_shot(
    tmp_path: Path, failure: str
) -> None:
    async def run() -> None:
        calls: list[str] = []
        settings = SimpleNamespace(resolved_catalog_path=lambda: tmp_path / "catalog.sqlite")

        def fail(name: str) -> object:
            calls.append(name)
            raise RuntimeError(failure)

        class Server(_Resource):
            bound_addresses = [("127.0.0.1", 1234)]

            async def start(self) -> None:
                calls.append("server.start")

        def server_factory(*args: object, **kwargs: object) -> Server:
            del args, kwargs
            if failure == "server":
                fail("server")
            return Server("server", calls)

        daemon = RuntimeDaemon(
            DaemonConfig(state_dir=tmp_path),
            settings_factory=lambda: fail("settings") if failure == "settings" else settings,
            token_loader=lambda _: fail("token") if failure == "token" else "token",
            catalog_factory=lambda _: fail("catalog") if failure == "catalog" else _Resource(
                "catalog", calls
            ),
            router_factory=lambda *_: fail("router") if failure == "router" else _Resource(
                "router", calls
            ),
            server_factory=server_factory,
            signal_installer=lambda _: (lambda: calls.append("signals")),
        )
        with pytest.raises(RuntimeError):
            await daemon.start()
        assert daemon.state == "stopped"
        with pytest.raises(RuntimeError):
            await daemon.start()
        assert not (tmp_path / "daemon.json").exists()

    asyncio.run(run())


def test_cli_hides_unexpected_exception(monkeypatch, capsys) -> None:
    del monkeypatch
    with patch(
        "synapse.runtime.daemon.entry.run_daemon",
        side_effect=RuntimeError("token=do-not-print"),
    ):
        from synapse.runtime.daemon.entry import main

        assert main(["--state-dir", ".s8-cli-state"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "synapse-runtime: unable to start daemon\n"
    assert "do-not-print" not in captured.err


def test_existing_state_directory_is_not_silently_chmodded(tmp_path: Path) -> None:
    ensure_directory(tmp_path)
    if os.name != "nt":
        tmp_path.chmod(0o755)
        with pytest.raises(ValueError):
            ensure_directory(tmp_path)


def test_posix_signal_partial_install_rolls_back(monkeypatch) -> None:
    if os.name == "nt" or not hasattr(signal, "SIGTERM"):
        pytest.skip("POSIX signals unavailable")

    async def run() -> None:
        loop = asyncio.get_running_loop()
        added: list[signal.Signals] = []

        def add(signum: signal.Signals, callback: object) -> None:
            del callback
            added.append(signum)
            if len(added) == 2:
                raise RuntimeError("install failure")

        removed: list[signal.Signals] = []
        monkeypatch.setattr(loop, "add_signal_handler", add)
        monkeypatch.setattr(loop, "remove_signal_handler", removed.append)
        with pytest.raises(RuntimeError):
            from synapse.runtime.daemon.application import install_signal_handlers

            install_signal_handlers(asyncio.Event())
        assert removed == [signal.SIGINT]

    asyncio.run(run())
