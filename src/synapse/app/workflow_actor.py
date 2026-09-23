"""Assemble and run one workflow actor: a role-bound agent with its own session identity.

An actor is deliberately *not* a second agent system.  It reuses the project's role
definitions, tool-policy source, prompt builder, middleware and state schema, and it is
built as a standalone graph with no subagents of its own:

- **One role, one policy.**  The effective tool policy comes from
  :func:`synapse.runtime.subagent_specs.resolve_role_tool_policy`, the same function the
  ``task`` compiler uses, so a role cannot be permissive as an actor and strict as a
  subagent (or the reverse).
- **No nested orchestration.**  ``task`` and the todo tools are always blocked.  A node
  that could start its own invisible sub-orchestration is exactly what a durable workflow
  must not allow, because the run could not record or recover it.
- **Its own checkpoint thread.**  Actors never share the user session's thread, and one
  actor's conversation never leaks into another's.
- **The host validates.**  A declared schema is requested from the model *and* checked
  here, because handing a schema to a provider is a request, not a guarantee.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.runtime.subagent_specs import (
    DEFAULT_INHERIT_TOOL_NAMES,
    SubAgentDefinition,
    build_role_system_prompt,
    resolve_role_tool_policy,
)
from synapse.workflows.contract import CallRequest
from synapse.workflows.errors import ResultValidationError, UnknownActorError
from synapse.workflows.sdk import validate_result
from synapse.workflows.store import WorkflowStore

__all__ = [
    "ACTOR_ALWAYS_BLOCKED",
    "ActorSpec",
    "WorkflowActorExecutor",
    "actor_blocked_tools",
    "actor_thread_id",
    "build_actor_agent",
    "render_call_prompt",
    "resolve_actor",
    "usage_from_state",
]

#: Tools an actor never gets, whatever its role says.
#: ``task`` would let one node start an orchestration the run cannot record or recover;
#: the todo tools belong to the main agent's own planning, not to a workflow node.
ACTOR_ALWAYS_BLOCKED = frozenset({"task", "write_todos", "todo_write", "todos"})

#: How much of one call's input is rendered into the prompt.
MAX_INPUT_CHARS = 20000

#: How many built actor graphs are kept before the oldest is dropped.
DEFAULT_GRAPH_CACHE = 16


def actor_thread_id(run_id: str, actor_key: str) -> str:
    """The checkpoint thread one actor uses inside one run.

    Namespaced so an actor's conversation can never collide with a user session's thread
    or with another actor's, and stable so a resumed run continues the same conversation.
    """
    if not run_id or not actor_key:
        raise UnknownActorError("actor identity requires a run id and an actor key")
    return f"wf:{run_id}:{actor_key}"


@dataclass(frozen=True, slots=True)
class ActorSpec:
    """A role resolved into everything needed to build its graph.

    Resolution happens before any model runs, so an unknown role or an impossible policy
    fails while the call is still just a request.
    """

    role: str
    thread_id: str
    system_prompt: str
    #: The role's declared model, or ``None`` to inherit the project's active model.
    model_spec: str | None
    #: The role's own allowlist as declared (``None`` = inherit, ``()`` = built-ins only).
    declared_tools: tuple[str, ...] | None
    #: Names removed from model requests and blocked at call time.
    blocked: frozenset[str]
    #: Names the role can actually reach.
    available_tools: frozenset[str]
    readonly: bool


def _find_definition(
    role: str, definitions: Sequence[SubAgentDefinition] | None
) -> SubAgentDefinition:
    if definitions is None:
        raise UnknownActorError(
            f"no role definitions were provided to resolve {role!r}"
        )
    for definition in definitions:
        if definition.name == role and definition.enabled:
            return definition
    raise UnknownActorError(f"unknown or disabled agent role {role!r}")


def resolve_actor(
    role: str,
    *,
    run_id: str,
    actor_key: str,
    definitions: Sequence[SubAgentDefinition] | None = None,
    workspace: Path | str | None = None,
    readonly: bool = False,
    inherit_tools: Sequence[Any] | None = None,
    extra_excluded_tools: Sequence[str] = (),
    shell_executable: str | None = None,
) -> ActorSpec:
    """Resolve one role into a buildable actor, or refuse an unknown role.

    ``readonly`` can only ever *narrow* the role's tools: the caller asking for a
    read-only actor cannot grant a role something its definition denies.
    """
    if not isinstance(role, str) or not role.strip():
        raise UnknownActorError("actor role must be a non-empty string")
    definition = _find_definition(role, definitions)
    excluded = set(str(name) for name in (extra_excluded_tools or ()))
    excluded |= ACTOR_ALWAYS_BLOCKED
    if readonly:
        from synapse.runtime.tool_contract import readonly_excluded_tools

        excluded |= set(readonly_excluded_tools())
    policy = resolve_role_tool_policy(
        definition,
        inherit_tools=inherit_tools,
        inherit_names=DEFAULT_INHERIT_TOOL_NAMES,
        extra_excluded_tools=tuple(sorted(excluded)),
    )
    return ActorSpec(
        role=definition.name,
        thread_id=actor_thread_id(run_id, actor_key),
        system_prompt=build_role_system_prompt(
            definition,
            workspace=workspace,
            shell_executable=shell_executable,
            available_tools=policy.available_tools,
        ),
        model_spec=definition.model,
        declared_tools=policy.declared_tools,
        blocked=policy.blocked,
        available_tools=policy.available_tools,
        readonly=bool(readonly),
    )


def render_call_prompt(request: CallRequest) -> str:
    """The user message for one actor call.

    The declared schema travels *in the prompt*.  That is not a stylistic choice: an
    OpenAI-compatible gateway is entitled to reject the tool strategy's ``tool_choice``
    (measured: ``422 invalid_request_error``) and to accept ``response_format`` while
    ignoring it, so the format is requested in words and enforced by the host afterwards.
    """
    parts = [request.prompt]
    if request.input is not None:
        rendered = json.dumps(request.input, ensure_ascii=False, sort_keys=True)
        if len(rendered) > MAX_INPUT_CHARS:
            rendered = rendered[:MAX_INPUT_CHARS] + "...(truncated)"
        parts.append(f"Input (JSON):\n{rendered}")
    if request.schema is not None:
        schema_text = json.dumps(dict(request.schema), ensure_ascii=False, sort_keys=True)
        parts.append(
            "Answer with a single JSON object that matches this JSON Schema exactly. "
            "No prose, no code fences, no explanation:\n" + schema_text
        )
    return "\n\n".join(parts)


#: Runs one actor call against an already-built agent.  Injectable so the execution
#: contract can be tested without a model.
AgentRunner = Callable[[Any, ActorSpec, CallRequest, Mapping[str, Any] | None], Awaitable[Any]]


def actor_blocked_tools(spec: ActorSpec, *, correction: bool = False) -> frozenset[str]:
    """The names hidden from an actor's model requests for this attempt kind.

    The corrective attempt additionally blocks the write and shell tools, so repairing a
    malformed answer cannot itself change the workspace.
    """
    blocked = set(spec.blocked)
    if correction:
        from synapse.runtime.tool_contract import readonly_excluded_tools

        blocked |= set(readonly_excluded_tools())
    return frozenset(blocked)


def build_actor_agent(
    spec: ActorSpec,
    *,
    correction: bool,
    model: Any,
    backend: Any,
    checkpointer: Any = None,
    project_root: Path | str | None = None,
    tools: Sequence[Any] = (),
    permissions: Any = None,
    tool_description_overrides: Any = None,
) -> Any:
    """Build the graph for one actor, schema and attempt kind.

    ``correction`` is the format-fix attempt: the write and shell tools are blocked, so a
    malformed answer can be repaired without the repair itself being able to change
    anything.
    """
    from deepagents import create_deep_agent

    from synapse.app.agent_md import build_agent_md_middleware
    from synapse.app.state_schema import SynapseAgentState
    from synapse.runtime.middleware import (
        build_compact_tool_descriptions,
        build_path_normalize_middleware,
        build_strip_redundant_prompt_blocks,
        build_tool_exclusion_middleware,
    )
    from synapse.runtime.session_header_middleware import build_session_header_middleware

    blocked = actor_blocked_tools(spec, correction=correction)

    middleware: list[Any] = [
        build_session_header_middleware(),
    ]
    if project_root is not None:
        middleware.append(build_agent_md_middleware(Path(project_root)))
        middleware.append(build_path_normalize_middleware(Path(project_root)))
    middleware.extend(
        [
            build_strip_redundant_prompt_blocks(),
            build_tool_exclusion_middleware(frozenset(blocked)),
            build_compact_tool_descriptions(),
        ]
    )
    return create_deep_agent(
        model=model,
        state_schema=SynapseAgentState,
        system_prompt=spec.system_prompt,
        backend=backend,
        tools=list(tools),
        middleware=middleware,
        permissions=permissions,
        # No subagents of its own: an actor is a worker, not an orchestrator.
        subagents=None,
        # No provider-side structured output: see ``render_call_prompt``.  The declared
        # schema is requested in the prompt and enforced by the host, which is the only
        # guarantee that holds across providers.
        response_format=None,
        checkpointer=checkpointer,
        name=f"workflow-actor:{spec.role}",
    )


async def _default_agent_runner(
    agent: Any, spec: ActorSpec, request: CallRequest, _schema: Mapping[str, Any] | None
) -> Any:
    """Run one turn on an actor graph and return its state."""
    return await agent.ainvoke(
        {"messages": [{"role": "user", "content": render_call_prompt(request)}]},
        {"configurable": {"thread_id": spec.thread_id}},
    )


async def _message_count(agent: Any, thread_id: str) -> int | None:
    """How many messages one actor thread already holds, or ``None`` if unreadable.

    Read before the call so the usage delta can be exact.  An unreadable thread yields
    ``None``, which makes the caller skip accounting instead of overcharging the run.
    """
    reader = getattr(agent, "aget_state", None)
    if not callable(reader):
        return None
    try:
        snapshot = await reader({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - accounting is best effort, the call is not
        return None
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping):
        # A thread that has run nothing yet: nothing to subtract, so the whole answer is
        # this call's usage.  Returning ``None`` here silently uncharged every *first* call
        # of every actor, which is the most expensive one (the system prompt).
        return 0
    messages = values.get("messages")
    if not isinstance(messages, Sequence):
        return 0
    return len(messages)


@dataclass(slots=True)
class WorkflowActorExecutor:
    """The host-side call handler: resolve a role, run its actor, enforce the contract."""

    run_id: str
    definitions: Sequence[SubAgentDefinition] | None = None
    workspace: Path | str | None = None
    inherit_tools: Sequence[Any] | None = None
    extra_excluded_tools: Sequence[str] = ()
    shell_executable: str | None = None
    #: Builds a graph for (spec, correction).  Injected by the daemon with real
    #: model/backend/checkpointer resources; a test injects a stub.  The declared schema is
    #: not an input: it travels in the prompt, so one actor graph serves every schema.
    agent_builder: Callable[[ActorSpec, bool], Any] | None = None
    #: Runs one turn on a built agent.
    agent_runner: AgentRunner = _default_agent_runner
    max_graphs: int = DEFAULT_GRAPH_CACHE
    #: Where per-call usage is recorded.  ``None`` disables accounting rather than
    #: silently inventing numbers.
    store: WorkflowStore | None = None
    _specs: dict[tuple[str, bool, str], ActorSpec] = field(default_factory=dict)
    _graphs: dict[tuple[str, str, bool], Any] = field(default_factory=dict)

    async def __call__(self, request: CallRequest, correction: bool) -> Any:
        """Answer one workflow call: this is the coordinator's ``on_call`` seam."""
        spec = self.spec_for(request)
        agent = self._agent_for(spec, request, correction)
        # The thread's message count before this call is what makes the usage delta exact;
        # without it the call's tokens would be unknown and are simply not recorded.
        before = await _message_count(agent, spec.thread_id)
        state = await self.agent_runner(agent, spec, request, request.schema)
        value = extract_result(state, request.schema)
        # The schema was requested from the model; this is the check that makes it real.
        validate_result(value, request.schema)
        self._record_usage(request, state, before)
        return value

    def _record_usage(self, request: CallRequest, state: Any, before: int | None) -> None:
        if self.store is None or before is None:
            return
        usage = usage_from_state(state, since=before)
        if usage is None:
            return
        try:
            self.store.record_call_usage(
                self.run_id,
                request.call_key,
                input_tokens=usage[0],
                output_tokens=usage[1],
            )
        except Exception:  # noqa: BLE001 - accounting must not fail the call itself
            return

    def spec_for(self, request: CallRequest) -> ActorSpec:
        """Resolve (and cache) the actor for one call."""
        key = (request.role, bool(request.readonly), request.actor_key)
        spec = self._specs.get(key)
        if spec is None:
            spec = resolve_actor(
                request.role,
                run_id=self.run_id,
                actor_key=request.actor_key,
                definitions=self.definitions,
                workspace=self.workspace,
                readonly=request.readonly,
                inherit_tools=self.inherit_tools,
                extra_excluded_tools=self.extra_excluded_tools,
                shell_executable=self.shell_executable,
            )
            self._specs[key] = spec
        return spec

    def _agent_for(self, spec: ActorSpec, request: CallRequest, correction: bool) -> Any:
        if self.agent_builder is None:
            raise UnknownActorError(
                "no actor agent builder is attached to this executor"
            )
        key = (spec.role, bool(correction))
        agent = self._graphs.get(key)
        if agent is None:
            agent = self.agent_builder(spec, correction)
            if len(self._graphs) >= max(1, self.max_graphs):
                self._graphs.pop(next(iter(self._graphs)))
            self._graphs[key] = agent
        return agent

    @property
    def cached_graphs(self) -> int:
        """How many actor graphs are currently built (diagnostics)."""
        return len(self._graphs)


