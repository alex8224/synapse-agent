from __future__ import annotations

import time

from synapse.observability.debug_server import (
    _PAGE_HTML,
    _record_summary,
    _record_to_dict,
    _request_delta_start,
    _tool_pairs,
)
from synapse.observability.llm_debug import DebugCaptureRecord


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
