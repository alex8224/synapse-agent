"""Codex usage / reset-credit surface: DTO bounds, service boundary, adapter.

Every test here runs against fakes: no test touches the real Codex endpoints,
the user's OAuth credential file, or the network.  The service tests inject a
fake provider through the documented port, and the adapter tests inject a fake
client through the adapter's own ``_new_client`` seam.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from types import SimpleNamespace

import pytest

from synapse.integrations import openai_usage as usage_module
from synapse.integrations.openai_oauth import OpenAIOAuthTokens
from synapse.integrations.openai_usage import (
    CodexUsageClient,
    CodexUsageSnapshot,
    ConsumeResetResult,
    ResetCreditDetail,
    ResetCredits,
    UsageWindow,
)
from synapse.runtime.daemon.codex_usage import MAX_CODEX_COMMAND_RECORDS, CodexUsageAdapter
from synapse.runtime.service import config_source
from synapse.runtime.service.codex_usage import (
    MAX_CODEX_CREDITS,
    CodexConsumeResult,
    CodexResetCredit,
    CodexResetCreditsView,
    CodexUsageConflictError,
    CodexUsageView,
    CodexUsageWindow,
    ConsumeCodexResetCommand,
    GetCodexResetCreditsQuery,
    GetCodexUsageQuery,
)
from synapse.runtime.service.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RuntimeServiceError,
)
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.service.runtime_config import GetRuntimeConfigQuery, RuntimeConfigView
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef

REF = SessionRef(project_id="p1", thread_id="thread-a")
OTHER = SessionRef(project_id="p1", thread_id="thread-b")
MODEL = "codex-oauth"


def run(coro):
    return asyncio.run(coro)


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "active_model": MODEL,
        "model": "openai:gpt-5.2-codex",
        "workspace": None,
        "enable_thinking": True,
        "reasoning_effort": "high",
        "enable_mcp": False,
        "mcp_config_path": None,
        "mcp_servers_json": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manager(settings: object | None = None, *, project_id: str = REF.project_id) -> RuntimeManager:
    return RuntimeManager(
        settings=settings if settings is not None else _settings(),
        agent_factory=lambda thread_id, shared: object(),
        project_id=project_id,
    )


def _open(
    manager: RuntimeManager, settings: object | None = None, *, ref: SessionRef = REF
) -> None:
    manager._sessions[ref.thread_id] = SimpleNamespace(
        settings=settings if settings is not None else _settings()
    )


class _Profile:
    def __init__(self, auth: str | None) -> None:
        self.auth = auth
        self.context_window = None


class _Registry:
    def __init__(self, auth: str | None, names: tuple[str, ...] = (MODEL,)) -> None:
        self.default = names[0]
        self.thinking_levels = ["high"]
        self._auth = auth
        self._names = list(names)

    def list_names(self) -> list[str]:
        return list(self._names)

    def get(self, name: str | None = None) -> _Profile:
        return _Profile(self._auth)

    def allowed_thinking_levels(self, name: str | None = None) -> list[str]:
        return ["high"]


def _profile_auth(monkeypatch, auth: str | None) -> None:
    monkeypatch.setattr(config_source, "registry_from_settings", lambda _s: _Registry(auth))


def _usage_view(session: SessionRef = REF, model: str = MODEL) -> CodexUsageView:
    return CodexUsageView(
        session=session,
        model=model,
        primary=CodexUsageWindow(used_percent=18.0, window_minutes=300, reset_at=1_700_000_600.0),
        secondary=None,
        captured_at=1_700_000_000.0,
        available_reset_count=2,
    )


class FakeProvider:
    """Recording stand-in for the injected Codex usage provider."""

    def __init__(self, *, usage=None, credits=None, consume=None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.usage = usage
        self.credits = credits
        self.consume = consume

    async def get_usage(self, session, model, force):
        self.calls.append(("usage", session, model, force))
        if isinstance(self.usage, BaseException):
            raise self.usage
        return self.usage if self.usage is not None else _usage_view(session, model)

    async def get_reset_credits(self, session, model, force):
        self.calls.append(("credits", session, model, force))
        if isinstance(self.credits, BaseException):
            raise self.credits
        if self.credits is not None:
            return self.credits
        return CodexResetCreditsView(session=session, model=model, available_count=0, credits=())

    async def consume_reset(self, command, model):
        self.calls.append(("consume", command.session, model, command.command_id))
        if isinstance(self.consume, BaseException):
            raise self.consume
        if self.consume is not None:
            return self.consume
        return CodexConsumeResult(
            session=command.session,
            model=model,
            command_id=command.command_id,
            outcome="reset",
        )


def _service(manager: RuntimeManager, provider=None) -> LocalAgentRuntimeService:
    return LocalAgentRuntimeService(lambda project_id: manager, codex_usage_provider=provider)


def _command(**overrides: object) -> ConsumeCodexResetCommand:
    values: dict[str, object] = {
        "session": REF,
        "expected_model": MODEL,
        "credit_id": "credit-1",
        "command_id": "cmd-1",
        "confirmed": True,
    }
    values.update(overrides)
    return ConsumeCodexResetCommand(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DTO contract
# ---------------------------------------------------------------------------


def test_codex_dtos_are_frozen_slotted_and_carry_every_declared_key() -> None:
    window = CodexUsageWindow(used_percent=None, window_minutes=None, reset_at=None)
    credit = CodexResetCredit("c1", "rate_limit", "available", None, None, None, None)
    dtos = (
        GetCodexUsageQuery(REF),
        GetCodexResetCreditsQuery(REF),
        _command(),
        window,
        credit,
        _usage_view(),
        CodexResetCreditsView(REF, MODEL, 0, ()),
        CodexConsumeResult(REF, MODEL, "cmd-1", "reset"),
    )
    for dto in dtos:
        assert type(dto).__dataclass_params__.frozen is True
        assert hasattr(type(dto), "__slots__")
    # Nullable fields are always present, never omitted.
    assert [field.name for field in dataclasses.fields(CodexUsageWindow)] == [
        "used_percent",
        "window_minutes",
        "reset_at",
    ]
    assert [field.name for field in dataclasses.fields(CodexResetCredit)] == [
        "id",
        "reset_type",
        "status",
        "granted_at",
        "expires_at",
        "title",
        "description",
    ]
    assert [field.name for field in dataclasses.fields(CodexUsageView)] == [
        "session",
        "model",
        "primary",
        "secondary",
        "captured_at",
        "available_reset_count",
    ]
    assert [field.name for field in dataclasses.fields(CodexConsumeResult)] == [
        "session",
        "model",
        "command_id",
        "outcome",
    ]
    # The OAuth grant's expiry has no field to travel in.
    assert not hasattr(_usage_view(), "expires_at")


@pytest.mark.parametrize(
    "field,value",
    [
        ("used_percent", 100.5),
        ("used_percent", -1.0),
        ("used_percent", "10"),
        ("used_percent", True),
        ("window_minutes", 0),
        ("window_minutes", 527_041),
        ("window_minutes", 300.0),
        ("reset_at", -1.0),
        ("reset_at", 4_102_444_801.0),
        ("reset_at", float("nan")),
    ],
)
def test_usage_window_rejects_values_outside_the_wire_bounds(field: str, value: object) -> None:
    payload: dict[str, object] = {"used_percent": None, "window_minutes": None, "reset_at": None}
    payload[field] = value
    with pytest.raises(ValueError):
        CodexUsageWindow(**payload)  # type: ignore[arg-type]


def test_usage_view_and_credit_bounds_are_rejections_not_clamps() -> None:
    assert CodexUsageView(REF, MODEL, None, None, 0.0, None).available_reset_count is None
    with pytest.raises(ValueError):
        CodexUsageView(REF, MODEL, None, None, 0.0, 1001)
    with pytest.raises(ValueError):
        CodexUsageView(REF, MODEL, None, None, 0.0, True)
    with pytest.raises(ValueError):
        CodexUsageView(REF, MODEL, "not-a-window", None, 0.0, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CodexUsageView(OTHER, "x" * 257, None, None, 0.0, None)
    with pytest.raises(ValueError):
        CodexResetCreditsView(REF, MODEL, 0, tuple(_credit() for _ in range(MAX_CODEX_CREDITS + 1)))
    with pytest.raises(ValueError):
        CodexResetCredit("c" * 129, "rate_limit", "available", None, None, None, None)
    with pytest.raises(ValueError):
        CodexResetCredit("c1", "r" * 65, "available", None, None, None, None)
    with pytest.raises(ValueError):
        CodexResetCredit("c1", "rate_limit", "available", None, None, "t" * 257, None)
    with pytest.raises(ValueError):
        CodexResetCredit("c1", "rate_limit", "available", None, None, None, "d" * 1025)
    with pytest.raises(ValueError):
        CodexResetCredit("", "rate_limit", "available", None, None, None, None)
    with pytest.raises(ValueError):
        CodexConsumeResult(REF, MODEL, "cmd-1", "redeemed")  # type: ignore[arg-type]


def test_queries_and_command_validate_session_force_and_confirmation() -> None:
    for bad in (None, "p:t", object(), SessionRef("", "t"), SessionRef("p", "")):
        with pytest.raises(ValueError):
            GetCodexUsageQuery(bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            GetCodexResetCreditsQuery(bad)  # type: ignore[arg-type]
    assert GetCodexUsageQuery(REF).force is False
    with pytest.raises(ValueError):
        GetCodexUsageQuery(REF, force=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GetCodexResetCreditsQuery(REF, force="yes")  # type: ignore[arg-type]
    # Only the literal True is a confirmation.
    for value in (False, 1, "true", None):
        with pytest.raises(ValueError):
            _command(confirmed=value)
    assert _command(confirmed=True).confirmed is True
    with pytest.raises(ValueError):
        _command(credit_id="")
    with pytest.raises(ValueError):
        _command(command_id="k" * 129)
    with pytest.raises(ValueError):
        _command(expected_model="m" * 257)


# ---------------------------------------------------------------------------
# Service boundary: provider presence, open session, profile auth
# ---------------------------------------------------------------------------


def test_missing_provider_reports_the_surface_as_unavailable(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    manager = _manager()
    _open(manager)
    service = _service(manager, None)
    for call in (
        service.get_codex_usage(GetCodexUsageQuery(REF)),
        service.get_codex_reset_credits(GetCodexResetCreditsQuery(REF)),
        service.consume_codex_reset(_command()),
    ):
        with pytest.raises(InvalidRequestError, match="codex usage is unavailable"):
            run(call)


def test_non_oauth_profile_is_refused_by_every_method(monkeypatch) -> None:
    _profile_auth(monkeypatch, "api_key")
    provider = FakeProvider()
    manager = _manager()
    _open(manager)
    service = _service(manager, provider)
    for call in (
        service.get_codex_usage(GetCodexUsageQuery(REF)),
        service.get_codex_reset_credits(GetCodexResetCreditsQuery(REF)),
        service.consume_codex_reset(_command()),
    ):
        with pytest.raises(InvalidRequestError, match="not enabled"):
            run(call)
    assert provider.calls == []


def test_an_unopened_session_is_not_found(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    provider = FakeProvider()
    service = _service(_manager(), provider)
    for call in (
        service.get_codex_usage(GetCodexUsageQuery(REF)),
        service.get_codex_reset_credits(GetCodexResetCreditsQuery(REF)),
        service.consume_codex_reset(_command()),
    ):
        with pytest.raises(NotFoundError):
            run(call)
    assert provider.calls == []


def test_reads_use_the_session_bound_model_and_forward_force(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    provider = FakeProvider()
    manager = _manager()
    _open(manager, _settings(active_model="bound-alias"))
    service = _service(manager, provider)
    view = run(service.get_codex_usage(GetCodexUsageQuery(REF, force=True)))
    credits = run(service.get_codex_reset_credits(GetCodexResetCreditsQuery(REF)))
    assert view.model == "bound-alias"
    assert credits.model == "bound-alias"
    assert provider.calls == [
        ("usage", REF, "bound-alias", True),
        ("credits", REF, "bound-alias", False),
    ]


def test_wrong_query_types_are_rejected(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    service = _service(_manager(), FakeProvider())
    with pytest.raises(InvalidRequestError):
        run(service.get_codex_usage(GetCodexResetCreditsQuery(REF)))  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        run(service.get_codex_reset_credits(GetCodexUsageQuery(REF)))  # type: ignore[arg-type]
    with pytest.raises(InvalidRequestError):
        run(service.consume_codex_reset(GetCodexUsageQuery(REF)))  # type: ignore[arg-type]


def test_consume_refuses_a_model_mismatch_and_a_missing_confirmation(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    provider = FakeProvider()
    manager = _manager()
    _open(manager)
    service = _service(manager, provider)
    with pytest.raises(ConflictError):
        run(service.consume_codex_reset(_command(expected_model="openai:other")))
    # A hand-built command bypasses the DTO's literal-True check; the service
    # still refuses it before any provider call.
    forged = object.__new__(ConsumeCodexResetCommand)
    for name, value in (
        ("session", REF),
        ("expected_model", MODEL),
        ("credit_id", "credit-1"),
        ("command_id", "cmd-1"),
        ("confirmed", 1),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(InvalidRequestError, match="explicit confirmation"):
        run(service.consume_codex_reset(forged))
    assert provider.calls == []


def test_consume_returns_the_provider_result(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    provider = FakeProvider()
    manager = _manager()
    _open(manager)
    service = _service(manager, provider)
    result = run(service.consume_codex_reset(_command()))
    assert result.outcome == "reset"
    assert provider.calls == [("consume", REF, MODEL, "cmd-1")]


def test_provider_failures_are_redacted_and_conflicts_are_mapped(monkeypatch) -> None:
    _profile_auth(monkeypatch, "openai_oauth")
    manager = _manager()
    _open(manager)
    secret = "Bearer SENTINEL-TOKEN-abc123"
    failing = _service(manager, FakeProvider(usage=RuntimeError(f"503 {secret}")))
    with pytest.raises(RuntimeServiceError) as excinfo:
        run(failing.get_codex_usage(GetCodexUsageQuery(REF)))
    assert secret not in str(excinfo.value)
    assert str(excinfo.value) == "codex usage request failed"

    conflicting = _service(manager, FakeProvider(consume=CodexUsageConflictError(secret)))
    with pytest.raises(ConflictError) as conflict:
        run(conflicting.consume_codex_reset(_command()))
    assert secret not in str(conflict.value)

    # A provider that answers for another session (or with another type) is
    # treated as a failed request, never projected.
    wrong_session = _service(manager, FakeProvider(usage=_usage_view(OTHER)))
    with pytest.raises(RuntimeServiceError):
        run(wrong_session.get_codex_usage(GetCodexUsageQuery(REF)))
    wrong_type = _service(manager, FakeProvider(credits="not-a-view"))
    with pytest.raises(RuntimeServiceError):
        run(wrong_type.get_codex_reset_credits(GetCodexResetCreditsQuery(REF)))


def test_codex_failure_logs_category_not_exception_text(monkeypatch) -> None:
    from synapse.runtime.service import local as local_module

    messages: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(
        local_module._CODEX_LOGGER, "error", lambda fmt, *args: messages.append((fmt, args))
    )
    _profile_auth(monkeypatch, "openai_oauth")
    manager = _manager()
    _open(manager)
    secret = "Bearer SENTINEL-TOKEN-abc123"
    service = _service(manager, FakeProvider(usage=RuntimeError(secret)))
    with pytest.raises(RuntimeServiceError):
        run(service.get_codex_usage(GetCodexUsageQuery(REF)))
    assert messages == [
        ("codex %s failed: exceptions=%s status=%s", ("CodexUsageView", "RuntimeError", None))
    ]


def test_daemon_codex_error_log_is_bounded_and_redacted(tmp_path) -> None:
    import logging

    import httpx

    from synapse.runtime.daemon.entry import _configure_error_log
    from synapse.runtime.service.local import _log_codex_failure

    handler = _configure_error_log(tmp_path)
    try:
        response = httpx.Response(503, request=httpx.Request("GET", "https://example.test/secret"))
        failure = httpx.HTTPStatusError("Bearer SENTINEL-TOKEN", request=response.request,
                                         response=response)
        _log_codex_failure(failure, "CodexResetCreditsView")
        handler.flush()
        line = (tmp_path / "errors.log").read_text(encoding="utf-8")
        assert "CodexResetCreditsView" in line
        assert "HTTPStatusError" in line
        assert "503" in line
        assert "SENTINEL-TOKEN" not in line
        assert "example.test" not in line
    finally:
        logging.getLogger("synapse.runtime.codex_usage").removeHandler(handler)
        handler.close()


# ---------------------------------------------------------------------------
# runtime.config.get gate
# ---------------------------------------------------------------------------


def _spy_view(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    def spy(settings, *, session, **kwargs):
        captured.append(kwargs)
        return RuntimeConfigView(
            current_model=MODEL,
            available_models=(MODEL,),
            thinking_level=None,
            thinking_levels=("high",),
            mcp_servers=(),
            mcp_enabled=False,
        )

    monkeypatch.setattr(config_source, "build_config_view", spy)
    return captured


def test_config_gate_needs_provider_open_session_and_oauth_profile(monkeypatch) -> None:
    captured = _spy_view(monkeypatch)
    _profile_auth(monkeypatch, "openai_oauth")
    manager = _manager()
    _open(manager)
    run(_service(manager, FakeProvider()).get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured[-1]["codex_usage_enabled"] is True

    # No provider injected: the entry stays hidden.
    run(_service(manager, None).get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured[-1]["codex_usage_enabled"] is False

    # Provider present but the session is not open yet.
    run(_service(_manager(), FakeProvider()).get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured[-1]["codex_usage_enabled"] is False

    # Provider present, session open, but the selected profile is not OAuth.
    _profile_auth(monkeypatch, "api_key")
    run(_service(manager, FakeProvider()).get_runtime_config(GetRuntimeConfigQuery(REF)))
    assert captured[-1]["codex_usage_enabled"] is False


def test_config_gate_defaults_to_disabled_and_rejects_non_bool() -> None:
    view = RuntimeConfigView(
        current_model=MODEL,
        available_models=(MODEL,),
        thinking_level=None,
        thinking_levels=("high",),
        mcp_servers=(),
        mcp_enabled=False,
    )
    assert view.codex_usage_enabled is False
    with pytest.raises(ValueError):
        RuntimeConfigView(
            current_model=MODEL,
            available_models=(MODEL,),
            thinking_level=None,
            thinking_levels=("high",),
            mcp_servers=(),
            mcp_enabled=False,
            codex_usage_enabled=1,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Lifecycle guard
# ---------------------------------------------------------------------------


def test_binding_guard_keeps_the_coordinator_until_the_worker_settles() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        manager = _manager()
        _open(manager)
        lock = manager._lifecycle_locks.setdefault(REF.thread_id, asyncio.Lock())
        entered = threading.Event()
        release = threading.Event()

        async def body() -> None:
            async with manager.session_binding_guard(REF) as binding:
                assert binding.session is not None

                async def worker() -> str:
                    def block() -> str:
                        entered.set()
                        release.wait(5)
                        return "done"

                    return await asyncio.to_thread(block)

                await binding.run_worker(worker())

        task = asyncio.ensure_future(body())
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        # The caller is cancelled, but the step it dispatched is still running:
        # the coordinator must not be handed to an open/close/rebind yet.
        await asyncio.sleep(0.05)
        held_while_running = lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return held_while_running, lock.locked(), manager._sessions.get(REF.thread_id) is not None

    held, locked_after, session_kept = run(scenario())
    assert held is True
    assert locked_after is False
    assert session_kept is True


def test_binding_guard_reports_no_session_for_an_unopened_thread() -> None:
    async def scenario() -> object:
        manager = _manager()
        async with manager.session_binding_guard(REF) as binding:
            return binding.session

    assert run(scenario()) is None


# ---------------------------------------------------------------------------
# Adapter: real-client plumbing behind a fake client
# ---------------------------------------------------------------------------


def _credit(
    credit_id: str = "credit-1",
    *,
    status: str = "available",
    expires_at: float | None = None,
) -> ResetCreditDetail:
    return ResetCreditDetail(
        id=credit_id,
        reset_type="rate_limit",
        status=status,
        granted_at=1_700_000_000.0,
        expires_at=expires_at,
        title="Reset window",
        description=None,
    )


class FakeClient:
    def __init__(self, *, snapshot=None, credits=None, outcomes=None) -> None:
        self.snapshot = snapshot if snapshot is not None else CodexUsageSnapshot()
        self.credits = credits if credits is not None else ResetCredits(available_count=0)
        self.outcomes = list(outcomes or ["reset"])
        self.posts: list[dict] = []
        self.credit_fetches = 0
        self.invalidated = 0
        self.account = "fake-account"

    def account_key(self) -> str:
        return self.account

    def fetch(self, *, force: bool = False) -> CodexUsageSnapshot:
        return self.snapshot

    def fetch_reset_credits(self, *, force: bool = False) -> ResetCredits:
        self.credit_fetches += 1
        return self.credits

    def consume_reset_credit(
        self, *, credit_id=None, idempotency_key=None, expected_account_key=None
    ) -> ConsumeResetResult:
        assert expected_account_key == self.account
        self.posts.append({"credit_id": credit_id, "key": idempotency_key})
        outcome = self.outcomes.pop(0) if self.outcomes else "reset"
        if isinstance(outcome, Exception):
            raise outcome
        return ConsumeResetResult(outcome=outcome, idempotency_key=idempotency_key or "")

    def invalidate(self) -> None:
        self.invalidated += 1


def _adapter(client: FakeClient) -> CodexUsageAdapter:
    adapter = CodexUsageAdapter()
    adapter._new_client = lambda: client  # type: ignore[method-assign]
    return adapter


def test_adapter_projects_usage_without_the_grant_expiry() -> None:
    client = FakeClient(
        snapshot=CodexUsageSnapshot(
            primary=UsageWindow(used_percent=18.0, window_minutes=300, reset_at=1_700_000_600.0),
            secondary=UsageWindow(used_percent=200.0, window_minutes=999_999, reset_at=None),
            captured_at=1_700_000_000.0,
            expires_at=1_700_010_000.0,
            reset_credits=ResetCredits(available_count=3),
        )
    )
    view = run(_adapter(client).get_usage(REF, MODEL, False))
    assert view.captured_at == 1_700_000_000.0
    assert view.primary == CodexUsageWindow(18.0, 300, 1_700_000_600.0)
    # Out-of-range upstream numbers are clamped into the wire bounds.
    assert view.secondary is not None
    assert view.secondary.used_percent == 100.0
    assert view.secondary.window_minutes == 527_040
    assert view.available_reset_count == 3
    assert 1_700_010_000.0 not in dataclasses.astuple(view)


def test_adapter_independent_reads_can_overlap() -> None:
    started = threading.Event()
    release = threading.Event()
    client = FakeClient()

    def slow_usage(*, force: bool = False) -> CodexUsageSnapshot:
        started.set()
        assert release.wait(3)
        return client.snapshot

    client.fetch = slow_usage  # type: ignore[method-assign]
    adapter = _adapter(client)

    async def read_both() -> None:
        usage = asyncio.create_task(adapter.get_usage(REF, MODEL, True))
        assert await asyncio.to_thread(started.wait, 3)
        try:
            credits = await asyncio.wait_for(adapter.get_reset_credits(REF, MODEL, True), 2)
            assert credits.available_count == 0
        finally:
            release.set()
        await usage

    run(read_both())


def test_adapter_drops_a_credit_row_the_console_would_reject() -> None:
    client = FakeClient(
        credits=ResetCredits(
            available_count=2,
            credits=[_credit("ok"), _credit("x" * 129), _credit("bad", status="y" * 65)],
        )
    )
    view = run(_adapter(client).get_reset_credits(REF, MODEL, False))
    assert [row.id for row in view.credits] == ["ok"]
    assert view.available_count == 2


def test_adapter_consume_is_idempotent_and_invalidates_both_caches() -> None:
    client = FakeClient(credits=ResetCredits(available_count=1, credits=[_credit()]))
    adapter = _adapter(client)
    first = run(adapter.consume_reset(_command(), MODEL))
    assert first.outcome == "reset"
    assert client.posts == [{"credit_id": "credit-1", "key": "cmd-1"}]
    assert client.invalidated == 1

    replay = run(adapter.consume_reset(_command(), MODEL))
    assert replay == first
    assert len(client.posts) == 1  # a replay never posts again

    with pytest.raises(CodexUsageConflictError):
        run(adapter.consume_reset(_command(credit_id="credit-2"), MODEL))
    assert len(client.posts) == 1


def test_adapter_never_retries_an_unresolved_credit_with_a_new_key() -> None:
    client = FakeClient(
        credits=ResetCredits(available_count=1, credits=[_credit()]),
        outcomes=["something-new"],
    )
    adapter = _adapter(client)
    first = run(adapter.consume_reset(_command(), MODEL))
    assert first.outcome == "unknown"
    second = run(adapter.consume_reset(_command(command_id="cmd-2"), MODEL))
    assert second.outcome == "unknown"
    assert len(client.posts) == 1  # no new key for a credit that never resolved


def test_adapter_reports_no_credit_without_posting() -> None:
    for credits in (
        ResetCredits(available_count=0, credits=[]),
        ResetCredits(available_count=1, credits=[_credit(status="redeemed")]),
        ResetCredits(available_count=1, credits=[_credit(status="unknown")]),
        ResetCredits(available_count=1, credits=[_credit(status="pending")]),
        ResetCredits(available_count=1, credits=[_credit(expires_at=float("nan"))]),
        ResetCredits(
            available_count=1, credits=[_credit(expires_at=time.time() - 60.0)]
        ),
    ):
        client = FakeClient(credits=credits)
        result = run(_adapter(client).consume_reset(_command(), MODEL))
        assert result.outcome == "noCredit"
        assert client.posts == []


def test_adapter_maps_upstream_verbs_and_keeps_the_ledger_bounded() -> None:
    client = FakeClient(
        credits=ResetCredits(available_count=1, credits=[_credit()]),
        outcomes=["already_redeemed"],
    )
    adapter = _adapter(client)
    assert run(adapter.consume_reset(_command(), MODEL)).outcome == "alreadyRedeemed"

    client.outcomes = ["nothing_to_reset"]
    client.credits = ResetCredits(available_count=1, credits=[_credit("credit-2")])
    second = run(
        adapter.consume_reset(_command(command_id="cmd-3", credit_id="credit-2"), MODEL)
    )
    assert second.outcome == "nothingToReset"

    client.outcomes = ["reset"]
    client.credits = ResetCredits(available_count=1, credits=[_credit()])

    async def bulk() -> None:
        for index in range(MAX_CODEX_COMMAND_RECORDS + 2):
            await adapter.consume_reset(
                _command(command_id=f"bulk-{index}", credit_id=f"bulk-credit-{index}"), MODEL
            )

    run(bulk())
    assert len(adapter._commands) <= MAX_CODEX_COMMAND_RECORDS


# ---------------------------------------------------------------------------
# Client cache invalidation / account change (no network)
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Store:
    def __init__(self, account_id: str = "acct-1") -> None:
        self.tokens = OpenAIOAuthTokens(
            access_token="token-value",
            refresh_token="refresh-value",
            expires_at=time.time() + 3600,
            account_id=account_id,
        )

    def load(self) -> OpenAIOAuthTokens:
        return self.tokens

    def save(self, tokens: OpenAIOAuthTokens) -> None:
        self.tokens = tokens


def test_usage_client_invalidate_drops_both_caches_and_account_change_rebuilds(
    monkeypatch,
) -> None:
    payload = {
        "rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18000}}
    }
    monkeypatch.setattr(usage_module.httpx, "get", lambda url, **kwargs: _Response(payload))
    store = _Store()
    client = CodexUsageClient(store=store, cache_ttl=300.0)
    first = client.fetch()
    client.fetch_reset_credits()
    assert client.get_cached() is first
    assert client.get_cached_details() is not None

    client.invalidate()
    assert client.get_cached() is None
    assert client.get_cached_details() is None

    client.fetch()
    assert client.get_cached() is not None
    # A different OAuth account must not be served the previous snapshot.
    store.tokens = OpenAIOAuthTokens(
        access_token="other-token",
        refresh_token="refresh-value",
        expires_at=time.time() + 3600,
        account_id="acct-2",
    )
    assert client.fetch() is not first
    assert client.get_cached() is not first


def test_usage_client_does_not_mistake_a_token_rotation_for_an_account_switch(
    monkeypatch,
) -> None:
    """A grant without an account id must keep its warm cache (the TUI's own rule).

    The access token rotates on every refresh, so hashing it as an identity would
    report a rotation as an account switch: the warm snapshot would be dropped and
    the in-flight read would fail for no reason.  An unknown identity therefore
    degrades to the pre-existing TTL-only behaviour instead.
    """
    payload = {
        "rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18000}}
    }
    monkeypatch.setattr(usage_module.httpx, "get", lambda url, **kwargs: _Response(payload))
    store = _Store(account_id=None)
    client = CodexUsageClient(store=store, cache_ttl=300.0)
    first = client.fetch()
    store.tokens = OpenAIOAuthTokens(
        access_token="rotated-token",
        refresh_token="refresh-value",
        expires_at=time.time() + 3600,
        account_id=None,
    )
    assert client.account_key() is None
    assert client.fetch() is first, "a rotation is not an account switch"
