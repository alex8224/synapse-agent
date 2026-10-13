"""End-to-end PTC integration through the real ``app.build_coding_agent`` assembly.

These tests build the *actual* coding agent (``synapse.app.agent.build_coding_agent``)
with an offline, tool-bindable fake chat model and a temp workspace, then drive it
through a genuine LangGraph run. Nothing here is a hand-written middleware stack:
the PTC middleware, the tool-exclusion middleware and the HITL middleware all come
from the app assembly, and every ``run_code`` execution spawns the real PTC worker
subprocess.

Isolation: the user config dir is redirected to a temp home, memory / MCP /
subagents / goals are disabled, every state path lives under ``tmp_path``, and no
network or private config is touched. Safety profiles are set on ``settings`` and
``build_coding_agent`` rewrites ``require_approval`` / ``readonly`` through
``apply_safety_to_settings`` exactly as production does.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import deepagents
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import Field

from synapse.app.agent import build_coding_agent
from synapse.models.registry import ModelProfile, ModelRegistry
from synapse.settings import Settings

MODEL_SPEC = "openai:gpt-4.1"
SDK_HEADER = "Programmatic tool calling"
DONE = AIMessage(content="done")


@pytest.fixture(autouse=True)
def _isolate_home_and_network(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Keep every test off the real HOME/config and off the network."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr("synapse.content.prompts.user_config_dir", lambda: home)
    monkeypatch.setattr("synapse.settings.config_paths.user_config_dir", lambda: home)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    yield


# --------------------------------------------------------------------------- #
# Fake tool-bindable model
# --------------------------------------------------------------------------- #
def _text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


class _RecordingModel(FakeMessagesListChatModel):
    """A fake model that accepts ``bind_tools`` and records each request."""

    tool_name_batches: list[list[str]] = Field(default_factory=list)
    system_texts: list[str] = Field(default_factory=list)

    def bind_tools(self, tools: Any, **kwargs: Any) -> _RecordingModel:  # noqa: ARG002
        self.tool_name_batches.append([_tool_name(tool) for tool in tools])
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any):
        for message in messages:
            if isinstance(message, SystemMessage):
                self.system_texts.append(_text(message))
                break
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _run_code_ai(code: str, call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "run_code",
                "args": {"code": code, "intent": "orchestrate"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _run_code_ai_with_args(args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "run_code", "args": args, "id": call_id, "type": "tool_call"}],
    )


# --------------------------------------------------------------------------- #
# Assembly helpers
# --------------------------------------------------------------------------- #
def _settings(
    workspace: Path, *, profile: str, mode: str, excluded: list[str] | None = None
) -> Settings:
    return Settings(
        workspace=workspace,
        model=MODEL_SPEC,
        active_model=MODEL_SPEC,
        safety_profile=profile,
        tool_mode=mode,
        checkpoint_backend="memory",
        checkpoint_path=workspace / "checkpoints.sqlite",
        sessions_path=workspace / "sessions.sqlite",
        enable_mcp=False,
        enable_goals=False,
        enable_memory=False,
        enable_long_term_memory=False,
        enable_rag=False,
        enable_subagents=False,
        enable_custom_subagents=False,
        enable_prompt_cache_boundary=False,
        skills_paths=[],
        excluded_tools=excluded or [],
        langsmith_tracing=False,
        turbo=False,
        inherit_env=False,
        _env_file=None,
    )


def _registry() -> ModelRegistry:
    return ModelRegistry(
        profiles={MODEL_SPEC: ModelProfile(name=MODEL_SPEC, model=MODEL_SPEC)},
        default=MODEL_SPEC,
    )


@pytest.fixture
def assemble(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build the real agent, capturing the kwargs it hands to create_deep_agent."""

    def _build(*, profile: str, mode: str, responses: list[AIMessage], excluded=None):
        model = _RecordingModel(responses=responses)
        captured: dict[str, Any] = {}
        real_create = deepagents.create_deep_agent

        def _spy(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return real_create(**kwargs)

        monkeypatch.setattr(deepagents, "create_deep_agent", _spy)
        agent = build_coding_agent(
            _settings(tmp_path, profile=profile, mode=mode, excluded=excluded),
            project_root=tmp_path,
            model=model,
            model_registry=_registry(),
            checkpointer=InMemorySaver(),
        )
        return model, agent, captured

    return _build


def _ptc(captured: dict[str, Any]) -> Any:
    return next(
        (m for m in captured.get("middleware", []) if type(m).__name__ == "_PtcMiddleware"), None
    )


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}


