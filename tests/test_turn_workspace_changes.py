"""A turn reports what it did to the workspace, and never fails over it."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from synapse.runtime.agent_loop import AgentTurnRuntime, TurnContext, TurnRequest, TurnStatus
from synapse.runtime.async_runtime import AsyncRuntime
from synapse.ui.stream_events import StreamResult


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _workspace(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "tracked.py").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.py")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _context(workspace: Path | None) -> TurnContext:
    settings = SimpleNamespace(
        token_stream=True,
        max_concurrency=2,
        show_reasoning_placeholders=True,
    )
    if workspace is not None:
        settings.workspace = workspace
    return TurnContext(
        thread_id="thread-1",
        turn_id="turn-1",
        agent=object(),
        settings=settings,
        request=TurnRequest(
            payload={"messages": [{"role": "user", "content": "hello"}]},
            config={"configurable": {"thread_id": "thread-1"}, "max_concurrency": 2},
            thread_id="thread-1",
        ),
    )


def _result() -> StreamResult:
    return StreamResult(state={"messages": []}, final_text="answer", streamed_answer=True)


def test_a_turn_reports_the_files_it_changed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    runtime_loop = AsyncRuntime(name="turn-changes")

    def runner(*args: object, **kwargs: object) -> StreamResult:
        # What the turn does to the workspace happens between the two snapshots.
        (workspace / "tracked.py").write_text("one\ntwo\n", encoding="utf-8")
        (workspace / "fresh.py").write_text("hello\n", encoding="utf-8")
        return _result()

    try:
        result = AgentTurnRuntime(runtime_loop, stream_runner=runner).run(
            _context(workspace), timeout=10
        )
    finally:
        runtime_loop.close()

    assert result.status is TurnStatus.COMPLETED
    assert result.changes_total == 2
    by_path = {change.path: change for change in result.changes}
    assert (by_path["tracked.py"].status, by_path["tracked.py"].insertions) == ("modified", 1)
    assert (by_path["fresh.py"].status, by_path["fresh.py"].insertions) == ("added", 1)


def test_a_turn_without_a_workspace_reports_nothing(tmp_path: Path) -> None:
    runtime_loop = AsyncRuntime(name="turn-changes-none")
    try:
        result = AgentTurnRuntime(
            runtime_loop, stream_runner=lambda *args, **kwargs: _result()
        ).run(_context(None), timeout=10)
    finally:
        runtime_loop.close()

    assert (result.changes, result.changes_total) == ((), 0)


def test_a_turn_that_commits_its_own_work_reports_nothing(tmp_path: Path) -> None:
    """A commit does not change the workspace, so there is nothing to report.

    The tree is dirty when the turn starts -- earlier turns' work is still uncommitted --
    and clean when it ends, and every one of those files would be reported as deleted if
    the second snapshot did not carry the first one's paths.
    """
    workspace = _workspace(tmp_path)
    (workspace / "tracked.py").write_text("one\ntwo\n", encoding="utf-8")
    runtime_loop = AsyncRuntime(name="turn-changes-commit")

    def runner(*args: object, **kwargs: object) -> StreamResult:
        _git(workspace, "commit", "-q", "-am", "the turn commits its own work")
        return _result()

    try:
        result = AgentTurnRuntime(runtime_loop, stream_runner=runner).run(
            _context(workspace), timeout=10
        )
    finally:
        runtime_loop.close()

    assert result.status is TurnStatus.COMPLETED
    assert (result.changes, result.changes_total) == ((), 0)


def test_a_turn_that_deletes_a_file_reports_the_whole_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    runtime_loop = AsyncRuntime(name="turn-changes-delete")

    def runner(*args: object, **kwargs: object) -> StreamResult:
        (workspace / "tracked.py").unlink()
        return _result()

    try:
        result = AgentTurnRuntime(runtime_loop, stream_runner=runner).run(
            _context(workspace), timeout=10
        )
    finally:
        runtime_loop.close()

    assert result.status is TurnStatus.COMPLETED
    assert [(c.path, c.status, c.insertions, c.deletions) for c in result.changes] == [
        ("tracked.py", "deleted", 0, 1)
    ]


def test_a_failed_turn_still_reports_what_it_changed(tmp_path: Path) -> None:
    # A turn that wrote a file and then failed leaves the file behind; the reader is
    # owed the same list as for a turn that finished.
    workspace = _workspace(tmp_path)
    runtime_loop = AsyncRuntime(name="turn-changes-failed")

    def runner(*args: object, **kwargs: object) -> StreamResult:
        (workspace / "half.py").write_text("partial\n", encoding="utf-8")
        raise RuntimeError("model exploded")

    try:
        result = AgentTurnRuntime(runtime_loop, stream_runner=runner).run(
            _context(workspace), timeout=10
        )
    finally:
        runtime_loop.close()

    assert result.status is TurnStatus.FAILED
    # The turn's own error log lives in the workspace too (`.synapse/logs/...`), and it
    # is not something the reader asked the turn to do: only the work is reported.
    assert [change.path for change in result.changes] == ["half.py"]
