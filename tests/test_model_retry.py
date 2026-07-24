"""Model retries supplement provider SDK retries for transient SSE / 5xx failures."""

from __future__ import annotations

import asyncio

import pytest

from synapse.middleware import (
    NotifyingModelRetryMiddleware,
    build_model_retry_middleware,
    clear_retry_notifier,
    set_retry_notifier,
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
        # -- no status code: text-based transient markers → retry -------------
        (_ProviderError("empty model output"), True),
        (_ProviderError("stream failed", body={"error": {"type": "overloaded_error"}}), True),
        (_ProviderError("temporarily unavailable"), True),
        (_ProviderError("rate_limit_error"), True),
        # -- 4xx: never retried -------------------------------------------------
        (_ProviderError("rate limit", status_code=429), False),
        (_ProviderError("invalid API key", status_code=401), False),
        # -- 5xx without a recognised transient body marker → no retry --------
        (_ProviderError("internal error", status_code=500), False),
        (_ProviderError("context length exceeded", status_code=500), False),
        # -- 5xx *with* a recognised transient marker → retry -----------------
        (
            _ProviderError(
                "service unavailable",
                status_code=503,
                body={"error": {"message": "auth_unavailable: no auth available"}},
            ),
            True,
        ),
        (
            _ProviderError(
                "Bad Gateway",
                status_code=502,
                body={"error": {"message": "temporarily unavailable"}},
            ),
            True,
        ),
        (
            _ProviderError(
                "Gateway Timeout",
                status_code=504,
                body={"error": {"message": "upstream request timeout"}},
            ),
            True,
        ),
        # -- 503 with "service unavailable" message → retry -------------------
        (_ProviderError("service unavailable", status_code=503), True),
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


def test_model_retry_middleware_retries_5xx_auth_unavailable(monkeypatch) -> None:  # noqa: ANN001
    """503 + auth_unavailable markers should now be retried."""
    monkeypatch.setattr("synapse.middleware.time.sleep", lambda _: None)
    middleware = build_model_retry_middleware()
    attempts = 0
    err = _ProviderError(
        "service unavailable",
        status_code=503,
        body={"error": {"message": "auth_unavailable: no auth available"}},
    )

    def handler(_request):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise err
        return "ok"

    assert middleware.wrap_model_call(object(), handler) == "ok"
    assert attempts == 3


def test_model_retry_middleware_does_not_retry_4xx() -> None:
    """4xx errors (e.g. 401) should never be retried."""
    middleware = build_model_retry_middleware()
    attempts = 0
    error = _ProviderError("invalid API key", status_code=401)

    def handler(_request):  # noqa: ANN001
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(_ProviderError) as raised:
        middleware.wrap_model_call(object(), handler)

    assert raised.value is error
    assert attempts == 1


def test_model_retry_middleware_notifier_called(monkeypatch) -> None:  # noqa: ANN001
    """The module-level retry notifier receives retry events."""
    monkeypatch.setattr("synapse.middleware.time.sleep", lambda _: None)
    middleware = build_model_retry_middleware()

    calls: list[tuple[int, float, str]] = []

    set_retry_notifier(lambda a, d, r: calls.append((a, d, r)))

    error = _ProviderError(
        "service unavailable",
        status_code=503,
        body={"error": {"message": "auth_unavailable: no auth available"}},
    )

    def handler(_request):  # noqa: ANN001
        raise error

    try:
        with pytest.raises(_ProviderError):
            middleware.wrap_model_call(object(), handler)
    finally:
        clear_retry_notifier()

    # 1 initial + 5 retries = 5 retry notifications
    assert len(calls) == 5
    for attempt, delay, reason in calls:
        assert 1 <= attempt <= 5
        assert delay >= 0
        assert "auth_unavailable" in reason or "503" in reason


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

    assert isinstance(middleware, NotifyingModelRetryMiddleware)
    assert middleware.max_retries == 5
    assert middleware.on_failure == "error"
    assert middleware.initial_delay == 1.0
    assert middleware.max_delay == 8.0