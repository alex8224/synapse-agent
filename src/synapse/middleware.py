"""Agent middleware helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage

from synapse.pathing import rewrite_tool_args_paths


def _dual_wrap_model_call(*, name: str, apply):
    """Build model-call middleware with both sync and async hooks.

    ``apply(request) -> request`` mutates/overrides the request; the handler is
    always invoked (sync or async). Required for ``astream`` / ``ainvoke``.
    """

    def wrap_model_call(self, request, handler):  # noqa: ANN001, ARG001
        return handler(apply(request))

    async def awrap_model_call(self, request, handler):  # noqa: ANN001, ARG001
        return await handler(apply(request))

    return type(
        name,
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "wrap_model_call": wrap_model_call,
            "awrap_model_call": awrap_model_call,
        },
    )()


def _dual_wrap_tool_call(*, name: str, apply):
    """Build tool-call middleware with both sync and async hooks."""

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        return handler(apply(request))

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        return await handler(apply(request))

    return type(
        name,
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "wrap_tool_call": wrap_tool_call,
            "awrap_tool_call": awrap_tool_call,
        },
    )()

# Required model-facing field: short purpose shown in the timeline UI.
TOOL_INTENT_KEY = "intent"
TOOL_INTENT_DESCRIPTION = (
    "Required. One short sentence describing WHY this tool is being called "
    "(user-facing intent for the timeline UI). Prefer Chinese. "
    "Example: 'inspect pytest config' / 'locate login failure'. "
    "Do not dump raw args or only restate the tool name."
)


def build_path_normalize_middleware(workspace: Path):
    """Rewrite host/Windows paths in tool args to virtual ``/`` paths."""

    root = Path(workspace).resolve()

    def _apply(request):  # type: ignore[no-untyped-def]
        tool_call = request.tool_call
        # tool_call may be dict-like
        if isinstance(tool_call, dict):
            args = dict(tool_call.get("args") or {})
            new_args = rewrite_tool_args_paths(args, root)
            if new_args != args:
                new_call = {**tool_call, "args": new_args}
                return request.override(tool_call=new_call)
            return request
        args = dict(getattr(tool_call, "args", None) or {})
        new_args = rewrite_tool_args_paths(args, root)
        if new_args != args:
            # Best-effort for object-style tool_call
            try:
                new_call = {
                    "name": getattr(tool_call, "name", None),
                    "args": new_args,
                    "id": getattr(tool_call, "id", None),
                    "type": getattr(tool_call, "type", "tool_call"),
                }
                return request.override(tool_call=new_call)
            except Exception:  # noqa: BLE001
                return request
        return request

    return _dual_wrap_tool_call(name="normalize_virtual_paths", apply=_apply)


def build_tool_error_recovery_middleware():
    """Return tool failures to the model instead of terminating the agent graph."""

    def _error_message(request, exc: Exception) -> ToolMessage:  # type: ignore[no-untyped-def]
        tool_call = request.tool_call
        if isinstance(tool_call, dict):
            name = str(tool_call.get("name") or "tool")
            call_id = str(tool_call.get("id") or "unknown")
        else:
            name = str(getattr(tool_call, "name", None) or "tool")
            call_id = str(getattr(tool_call, "id", None) or "unknown")
        return ToolMessage(
            content=(
                f"Error: {name} failed ({type(exc).__name__}): {exc}\n"
                "The tool call failed. Continue the task by correcting the arguments "
                "or choosing another safe tool."
            ),
            tool_call_id=call_id,
            name=name,
            status="error",
        )

    def wrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        try:
            return handler(request)
        except Exception as exc:  # noqa: BLE001
            return _error_message(request, exc)

    async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ARG001
        try:
            return await handler(request)
        except Exception as exc:  # noqa: BLE001
            return _error_message(request, exc)

    return type(
        "recover_tool_errors",
        (AgentMiddleware,),
        {
            "state_schema": AgentState,
            "tools": [],
            "wrap_tool_call": wrap_tool_call,
            "awrap_tool_call": awrap_tool_call,
        },
    )()


def _tool_name(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    return str(getattr(tool, "__name__", tool))


def build_tool_exclusion_middleware(excluded: set[str] | frozenset[str] | list[str]):
    """Hide tools from the model request (LocalShell-safe alternative to permissions).

    deepagents ``FilesystemPermission`` cannot be combined with backends that
    implement command execution. Use this middleware for product isolation.
    """
    blocked = frozenset(str(x) for x in excluded if x)

    def _apply(request):  # type: ignore[no-untyped-def]
        if not blocked:
            return request
        tools = getattr(request, "tools", None) or []
        filtered = [t for t in tools if _tool_name(t) not in blocked]
        if len(filtered) != len(tools):
            return request.override(tools=filtered)
        return request

    return _dual_wrap_model_call(name="exclude_tools", apply=_apply)


def _strip_intent_from_tool_call(tool_call: Any) -> Any:
    """Remove intent from args before the real tool schema validates."""
    if isinstance(tool_call, dict):
        args = dict(tool_call.get("args") or {})
        if TOOL_INTENT_KEY not in args:
            return tool_call
        args.pop(TOOL_INTENT_KEY, None)
        return {**tool_call, "args": args}

    args = dict(getattr(tool_call, "args", None) or {})
    if TOOL_INTENT_KEY not in args:
        return tool_call
    args.pop(TOOL_INTENT_KEY, None)
    try:
        return {
            "name": getattr(tool_call, "name", None),
            "args": args,
            "id": getattr(tool_call, "id", None),
            "type": getattr(tool_call, "type", "tool_call"),
        }
    except Exception:  # noqa: BLE001
        return tool_call


def _field_definitions_with_intent(schema: Any) -> dict[str, Any] | None:
    """Build create_model field map with required intent first."""
    from pydantic import Field
    from pydantic_core import PydanticUndefined

    fields = getattr(schema, "model_fields", None)
    if not fields:
        return None
    if TOOL_INTENT_KEY in fields:
        return None

    defs: dict[str, Any] = {
        TOOL_INTENT_KEY: (
            str,
            Field(..., description=TOOL_INTENT_DESCRIPTION),
        )
    }
    for name, finfo in fields.items():
        ann = finfo.annotation
        desc = finfo.description or name
        if finfo.is_required():
            defs[name] = (ann, Field(..., description=desc))
            continue
        default = finfo.default
        default_factory = getattr(finfo, "default_factory", None)
        if default_factory is not None and default is PydanticUndefined:
            defs[name] = (ann, Field(default_factory=default_factory, description=desc))
        elif default is PydanticUndefined:
            defs[name] = (ann, Field(default=None, description=desc))
        else:
            defs[name] = (ann, Field(default=default, description=desc))
    return defs


def add_intent_to_tool(tool: Any, *, cache: dict[int, Any] | None = None) -> Any:
    """Return a model-facing tool clone with required ``intent`` in args schema.

    Tools may include injected runtime args (e.g. ``ToolRuntime`` / ``BaseStore``).
    Those annotations are not JSON-serializable; create the schema with
    ``arbitrary_types_allowed=True`` so wrapping does not crash the agent.
    LangChain's ``tool_call_schema`` still hides injected fields from the model.
    """
    if cache is not None:
        cached = cache.get(id(tool))
        if cached is not None:
            return cached

    get_schema = getattr(tool, "get_input_schema", None)
    if not callable(get_schema):
        return tool
    try:
        schema = get_schema()
    except Exception:  # noqa: BLE001
        return tool

    defs = _field_definitions_with_intent(schema)
    if defs is None:
        return tool

    from langchain_core.tools import StructuredTool
    from pydantic import ConfigDict, create_model

    title = getattr(schema, "__name__", None) or f"{_tool_name(tool)}Schema"
    try:
        # compact_conversation etc. carry ToolRuntime (contains BaseStore).
        NewModel = create_model(
            f"{title}WithIntent",
            __config__=ConfigDict(arbitrary_types_allowed=True),
            **defs,
        )
    except Exception:  # noqa: BLE001
        # Never break the whole turn because one tool schema is exotic.
        return tool

    def _sync(**kwargs: Any) -> Any:
        data = dict(kwargs)
        data.pop(TOOL_INTENT_KEY, None)
        return tool.invoke(data)

    async def _async(**kwargs: Any) -> Any:
        data = dict(kwargs)
        data.pop(TOOL_INTENT_KEY, None)
        return await tool.ainvoke(data)

    try:
        wrapped = StructuredTool.from_function(
            func=_sync,
            coroutine=_async,
            name=_tool_name(tool),
            description=getattr(tool, "description", None) or _tool_name(tool),
            args_schema=NewModel,
        )
    except Exception:  # noqa: BLE001
        return tool

    if cache is not None:
        cache[id(tool)] = wrapped
    return wrapped


def build_intent_schema_middleware():
    """Inject required ``intent`` into every tool schema the model sees.

    Execution path strips ``intent`` so original deepagents tools keep working.
    The stream/UI still sees ``intent`` on AI tool_calls and can render it.
    """
    cache: dict[int, Any] = {}

    def _apply_inject(request):  # type: ignore[no-untyped-def]
        tools = list(getattr(request, "tools", None) or [])
        if not tools:
            return request
        rewritten = [add_intent_to_tool(t, cache=cache) for t in tools]
        if rewritten != tools:
            return request.override(tools=rewritten)
        return request

    def _apply_strip(request):  # type: ignore[no-untyped-def]
        tool_call = getattr(request, "tool_call", None)
        if tool_call is None:
            return request
        new_call = _strip_intent_from_tool_call(tool_call)
        if new_call is not tool_call:
            try:
                return request.override(tool_call=new_call)
            except Exception:  # noqa: BLE001
                return request
        return request

    # Stack as a list: model-facing schema rewrite + tool-exec intent strip.
    return [
        _dual_wrap_model_call(name="require_tool_intent", apply=_apply_inject),
        _dual_wrap_tool_call(name="strip_tool_intent", apply=_apply_strip),
    ]