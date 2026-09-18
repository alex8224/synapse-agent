"""Regression tests for the Textual transcript stream sink."""

from __future__ import annotations

from typing import Any

from synapse.ui.textual_stream_sink import TextualStreamSink


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.transcript_generation = 0

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        return callback(*args, **kwargs)

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def test_hidden_reasoning_placeholder_can_be_skipped_without_thought_node() -> None:
    from synapse.ui.stream_events import reasoning_placeholder_text

    host = _Host()
    sink = TextualStreamSink(host)

    placeholder = reasoning_placeholder_text(69, enabled=False)
    if placeholder:
        sink.write_reasoning(placeholder)
    sink.close_reasoning()

    assert "commit_thought" not in [name for name, _, _ in host.calls]
    assert sink.streamed_reasoning is False


def test_reasoning_after_answer_tokens_seals_preview_before_switch() -> None:
    host = _Host()
    sink = TextualStreamSink(host)

    sink.write_answer_token("## 结论\n")
    sink.write_answer_token("完成")
    sink.write_reasoning(
        "(reasoning text not exposed by gateway; ~69 reasoning tokens)\n"
    )
    sink.close_reasoning()
    sink.write_answer_complete("## 结论\n完成", msg_id="msg-1")

    commits = [(name, args) for name, args, _ in host.calls if name == "commit_answer"]
    assert commits == [("commit_answer", ("## 结论\n完成",))]
    assert sink.answer_buf == ["## 结论\n完成"]

    names = [name for name, _, _ in host.calls]
    assert names.index("commit_answer") < names.index("commit_thought")
    assert "commit_thought" in names


def test_sink_drops_callbacks_after_transcript_generation_changes() -> None:
    host = _Host()
    sink = TextualStreamSink(host)

    host.transcript_generation += 1
    sink.write_answer_complete("stale answer", msg_id="stale")
    sink.activity_stop()

    assert host.calls == []


def test_sink_host_protocol_is_explicit() -> None:
    """The sink must depend on the declared transcript host surface."""
    from synapse.ui.textual_stream_sink import TextualStreamHost
    from synapse.ui.transcript.controller import TranscriptController

    assert isinstance(TranscriptController(object()), TextualStreamHost)


def test_pending_approval_forwards_to_host_mount() -> None:
    """HITL interrupts mount an approval block instead of raw JSON text."""
    from synapse.runtime.hitl import PendingAction

    host = _Host()
    sink = TextualStreamSink(host)

    sink.pending_approval(
        [PendingAction(name="execute", args={"command": "ls -la"})],
        raw={"action_requests": []},
    )

    mounts = [args for name, args, _ in host.calls if name == "mount_approval"]
    assert len(mounts) == 1
    pending = mounts[0][0]
    assert len(pending.actions) == 1
    assert pending.actions[0].name == "execute"
    assert pending.actions[0].args == {"command": "ls -la"}
    assert "info" not in [name for name, _, _ in host.calls]


def test_activity_stop_closes_open_reasoning_before_clear_stream() -> None:
    host = _Host()
    sink = TextualStreamSink(host)
    sink.write_reasoning("pondering some more")
    sink.activity_stop()
    names = [name for name, _, _ in host.calls]
    assert "commit_thought" in names
    assert "clear_stream" in names
    assert names.index("commit_thought") < names.index("clear_stream")


def test_clear_stream_seals_substantive_live_thought_block() -> None:
    from synapse.ui.transcript.controller import TranscriptController
    from synapse.ui.transcript_blocks import ThoughtBlock

    class _App:
        settings = type("Settings", (), {"expand_thinking": False})()
        def query_one(self, *args: Any, **kwargs: Any) -> Any:
            raise LookupError()
        def call_after_refresh(self, fn: Any, *args: Any, **kwargs: Any) -> None:
            pass

    ctrl = TranscriptController(_App())
    block = ThoughtBlock(1.0, "Substantive thought", live=True)
    ctrl.state.live_stream_block = block
    ctrl.state.live_stream_kind = "reasoning"
    ctrl.state.thought_blocks.append(block)

    ctrl.clear_stream()
    assert block.live is False
    assert block.body == "Substantive thought"
    assert block in ctrl.state.thought_blocks
    assert ctrl.state.live_stream_block is None
