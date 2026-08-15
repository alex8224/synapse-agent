"""P3 Textual TurnEvent renderer and bridge contracts."""

from __future__ import annotations

from typing import Any

from synapse.runtime.sessions import SessionEventBroker
from synapse.runtime.streaming import (
    EVENT_VERSION,
    TextPayload,
    ToolBatchPayload,
    ToolCallPayload,
    ToolResultPayload,
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


def test_event_renderer_routes_legacy_tool_result_to_tool_group() -> None:
    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")

    renderer.emit(
        _event(
            1,
            TurnEventKind.TOOL_BATCH_STARTED,
            ToolBatchPayload(
                calls=(ToolCallPayload("call-1", "read_file", "{'file_path': '/a.py'}"),),
                parallel=False,
            ),
        )
    )
    renderer.emit(
        _event(
            2,
            TurnEventKind.TOOL_RESULT,
            ToolResultPayload("read_file", "ok (20 chars, 2 lines)"),
        )
    )

    assert not [
        call
        for call in host.calls
        if call[0] == "append_meta" and "{'tool':" in str(call[1])
    ]
    headers = [call for call in host.calls if call[0] == "write_tool_group_header"]
    assert headers
    assert "read" in str(headers[-1][1]).lower()


def test_event_renderer_groups_multiple_items_from_one_batch() -> None:
    from synapse.runtime.streaming import ToolItemPayload

    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    calls = (
        ToolCallPayload("call-1", "read_file", "{'file_path': '/a.py'}"),
        ToolCallPayload("call-2", "search_files", "{'pattern': 'TODO'}"),
    )
    renderer.emit(
        _event(1, TurnEventKind.TOOL_BATCH_STARTED, ToolBatchPayload(calls, parallel=True))
    )
    for sequence, (item_id, call_id, name, category, label) in enumerate(
        (
            ("g1-0", "call-1", "read_file", "read", "Read a.py"),
            ("g1-1", "call-2", "search_files", "search", "Searched TODO"),
        ),
        start=2,
    ):
        renderer.emit(
            _event(
                sequence,
                TurnEventKind.TOOL_STARTED,
                ToolItemPayload(
                    item_id=item_id,
                    call_id=call_id,
                    name=name,
                    category=category,
                    label=label,
                    path=None,
                    status="running",
                    preview=None,
                    error=False,
                    sub=False,
                    parent_id=None,
                ),
            )
        )

    group_headers = [call for call in host.calls if call[0] == "write_tool_group_header"]
    item_rows = [call for call in host.calls if call[0] == "write_tool_item"]
    assert len(group_headers) == 1
    assert len(item_rows) == 2
    assert any(call[0] == "update_tool_group_header" for call in host.calls)


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


def test_renderer_logs_bounded_warning_on_render_failure(caplog: Any) -> None:
    """F2: renderer failure logs locating fields without leaking the payload."""
    import logging

    class _BoomHost(_Host):
        def commit_answer(self, text: str) -> None:
            del text
            raise RuntimeError("sink exploded")

    host = _BoomHost()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")

    with caplog.at_level(logging.WARNING, logger="synapse.ui.turn.event_renderer"):
        renderer.emit(
            _event(7, TurnEventKind.ANSWER_COMPLETED, TextPayload("TOP-SECRET-BODY"))
        )

    assert renderer.closed is True
    messages = [record.getMessage() for record in caplog.records]
    assert messages, "expected a renderer warning"
    warning = messages[0]
    assert "thread=thread" in warning
    assert "turn=turn" in warning
    assert "kind=ANSWER_COMPLETED" in warning
    assert "seq=7" in warning
    assert "error=RuntimeError" in warning
    assert "TOP-SECRET-BODY" not in warning


def test_renderer_keeps_ignoring_stale_events_without_warning(caplog: Any) -> None:
    """F2: stale generation / duplicate sequence stay silent (no false alarms)."""
    import logging

    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    host.transcript_generation = 2

    with caplog.at_level(logging.WARNING, logger="synapse.ui.turn.event_renderer"):
        renderer.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("stale")))
        renderer.emit(_event(1, TurnEventKind.ANSWER_DELTA, TextPayload("dup")))

    assert caplog.records == []


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


