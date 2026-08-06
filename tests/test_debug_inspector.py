from __future__ import annotations

import socket
import time
from pathlib import Path

import synapse.observability.debug_server as debug_server
from synapse.observability.debug_server import (
    _PAGE_HTML,
    DebugHttpServer,
    _record_summary,
    _record_to_dict,
    _request_delta_start,
    _tool_pairs,
)
from synapse.observability.llm_debug import DebugCaptureRecord, get_debug_store


def _record(
    *,
    call: int,
    request: list[dict],
    response: list[dict],
) -> DebugCaptureRecord:
    return DebugCaptureRecord(
        turn_index=1,
        model_call_index=call,
        request_messages=request,
        response_text="",
        response_messages=response,
        usage={"input_tokens": 100, "output_tokens": 20},
        provider="openai",
        model_name="test-model",
        started_at=time.time(),
        duration_ms=123.0,
    )


def test_page_uses_two_column_task_focused_layout() -> None:
    assert "grid-template-columns:300px minmax(0,1fr)" in _PAGE_HTML
    assert "分析上下文" not in _PAGE_HTML
    assert "概览" in _PAGE_HTML
    assert "请求" in _PAGE_HTML
    assert "响应" in _PAGE_HTML
    assert "原始HTTP" in _PAGE_HTML
    assert "原始" in _PAGE_HTML
    assert "跟随最新" in _PAGE_HTML
    assert "collapsedTurns" in _PAGE_HTML
    assert "turn-group" in _PAGE_HTML
    assert "overflow-y:scroll" in _PAGE_HTML


def test_page_hides_raw_function_calls_in_default_message_view() -> None:
    assert "looksLikeRawCalls(text)" in _PAGE_HTML
    assert "模型发起 ${hasCalls} 个结构化工具调用" in _PAGE_HTML
    assert "默认折叠参数和结果" in _PAGE_HTML


def test_record_summary_includes_tool_count_and_names() -> None:
    record = _record(
        call=1,
        request=[],
        response=[
            {
                "role": "ai",
                "tool_calls": [
                    {"id": "call-1", "name": "read_file", "args": "{}"},
                    {"id": "call-2", "name": "execute", "args": "{}"},
                ],
            }
        ],
    )

    summary = _record_summary(record, 0)

    assert summary["tool_count"] == 2
    assert summary["tool_names"] == ["read_file", "execute"]
    assert summary["has_tools"] is True


def test_tool_pairs_only_include_tools_directly_related_to_selected_call() -> None:
    first = _record(
        call=1,
        request=[{"role": "human", "content_full": "inspect"}],
        response=[
            {
                "role": "ai",
                "content_full": "",
                "tool_calls": [
                    {"id": "old", "name": "read_file", "args": '{"file_path":"/old"}'},
                    {"id": "direct", "name": "execute", "args": '{"intent":"run checks"}'},
                ],
            }
        ],
    )
    second = _record(
        call=2,
        request=[
            {"role": "human", "content_full": "inspect"},
            {"role": "tool", "tool_call_id": "direct", "content_full": "checks passed"},
        ],
        response=[
            {
                "role": "ai",
                "content_full": "",
                "tool_calls": [
                    {"id": "next", "name": "search_files", "args": '{"intent":"find tests"}'},
                ],
            }
        ],
    )
    records = [first, second]

    assert _request_delta_start(records, 1) == 1
    pairs = _tool_pairs(records, 1)

    assert [pair["id"] for pair in pairs] == ["direct", "next"]
    assert pairs[0]["result"] == "checks passed"
    assert pairs[0]["error"] is False
    assert pairs[1]["result"] is None
    assert pairs[1]["error"] is None


def test_record_detail_includes_raw_http_payloads() -> None:
    record = _record(
        call=1,
        request=[{"role": "human", "content_full": "hi"}],
        response=[],
    )
    record.raw_request = {
        "method": "POST",
        "url": "http://example/v1/chat/completions",
        "body": '{"model":"m","messages":[]}',
        "body_truncated": False,
    }
    record.raw_response = {"body": '{"choices":[]}', "body_truncated": False}

    detail = _record_to_dict(record)

    assert detail["raw_request"]["method"] == "POST"
    assert detail["raw_request"]["body"] == '{"model":"m","messages":[]}'
    assert detail["raw_response"]["body"] == '{"choices":[]}'
    assert detail["raw_request"]["body_truncated"] is False


def test_page_has_heap_dump_export_button() -> None:
    assert "内存 Dump" in _PAGE_HTML
    assert "id=\"heapDump\"" in _PAGE_HTML
    assert "/api/heap-dump" in _PAGE_HTML
    assert "renderHeapDump" in _PAGE_HTML


def test_write_heap_dump_writes_file_and_returns_summary(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setattr(debug_server, "_dump_dir", lambda: tmp_path)

    summary = debug_server._write_heap_dump()

    assert summary["pid"] > 0
    assert summary["gc_objects"] > 0
    assert summary["top_types"], "top_types must not be empty"
    assert summary["process_memory"], "process_memory must not be empty"
    assert summary["python_approx_bytes"] > 0
    target = tmp_path / summary["file"]
    assert target.is_file()
    assert summary["size_bytes"] == target.stat().st_size
    assert summary["download_url"].startswith("/api/heap-dump/download?file=")
    # The returned summary is bounded (only 15 top types), not the full dump.
    assert len(summary["top_types"]) <= 15


def test_prune_dumps_keeps_only_newest(tmp_path: Path) -> None:
    for i in range(4):
        (tmp_path / f"heap_{100 + i}_{i}.json").write_text(f"dump {i}", encoding="utf-8")
    (tmp_path / "unrelated.txt").write_text("keep me", encoding="utf-8")

    debug_server._prune_dumps(tmp_path, keep=2)

    remaining = sorted(p.name for p in tmp_path.glob("heap_*.json"))
    assert remaining == ["heap_102_2.json", "heap_103_3.json"]
    assert (tmp_path / "unrelated.txt").exists()


def test_find_free_port_returns_bindable_port() -> None:
    port = debug_server._find_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        assert s.getsockname()[1] == port


def test_start_falls_back_when_preferred_port_is_blocked(monkeypatch: object) -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    blocked_port = blocker.getsockname()[1]
    monkeypatch.setattr(debug_server, "_find_free_port", lambda: blocked_port)

    server = DebugHttpServer(get_debug_store())
    try:
        server.start()
        assert server._httpd is not None
        assert server._port != blocked_port
        assert server._port > 0
        assert server.url == f"http://127.0.0.1:{server._port}"
    finally:
        server.stop()
        blocker.close()
