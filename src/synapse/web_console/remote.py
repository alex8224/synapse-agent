"""Remote access, device trust registry, and pairing approval manager for Web Console."""

from __future__ import annotations

import json
import logging
import secrets
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TRUST_TTL_SECONDS = 30 * 24 * 3600  # 30 days
DEFAULT_REQUEST_TTL_SECONDS = 300  # 5 minutes


@dataclass(slots=True)
class DeviceInfo:
    """A registered trusted client device."""

    device_id: str
    name: str
    device_token: str
    ip: str = ""
    user_agent: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    last_seen_at: float = 0.0

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current > self.expires_at

    def to_public_dict(self) -> dict[str, Any]:
        """Dictionary without the secret token for UI display."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_seen_at": self.last_seen_at,
        }


@dataclass(slots=True)
class PendingRequest:
    """A pairing request initiated by an untrusted remote browser."""

    request_id: str
    device_id: str
    name: str
    code: str
    ip: str = ""
    user_agent: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    status: str = "pending"  # "pending" | "approved" | "rejected"
    device_token: str | None = None

    def is_expired(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        return current > self.expires_at

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "device_id": self.device_id,
            "name": self.name,
            "code": self.code,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class DeviceRegistry:
    """Persistent registry of trusted remote devices."""

    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path
        self._devices_by_token: dict[str, DeviceInfo] = {}
        self._devices_by_id: dict[str, DeviceInfo] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_path is None or not self.storage_path.is_file():
            return
        try:
            raw = self.storage_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            now = time.time()
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "device_token" in item:
                        dev = DeviceInfo(**item)
                        if not dev.is_expired(now):
                            self._devices_by_token[dev.device_token] = dev
                            self._devices_by_id[dev.device_id] = dev
        except Exception:
            logger.exception("Failed to load trusted devices from %s", self.storage_path)

    def _save(self) -> None:
        if self.storage_path is None:
            return
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            active = [
                asdict(dev)
                for dev in self._devices_by_token.values()
                if not dev.is_expired()
            ]
            self.storage_path.write_text(json.dumps(active, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("Failed to save trusted devices to %s", self.storage_path)

    def register(
        self,
        device_id: str,
        name: str,
        *,
        ip: str = "",
        user_agent: str = "",
        ttl_seconds: float = DEFAULT_TRUST_TTL_SECONDS,
        now: float | None = None,
    ) -> DeviceInfo:
        current = time.time() if now is None else now
        # Revoke existing device with same id if any
        if device_id in self._devices_by_id:
            old = self._devices_by_id.pop(device_id)
            self._devices_by_token.pop(old.device_token, None)

        token = secrets.token_urlsafe(32)
        dev = DeviceInfo(
            device_id=device_id,
            name=name,
            device_token=token,
            ip=ip,
            user_agent=user_agent,
            created_at=current,
            expires_at=current + ttl_seconds,
            last_seen_at=current,
        )
        self._devices_by_token[token] = dev
        self._devices_by_id[device_id] = dev
        self._save()
        return dev

    def verify_token(self, token: str | None, *, now: float | None = None) -> DeviceInfo | None:
        if not token or token not in self._devices_by_token:
            return None
        current = time.time() if now is None else now
        dev = self._devices_by_token[token]
        if dev.is_expired(current):
            self.revoke_token(token)
            return None
        dev.last_seen_at = current
        return dev

    def revoke(self, device_id: str) -> bool:
        dev = self._devices_by_id.pop(device_id, None)
        if dev:
            self._devices_by_token.pop(dev.device_token, None)
            self._save()
            return True
        return False

    def revoke_token(self, token: str) -> bool:
        dev = self._devices_by_token.pop(token, None)
        if dev:
            self._devices_by_id.pop(dev.device_id, None)
            self._save()
            return True
        return False

    def list_devices(self, *, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        return [
            dev.to_public_dict()
            for dev in self._devices_by_id.values()
            if not dev.is_expired(current)
        ]

    def clear(self) -> int:
        count = len(self._devices_by_id)
        self._devices_by_id.clear()
        self._devices_by_token.clear()
        self._save()
        return count


class PendingPairingManager:
    """In-memory manager for pending remote pairing requests."""

    def __init__(self) -> None:
        self._requests: dict[str, PendingRequest] = {}
        self._by_device_id: dict[str, str] = {}

    def _cleanup(self, now: float) -> None:
        expired = [r_id for r_id, req in self._requests.items() if req.is_expired(now)]
        for r_id in expired:
            req = self._requests.pop(r_id, None)
            if req:
                self._by_device_id.pop(req.device_id, None)

    def create_request(
        self,
        device_id: str,
        name: str,
        *,
        ip: str = "",
        user_agent: str = "",
        ttl_seconds: float = DEFAULT_REQUEST_TTL_SECONDS,
        now: float | None = None,
    ) -> PendingRequest:
        current = time.time() if now is None else now
        self._cleanup(current)

        # If previous pending request exists for this device, remove it
        if device_id in self._by_device_id:
            old_id = self._by_device_id.pop(device_id)
            self._requests.pop(old_id, None)

        request_id = secrets.token_urlsafe(16)
        # 4-digit human-friendly verification PIN (e.g. "4921")
        code = f"{secrets.randbelow(9000) + 1000}"

        req = PendingRequest(
            request_id=request_id,
            device_id=device_id,
            name=name,
            code=code,
            ip=ip,
            user_agent=user_agent,
            created_at=current,
            expires_at=current + ttl_seconds,
            status="pending",
        )
        self._requests[request_id] = req
        self._by_device_id[device_id] = req.request_id
        return req

    def get_request(self, request_id: str, *, now: float | None = None) -> PendingRequest | None:
        current = time.time() if now is None else now
        req = self._requests.get(request_id)
        if req and req.is_expired(current):
            self._requests.pop(request_id, None)
            self._by_device_id.pop(req.device_id, None)
            return None
        return req

    def get_by_device_id(
        self, device_id: str, *, now: float | None = None
    ) -> PendingRequest | None:
        req_id = self._by_device_id.get(device_id)
        if not req_id:
            return None
        return self.get_request(req_id, now=now)

    def list_pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        self._cleanup(current)
        return [
            req.to_public_dict()
            for req in self._requests.values()
            if req.status == "pending" and not req.is_expired(current)
        ]

    def approve(
        self,
        request_id: str,
        registry: DeviceRegistry,
        *,
        ttl_seconds: float = DEFAULT_TRUST_TTL_SECONDS,
        now: float | None = None,
    ) -> tuple[PendingRequest | None, str | None]:
        current = time.time() if now is None else now
        req = self.get_request(request_id, now=current)
        if not req or req.status != "pending":
            return None, None

        dev = registry.register(
            device_id=req.device_id,
            name=req.name,
            ip=req.ip,
            user_agent=req.user_agent,
            ttl_seconds=ttl_seconds,
            now=current,
        )
        req.status = "approved"
        req.device_token = dev.device_token
        return req, dev.device_token

    def reject(self, request_id: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        req = self.get_request(request_id, now=current)
        if not req or req.status != "pending":
            return False
        req.status = "rejected"
        return True


def get_lan_ips() -> list[str]:
    """Return available IPv4 addresses on local network interfaces."""
    ips = set()
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
            if primary and not primary.startswith("127."):
                ips.add(primary)
    except Exception:
        pass
    return sorted(ips)
