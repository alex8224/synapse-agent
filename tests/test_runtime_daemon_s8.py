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


def test_mcp_rebinding_writes_only_what_was_asked_and_reuses_a_live_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from typing import Any

    from synapse.runtime.daemon import application as app

    workspace = tmp_path / "proj"
    workspace.mkdir()
    written: list[tuple[str, object, object]] = []
    builds: list[dict[str, object]] = []
    released: list[str] = []

    class FakePool:
        tools = ["search__query"]

    live_pool: dict[str, object] = {}

    class FakeRegistry:
        def get(self, key: str) -> object:
            return live_pool.get(key)

        def release(self, key: str) -> None:
            released.append(key)
            live_pool.pop(key, None)

    monkeypatch.setattr(
        app,
        "set_mcp_server_enabled",
        lambda name, value, **kwargs: written.append(("enabled", name, value)),
    )
    monkeypatch.setattr(
        app,
        "set_mcp_server_include_tools",
        lambda name, value, **kwargs: written.append(("include_tools", name, value)),
    )
    monkeypatch.setattr(app, "load_project_settings", lambda workspace: SimpleNamespace())
    monkeypatch.setattr(
        app, "registry_from_settings", lambda settings: SimpleNamespace(get=lambda model: None)
    )
    monkeypatch.setattr(app, "apply_profile_to_settings", lambda *a, **k: None)
    monkeypatch.setattr(app, "_mcp_server_states", lambda *a, **k: ["state"])

    def fake_build(settings: Any, **kwargs: Any) -> Any:
        builds.append(kwargs)
        return SimpleNamespace(_coding_mcp_servers=["search"], _coding_mcp_tool_names=["x"])

    monkeypatch.setattr(app, "build_coding_agent", fake_build)
    monkeypatch.setattr(
        "synapse.integrations.mcp_client.get_mcp_pool_registry", lambda: FakeRegistry()
    )

    descriptor = SimpleNamespace(project_id="p1", workspace=workspace)
    binding = SimpleNamespace(settings=SimpleNamespace(active_model="m", model="m"))
    common = {
        "descriptor": descriptor,
        "project_settings": SimpleNamespace(mcp_config_path=None),
        "thread_id": "t1",
        "binding": binding,
    }

    # Attach-all: nothing persisted, live pool reused, no reconnect.
    live_pool["p1:t1"] = FakePool()
    agent, _settings = app.apply_mcp_rebinding(
        server=None, enabled=None, include_tools=None, **common
    )
    assert written == []
    assert released == []
    assert builds[-1]["mcp_tools"] == ["search__query"]
    assert builds[-1]["load_mcp"] is False
    assert agent._coding_mcp_server_states == ["state"]

    # Enabled write: persisted, pool released, reconnect forced.
    app.apply_mcp_rebinding(server="search", enabled=False, include_tools=None, **common)
    assert written == [("enabled", "search", False)]
    assert released == ["p1:t1"]
    assert builds[-1]["load_mcp"] is True

    # Tool whitelist write: persisted as given (empty list = load everything).
    app.apply_mcp_rebinding(server="search", enabled=None, include_tools=(), **common)
    assert written[-1] == ("include_tools", "search", ())
    assert builds[-1]["load_mcp"] is True


def test_mcp_server_states_report_config_and_live_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from synapse.runtime.daemon.application import _mcp_server_states
    from synapse.settings.config_paths import MCP_FILENAME

    # Only the project layer may contribute servers: the developer's own
    # ~/.synapse/mcp.json must never leak into this assertion.
    monkeypatch.setattr(
        "synapse.settings.config_paths.user_config_dir",
        lambda: (tmp_path / "home" / ".synapse").resolve(),
    )
    monkeypatch.setattr("synapse.settings.config_paths.executable_config_dirs", lambda: [])
    workspace = tmp_path / "proj"
    config_dir = workspace / ".synapse"
    config_dir.mkdir(parents=True)
    (config_dir / MCP_FILENAME).write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "search",
                        "transport": "streamable_http",
                        "enabled": True,
                        "include_tools": ["query"],
                    },
                    {"name": "other", "transport": "stdio", "enabled": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = SimpleNamespace(
        workspace=str(workspace), mcp_config_path=None, mcp_servers_json=None
    )
    pool = SimpleNamespace(discovered_tools={"search": ["query", "fetch"]})
    agent = SimpleNamespace(
        _coding_mcp_tool_names=["search__query"],
    )

    states = _mcp_server_states(settings, pool, agent, active=("search",))

    assert [state["name"] for state in states] == ["search", "other"]
    assert states[0] == {
        "name": "search",
        "enabled": True,
        "attached": True,
        "include_tools": ["query"],
        "discovered": ["query", "fetch"],
        "loaded": ["search__query"],
    }
    # A disabled server is reported as configured-but-not-attached, and the
    # live pool has nothing for it.
    assert states[1]["enabled"] is False
    assert states[1]["attached"] is False
    assert states[1]["discovered"] == []
    assert states[1]["loaded"] == []


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

    async def pending_approval(self, value: object) -> object:
        return value

    async def resume_turn(self, value: object) -> object:
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
