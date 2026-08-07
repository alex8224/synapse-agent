"""P3 Textual TurnEvent renderer and bridge contracts."""

from __future__ import annotations

from typing import Any

from synapse.runtime.sessions import SessionEventBroker
from synapse.runtime.streaming import (
    EVENT_VERSION,
    TextPayload,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
)
from synapse.ui.turn.controller import TurnController
from synapse.ui.turn.event_bridge import TextualTurnEventBridge
from synapse.ui.turn.event_renderer import TextualTurnEventRenderer


class _Host:
    transcript_generation = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.wakes = 0

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        return callback(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        if name in {"should_suppress_dag_task_tool_group"}:
            return lambda calls: False
        if name in {"sync_subagent_monitor_block"}:
            return lambda **kwargs: None

        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _event(sequence: int, kind: TurnEventKind, payload: Any) -> TurnEvent:
    return TurnEvent(
        version=EVENT_VERSION,
        thread_id="thread",
        turn_id="turn",
        sequence=sequence,
        kind=kind,
        payload=payload,
    )


def test_event_renderer_maps_answer_and_terminal() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")

    renderer.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("hel", "m1")))
    renderer.emit(_event(2, TurnEventKind.ANSWER_DELTA, TextPayload("lo", "m1")))
    renderer.emit(_event(3, TurnEventKind.ANSWER_COMPLETED, TextPayload("hello", "m1")))
    renderer.emit(
        _event(
            4,
            TurnEventKind.TURN_COMPLETED,
            TurnTerminalPayload(status="completed", final_text="hello"),
        )
    )

    answers = [call for call in host.calls if call[0] == "commit_answer"]
    assert answers[-1][1] == ("hello",)
    assert renderer.closed is True
    assert renderer.last_sequence == 4


def test_generation_change_detaches_renderer() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    host.transcript_generation = 2

    renderer.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("stale")))

    assert host.calls == []
    assert renderer.closed is True


def test_renderer_ignores_wrong_turn_and_duplicate_sequence() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    wrong = TurnEvent(
        version=EVENT_VERSION,
        thread_id="other",
        turn_id="turn",
        sequence=1,
        kind=TurnEventKind.ANSWER_COMPLETED,
        payload=TextPayload("wrong"),
    )
    renderer.emit(wrong)
    renderer.emit(_event(1, TurnEventKind.ANSWER_COMPLETED, TextPayload("once")))
    renderer.emit(_event(1, TurnEventKind.ANSWER_COMPLETED, TextPayload("twice")))

    answers = [call for call in host.calls if call[0] == "commit_answer"]
    assert len(answers) == 1
    assert answers[0][1] == ("once",)


def test_bridge_coalesces_high_frequency_deltas_and_one_wakeup() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    callbacks: list[Any] = []
    bridge = TextualTurnEventBridge(renderer, callbacks.append)

    for sequence in range(1, 101):
        bridge.emit(
            _event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x", "message"))
        )

    assert len(callbacks) == 1
    assert bridge.pending_count == 1
    callbacks.pop()()
    assert renderer.last_sequence == 100


def test_bridge_keeps_terminal_and_stops_after_close() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    callbacks: list[Any] = []
    bridge = TextualTurnEventBridge(renderer, callbacks.append, max_events=16)

    for sequence in range(1, 40):
        bridge.emit(_event(sequence, TurnEventKind.ANSWER_DELTA, TextPayload("x")))
    bridge.emit(
        _event(
            40,
            TurnEventKind.TURN_CANCELLED,
            TurnTerminalPayload(status="cancelled"),
        )
    )
    callbacks.pop()()

    assert renderer.closed is True
    bridge.emit(_event(41, TurnEventKind.INFO, "ignored"))
    assert bridge.pending_count == 0


def test_turn_controller_unwraps_session_event_envelope_for_renderer() -> None:
    host = _Host()
    broker = SessionEventBroker("thread")
    runtime = type(
        "Runtime",
        (),
        {
            "thread_id": "thread",
            "active_context": lambda self: type(
                "Context", (), {"thread_id": "thread", "turn_id": "turn"}
            )(),
            "subscribe": lambda self, callback, *, after_sequence=0: broker.subscribe(
                callback, after_sequence=after_sequence
            ),
        },
    )()
    app = type("App", (), {"_transcript": host, "thread_id": "thread"})()
    controller = TurnController(app)

    controller.attach(runtime)
    broker.emit(_event(1, TurnEventKind.ANSWER_COMPLETED, TextPayload("hello", "m1")))

    answers = [call for call in host.calls if call[0] == "commit_answer"]
    assert answers == [("commit_answer", ("hello",), {})]
    controller._detach_renderer()


def test_turn_controller_replays_events_emitted_before_renderer_attach() -> None:
    host = _Host()
    broker = SessionEventBroker("thread")
    broker.emit(_event(1, TurnEventKind.ANSWER_COMPLETED, TextPayload("early", "m1")))
    runtime = type(
        "Runtime",
        (),
        {
            "thread_id": "thread",
            "active_context": lambda self: type(
                "Context", (), {"thread_id": "thread", "turn_id": "turn"}
            )(),
            "snapshot": lambda self: type("Snapshot", (), {"latest_sequence": 1})(),
            "subscribe": lambda self, callback, *, after_sequence=0: broker.subscribe(
                callback, after_sequence=after_sequence
            ),
        },
    )()
    app = type("App", (), {"_transcript": host, "thread_id": "thread"})()
    controller = TurnController(app)

    controller._attach_renderer(runtime, runtime.active_context())

    answers = [call for call in host.calls if call[0] == "commit_answer"]
    assert answers == [("commit_answer", ("early",), {})]
    controller._detach_renderer()