def _invoke(agent: Any, thread_id: str) -> dict[str, Any]:
    return agent.invoke({"messages": [HumanMessage(content="go")]}, config=_config(thread_id))


def _resume(agent: Any, decision: dict[str, Any], thread_id: str) -> dict[str, Any]:
    return agent.invoke(Command(resume={"decisions": [decision]}), config=_config(thread_id))


def _run_code_message(result: dict[str, Any]) -> ToolMessage:
    found = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "run_code"]
    assert found, "no run_code ToolMessage in result"
    return found[-1]


def _value(message: ToolMessage) -> Any:
    return json.loads(message.content)["value"]


# --------------------------------------------------------------------------- #
# Model-facing registration / folding
# --------------------------------------------------------------------------- #
def test_native_mode_has_no_run_code_and_no_sdk(assemble) -> None:
    model, agent, captured = assemble(profile="dev-autopass", mode="native", responses=[DONE])
    _invoke(agent, "native")
    assert _ptc(captured) is None
    assert "run_code" not in model.tool_name_batches[0]
    assert SDK_HEADER not in model.system_texts[0]
    assert isinstance(agent._coding_checkpointer, InMemorySaver)


def test_code_mode_folds_orchestratable_tools_and_injects_sdk(assemble) -> None:
    # The default budget (32 KB) folds the *full* default tool set, so no tool is
    # excluded here -- the write/edit/execute tools fold away too.
    model, agent, captured = assemble(profile="dev-autopass", mode="code", responses=[DONE])
    _invoke(agent, "code")
    names = set(model.tool_name_batches[0])
    # Orchestratable tools are folded away; run_code + the denied session-state
    # tools stay native.
    folded = {
        "read_file",
        "find_files",
        "search_files",
        "patch",
        "write_file",
        "edit_file",
        "execute",
    }
    assert folded & names == set()
    assert {"run_code", "write_todos"} <= names
    text = model.system_texts[0]
    assert SDK_HEADER in text
    # Folding only happens when the full SDK (with typed stubs) fits, so every
    # folded tool's exact schema must be present.
    for name in ("find_files", "read_file", "write_file", "edit_file", "execute"):
        assert f"async def {name}(" in text
    assert _ptc(captured) is not None


def test_both_mode_keeps_native_tools_and_registers_run_code(assemble) -> None:
    model, agent, _ = assemble(profile="dev-autopass", mode="both", responses=[DONE])
    _invoke(agent, "both")
    assert {"run_code", "read_file", "find_files"} <= set(model.tool_name_batches[0])
    assert SDK_HEADER in model.system_texts[0]


class _ModelRequest:
    """A minimal model-call request for driving the assembled middleware."""

    def __init__(self, tools: list[Any], system_message: Any = None, runtime: Any = None) -> None:
        self.tools = list(tools)
        self.system_message = system_message
        self.runtime = runtime

    def override(self, **changes: Any) -> _ModelRequest:
        clone = _ModelRequest(self.tools, self.system_message, self.runtime)
        for key, value in changes.items():
            setattr(clone, key, value)
        return clone


