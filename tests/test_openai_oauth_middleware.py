from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage

from synapse.integrations.openai_oauth_middleware import (
    _codex_prompt_cache_key,
    _prepare_codex_request,
)

INCLUDE = ["reasoning.encrypted_content"]
STREAM_OPTIONS = {"reasoning_summary_delivery": "sequential_cutoff"}


class _Request(SimpleNamespace):
    def override(self, **updates):
        values = dict(vars(self))
        values.update(updates)
        return _Request(**values)


def test_prepare_codex_request_moves_system_to_instructions() -> None:
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

    # System instructions are hoisted to the top-level `instructions` field
    # (codex-rs contract); history system messages stay as developer messages.
    assert prepared.system_message is None
    assert prepared.messages[0].additional_kwargs["__openai_role__"] == "developer"
    assert prepared.messages[1] is user
    assert prepared.model_settings["instructions"] == "primary instructions"
    assert prepared.model_settings["store"] is False
    assert prepared.model_settings["include"] == INCLUDE
    assert prepared.model_settings["stream_options"] == STREAM_OPTIONS
    assert prepared.model_settings["prompt_cache_key"] == _codex_prompt_cache_key(
        "primary instructions"
    )
    assert prepared.model_settings["extra_body"] == {"service_tier": "priority"}
    assert system.additional_kwargs == {}
    assert history_system.additional_kwargs == {}


def test_prepare_codex_request_preserves_developer_and_forces_store_false() -> None:
    developer = SystemMessage(
        content="instructions", additional_kwargs={"__openai_role__": "developer"}
    )
    request = _Request(system_message=developer, messages=[], model_settings={"store": True})

    prepared = _prepare_codex_request(request)
    assert prepared.system_message is None
    assert prepared.model_settings["instructions"] == "instructions"
    assert prepared.model_settings["store"] is False
    assert prepared.model_settings["include"] == INCLUDE
    assert prepared.model_settings["stream_options"] == STREAM_OPTIONS
    assert "service_tier" not in prepared.model_settings


def test_prepare_codex_request_fast_mode_injects_service_tier() -> None:
    request = _Request(
        system_message=None,
        messages=[],
        model_settings={"store": False, "extra_body": {"service_tier": "priority"}},
    )

    prepared = _prepare_codex_request(request, fast_mode=True)

    # Top-level service_tier is authoritative; extra_body must not duplicate it.
    assert prepared.model_settings == {
        "store": False,
        "service_tier": "priority",
        "include": INCLUDE,
        "stream_options": STREAM_OPTIONS,
    }
    assert "prompt_cache_key" not in prepared.model_settings
    assert "instructions" not in prepared.model_settings


def test_prepare_codex_request_fast_mode_off_is_noop() -> None:
    request = _Request(
        system_message=None,
        messages=[],
        model_settings={"store": False},
    )

    prepared = _prepare_codex_request(request, fast_mode=False)
    assert prepared.model_settings["store"] is False
    assert "service_tier" not in prepared.model_settings
    assert prepared.model_settings["include"] == INCLUDE
    assert prepared.model_settings["stream_options"] == STREAM_OPTIONS
    assert "prompt_cache_key" not in prepared.model_settings


def test_system_message_with_block_content_extracts_instructions() -> None:
    system = SystemMessage(
        content=[
            {"type": "text", "text": "primary instructions"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
            {"type": "text", "text": "second paragraph"},
        ]
    )
    request = _Request(system_message=system, messages=[], model_settings={})

    prepared = _prepare_codex_request(request)

    assert prepared.system_message is None
    assert prepared.model_settings["instructions"] == (
        "primary instructions\nsecond paragraph"
    )
    assert prepared.model_settings["prompt_cache_key"] == _codex_prompt_cache_key(
        "primary instructions\nsecond paragraph"
    )


def test_external_prompt_cache_key_overrides_digest() -> None:
    system = SystemMessage(content="primary instructions")
    request = _Request(system_message=system, messages=[], model_settings={})

    prepared = _prepare_codex_request(request, prompt_cache_key="thread-42")
    assert prepared.model_settings["prompt_cache_key"] == "thread-42"


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
