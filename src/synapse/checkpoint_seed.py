"""Fail-closed terminal checkpoint seeding for projected Codex text.

This module is deliberately narrow. It writes an already validated visible-text
snapshot into a fresh Synapse thread, then seals the graph at ``END`` so the
thread has no pending tasks. It does not resume Codex runtime state, perform
source discovery, create session metadata, or implement import idempotency.

The implementation relies only on public LangGraph graph APIs, but its
``model`` node behavior is compatibility-gated to the installed DeepAgents and
LangGraph versions. Upgrade those dependencies only together with the contract
tests in ``tests/test_checkpoint_seed.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

from synapse.codex_history import PARSER_VERSION, PROJECTION_KIND

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage

    from synapse.codex_history import CodexTextSnapshot

SUPPORTED_LANGGRAPH_VERSION = "1.2.9"
SUPPORTED_DEEPAGENTS_VERSION = "0.6.12"


class CheckpointSeedError(RuntimeError):
    """The requested checkpoint seed was rejected or could not be verified."""


@dataclass(frozen=True)
class CheckpointSeedResult:
    """Verified terminal checkpoint location for a newly seeded thread."""

    thread_id: str
    config: dict[str, Any]
    message_count: int


class CheckpointSeeder:
    """Seed a validated visible-text snapshot into a fresh, terminal thread.

    The seed operation has one intentionally narrow compatibility contract:

    - LangGraph 1.2.9 and DeepAgents 0.6.12 only.
    - A root-namespace graph with a ``model`` node and a ``messages`` channel.
    - An empty target thread only; a future import ledger owns idempotency.
    - User/assistant text only, represented as deterministic message IDs.

    Any failed write is compensated with ``delete_thread(thread_id)`` after the
    target was proven empty before seeding. A cleanup failure is surfaced rather
    than silently claiming a safe result.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def seed_snapshot(
        self,
        thread_id: str,
        snapshot: CodexTextSnapshot,
    ) -> CheckpointSeedResult:
        """Write one safe Codex snapshot and verify that it is terminal."""
        messages = _messages_from_snapshot(snapshot)
        return self.seed_messages(thread_id, messages)

    def verify_snapshot(
        self,
        thread_id: str,
        snapshot: CodexTextSnapshot,
    ) -> CheckpointSeedResult:
        """Verify that a thread exactly contains a terminal snapshot seed."""
        normalized_thread_id = _validate_thread_id(thread_id)
        expected = _messages_from_snapshot(snapshot)
        self._validate_compatibility()
        saver = self._checkpointer()
        config = {"configurable": {"thread_id": normalized_thread_id, "checkpoint_ns": ""}}
        self._verify_terminal(config, expected, saver)
        state = self._agent.get_state(config)
        return CheckpointSeedResult(
            thread_id=normalized_thread_id,
            config=_copy_config(state.config),
            message_count=len(expected),
        )

    def has_thread(self, thread_id: str) -> bool:
        """Return whether the root checkpoint namespace has any saved state."""
        normalized_thread_id = _validate_thread_id(thread_id)
        config = {"configurable": {"thread_id": normalized_thread_id, "checkpoint_ns": ""}}
        return self._checkpointer().get_tuple(config) is not None

    def delete_thread(self, thread_id: str) -> None:
        """Delete a thread only for a failed import compensation path."""
        self._checkpointer().delete_thread(_validate_thread_id(thread_id))

    def seed_messages(
        self,
        thread_id: str,
        messages: Sequence[HumanMessage | AIMessage],
    ) -> CheckpointSeedResult:
        """Write explicit safe messages into a new terminal graph thread.

        This lower-level entry point is retained for future non-Codex importers,
        but it accepts only plain ``HumanMessage`` and ``AIMessage`` values with
        stable unique IDs. It rejects tool calls and all metadata-bearing input.
        """
        normalized_thread_id = _validate_thread_id(thread_id)
        expected = _validate_messages(messages)
        self._validate_compatibility()
        saver = self._checkpointer()
        config = {"configurable": {"thread_id": normalized_thread_id, "checkpoint_ns": ""}}
        if saver.get_tuple(config) is not None:
            raise CheckpointSeedError("target thread already has a checkpoint")

        attempted_write = False
        try:
            attempted_write = True
            seeded_config = self._agent.update_state(
                config,
                {"messages": expected},
                as_node="model",
            )
            terminal_config = self._agent.update_state(seeded_config, None, as_node=END)
            self._verify_terminal(terminal_config, expected, saver)
        except Exception as exc:
            if attempted_write:
                self._compensate(saver, normalized_thread_id, exc)
            if isinstance(exc, CheckpointSeedError):
                raise
            raise CheckpointSeedError("checkpoint seed failed") from exc

        return CheckpointSeedResult(
            thread_id=normalized_thread_id,
            config=_copy_config(terminal_config),
            message_count=len(expected),
        )

    def _validate_compatibility(self) -> None:
        installed = _framework_versions()
        if installed != (SUPPORTED_LANGGRAPH_VERSION, SUPPORTED_DEEPAGENTS_VERSION):
            raise CheckpointSeedError(
                "checkpoint seeding is unsupported for installed framework versions"
            )
        nodes = getattr(self._agent, "nodes", None)
        channels = getattr(self._agent, "channels", None)
        if not isinstance(nodes, dict) or "model" not in nodes:
            raise CheckpointSeedError("checkpoint seeding requires a graph model node")
        if not isinstance(channels, dict) or "messages" not in channels:
            raise CheckpointSeedError("checkpoint seeding requires a messages channel")
        for name in ("update_state", "get_state"):
            if not callable(getattr(self._agent, name, None)):
                raise CheckpointSeedError(f"checkpoint seeding requires agent.{name}")

    def _checkpointer(self) -> Any:
        saver = getattr(self._agent, "_coding_checkpointer", None)
        if saver is None:
            saver = getattr(self._agent, "checkpointer", None)
        if not callable(getattr(saver, "get_tuple", None)):
            raise CheckpointSeedError("checkpoint seeding requires a readable checkpointer")
        if not callable(getattr(saver, "delete_thread", None)):
            raise CheckpointSeedError("checkpoint seeding requires checkpoint compensation")
        return saver

    def _verify_terminal(
        self,
        config: dict[str, Any],
        expected: Sequence[HumanMessage | AIMessage],
        saver: Any,
    ) -> None:
        state = self._agent.get_state(config)
        if tuple(getattr(state, "next", ()) or ()):
            raise CheckpointSeedError("seeded checkpoint still has pending graph tasks")
        if tuple(getattr(state, "interrupts", ()) or ()):
            raise CheckpointSeedError("seeded checkpoint has pending interrupts")
        actual = list((getattr(state, "values", None) or {}).get("messages") or [])
        if not _messages_match(actual, expected):
            raise CheckpointSeedError("seeded checkpoint messages did not round-trip exactly")
        checkpoint_tuple = saver.get_tuple(config)
        if checkpoint_tuple is None:
            raise CheckpointSeedError("seeded checkpoint could not be read back")
        if getattr(checkpoint_tuple, "pending_writes", ()):
            raise CheckpointSeedError("seeded checkpoint still has pending writes")

    @staticmethod
    def _compensate(saver: Any, thread_id: str, cause: Exception) -> None:
        try:
            saver.delete_thread(thread_id)
        except Exception as cleanup_error:  # noqa: BLE001
            raise CheckpointSeedError(
                "checkpoint seed failed and cleanup was unsuccessful"
            ) from cleanup_error


