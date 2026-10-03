"""Collaboration tools must survive the production agent's middleware stack."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from synapse.app.agent import build_coding_agent
from synapse.models.profile import ModelProfile
from synapse.models.registry import ModelRegistry
from synapse.projects.catalog import ProjectCatalog
from synapse.runtime.collaboration.coordinator import CollaborationCoordinator
from synapse.runtime.collaboration.model import DeliveryState, MessageKind, RequestState
from synapse.runtime.collaboration.store import CollaborationStore
from synapse.runtime.collaboration.tools import build_collaboration_tools
from synapse.runtime.daemon.application import RuntimeDaemon
from synapse.runtime.daemon.config import DaemonConfig
from synapse.runtime.sessions.ref import SessionRef
from synapse.settings import Settings


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _Model:
        return self


def _agent(
    root: Path,
    project: str,
    coordinator: CollaborationCoordinator,
    call: dict[str, Any],
) -> Any:
    settings = Settings(
        _env_file=None,
        workspace=root,
        model="openai:test",
        active_model="test",
        checkpoint_backend="memory",
        sessions_path=root / "sessions.sqlite",
        enable_mcp=False,
        enable_subagents=False,
        enable_custom_subagents=False,
        enable_memory=False,
        enable_goals=False,
        skills_paths=[],
    )
    registry = ModelRegistry(
        default="test", profiles={"test": ModelProfile(name="test", model="openai:test")}
    )
    return build_coding_agent(
        settings,
        project_root=root,
        model=_Model(responses=[
            AIMessage(content="", tool_calls=[call]), AIMessage(content="done")
        ]),
        model_registry=registry,
        extra_tools=build_collaboration_tools(coordinator, source_project=project),
        system_prompt="Use the provided collaboration tools. Do not delegate or modify files.",
    )


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("prefix", ["", "@session:"], ids=["bare", "pill"])
def test_contact_and_reply_through_production_agent(
    tmp_path: Path, async_mode: bool, prefix: str,
) -> None:
    catalog = ProjectCatalog(tmp_path / "catalog.sqlite")
    daemon = RuntimeDaemon(DaemonConfig(state_dir=tmp_path / "daemon"))
    daemon.catalog = catalog
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()
    source = SessionRef(catalog.register_project(source_root, detect_git=False).project_id, "a")
    target = SessionRef(catalog.register_project(target_root, detect_git=False).project_id, "b")

    def invoke(agent: Any, text: str, configurable: dict[str, Any]) -> dict[str, Any]:
        inputs = {"messages": [{"role": "user", "content": text}]}
        config = {"configurable": configurable}
        if async_mode:
            return asyncio.run(agent.ainvoke(inputs, config))
        return agent.invoke(inputs, config)

    try:
        with CollaborationStore(tmp_path / "collaboration.sqlite") as store:
            coordinator = CollaborationCoordinator(
                store, allows=daemon._collaboration_allowed,
                reachable=daemon._collaboration_reachable,
            )
            source_agent = _agent(source_root, source.project_id, coordinator, {
                "name": "contact_session", "id": "contact-1", "type": "tool_call",
                "args": {"intent": "request a check", "target": prefix + target.global_id,
                         "request": "Confirm the interface", "expect": "a conclusion"},
            })
            result = invoke(
                source_agent, "Ask the selected session", {"thread_id": source.thread_id}
            )
            tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
            assert len(tool_messages) == 1
            assert str(tool_messages[0].content).startswith("accepted:"), tool_messages[0].content
            delivery = store.claim_next(target)
            assert delivery is not None
            request = store.get_request(delivery.request_id, source)
            assert request.source == source and request.target == target
            store.transition_delivery(
                delivery.id, target, DeliveryState.RUNNING,
                expected_version=delivery.version, turn_id="target-turn",
            )
            target_agent = _agent(target_root, target.project_id, coordinator, {
                "name": "reply_to_collaboration", "id": "reply-1", "type": "tool_call",
                "args": {"intent": "deliver the conclusion", "message": "POST /login",
                         "kind": "reply"},
            })
            reply = invoke(target_agent, request.body, {
                "thread_id": target.thread_id,
                "collaboration": {"role": "target", "request_id": request.id,
                                  "delivery_id": delivery.id, "anchor_turn_id": "target-turn"},
            })
            replies = [m for m in reply["messages"] if isinstance(m, ToolMessage)]
            assert len(replies) == 1
            assert str(replies[0].content).startswith("recorded:"), replies[0].content
            assert store.get_request(request.id, source).state is RequestState.REPLIED
            messages = store.messages(request.id, source, limit=10)
            assert [m.kind for m in messages] == [MessageKind.REQUEST, MessageKind.REPLY]
            assert messages[-1].body == "POST /login"
            assert len(store.deliveries(source, limit=10)) == 1
    finally:
        catalog.close()


def test_progress_through_production_agent_is_notification_only(tmp_path: Path) -> None:
    source, target = SessionRef('p1', 'a'), SessionRef('p2', 'b')
    with CollaborationStore(tmp_path / 'db') as store:
        coordinator = CollaborationCoordinator(store)
        group = coordinator.begin_work(source, 'work')
        request = coordinator.request(group.id, source, target, operation_key='request',
                                      body='check', anchor_turn_id='')
        delivery = store.claim_next(target)
        store.transition_delivery(delivery.id, target, DeliveryState.RUNNING,
                                  expected_version=delivery.version, turn_id='target')
        agent = _agent(tmp_path, target.project_id, coordinator, {
            'name': 'reply_to_collaboration', 'id': 'progress', 'type': 'tool_call',
            'args': {'intent': 'report progress', 'message': 'checking compatibility',
                     'kind': 'progress'},
        })
        result = agent.invoke({'messages': [{'role': 'user', 'content': 'report progress'}]}, {
            'configurable': {'thread_id': target.thread_id, 'collaboration': {
                'role': 'target', 'request_id': request.id, 'delivery_id': delivery.id,
            }},
        })
        tools = [m for m in result['messages'] if isinstance(m, ToolMessage)]
        assert str(tools[-1].content).startswith('recorded:')
        assert store.messages(request.id, source)[-1].kind is MessageKind.PROGRESS
        assert store.claim_next(source) is None
