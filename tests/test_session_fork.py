"""Lightweight conversation fork: projection and lineage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from synapse.sessions.fork import (
    ForkOrigin,
    fork_thin,
    project_messages,
    split_at_turn,
)
from synapse.sessions.store import SessionStore


def _ai(content: str = "", *, calls: list[dict[str, Any]] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=calls or [])


def test_projection_keeps_text_and_drops_tool_results() -> None:
    messages: list[Any] = [
        HumanMessage(content="do the thing"),
        _ai(
            "calling a tool",
            calls=[{"id": "c1", "name": "read_file", "args": {"file_path": "a.py"}}],
        ),
        ToolMessage(content="HUGE OUTPUT " * 1000, tool_call_id="c1"),
        _ai("done: the file reads fine"),
    ]
    projected = project_messages(messages)
    assert [type(m).__name__ for m in projected] == ["HumanMessage", "AIMessage", "AIMessage"]
    assert projected[0].content == "do the thing"
    # Assistant text is preserved, but the tool call is dropped entirely so the
    # model cannot mistake its own tool JSON for prior speech.
    assert projected[1].content == "calling a tool"
    assert "read_file" not in projected[1].content
    assert "a.py" not in projected[1].content
    # Tool output is gone too.
    assert all("HUGE OUTPUT" not in str(m.content) for m in projected)
    assert projected[2].content == "done: the file reads fine"


def test_projection_ids_are_unique_and_text_only() -> None:
    messages: list[Any] = [
        HumanMessage(content="a"),
        _ai("b"),
        HumanMessage(content="c"),
    ]
    projected = project_messages(messages)
    ids = [m.id for m in projected]
    assert len(set(ids)) == len(ids)
    for message in projected:
        assert isinstance(message.content, str) and message.content.strip()
        assert not message.additional_kwargs
        assert not isinstance(message, AIMessage) or not message.tool_calls


def test_projection_drops_tool_only_assistant_turns() -> None:
    # A tool-only assistant turn has no visible text, so it projects to nothing.
    messages: list[Any] = [
        HumanMessage(content="go"),
        _ai("", calls=[{"id": "c1", "name": "shell", "args": {"command": "ls"}}]),
    ]
    projected = project_messages(messages)
    assert [m.content for m in projected] == ["go"]


def test_projection_skips_messages_without_text() -> None:
    messages: list[Any] = [
        HumanMessage(content="   "),
        _ai(""),
    ]
    assert project_messages(messages) == []


def test_projection_never_emits_tool_json() -> None:
    calls = [{"id": f"c{i}", "name": "t", "args": {"i": i}} for i in range(100)]
    projected = project_messages([_ai("x", calls=calls)])
    assert projected[0].content == "x"


def test_split_at_turn_cuts_before_requested_turn() -> None:
    messages: list[Any] = [
        HumanMessage(content="turn0"),
        _ai("reply0"),
        HumanMessage(content="turn1"),
        _ai("reply1"),
    ]
    # turn index 1 => keep everything before the second user message
    kept = split_at_turn(messages, "1")
    assert [m.content for m in kept] == ["turn0", "reply0"]


def test_split_at_turn_none_or_out_of_range_returns_all() -> None:
    messages: list[Any] = [HumanMessage(content="u"), _ai("a")]
    assert split_at_turn(messages, None) == list(messages)
    assert split_at_turn(messages, "9") == list(messages)
    assert split_at_turn(messages, "0") == []


class _RecordingSeeder:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.messages: list[Any] = []

    def seed_messages(self, thread_id: str, messages: list[Any]) -> Any:
        self.thread_id = thread_id
        self.messages = list(messages)
        return None


def test_fork_thin_seeds_projected_messages_and_reports_origin() -> None:
    seeder = _RecordingSeeder()
    result = fork_thin(
        parent_thread_id="parent",
        child_thread_id="child",
        messages=[
            HumanMessage(content="hello"),
            _ai("hi there"),
            ToolMessage(content="noise" * 100, tool_call_id="x"),
        ],
        seeder=seeder,
    )
    assert seeder.thread_id == "child"
    assert len(seeder.messages) == 2
    assert result.child_thread_id == "child"
    assert result.parent_thread_id == "parent"
    assert result.message_count == 2
    assert result.origin == ForkOrigin(parent_thread_id="parent", boundary="latest")


def test_session_store_records_fork_origin(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite")
    store.ensure("child", title="child session")
    assert store.set_fork_origin(
        "child", parent_thread_id="parent", boundary="latest"
    )
    info = store.get("child")
    assert info is not None
    assert info.forked_from_thread_id == "parent"
    assert info.forked_from_boundary == "latest"
    assert info.forked_from_turn_id is None
    assert info.to_dict()["forked_from_thread_id"] == "parent"
    store.close()


def test_fork_thin_end_to_end_through_real_delta_channel() -> None:
    """Fork a real deepagents thread into a fresh one via the seeder."""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import MemorySaver

    from synapse.integrations.checkpoint_seed import CheckpointSeeder
    from synapse.sessions.transcript import load_messages_from_agent

    class _ToolBindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolBindableFakeModel:
            return self

    saver = MemorySaver()
    agent = create_deep_agent(
        model=_ToolBindableFakeModel(responses=[AIMessage(content="Live answer", id="live-ai")]),
        checkpointer=saver,
        tools=[],
        subagents=[],
    )
    agent._coding_checkpointer = saver
    parent = {"configurable": {"thread_id": "parent"}}
    agent.update_state(
        parent,
        {
            "messages": [
                HumanMessage(content="explain the repo", id="p-user"),
                AIMessage(
                    content="reading files",
                    id="p-ai",
                    tool_calls=[
                        {
                            "id": "p-tool",
                            "name": "read_file",
                            "args": {"file_path": "README.md"},
                        }
                    ],
                ),
                ToolMessage(
                    content="very large tool output " * 500, tool_call_id="p-tool"
                ),
                AIMessage(content="here is the summary", id="p-ai2"),
            ]
        },
        as_node="model",
    )
    agent.update_state(parent, None, as_node="__end__")

    messages = load_messages_from_agent(agent, "parent")
    result = fork_thin(
        parent_thread_id="parent",
        child_thread_id="child",
        messages=messages,
        seeder=CheckpointSeeder(agent),
    )

    assert result.message_count == 3
    child_state = agent.get_state({"configurable": {"thread_id": "child"}})
    assert child_state.next == ()
    contents = [str(m.content) for m in child_state.values["messages"]]
    assert contents[0] == "explain the repo"
    assert contents[1] == "reading files"
    assert all("read_file" not in c and "README.md" not in c for c in contents)
    assert contents[2] == "here is the summary"
    assert all("very large tool output" not in c for c in contents)

    # The child is a usable terminal thread: a live turn continues from it.
    output = agent.invoke(
        {"messages": [HumanMessage(content="next", id="c-user")]},
        {"configurable": {"thread_id": "child"}},
    )
    assert output["messages"][-1].content == "Live answer"


def test_service_fork_records_lineage_and_exposes_child(tmp_path: Path) -> None:
    """End-to-end fork through the runtime service, with a real agent."""
    import asyncio
    from types import SimpleNamespace

    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import MemorySaver

    from synapse.runtime.service.access import (
        SESSION_FORK,
        AclAuthorizer,
        AclGrant,
        Principal,
        bind_access,
    )
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.session_management import ForkSessionCommand
    from synapse.runtime.sessions import RuntimeManager
    from synapse.runtime.sessions.ref import SessionRef
    from synapse.sessions.store import SessionStore

    class _ToolBindableFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> _ToolBindableFakeModel:
            return self

    saver = MemorySaver()
    agent = create_deep_agent(
        model=_ToolBindableFakeModel(responses=[AIMessage(content="Live answer", id="live-ai")]),
        checkpointer=saver,
        tools=[],
        subagents=[],
    )
    agent._coding_checkpointer = saver

    db = tmp_path / "sessions.sqlite"
    settings = SimpleNamespace(
        max_concurrency=2,
        model="test",
        resolved_sessions_path=lambda: db,
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        workspace=tmp_path / "workspace",
    )
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: agent,
        project_id="proj",
    )
    service = LocalAgentRuntimeService(lambda requested: manager if requested == "proj" else None)
    bound = bind_access(
        service,
        Principal("user"),
        AclAuthorizer((AclGrant("user", "proj", frozenset({SESSION_FORK})),)),
    )

    async def run() -> Any:
        parent = SessionRef("proj", "parent")
        # Open the parent so its agent is loaded (fork requires an open source).
        await manager.open_session_ref(parent)
        agent.update_state(
            {"configurable": {"thread_id": "parent"}},
            {
                "messages": [
                    HumanMessage(content="hello", id="p-u"),
                    AIMessage(
                        content="reading",
                        id="p-a",
                        tool_calls=[
                            {"id": "t1", "name": "read_file", "args": {"file_path": "x.py"}}
                        ],
                    ),
                    ToolMessage(content="noise" * 500, tool_call_id="t1"),
                    AIMessage(content="done", id="p-a2"),
                ]
            },
            as_node="model",
        )
        agent.update_state(
            {"configurable": {"thread_id": "parent"}}, None, as_node="__end__"
        )
        return await bound.fork_session(ForkSessionCommand(source=parent))

    result = asyncio.run(run())
    assert result.forked_from == "parent"
    assert result.message_count == 3

    store = SessionStore(db)
    child = store.get(result.session.thread_id)
    assert child is not None
    assert child.forked_from_thread_id == "parent"
    assert child.forked_from_boundary == "latest"
    store.close()

    child_state = agent.get_state({"configurable": {"thread_id": result.session.thread_id}})
    assert child_state.next == ()
    assert all("noise" not in str(m.content) for m in child_state.values["messages"])

    # The child's transcript projection is built at fork time, so the history
    # read (projection-only) renders the inherited conversation immediately.
    from synapse.runtime.service.history import ReadSessionHistoryQuery
    from synapse.runtime.service.history_store import read_session_history_page

    page = read_session_history_page(
        settings, ReadSessionHistoryQuery(session=result.session)
    )
    assert page.available is True
    assert page.total_turns >= 1
    assert [event.kind for event in page.events].count("user") >= 1
    assert any("hello" in str(getattr(e, "text", "")) for e in page.events)


def test_service_fork_rejects_an_unopened_source(tmp_path: Path) -> None:
    """A source that is not open is refused instead of copying nothing."""
    import asyncio
    from types import SimpleNamespace

    from synapse.runtime.service.access import (
        SESSION_FORK,
        AclAuthorizer,
        AclGrant,
        Principal,
        bind_access,
    )
    from synapse.runtime.service.errors import InvalidRequestError
    from synapse.runtime.service.local import LocalAgentRuntimeService
    from synapse.runtime.service.session_management import ForkSessionCommand
    from synapse.runtime.sessions import RuntimeManager
    from synapse.runtime.sessions.ref import SessionRef

    manager = RuntimeManager(
        settings=SimpleNamespace(max_concurrency=1, model="test"),
        agent_factory=lambda thread_id, shared: SimpleNamespace(),
        project_id="proj",
    )
    service = LocalAgentRuntimeService(lambda requested: manager if requested == "proj" else None)
    bound = bind_access(
        service,
        Principal("user"),
        AclAuthorizer((AclGrant("user", "proj", frozenset({SESSION_FORK})),)),
    )

    async def run() -> object:
        return await bound.fork_session(
            ForkSessionCommand(source=SessionRef("proj", "never-opened"))
        )

    try:
        asyncio.run(run())
    except InvalidRequestError:
        return
    raise AssertionError("fork of an unopened source must be rejected")
