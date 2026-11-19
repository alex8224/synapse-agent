"""Stdio MCP bridge for external ACP agents.

Bridges:
- ``streamable_http`` / ``http`` / ``sse`` remote MCP servers to ``stdio`` when
  an external ACP agent does not advertise ``http`` / ``sse`` MCP capabilities.
- ``stdio`` MCP servers when tool input schemas need sanitization (e.g. removing
  empty ``enum`` values rejected by Gemini's ``GenerateContentRequest``).

Why this module owns stdin/stdout framing and Streamable HTTP directly instead
of delegating to ``mcp.server.stdio.stdio_server`` + ``streamablehttp_client``:
- On Windows, ``stdio_server`` reads ``stdin`` inside a non-daemon ``anyio``
  worker thread; if the remote transport errors while ``stdin`` stays open,
  shutdown deadlocks waiting for ``ReadFile`` to return, leaving a zombie proxy
  whose open ``stdout`` pipe makes the ACP agent hang until its tool timeout.
- ``mcp.client.streamable_http`` uses a 300s read timeout on plain JSON POSTs,
  has a loop-variable capture hazard in ``post_writer``, and aborts the whole
  task group on a single stale keep-alive socket (``CloseWait``) or HTTP error
  instead of retrying or returning a JSON-RPC error response.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from httpx_sse import EventSource
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest, JSONRPCResponse

from synapse.integrations.mcp_client import _make_http_client_factory, _resolve_proxy

HEADERS_ENV_VAR = "SYNAPSE_MCP_PROXY_HEADERS_JSON"
STDIO_TARGET_ENV_VAR = "SYNAPSE_MCP_PROXY_STDIO_JSON"
PROXY_URL_ENV_VAR = "SYNAPSE_MCP_PROXY_URL"
_INTERNAL_ENV_VARS = frozenset({HEADERS_ENV_VAR, STDIO_TARGET_ENV_VAR, PROXY_URL_ENV_VAR})
_RETRYABLE_HTTP_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
)
_MAX_ERROR_BODY_CHARS = 512
_LOGGER = logging.getLogger(__name__)


def _parse_headers_env(raw: str | None) -> dict[str, str] | None:
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{HEADERS_ENV_VAR} must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


def _parse_stdio_target_env(raw: str | None) -> tuple[str, list[str]]:
    if not raw:
        raise ValueError(f"{STDIO_TARGET_ENV_VAR} is required for stdio transport")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("command"), str):
        raise ValueError(f"{STDIO_TARGET_ENV_VAR} must be a JSON object with 'command'")
    args = parsed.get("args") or []
    if not isinstance(args, list):
        raise ValueError(f"{STDIO_TARGET_ENV_VAR}.args must be a list")
    return parsed["command"], [str(item) for item in args]


def sanitize_tool_input_schema(node: Any) -> Any:
    """Recursively sanitize JSON Schema nodes for strict providers like Gemini.

    Gemini's ``Schema`` rejects empty strings in ``enum`` (``enum[0]: cannot be
    empty``), empty ``enum`` lists, and boolean schemas carrying ``enum``.
    """
    if isinstance(node, list):
        return [sanitize_tool_input_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    sanitized: dict[str, Any] = {
        key: sanitize_tool_input_schema(value) for key, value in node.items()
    }
    if "enum" in sanitized and isinstance(sanitized["enum"], list):
        enum_values = sanitized["enum"]
        if sanitized.get("type") == "boolean" or (
            enum_values and all(isinstance(v, bool) for v in enum_values)
        ):
            sanitized.pop("enum", None)
            sanitized.setdefault("type", "boolean")
        else:
            cleaned = [v for v in enum_values if v is not None and v != ""]
            if cleaned:
                sanitized["enum"] = cleaned
            else:
                sanitized.pop("enum", None)
    return sanitized


def _sanitize_tools_list_payload(payload: dict[str, Any]) -> None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return
    tools = result.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("inputSchema"), dict):
            tool["inputSchema"] = sanitize_tool_input_schema(tool["inputSchema"])


def _sanitize_tools_list_message(
    item: SessionMessage,
    pending_tools_list_ids: set[str | int],
) -> None:
    root = item.message.root
    if not isinstance(root, JSONRPCResponse):
        return
    if root.id not in pending_tools_list_ids:
        return
    pending_tools_list_ids.discard(root.id)
    if not isinstance(root.result, dict):
        return
    tools = root.result.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("inputSchema"), dict):
            tool["inputSchema"] = sanitize_tool_input_schema(tool["inputSchema"])


def _start_stdin_reader(loop: asyncio.AbstractEventLoop) -> asyncio.Queue[str | None]:
    """Read UTF-8 JSON-RPC lines from stdin on a daemon thread so exit never hangs."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _reader() -> None:
        raw_stdin = sys.stdin.buffer
        try:
            while True:
                raw_line = raw_stdin.readline()
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, text)
        except Exception as exc:  # noqa: BLE001 - treat broken stdin pipe as EOF
            _LOGGER.warning("MCP stdio proxy stdin read stopped: %s", exc)
        finally:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except RuntimeError:
                pass

    thread = threading.Thread(target=_reader, name="mcp-stdio-proxy-stdin", daemon=True)
    thread.start()
    return queue


