"""Lightweight HTTP debug inspector for LLM request/response inspection.

Starts a background HTTP server on ``127.0.0.1:9090`` (configurable) serving
a single-page app that displays captured model-call records in real time.

Usage::

    from synapse.observability.debug_server import DebugHttpServer

    server = DebugHttpServer(get_debug_store())
    server.start()
    server.open_browser()  # opens http://127.0.0.1:9090
"""

# ruff: noqa: E501  (embedded HTML / JS / CSS)

from __future__ import annotations

import dataclasses
import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from synapse.observability.llm_debug import DebugCaptureStore

_DEFAULT_HOST = "127.0.0.1"
_PORT_START = 9090
_PORT_END = 9100


def _find_free_port(start: int = _PORT_START, end: int = _PORT_END) -> int:
    """Find a free TCP port in [start, end); fall back to OS-assigned."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    # Exhausted: let the OS decide
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

# ---------------------------------------------------------------------------
# JSON encoder for DebugCaptureRecord
# ---------------------------------------------------------------------------


def _record_to_dict(record: Any, *, request_delta_start: int = 0) -> dict[str, Any]:
    """Convert a DebugCaptureRecord to a JSON-safe dict."""
    d = dataclasses.asdict(record)
    d["request_delta_start"] = request_delta_start
    # Preserve a bounded body for expandable message inspection.
    for msg in d.get("request_messages", []):
        content = msg.get("content_full", "")
        msg["content_full"] = content[:16_384]
        msg["content_truncated"] = len(content) > len(msg["content_full"])
    for msg in d.get("response_messages", []):
        content = msg.get("content_full", "")
        msg["content_full"] = content[:16_384]
        msg["content_truncated"] = len(content) > len(msg["content_full"])
    # Raw provider-level HTTP payloads (already bounded at capture time).
    d["raw_request"] = record.raw_request
    d["raw_response"] = record.raw_response
    return d


def _record_to_raw_dict(record: Any) -> dict[str, Any]:
    """Return the complete bounded capture for explicit raw-record inspection."""
    return dataclasses.asdict(record)


def _message_identity(message: dict[str, Any]) -> tuple[Any, ...]:
    """Build a stable identity for common-prefix comparison between calls."""
    return (
        message.get("role"),
        message.get("name"),
        message.get("tool_call_id"),
        message.get("content_full"),
        tuple(
            (call.get("id"), call.get("name"), call.get("args"))
            for call in message.get("tool_calls", [])
        ),
    )


def _request_delta_start(records: list[Any], index: int) -> int:
    """Return count of unchanged leading request messages in this turn."""
    if index <= 0 or records[index - 1].turn_index != records[index].turn_index:
        return 0
    previous = records[index - 1].request_messages
    current = records[index].request_messages
    common = 0
    for previous_message, current_message in zip(previous, current, strict=False):
        if _message_identity(previous_message) != _message_identity(current_message):
            break
        common += 1
    return common


def _looks_like_tool_error(content: str) -> bool:
    """Heuristic: does this tool-result content look like a failure?"""
    if not content:
        return False
    # LangChain / Python exceptions
    if "Traceback (most recent call last)" in content:
        return True
    stripped = content.strip()
    if stripped.startswith("Error:") or stripped.startswith("Error "):
        return True
    # Short error-like messages (e.g. "<tool_name> failed: ...")
    if len(stripped) < 200 and (" failed:" in stripped or " failed " in stripped):
        return True
    return False


def _tool_pairs(records: list[Any], index: int) -> list[dict[str, Any]]:
    """Pair tools directly consumed or emitted by the selected model call."""
    turn_index = records[index].turn_index
    start = index
    while start > 0 and records[start - 1].turn_index == turn_index:
        start -= 1

    current_record = records[index]
    delta_start = _request_delta_start(records, index)
    request_delta = current_record.request_messages[delta_start:]
    relevant_ids: set[str] = set()
    for message in [*request_delta, *current_record.response_messages]:
        relevant_ids.update(str(call.get("id") or "") for call in message.get("tool_calls", []))
        if message.get("role") == "tool":
            relevant_ids.add(str(message.get("tool_call_id") or ""))
    relevant_ids.discard("")

    # Search the current turn history so a tool result in the request delta can
    # still be displayed beside the tool call that produced it.

    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[start : index + 1]:
        for message in [*record.request_messages, *record.response_messages]:
            for tool_call in message.get("tool_calls", []):
                call_id = str(tool_call.get("id") or "")
                if not call_id or call_id in calls:
                    continue
                calls[call_id] = {
                    "id": call_id,
                    "name": str(tool_call.get("name") or "unknown"),
                    "args": str(tool_call.get("args") or ""),
                    "result": None,
                    "error": None,
                }
                order.append(call_id)
            if message.get("role") != "tool":
                continue
            call_id = str(message.get("tool_call_id") or "")
            if not call_id:
                continue
            if call_id not in calls:
                calls[call_id] = {
                    "id": call_id,
                    "name": "(missing tool call)",
                    "args": "",
                    "result": None,
                    "error": None,
                }
                order.append(call_id)
            result_content = message.get("content_full", "")
            calls[call_id]["result"] = result_content
            # Derive error flag: explicit status, or content patterns
            if message.get("is_error"):
                calls[call_id]["error"] = True
            elif result_content and _looks_like_tool_error(result_content):
                calls[call_id]["error"] = True
            else:
                calls[call_id]["error"] = False

    return [calls[call_id] for call_id in order if call_id in relevant_ids]


def _record_summary(record: Any, index: int) -> dict[str, Any]:
    """Return the small, polling-safe projection used by the record list."""
    request_messages = record.request_messages
    response_messages = record.response_messages
    tool_calls = [
        call
        for message in response_messages
        for call in message.get("tool_calls", [])
    ]
    tool_results = [message for message in request_messages if message.get("role") == "tool"]
    tool_names = [str(call.get("name") or "") for call in tool_calls]
    has_tools = bool(tool_calls or tool_results)
    return {
        "index": index,
        "turn_index": record.turn_index,
        "model_call_index": record.model_call_index,
        "usage": record.usage,
        "provider": record.provider,
        "model_name": record.model_name,
        "started_at": record.started_at,
        "duration_ms": record.duration_ms,
        "error": record.error,
        "request_count": len(request_messages),
        "response_count": len(response_messages),
        "has_tools": has_tools,
        "tool_count": len(tool_calls),
        "tool_names": tool_names,
    }


# ---------------------------------------------------------------------------
# Inspector page
# ---------------------------------------------------------------------------

_PAGE_HTML = (Path(__file__).with_name("debug_inspector.html")).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------


class _DebugHandler(BaseHTTPRequestHandler):
    """Serves the inspector page and JSON API endpoints."""

    # Class-level reference set by DebugHttpServer before starting.
    store: DebugCaptureStore | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP log noise."""
        pass

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._html(_PAGE_HTML)
            return

        if path == "/api/status":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        if path == "/api/records":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            records = store.records()
            self._json([_record_summary(record, index) for index, record in enumerate(records)])
            return

        if path.startswith("/api/records/"):
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            raw = path.endswith("/raw")
            suffix = "/raw" if raw else ""
            try:
                index = int(path.removeprefix("/api/records/").removesuffix(suffix))
            except ValueError:
                self._text("Not Found", 404)
                return
            records = store.records()
            if not 0 <= index < len(records):
                self._text("Not Found", 404)
                return
            if raw:
                self._json(_record_to_raw_dict(records[index]))
            else:
                detail = _record_to_dict(records[index], request_delta_start=_request_delta_start(records, index))
                detail["tool_pairs"] = _tool_pairs(records, index)
                self._json(detail)
            return

        self._text("Not Found", 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/toggle":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            store.enabled = not store.enabled
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        if path == "/api/clear":
            store = self.store or getattr(self.__class__, "store", None)
            if store is None:
                self._json({"error": "no store"}, 500)
                return
            store.clear()
            self._json({"enabled": store.enabled, "record_count": store.record_count})
            return

        self._text("Not Found", 404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


class DebugHttpServer:
    """Manages the background HTTP debug inspector server.

    Usage::

        server = DebugHttpServer(get_debug_store())
        server.start()       # background thread
        server.open_browser()  # opens in default browser
        # ...
        server.stop()
    """

    def __init__(
        self,
        store: DebugCaptureStore,
        *,
        host: str = _DEFAULT_HOST,
    ) -> None:
        self._store = store
        self._host = host
        self._port: int = 0  # assigned on start()
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the HTTP server in a daemon background thread."""
        if self._httpd is not None:
            return  # already running

        self._port = _find_free_port()
        _DebugHandler.store = self._store
        self._httpd = HTTPServer((self._host, self._port), _DebugHandler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="debug-http-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the HTTP server."""
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            self._httpd = None
        self._thread = None

    def open_browser(self) -> None:
        """Open the inspector page in the default web browser."""
        webbrowser.open(self.url)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> DebugHttpServer:
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Singleton convenience
# ---------------------------------------------------------------------------

_server: DebugHttpServer | None = None


def get_debug_server() -> DebugHttpServer:
    """Return a process-level DebugHttpServer singleton (does NOT auto-start)."""
    global _server
    from synapse.observability.llm_debug import get_debug_store

    if _server is None:
        _server = DebugHttpServer(get_debug_store())
    return _server
