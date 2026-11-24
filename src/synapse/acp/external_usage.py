"""Read-only quota / usage reader for supported external ACP agents (e.g. ``agy``).

Runs the agent's own documented CLI status command (``agy --output-format json
--print=/usage``) inside the daemon using the configured executable and proxy
environment from ``external-acp-agents.json``.  No OAuth token, account id,
command path, proxy URL, or raw stderr ever crosses the boundary to clients.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from synapse.runtime.service.external_agents import LocalExternalAgent
from synapse.runtime.service.external_ports import (
    ExternalUsageBucket,
    ExternalUsageGroup,
    ExternalUsageSnapshot,
)

__all__ = [
    "ExternalUsageCache",
    "EXTERNAL_USAGE_CACHE_TTL_S",
    "is_agy_agent",
    "parse_agy_usage_json",
]

#: Default server-side cache TTL (matches Codex usage's 300s cadence).
EXTERNAL_USAGE_CACHE_TTL_S = 300.0
_SUBPROCESS_TIMEOUT_S = 20.0
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_GROUPS = 16
_MAX_BUCKETS = 16
_MAX_TIMESTAMP = 4_102_444_800.0
_MAX_NAME_LEN = 128
_MAX_DESC_LEN = 512
_MAX_ID_LEN = 128
_MAX_WINDOW_LEN = 32

_WINDOW_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)

CommandRunner = Callable[[LocalExternalAgent], Awaitable[bytes]]


def is_agy_agent(agent: LocalExternalAgent) -> bool:
    """Whether *agent* is an Antigravity CLI (``agy``) executable."""
    if agent.info.id.strip().lower() == "agy":
        return True
    stem = Path(agent.command).stem.strip().lower()
    return stem == "agy"


def _bounded_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\x00", "").strip()
    if len(cleaned) > maximum:
        return cleaned[:maximum]
    return cleaned


def _parse_window_minutes(window: str) -> int | None:
    key = window.strip().lower()
    if not key:
        return None
    if key in {"weekly", "week", "1w", "7d"}:
        return 7 * 24 * 60
    if key in {"daily", "day", "1d", "24h"}:
        return 24 * 60
    match = _WINDOW_RE.match(key)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if amount <= 0:
        return None
    factor = {"m": 1, "h": 60, "d": 24 * 60, "w": 7 * 24 * 60}[unit]
    minutes = amount * factor
    return minutes if 1 <= minutes <= 527_040 else None


def _parse_iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ts = dt.timestamp()
    if not math.isfinite(ts) or ts < 0 or ts > _MAX_TIMESTAMP:
        return None
    return ts


def _parse_remaining_percent(raw_fraction: object) -> float | None:
    if isinstance(raw_fraction, bool) or not isinstance(raw_fraction, (int, float)):
        return None
    fraction = float(raw_fraction)
    if not math.isfinite(fraction):
        return None
    clamped = min(max(fraction, 0.0), 1.0)
    return round(clamped * 100.0, 2)


def _parse_bucket(row: Any, fallback_idx: int) -> ExternalUsageBucket | None:
    if not isinstance(row, dict):
        return None
    window = _bounded_text(row.get("window"), maximum=_MAX_WINDOW_LEN)
    name = _bounded_text(row.get("name"), maximum=_MAX_NAME_LEN)
    bucket_id = _bounded_text(row.get("id"), maximum=_MAX_ID_LEN) or f"bucket-{fallback_idx}"
    if not name and not window:
        return None
    return ExternalUsageBucket(
        id=bucket_id,
        name=name or window,
        window=window,
        window_minutes=_parse_window_minutes(window),
        remaining_percent=_parse_remaining_percent(row.get("remaining_fraction")),
        reset_at=_parse_iso_timestamp(row.get("reset_time")),
    )


def _parse_tsv_fallback(text: str) -> tuple[ExternalUsageGroup, ...]:
    """Fallback parser for ``agy --print /usage`` tab-separated lines."""
    grouped: dict[str, list[ExternalUsageBucket]] = {}
    for idx, line in enumerate(text.splitlines()):
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 3:
            continue
        group_name = _bounded_text(parts[0], maximum=_MAX_NAME_LEN)
        bucket_name = _bounded_text(parts[1], maximum=_MAX_NAME_LEN)
        pct_text = parts[2].rstrip("%").strip()
        reset_text = parts[3] if len(parts) >= 4 else None
        if not group_name or not bucket_name:
            continue
        remaining: float | None = None
        try:
            parsed = float(pct_text)
            if math.isfinite(parsed):
                remaining = round(min(max(parsed, 0.0), 100.0), 2)
        except ValueError:
            remaining = None
        lower_name = bucket_name.lower()
        window = "5h" if "five hour" in lower_name or "5h" in lower_name else (
            "weekly" if "weekly" in lower_name else ""
        )
        bucket = ExternalUsageBucket(
            id=f"tsv-{idx}",
            name=bucket_name,
            window=window,
            window_minutes=_parse_window_minutes(window),
            remaining_percent=remaining,
            reset_at=_parse_iso_timestamp(reset_text),
        )
        grouped.setdefault(group_name, []).append(bucket)
    groups: list[ExternalUsageGroup] = []
    for name, buckets in list(grouped.items())[:_MAX_GROUPS]:
        groups.append(
            ExternalUsageGroup(
                name=name,
                description=None,
                buckets=tuple(buckets[:_MAX_BUCKETS]),
            )
        )
    return tuple(groups)


def parse_agy_usage_json(raw: bytes | str) -> tuple[ExternalUsageGroup, ...]:
    """Decode ``agy --output-format json --print=/usage`` stdout into bounded groups."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return ()
    try:
        payload = json.loads(text)
    except ValueError:
        return _parse_tsv_fallback(text)
    if not isinstance(payload, dict):
        return ()
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    raw_groups = data.get("groups") if isinstance(data, dict) else None
    if isinstance(raw_groups, list):
        groups: list[ExternalUsageGroup] = []
        for entry in raw_groups[:_MAX_GROUPS]:
            if not isinstance(entry, dict):
                continue
            name = _bounded_text(entry.get("name"), maximum=_MAX_NAME_LEN)
            if not name:
                continue
            desc_raw = _bounded_text(entry.get("description"), maximum=_MAX_DESC_LEN)
            raw_buckets = entry.get("buckets")
            buckets: list[ExternalUsageBucket] = []
            if isinstance(raw_buckets, list):
                for idx, bucket_row in enumerate(raw_buckets[:_MAX_BUCKETS]):
                    bucket = _parse_bucket(bucket_row, idx)
                    if bucket is not None:
                        buckets.append(bucket)
            if buckets:
                groups.append(
                    ExternalUsageGroup(
                        name=name,
                        description=desc_raw or None,
                        buckets=tuple(buckets),
                    )
                )
        if groups:
            return tuple(groups)
    response_text = payload.get("response")
    if isinstance(response_text, str) and response_text.strip():
        return _parse_tsv_fallback(response_text)
    return ()


