"""P4 ACP history replay and session config tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import acp
import pytest
from acp.helpers import update_agent_message_text
from acp.schema import ClientCapabilities

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.lifecycle import ACPSessionCatalog
from synapse.acp.sessions import (
    ACPManagedSession,
    ACPSessionDescriptor,
    ACPSessionRegistry,
    _apply_session_config,
)
from tests.acp_service_fakes import FakeAgentRuntimeService, simple_managed


class _Client:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del session_id, kwargs
        self.updates.append(update)


def test_load_replays_stored_updates_in_order(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        stored = catalog.create(cwd=tmp_path)
        catalog.append_update(stored.session_id, 1, update_agent_message_text("hello"))
        client = _Client()

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory), catalog=catalog
        )
        agent.on_connect(client)  # type: ignore[arg-type]
        await agent.initialize(1, ClientCapabilities())
        response = await agent.load_session(str(tmp_path), stored.session_id)
        assert response is not None
        assert len(client.updates) == 1
        assert client.updates[0].session_update == "agent_message_chunk"
        await agent.shutdown()

    asyncio.run(run())


def test_session_config_maps_thinking_and_approval_without_mutating_source() -> None:
    class Settings:
        enable_thinking = True
        reasoning_effort = "high"
        require_approval = False

        def model_copy(self, *, update: dict[str, Any], deep: bool) -> Settings:
            del deep
            result = Settings()
            result.__dict__.update(self.__dict__)
            result.__dict__.update(update)
            return result

    source = Settings()
    updated = _apply_session_config(
        source,
        {"thinking": "off", "approval": True},
    )
    assert updated is not source
    assert updated.enable_thinking is False
    assert updated.require_approval is True
    assert source.enable_thinking is True
    assert source.require_approval is False


@pytest.mark.parametrize(
    ("level", "enabled", "effort"),
    [
        ("off", False, None),
        ("minimal", True, "minimal"),
        ("low", True, "low"),
        ("medium", True, "medium"),
        ("high", True, "high"),
        ("max", True, "max"),
    ],
)
def test_thinking_levels_map_directly_to_reasoning_effort(
    level: str, enabled: bool, effort: str | None
) -> None:
    class Settings:
        enable_thinking = True
        reasoning_effort = "high"

        def model_copy(self, *, update: dict[str, Any], deep: bool) -> Settings:
            del deep
            result = Settings()
            result.__dict__.update(self.__dict__)
            result.__dict__.update(update)
            return result

    updated = _apply_session_config(Settings(), {"thinking": level})
    assert updated.enable_thinking is enabled
    if effort is None:
        assert "reasoning_effort" not in updated.__dict__
    else:
        assert updated.reasoning_effort == effort


def test_authentication_is_not_advertised_or_accepted_without_provider(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(registry=ACPSessionRegistry(factory), catalog=catalog)
        initialized = await agent.initialize(1)
        assert initialized.auth_methods == []
        with pytest.raises(acp.RequestError, match="Invalid params"):
            await agent.authenticate("missing")
        await agent.shutdown()

    asyncio.run(run())


def test_mode_and_config_are_session_local_and_persisted(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory), catalog=catalog
        )
        await agent.initialize(1)
        session = await agent.new_session(str(tmp_path))
        await agent.set_session_mode(session.session_id, "default")
        result = await agent.set_config_option("approval", session.session_id, True)
        approval = next(item for item in result.config_options if item.id == "approval")
        assert approval.current_value is True
        assert catalog.get(session.session_id).config == {"approval": True}
        await agent.shutdown()

    asyncio.run(run())


def test_extension_meta_kwargs_are_accepted_but_not_persisted(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory), catalog=catalog
        )
        await agent.initialize(1)
        session = await agent.new_session(str(tmp_path))
        # The SDK router spreads `_meta` keys into handler kwargs.
        result = await agent.set_config_option(
            "approval", session.session_id, True, trace_id="abc123", span_id="x"
        )
        approval = next(item for item in result.config_options if item.id == "approval")
        assert approval.current_value is True
        # Extension fields must not pollute persisted standard objects.
        assert catalog.get(session.session_id).config == {"approval": True}
        await agent.shutdown()

    asyncio.run(run())


def test_providers_list_and_set_select_session_model(tmp_path: Path) -> None:
    async def run() -> None:
        def settings_factory(cwd: Path) -> Any:
            class Settings:
                workspace = cwd
                models_config_path = tmp_path / "missing.json"
                models_json = json.dumps(
                    {
                        "default": "a",
                        "models": {
                            "a": {
                                "model": "openai:gpt-4o",
                                "base_url": "https://example.invalid/v1",
                            },
                            "b": {"model": "anthropic:claude-sonnet"},
                        },
                    }
                )
                active_model = None
                model = None

            return Settings()

        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
            settings_factory=settings_factory,
        )
        await agent.initialize(1)
        session = await agent.new_session(str(tmp_path))

        providers = await agent.list_providers()
        assert [item.provider_id for item in providers.providers] == ["a", "b"]
        assert providers.providers[0].current.base_url == "https://example.invalid/v1"

        with pytest.raises(acp.RequestError, match="Invalid params"):
            await agent.set_provider("missing")

        await agent.set_provider("b")
        assert catalog.get(session.session_id).config == {"model": "b"}
        await agent.shutdown()

    asyncio.run(run())


def test_failed_config_rebuild_rolls_back_and_restores_session(tmp_path: Path) -> None:
    async def run() -> None:
        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")
        calls = 0

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated rebuild failure")
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory), catalog=catalog
        )
        await agent.initialize(1)
        session = await agent.new_session(str(tmp_path))
        assert catalog.get(session.session_id).config == {}

        with pytest.raises(RuntimeError, match="simulated rebuild failure"):
            await agent.set_config_option("approval", session.session_id, True)

        # The failed update must not leave the catalog or registry in the new state.
        assert catalog.get(session.session_id).config == {}
        restored = agent.sessions.get(session.session_id)
        assert restored is not None
        assert restored.descriptor.config == {}
        assert calls == 3
        await agent.shutdown()

    asyncio.run(run())


def test_tui_session_bridge_new_list_load_delete(tmp_path: Path) -> None:
    async def run() -> None:
        from acp.helpers import text_block

        from synapse.projects.catalog import ProjectCatalog
        from synapse.sessions.store import SessionStore

        tui_db = tmp_path / "tui-sessions.sqlite"
        project_catalog_db = tmp_path / "project-catalog.sqlite"

        def settings_factory(cwd: Path) -> Any:
            class Settings:
                workspace = cwd
                models_config_path = tmp_path / "missing.json"
                models_json = json.dumps(
                    {"default": "a", "models": {"a": {"model": "openai:gpt-4o"}}}
                )
                active_model = None
                model = None

                def resolved_sessions_path(self) -> Path:
                    return tui_db

                def resolved_catalog_path(self) -> Path:
                    return project_catalog_db

            return Settings()

        with SessionStore(tui_db) as store:
            store.ensure("tui-thread-1", title="TUI session")

        catalog = ACPSessionCatalog(tmp_path / "catalog.sqlite")

        async def factory(descriptor: ACPSessionDescriptor) -> ACPManagedSession:
            return simple_managed(descriptor, FakeAgentRuntimeService())

        agent = SynapseACPAgent(
            registry=ACPSessionRegistry(factory),
            catalog=catalog,
            settings_factory=settings_factory,
        )
        await agent.initialize(1)

        # new_session writes through to the shared TUI store.
        session = await agent.new_session(str(tmp_path))
        with SessionStore(tui_db) as store:
            assert store.get(session.session_id) is not None
            assert session.session_id not in {
                item.thread_id for item in store.list_nonempty()
            }

        # The first Zed prompt replaces the placeholder title, making the
        # session visible in the TUI's list_nonempty() dialog.
        await agent.prompt(session.session_id, [text_block("Zed first message")])
        with SessionStore(tui_db) as store:
            visible = store.list_nonempty()
            bridged = next(item for item in visible if item.thread_id == session.session_id)
            assert bridged.title == "Zed first message"

        # A session created by the older bridge has a catalog title but only a
        # placeholder in the TUI store. Listing repairs it without a new prompt.
        legacy = catalog.create(cwd=tmp_path, title="Legacy Zed session")
        with SessionStore(tui_db) as store:
            store.ensure(legacy.thread_id)
            assert legacy.thread_id not in {
                item.thread_id for item in store.list_nonempty()
            }

        # list_sessions merges pre-existing TUI sessions.
        listed = await agent.list_sessions(cwd=str(tmp_path))
        listed_ids = [item.session_id for item in listed.sessions]
        assert "tui-thread-1" in listed_ids
        assert session.session_id in listed_ids
        with SessionStore(tui_db) as store:
            repaired = store.get(legacy.thread_id)
            assert repaired is not None
            assert repaired.title == "Legacy Zed session"

        # Zed's global history request omits cwd. It must still include TUI
        # sessions through the bounded user-level project projection.
        project_catalog = ProjectCatalog(project_catalog_db)
        try:
            project_catalog.upsert_session(
                tmp_path,
                thread_id="global-tui-thread",
                title="Global TUI session",
            )
        finally:
            project_catalog.close()
        global_listed = await agent.list_sessions()
        assert "global-tui-thread" in [item.session_id for item in global_listed.sessions]

        # load adopts a TUI session into the ACP catalog.
        loaded = await agent.load_session(str(tmp_path), "tui-thread-1")
        assert loaded is not None
        assert catalog.get("tui-thread-1") is not None

        # delete removes from both stores.
        await agent.delete_session("tui-thread-1")
        assert catalog.get("tui-thread-1") is None
        with SessionStore(tui_db) as store:
            assert store.get("tui-thread-1") is None
        await agent.shutdown()

    asyncio.run(run())
