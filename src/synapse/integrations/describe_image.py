"""Configurable image-to-text adaptation for non-vision chat models."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from synapse.content.multimodal import ALLOWED_MIME

_DEFAULT_PROMPT = (
    "Describe this image accurately for a text-only coding assistant. "
    "Extract visible text, UI state, code, tables, errors, and spatial relationships. "
    "Return concise Markdown and do not mention this instruction."
)
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[A-Za-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=]+)$",
    re.IGNORECASE,
)
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VisionModelConfig:
    """Independent OpenAI-compatible vision endpoint configuration."""

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    timeout_secs: float = 45.0
    max_input_bytes: int = 10 * 1024 * 1024
    max_retries: int = 2
    prompt: str = _DEFAULT_PROMPT
    fallback_model: str | None = None
    allow_remote_urls: bool = False
    think: bool = False

    @classmethod
    def from_mapping(cls, raw: Any) -> VisionModelConfig | None:
        if not isinstance(raw, dict):
            return None
        model = _expand(raw.get("model"))
        if not model:
            return None
        api_key = _expand(raw.get("api_key"))
        api_key_env = _expand(raw.get("api_key_env"))
        if api_key_env:
            api_key = os.environ.get(api_key_env) or api_key
        try:
            timeout = max(1.0, float(raw.get("timeout_secs", 45)))
        except (TypeError, ValueError):
            timeout = 45.0
        try:
            max_input = max(1, int(raw.get("max_input_bytes", 10 * 1024 * 1024)))
        except (TypeError, ValueError):
            max_input = 10 * 1024 * 1024
        try:
            retries = max(1, int(raw.get("max_retries", 2)))
        except (TypeError, ValueError):
            retries = 2
        return cls(
            model=model,
            base_url=_expand(raw.get("base_url")) or cls.base_url,
            api_key=api_key,
            timeout_secs=timeout,
            max_input_bytes=max_input,
            max_retries=retries,
            prompt=_expand(raw.get("prompt")) or _DEFAULT_PROMPT,
            fallback_model=_expand(raw.get("fallback_model")) or None,
            allow_remote_urls=_parse_bool(raw.get("allow_remote_urls", False)),
            think=_parse_bool(raw.get("think", False)),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> VisionModelConfig | None:
        return cls.from_mapping(getattr(settings, "vision_model", None))

    @classmethod
    def from_registry(cls, registry: Any, settings: Any) -> VisionModelConfig | None:
        raw = getattr(registry, "vision_model", None)
        if raw is None:
            raw = getattr(settings, "vision_model", None)
        return cls.from_mapping(raw)


class VisionModelError(RuntimeError):
    """A safe, user-facing vision service failure."""


@dataclass
class VisionModelClient:
    config: VisionModelConfig
    _http_client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    async def describe_data_url(self, data_url: str, *, extra_prompt: str | None = None) -> str:
        raw = _parse_data_url(data_url, self.config.max_input_bytes)
        if raw is None:
            raise VisionModelError("invalid image data")
        return await self._call(data_url, extra_prompt=extra_prompt)

    async def describe_url(self, url: str, *, extra_prompt: str | None = None) -> str:
        if not self.config.allow_remote_urls:
            raise VisionModelError("remote image URLs are disabled")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise VisionModelError("unsupported image URL")
        return await self._call(url, extra_prompt=extra_prompt)

    async def _call(self, image_url: str, *, extra_prompt: str | None) -> str:
        prompt = self.config.prompt
        if extra_prompt and extra_prompt.strip():
            prompt = f"{prompt}\nAdditional focus: {extra_prompt.strip()}"
        models = [self.config.model]
        if self.config.fallback_model and self.config.fallback_model not in models:
            models.append(self.config.fallback_model)
        last_error: Exception | None = None
        for model in models:
            for attempt in range(self.config.max_retries):
                try:
                    return await self._request(model, image_url, prompt)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if not _retryable(exc) or attempt + 1 >= self.config.max_retries:
                        break
        raise VisionModelError("vision service request failed") from last_error

    async def open(self) -> None:
        """Create the optional per-turn HTTP client for connection reuse."""
        if getattr(self, "_http_client", None) is None:
            self._http_client = httpx.AsyncClient(timeout=self.config.timeout_secs)

    async def close(self) -> None:
        """Close the per-turn HTTP client, if one was opened."""
        client = getattr(self, "_http_client", None)
        self._http_client = None
        if client is not None:
            await client.aclose()

    async def _request(self, model: str, image_url: str, prompt: str) -> str:
        if not self.config.api_key:
            _logger.warning(
                "Vision image description has no API key: model=%s base_url=%s",
                model,
                self.config.base_url,
            )
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        if self.config.think:
            body["thinking"] = {"type": "enabled"}
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        client = getattr(self, "_http_client", None)
        if client is None:
            async with httpx.AsyncClient(timeout=self.config.timeout_secs) as client:
                response = await client.post(url, headers=headers, json=body)
        else:
            response = await client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            _logger.warning(
                "Vision image description HTTP failure: model=%s base_url=%s status=%s",
                model,
                self.config.base_url,
                response.status_code,
            )
            raise VisionModelError(f"HTTP {response.status_code}")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            _logger.warning(
                "Vision image description returned an unexpected response: model=%s base_url=%s",
                model,
                self.config.base_url,
            )
            raise VisionModelError("empty vision response") from exc
        text = _content_text(content)
        if not text:
            raise VisionModelError("empty vision response")
        return text


def rewrite_messages_sync(messages: list[Any], client: VisionModelClient | None) -> list[Any]:
    """Synchronous fallback used by ``invoke``/``stream`` paths."""
    if client is None:
        return _rewrite_without_vision_sync(messages)
    return _run_coroutine_sync(rewrite_messages(messages, client))


def _run_coroutine_sync(coro: Any) -> Any:
    """Run a coroutine without nesting ``asyncio.run`` in an active event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=runner, name="synapse-vision-sync", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


