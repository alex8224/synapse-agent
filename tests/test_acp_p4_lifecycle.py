"""ACP P4 persistent session lifecycle tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from acp.helpers import update_agent_message_text, update_agent_thought_text
from acp.schema import InitializeResponse

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.sessions import ACPManagedSession, ACPSessionDescriptor, ACPSessionRegistry
from tests.acp_service_fakes import FakeAgentRuntimeService, FakeOwner, simple_managed


class _Checkpointer:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _ForkCheckpointer:
    def __init__(self) -> None:
        self.copies: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.fail_copy = False

    async def acopy_thread(self, thread_id: str, target: str) -> None:
        if self.fail_copy:
            raise RuntimeError("simulated copy failure")
        self.copies.append((thread_id, target))

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


def test_catalog_persists_scope_and_uses_opaque_cursor(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    first = ACPSessionCatalog(path)
    one = first.create(cwd=tmp_path / "one", additional_directories=(tmp_path / "extra",))
    two = first.create(cwd=tmp_path / "two")
    assert one.session_id.startswith("sess_")
    items, cursor = first.list_page(cwd=tmp_path / "one")
    assert [item.session_id for item in items] == [one.session_id]
    assert cursor is None
    first.close()

    second = ACPSessionCatalog(path)
    loaded = second.get(one.session_id)
    assert loaded is not None
    assert loaded.cwd == tmp_path / "one"
    assert loaded.additional_directories == (tmp_path / "extra",)
    assert second.delete(two.session_id) is True
    second.close()


def test_catalog_preserves_multiple_updates_per_runtime_sequence(tmp_path: Path) -> None:
    catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
    session = catalog.create(cwd=tmp_path)
    catalog.append_update(session.session_id, 4, update_agent_message_text("a"), update_index=0)
    catalog.append_update(session.session_id, 4, update_agent_thought_text("b"), update_index=1)
    history = catalog.updates(session.session_id)
    assert [item["sessionUpdate"] for item in history] == [
        "agent_message_chunk",
        "agent_thought_chunk",
    ]
    catalog.close()


def test_catalog_update_history_is_bounded(tmp_path: Path) -> None:
    from synapse.acp.lifecycle import _MAX_UPDATES_PER_SESSION

    catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
    session = catalog.create(cwd=tmp_path)
    for i in range(_MAX_UPDATES_PER_SESSION + 50):
        catalog.append_update(session.session_id, i, update_agent_message_text(f"m{i}"))
    history = catalog.updates(session.session_id)
    assert len(history) == _MAX_UPDATES_PER_SESSION
    assert history[0]["sessionUpdate"] == "agent_message_chunk"
    catalog.close()


def test_agent_load_list_close_delete_lifecycle(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        checkpointer = _Checkpointer()

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            async def delete() -> None:
                await checkpointer.adelete_thread(descriptor.thread_id)
            return simple_managed(
                descriptor, FakeAgentRuntimeService(), owner=FakeOwner(),
                delete_session_state=delete,
            )

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        initialized = await agent.initialize(1)
        assert isinstance(initialized, InitializeResponse)
        created = await agent.new_session(
            str(tmp_path), additional_directories=[str(tmp_path / "x")]
        )
        listed = await agent.list_sessions(cwd=str(tmp_path))
        assert [item.session_id for item in listed.sessions] == [created.session_id]
        await agent.close_session(created.session_id)
        loaded = await agent.load_session(str(tmp_path), created.session_id)
        assert loaded is not None
        await agent.delete_session(created.session_id)
        assert (await agent.list_sessions(cwd=str(tmp_path))).sessions == []
        assert checkpointer.deleted == [created.session_id]
        await agent.shutdown()

    asyncio.run(run())


def test_illegal_lifecycle_transitions_are_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        import acp

        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        await agent.initialize(1)
        created = await agent.new_session(str(tmp_path))

        for coro in (
            agent.load_session(str(tmp_path), "missing"),
            agent.delete_session("missing"),
            agent.close_session("missing"),
            agent.fork_session("missing", str(tmp_path)),
            agent.prompt("missing", [{"type": "text", "text": "hi"}]),
            agent.cancel("missing"),
            agent.set_config_option("approval", "missing", True),
        ):
            with pytest.raises(acp.RequestError, match="Resource not found"):
                await coro

        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await agent.load_session(str(other), created.session_id)
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await agent.set_session_mode(created.session_id, "safety")
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await agent.set_config_option("missing-option", created.session_id, True)
        await agent.shutdown()

    asyncio.run(run())


def test_fork_without_checkpoint_copy_is_rejected(tmp_path: Path) -> None:
    async def run() -> None:
        import acp

        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        await agent.initialize(1)
        created = await agent.new_session(str(tmp_path))
        with pytest.raises(acp.RequestError, match="Internal error") as excinfo:
            await agent.fork_session(created.session_id, str(tmp_path))
        assert excinfo.value.data == {
            "details": "checkpoint backend does not support session fork"
        }
        await agent.shutdown()

    asyncio.run(run())


def test_fork_copies_checkpoint_and_keeps_child_independent(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        checkpointer = _ForkCheckpointer()

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            async def copy(target: str) -> None:
                await checkpointer.acopy_thread(descriptor.thread_id, target)
            async def delete() -> None:
                await checkpointer.adelete_thread(descriptor.thread_id)
            return simple_managed(
                descriptor, FakeAgentRuntimeService(), copy_session_state=copy,
                delete_session_state=delete,
            )

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        await agent.initialize(1)
        parent = await agent.new_session(str(tmp_path))
        child = await agent.fork_session(parent.session_id, str(tmp_path))

        assert child.session_id != parent.session_id
        assert checkpointer.copies == [(parent.session_id, child.session_id)]
        # Parent and child coexist as distinct catalog and registry entries.
        assert catalog.get(parent.session_id) is not None
        assert catalog.get(child.session_id) is not None
        assert agent.sessions.get(parent.session_id) is not None
        assert agent.sessions.get(child.session_id) is not None
        assert agent.sessions.get(child.session_id) is not agent.sessions.get(parent.session_id)
        # Fork inherits mode/config without sharing mutable state.
        assert catalog.get(child.session_id).config == catalog.get(parent.session_id).config
        await agent.shutdown()

    asyncio.run(run())


def test_fork_copy_failure_rolls_back_child_resources(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        checkpointer = _ForkCheckpointer()

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            async def copy(target: str) -> None:
                await checkpointer.acopy_thread(descriptor.thread_id, target)
            async def delete() -> None:
                await checkpointer.adelete_thread(descriptor.thread_id)
            return simple_managed(
                descriptor, FakeAgentRuntimeService(), copy_session_state=copy,
                delete_session_state=delete,
            )

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        await agent.initialize(1)
        parent = await agent.new_session(str(tmp_path))
        checkpointer.fail_copy = True

        with pytest.raises(RuntimeError, match="simulated copy failure"):
            await agent.fork_session(parent.session_id, str(tmp_path))

        # A failed fork must not leave an orphan child in catalog or registry.
        assert [item.session_id for item in catalog.list_page()[0]] == [parent.session_id]
        assert len(agent.sessions) == 1
        assert checkpointer.deleted and checkpointer.deleted[0] != parent.session_id
        await agent.shutdown()

    asyncio.run(run())


def test_delete_one_session_does_not_affect_others(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
        )
        await agent.initialize(1)
        first = await agent.new_session(str(tmp_path))
        second = await agent.new_session(str(tmp_path))

        await agent.delete_session(first.session_id)

        # The surviving session remains listed and resumable.
        listed = await agent.list_sessions(cwd=str(tmp_path))
        assert [item.session_id for item in listed.sessions] == [second.session_id]
        resumed = await agent.resume_session(second.session_id, str(tmp_path))
        assert resumed is not None
        await agent.shutdown()

    asyncio.run(run())
