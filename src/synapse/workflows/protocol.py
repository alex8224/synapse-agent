"""Line-delimited JSON messages between a workflow worker and its parent.

The worker runs the generated script and the orchestration checkpoint, but it does *not*
run agents: every agent call is a message to the parent, which executes it through the
existing Agent Runtime and replies.  That is what keeps credentials, tool policy and
approvals in the daemon, and it is why the wire shapes live in their own module that
both sides import.

One message per line, UTF-8, no embedded newlines.  Payloads must be JSON-safe, so a
value that cannot be sent is reported instead of silently coerced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from synapse.workflows.contract import CallRequest, WorkflowLimits

__all__ = [
    "KIND_APPROVAL",
    "KIND_APPROVAL_RESULT",
    "KIND_CALL",
    "KIND_CALL_ERROR",
    "KIND_CALL_RESULT",
    "KIND_ERROR",
    "KIND_RESULT",
    "KIND_START",
    "MAX_LINE_BYTES",
    "ProtocolError",
    "WorkerConfig",
    "call_request_from_payload",
    "call_request_payload",
    "checkpoint_path_for",
    "decode",
    "encode",
]

KIND_START = "start"
KIND_CALL = "call"
KIND_CALL_RESULT = "call_result"
KIND_CALL_ERROR = "call_error"
KIND_APPROVAL = "approval"
KIND_APPROVAL_RESULT = "approval_result"
KIND_RESULT = "result"
KIND_ERROR = "error"

#: One line may not exceed this, so a runaway result cannot exhaust the parent's memory.
MAX_LINE_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    """A message that cannot be encoded or decoded."""


def checkpoint_path_for(db_path: str) -> str:
    """The orchestration checkpoint file that belongs to one workflow database.

    Deliberately a **separate** SQLite file, measured rather than assumed: the LangGraph
    checkpointer keeps its own write transaction while a task runs, so sharing one file
    with the workflow records makes the two writers block each other — the store's write
    waits out its busy timeout and then fails with ``database is locked``.  This mirrors
    the project's own layout, where agent checkpoints live in their own file too.
    """
    if not isinstance(db_path, str) or not db_path.strip():
        raise ProtocolError("db_path must be a non-empty string")
    return f"{db_path}.checkpoint"


def encode(message: Mapping[str, Any]) -> str:
    """Encode one message as a single line, refusing a value that would break framing."""
    try:
        text = json.dumps(dict(message), ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        kind = message.get("type", "?")
        raise ProtocolError(
            f"message {kind!r} of type {type(message).__name__!r} is not JSON-serializable"
        ) from exc
    if "\n" in text or "\r" in text:
        raise ProtocolError("encoded message contains a line break")
    if len(text.encode("utf-8")) > MAX_LINE_BYTES:
        raise ProtocolError("encoded message exceeds the maximum line size")
    return text


def decode(line: str) -> dict[str, Any]:
    """Decode one line into a message mapping."""
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ProtocolError("received line exceeds the maximum line size")
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise ProtocolError("received line is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("received message must be a JSON object")
    kind = payload.get("type")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("received message has no type")
    return payload


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Everything one worker invocation needs; sent as the first stdin line.

    ``known_roles`` is the enabled role registry snapshot taken when the run started, so
    the worker can refuse an unknown role before asking the parent to execute anything.
    """

    run_id: str
    thread_id: str
    db_path: str
    script: str
    inputs: Any = None
    limits: WorkflowLimits = field(default_factory=WorkflowLimits)
    known_roles: tuple[str, ...] = ()
    workspace: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "thread_id", "db_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ProtocolError(f"{name} must be a non-empty string")
        if not isinstance(self.script, str) or not self.script.strip():
            raise ProtocolError("script must be a non-empty string")
        if not isinstance(self.known_roles, tuple):
            object.__setattr__(self, "known_roles", tuple(self.known_roles))

    def to_message(self) -> dict[str, Any]:
        return {
            "type": KIND_START,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "db_path": self.db_path,
            "script": self.script,
            "inputs": self.inputs,
            "limits": self.limits.to_json(),
            "known_roles": list(self.known_roles),
            "workspace": self.workspace,
        }

    @classmethod
    def from_message(cls, message: Mapping[str, Any]) -> WorkerConfig:
        if message.get("type") != KIND_START:
            raise ProtocolError("first message must be a start message")
        roles = message.get("known_roles") or ()
        return cls(
            run_id=str(message.get("run_id", "")),
            thread_id=str(message.get("thread_id", "")),
            db_path=str(message.get("db_path", "")),
            script=str(message.get("script", "")),
            inputs=message.get("inputs"),
            limits=WorkflowLimits.from_json(message.get("limits")),
            known_roles=tuple(str(role) for role in roles),
            workspace=(
                None if message.get("workspace") is None else str(message["workspace"])
            ),
        )


def call_request_payload(request: CallRequest) -> dict[str, Any]:
    """A JSON-safe view of one call, used both on the wire and inside a LangGraph task.

    The LangGraph task deliberately takes this mapping instead of the dataclass: a
    checkpoint stores the payload, and a plain dict is what the framework's serializer is
    built to round-trip.
    """
    return {
        "role": request.role,
        "actor_key": request.actor_key,
        "call_key": request.call_key,
        "prompt": request.prompt,
        "input": request.input,
        "schema": None if request.schema is None else dict(request.schema),
        "readonly": request.readonly,
    }


def call_request_from_payload(payload: Mapping[str, Any]) -> CallRequest:
    """Rebuild a :class:`CallRequest` from its JSON-safe view."""
    return CallRequest(
        role=str(payload.get("role", "")),
        actor_key=str(payload.get("actor_key", "")),
        call_key=str(payload.get("call_key", "")),
        prompt=str(payload.get("prompt", "")),
        input=payload.get("input"),
        schema=payload.get("schema"),
        readonly=bool(payload.get("readonly", False)),
    )
