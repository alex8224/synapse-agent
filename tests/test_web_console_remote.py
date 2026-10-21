"""Unit tests for Web Console remote access, device registry, and pending pairing."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from synapse.web_console.config import WebConsoleConfig
from synapse.web_console.host import ProjectView, WebConsoleHost
from synapse.web_console.remote import (
    DeviceRegistry,
    PendingPairingManager,
    get_lan_ips,
)
from synapse.web_console.security import host_allowed, origin_allowed


def test_device_registry_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = Path(td) / "trusted.json"
        reg = DeviceRegistry(storage)

        # Register device
        dev = reg.register("dev-phone", "iPhone 15", ip="192.168.1.100", user_agent="Safari/17.0")
        assert dev.device_id == "dev-phone"
        assert dev.name == "iPhone 15"
        token = dev.device_token
        assert len(token) > 20

        # Verify
        verified = reg.verify_token(token)
        assert verified is not None
        assert verified.device_id == "dev-phone"
        assert reg.verify_token("bad-token") is None
        assert reg.verify_token("") is None

        # Re-load from storage file
        reg2 = DeviceRegistry(storage)
        verified2 = reg2.verify_token(token)
        assert verified2 is not None
        assert verified2.name == "iPhone 15"
        assert len(reg2.list_devices()) == 1

        # Re-register same device_id updates the token
        dev_new = reg2.register("dev-phone", "iPhone 15 Pro")
        assert dev_new.device_token != token
        assert reg2.verify_token(token) is None
        assert reg2.verify_token(dev_new.device_token) is not None

        # Revoke
        assert reg2.revoke("dev-phone") is True
        assert reg2.verify_token(dev_new.device_token) is None
        assert len(reg2.list_devices()) == 0


def test_device_registry_expiry() -> None:
    now = 1000.0
    reg = DeviceRegistry(None)
    dev = reg.register("dev-1", "Test", ttl_seconds=10.0, now=now)
    assert reg.verify_token(dev.device_token, now=now + 5.0) is not None
    # Expired
    assert reg.verify_token(dev.device_token, now=now + 15.0) is None
    assert len(reg.list_devices(now=now + 15.0)) == 0


def test_pending_pairing_manager_flow() -> None:
    now = 1000.0
    pm = PendingPairingManager()
    reg = DeviceRegistry(None)

    req = pm.create_request("dev-pad", "iPad Air", ip="192.168.1.102", now=now)
    assert req.status == "pending"
    assert len(req.code) == 4
    assert req.code.isdigit()

    pending_list = pm.list_pending(now=now)
    assert len(pending_list) == 1
    assert pending_list[0]["request_id"] == req.request_id

    # Approve
    approved, token = pm.approve(req.request_id, reg, now=now + 10.0)
    assert approved is not None
    assert approved.status == "approved"
    assert token is not None
    assert reg.verify_token(token, now=now + 10.0) is not None

    # No longer pending
    assert len(pm.list_pending(now=now + 10.0)) == 0


def test_pending_pairing_reject() -> None:
    now = 1000.0
    pm = PendingPairingManager()
    req = pm.create_request("dev-bad", "Hacker", now=now)
    assert pm.reject(req.request_id, now=now) is True
    assert pm.get_request(req.request_id, now=now).status == "rejected"
    assert len(pm.list_pending(now=now)) == 0


def test_get_lan_ips() -> None:
    ips = get_lan_ips()
    assert isinstance(ips, list)
    for ip in ips:
        assert not ip.startswith("127.")


def test_allow_remote_config_and_security(tmp_path: Path) -> None:
    workspace = tmp_path / "w"
    workspace.mkdir()

    # allow_remote=True permits 0.0.0.0
    cfg = WebConsoleConfig(workspace=workspace, host="0.0.0.0", allow_remote=True)
    assert cfg.host == "0.0.0.0"
    assert cfg.allow_remote is True

    # host_allowed
    assert not host_allowed("192.168.1.50:8080", bound_port=8080, allow_remote=False)
    assert host_allowed("192.168.1.50:8080", bound_port=8080, allow_remote=True)
    assert not host_allowed("192.168.1.50:9000", bound_port=8080, allow_remote=True)

    # origin_allowed
    assert not origin_allowed("http://192.168.1.50:8080", bound_port=8080, allow_remote=False)
    assert origin_allowed("http://192.168.1.50:8080", bound_port=8080, allow_remote=True)


def test_remote_api_endpoints_flow(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestClient, TestServer

    workspace = tmp_path / "w"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    token_file = state_dir / "token"
    token_file.write_text("test-token", encoding="utf-8")

    config = WebConsoleConfig(
        workspace=workspace,
        static_dir=workspace,
        state_dir=state_dir,
        host="127.0.0.1",
        port=0,
        allow_remote=True,
        pairing_required=True,
    )
    project = ProjectView(
        project_id="proj_1",
        workspace_path=str(workspace),
        name="test-project",
        git_branch=None,
    )
    host = WebConsoleHost(config, project, token="test-token")

    async def _run_test() -> None:
        server = TestServer(host._app)
        client = TestClient(server)
        await client.start_server()
        host.bound = ("127.0.0.1", server.port)

        try:
            # 1. GET /api/remote/status
            resp = await client.get("/api/remote/status")
            assert resp.status == 200
            status_data = await resp.json()
            assert status_data["allow_remote"] is True
            assert status_data["remote_enabled"] is True

            # 2. POST /api/remote/request-access
            req_resp = await client.post(
                "/api/remote/request-access",
                json={"device_id": "phone-1", "name": "Test iPhone"},
            )
            assert req_resp.status == 200
            req_data = await req_resp.json()
            request_id = req_data["request_id"]
            assert req_data["status"] == "pending"
            assert len(req_data["code"]) == 4

            # 3. GET /api/remote/pending
            pending_resp = await client.get("/api/remote/pending")
            assert pending_resp.status == 200
            pending_data = await pending_resp.json()
            assert len(pending_data["pending"]) == 1
            assert pending_data["pending"][0]["request_id"] == request_id

            # 4. POST /api/remote/approve
            approve_resp = await client.post(
                "/api/remote/approve",
                json={"request_id": request_id},
                headers={"X-Synapse-Console": "1", "Origin": f"http://127.0.0.1:{server.port}"},
            )
            assert approve_resp.status == 200

            # 5. GET /api/remote/check-access
            check_resp = await client.get(f"/api/remote/check-access?request_id={request_id}")
            assert check_resp.status == 200
            check_data = await check_resp.json()
            assert check_data["status"] == "approved"
            assert "device_token" in check_data
            assert "synapse_web_session" in check_resp.cookies
            assert "synapse_device_token" in check_resp.cookies

            # 6. GET /api/remote/devices
            devices_resp = await client.get("/api/remote/devices")
            assert devices_resp.status == 200
            devices_data = await devices_resp.json()
            assert len(devices_data["devices"]) == 1
            assert devices_data["devices"][0]["device_id"] == "phone-1"

            # 7. POST /api/remote/revoke-device
            revoke_resp = await client.post(
                "/api/remote/revoke-device",
                json={"device_id": "phone-1"},
                headers={"X-Synapse-Console": "1", "Origin": f"http://127.0.0.1:{server.port}"},
            )
            assert revoke_resp.status == 200
            devices_resp2 = await client.get("/api/remote/devices")
            devices_data2 = await devices_resp2.json()
            assert len(devices_data2["devices"]) == 0

        finally:
            await host.close()
            await client.close()

    asyncio.run(_run_test())


def test_remote_toggle_dynamic_site_binding(tmp_path: Path) -> None:
    from aiohttp import ClientSession

    workspace = tmp_path / "w"
    workspace.mkdir()
    (workspace / "index.html").write_text("<html></html>", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    token_file = state_dir / "token"
    token_file.write_text("test-token", encoding="utf-8")

    config = WebConsoleConfig(
        workspace=workspace,
        static_dir=workspace,
        state_dir=state_dir,
        host="127.0.0.1",
        port=0,
        allow_remote=False,
        pairing_required=False,
    )
    project = ProjectView(
        project_id="proj_1",
        workspace_path=str(workspace),
        name="test-project",
        git_branch=None,
    )
    host = WebConsoleHost(config, project, token="test-token")

    async def _run_test() -> None:
        meta = await host.start()
        bound_port = meta["port"]
        assert host.bound is not None
        assert host.bound[0] == "127.0.0.1"
        assert host.bound[1] == bound_port
        assert host.remote_enabled is False

        headers = {
            "X-Synapse-Console": "1",
            "Origin": f"http://127.0.0.1:{bound_port}",
            "Content-Type": "application/json",
        }

        try:
            async with ClientSession() as session:
                # 1. Toggle ON -> should switch to 0.0.0.0 on same port
                async with session.post(
                    f"http://127.0.0.1:{bound_port}/api/remote/toggle",
                    json={"enabled": True},
                    headers=headers,
                ) as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["remote_enabled"] is True
                assert host.bound[0] == "0.0.0.0"
                assert host.bound[1] == bound_port

                # 2. Toggle OFF -> should switch back to 127.0.0.1 on same port
                async with session.post(
                    f"http://127.0.0.1:{bound_port}/api/remote/toggle",
                    json={"enabled": False},
                    headers=headers,
                ) as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["remote_enabled"] is False
                assert host.bound[0] == "127.0.0.1"
                assert host.bound[1] == bound_port
        finally:
            await host.close()

    asyncio.run(_run_test())