class _StdoutWriter:
    """Serialized, immediately flushed UTF-8 NDJSON writer for MCP stdout."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def write_json(self, payload: dict[str, Any]) -> None:
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        await self.write_bytes(encoded)

    async def write_bytes(self, data: bytes) -> None:
        async with self._lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host in {"127.0.0.1", "localhost", "::1"}


def _jsonrpc_error(req_id: Any, message: str, *, code: int = -32000) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


async def _run_streamable_http_proxy(
    url: str,
    headers: dict[str, str] | None,
    *,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> None:
    """Bridge NDJSON stdio directly to a Streamable HTTP MCP server."""
    loop = asyncio.get_running_loop()
    stdin_queue = _start_stdin_reader(loop)
    stdout = _StdoutWriter()

    bypass_system_proxy = proxy is None and _is_loopback_url(url)
    resolved_proxy = None if bypass_system_proxy else _resolve_proxy(proxy)
    read_timeout = max(float(timeout), 60.0)
    connect_timeout = min(max(float(timeout), 5.0), 15.0)
    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "trust_env": not bypass_system_proxy,
        "timeout": httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=30.0,
            pool=15.0,
        ),
        "limits": httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=20.0,
        ),
    }
    if headers:
        client_kwargs["headers"] = dict(headers)
    if resolved_proxy:
        client_kwargs["proxy"] = resolved_proxy

    session_id: str | None = None
    protocol_version: str | None = None
    last_init_payload: dict[str, Any] | None = None
    reinit_lock = asyncio.Lock()
    in_flight: set[asyncio.Task[None]] = set()

    def _request_headers() -> dict[str, str]:
        req_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if session_id:
            req_headers["mcp-session-id"] = session_id
        if protocol_version:
            req_headers["mcp-protocol-version"] = protocol_version
        return req_headers

    def _capture_protocol_version(resp_payload: dict[str, Any]) -> None:
        nonlocal protocol_version
        result = resp_payload.get("result")
        if isinstance(result, dict):
            version = result.get("protocolVersion")
            if isinstance(version, str) and version:
                protocol_version = version

    async def _reinitialize_session(client: httpx.AsyncClient) -> bool:
        nonlocal session_id
        if last_init_payload is None:
            return False
        async with reinit_lock:
            session_id = None
            try:
                async with client.stream(
                    "POST", url, json=last_init_payload, headers=_request_headers()
                ) as resp:
                    if resp.status_code >= 400:
                        return False
                    new_sid = resp.headers.get("mcp-session-id")
                    if new_sid:
                        session_id = new_sid
                    await resp.aread()
                # Send initialized notification best-effort after re-init.
                try:
                    await client.post(
                        url,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers=_request_headers(),
                    )
                except Exception:  # noqa: BLE001 - best-effort notification
                    pass
                return True
            except Exception as exc:  # noqa: BLE001 - fall back to error response
                _LOGGER.warning("MCP stdio proxy session re-init failed: %s", exc)
                return False

    async def _send_and_emit(
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        allow_reinit: bool = True,
    ) -> None:
        nonlocal session_id, last_init_payload
        method = payload.get("method")
        has_id = "id" in payload and payload["id"] is not None
        req_id = payload.get("id")
        is_init = method == "initialize"
        is_tools_list = method == "tools/list"
        if is_init:
            last_init_payload = payload

        last_exc: Exception | None = None
        for attempt in range(2):
            emitted_any = False
            try:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=_request_headers(),
                ) as response:
                    if response.status_code == 202:
                        return
                    if response.status_code == 404 and session_id and allow_reinit and not is_init:
                        if await _reinitialize_session(client):
                            await _send_and_emit(client, payload, allow_reinit=False)
                            return
                        if has_id:
                            await stdout.write_json(
                                _jsonrpc_error(req_id, "MCP HTTP session terminated (HTTP 404)")
                            )
                        return
                    if response.status_code >= 400:
                        raw_err = (await response.aread()).decode("utf-8", errors="replace").strip()
                        snippet = raw_err[:_MAX_ERROR_BODY_CHARS]
                        detail = f"HTTP {response.status_code}" + (
                            f": {snippet}" if snippet else ""
                        )
                        _LOGGER.warning("MCP stdio proxy upstream error: %s", detail)
                        if has_id:
                            await stdout.write_json(_jsonrpc_error(req_id, detail))
                        return

                    if is_init:
                        new_sid = response.headers.get("mcp-session-id")
                        if new_sid:
                            session_id = new_sid

                    if not has_id:
                        await response.aread()
                        return

                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith("application/json"):
                        body = await response.aread()
                        resp_payload = json.loads(body.decode("utf-8"))
                        if isinstance(resp_payload, dict):
                            if is_init:
                                _capture_protocol_version(resp_payload)
                            if is_tools_list:
                                _sanitize_tools_list_payload(resp_payload)
                            await stdout.write_json(resp_payload)
                        return

                    if content_type.startswith("text/event-stream"):
                        event_source = EventSource(response)
                        async for sse in event_source.aiter_sse():
                            if sse.event != "message" or not sse.data:
                                continue
                            resp_payload = json.loads(sse.data)
                            if not isinstance(resp_payload, dict):
                                continue
                            if is_init:
                                _capture_protocol_version(resp_payload)
                            if is_tools_list:
                                _sanitize_tools_list_payload(resp_payload)
                            await stdout.write_json(resp_payload)
                            emitted_any = True
                            if "result" in resp_payload or "error" in resp_payload:
                                return
                        if not emitted_any:
                            await stdout.write_json(
                                _jsonrpc_error(req_id, "Upstream SSE stream ended without response")
                            )
                        return

                    raw_body = (await response.aread()).decode("utf-8", errors="replace").strip()
                    await stdout.write_json(
                        _jsonrpc_error(
                            req_id,
                            f"Unexpected MCP content-type {content_type!r}: "
                            f"{raw_body[:_MAX_ERROR_BODY_CHARS]}",
                        )
                    )
                    return
            except _RETRYABLE_HTTP_ERRORS as exc:
                last_exc = exc
                if attempt == 0 and not emitted_any:
                    _LOGGER.warning(
                        "MCP stdio proxy transient transport error (%s), retrying once",
                        type(exc).__name__,
                    )
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - isolate per-request failures
                last_exc = exc
                break

        if last_exc is not None:
            _LOGGER.warning(
                "MCP stdio proxy request %s failed: %s: %s",
                method,
                type(last_exc).__name__,
                last_exc,
            )
            if has_id:
                await stdout.write_json(
                    _jsonrpc_error(req_id, f"{type(last_exc).__name__}: {last_exc}")
                )

    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            while True:
                line = await stdin_queue.get()
                if line is None:
                    break
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning("MCP stdio proxy ignored invalid JSON on stdin: %s", exc)
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning("MCP stdio proxy ignored non-object JSON-RPC message")
                    continue
                task = asyncio.create_task(_send_and_emit(client, payload))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            if in_flight:
                for task in list(in_flight):
                    task.cancel()
                await asyncio.gather(*in_flight, return_exceptions=True)
            if session_id:
                try:
                    await client.delete(url, headers=_request_headers(), timeout=2.0)
                except Exception:  # noqa: BLE001 - best-effort cleanup on shutdown
                    pass


async def _pump_remote_streams(
    remote_read: MemoryObjectReceiveStream[SessionMessage | Exception],
    remote_write: MemoryObjectSendStream[SessionMessage],
) -> None:
    """Bridge daemon-thread stdin/stdout with anyio MCP client streams (stdio/sse)."""
    loop = asyncio.get_running_loop()
    stdin_queue = _start_stdin_reader(loop)
    stdout = _StdoutWriter()
    pending_tools_list_ids: set[str | int] = set()

    async with anyio.create_task_group() as tg:

        async def forward_stdin_to_remote() -> None:
            try:
                async with remote_write:
                    while True:
                        line = await stdin_queue.get()
                        if line is None:
                            break
                        try:
                            message = JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:  # noqa: BLE001 - return error instead of stalling
                            _LOGGER.warning("MCP stdio proxy stdin parse error: %s", exc)
                            try:
                                raw = json.loads(line)
                                if isinstance(raw, dict) and raw.get("id") is not None:
                                    err = _jsonrpc_error(
                                        raw["id"], f"Invalid JSON-RPC message: {exc}"
                                    )
                                    await stdout.write_json(err)
                            except Exception:  # noqa: BLE001
                                pass
                            continue
                        root = message.root
                        if isinstance(root, JSONRPCRequest) and root.method == "tools/list":
                            pending_tools_list_ids.add(root.id)
                        await remote_write.send(SessionMessage(message))
            finally:
                tg.cancel_scope.cancel()

        async def forward_remote_to_stdout() -> None:
            try:
                async for item in remote_read:
                    if isinstance(item, Exception):
                        _LOGGER.warning("MCP stdio proxy remote error: %s", item)
                        break
                    _sanitize_tools_list_message(item, pending_tools_list_ids)
                    encoded = (
                        item.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                    ).encode("utf-8")
                    await stdout.write_bytes(encoded)
            finally:
                tg.cancel_scope.cancel()

        tg.start_soon(forward_stdin_to_remote)
        tg.start_soon(forward_remote_to_stdout)


async def run_proxy(
    transport: str,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    stdio_target: tuple[str, list[str]] | None = None,
    *,
    proxy: str | None = None,
    timeout: float = 60.0,
) -> None:
    """Bridge stdio MCP traffic to a remote HTTP/SSE or child stdio MCP server."""
    normalized = transport.casefold()
    if normalized in {"streamable_http", "http"}:
        if not url:
            raise ValueError(f"--url is required for {transport!r} transport")
        await _run_streamable_http_proxy(url, headers, proxy=proxy, timeout=timeout)
        return
    if normalized == "stdio":
        if stdio_target is None:
            raise ValueError("stdio_target is required for stdio transport")
        command, args = stdio_target
        child_env = {k: v for k, v in os.environ.items() if k not in _INTERNAL_ENV_VARS}
        params = StdioServerParameters(command=command, args=args, env=child_env)
        async with stdio_client(params) as (remote_read, remote_write):
            await _pump_remote_streams(remote_read, remote_write)
        return
    if normalized == "sse":
        if not url:
            raise ValueError("--url is required for 'sse' transport")
        bypass_system_proxy = proxy is None and _is_loopback_url(url)
        if bypass_system_proxy:

            def client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                kwargs: dict[str, Any] = {"follow_redirects": True, "trust_env": False}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                if headers is not None:
                    kwargs["headers"] = headers
                if auth is not None:
                    kwargs["auth"] = auth
                return httpx.AsyncClient(**kwargs)
        else:
            client_factory = _make_http_client_factory(proxy)
        async with sse_client(
            url,
            headers=headers,
            httpx_client_factory=client_factory,
            timeout=max(float(timeout), 15.0),
        ) as (remote_read, remote_write):
            await _pump_remote_streams(remote_read, remote_write)
        return
    raise ValueError(f"Unsupported MCP proxy transport: {transport!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the stdio MCP proxy."""
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="synapse-mcp-stdio-proxy")
    parser.add_argument(
        "--transport",
        required=True,
        choices=["streamable_http", "http", "sse", "stdio"],
    )
    parser.add_argument("--url", default=None)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    headers = _parse_headers_env(os.environ.get(HEADERS_ENV_VAR))
    proxy_url = os.environ.get(PROXY_URL_ENV_VAR) or None
    stdio_target = (
        _parse_stdio_target_env(os.environ.get(STDIO_TARGET_ENV_VAR))
        if args.transport == "stdio"
        else None
    )
    try:
        asyncio.run(
            run_proxy(
                args.transport,
                args.url,
                headers,
                stdio_target,
                proxy=proxy_url,
                timeout=args.timeout,
            )
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - surface proxy startup/transport failure on stderr
        _LOGGER.error("MCP stdio proxy failed (%s): %s", args.transport, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