def test_code_mode_falls_back_to_bounded_sdk_for_an_oversized_schema(assemble) -> None:
    # A pathological schema blows the budget, so the real middleware must fall
    # back to the bounded compact SDK and refuse to fold -- keeping every exact
    # native schema visible instead of shipping a truncated one.
    _, _, captured = assemble(profile="dev-autopass", mode="code", responses=[DONE])
    ptc = _ptc(captured)
    assert ptc is not None
    huge = {
        "type": "function",
        "function": {
            "name": "read_huge",
            "description": "oversized",
            "parameters": {
                "type": "object",
                "properties": {f"field_{index:04d}": {"type": "string"} for index in range(2_000)},
                "required": ["field_0000"],
            },
        },
    }
    events: list[dict[str, Any]] = []
    request = _ModelRequest([*captured["tools"], huge])
    request.runtime = SimpleNamespace(stream_writer=events.append)
    out = ptc._prepare_request(request)
    text = _text(out.system_message)
    assert SDK_HEADER in text
    assert "exceeded the SDK size budget" in text
    # Nothing is folded: every native tool keeps its exact schema.
    native_names = {_tool_name(tool) for tool in captured["tools"]}
    assert native_names <= {_tool_name(tool) for tool in out.tools}
    assert any(event.get("event") == "budget_exceeded" for event in events)


def test_extra_run_code_argument_is_rejected_before_running(assemble) -> None:
    # The real middleware intercepts before the tool node validates, so a stray
    # argument must be rejected here (the model-facing schema forbids extras).
    _, agent, _ = assemble(
        profile="dev-autopass",
        mode="both",
        responses=[
            _run_code_ai_with_args({"code": "return 1", "intent": "go", "sneaky": "x"}),
            DONE,
        ],
    )
    message = _run_code_message(_invoke(agent, "extra-arg"))
    assert message.status == "error"
    assert "no other arguments" in message.content


