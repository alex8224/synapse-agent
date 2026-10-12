"""Tests for post-turn action suggestions in the web console host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, CookieJar

from synapse.models.profile import ModelProfile
from synapse.models.registry import ModelRegistry
from synapse.settings.schema import Settings
from synapse.web_console.config import WebConsoleConfig
from synapse.web_console.host import ProjectView, WebConsoleHost
from synapse.web_console.security import CONSOLE_HEADER_NAME, CONSOLE_HEADER_VALUE
from synapse.web_console.turn_suggestions import (
    TurnSuggestionsError,
    _default_model_invoker,
    generate_turn_suggestions,
    parse_model_suggestions_json,
    resolve_suggestion_model,
)

TOKEN = "daemon-secret-token-0ab3f9"
PROJECT_ID = "proj-ok"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_host(
    tmp_path: Path,
    *,
    suggestion_invoker: Any | None = None,
) -> WebConsoleHost:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    token_file = state / "token"
    token_file.write_bytes((TOKEN + "\n").encode("ascii"))
    static = tmp_path / "static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text("<div id='root'></div>", encoding="utf-8")

    config = WebConsoleConfig(
        workspace=workspace,
        host="127.0.0.1",
        port=0,
        static_dir=static,
        state_dir=state,
        token_file=token_file,
        runtime_host="127.0.0.1",
        runtime_port=65530,
        pairing_required=False,
    )
    project = ProjectView(
        project_id=PROJECT_ID,
        workspace_path=str(workspace.resolve()),
        name="synapse-workspace",
        git_branch="main",
    )
    return WebConsoleHost(
        config,
        project,
        token=TOKEN,
        suggestion_invoker=suggestion_invoker,
    )


def _csrf_headers(port: int) -> dict[str, str]:
    return {
        "Origin": f"http://127.0.0.1:{port}",
        "Content-Type": "application/json",
        CONSOLE_HEADER_NAME: CONSOLE_HEADER_VALUE,
    }


def test_parse_model_suggestions_json_handles_fences_and_caps() -> None:
    raw = """
    Here are the actions:
    ```json
    [
      {"label": "运行单元测试", "prompt": "请运行 pytest 验证修改", "mode": "submit"},
      {"label": "运行单元测试", "prompt": "重复项会被过滤", "mode": "submit"},
      {"label": "补充配置参数", "prompt": "将超时时间设置为 ", "mode": "fill"},
      {"label": "第三个动作", "prompt": "执行步骤三", "mode": "submit"},
      {"label": "超出上限动作", "prompt": "不应出现", "mode": "submit"}
    ]
    ```
    """
    items = parse_model_suggestions_json(raw)
    assert len(items) == 3
    assert items[0] == {
        "label": "运行单元测试",
        "prompt": "请运行 pytest 验证修改",
        "mode": "submit",
    }
    assert items[1]["mode"] == "fill"
    assert items[2]["label"] == "第三个动作"
    assert parse_model_suggestions_json("not json") == []


def test_generate_turn_suggestions_validates_and_degrades_cleanly(tmp_path: Path) -> None:
    async def run() -> None:
        empty = await generate_turn_suggestions(
            tmp_path,
            payload={"user_prompt": "hi", "conclusion": "   "},
        )
        assert empty == {"suggestions": []}

        with pytest.raises(TurnSuggestionsError):
            await generate_turn_suggestions(
                tmp_path,
                payload={"user_prompt": 123, "conclusion": "done"},
            )

        async def failing_invoker(*_args: Any) -> str:
            raise RuntimeError("network down")

        degraded = await generate_turn_suggestions(
            tmp_path,
            payload={"user_prompt": "fix bug", "conclusion": "fixed"},
            model_invoker=failing_invoker,
        )
        assert degraded == {"suggestions": []}

    _run(run())


def test_turn_suggestions_http_route_guards_and_response(tmp_path: Path) -> None:
    captured: list[tuple[Path, str | None, str, str]] = []

    async def fake_invoker(
        workspace: Path,
        model_alias: str | None,
        user_prompt: str,
        conclusion: str,
    ) -> str:
        captured.append((workspace, model_alias, user_prompt, conclusion))
        return '[{"label": "采用方案 A", "prompt": "按方案 A 实现", "mode": "submit"}]'

    async def run() -> None:
        host = _make_host(tmp_path, suggestion_invoker=fake_invoker)
        await host.start()
        try:
            assert host.bound is not None
            _, port = host.bound
            base = f"http://127.0.0.1:{port}"
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as session:
                # 1. GET is rejected with 405
                async with session.get(f"{base}/api/turn-suggestions") as resp:
                    assert resp.status == 405

                # 2. POST without CSRF header is rejected with 403
                async with session.post(
                    f"{base}/api/turn-suggestions",
                    json={"user_prompt": "a", "conclusion": "b"},
                ) as resp:
                    assert resp.status == 403

                # 3. POST with CSRF header but without session cookie is rejected with 401
                async with session.post(
                    f"{base}/api/turn-suggestions",
                    headers=_csrf_headers(port),
                    json={"user_prompt": "a", "conclusion": "b"},
                ) as resp:
                    assert resp.status == 401

                # 4. Acquire session cookie via GET /api/session (--no-pairing)
                async with session.get(
                    f"{base}/api/session",
                    headers={"Origin": base},
                ) as resp:
                    assert resp.status == 200

                # 5. Authenticated POST succeeds and returns suggestions
                async with session.post(
                    f"{base}/api/turn-suggestions",
                    headers=_csrf_headers(port),
                    json={
                        "user_prompt": "重构日志模块",
                        "conclusion": "提供方案 A 和方案 B，请确认。",
                        "project_id": PROJECT_ID,
                        "model": "gpt-4.1",
                    },
                ) as resp:
                    assert resp.status == 200
                    assert resp.headers.get("Cache-Control") == "no-store"
                    body = await resp.json()
                    assert body == {
                        "suggestions": [
                            {
                                "label": "采用方案 A",
                                "prompt": "按方案 A 实现",
                                "mode": "submit",
                            }
                        ]
                    }
                assert len(captured) == 1
                assert captured[0][1] == "gpt-4.1"
                assert captured[0][2] == "重构日志模块"
        finally:
            await host.close()

    _run(run())


def test_resolve_suggestion_model_priority_and_fallback() -> None:
    registry = ModelRegistry(
        profiles={
            "main-pro": ModelProfile(name="main-pro", model="openai:gpt-5"),
            "session-sonnet": ModelProfile(
                name="session-sonnet", model="anthropic:claude-sonnet-4"
            ),
            "fast-mini": ModelProfile(name="fast-mini", model="openai:gpt-4.1-mini"),
            "reg-mini": ModelProfile(name="reg-mini", model="openai:gpt-4o-mini"),
        },
        default="main-pro",
        suggestion_model="reg-mini",
    )

    # 1. settings.suggestion_model has highest priority over registry & session model
    settings_with_suggestion = Settings(
        _env_file=None,
        active_model="main-pro",
        suggestion_model="fast-mini",
    )
    assert (
        resolve_suggestion_model(
            settings_with_suggestion, registry, "session-sonnet"
        )
        == "fast-mini"
    )

    # 2. registry.suggestion_model wins when settings.suggestion_model is unset
    settings_no_suggestion = Settings(_env_file=None, active_model="main-pro")
    assert (
        resolve_suggestion_model(settings_no_suggestion, registry, "session-sonnet")
        == "reg-mini"
    )

    # 3. When no suggestion_model is configured anywhere, falls back to current session model
    registry_no_suggestion = ModelRegistry(
        profiles=registry.profiles,
        default="main-pro",
        suggestion_model=None,
    )
    assert (
        resolve_suggestion_model(
            settings_no_suggestion, registry_no_suggestion, "session-sonnet"
        )
        == "session-sonnet"
    )

    # 4. When session model is also absent, falls back to settings.active_model
    assert (
        resolve_suggestion_model(settings_no_suggestion, registry_no_suggestion, None)
        == "main-pro"
    )

    # 5. Stale/unknown suggestion_model gracefully falls back to current session model
    settings_stale = Settings(
        _env_file=None,
        active_model="main-pro",
        suggestion_model="deleted-profile",
    )
    assert (
        resolve_suggestion_model(
            settings_stale, registry_no_suggestion, "session-sonnet"
        )
        == "session-sonnet"
    )


def test_default_model_invoker_uses_suggestion_model_and_disables_thinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_workspaces: list[Path] = []
    captured_build: list[tuple[str | None, bool | None]] = []

    class _FakeResponse:
        content = '[{"label": "运行测试", "prompt": "运行 pytest", "mode": "submit"}]'

    class _FakeChatModel:
        async def ainvoke(self, _messages: Any) -> _FakeResponse:
            return _FakeResponse()

    registry = ModelRegistry(
        profiles={
            "main-pro": ModelProfile(name="main-pro", model="openai:gpt-5"),
            "fast-mini": ModelProfile(name="fast-mini", model="openai:gpt-4.1-mini"),
        },
        default="main-pro",
    )

    def fake_load_settings(**overrides: Any) -> Settings:
        if "workspace" in overrides:
            captured_workspaces.append(Path(overrides["workspace"]))
        return Settings(
            _env_file=None,
            active_model="main-pro",
            suggestion_model="fast-mini",
        )

    def fake_build_chat_model(
        name: str | None = None,
        *,
        enable_thinking: bool | None = None,
        **_kwargs: Any,
    ) -> _FakeChatModel:
        captured_build.append((name, enable_thinking))
        return _FakeChatModel()

    monkeypatch.setattr(
        "synapse.settings.schema.load_settings",
        fake_load_settings,
    )
    monkeypatch.setattr(
        "synapse.models.registry.registry_from_settings",
        lambda _settings: registry,
    )
    monkeypatch.setattr(registry, "build_chat_model", fake_build_chat_model)

    raw = _run(
        _default_model_invoker(
            tmp_path,
            "main-pro",
            "帮我检查配置",
            "配置已更新完成",
        )
    )
    assert "运行测试" in raw
    assert captured_workspaces == [tmp_path]
    assert captured_build == [("fast-mini", False)]
