from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import HumanMessage

from synapse.describe_image import (
    VisionModelClient,
    VisionModelConfig,
    VisionModelError,
    rewrite_messages,
)
from synapse.models_registry import (
    load_models_json_blob,
    model_supports_image_input,
)

PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()


def test_vision_model_config_is_independent_and_env_configurable(monkeypatch):
    monkeypatch.setenv("VISION_API_KEY", "vision-secret")
    config = VisionModelConfig.from_mapping(
        {
            "model": "qwen-vl-max",
            "base_url": "https://vision.example/v1",
            "api_key_env": "VISION_API_KEY",
            "timeout_secs": 12,
            "max_input_bytes": 1234,
            "fallback_model": "qwen-vl-plus",
            "think": True,
        }
    )
    assert config is not None
    assert config.model == "qwen-vl-max"
    assert config.base_url == "https://vision.example/v1"
    assert config.api_key == "vision-secret"
    assert config.timeout_secs == 12
    assert config.max_input_bytes == 1234
    assert config.fallback_model == "qwen-vl-plus"
    assert config.think is True


def test_models_json_carries_vision_config_and_image_override():
    registry = load_models_json_blob(
        '{"default":"text","vision_model":{"model":"glm-4v","base_url":"http://vision/v1"},'
        '"models":{"text":{"model":"openai:custom","image_input":false},'
        '"native":{"model":"openai:custom","capabilities":{"image_input":true}}}}'
    )
    assert registry is not None
    assert registry.vision_model == {"model": "glm-4v", "base_url": "http://vision/v1"}
    assert registry.profiles["text"].image_input is False
    assert registry.profiles["native"].image_input is True


def test_builtin_vision_models_are_inferred_but_unknown_gateways_are_not():
    assert model_supports_image_input("openai:gpt-4.1") is True
    assert (
        model_supports_image_input(
            "openai:gpt-4.1", base_url="https://gateway.example/v1"
        )
        is False
    )
    assert model_supports_image_input("openai:gpt-4-turbo") is True
    assert model_supports_image_input("anthropic:claude-sonnet-4-6") is True
    assert model_supports_image_input("openai:custom-vision-gateway") is False
    assert model_supports_image_input("openai:custom-gateway-model", True) is True


def test_string_image_capability_values_are_parsed_as_booleans():
    registry = load_models_json_blob(
        '{"default":"text","models":{"text":{"model":"openai:custom",'
        '"image_input":"false"}}}'
    )
    assert registry is not None
    assert registry.profiles["text"].image_input is False


def test_rewrite_messages_uses_configured_vision_client():
    message = HumanMessage(
        content=[
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ]
    )
    client = VisionModelClient(VisionModelConfig(model="glm-4v"))
    with patch.object(client, "describe_data_url", new=AsyncMock(return_value="A red square")):
        rewritten = asyncio.run(rewrite_messages([message], client))
    assert rewritten[0].content == [
        {"type": "text", "text": "What is shown?"},
        {"type": "text", "text": "[image]\nA red square\n[/image]"},
    ]


def test_rewrite_without_vision_never_keeps_raw_image_data():
    message = HumanMessage(
        content=[
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ]
    )
    rewritten = asyncio.run(rewrite_messages([message], None))
    assert "png-bytes" not in str(rewritten[0].content)
    assert rewritten[0].content == [
        {"type": "text", "text": "[image unavailable: automatic description failed]"}
    ]


def test_rewrite_without_vision_strips_remote_image_urls():
    message = HumanMessage(
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
        ]
    )
    rewritten = asyncio.run(rewrite_messages([message], None))
    assert rewritten[0].content == [
        {"type": "text", "text": "[image unavailable: automatic description failed]"}
    ]


def test_rewrite_handles_direct_base64_image_blocks():
    message = HumanMessage(
        content=[
            {
                "type": "image",
                "base64": base64.b64encode(b"png-bytes").decode(),
                "mime_type": "image/png",
            }
        ]
    )
    client = VisionModelClient(VisionModelConfig(model="selected-vl"))
    with patch.object(client, "describe_data_url", new=AsyncMock(return_value="A diagram")):
        rewritten = asyncio.run(rewrite_messages([message], client))
    assert rewritten[0].content == [
        {"type": "text", "text": "[image]\nA diagram\n[/image]"}
    ]


def test_remote_url_description_requires_explicit_opt_in():
    disabled = VisionModelClient(VisionModelConfig(model="selected-vl"))
    try:
        asyncio.run(disabled.describe_url("https://example.invalid/a.png"))
    except VisionModelError as exc:
        assert str(exc) == "remote image URLs are disabled"
    else:
        raise AssertionError("remote URL should require explicit opt-in")


def test_vision_client_posts_to_selected_model():
    config = VisionModelConfig(
        model="selected-vl",
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        think=True,
    )
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": "A screenshot"}}]},
    )
    client = VisionModelClient(config)
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
        result = asyncio.run(client.describe_data_url(PNG_DATA_URL))
    assert result == "A screenshot"
    body = post.call_args.kwargs["json"]
    assert body["model"] == "selected-vl"
    assert body["thinking"] == {"type": "enabled"}
    assert body["messages"][0]["content"][0]["image_url"]["url"] == PNG_DATA_URL


def test_middleware_skips_configured_vision_for_native_model():
    from synapse.vision_middleware import build_describe_image_middleware

    middleware = build_describe_image_middleware(
        image_input=True,
        config=VisionModelConfig(model="selected-vl"),
    )
    request = SimpleNamespace(messages=[HumanMessage(content=[{"type": "image_url"}])])
    request.override = lambda **kwargs: request
    called = {"n": 0}

    def handler(value):
        called["n"] += 1
        assert value is request
        return "ok"

    assert middleware.wrap_model_call(request, handler) == "ok"
    assert called["n"] == 1


def test_sync_middleware_rewrite_works_inside_running_event_loop():
    from synapse.vision_middleware import build_describe_image_middleware

    middleware = build_describe_image_middleware(image_input=False, config=None)
    message = HumanMessage(
        content=[{"type": "image_url", "image_url": {"url": PNG_DATA_URL}}]
    )
    request = SimpleNamespace(messages=[message])
    request.override = lambda **kwargs: SimpleNamespace(messages=kwargs["messages"])
    result = []

    async def run():
        result.append(middleware.wrap_model_call(request, lambda value: value.messages))

    asyncio.run(run())
    assert result[0][0].content[0]["text"].startswith("[image unavailable")
