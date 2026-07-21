"""AsyncSqliteSaver + process async runtime."""

from __future__ import annotations

from pathlib import Path

from synapse.agent import _build_async_sqlite_checkpointer, _build_checkpointer
from synapse.async_runtime import get_async_runtime, reset_async_runtime_for_tests
from synapse.config import load_settings
from synapse.ui.stream import checkpointer_supports_async


def test_async_runtime_run_coroutine():
    reset_async_runtime_for_tests()
    runtime = get_async_runtime()

    async def _add(a: int, b: int) -> int:
        return a + b

    assert runtime.run(_add(2, 3)) == 5
    assert runtime.loop.is_running()


def test_build_async_sqlite_checkpointer(tmp_path: Path):
    reset_async_runtime_for_tests()
    db = tmp_path / "ckpt.sqlite"
    saver = _build_async_sqlite_checkpointer(str(db))
    assert type(saver).__name__ == "AsyncSqliteSaver"
    assert checkpointer_supports_async(saver) is True
    # Sync get_tuple from non-loop thread works (LangGraph schedules onto saver.loop).
    cfg = {"configurable": {"thread_id": "t1"}}
    assert saver.get_tuple(cfg) is None


def test_build_checkpointer_sqlite_uses_async(tmp_path: Path):
    reset_async_runtime_for_tests()
    settings = load_settings(
        workspace=tmp_path,
        checkpoint_backend="sqlite",
        checkpoint_path=tmp_path / "agent.sqlite",
        enable_mcp=False,
    )
    saver = _build_checkpointer(settings)
    assert type(saver).__name__ == "AsyncSqliteSaver"
    assert checkpointer_supports_async(saver) is True


def test_build_checkpointer_memory(tmp_path: Path):
    settings = load_settings(
        workspace=tmp_path,
        checkpoint_backend="memory",
        enable_mcp=False,
    )
    saver = _build_checkpointer(settings)
    assert type(saver).__name__ in {"MemorySaver", "InMemorySaver"}