def extract_result(state: Any, schema: Mapping[str, Any] | None) -> Any:
    """Read one actor's answer out of its final state.

    A call with a schema must produce a structured response; a missing one is reported as a
    validation failure so the SDK's single corrective attempt can run, rather than being
    silently treated as an empty answer.
    """
    from synapse.runtime.streaming.stream_events import extract_last_ai_text

    if not isinstance(state, Mapping):
        raise ResultValidationError("actor returned no state to read a result from")
    if schema is None:
        return extract_last_ai_text(dict(state))
    structured = state.get("structured_response")
    if structured is not None:
        return structured
    # No provider-side structured output: the answer is the model's text, and it has to be
    # read as JSON before the host can check it against the schema.
    text = extract_last_ai_text(dict(state))
    if not text.strip():
        raise ResultValidationError("actor returned no answer to read a result from")
    return _json_from_text(text)


def _json_from_text(text: str) -> Any:
    """Parse the JSON value an actor was asked for out of its answer text.

    Tolerant about the two habits models have — a code fence, and a sentence around the
    object — and strict about the rest: when no JSON value can be read, the call fails and
    the SDK's single corrective attempt runs.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    candidates = [cleaned.strip()]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ResultValidationError(
        "actor did not answer with JSON for a call that declared a schema"
    )


def usage_from_state(state: Any, *, since: int) -> tuple[int, int] | None:
    """Tokens the messages produced *from index ``since``* report, or ``None`` if unknown.

    An actor thread accumulates messages, and each ``AIMessage`` carries the usage of the
    model call that produced it, so counting the whole thread would charge every earlier
    call again.  ``since`` is the thread's message count before this call started; when the
    caller could not read it, this returns ``None`` and nothing is recorded rather than a
    number that would overcharge the run.
    """
    if not isinstance(state, Mapping):
        return None
    messages = state.get("messages")
    if not isinstance(messages, Sequence):
        return None
    input_tokens = 0
    output_tokens = 0
    seen = False
    for message in list(messages)[max(0, int(since)) :]:
        usage = getattr(message, "usage_metadata", None)
        if not isinstance(usage, Mapping):
            continue
        seen = True
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
    return (input_tokens, output_tokens) if seen else None
