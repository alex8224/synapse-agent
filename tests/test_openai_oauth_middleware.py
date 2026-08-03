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


def test_prepare_codex_request_fast_mode_injects_service_tier() -> None:
    request = _Request(
        system_message=None,
        messages=[],
        model_settings={"store": False, "extra_body": {"service_tier": "priority"}},
    )

    prepared = _prepare_codex_request(request, fast_mode=True)

    # Top-level service_tier is authoritative; extra_body must not duplicate it.
    assert prepared.model_settings == {"store": False, "service_tier": "priority"}


def test_prepare_codex_request_fast_mode_off_is_noop() -> None:
    request = _Request(
        system_message=None,
        messages=[],
        model_settings={"store": False},
    )

    prepared = _prepare_codex_request(request, fast_mode=False)
    assert prepared.model_settings == {"store": False}
    assert "service_tier" not in prepared.model_settings


def test_build_middleware_polls_fast_mode_callable() -> None:
    from synapse.integrations.openai_oauth_middleware import (
        build_openai_oauth_compat_middleware,
    )

    state = {"on": True}
    mw = build_openai_oauth_compat_middleware(fast_mode=lambda: state["on"])

    async def handler(request):
        return request.model_settings

    import asyncio

    request = _Request(system_message=None, messages=[], model_settings={})
    settings = asyncio.run(mw.awrap_model_call(request, handler))
    assert settings.get("service_tier") == "priority"

    state["on"] = False
    request2 = _Request(system_message=None, messages=[], model_settings={})
    settings2 = asyncio.run(mw.awrap_model_call(request2, handler))
    assert "service_tier" not in settings2
