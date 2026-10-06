"""Python SDK surface advertised to the model for programmatic tool calling (PTC).

The PTC middleware folds orchestratable tools into a single ``run_code`` tool.
For the model to write *useful* code it needs a precise description of the tools
it may orchestrate from inside that code. This module turns the model-facing
tool list into a compact, deterministic Python SDK document:

* an exact JSON Schema for each tool's inputs when the tool publishes one; when
  only the argument *names* are known the stub is honest about it (``**kwargs``
  plus an "unknown schema" note) instead of inventing optional parameters,
* a typed ``async`` Python function stub per tool, called with keyword arguments
  and ``await``-ed exactly like the worker's ``tools`` object,
* a generic ``tools.call(name, args)`` escape hatch for exotic names or argument
  names that are not valid Python identifiers,
* the shared ``ToolEnvelope`` return contract and the ``ToolCallError`` surface.

Design constraints (see the PTC requirements):

* The code block must be *compilable Python*: stubs are ``async def`` with
  keyword-only parameters, and any name that cannot be a Python identifier falls
  back to ``**kwargs`` or the generic ``tools.call`` recipe.
* Descriptions are bounded per tool, and the whole document has a byte budget.
  When the budget is exceeded we raise :class:`SdkBudgetExceeded` instead of
  silently truncating a schema -- a half-schema is worse than a loud failure.
  The middleware then falls back to :func:`build_compact_sdk_prompt`, which is
  itself byte-bounded and only lists a bounded name prefix.
* Runtime-injected parameters (``InjectedState`` / ``InjectedStore`` /
  ``ToolRuntime``) are stripped: the model's code cannot supply them and the
  bridge injects them automatically.
* Output types are never guessed. Every stub returns ``ToolEnvelope`` and the
  ``data`` shape is only advertised when a tool publishes one through its
  ``metadata['ptc_output_schema']``.
"""

from __future__ import annotations

import json
import keyword
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Default byte budget for the whole SDK document. Sized to fold the *default*
#: coding toolset (about 25 KB of typed stubs) while staying near an 8k-token
#: English ceiling; it is deliberately finite, so a pathological schema set
#: still trips the loud failure below rather than shipping a truncated schema.
DEFAULT_MAX_SDK_BYTES = 32_000

#: Per-tool description cap so one verbose tool cannot eat the whole budget.
DEFAULT_DESCRIPTION_CHARS = 600

#: Bound for the compact fallback: a total byte ceiling plus a cap on how many
#: tool names it lists. Both are enforced, so the fallback can never itself grow
#: without limit.
COMPACT_MAX_BYTES = 4_000
COMPACT_MAX_NAMES = 60
COMPACT_DESCRIPTION_CHARS = 120

#: Metadata key a tool uses to publish the JSON Schema of its ``data`` payload.
OUTPUT_SCHEMA_METADATA_KEY = "ptc_output_schema"

_JSON_TYPE_TO_PY: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}

_SELECTION_BLOCK = """\
When to use `run_code`:
* Prefer it for batching tool calls, mechanical pagination/filtering/aggregation,
  or substantially reducing tool output before returning it to the model.
* When native tools are visible, use them directly for a single command/read or
  exploratory steps whose next action requires model judgment. Do not wrap a
  single `execute` call merely to run a script; `execute` can already do that.
* Batch only steps already known or mechanically determined from tool results.
  Return to the model when interpretation or planning is needed.
* If a needed tool is hidden in code mode, use a minimal `run_code` call; do not
  invent extra batching. Follow explicit user requests to use `run_code`.
"""