def _framework_versions() -> tuple[str | None, str | None]:
    try:
        return version("langgraph"), version("deepagents")
    except PackageNotFoundError:
        return None, None


def _validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str):
        raise CheckpointSeedError("thread_id must be a string")
    value = thread_id.strip()
    if not value or len(value) > 120 or "\x00" in value:
        raise CheckpointSeedError("thread_id is invalid")
    return value


def _messages_from_snapshot(snapshot: CodexTextSnapshot) -> list[HumanMessage | AIMessage]:
    if not snapshot.importable:
        raise CheckpointSeedError("snapshot is not safe to seed")
    if (
        snapshot.projection_kind != PROJECTION_KIND
        or snapshot.parser_version != PARSER_VERSION
    ):
        raise CheckpointSeedError("snapshot projection contract is unsupported")

    messages: list[HumanMessage | AIMessage] = []
    for source in snapshot.messages:
        if source.role == "user":
            messages.append(HumanMessage(content=source.text, id=source.source_id))
        elif source.role == "assistant":
            messages.append(AIMessage(content=source.text, id=source.source_id))
        else:
            raise CheckpointSeedError("snapshot contains an unsupported message role")
    return _validate_messages(messages)


def _validate_messages(
    messages: Sequence[HumanMessage | AIMessage],
) -> list[HumanMessage | AIMessage]:
    if not messages:
        raise CheckpointSeedError("checkpoint seed requires at least one message")
    normalized: list[HumanMessage | AIMessage] = []
    ids: set[str] = set()
    for message in messages:
        if type(message) not in {HumanMessage, AIMessage}:
            raise CheckpointSeedError("checkpoint seed accepts only user and assistant messages")
        if not isinstance(message.id, str) or not message.id or message.id in ids:
            raise CheckpointSeedError("checkpoint seed messages need unique stable IDs")
        if not isinstance(message.content, str) or not message.content.strip():
            raise CheckpointSeedError("checkpoint seed messages need nonempty text content")
        if message.additional_kwargs or message.response_metadata:
            raise CheckpointSeedError("checkpoint seed messages cannot carry metadata")
        if isinstance(message, AIMessage) and (
            message.tool_calls or message.invalid_tool_calls or message.usage_metadata
        ):
            raise CheckpointSeedError("checkpoint seed assistant messages cannot carry tool state")
        ids.add(message.id)
        normalized.append(message)
    return normalized


def _messages_match(actual: Sequence[BaseMessage], expected: Sequence[BaseMessage]) -> bool:
    if len(actual) != len(expected):
        return False
    return all(
        type(found) is type(wanted)
        and found.id == wanted.id
        and found.content == wanted.content
        and not found.additional_kwargs
        and not found.response_metadata
        and (not isinstance(found, AIMessage) or not found.tool_calls)
        for found, wanted in zip(actual, expected, strict=True)
    )


def _copy_config(config: dict[str, Any]) -> dict[str, Any]:
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        raise CheckpointSeedError("checkpoint seeding returned an invalid graph configuration")
    return {"configurable": dict(configurable)}
