"""Tests for dependency-free tool-output transformation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from synapse.execute_capture import capture_execute_output
from synapse.tool_output import (
    ContentType,
    GitSummaryTransformer,
    LogTransformer,
    NativeTransformer,
    SearchTransformer,
    ToolOutputRepository,
    ToolOutputTransformPipeline,
    TransformContext,
    TransformEvent,
    clear_metrics_notifier,
    detect_content_type,
    load_transformer_plugins,
    set_metrics_notifier,
)
from synapse.tool_output_middleware import build_tool_output_transform_middleware
from synapse.tools.session_tools import build_tool_result_reader_tool


def _request(*, name: str = "execute", query: str = ""):
    messages = [SimpleNamespace(type="human", content=query)] if query else []
    return SimpleNamespace(
        tool_call={"id": "call-1", "name": name, "args": {}},
        runtime=SimpleNamespace(
            config={"configurable": {"thread_id": "thread-a", "checkpoint_ns": "task"}},
            state={"messages": messages},
        ),
    )


def _search() -> str:
    lines = [f"src/file.py:{index}: ordinary symbol" for index in range(80)]
    lines[40] = "src/auth.py:40: FATAL AUTH_TIMEOUT retry exhausted"
    return "\n".join(lines)


def _log() -> str:
    lines = [f"2026-01-01 INFO item={index}" for index in range(140)]
    lines[50:54] = ["2026-01-01 FATAL AUTH_TIMEOUT", "Traceback", "frame", "TimeoutError: upstream"]
    return "\n".join(lines)


def test_repository_is_deduplicated_guarded_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "outputs.sqlite"
    repo = ToolOutputRepository(path)
    one = repo.put(thread_id="a", content="same")
    two = repo.put(thread_id="a", content="same")
    assert one.ref != two.ref and one.sha256 == two.sha256
    assert repo.get(one.ref, expected_thread_id="a").content == "same"  # type: ignore[union-attr]
    assert repo.get(one.ref, expected_thread_id="b") is None
    path.unlink()


def test_search_log_and_retrieval_metrics(tmp_path: Path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_tool_output_transform_middleware(repo, threshold_bytes=100)
    search = middleware.wrap_tool_call(
        _request(query="authentication"),
        lambda _: ToolMessage(content=_search(), tool_call_id="call-1", name="execute"),
    )
    assert "AUTH_TIMEOUT" in search.content
    assert "type: search" in search.content
    log = middleware.wrap_tool_call(
        _request(), lambda _: ToolMessage(content=_log(), tool_call_id="call-2", name="execute")
    )
    assert "TimeoutError" in log.content
    ref = search.artifact["tool_output_transform"]["ref"]
    reader = build_tool_result_reader_tool(tmp_path / "outputs.sqlite")
    runtime = ToolRuntime(
        state={},
        context=None,
        config={"configurable": {"thread_id": "thread-a"}},
        stream_writer=lambda _: None,
        tool_call_id="read",
        store=None,
    )
    assert "AUTH_TIMEOUT" in reader.invoke(
        {"runtime": runtime, "ref": ref, "query": "auth timeout"}
    )
    stats = repo.stats(thread_id="thread-a")
    assert stats["transformed"] == 2 and stats["retrieval_bytes"] > 0


def test_diff_json_code_and_plugin_transforms() -> None:
    pipeline = ToolOutputTransformPipeline()
    diff = "\n".join(
        ["diff --git a/a b/a", "@@ -1 +1 @@", *[" context" for _ in range(40)], "-old", "+new"]
    )
    assert (
        "+new"
        in pipeline.transform(diff, TransformContext(tool_name="git", status="success")).content
    )
    data = json.dumps(
        [{"id": i, "status": "error" if i == 10 else "ok", "payload": "x" * 100} for i in range(30)]
    )
    assert (
        "error"
        in pipeline.transform(
            data, TransformContext(tool_name="x", status="success", query="error")
        ).content
    )
    code = "from pathlib import Path\n" + "\n".join(
        f"def work_{i}(x):\n x += 1\n x += 2\n x += 3\n return x\n" for i in range(15)
    )
    result = pipeline.transform(code, TransformContext(tool_name="read_file", status="success"))
    assert "from pathlib import Path" in result.content and "def work_0(x):" in result.content
    plugins = load_transformer_plugins(["tests.tool_output_plugin_fixture:fixture_transformer"])
    assert (
        ToolOutputTransformPipeline(transformers=plugins)
        .transform(data, TransformContext(tool_name="x", status="success"))
        .transformer
        == "fixture-json-v1"
    )


def test_capture_error_command_async_exclusion_and_detection(tmp_path: Path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_tool_output_transform_middleware(repo, threshold_bytes=20)

    def handler(_):
        capture_execute_output(full_output=_log(), displayed_output="truncated", truncated=True)
        return ToolMessage(content="truncated", tool_call_id="call-1", name="execute")

    assert (
        middleware.wrap_tool_call(_request(), handler).artifact[
            "tool_output_contains_untruncated_execute_output"
        ]
        is True
    )
    error = middleware.wrap_tool_call(
        _request(name="read_file"),
        lambda _: ToolMessage(
            content="Error details" * 50, tool_call_id="call-1", name="read_file", status="error"
        ),
    )
    assert error.content.startswith("Error details")
    source = _search()
    command = middleware.wrap_tool_call(
        _request(),
        lambda _: Command(
            update={"messages": [ToolMessage(content=source, tool_call_id="call-1")]}
        ),
    )

    async def run():
        return await middleware.awrap_tool_call(_request(), lambda _: async_message(source))

    async def async_message(value):
        return ToolMessage(content=value, tool_call_id="call-1", name="execute")

    assert "[tool output transformed]" in command.update["messages"][0].content
    assert "[tool output transformed]" in asyncio.run(run()).content
    direct = middleware.wrap_tool_call(
        _request(name="read_tool_result"),
        lambda _: ToolMessage(content=source, tool_call_id="call-1", name="read_tool_result"),
    )
    assert direct.content == source
    samples = {
        ContentType.SEARCH: "a.py:1:x\nb.py:2:x\nc.py:3:x",
        ContentType.LOG: "2026-01-01 INFO x\n2026-01-01 ERROR y\n2026-01-01 INFO z",
        ContentType.DIFF: "diff --git a/a b/a\n@@ -1 +1 @@\n-old\n+new",
        ContentType.JSON: '[{"id":1}]',
        ContentType.CODE: "def a():\n pass\ndef b():\n pass\ndef c():\n pass",
    }
    for expected, content in samples.items():
        assert detect_content_type(content).content_type is expected


def test_search_transformer_handles_ripgrep_context_and_global_budget() -> None:
    content = "\n".join(
        [
            "src/main.py-40-before context",
            "src/main.py:42:def process_data():",
            "src/main.py-43-after context",
            *[f"src/other.py:{index}: ordinary match" for index in range(50)],
            "src/other.py:99: TODO important follow-up",
        ]
    )
    result = SearchTransformer(max_matches_per_file=3, max_total_matches=5).transform(
        content,
        TransformContext(tool_name="execute", status="success", query="process todo"),
    )

    assert "src/main.py-40-before context" in result.content
    assert "src/main.py:42:def process_data():" in result.content
    assert "TODO important follow-up" in result.content
    matches = [
        line
        for line in result.content.splitlines()
        if line.startswith("src/") and "matches omitted" not in line
    ]
    assert len(matches) <= 5
    assert result.metadata["omitted_matches"] > 0


def test_log_transformer_deduplicates_warnings_and_keeps_stack_context() -> None:
    lines = [f"2026-01-01 WARN retry attempt={index} path=/tmp/{index}" for index in range(20)]
    lines.extend(
        [
            "2026-01-01 FATAL DATABASE_DEADLOCK transaction failed",
            "Traceback (most recent call last):",
            '  File "store.py", line 42, in save',
            "DatabaseError: deadlock",
        ]
    )
    lines.extend(f"2026-01-01 INFO completed={index}" for index in range(40))
    result = LogTransformer(max_warnings=2, min_lines_for_compression=20).transform(
        "\n".join(lines), TransformContext(tool_name="execute", status="success")
    )

    assert "DATABASE_DEADLOCK" in result.content
    assert "DatabaseError: deadlock" in result.content
    assert result.metadata["warnings"] == 20
    assert result.content.count("WARN retry") <= 2
    assert result.metadata["omitted_lines"] > 0


def test_native_transformers_are_used_when_wheel_is_installed() -> None:
    pipeline = ToolOutputTransformPipeline(use_native=True)
    result = pipeline.transform(
        _search(), TransformContext(tool_name="execute", status="success", query="authentication")
    )

    assert result.transformer == "headroom-search-v1"
    assert result.metadata["native"] is True
    assert "AUTH_TIMEOUT" in result.content


def test_native_transform_error_falls_back_to_python_transformer() -> None:
    broken_native = NativeTransformer(
        name="broken-native",
        content_type=ContentType.SEARCH,
        native_transform=lambda _content, _context: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    pipeline = ToolOutputTransformPipeline(transformers=[broken_native], use_native=False)
    result = pipeline.transform(
        _search(), TransformContext(tool_name="execute", status="success", query="authentication")
    )

    assert result.transformer == "search-v1"
    assert result.metadata.get("native") is None
    assert "AUTH_TIMEOUT" in result.content


def test_native_pipeline_can_be_disabled() -> None:
    pipeline = ToolOutputTransformPipeline(use_native=False)
    result = pipeline.transform(
        _search(), TransformContext(tool_name="execute", status="success", query="authentication")
    )

    assert result.transformer == "search-v1"


def test_native_unhelpful_result_falls_back_to_python_transformer() -> None:
    unhelpful_native = NativeTransformer(
        name="unhelpful-native",
        content_type=ContentType.SEARCH,
        native_transform=lambda content, _context: {"content": content, "transformer": "native"},
    )
    pipeline = ToolOutputTransformPipeline(transformers=[unhelpful_native], use_native=False)
    result = pipeline.transform(
        _search(), TransformContext(tool_name="execute", status="success", query="authentication")
    )

    assert result.transformer == "search-v1"
    assert len(result.content) < len(_search())


def test_repository_events_include_execution_path_and_retrieval(tmp_path: Path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_tool_output_transform_middleware(repo, threshold_bytes=100)
    transformed = middleware.wrap_tool_call(
        _request(query="authentication"),
        lambda _: ToolMessage(content=_search(), tool_call_id="call-1", name="execute"),
    )
    ref = transformed.artifact["tool_output_transform"]["ref"]
    repo.record_retrieval(
        thread_id="thread-a", ref=ref, mode="query", returned_bytes=42, duration_ms=1.0
    )
    passthrough = "\n".join(f"src/file.py:{index}: unique token {index}" for index in range(120))
    disabled_middleware = build_tool_output_transform_middleware(
        repo,
        threshold_bytes=100,
        pipeline=ToolOutputTransformPipeline(disabled_types={"search"}),
    )
    disabled_middleware.wrap_tool_call(
        _request(),
        lambda _: ToolMessage(content=passthrough, tool_call_id="call-2", name="execute"),
    )

    events = repo.events(thread_id="thread-a")
    assert len(events) == 2
    assert events[1]["outcome"] == "transformed"
    assert events[1]["execution_path"] in {"native", "python_only", "python_fallback_after_native"}
    assert events[1]["retrieval_bytes"] == 42
    assert events[0]["outcome"] == "passthrough"
    assert events[0]["ref"] is None
    assert repo.stats(thread_id="thread-a")["execution_paths"]


def test_repository_notifies_on_transform_and_retrieval(tmp_path: Path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    notified: list[str] = []
    set_metrics_notifier(notified.append)
    try:
        event = TransformEvent(
            content_type="log",
            transformer="log-v1",
            outcome="transformed",
            original_bytes=2048,
            visible_bytes=512,
            duration_ms=1.0,
            critical_total=1,
            critical_retained=1,
            ref_created=True,
        )
        repo.record_event("thread-a", event, ref="tool-output://ref")
        repo.record_retrieval(
            thread_id="thread-a",
            ref="tool-output://ref",
            mode="pagination",
            returned_bytes=100,
            duration_ms=1.0,
        )
    finally:
        clear_metrics_notifier()

    assert notified == ["thread-a", "thread-a"]


def _git_summary() -> str:
    entries = [
        f" src/generated_{index}.py | {index + 1:4} " + "+" * ((index % 8) + 1)
        for index in range(80)
    ]
    modes = [f" create mode 100644 src/generated_{index}.py" for index in range(80)]
    return "\n".join(
        [
            "Merge made by the 'ort' strategy.",
            *entries,
            " 160 files changed, 5000 insertions(+), 100 deletions(-)",
            *modes,
        ]
    )


def test_git_summary_detection_and_transform_preserves_operation_totals() -> None:
    content = _git_summary()
    assert detect_content_type(content).content_type is ContentType.GIT_SUMMARY

    result = GitSummaryTransformer().transform(
        content, TransformContext(tool_name="execute", status="success")
    )

    assert result.transformer == "git-summary-v1"
    assert len(result.content.encode()) < len(content.encode())
    assert "Merge made by the 'ort' strategy." in result.content
    assert "160 files changed, 5000 insertions(+), 100 deletions(-)" in result.content
    assert "git summary compressed" in result.content


def test_default_low_threshold_transforms_profitable_git_summary(tmp_path: Path) -> None:
    repo = ToolOutputRepository(tmp_path / "outputs.sqlite")
    middleware = build_tool_output_transform_middleware(repo)
    result = middleware.wrap_tool_call(
        _request(),
        lambda _: ToolMessage(content=_git_summary(), tool_call_id="call-git", name="execute"),
    )

    assert "[tool output transformed]" in result.content
    assert "type: git-summary" in result.content
    event = repo.events(thread_id="thread-a")[0]
    assert event["content_type"] == "git-summary"
    assert event["transformer"] == "git-summary-v1"