def test_turn_controller_uses_non_blocking_ui_wakeup_when_available() -> None:
    class _AsyncHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.callbacks: list[Any] = []
            self.blocking_calls = 0

        def call_after_refresh(self, callback: Any, *args: Any, **kwargs: Any) -> bool:
            self.callbacks.append(lambda: callback(*args, **kwargs))
            return True

        def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
            del callback, args, kwargs
            self.blocking_calls += 1
            # Textual raises this when the sink is already executing on the UI
            # thread; TextualStreamSink then applies the DOM update inline.
            raise RuntimeError("already on UI thread")

    host = _AsyncHost()
    broker = SessionEventBroker("thread")
    runtime = type(
        "Runtime",
        (),
        {
            "thread_id": "thread",
            "broker": broker,
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
    controller.attach(runtime, after_sequence=0)

    broker.emit(_event(1, TurnEventKind.ANSWER_COMPLETED, TextPayload("hello", "m1")))

    assert host.calls == []
    assert len(host.callbacks) == 1
    assert host.blocking_calls == 0
    host.callbacks.pop()()
    assert host.blocking_calls == 1
    assert [call for call in host.calls if call[0] == "commit_answer"] == [
        ("commit_answer", ("hello",), {})
    ]
    controller._detach_renderer()


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


def test_bridge_replay_batch_accumulates_tool_writes_per_batch() -> None:
    """Replay drains tool events through one begin/end batch pair per batch."""
    from synapse.runtime.streaming import ToolItemPayload

    host = _Host()
    renderer = TextualTurnEventRenderer(host, thread_id="thread", turn_id="turn")
    callbacks: list[Any] = []
    bridge = TextualTurnEventBridge(renderer, callbacks.append)

    def item(sequence: int, item_id: str, name: str, category: str, label: str) -> TurnEvent:
        return _event(
            sequence,
            TurnEventKind.TOOL_STARTED,
            ToolItemPayload(
                item_id=item_id,
                call_id=item_id,
                name=name,
                category=category,
                label=label,
                path=None,
                status="running",
                preview=None,
                error=False,
                sub=False,
                parent_id=None,
            ),
        )

    bridge.replay_batch(
        [
            item(1, "g1-0", "read_file", "read", "Read a.py"),
            item(2, "g1-1", "search_files", "search", "Searched TODO"),
        ]
    )

    assert len(callbacks) == 1
    callbacks.pop()()

    begin = [c for c in host.calls if c[0] == "begin_tool_batch"]
    end = [c for c in host.calls if c[0] == "end_tool_batch"]
    items = [c for c in host.calls if c[0] == "write_tool_item"]
    assert len(begin) == 1
    assert len(end) == 1
    assert len(items) == 2


def test_tool_group_block_batch_flush_renders_once(monkeypatch: Any) -> None:
    """``render=False`` accumulates writes; ``flush`` renders once."""
    from synapse.ui.timeline import ToolItem
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    renders = 0

    def counting_render() -> None:
        nonlocal renders
        renders += 1

    monkeypatch.setattr(block, "_render_block", counting_render)

    def item(item_id: str, name: str, category: str, label: str) -> ToolItem:
        return ToolItem(
            id=item_id,
            name=name,
            category=category,
            label=label,
            path=None,
            status="running",
            preview=None,
            error=False,
            sub=False,
            parent_id=None,
            call_id=item_id,
        )

    block.add_item(item("a", "read_file", "read", "Read a.py"), render=False)
    block.add_item(item("b", "search_files", "search", "Searched TODO"), render=False)
    block.set_collapsed(False, render=False)
    assert renders == 0

    block.flush()
    assert renders == 1
    assert len(block.items) == 2


def _tool_item(
    item_id: str,
    *,
    status: str = "done",
    error: bool = False,
    sub: bool = False,
    parent_id: str | None = None,
    name: str = "read_file",
    category: str = "read",
) -> Any:
    from synapse.ui.timeline import ToolItem

    return ToolItem(
        id=item_id,
        name=name,
        category=category,
        label=f"{name} {item_id}",
        path=None,
        status=status,
        preview=None,
        error=error,
        sub=sub,
        parent_id=parent_id,
        call_id=item_id,
    )


def _group_parent_ids(groups: Any) -> list[str]:
    return [parent.id for parent, _ in groups]


def test_tool_group_block_overflow_keeps_newest_and_live_visible() -> None:
    """Overflow folds oldest completed rows, never the running/latest ones."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    for i in range(1, 14):
        block.add_item(_tool_item(f"n{i}"), render=False)
    block.add_item(_tool_item("running", status="running"), render=False)

    groups = block._grouped_items()
    visible, overflow = block._select_visible_groups(groups)
    ids = _group_parent_ids(visible)

    assert "running" in ids, "the in-flight row must stay visible"
    assert "n1" not in ids and "n2" not in ids, "oldest completed rows should fold"
    assert "n13" in ids, "newest completed rows should remain"
    assert len(visible) == 12
    assert overflow == 2


def test_tool_group_block_overflow_keeps_error_visible() -> None:
    """An old errored row is never folded away by newer completed rows."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    block.add_item(_tool_item("e1"), render=False)
    block.add_item(_tool_item("boom", error=True), render=False)
    for i in range(2, 14):
        block.add_item(_tool_item(f"n{i}"), render=False)
    block.add_item(_tool_item("running", status="running"), render=False)

    groups = block._grouped_items()
    visible, overflow = block._select_visible_groups(groups)
    ids = _group_parent_ids(visible)

    assert "boom" in ids, "errored row must stay visible"
    assert "running" in ids
    assert overflow == 3


def test_tool_group_block_groups_sub_items_by_parent() -> None:
    """Interleaved subagent calls stay attached to their own parent row."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    block.add_item(_tool_item("taskA", name="task", category="task"), render=False)
    block.add_item(
        _tool_item("a1", sub=True, parent_id="taskA", name="read_file"),
        render=False,
    )
    block.add_item(_tool_item("taskB", name="task", category="task"), render=False)
    block.add_item(
        _tool_item("b1", sub=True, parent_id="taskB", name="search_files"),
        render=False,
    )
    block.add_item(
        _tool_item("a2", sub=True, parent_id="taskA", name="read_file"),
        render=False,
    )

    groups = block._grouped_items()
    assert [(p.id, [s.id for s in subs]) for p, subs in groups] == [
        ("taskA", ["a1", "a2"]),
        ("taskB", ["b1"]),
    ]


def test_tool_group_block_drops_orphan_sub_items() -> None:
    """A sub-item with an unknown parent is never misattributed."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    block.add_item(_tool_item("taskA", name="task", category="task"), render=False)
    block.add_item(
        _tool_item("orphan", sub=True, parent_id="ghost", name="read_file"),
        render=False,
    )
    block.add_item(_tool_item("taskB", name="task", category="task"), render=False)

    groups = block._grouped_items()
    assert [(p.id, [s.id for s in subs]) for p, subs in groups] == [
        ("taskA", []),
        ("taskB", []),
    ]


def test_tool_group_block_shows_recent_three_subs_per_parent() -> None:
    """Each subagent shows only its three most recent nested calls."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    subs = [
        _tool_item(f"s{i}", sub=True, parent_id="taskA", name="read_file")
        for i in range(1, 6)
    ]

    visible, overflow = block._visible_subs(subs)
    assert [s.id for s in visible] == ["s3", "s4", "s5"]
    assert overflow == 2


def test_tool_group_block_keeps_live_subs_visible() -> None:
    """Running/errored nested calls are never folded into the earlier line."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    subs = [
        _tool_item("s1", sub=True, parent_id="taskA", status="running"),
        _tool_item("s2", sub=True, parent_id="taskA", error=True, status="done"),
        _tool_item("s3", sub=True, parent_id="taskA"),
        _tool_item("s4", sub=True, parent_id="taskA"),
        _tool_item("s5", sub=True, parent_id="taskA"),
    ]

    visible, overflow = block._visible_subs(subs)
    ids = [s.id for s in visible]
    assert "s1" in ids and "s2" in ids, "live nested calls must stay visible"
    assert len(visible) == 3
    assert overflow == 2


def _rendered_lines(block: Any) -> list[str]:
    cache = getattr(block, "_layout_cache", None)
    if cache is not None:
        cache.clear()
    visual = block._render()
    group = getattr(visual, "_renderable", visual)
    renderables = getattr(group, "renderables", None) or ()
    return [str(getattr(r, "plain", r)) for r in renderables]


def test_tool_group_block_renders_subagent_phase() -> None:
    """A running subagent row shows a transient thinking/answering stage."""
    from synapse.ui.tool_blocks import ToolGroupBlock

    block = ToolGroupBlock("tools")
    block.add_item(
        _tool_item("taskA", name="task", category="task", status="running"),
        render=False,
    )

    block.set_subagent_phase("taskA", "thinking", render=False)
    block._render_block()
    assert any("thinking" in line for line in _rendered_lines(block))

    block.set_subagent_phase("taskA", "answering", render=False)
    block._render_block()
    lines = _rendered_lines(block)
    assert any("answering" in line for line in lines)
    assert not any("thinking" in line for line in lines)

    block.set_subagent_phase("taskA", None, render=False)
    block._render_block()
    lines = _rendered_lines(block)
    assert not any("thinking" in line or "answering" in line for line in lines)


# --------------------------------------------------------------------------- #
# subagent metadata payload round-trip
# --------------------------------------------------------------------------- #


def test_tool_item_payload_round_trips_subagent_metadata() -> None:
    """ToolItem -> ToolItemPayload -> ToolItem must keep all metadata."""
    import json

    from synapse.runtime.streaming.events import tool_item_payload
    from synapse.runtime.streaming.tool_model import ToolItem
    from synapse.ui.turn.event_renderer import _tool_item

    item = ToolItem(
        id="g1-0",
        name="task",
        category="task",
        label="审查修复",
        path=None,
        status="running",
        error=False,
        sub=False,
        call_id="task-1",
        subagent_name="reviewer",
        subagent_model="gpt-5.2",
        subagent_reasoning_effort="high",
        subagent_model_inherited=False,
        subagent_reasoning_inherited=True,
    )
    payload = tool_item_payload(item)
    restored = _tool_item(payload)
    assert restored.subagent_name == "reviewer"
    assert restored.subagent_model == "gpt-5.2"
    assert restored.subagent_reasoning_effort == "high"
    assert restored.subagent_model_inherited is False
    assert restored.subagent_reasoning_inherited is True

    # The event envelope serializes the new fields and stays JSON-safe.
    event = _event(1, TurnEventKind.TOOL_BATCH_STARTED, payload)
    dumped = json.dumps(event.to_dict(), ensure_ascii=False)
    assert '"subagent_name": "reviewer"' in dumped
    assert '"subagent_model": "gpt-5.2"' in dumped


def test_tool_item_payload_defaults_for_legacy_items() -> None:
    """Payloads without subagent fields (older events) must degrade cleanly."""
    import json
    from dataclasses import asdict

    from synapse.runtime.streaming.events import tool_item_payload
    from synapse.runtime.streaming.tool_model import ToolItem
    from synapse.ui.turn.event_renderer import _tool_item

    item = ToolItem(id="g1-0", name="read_file", category="read", label="Read a.py")
    payload = tool_item_payload(item)
    assert payload.subagent_name is None
    assert payload.subagent_model is None
    assert payload.subagent_reasoning_effort is None
    assert payload.subagent_model_inherited is False
    assert payload.subagent_reasoning_inherited is False

    restored = _tool_item(payload)
    assert restored.subagent_name is None
    assert restored.subagent_model is None
    assert json.dumps(asdict(payload))  # default fields are JSON-safe
