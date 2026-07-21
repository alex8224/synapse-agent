"""MCP client adapter: discover remote tools and inject as LangChain tools.

deepagents has no MCP support. Extension path is create_deep_agent(tools=[...]).
Failures are non-fatal: missing servers degrade to an empty tool list + warnings.

Connections are kept alive on a dedicated asyncio loop thread so tool calls
do not re-spawn stdio/HTTP sessions every time.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"  # stdio | sse | streamable_http
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    tool_prefix: str | None = None


@dataclass
class McpLoadResult:
    tools: list[Any]
    warnings: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    pool: Any | None = None


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} or $VAR in strings (headers/url/command/env values)."""
    import os
    import re

    if not isinstance(value, str):
        return value
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

    def repl(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        return os.environ.get(key, "")

    return pattern.sub(repl, value)


def _expand_mapping(raw: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in dict(raw or {}).items():
        out[str(k)] = str(_expand_env(v))
    return out


def _parse_server(raw: dict[str, Any]) -> McpServerConfig:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("mcp server missing name")
    command = raw.get("command")
    url = raw.get("url")
    return McpServerConfig(
        name=name,
        transport=str(raw.get("transport") or "stdio").strip().lower(),
        command=_expand_env(command) if command is not None else None,
        args=[str(_expand_env(a)) for a in list(raw.get("args") or [])],
        env=_expand_mapping(raw.get("env")),
        url=_expand_env(url) if url is not None else None,
        headers=_expand_mapping(raw.get("headers")),
        enabled=bool(raw.get("enabled", True)),
        tool_prefix=raw.get("tool_prefix"),
    )


def load_mcp_server_configs(
    *,
    path: Path | str | None = None,
    paths: list[Path | str] | None = None,
    json_blob: str | None = None,
    workspace: Path | str | None = None,
) -> list[McpServerConfig]:
    """Load MCP servers from one or more JSON files (later overrides by name).

    When ``path``/``paths`` are unset, loads layered:
      ``~/.coding-agent/mcp.json`` then ``<workspace>/.coding-agent/mcp.json``.
    """
    file_paths: list[Path] = []
    if paths:
        file_paths.extend(Path(p).expanduser() for p in paths)
    if path is not None:
        file_paths.append(Path(path).expanduser())
    if not file_paths:
        from synapse.config_paths import mcp_config_paths

        file_paths = list(mcp_config_paths(workspace))

    by_name: dict[str, McpServerConfig] = {}
    for p in file_paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("mcp config %s: invalid JSON: %s", p, exc)
            continue
        for raw in _iter_server_dicts(data):
            cfg = _parse_server(raw)
            by_name[cfg.name] = cfg

    if json_blob and json_blob.strip():
        try:
            data = json.loads(json_blob)
        except json.JSONDecodeError as exc:
            logger.warning("mcp config from json_blob: invalid JSON: %s", exc)
            data = None
        if data is not None:
            for raw in _iter_server_dicts(data):
                cfg = _parse_server(raw)
                by_name[cfg.name] = cfg

    return list(by_name.values())


def _iter_server_dicts(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, dict):
        servers = data.get("servers")
        if isinstance(servers, list):
            return [s for s in servers if isinstance(s, dict)]
        # single server object
        if "name" in data:
            return [data]
        return []
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    return []


def _json_schema_to_args(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON schema object suitable for StructuredTool args."""
    if not schema:
        return {"type": "object", "properties": {}}
    if schema.get("type") == "object" or "properties" in schema:
        # Ensure type=object even when only properties/required are present.
        out = dict(schema)
        out.setdefault("type", "object")
        out.setdefault("properties", {})
        return out
    return {"type": "object", "properties": {"input": schema}}


def _annotation_for_prop(prop: Any) -> Any:
    """Map a JSON-schema property fragment to a loose Python annotation."""
    if not isinstance(prop, dict):
        return Any
    t = prop.get("type")
    if isinstance(t, list):
        # e.g. ["string", "null"]
        non_null = [x for x in t if x != "null"]
        base = _annotation_for_prop({**prop, "type": non_null[0] if non_null else "string"})
        return base | None if "null" in t else base
    if t == "string":
        return str
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        items = prop.get("items")
        return list[_annotation_for_prop(items)] if isinstance(items, dict) else list[Any]
    if t == "object" or "properties" in prop or prop.get("additionalProperties") is not None:
        return dict[str, Any]
    # anyOf / oneOf / $ref / missing type → keep open
    return Any


def json_schema_to_pydantic_model(
    name: str,
    schema: dict[str, Any] | None,
) -> type[Any]:
    """Build a flat pydantic model from MCP JSON Schema.

    Passing a raw dict to ``StructuredTool.from_function(args_schema=...)`` can
    collapse into a single ``root: anyOf[...]`` field (RootModel-like). Models
    then invent wrong arguments and MCP calls fail repeatedly.
    """
    from pydantic import ConfigDict, Field, create_model

    schema_obj = _json_schema_to_args(schema)
    props = schema_obj.get("properties") or {}
    required = set(schema_obj.get("required") or [])
    if not isinstance(props, dict):
        props = {}

    field_defs: dict[str, Any] = {}
    for key, prop in props.items():
        key_s = str(key)
        annotation = _annotation_for_prop(prop)
        desc = ""
        if isinstance(prop, dict):
            desc = str(prop.get("description") or "")
        if key_s in required:
            field_defs[key_s] = (annotation, Field(description=desc or key_s))
        else:
            # Optional: default None so model may omit the field.
            field_defs[key_s] = (
                annotation | None,
                Field(default=None, description=desc or key_s),
            )

    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name) or "mcp_tool"
    model_name = f"{safe}_input"
    # extra=allow keeps forward-compatible MCP args / free-form objects.
    return create_model(
        model_name,
        __config__=ConfigDict(extra="allow"),
        **field_defs,
    )


def _content_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        else:
            parts.append(str(block))
    if getattr(result, "isError", False):
        return "MCP error: " + ("\n".join(parts) or "unknown")
    return "\n".join(parts) if parts else "(empty MCP result)"


def _make_tool(
    *,
    server: McpServerConfig,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any] | None,
    call_fn,
):
    from langchain_core.tools import StructuredTool

    prefix = server.tool_prefix if server.tool_prefix is not None else f"{server.name}__"
    full_name = f"{prefix}{tool_name}" if prefix else tool_name
    safe_name = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in full_name)
    args_model = json_schema_to_pydantic_model(safe_name, input_schema)

    def _invoke(**kwargs: Any) -> str:
        # Drop explicit Nones so optional MCP fields stay omitted.
        arguments = {k: v for k, v in kwargs.items() if v is not None}
        return call_fn(tool_name, arguments)

    return StructuredTool.from_function(
        func=_invoke,
        name=safe_name,
        description=description or f"MCP tool {tool_name} from {server.name}",
        args_schema=args_model,
    )


class _LoopThread:
    """Background event loop for long-lived async MCP sessions."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="coding-agent-mcp-loop",
            daemon=True,
        )
        self._started = threading.Event()
        self._thread.start()
        self._started.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def submit(self, coro) -> Future:
        if not self._thread.is_alive():
            raise RuntimeError("MCP event loop thread is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self, coro, timeout: float | None = 120.0) -> Any:
        fut = self.submit(coro)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            # Attempt to cancel the still-running coroutine on the event loop
            # so we don't leak subprocesses / connections.
            fut.cancel()
            raise

    def stop(self) -> None:
        if not self._thread.is_alive():
            return

        def _stop() -> None:
            self._loop.stop()

        self._loop.call_soon_threadsafe(_stop)
        self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class _LiveServer:
    config: McpServerConfig
    session: Any
    # Keep transport context managers open for process lifetime.
    transport_cm: Any
    session_cm: Any
    streams: Any = None


class McpSessionPool:
    """Process-local pool of live MCP ClientSessions."""

    def __init__(self) -> None:
        self._loop = _LoopThread()
        self._servers: dict[str, _LiveServer] = {}
        self._closed = False
        self.warnings: list[str] = []
        self.tool_names: list[str] = []
        self.tools: list[Any] = []

    @property
    def server_names(self) -> list[str]:
        return sorted(self._servers)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Drop tool references first so any agent still holding them fails
        # cleanly instead of hitting a stopped event loop.
        self.tools = []
        self.tool_names = []
        try:
            self._loop.run(self._aclose_all(), timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP pool close failed: %s", exc)
        finally:
            self._loop.stop()

    async def _aclose_all(self) -> None:
        for live in list(self._servers.values()):
            try:
                await live.session_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                await live.transport_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._servers.clear()

    async def _open_stdio(self, server: McpServerConfig) -> _LiveServer:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not server.command:
            raise ValueError(f"mcp server {server.name}: stdio requires command")
        params = StdioServerParameters(
            command=server.command,
            args=server.args,
            env=server.env or None,
        )
        transport_cm = stdio_client(params)
        read, write = await transport_cm.__aenter__()
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await session.initialize()
        return _LiveServer(
            config=server,
            session=session,
            transport_cm=transport_cm,
            session_cm=session_cm,
        )

    async def _open_http(self, server: McpServerConfig) -> _LiveServer:
        if not server.url:
            raise ValueError(
                f"mcp server {server.name}: url required for {server.transport}"
            )
        if server.transport in {"streamable_http", "http"}:
            from mcp.client.streamable_http import streamablehttp_client

            transport_cm = streamablehttp_client(
                server.url, headers=server.headers or None
            )
        else:
            from mcp.client.sse import sse_client

            transport_cm = sse_client(server.url, headers=server.headers or None)

        streams = await transport_cm.__aenter__()
        read, write = streams[0], streams[1]
        from mcp import ClientSession

        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await session.initialize()
        return _LiveServer(
            config=server,
            session=session,
            transport_cm=transport_cm,
            session_cm=session_cm,
            streams=streams,
        )

    async def _open_one(self, server: McpServerConfig) -> tuple[_LiveServer | None, str | None]:
        if not server.enabled:
            return None, None
        try:
            if server.transport == "stdio":
                live = await self._open_stdio(server)
            elif server.transport in {"sse", "streamable_http", "http"}:
                live = await self._open_http(server)
            else:
                return None, f"mcp server {server.name}: unsupported transport {server.transport}"
            self._servers[server.name] = live
            return live, None
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP server %s open failed: %s", server.name, exc)
            return None, f"mcp server {server.name}: {exc}"

    async def _call(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        live = self._servers.get(server_name)
        if live is None:
            return f"MCP error: server {server_name} is not connected"
        try:
            result = await live.session.call_tool(tool_name, arguments=arguments)
            return _content_to_text(result)
        except Exception as exc:
            # Connection broken (e.g. stdio process exited, HTTP stream closed,
            # anyio.ClosedResourceError).  Drop the dead session so follow-up
            # calls fail fast with "not connected" instead of reusing a stale
            # transport that will raise again.
            logger.warning(
                "MCP call_tool %s/%s failed: %s (removing session)",
                server_name,
                tool_name,
                exc,
            )
            self._servers.pop(server_name, None)
            return f"MCP error: {server_name}/{tool_name}: {exc}"

    def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        try:
            return self._loop.run(self._call(server_name, tool_name, arguments))
        except Exception as exc:
            logger.warning("MCP call_tool %s/%s loop error: %s", server_name, tool_name, exc)
            return f"MCP error: {server_name}/{tool_name}: {exc}"

    async def _discover(self, servers: list[McpServerConfig]) -> McpLoadResult:
        tools: list[Any] = []
        warnings: list[str] = []
        ok_servers: list[str] = []
        tool_names: list[str] = []

        for server in servers:
            if not server.enabled:
                continue
            live, err = await self._open_one(server)
            if err:
                warnings.append(err)
                continue
            if live is None:
                continue
            try:
                listed = await live.session.list_tools()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"mcp server {server.name}: list_tools failed: {exc}")
                self._servers.pop(server.name, None)
                continue

            def make_call(server_name: str = server.name):
                def _call(name: str, arguments: dict[str, Any]) -> str:
                    return self.call_tool(server_name, name, arguments)

                return _call

            call_fn = make_call()
            for item in listed.tools:
                tool = _make_tool(
                    server=server,
                    tool_name=item.name,
                    description=getattr(item, "description", "") or "",
                    input_schema=getattr(item, "inputSchema", None),
                    call_fn=call_fn,
                )
                tools.append(tool)
                tool_names.append(getattr(tool, "name", item.name))
            ok_servers.append(server.name)
            if not listed.tools:
                warnings.append(f"mcp server {server.name}: no tools discovered")

        self.warnings = warnings
        self.tool_names = tool_names
        # Keep tool objects so rebuilds can reuse them without reconnecting.
        self.tools = tools
        return McpLoadResult(
            tools=tools,
            warnings=warnings,
            servers=ok_servers,
            tool_names=tool_names,
            pool=self,
        )

    def load(self, servers: list[McpServerConfig]) -> McpLoadResult:
        return self._loop.run(self._discover(servers))


# Process-level active pool (replaced on reload).
_ACTIVE_POOL: McpSessionPool | None = None
_POOL_LOCK = threading.Lock()


def close_active_mcp_pool() -> None:
    global _ACTIVE_POOL
    with _POOL_LOCK:
        pool = _ACTIVE_POOL
        _ACTIVE_POOL = None
    if pool is not None:
        pool.close()


def get_active_mcp_pool() -> McpSessionPool | None:
    with _POOL_LOCK:
        return _ACTIVE_POOL


def load_mcp_tools(
    servers: list[McpServerConfig],
    *,
    enabled: bool = True,
    reuse_pool: bool = True,
) -> McpLoadResult:
    """Synchronously load tools from configured MCP servers.

    Opens long-lived sessions on a background loop. Subsequent tool calls
    reuse those sessions instead of spawning a new process/connection.
    """
    global _ACTIVE_POOL
    if not enabled or not servers:
        return McpLoadResult(tools=[], warnings=[], servers=[], tool_names=[])

    with _POOL_LOCK:
        if reuse_pool and _ACTIVE_POOL is not None:
            # Replace pool on reload so config changes take effect.
            old = _ACTIVE_POOL
            _ACTIVE_POOL = None
        else:
            old = None

    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    pool = McpSessionPool()
    try:
        result = pool.load(servers)
    except Exception as exc:  # noqa: BLE001
        pool.close()
        return McpLoadResult(
            tools=[],
            warnings=[f"mcp pool failed: {exc}"],
            servers=[],
            tool_names=[],
        )

    with _POOL_LOCK:
        _ACTIVE_POOL = pool
    return result


atexit.register(close_active_mcp_pool)