# --------------------------------------------------------------------------- #
# Real worker dispatch through the assembled graph
# --------------------------------------------------------------------------- #
def test_code_mode_real_worker_returns_canonical_find_files_and_read_file(
    assemble, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text("print('hello-ptc')\n", encoding="utf-8")
    code = (
        "files = await tools.find_files(pattern='**/*.py')\n"
        "content = await tools.read_file(file_path='/a.py')\n"
        "return {'files': files, 'content': content}\n"
    )
    _, agent, _ = assemble(
        profile="dev-autopass", mode="code", responses=[_run_code_ai(code), DONE]
    )
    result = asyncio.run(
        agent.ainvoke({"messages": [HumanMessage(content="go")]}, config=_config("worker"))
    )
    message = _run_code_message(result)
    assert message.status == "success", message.content
    value = _value(message)
    assert value["files"]["data"]["matches"] == [{"path": "/a.py", "is_dir": False}]
    assert value["files"]["truncated"] is False
    assert "hello-ptc" in value["content"]["content"]


# --------------------------------------------------------------------------- #
# Exclusions / readonly
# --------------------------------------------------------------------------- #
def test_excluded_tool_is_hidden_and_denied_from_run_code(assemble, tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('x')\n", encoding="utf-8")
    code = "return await tools.read_file(file_path='/a.py')\n"
    model, agent, _ = assemble(
        profile="dev-autopass",
        mode="both",
        responses=[_run_code_ai(code), DONE],
        excluded=["read_file"],
    )
    result = _invoke(agent, "excluded")
    assert "read_file" not in model.tool_name_batches[0]
    message = _run_code_message(result)
    # A literal reference to an excluded tool is refused by the pre-flight check
    # *before* the worker starts, so the run fails fast instead of midway through
    # (the observed session's regression). A runtime-computed name still reaches
    # the worker and is denied there -- see
    # ``test_require_approval_denies_nested_write_and_execute``.
    assert message.status == "error", message.content
    payload = json.loads(message.content)
    assert payload["error"]["kind"] == "unknown_tool"
    assert "read_file" in payload["error"]["message"]
    assert "excluded" in payload["error"]["message"]


class _ToolCallRequest:
    def __init__(self, tool_call: dict[str, Any]) -> None:
        self.tools: list[Any] = []
        self.tool_call = tool_call
        self.system_message = None
        self.runtime = None


def test_readonly_hides_run_code_and_refuses_forced_call(assemble) -> None:
    model, agent, captured = assemble(profile="readonly", mode="both", responses=[DONE])
    _invoke(agent, "readonly")
    names = set(model.tool_name_batches[0])
    assert "run_code" not in names
    assert "write_file" not in names  # readonly harness exclusion
    ptc = _ptc(captured)
    assert ptc is not None and list(ptc.tools) == []

    def _handler(_request: Any) -> ToolMessage:  # pragma: no cover - must not run
        raise AssertionError("readonly run_code must never reach the tool node")

    request = _ToolCallRequest({"name": "run_code", "args": {}, "id": "c", "type": "tool_call"})
    refusal = ptc.wrap_tool_call(request, _handler)
    assert refusal.status == "error"
    assert "Permission denied" in refusal.content


# --------------------------------------------------------------------------- #
# require_approval (dev-approve) HITL on the outer run_code
# --------------------------------------------------------------------------- #
SIDE_EFFECT_CODE = (
    "from pathlib import Path\n"
    "Path('side_effect.txt').write_text('ran', encoding='utf-8')\n"
    "return 'ran'\n"
)


def test_require_approval_interrupts_outer_run_code_before_side_effect(assemble, tmp_path) -> None:
    _, agent, captured = assemble(
        profile="dev-approve", mode="both", responses=[_run_code_ai(SIDE_EFFECT_CODE), DONE]
    )
    assert captured["interrupt_on"]["run_code"] is True
    result = _invoke(agent, "approve-interrupt")
    interrupts = result.get("__interrupt__")
    assert interrupts, "run_code must pause for approval"
    assert "run_code" in str(interrupts[0].value)
    assert not (tmp_path / "side_effect.txt").exists()


def test_rejected_resume_never_executes_run_code(assemble, tmp_path) -> None:
    _, agent, _ = assemble(
        profile="dev-approve", mode="both", responses=[_run_code_ai(SIDE_EFFECT_CODE), DONE]
    )
    thread = "approve-reject"
    assert _invoke(agent, thread).get("__interrupt__")
    result = _resume(agent, {"type": "reject"}, thread)
    assert not (tmp_path / "side_effect.txt").exists()
    message = _run_code_message(result)
    assert message.status == "error"
    assert "reject" in message.content.lower()


def test_approved_resume_runs_readonly_subtools(assemble, tmp_path) -> None:
    (tmp_path / "a.py").write_text("print('approved-ptc')\n", encoding="utf-8")
    code = (
        "files = await tools.find_files(pattern='**/*.py')\n"
        "content = await tools.read_file(file_path='/a.py')\n"
        "return {'files': files, 'content': content}\n"
    )
    _, agent, _ = assemble(profile="dev-approve", mode="both", responses=[_run_code_ai(code), DONE])
    thread = "approve-ok"
    assert _invoke(agent, thread).get("__interrupt__")
    message = _run_code_message(_resume(agent, {"type": "approve"}, thread))
    assert message.status == "success", message.content
    value = _value(message)
    assert value["files"]["data"]["matches"] == [{"path": "/a.py", "is_dir": False}]
    assert "approved-ptc" in value["content"]["content"]


def test_require_approval_denies_nested_write_and_execute(assemble, tmp_path) -> None:
    code = (
        "seen = []\n"
        "for name, args in [\n"
        "    ('write_file', {'file_path': '/nope.txt', 'content': 'x'}),\n"
        "    ('execute', {'command': 'echo hi'}),\n"
        "]:\n"
        "    try:\n"
        "        await tools.call(name, args)\n"
        "        seen.append(name + ':allowed')\n"
        "    except ToolCallError as exc:\n"
        "        seen.append(name + ':' + exc.kind)\n"
        "return seen\n"
    )
    _, agent, _ = assemble(profile="dev-approve", mode="both", responses=[_run_code_ai(code), DONE])
    thread = "approve-nested"
    assert _invoke(agent, thread).get("__interrupt__")
    message = _run_code_message(_resume(agent, {"type": "approve"}, thread))
    assert message.status == "success", message.content
    assert _value(message) == ["write_file:denied", "execute:denied"]
    assert not (tmp_path / "nope.txt").exists()


# --------------------------------------------------------------------------- #
# dev-autopass can write a temp file from run_code
# --------------------------------------------------------------------------- #
def test_autopass_nested_write_file_writes_into_workspace(assemble, tmp_path) -> None:
    code = "return await tools.write_file(file_path='/written.txt', content='hello-ptc')\n"
    _, agent, _ = assemble(
        profile="dev-autopass", mode="both", responses=[_run_code_ai(code), DONE]
    )
    message = _run_code_message(_invoke(agent, "autopass-write"))
    assert message.status == "success", message.content
    assert (tmp_path / "written.txt").read_text(encoding="utf-8") == "hello-ptc"


# --------------------------------------------------------------------------- #
# Unavailable tools are refused before the script runs (no partial side effect)
# --------------------------------------------------------------------------- #
def test_preflight_refuses_unavailable_tool_before_any_side_effect(assemble, tmp_path) -> None:
    """A script naming an excluded tool must not run at all.
    The write comes *first*: if the check happened mid-script (as it did before
    the pre-flight), the file would already exist when the run failed.
    """
    code = (
        "await tools.write_file(file_path='/side-effect.txt', content='x')\n"
        "await tools.read_file(file_path='/a.py')\n"
        "return 'done'\n"
    )
    _, agent, _ = assemble(
        profile="dev-autopass",
        mode="both",
        responses=[_run_code_ai(code), DONE],
        excluded=["read_file"],
    )
    message = _run_code_message(_invoke(agent, "preflight"))

    assert message.status == "error", message.content
    payload = json.loads(message.content)
    assert payload["error"]["kind"] == "unknown_tool"
    assert "read_file" in payload["error"]["message"]
    assert "Available tools:" in payload["error"]["message"]
    assert payload["logs"] == []
    # Nothing executed: no write, and the refused tool never ran either.
    assert not (tmp_path / "side-effect.txt").exists()


def test_preflight_does_not_block_dynamic_tool_access(assemble, tmp_path) -> None:
    """`tools.call(name, ...)` with a variable cannot be judged, so it still runs."""
    code = (
        "name = 'write_file'\n"
        "res = await tools.call(name, {'file_path': '/dynamic.txt', 'content': 'ok'})\n"
        "return res['content']\n"
    )
    _, agent, _ = assemble(
        profile="dev-autopass", mode="both", responses=[_run_code_ai(code), DONE]
    )
    message = _run_code_message(_invoke(agent, "dynamic"))

    assert message.status == "success", message.content
    assert (tmp_path / "dynamic.txt").read_text(encoding="utf-8") == "ok"


def test_tools_available_matches_the_callable_set(assemble) -> None:
    """The script can self-check; the tuple excludes policy-denied tools."""
    code = (
        "return {\n"
        "    'is_tuple': isinstance(tools.available, tuple),\n"
        "    'has_execute': 'execute' in tools.available,\n"
        "    'has_read_file': 'read_file' in tools.available,\n"
        "    'count': len(tools.available),\n"
        "}\n"
    )
    _, agent, _ = assemble(
        profile="dev-autopass",
        mode="both",
        responses=[_run_code_ai(code), DONE],
        excluded=["read_file"],
    )
    value = _value(_run_code_message(_invoke(agent, "available")))

    assert value["is_tuple"] is True
    assert value["has_read_file"] is False
    assert value["has_execute"] is True
    assert value["count"] > 0


def test_injected_sdk_states_the_allowlist_and_dict_access(assemble) -> None:
    """The prompt the model reads must forbid unlisted names and object access."""
    model, agent, _ = assemble(profile="dev-autopass", mode="both", responses=[DONE])
    _invoke(agent, "sdk-text")
    text = model.system_texts[0]

    assert "Only the tools listed below are callable" in text
    assert "tools.available" in text
    assert 'res["content"]' in text or "res['content']" in text
    assert "res.content" not in text
