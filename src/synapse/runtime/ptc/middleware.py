"""PTC middleware: expose tools through a single ``run_code`` tool.

``build_ptc_middleware`` returns an :class:`AgentMiddleware` that:

* registers a ``run_code(code: str, intent: str)`` tool (its Python body is a
  placeholder -- every real invocation is intercepted and handed to the local
  subprocess runtime),
* injects a stable Python SDK description of the registered tools into the
  model's system message so the model knows what it can orchestrate,
* intercepts the ``run_code`` tool call, runs the code through
  ``synapse.runtime.ptc.process.run_code`` with a dispatch callback that
  re-enters the tool node for each child call, and returns a single JSON
  ``ToolMessage`` (its status follows the run's error),
* in ``code`` mode, hides the orchestratable tools from the model so only
  ``run_code`` and the non-orchestratable (approval / session-state) tools stay
  native.

The subprocess is a crash/IO boundary, **not** an OS permission sandbox: model
code runs with the same trust as the user's shell. The middleware is meant to sit
*before* the tool-exclusion middleware in the stack, so it re-applies
``excluded_tools`` itself when building the SDK and when dispatching child calls.
Read-only mode and an explicit ``run_code`` exclusion both disable the tool
entirely: plain Python can still write to the OS, so the only safe read-only
behaviour is to not offer the tool at all.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from synapse.runtime.ptc import protocol
from synapse.runtime.ptc import sdk as sdk_module
from synapse.runtime.ptc.bridge import (
    RUN_CODE,
    CallContext,
    PtcBridge,
)
from synapse.runtime.ptc.process import _finalize_output
from synapse.runtime.ptc.scheduler import FairReadWriteScheduler

if TYPE_CHECKING:
    from synapse.runtime.ptc.protocol import PtcLimits

#: Modes accepted by :func:`build_ptc_middleware`.
MODES = frozenset({"native", "both", "code"})

#: Upper bound on the error text we echo back for a failed ``run_code`` call.
_MAX_ERROR_CHARS = 2_000

#: The only keys ``run_code`` accepts. The middleware intercepts the call before
#: the tool's Pydantic schema (which forbids extras) runs, so it must reject any
#: other key here rather than silently ignoring it.
_ALLOWED_RUN_CODE_KEYS = frozenset({"code", "intent"})

_RUN_CODE_DESCRIPTION = (
    "Execute Python code that orchestrates the available tools programmatically. "
    "The code runs as the body of an async function in a fresh local subprocess "
    "(not a security sandbox) and may call the tools documented in the "
    "programmatic tool-calling SDK. Returns JSON with `logs`, `value` and an "
    "optional `error`."
)


def _run_code_placeholder(code: str, intent: str) -> str:  # noqa: ARG001
    """Placeholder body; the middleware intercepts every real invocation."""
    return json.dumps(
        {
            "logs": [],
            "value": None,
            "error": {
                "kind": "unreachable",
                "message": "run_code placeholder executed without the PTC middleware",
            },
        }
    )


def _tool_name(tool: Any) -> str:
    """Return a tool name from a ``BaseTool`` or a dict-style schema."""
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, Mapping):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", None) or "")


def _tool_call_args(tool_call: Any) -> Mapping[str, Any] | None:
    if isinstance(tool_call, Mapping):
        args = tool_call.get("args")
    else:
        args = getattr(tool_call, "args", None)
    return args if isinstance(args, Mapping) else None


def _run_code_args(tool_call: Any) -> tuple[str, str] | None:
    """Strictly validate ``run_code``'s arguments, or ``None`` when invalid.

    The middleware intercepts the call before the tool's Pydantic schema runs, so
    it must validate here. Only ``code`` and ``intent`` are accepted: the tool's
    model-facing schema forbids extra keys, but interception would otherwise let
    a stray field through silently. ``code`` must be a ``str`` (an empty string
    is allowed -- it simply runs as an empty body) and ``intent`` a non-empty
    ``str``. Coercing an arbitrary value with ``str(...)`` would let a non-string
    ``code`` (a dict, a list) slip past the schema and execute as its ``repr``.
    """
    args = _tool_call_args(tool_call)
    if args is None:
        return None
    if any(key not in _ALLOWED_RUN_CODE_KEYS for key in args):
        return None
    code = args.get("code")
    intent = args.get("intent")
    if not isinstance(code, str) or not isinstance(intent, str) or not intent.strip():
        return None
    return code, intent


def _bounded_text(text: str, limit: int = _MAX_ERROR_CHARS) -> str:
    """Truncate human-readable message text to a bounded length."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


