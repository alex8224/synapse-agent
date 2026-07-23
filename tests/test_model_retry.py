"""Model retries supplement provider SDK retries for transient SSE failures."""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware import ModelRetryMiddleware

from synapse.middleware import (
    build_model_retry_middleware,
    should_retry_transient_model_error,
)


class _ProviderError(RuntimeError):
    def __init__(self, message: str, *, body=None, status_code=None):  # noqa: ANN001
        super().__init__(message)
        self.body = body
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_ProviderError("empty model output"), True),
        (_ProviderError("stream failed", body={"error": {"type": "overloaded_error"}}), True),
        (_ProviderError("temporarily unavailable"), True),
        (_ProviderError("rate_limit_error"), True),
        (_ProviderError("rate limit", status_code=429), False),
        (_ProviderError("service unavailable", status_code=503), False),
        (_ProviderError("invalid API key", status_code=401), False),
        (_ProviderError("context length exceeded"), False),
    ],
)
def test_should_retry_only_transient_stream_errors(exc: Exception, expected: bool) -> None:
    assert should_retry_transient_model_error(exc) is expected


def test_model_retry_middleware_retries_then_succeeds(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("langchain.agents.middleware.model_retry.time.sleep", lambda _: None)
    middleware = build_model_retry_middleware()
    attempts = 0

    def handler(_request):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _ProviderError("empty model output")
        return "ok"

    assert middleware.wrap_model_call(object(), handler) == "ok"
    assert attempts == 3


def test_model_retry_middleware_does_not_retry_http_error() -> None:
    middleware = build_model_retry_middleware()
    attempts = 0
    error = _ProviderError("service unavailable", status_code=503)

    def handler(_request):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(_ProviderError) as raised:
        middleware.wrap_model_call(object(), handler)

    assert raised.value is error
    assert attempts == 1


def test_model_retry_middleware_async_retries(monkeypatch) -> None:  # noqa: ANN001
    async def no_sleep(_delay):  # noqa: ANN001
        return None

    monkeypatch.setattr("langchain.agents.middleware.model_retry.asyncio.sleep", no_sleep)
    middleware = build_model_retry_middleware()
    attempts = 0

    async def handler(_request):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _ProviderError("upstream request timeout")
        return "ok"

    result = asyncio.run(middleware.awrap_model_call(object(), handler))

    assert result == "ok"
    assert attempts == 2


def test_model_retry_middleware_configuration() -> None:
    middleware = build_model_retry_middleware()

    assert isinstance(middleware, ModelRetryMiddleware)
    assert middleware.max_retries == 5
    assert middleware.on_failure == "error"
    assert middleware.initial_delay == 1.0
    assert middleware.max_delay == 8.0