_ENVELOPE_BLOCK = """\
Every successful call returns a ``ToolEnvelope``: a **plain ``dict``**, not an
object. Read its fields with subscripts -- ``res["content"]``, ``res["data"]``,
``res["truncated"]`` -- never with attribute access: a ``dict`` has no field
attributes, so attribute access raises ``AttributeError``::

    class ToolEnvelope(TypedDict):
        content: str | list       # human-readable text; may be truncated
        data: Any | None          # canonical structured payload, or None
        truncated: bool | None    # True/False when known, None when unknown

After one awaited call, read the envelope by key. For example::

    res = await tools.find_files(pattern="**/*.py", path="/")
    paths = [match["path"] for match in (res["data"] or {}).get("matches", [])]

and for a tool that publishes no canonical payload::

    res = await tools.read_file(path="README.md")
    print(res["content"])

``data`` carries the tool's canonical machine-readable payload only when the
tool publishes one; otherwise it is ``None`` and ``content`` holds the full
output. **Never guess a shape for ``data``**: when it is ``None`` do not index
it (for example do not assume ``data["matches"]``) -- read ``content`` instead.
``truncated`` is ``True``/``False`` only when the host knows whether output was
cut, and ``None`` when completeness is unknown; never treat ``None`` as
"complete".

A failed call raises ``ToolCallError`` with:

* ``kind`` -- ``"denied"``, ``"unknown"``, ``"tool_error"``, ``"command"`` or ``"limit"``
* ``name`` / ``tool_name`` -- the tool that failed
* ``message`` -- a human-readable reason

A denied, unregistered or approval-gated tool raises the same error and never
runs. Output types are not declared; always read the envelope.
"""


class SdkBudgetExceeded(Exception):
    """Raised when the full SDK document exceeds its byte budget.

    The caller (the middleware) turns this into an explicit, smaller fallback
    SDK rather than shipping a truncated schema.
    """

    def __init__(self, *, size: int, budget: int) -> None:
        super().__init__(
            f"programmatic tool-calling SDK is {size} bytes, over the {budget}-byte budget"
        )
        self.size = size
        self.budget = budget


@dataclass(frozen=True)
class ToolSpec:
    """One orchestratable tool as the model's code sees it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    injected_keys: frozenset[str] = frozenset()
    output_schema: dict[str, Any] | None = None
    #: ``True`` when ``input_schema`` is the tool's full JSON Schema (carrying
    #: ``required`` / ``additionalProperties`` / ``$defs``). ``False`` when only
    #: the argument *names* could be recovered, so the SDK must not claim the
    #: missing arguments are optional.
    schema_fidelity: bool = True

    @property
    def is_python_name(self) -> bool:
        """Whether ``name`` can be emitted as a Python function definition."""
        return _is_python_identifier(self.name)


def _is_python_identifier(name: Any) -> bool:
    return isinstance(name, str) and name.isidentifier() and not keyword.iskeyword(name)


def _tool_name(tool: Any) -> str:
    """Return a tool name from a ``BaseTool`` or a model-facing dict schema."""
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _tool_description(tool: Any) -> str:
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            return str(function.get("description") or "")
        return str(tool.get("description") or "")
    return str(getattr(tool, "description", "") or "")


def _tool_metadata(tool: Any) -> Mapping[str, Any]:
    """Return a tool's metadata mapping, tolerating both tool and dict shapes."""
    if isinstance(tool, Mapping):
        function = tool.get("function")
        nested = function.get("metadata") if isinstance(function, Mapping) else None
        for candidate in (tool.get("metadata"), nested):
            if isinstance(candidate, Mapping):
                return candidate
        return {}
    metadata = getattr(tool, "metadata", None)
    return metadata if isinstance(metadata, Mapping) else {}


def _output_schema(tool: Any) -> dict[str, Any] | None:
    """The tool's declared ``data`` schema, or ``None`` when it publishes none."""
    raw = _tool_metadata(tool).get(OUTPUT_SCHEMA_METADATA_KEY)
    return dict(raw) if isinstance(raw, Mapping) else None


def _injected_keys(tool: Any) -> frozenset[str]:
    """Names of framework-injected args hidden from the model's own schema."""
    try:
        from langgraph.prebuilt.tool_node import _get_all_injected_args

        return frozenset(_get_all_injected_args(tool).all_injected_keys)
    except Exception:  # noqa: BLE001 - schema introspection is best-effort
        return frozenset()


def _strip_injected(schema: dict[str, Any], injected: frozenset[str]) -> dict[str, Any]:
    """Remove injected parameters from a raw JSON schema.

    ``tool_call_schema`` already hides injected args, but this is a defensive
    second pass for tools whose raw schema still carries ``runtime`` / ``state``.
    """
    if not injected:
        return schema
    cleaned = dict(schema)
    properties = cleaned.get("properties")
    if isinstance(properties, Mapping):
        cleaned["properties"] = {
            key: value for key, value in properties.items() if key not in injected
        }
    required = cleaned.get("required")
    if isinstance(required, list):
        filtered = [item for item in required if item not in injected]
        if filtered:
            cleaned["required"] = filtered
        else:
            cleaned.pop("required", None)
    return cleaned