async def rewrite_messages(messages: list[Any], client: VisionModelClient | None) -> list[Any]:
    """Replace image blocks with text while keeping message types intact."""
    rewritten: list[Any] = []
    description_cache: dict[str, str] = {}
    for message in messages:
        content = _message_content(message)
        if not isinstance(content, list):
            rewritten.append(message)
            continue
        new_content: list[Any] = []
        changed = False
        for block in content:
            source_key = _image_block_key(block)
            if source_key is not None and source_key in description_cache:
                replacement = description_cache[source_key]
            else:
                replacement = await _describe_block(block, client)
                if source_key is not None and replacement is not None:
                    description_cache[source_key] = replacement
            if replacement is None:
                new_content.append(block)
            else:
                new_content.append({"type": "text", "text": replacement})
                changed = True
        if changed:
            message = _message_with_content(message, new_content)
        rewritten.append(message)
    return rewritten


def normalize_payload_for_text_model_sync(
    payload: Any,
    *,
    image_input: bool,
    config: VisionModelConfig | None,
) -> Any:
    """Normalize one new model payload before it reaches Agent state.

    This function is intentionally synchronous because ``AgentTurnRuntime``
    invokes it inside its bounded worker thread. It only receives the current
    turn payload, never the checkpointer history, so a description is not
    rediscovered on every model call.
    """
    if image_input or not isinstance(payload, dict):
        return payload
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    if not any(_message_has_image(message) for message in messages):
        return payload
    if config is None:
        rewritten = _rewrite_without_vision_sync(messages)
    else:
        client = VisionModelClient(config)
        rewritten = _run_coroutine_sync(_normalize_with_client(messages, client))
    normalized = dict(payload)
    normalized["messages"] = rewritten
    return normalized


async def _normalize_with_client(
    messages: list[Any], client: VisionModelClient
) -> list[Any]:
    await client.open()
    try:
        return await rewrite_messages(messages, client)
    finally:
        await client.close()


