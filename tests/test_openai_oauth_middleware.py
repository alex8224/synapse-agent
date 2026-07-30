from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage

from synapse.integrations.openai_oauth_middleware import _prepare_codex_request


class _Request(SimpleNamespace):
    def override(self, **updates):
        values = dict(vars(self))
        values.update(updates)
        return _Request(**values)


def test_prepare_codex_request_rewrites_all_system_roles_to_developer() -> None:
    system = SystemMessage(content="primary instructions")
    history_system = SystemMessage(content="historical instructions")
    user = HumanMessage(content="hello")
    request = _Request(
        system_message=system,
        messages=[history_system, user],
        model_settings={
            "extra_body": {"thinking": {"type": "enabled"}, "service_tier": "priority"},
            "timeout": 30,
        },
    )

    prepared = _prepare_codex_request(request)

    assert prepared.system_message.additional_kwargs["__openai_role__"] == "developer"
    assert prepared.messages[0].additional_kwargs["__openai_role__"] == "developer"
    assert prepared.messages[1] is user
    assert prepared.model_settings == {
        "extra_body": {"service_tier": "priority"},
        "timeout": 30,
        "store": False,
    }
    assert system.additional_kwargs == {}
    assert history_system.additional_kwargs == {}


def test_prepare_codex_request_preserves_developer_and_forces_store_false() -> None:
    developer = SystemMessage(
        content="instructions", additional_kwargs={"__openai_role__": "developer"}
    )
    request = _Request(system_message=developer, messages=[], model_settings={"store": True})

    prepared = _prepare_codex_request(request)
    assert prepared.system_message is developer
    assert prepared.model_settings == {"store": False}
