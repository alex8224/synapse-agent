"""Lightweight conversation fork: project a parent thread's history into a
new thread as plain user/assistant text, with tool calls reduced to an inline
summary line.

Design (tier-2 "lightweight fork"):

- Read the parent thread's ``messages`` from its LangGraph checkpointer.
- Cut on user-turn boundaries and keep every completed turn before the fork
  point.
- Project each turn to alternating ``HumanMessage`` / ``AIMessage`` text:
  - user text and assistant text are copied verbatim;
  - assistant ``tool_calls`` become a compact ``[Tool calls]`` summary block
    (name + args only, never tool output);
  - ``ToolMessage`` results and reasoning/thinking are dropped.
- Seed the projected messages into a fresh thread via
  :meth:`synapse.integrations.checkpoint_seed.CheckpointSeeder.seed_messages`,
  which validates the text-only contract and seals the graph at ``END``.
- Record lineage on the child session row (``forked_from_*``).

The projection is a pure function so it can be unit tested without a
checkpointer. Orchestration reuses the existing seeder rather than copying raw
messages, which keeps tool-call/tool-result pairing out of scope entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from synapse.sessions.transcript import _message_content, _message_role


@dataclass(frozen=True, slots=True)
class ForkOrigin:
    """Immutable lineage for a forked thread."""

    parent_thread_id: str
    boundary: str
    turn_id: str | None = None

    def as_columns(self) -> dict[str, str | None]:
        return {
            "forked_from_thread_id": self.parent_thread_id,
            "forked_from_boundary": self.boundary,
            "forked_from_turn_id": self.turn_id,
        }


@dataclass(frozen=True, slots=True)
class ForkResult:
    parent_thread_id: str
    child_thread_id: str
    message_count: int
    origin: ForkOrigin


def _assistant_text(msg: Any) -> str:
    """The assistant's visible text only.

    Tool calls and their outputs are deliberately NOT carried into the fork:
    inlining them as text turns the parent's tool JSON into the model's own
    prior "speech", which the model then imitates and misbehaves around.  A turn
    that called tools but produced no visible text projects to nothing.
    """
    return _message_content(msg).strip()


def _text_of(msg: Any) -> str:
    """User text (never includes tool payloads)."""
    return _message_content(msg).strip()


def project_messages(messages: Sequence[Any]) -> list[HumanMessage | AIMessage]:
    """Project parent checkpoint messages into plain text fork messages.

    Keeps human/assistant visible text and nothing else: tool calls, tool
    results, reasoning and every message type the seeder does not accept are all
    dropped.  A turn that only called tools (no visible text) contributes no
    message, so the child is a clean user/assistant transcript with no tool
    traces to imitate.

    The returned messages carry deterministic, unique ids so the seed round-trip
    is stable, and no metadata (the seeder rejects it).
    """
    projected: list[HumanMessage | AIMessage] = []
    seq = 0
    for msg in messages:
        role = _message_role(msg)
        if role in {"human", "user"}:
            text = _text_of(msg)
            if not text:
                continue
            projected.append(HumanMessage(content=text, id=f"fork-{seq}"))
            seq += 1
            continue
        if role in {"ai", "assistant"}:
            text = _assistant_text(msg)
            if not text:
                continue
            projected.append(AIMessage(content=text, id=f"fork-{seq}"))
            seq += 1
            continue
        # tool / system / unknown: dropped (tool output is the size hog).
    return projected


def split_at_turn(messages: Sequence[Any], turn_id: str | None) -> list[Any]:
    """Return the message prefix up to (but not including) ``turn_id``.

    A turn starts at a human/user message. ``turn_id`` may be the index
    (as a string) of that message among the projected turns. When ``turn_id``
    is ``None`` the full history is returned (boundary ``latest``).
    """
    if turn_id is None:
        return list(messages)
    try:
        target = int(turn_id)
    except (TypeError, ValueError):
        return list(messages)
    if target <= 0:
        return []
    starts: list[int] = []
    for index, msg in enumerate(messages):
        if _message_role(msg) in {"human", "user"}:
            starts.append(index)
    if len(starts) <= target:
        return list(messages)
    return list(messages[: starts[target]])


def fork_thin(
    *,
    parent_thread_id: str,
    child_thread_id: str,
    messages: Sequence[Any],
    seeder: Any,
    boundary: str = "latest",
    turn_id: str | None = None,
) -> ForkResult:
    """Project + seed a lightweight fork; return the result descriptor.

    ``messages`` are the parent's raw checkpoint messages (as read by
    ``load_messages_from_checkpointer``). ``seeder`` is a
    :class:`CheckpointSeeder` instance. Raises ``CheckpointSeedError`` from the
    seeder when the projection cannot be persisted.
    """
    prefix = split_at_turn(messages, turn_id if boundary == "through_turn" else None)
    projected = project_messages(prefix)
    if not projected:
        from synapse.integrations.checkpoint_seed import CheckpointSeedError

        raise CheckpointSeedError("fork produced no forkable messages")
    seeder.seed_messages(child_thread_id, projected)
    return ForkResult(
        parent_thread_id=parent_thread_id,
        child_thread_id=child_thread_id,
        message_count=len(projected),
        origin=ForkOrigin(
            parent_thread_id=parent_thread_id,
            boundary=boundary,
            turn_id=turn_id,
        ),
    )


def rebuild_fork_projection(settings: Any, agent: Any, child_thread_id: str) -> None:
    """Build a forked child's transcript projection from its new checkpoint.

    The history read path is projection-only: without a projection row the child
    renders as an empty conversation even though its checkpoint (what the model
    actually reads) holds the inherited history.  Called by every fork entry
    point (runtime service and TUI) so a freshly forked session shows its
    inherited messages immediately.

    Best-effort by design: a settings object without a resolvable session path
    skips the projection, and any failure is swallowed because the projection is
    rebuildable from the checkpoint (a fork must not fail over a derived store).
    """
    try:
        resolve = getattr(settings, "resolved_sessions_path", None)
        if not callable(resolve):
            return
        projection_path = Path(resolve()).parent / "transcript.sqlite"
        from synapse.sessions.transcript import (
            latest_checkpoint_id_from_sqlite_file,
            load_messages_from_agent,
        )
        from synapse.sessions.transcript_projection import TranscriptProjection

        checkpoint_path = getattr(settings, "checkpoint_path", None)
        messages = load_messages_from_agent(agent, child_thread_id)
        snapshot_id = (
            latest_checkpoint_id_from_sqlite_file(checkpoint_path, child_thread_id)
            if checkpoint_path
            else None
        )
        projection = TranscriptProjection(projection_path)
        try:
            projection.replace_from_messages(
                child_thread_id,
                messages,
                source_checkpoint_id=snapshot_id,
            )
        finally:
            projection.close()
    except Exception:  # noqa: BLE001 - a fork must not fail on projection rebuild
        return
