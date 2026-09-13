from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from synapse.ui.turn.controller import TurnController


class ErrorApp:
    """Record only actual rendering, unlike call-dispatch bookkeeping in older fakes."""

    def __init__(self, workspace: Path) -> None:
        self.settings = SimpleNamespace(workspace=workspace)
        self.thread_id = "thread1"
        self.project = "project1"
        self.agent = object()
        self._agent_ready = SimpleNamespace(wait=lambda timeout: True)
        self._agent_error = None
        self._transcript_generation = 1
        self.messages: list[str] = []
        self.finished = 0

    def _current_project_id(self) -> str:
        return self.project

    def _begin_turn_usage(self) -> None:
        pass

    def call_from_thread(self, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return callback(*args, **kwargs)

    def _call_for_transcript(
        self, generation: int, callback: Callable[..., Any], *args: Any
    ) -> Any:
        if generation == self._transcript_generation:
            return callback(*args)
        return None

    def append_event(self, text: str, style: str) -> None:
        self.messages.append(text)

    def _turn_done(self) -> None:
        self.finished += 1


def setup_controller(
    workspace: Path, failure: Exception | None, detail: str = ""
) -> tuple[TurnController, ErrorApp, SimpleNamespace]:
    app = ErrorApp(workspace)
    controller = TurnController(app)

    async def pending() -> Any:
        return SimpleNamespace(turn_id="turn1", actions=(object(),))

    async def run(*args: Any, **kwargs: Any) -> Any:
        if failure is not None:
            raise failure
        return SimpleNamespace(
            status="failed", turn_id="turn1", error_message=detail,
            final_text="", already_streamed=False,
        )

    facade = SimpleNamespace(
        submit=run, resume=run, pending_approval=pending,
        binding=SimpleNamespace(
            session=SimpleNamespace(project_id="project1", thread_id="thread1"),
            settings={"workspace": str(workspace)},
        ),
    )
    controller._service_facade = lambda thread: facade
    controller._service_session_cached = lambda thread: facade
    controller._turn_finished = lambda *a, **k: app._turn_done()
    controller.apply_consumer_result = MagicMock(return_value=False)
    return controller, app, facade


def run_controller(controller: TurnController, mode: str) -> None:
    if mode == "submit":
        controller.run_turn("PRIVATE-PROMPT")
    else:
        controller.run_resume("approve", "PRIVATE-APPROVAL")


@pytest.mark.parametrize("mode", ["submit", "resume"])
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError(), ConnectionError()])
def test_empty_errors_show_type_once_and_log(tmp_path: Path, mode: str, failure: Exception) -> None:
    controller, app, _ = setup_controller(tmp_path, failure)
    run_controller(controller, mode)
    errors = [m for m in app.messages if m.startswith("ERROR:")]
    assert errors == [f"ERROR: {type(failure).__name__}"]
    assert app.finished == 1
    paths = [
        Path(m.removeprefix("Error log: ")) for m in app.messages
        if m.startswith("Error log:")
    ]
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["operation"] == f"tui.{mode}"
    assert record["thread_id"] == "thread1"
    assert "Traceback" in record["stack"]
    assert "PRIVATE-PROMPT" not in text
    assert "PRIVATE-APPROVAL" not in text


@pytest.mark.parametrize("mode", ["submit", "resume"])
@pytest.mark.parametrize("detail", ["boom details", "", "   "])
def test_failed_result_has_detail_or_meaningful_fallback(
    tmp_path: Path, mode: str, detail: str
) -> None:
    controller, app, _ = setup_controller(tmp_path, None, detail)
    run_controller(controller, mode)
    errors = [m for m in app.messages if m.startswith("ERROR:")]
    assert len(errors) == 1
    if detail.strip():
        assert detail.strip() in errors[0]
    else:
        assert "no error details" in errors[0]
    assert any(m.startswith("Error log:") for m in app.messages)


@pytest.mark.parametrize("mode", ["submit", "resume"])
def test_log_failure_keeps_original_error_and_reports_unavailability(
    tmp_path: Path, mode: str
) -> None:
    (tmp_path / ".synapse").write_text("blocked", encoding="utf-8")
    controller, app, _ = setup_controller(tmp_path, TimeoutError())
    run_controller(controller, mode)
    assert "ERROR: TimeoutError" in app.messages
    assert any(m.startswith("Error log unavailable") for m in app.messages)
    assert not any(m.startswith("Error log:") for m in app.messages)
    assert app.finished == 1


@pytest.mark.parametrize("mode", ["submit", "resume"])
def test_late_error_logs_to_original_project_but_not_new_transcript(
    tmp_path: Path, mode: str
) -> None:
    original = tmp_path / "original"
    current = tmp_path / "current"
    controller, app, facade = setup_controller(original, TimeoutError())

    async def switch_then_fail(*args: Any, **kwargs: Any) -> None:
        app.settings = SimpleNamespace(workspace=current)
        app.project = "project2"
        app._transcript_generation += 1
        raise TimeoutError()

    facade.submit = switch_then_fail
    facade.resume = switch_then_fail
    run_controller(controller, mode)
    assert not app.messages
    assert len(list((original / ".synapse" / "logs").glob("errors-*.log"))) == 1
    assert not (current / ".synapse").exists()


def test_nonempty_error_keeps_useful_summary_without_credentials(tmp_path: Path) -> None:
    controller, app, _ = setup_controller(
        tmp_path, RuntimeError("provider unavailable; api_key=FIXTURE-CREDENTIAL")
    )
    controller.run_turn("PRIVATE-PROMPT")
    assert "provider unavailable" in app.messages[0]
    assert "FIXTURE-CREDENTIAL" not in "\n".join(app.messages)
