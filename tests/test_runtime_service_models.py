"""Unit tests for the runtime model-endpoint CRUD and probe service surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.runtime.service import (
    AclAuthorizer,
    AclGrant,
    InvalidRequestError,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    bind_access,
)
from synapse.runtime.service.access import MODELS_READ, MODELS_WRITE
from synapse.runtime.service.model_management import (
    DeleteModelCommand,
    ListModelsQuery,
    ModelListResult,
    SaveModelCommand,
    TestModelCommand,
    TestModelResult,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import decode_params, dispatch

REF = SessionRef(project_id="test-proj", thread_id="thread-1")
PRINCIPAL = Principal("user-test")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_env(tmp_path: Path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    synapse_dir = ws / ".synapse"
    synapse_dir.mkdir()
    models_file = synapse_dir / "models.json"
    initial_data = {
        "default": "gpt-4o",
        "models": {
            "gpt-4o": {
                "model": "openai:gpt-4o",
                "api_key": "sk-secret-1234567890",
                "context_window": 128000,
                "reasoning_effort": "high",
            },
            "claude-3-7": {
                "model": "anthropic:claude-3-7-sonnet",
                "api_key": "sk-ant-secret",
                "context_window": 200000,
            },
        },
    }
    models_file.write_text(json.dumps(initial_data, indent=2), encoding="utf-8")
    settings = SimpleNamespace(
        workspace=ws,
        models_config_path=models_file,
        model="gpt-4o",
    )
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: object(),
        project_id="test-proj",
    )
    service = LocalAgentRuntimeService(
        manager_provider=lambda pid: manager if pid == "test-proj" else None
    )
    return SimpleNamespace(manager=manager, service=service, models_file=models_file)


def test_list_models_redacts_credentials(fake_env):
    result = run(fake_env.service.list_models(ListModelsQuery(session=REF)))
    assert isinstance(result, ModelListResult)
    assert result.default == "gpt-4o"
    assert len(result.models) == 2
    summaries = {m.alias: m for m in result.models}
    assert "gpt-4o" in summaries
    gpt = summaries["gpt-4o"]
    assert gpt.is_default is True
    assert gpt.has_api_key is True
    assert gpt.context_window == 128000
    # No credentials exposed on summary
    assert not hasattr(gpt, "api_key")


def test_save_and_set_default_model(fake_env):
    # Add new model with make_default=True
    save_cmd = SaveModelCommand(
        session=REF,
        alias="deepseek-chat",
        profile={
            "model": "openai:deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-ds-key-123",
        },
        make_default=True,
    )
    res = run(fake_env.service.save_model(save_cmd))
    assert res.default == "deepseek-chat"
    aliases = [m.alias for m in res.models]
    assert "deepseek-chat" in aliases

    # Update existing model
    update_cmd = SaveModelCommand(
        session=REF,
        alias="gpt-4o",
        profile={
            "model": "openai:gpt-4o-2024-11-20",
            "context_window": 130000,
        },
    )
    res2 = run(fake_env.service.save_model(update_cmd))
    summaries = {m.alias: m for m in res2.models}
    assert summaries["gpt-4o"].model == "openai:gpt-4o-2024-11-20"


def test_delete_model_last_remaining_refused(fake_env):
    # Delete claude-3-7
    del_cmd = DeleteModelCommand(session=REF, alias="claude-3-7")
    res = run(fake_env.service.delete_model(del_cmd))
    assert len(res.models) == 1
    assert res.models[0].alias == "gpt-4o"

    # Deleting last profile raises InvalidRequestError
    with pytest.raises(InvalidRequestError, match="cannot delete the last"):
        run(fake_env.service.delete_model(DeleteModelCommand(session=REF, alias="gpt-4o")))


def test_test_model_connectivity_handles_error_without_raising(fake_env):
    # Testing an endpoint with invalid network/credentials returns TestModelResult with ok=False
    test_cmd = TestModelCommand(session=REF, alias="gpt-4o")
    result = run(fake_env.service.test_model(test_cmd))
    assert isinstance(result, TestModelResult)
    assert result.ok is False
    assert result.latency_ms >= 0
    assert result.error is not None
    # Secret key must not leak in error
    assert "1234567890" not in result.error


def test_acl_authorizes_models_capabilities(fake_env):
    # Authorizer granting only MODELS_READ
    grant_read = AclGrant(
        subject=PRINCIPAL.subject, project_id=REF.project_id, capabilities=(MODELS_READ,)
    )
    authorizer_read = AclAuthorizer([grant_read])
    secured_read = bind_access(fake_env.service, PRINCIPAL, authorizer_read)

    # Read succeeds
    res = run(secured_read.list_models(ListModelsQuery(session=REF)))
    assert len(res.models) == 2

    # Write without MODELS_WRITE is denied
    with pytest.raises(PermissionDeniedError):
        run(secured_read.delete_model(DeleteModelCommand(session=REF, alias="claude-3-7")))

    # Granting MODELS_WRITE allows write
    grant_both = AclGrant(
        subject=PRINCIPAL.subject,
        project_id=REF.project_id,
        capabilities=(MODELS_READ, MODELS_WRITE),
    )
    secured_both = bind_access(fake_env.service, PRINCIPAL, AclAuthorizer([grant_both]))
    res_after = run(secured_both.delete_model(DeleteModelCommand(session=REF, alias="claude-3-7")))
    assert len(res_after.models) == 1


def test_protocol_decode_and_dispatch(fake_env):
    # Test decode_params
    decoded = decode_params(
        "runtime.models.save",
        {
            "session": {"project_id": "test-proj", "thread_id": "thread-1"},
            "alias": "new-alias",
            "profile": {"model": "openai:gpt-4o-mini"},
            "make_default": False,
        },
    )
    assert isinstance(decoded, SaveModelCommand)
    assert decoded.alias == "new-alias"

    # Test dispatch
    dispatched_res = run(
        dispatch(
            fake_env.service,
            "runtime.models.list",
            {"session": {"project_id": "test-proj", "thread_id": "thread-1"}},
        )
    )
    assert isinstance(dispatched_res, ModelListResult)
