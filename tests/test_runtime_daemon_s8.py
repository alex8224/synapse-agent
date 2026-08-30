from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from websockets.asyncio.client import connect

from synapse.runtime.daemon.application import RuntimeDaemon, install_signal_handlers
from synapse.runtime.daemon.auth import BearerTokenAuthenticator, TokenFileError, load_token
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.daemon.lease import DaemonAlreadyRunningError, DaemonLease
from synapse.runtime.service import (
    ALL_RUNTIME_CAPABILITIES,
    DaemonAuthorizer,
    PermissionDeniedError,
    Principal,
    SessionView,
    UsageView,
)
from synapse.runtime.sessions.ref import SessionRef


def test_token_is_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    first = load_token(path)
    assert first == load_token(path)
    assert path.read_text(encoding="utf-8") == first + "\n"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "content",
    [b"", b"one\ntwo\n", b"x" * 1025, b"\xff\n", b"\n"],
)
def test_malformed_token_is_rejected_without_echoing_value(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "token"
    path.write_bytes(content)
    with pytest.raises(TokenFileError) as caught:
        load_token(path)
    if content:
        assert content.decode("utf-8", errors="ignore") not in str(caught.value)


def test_token_symlink_and_wide_permissions_fail_closed(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_text("token\n", encoding="utf-8")
    link = tmp_path / "token"
    link.symlink_to(target)
    with pytest.raises(TokenFileError):
        load_token(link)
    link.unlink()
    link.write_text("token\n", encoding="utf-8")
    if os.name != "nt":
        link.chmod(0o644)
        with pytest.raises(TokenFileError):
            load_token(link)


def test_bearer_auth_is_exact_and_returns_fixed_principal() -> None:
    async def run() -> None:
        auth = BearerTokenAuthenticator("abc")
        assert (await auth({"Authorization": "Bearer abc"})).subject == "runtime-daemon"
        for value in ("Bearer abc extra", "Bearer  abc", "Basic abc", "Bearer wrong", "Bearer"):
            with pytest.raises(ValueError):
                await auth({"Authorization": value})

    asyncio.run(run())


def test_daemon_authorizer_allows_every_exact_runtime_scope_only() -> None:
    authorizer = DaemonAuthorizer()
    for project in ("a", "b"):
        for capability in ALL_RUNTIME_CAPABILITIES:
            authorizer.authorize(Principal("runtime-daemon"), capability, SessionRef(project, "t"))
    with pytest.raises(PermissionDeniedError):
        authorizer.authorize(
            Principal("other"), next(iter(ALL_RUNTIME_CAPABILITIES)), SessionRef("a", "t")
        )


def test_lease_competes_releases_idempotently_and_honors_metadata_owner(tmp_path: Path) -> None:
    first = DaemonLease(tmp_path)
    first.acquire()
    second = DaemonLease(tmp_path)
    with pytest.raises(DaemonAlreadyRunningError):
        second.acquire()
    first.publish(host="127.0.0.1", port=1234)
    data = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
    data["instance_id"] = "new-owner"
    (tmp_path / "daemon.json").write_text(json.dumps(data), encoding="utf-8")
    first.release()
    first.release()
    assert (tmp_path / "daemon.json").exists()
    second.acquire()
    second.release()


def test_shutdown_is_reverse_order_and_joined_when_cancelled() -> None:
    async def run() -> None:
        calls: list[str] = []

        class Resource:
            def __init__(self, name: str) -> None:
                self.name = name

            async def close(self) -> None:
                calls.append(self.name)

            async def shutdown(self) -> None:
                calls.append(self.name)

            def release(self) -> None:
                calls.append(self.name)

        daemon = RuntimeDaemon(DaemonConfig(state_dir=Path(".s8-test-state")))
        daemon.server = Resource("server")
        daemon.router = Resource("router")
        daemon.catalog = Resource("catalog")
        daemon.lease = Resource("lease")
        task = asyncio.create_task(daemon.shutdown())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await daemon.shutdown()
        assert calls == ["server", "router", "catalog", "lease"]

    asyncio.run(run())


def test_signal_handlers_restore_and_share_stop_event() -> None:
    async def run() -> None:
        event = asyncio.Event()
        loop = asyncio.get_running_loop()
        callbacks: dict[signal.Signals, object] = {}
        removed: list[signal.Signals] = []

        def add(signum: signal.Signals, callback: object) -> None:
            callbacks[signum] = callback

        def remove(signum: signal.Signals) -> bool:
            removed.append(signum)
            return True

        with patch.object(loop, "add_signal_handler", add), patch.object(
            loop, "remove_signal_handler", remove
        ):
            restore = install_signal_handlers(event)
            callbacks[signal.SIGINT]()  # type: ignore[operator]
            assert event.is_set()
            restore()
            restore()
        expected = [signal.SIGINT, signal.SIGTERM]
        if os.name == "nt" and hasattr(signal, "SIGBREAK"):
            expected.append(signal.SIGBREAK)
        assert removed == expected

    asyncio.run(run())


class _Service:
    async def submit_turn(self, value: object) -> object:
        return value

    async def open_session(self, value: object) -> object:
        return value

    async def cancel_turn(self, value: object) -> object:
        return value

    async def steer_turn(self, value: object) -> object:
        return value

    async def close_session(self, value: object) -> object:
        return value

    async def get_session(self, value: object) -> object:
        return SessionView(
            project_id="p",
            thread_id="t",
            status="idle",
            active_turn_id=None,
            latest_sequence=0,
            usage=UsageView(input_tokens=0, output_tokens=0, cache_tokens=0),
            last_error=None,
            last_activity_at="2025-01-01T00:00:00+00:00",
        )

    async def stat_artifact(self, value: object) -> object:
        return value

    async def list_artifacts(self, value: object) -> object:
        return value

    async def read_artifact(self, value: object) -> object:
        return value

    async def read_events(self, value: object) -> object:
        return value

    def watch_events(self, session: object, **kwargs: object) -> object:
        del session, kwargs
        raise RuntimeError("not used")


def test_daemon_publishes_only_after_bind_and_cleans_metadata(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SimpleNamespace(resolved_catalog_path=lambda: tmp_path / "catalog.sqlite")
        with patch(
            "synapse.runtime.daemon.application.load_global_settings", return_value=settings
        ):
            daemon = RuntimeDaemon(
                DaemonConfig(state_dir=tmp_path, port=0),
                service_factory=lambda principal: _Service(),
            )
            metadata = await daemon.start()
            assert metadata["port"] > 0
            assert "token" not in json.dumps(metadata)
            wire_metadata = json.loads(
                (tmp_path / "daemon.json").read_text(encoding="utf-8")
            )
            assert wire_metadata["port"] == metadata["port"]
            port = metadata["port"]
            token = (tmp_path / "token").read_text(encoding="utf-8").strip()
            async with connect(
                f"ws://127.0.0.1:{port}",
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "runtime.session.get",
                            "params": {"session": {"project_id": "p", "thread_id": "t"}},
                        }
                    )
                )
                response = json.loads(await ws.recv())
                assert response["result"]["project_id"] == "p"
            await daemon.shutdown()
            assert not (tmp_path / "daemon.json").exists()
            assert not daemon.lease.acquired

    asyncio.run(run())
