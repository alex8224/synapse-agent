"""PTC sandbox tool calls streamed through the ``custom`` mode.

The PTC middleware writes dicts via ``ToolRuntime.stream_writer``; the parser
turns the recognized ``ptc_tool`` shape into nested ``ToolItem`` lifecycle calls
and must ignore everything else.  These tests pin the mapping, the parent
resolution (a *call id*, not a namespace or item id), the bounded orphan queue
and the terminal cleanup that keeps a child from spinning forever.
"""

from __future__ import annotations

import threading
from typing import Any

from synapse.ui.stream import stream_agent


class _Chunk:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _ToolMsg:
    def __init__(self, name: str, content: str, tool_call_id: str) -> None:
        self.type = "tool"
        self.name = name
        self.content = content
        self.tool_call_id = tool_call_id
        self.id = f"result-{tool_call_id}"


class _PtcSink:
    """Enhanced sink: records the per-item lifecycle the parser emits."""

    streamed_answer = False
    streamed_reasoning = False

    def __init__(self) -> None:
        self.answer_buf: list[str] = []
        self.reasoning_buf: list[str] = []
        self.started: list[tuple[str, str, str | None, str | None]] = []
        self.finished: list[tuple[str, str, str | None, bool]] = []

    # -- renderer surface the InstrumentedStreamSink forwards to ------------
    def activity_start(self, phase: str = "thinking", detail: str = "") -> None:
        return None

    def activity_update(
        self,
        phase: str,
        detail: str = "",
        *,
        reset_timer: bool = False,
        force: bool = False,
    ) -> None:
        return None

    def activity_stop(self) -> None:
        return None

    def write_reasoning(self, text: str) -> None:
        self.reasoning_buf.append(text)

    def close_reasoning(self) -> None:
        return None

    def write_answer_token(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True

    def write_answer_complete(self, text: str, *, msg_id: str | None = None) -> None:
        self.answer_buf.append(text)
        self.streamed_answer = True

    def finalize_line(self) -> None:
        return None

    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None:
        return None

    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None:
        return None

    def tool_item_started(self, item: Any) -> None:
        self.started.append((item.id, item.name, item.parent_id, item.call_id))

    def tool_item_updated(self, item: Any) -> None:
        return None

    def tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None:
        self.finished.append((item_id, status, preview, error))

    def tool_group_closed(self, group_id: str) -> None:
        return None

    def subagent_phase(self, parent_id: str, phase: str | None) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def note_usage(self, **kwargs: Any) -> None:
        return None


class _PtcAgent:
    """Replay a fixed list of raw stream items; kwargs are ignored."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def stream(self, payload: Any, config: Any = None, **kwargs: Any):
        del payload, config, kwargs
        yield from self.script


def _run_code_batch(call_ids: list[str], *, msg_id: str = "m1") -> Any:
    return (
        "updates",
        {
            "model": {
                "messages": [
                    _Chunk(
                        type="ai",
                        content="",
                        id=msg_id,
                        tool_calls=[
                            {
                                "name": "run_code",
                                "args": {"intent": f"script {call_id}"},
                                "id": call_id,
                            }
                            for call_id in call_ids
                        ],
                    )
                ]
            }
        },
    )


def _ptc(event: str, parent: str, call_id: str, name: str, **extra: Any) -> Any:
    payload = {
        "type": "ptc_tool",
        "event": event,
        "parent_call_id": parent,
        "call_id": call_id,
        "name": name,
    }
    payload.update(extra)
    return ("custom", payload)


def _run_ptc(script: list[Any], *, cancel_event: threading.Event | None = None):
    sink = _PtcSink()
    result = stream_agent(
        _PtcAgent(script),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
        cancel_event=cancel_event,
    )
    return sink, result


def _by_name(sink: _PtcSink, name: str) -> list[tuple]:
    return [event for event in sink.started if event[1] == name]


def test_custom_stream_mode_is_requested() -> None:
    """The parser must ask LangGraph for ``custom`` (that is the PTC channel)."""
    seen: list[Any] = []

    class _Agent:
        def stream(self, payload: Any, config: Any = None, **kwargs: Any):
            seen.append(kwargs.get("stream_mode"))
            yield (
                "updates",
                {"model": {"messages": [_Chunk(type="ai", content="hi", id="m1")]}},
            )

    stream_agent(
        _Agent(),
        payload={"messages": []},
        config={},
        token_stream=True,
        prefer_async=False,
        subgraphs=False,
        sink=_PtcSink(),
    )
    assert seen, "the agent must be streamed"
    assert "custom" in seen[0], seen[0]
    assert "messages" in seen[0] and "updates" in seen[0], seen[0]


def test_ptc_children_attach_to_their_own_parent_by_call_id() -> None:
    """Two sandboxes interleave; each child lands under its own ``run_code``."""
    script = [
        _run_code_batch(["call-r1", "call-r2"]),
        _ptc("started", "call-r1", "c1", "read_file", args={"intent": "read a"}),
        _ptc("started", "call-r2", "c2", "execute", args={"intent": "run b"}),
        _ptc("finished", "call-r2", "c2", "execute", status="success", preview="b done"),
        _ptc("finished", "call-r1", "c1", "read_file", status="error", preview="a failed"),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done r1", "call-r1")]}},
        ),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done r2", "call-r2")]}},
        ),
    ]
    sink, result = _run_ptc(script)

    parents = _by_name(sink, "run_code")
    assert [event[0] for event in parents] == ["g1-0", "g1-1"]
    assert [event[3] for event in parents] == ["call-r1", "call-r2"]

    children = _by_name(sink, "read_file") + _by_name(sink, "execute")
    assert len(children) == 2, sink.started
    by_call = {event[3]: event for event in children}
    assert by_call["c1"][2] == "g1-0", "read_file must hang off call-r1's item"
    assert by_call["c2"][2] == "g1-1", "execute must hang off call-r2's item"

    finished = {event[0]: event for event in sink.finished}
    assert finished["g1-0-ptc-c1"][1] == "error"
    assert finished["g1-0-ptc-c1"][2] == "a failed"
    assert finished["g1-0-ptc-c1"][3] is True
    assert finished["g1-1-ptc-c2"][1] == "ok"
    assert finished["g1-1-ptc-c2"][2] == "b done"
    assert finished["g1-1-ptc-c2"][3] is False
    # Custom events are not model history.
    assert result.final_text == ""


def test_unrelated_custom_payloads_are_ignored() -> None:
    """Only ``type == 'ptc_tool'`` may open a tool item."""
    script = [
        _run_code_batch(["call-r1"]),
        ("custom", {"type": "some_other_channel", "event": "started"}),
        ("custom", "not a dict"),
        ("custom", {"type": "ptc_tool", "event": "started"}),
        ("custom", None),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done", "call-r1")]}},
        ),
    ]
    sink, _ = _run_ptc(script)

    assert [event[1] for event in sink.started] == ["run_code"]
    # The parent's own status is the runtime summary of its result body.
    assert [event[0] for event in sink.finished] == ["g1-0"]
    assert sink.finished[0][2] == "done"
    assert sink.finished[0][3] is False


def test_unknown_parent_event_does_not_pollute_a_task() -> None:
    """An orphan PTC event is dropped, never attached to another running call."""
    script = [
        _run_code_batch(["call-r1"], msg_id="m1"),
        (
            "updates",
            {
                "model": {
                    "messages": [
                        _Chunk(
                            type="ai",
                            content="",
                            id="m2",
                            tool_calls=[
                                {"name": "task", "args": {"description": "sub"}, "id": "task-1"}
                            ],
                        )
                    ]
                }
            },
        ),
        _ptc("started", "missing-parent", "orphan", "read_file", args={"intent": "x"}),
        _ptc("finished", "missing-parent", "orphan", "read_file", status="success"),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done", "call-r1")]}},
        ),
    ]
    sink, _ = _run_ptc(script)

    assert [event[1] for event in sink.started] == ["run_code", "task"]
    # The orphan's name never appears, and nothing hangs off the task.
    assert not _by_name(sink, "read_file")
    assert all(event[2] is None for event in sink.started if event[1] == "task")
    assert not [event for event in sink.finished if "orphan" in event[0]]


def test_deferred_event_flushes_when_the_parent_appears() -> None:
    """A child seen before its parent row waits in the bounded queue."""
    script = [
        _ptc("started", "call-r1", "c1", "read_file", args={"intent": "read"}),
        _ptc("finished", "call-r1", "c1", "read_file", status="success", preview="ok"),
        _run_code_batch(["call-r1"]),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done", "call-r1")]}},
        ),
    ]
    sink, _ = _run_ptc(script)

    # The parent row is emitted first, then its deferred child is flushed.
    assert [event[1] for event in sink.started] == ["run_code", "read_file"], sink.started
    child = sink.started[1]
    assert child[2] == "g1-0", "the deferred child must bind to the late parent"
    finished = {event[0]: event for event in sink.finished}
    assert finished["g1-0-ptc-c1"] == ("g1-0-ptc-c1", "ok", "ok", False)


def test_parent_finish_seals_a_child_that_never_reported() -> None:
    """A running child cannot outlive the sandbox that owns it."""
    script = [
        _run_code_batch(["call-r1"]),
        _ptc("started", "call-r1", "c1", "execute", args={"intent": "loop"}),
        (
            "updates",
            {"tools": {"messages": [_ToolMsg("run_code", "done", "call-r1")]}},
        ),
    ]
    sink, _ = _run_ptc(script)

    finished = {event[0]: event for event in sink.finished}
    assert finished["g1-0-ptc-c1"][1] == "ok"
    assert finished["g1-0-ptc-c1"][3] is False
    # The parent itself also lands in a terminal state (its result summary).
    assert "g1-0" in finished
    assert finished["g1-0"][3] is False


def test_parent_cancel_seals_a_running_child() -> None:
    """Cancelling the turn must not leave the sandbox child spinning."""
    cancel = threading.Event()
    script = [
        _run_code_batch(["call-r1"]),
        _ptc("started", "call-r1", "c1", "execute", args={"intent": "loop"}),
    ]

    class _CancelAgent(_PtcAgent):
        def stream(self, payload: Any, config: Any = None, **kwargs: Any):
            del payload, config, kwargs
            yield from self.script
            cancel.set()
            yield ("updates", {"model": {"messages": []}})

    sink = _PtcSink()
    result = stream_agent(
        _CancelAgent(script),
        payload={"messages": []},
        config={},
        token_stream=False,
        prefer_async=False,
        subgraphs=False,
        sink=sink,
        cancel_event=cancel,
    )

    assert result.cancelled is True
    finished = {event[0]: event for event in sink.finished}
    assert finished["g1-0-ptc-c1"][1] == "cancelled"
    assert finished["g1-0-ptc-c1"][3] is True