async def _run_agy_usage_command(agent: LocalExternalAgent) -> bytes:
    """Invoke ``agy --output-format json --print=/usage`` with the agent's proxy env."""
    env = dict(os.environ)
    for key, value in agent.proxy_env.items():
        env[key] = value
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = await asyncio.create_subprocess_exec(
        agent.command,
        "--output-format",
        "json",
        "--print=/usage",
        **kwargs,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_S)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 - best-effort reap on timeout
            pass
        raise RuntimeError("external usage request timed out") from None
    if proc.returncode != 0:
        raise RuntimeError("external usage request failed")
    return stdout[:_MAX_STDOUT_BYTES]


class ExternalUsageCache:
    """Per-agent serialized TTL cache for external ACP quota reads."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _run_agy_usage_command,
        clock: Callable[[], float] = time.time,
        ttl_s: float = EXTERNAL_USAGE_CACHE_TTL_S,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._ttl_s = ttl_s
        self._locks: dict[str, asyncio.Lock] = {}
        self._cached: dict[str, ExternalUsageSnapshot] = {}

    async def fetch(
        self, agent: LocalExternalAgent, *, force: bool = False
    ) -> ExternalUsageSnapshot:
        """Return the quota snapshot for *agent*, using the TTL cache when fresh."""
        if not is_agy_agent(agent):
            return ExternalUsageSnapshot(
                agent_id=agent.info.id,
                supported=False,
                groups=(),
                captured_at=self._clock(),
            )
        lock = self._locks.setdefault(agent.info.id, asyncio.Lock())
        async with lock:
            now = self._clock()
            existing = self._cached.get(agent.info.id)
            if (
                not force
                and existing is not None
                and (now - existing.captured_at) < self._ttl_s
            ):
                return existing
            raw = await self._runner(agent)
            groups = parse_agy_usage_json(raw)
            snapshot = ExternalUsageSnapshot(
                agent_id=agent.info.id,
                supported=True,
                groups=groups,
                captured_at=self._clock(),
            )
            self._cached[agent.info.id] = snapshot
            return snapshot