def _run_coroutine_sync(coro: Any) -> Any:
    """Run ``coro`` from synchronous tool-call code.

    The sync tool path normally runs on a worker thread with no event loop, so
    ``asyncio.run`` is safe. If a loop *is* running in this thread (an unusual
    embedding), the coroutine is driven on a private loop in its own thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _append_system_prompt(system_message: SystemMessage | None, prompt: str) -> SystemMessage:
    """Append ``prompt`` as a new text block, preserving the existing blocks.

    The system message may already be a list of content blocks carrying prompt
    cache markers or images; flattening it to one string would destroy those. We
    copy the *original* ``content`` list verbatim (not ``content_blocks``, whose
    normalisation could rewrite a block and move a cache boundary) and append the
    SDK as one more text block. ``model_copy`` preserves the original message
    metadata (``additional_kwargs`` and friends).
    """
    block = {"type": "text", "text": prompt}
    if system_message is None:
        return SystemMessage(content=[block])
    raw = getattr(system_message, "content", "")
    if isinstance(raw, list):
        blocks = list(raw)
    elif isinstance(raw, str) and raw:
        blocks = [raw]
    else:
        blocks = []
    content = [*blocks, block]
    try:
        return system_message.model_copy(update={"content": content})
    except Exception:  # noqa: BLE001 - exotic message subclass
        return SystemMessage(content=content)


class _PtcMiddleware(AgentMiddleware):
    """The PTC middleware instance returned by :func:`build_ptc_middleware`."""

    def __init__(
        self,
        *,
        mode: str,
        bridge: PtcBridge,
        run_code_tool: Any | None,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._bridge = bridge
        self.tools = [run_code_tool] if run_code_tool is not None else []

    # -- model call: inject the SDK, fold tools in code mode ----------------
    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return handler(self._prepare_request(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return await handler(self._prepare_request(request))

    def _prepare_request(self, request):  # type: ignore[no-untyped-def]
        if not self._bridge.run_code_enabled:
            return request
        tools = list(getattr(request, "tools", None) or [])
        specs = self._sdk_specs(tools)
        prompt, fold = self._sdk_prompt(request, specs)
        changes: dict[str, Any] = {}
        if prompt:
            changes["system_message"] = _append_system_prompt(
                getattr(request, "system_message", None), prompt
            )
        if fold and self._mode == "code":
            visible = [tool for tool in tools if not self._is_foldable(_tool_name(tool))]
            if len(visible) != len(tools):
                changes["tools"] = visible
        if not changes:
            return request
        return request.override(**changes)

    def _is_foldable(self, name: str) -> bool:
        if not name or self._bridge.is_excluded(name):
            return False
        return self._bridge.is_orchestratable(name)

    def _sdk_specs(self, tools: list[Any]) -> list[sdk_module.ToolSpec]:
        return sdk_module.collect_tool_specs(
            tool for tool in tools if self._is_foldable(_tool_name(tool))
        )

    def _sdk_prompt(
        self,
        request: Any,
        specs: list[sdk_module.ToolSpec],
    ) -> tuple[str, bool]:
        """Return ``(prompt, fold)``: the SDK text and whether folding is safe.

        When the full SDK trips its budget we fall back to the bounded compact
        SDK and refuse to fold for this request, so the model can still read the
        exact parameter schemas from the native tool definitions.
        """
        limits = self._bridge.limits
        try:
            prompt = sdk_module.build_sdk_prompt(
                specs,
                max_calls=getattr(limits, "max_calls", None),
                max_parallel=getattr(limits, "max_parallel", None),
            )
            return prompt, True
        except sdk_module.SdkBudgetExceeded as exc:
            # Explicit, smaller SDK -- never a silently truncated schema.
            self._emit_sdk_warning(request, exc)
            return sdk_module.build_compact_sdk_prompt(specs), False

    def _emit_sdk_warning(self, request: Any, exc: sdk_module.SdkBudgetExceeded) -> None:
        runtime = getattr(request, "runtime", None)
        writer = getattr(runtime, "stream_writer", None)
        if not callable(writer):
            return
        try:
            writer(
                {
                    "type": "ptc_sdk",
                    "event": "budget_exceeded",
                    "size": exc.size,
                    "budget": exc.budget,
                }
            )
        except Exception:  # noqa: BLE001 - streaming is best-effort
            pass

    # -- tool call: intercept run_code --------------------------------------
    def wrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        if _tool_name(getattr(request, "tool_call", None)) != RUN_CODE:
            return handler(request)
        if not self._bridge.run_code_enabled:
            return self._refusal(request)
        args = _run_code_args(getattr(request, "tool_call", None))
        if args is None:
            return self._invalid_args_message(request)
        code, _intent = args
        ctx = self._context(request, handler, offload=True)
        try:
            result = _run_coroutine_sync(self._bridge.invoke(ctx, code=code))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to an error message
            return self._error_message(request, exc)
        return self._result_message(request, result)

    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        if _tool_name(getattr(request, "tool_call", None)) != RUN_CODE:
            return await handler(request)
        if not self._bridge.run_code_enabled:
            return self._refusal(request)
        args = _run_code_args(getattr(request, "tool_call", None))
        if args is None:
            return self._invalid_args_message(request)
        code, _intent = args
        ctx = self._context(request, handler, offload=False)
        try:
            result = await self._bridge.invoke(ctx, code=code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to an error message
            return self._error_message(request, exc)
        return self._result_message(request, result)

    def _context(self, request: Any, handler: Any, *, offload: bool) -> CallContext:
        runtime = getattr(request, "runtime", None)
        tools: dict[str, Any] = {}
        for tool in getattr(runtime, "tools", None) or []:
            name = getattr(tool, "name", None)
            if name:
                tools[str(name)] = tool
        return CallContext(
            parent_call_id=_tool_call_id(getattr(request, "tool_call", None)),
            handler=handler,
            request=request,
            runtime=runtime,
            tools=tools,
            stream_writer=getattr(runtime, "stream_writer", None),
            offload=offload,
            # Hand the subprocess *every* registered name: exclusion, approval and
            # recursion are enforced by bridge.denial_reason, so a denied tool is
            # not masked as an unregistered one.
            tool_names=tuple(self._bridge.registered_names(tools)),
        )

    def _output_cap(self) -> int:
        """The combined byte cap the model-facing ``run_code`` message must fit."""
        cap = getattr(self._bridge.limits, "max_output_bytes", None)
        return int(cap) if isinstance(cap, int) and cap > 0 else 64_000

    def _encode_error(self, kind: str, message: str) -> str:
        """A bounded JSON ``{logs, value, error}`` message that always fits the cap.

        Routed through :func:`_finalize_output` so even a tiny
        ``max_output_bytes`` (the 256-byte floor) yields a valid, capped envelope
        rather than an oversized free-text string.
        """
        payload = {"logs": [], "value": None, "error": {"kind": kind, "message": message}}
        bounded = _finalize_output(payload, self._output_cap())
        return protocol.encode_json(bounded).decode("utf-8")

    def _result_message(self, request: Any, result: Any) -> ToolMessage:
        """Serialise a runner result into a canonical, capped ``ToolMessage``.

        The bridge may append a timeout warning *after* the runner returned its
        already-capped result, so the combined message is re-capped here with the
        same ``encode_json`` / ``_finalize_output`` contract the host uses. A
        non-object or non-finite (NaN/Infinity) result is an explicit error,
        never a success.
        """
        cap = self._output_cap()
        if not isinstance(result, Mapping):
            content = self._encode_error("result", "run_code returned a non-object result")
            is_error = True
        else:
            try:
                # Rejects NaN / Infinity (allow_nan=False) and non-serialisable values.
                protocol.encode_json(result)
            except (TypeError, ValueError):
                content = self._encode_error(
                    "serialize", "run_code result was not JSON serializable"
                )
                is_error = True
            else:
                bounded = _finalize_output(dict(result), cap)
                error = bounded.get("error")
                is_error = isinstance(error, Mapping) and bool(error)
                content = protocol.encode_json(bounded).decode("utf-8")
        return ToolMessage(
            content=content,
            tool_call_id=_tool_call_id(getattr(request, "tool_call", None)),
            name=RUN_CODE,
            status="error" if is_error else "success",
        )

    def _error_message(self, request: Any, exc: Exception) -> ToolMessage:
        detail = _bounded_text(f"{type(exc).__name__}: {exc}")
        return ToolMessage(
            content=self._encode_error("middleware", detail),
            tool_call_id=_tool_call_id(getattr(request, "tool_call", None)),
            name=RUN_CODE,
            status="error",
        )

    def _invalid_args_message(self, request: Any) -> ToolMessage:
        return ToolMessage(
            content=_bounded_text(
                "run_code requires `code` (a string), `intent` (a non-empty string) "
                "and no other arguments; the call was rejected before any code ran."
            ),
            tool_call_id=_tool_call_id(getattr(request, "tool_call", None)),
            name=RUN_CODE,
            status="error",
        )

    def _refusal(self, request: Any) -> ToolMessage:
        return ToolMessage(
            content=_bounded_text(
                "Permission denied: run_code is not available in this mode "
                "(read-only or excluded). Use the available native tools instead."
            ),
            tool_call_id=_tool_call_id(getattr(request, "tool_call", None)),
            name=RUN_CODE,
            status="error",
        )


def build_ptc_middleware(
    *,
    mode: str,
    project_root: Path,
    excluded_tools: Collection[str],
    require_approval: bool,
    readonly: bool,
    limits: PtcLimits,
    scheduler: FairReadWriteScheduler | None = None,
    run_code: Any | None = None,
) -> AgentMiddleware:
    """Build the PTC middleware for the coding agent.

    Args:
        mode: ``"native"`` (inert; the assembly does not call this), ``"both"``
            (native tools *and* ``run_code`` stay visible) or ``"code"`` (fold
            orchestratable tools into ``run_code``).
        project_root: Working directory for generated code (the subprocess is not
            a security sandbox).
        excluded_tools: Names the host hides from the model; re-applied here
            because this middleware runs before the exclusion middleware.
        require_approval: When true, child calls to approval-gated or
            unknown-contract tools are refused (use the native tool instead).
        readonly: When true, ``run_code`` is neither exposed nor runnable.
        limits: The run's :class:`~synapse.runtime.ptc.protocol.PtcLimits`.
        scheduler: Optional shared scheduler (mostly for tests).
        run_code: Optional runner override (mostly for tests).
    """
    normalized = str(mode).strip().lower()
    if normalized not in MODES:
        msg = f"unknown PTC mode {mode!r}; expected one of {sorted(MODES)}"
        raise ValueError(msg)

    bridge = PtcBridge(
        project_root=project_root,
        excluded_tools=excluded_tools,
        require_approval=require_approval,
        readonly=readonly,
        limits=limits,
        mode=normalized,
        scheduler=scheduler,
        run_code=run_code,
    )
    run_code_tool: Any | None = None
    if bridge.run_code_enabled:
        run_code_tool = StructuredTool.from_function(
            func=_run_code_placeholder,
            name=RUN_CODE,
            description=_RUN_CODE_DESCRIPTION,
        )
    return _PtcMiddleware(
        mode=normalized,
        bridge=bridge,
        run_code_tool=run_code_tool,
    )


__all__ = ["MODES", "build_ptc_middleware"]