def _raw_schema(tool: Any) -> tuple[dict[str, Any], bool]:
    """Extract ``(schema, exact)`` for ``tool``'s model-facing arguments.

    ``exact`` is ``True`` only when the schema is the tool's full JSON Schema --
    including ``required`` / ``additionalProperties`` / ``$defs``. A plain-dict
    ``tool_call_schema`` (or ``args_schema``) is respected *verbatim*: calling
    ``model_json_schema`` on a Pydantic model would otherwise be the only path
    and a dict schema would silently fall through to ``.args``, losing
    ``required``. The last-resort ``.args`` map only lists property names, so
    ``exact`` is ``False`` there and the SDK must not imply those are optional.
    """
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            candidate = function.get("parameters") or function.get("input_schema")
        else:
            candidate = tool.get("parameters") or tool.get("input_schema")
        return (dict(candidate), True) if isinstance(candidate, Mapping) else ({}, False)

    schema_obj = getattr(tool, "tool_call_schema", None)
    if isinstance(schema_obj, Mapping):
        # ``tool_call_schema`` may already be a plain dict; keep it exactly.
        return dict(schema_obj), True
    model_json_schema = getattr(schema_obj, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            return dict(model_json_schema()), True
        except Exception:  # noqa: BLE001 - fall through to args_schema / args
            pass

    args_schema = getattr(tool, "args_schema", None)
    if isinstance(args_schema, Mapping):
        return dict(args_schema), True
    args_json_schema = getattr(args_schema, "model_json_schema", None)
    if callable(args_json_schema):
        try:
            return dict(args_json_schema()), True
        except Exception:  # noqa: BLE001 - fall through to the raw args map
            pass

    args = getattr(tool, "args", None)
    if isinstance(args, Mapping):
        return {"type": "object", "properties": dict(args)}, False
    return {}, False


def extract_tool_spec(tool: Any) -> ToolSpec | None:
    """Build a :class:`ToolSpec` from a model-facing tool, or ``None``."""
    name = _tool_name(tool)
    if not name:
        return None
    injected = _injected_keys(tool)
    schema, exact = _raw_schema(tool)
    schema = _strip_injected(schema, injected)
    return ToolSpec(
        name=name,
        description=_tool_description(tool),
        input_schema=schema,
        injected_keys=injected,
        output_schema=_output_schema(tool),
        schema_fidelity=exact,
    )


def collect_tool_specs(tools: Iterable[Any]) -> list[ToolSpec]:
    """Extract specs for ``tools``, de-duplicated by name and name-sorted."""
    specs: dict[str, ToolSpec] = {}
    for tool in tools:
        spec = extract_tool_spec(tool)
        if spec is not None and spec.name not in specs:
            specs[spec.name] = spec
    return [specs[name] for name in sorted(specs)]


def _python_hint(fragment: Any) -> str:
    """Best-effort Python type hint for one JSON-schema fragment."""
    if not isinstance(fragment, Mapping):
        return "Any"
    any_of = fragment.get("anyOf")
    if isinstance(any_of, list):
        hints = [_python_hint(item) for item in any_of]
        unique = list(dict.fromkeys(hints))
        return " | ".join(unique) if unique else "Any"
    json_type = fragment.get("type")
    if isinstance(json_type, list):
        hints = [_JSON_TYPE_TO_PY.get(str(item), "Any") for item in json_type]
        unique = list(dict.fromkeys(hints))
        return " | ".join(unique) if unique else "Any"
    return _JSON_TYPE_TO_PY.get(str(json_type), "Any")


def _function_signature(spec: ToolSpec) -> str:
    """Render ``async def name(*, ...) -> ToolEnvelope`` for a spec.

    Parameters are keyword-only (``*,``) so a required argument may follow an
    optional one, exactly matching the worker's ``tools.<name>(**kwargs)`` call
    convention. The rendered definition is always valid Python.
    """
    properties = spec.input_schema.get("properties")
    required = spec.input_schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()
    params: list[str] = []
    if isinstance(properties, Mapping):
        for key, fragment in properties.items():
            hint = _python_hint(fragment)
            if key in required_names:
                params.append(f"{key}: {hint}")
                continue
            default = fragment.get("default") if isinstance(fragment, Mapping) else None
            if default is None:
                params.append(f"{key}: {hint} | None = None")
            else:
                params.append(f"{key}: {hint} = {default!r}")
    if not params:
        return f"async def {spec.name}() -> ToolEnvelope:"
    return f"async def {spec.name}(*, {', '.join(params)}) -> ToolEnvelope:"


def _has_unrepresentable_params(spec: ToolSpec) -> bool:
    """Whether any argument name cannot be a keyword parameter."""
    properties = spec.input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return False
    return any(not _is_python_identifier(key) for key in properties)


def _bounded_description(spec: ToolSpec, limit: int) -> str:
    text = " ".join(spec.description.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _docstring_safe(text: str) -> str:
    """Escape a string so it survives being embedded in a triple-quoted docstring."""
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _output_hint(spec: ToolSpec) -> str:
    """A one-line hint about the envelope's ``data`` payload."""
    if spec.output_schema:
        return f"data JSON Schema: {_docstring_safe(_compact_json(spec.output_schema))}"
    return "data shape is undeclared; if data is None read content instead"


def _tool_block(spec: ToolSpec, *, description_chars: int) -> str:
    description = _docstring_safe(_bounded_description(spec, description_chars))
    schema_text = _docstring_safe(_compact_json(spec.input_schema)) if spec.input_schema else "{}"
    output = _output_hint(spec)
    if not spec.is_python_name:
        # Exotic name: document it as a comment plus the generic call recipe.
        return (
            f"# {spec.name!r} is not a valid Python identifier; call it with "
            f"tools.call({spec.name!r}, {{...}}).\n"
            f"# {description}\n"
            f"# input schema: {schema_text}\n"
            f"# {output}"
        )
    if not spec.schema_fidelity:
        # Only the argument *names* are known; never render them as optional
        # parameters (that would claim a required argument is optional).
        return (
            f"async def {spec.name}(**kwargs) -> ToolEnvelope:\n"
            f'    """{description}\n'
            f"\n"
            f"    The exact input schema is unknown: this tool publishes only its "
            f"argument names, not which are required, so it is not rendered as a "
            f"typed signature. Pass keyword arguments from this (possibly "
            f"incomplete) schema: {schema_text}\n"
            f"    {output}\n"
            f'    """\n'
            f"    ..."
        )
    if _has_unrepresentable_params(spec):
        # An argument name is not a Python identifier, so expose **kwargs.
        return (
            f"async def {spec.name}(**kwargs) -> ToolEnvelope:\n"
            f'    """{description}\n'
            f"\n"
            f"    Pass keyword arguments matching this schema: {schema_text}\n"
            f"    {output}\n"
            f'    """\n'
            f"    ..."
        )
    return (
        f"{_function_signature(spec)}\n"
        f'    """{description}\n'
        f"\n"
        f"    input schema: {schema_text}\n"
        f"    {output}\n"
        f'    """\n'
        f"    ..."
    )


def _rules_block(*, max_calls: int | None, max_parallel: int | None) -> str:
    policy = "Tool calls are bounded per run"
    if max_calls is not None:
        policy += f" (at most {max_calls} calls)"
    if max_parallel is not None:
        policy += f", with at most {max_parallel} running at once"
    return (
        "Rules:\n"
        "* Only what you `print(...)` and the value you `return` reach the model; "
        "intermediate variables and tool results you do not return are discarded.\n"
        "* Every tool call is a coroutine and **must be awaited**: "
        "`await tools.<name>(...)` or `await tools.call('<name>', {...})`. "
        "A call you do not await never runs.\n"
        "* `tools.<name>(...)` takes keyword arguments matching the tool's schema. "
        "Use `await tools.call('<name>', {...})` for a name or an argument that is "
        "not a valid Python identifier.\n"
        f"* {policy}.\n"
        "* Output is bounded: `print` text and tool results may be truncated. Read "
        "the envelope's `truncated` flag instead of assuming completeness."
    )


def _whitelist_block() -> str:
    """The explicit allowlist plus its reverse prohibition, kept short."""
    return (
        "**Only the tools listed below are callable.** Any other name -- one you "
        "saw elsewhere or that the user's prompt mentions -- is unavailable, and "
        "calling it fails. The script can check itself at runtime: "
        "`tools.available` is a tuple of this run's callable names, for example "
        '`if "find_files" in tools.available:`.'
    )


def _header(*, max_calls: int | None, max_parallel: int | None) -> str:
    return (
        "## Programmatic tool calling\n\n"
        "`run_code(code=..., intent=...)` runs `code` as the **body of an `async` "
        "function** in a fresh local subprocess per call. That subprocess is **not** "
        "a security sandbox: it runs with the same trust as your shell. The injected "
        "names are `tools`, `asyncio`, `json` and `ToolCallError`.\n\n"
        f"{_SELECTION_BLOCK}\n"
        f"{_whitelist_block()}\n\n"
        f"{_rules_block(max_calls=max_calls, max_parallel=max_parallel)}\n\n"
        f"{_ENVELOPE_BLOCK}\n"
    )


def build_sdk_prompt(
    specs: Iterable[ToolSpec],
    *,
    max_bytes: int = DEFAULT_MAX_SDK_BYTES,
    description_chars: int = DEFAULT_DESCRIPTION_CHARS,
    max_calls: int | None = None,
    max_parallel: int | None = None,
) -> str:
    """Render the full SDK document, or raise :class:`SdkBudgetExceeded`."""
    ordered = sorted(specs, key=lambda item: item.name)
    blocks = [_tool_block(spec, description_chars=description_chars) for spec in ordered]
    body = "\n\n".join(blocks) if blocks else "# (no orchestratable tools are registered)"
    document = (
        f"{_header(max_calls=max_calls, max_parallel=max_parallel)}\n```python\n{body}\n```\n"
    )
    size = len(document.encode("utf-8"))
    if size > max_bytes:
        raise SdkBudgetExceeded(size=size, budget=max_bytes)
    return document


def build_compact_sdk_prompt(
    specs: Iterable[ToolSpec],
    *,
    max_bytes: int = COMPACT_MAX_BYTES,
    max_names: int = COMPACT_MAX_NAMES,
    description_chars: int = COMPACT_DESCRIPTION_CHARS,
) -> str:
    """A guaranteed-bounded SDK that lists names but omits per-tool schemas.

    Used only when :func:`build_sdk_prompt` trips the budget. It is *explicit*
    about the omission -- it never silently truncates a schema -- and points the
    model at the native tool definitions, which the middleware keeps visible for
    that request. The total byte size and the number of listed names are both
    bounded.
    """
    ordered = sorted(specs, key=lambda item: item.name)
    head = (
        "## Programmatic tool calling\n\n"
        "The per-tool schema listing was omitted because it exceeded the SDK size "
        "budget. Call tools from `run_code` with `await tools.call(name, args)`; the "
        "exact parameter JSON Schema for each tool is in its native tool definition, "
        "which stays visible in this request.\n\n"
        f"{_SELECTION_BLOCK}\n"
        f"{_ENVELOPE_BLOCK}\n"
        "Registered tools"
    )
    shown = list(ordered[: max(0, max_names)])
    lines = [
        f"* `{spec.name}` -- {_bounded_description(spec, description_chars) or 'no description'}"
        for spec in shown
    ]

    def render() -> str:
        omitted = len(ordered) - len(lines)
        note = (
            f"\n(only the first {len(lines)} of {len(ordered)} tools are listed)"
            if omitted > 0
            else ""
        )
        listing = "\n".join(lines) if lines else "* (no orchestratable tools are registered)"
        return f"{head}:\n{listing}{note}.\n"

    document = render()
    while lines and len(document.encode("utf-8")) > max_bytes:
        lines.pop()
        document = render()
    return document


__all__ = [
    "COMPACT_DESCRIPTION_CHARS",
    "COMPACT_MAX_BYTES",
    "COMPACT_MAX_NAMES",
    "DEFAULT_DESCRIPTION_CHARS",
    "DEFAULT_MAX_SDK_BYTES",
    "OUTPUT_SCHEMA_METADATA_KEY",
    "SdkBudgetExceeded",
    "ToolSpec",
    "build_compact_sdk_prompt",
    "build_sdk_prompt",
    "collect_tool_specs",
    "extract_tool_spec",
]