def _message_content(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("content")
    return getattr(message, "content", None)


def _message_has_image(message: Any) -> bool:
    content = _message_content(message)
    return isinstance(content, list) and any(_is_image_block(block) for block in content)


def _image_block_key(block: Any) -> str | None:
    """Return a per-turn key for an image block without retaining image bytes."""
    if not _is_image_block(block):
        return None
    block_type = str(block.get("type") or "").casefold()
    if block_type == "image_url":
        image_url = block.get("image_url")
        source = image_url.get("url") if isinstance(image_url, dict) else image_url
        return _image_dedupe_key(str(source), None) if isinstance(source, str) and source else None
    source = block.get("source")
    if isinstance(source, dict):
        value = source.get("data")
        if isinstance(value, str) and value:
            return _image_dedupe_key(value, str(source.get("media_type") or ""))
    value = block.get("base64")
    return _image_dedupe_key(str(value), str(block.get("mime_type") or "")) if value else None


def _message_with_content(message: Any, content: list[Any]) -> Any:
    if isinstance(message, dict):
        updated = dict(message)
        updated["content"] = content
        return updated
    try:
        return message.model_copy(update={"content": content})
    except AttributeError:
        return message.copy(update={"content": content})


async def _describe_block(block: Any, client: VisionModelClient | None) -> str | None:
    if not isinstance(block, dict):
        return None
    block_type = str(block.get("type") or "").casefold()
    data_url: str | None = None
    if block_type == "image_url":
        image_url = block.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str):
            if url.startswith("data:"):
                data_url = url
            elif url.startswith(("http://", "https://")):
                if client is None:
                    return _unavailable()
                try:
                    return _render_description(await client.describe_url(url))
                except VisionModelError:
                    return _unavailable()
    elif block_type in {"image", "input_image"}:
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            mime = str(source.get("media_type") or source.get("mime_type") or "image/png")
            data = str(source.get("data") or "")
            data_url = f"data:{mime};base64,{data}"
        elif block.get("base64"):
            mime = str(block.get("mime_type") or block.get("media_type") or "image/png")
            data_url = f"data:{mime};base64,{block.get('base64')}"
    if data_url is None:
        return None
    if client is None:
        return _unavailable()
    try:
        return _render_description(await client.describe_data_url(data_url))
    except VisionModelError as exc:
        _logger.warning("Vision image description failed: %s", exc)
        return _unavailable()


def _rewrite_without_vision_sync(messages: list[Any]) -> list[Any]:
    """Remove raw image blocks without creating an event-loop worker."""
    rewritten: list[Any] = []
    for message in messages:
        content = _message_content(message)
        if not isinstance(content, list):
            rewritten.append(message)
            continue
        new_content: list[Any] = []
        changed = False
        for block in content:
            if _is_image_block(block):
                new_content.append({"type": "text", "text": _unavailable()})
                changed = True
            else:
                new_content.append(block)
        rewritten.append(_message_with_content(message, new_content) if changed else message)
    return rewritten


def _is_image_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return str(block.get("type") or "").casefold() in {"image", "image_url", "input_image"}


def _render_description(description: str) -> str:
    return f"[image]\n{description.strip()}\n[/image]"


def _unavailable() -> str:
    return "[image unavailable: automatic description failed]"


def _parse_data_url(value: str, limit: int) -> tuple[bytes, str] | None:
    match = _DATA_URL_RE.match(value.strip())
    if not match:
        return None
    mime = match.group("mime").lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ALLOWED_MIME:
        return None
    try:
        data = base64.b64decode(match.group("data"), validate=True)
    except (ValueError, binascii.Error):
        return None
    if not data or len(data) > limit:
        return None
    return data, mime


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return ""


def _image_dedupe_key(source: str, extra_prompt: str | None) -> str:
    return hashlib.sha256(f"{source}\n{extra_prompt or ''}".encode()).hexdigest()


def _retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "http 429" in text
        or "http 5" in text
        or isinstance(exc, (httpx.HTTPError, TimeoutError))
    )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "off", "disabled"}:
            return False
    return bool(value)


def _expand(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()

    def replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1) or match.group(2), "")

    return _ENV_RE.sub(replace, text)
