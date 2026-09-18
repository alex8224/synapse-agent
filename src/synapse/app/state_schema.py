"""LangGraph state schema for the coding agent.

DeepAgents defines ``DeepAgentState`` with a ``DeltaChannel`` on ``messages`` so the
message list is stored as deltas instead of a full copy per checkpoint. It tunes that
channel's ``snapshot_frequency`` to 50 -- a full ``_DeltaSnapshot`` every ~50 message
updates, measured on a real store at roughly one full snapshot per conversation turn.

``snapshot_frequency`` is a direct disk/latency trade-off. Reconstructing a thread
replays ``writes`` back to the nearest ancestor snapshot, so the value bounds both the
replay depth and the snapshot volume: total snapshot bytes grow as ``O(M**2 /
frequency)`` for ``M`` messages. LangGraph's own default is 1000; DeepAgents chose 50,
which favours resume latency over disk.

``SNAPSHOT_FREQUENCY`` below raises it to 500. Measured effect on a 2,200-message
session: +0-55 ms to load, against roughly a 10x reduction in snapshot bytes. The
hard floor is unchanged either way -- every thread keeps its newest snapshot, because
``writes`` alone is not a faithful change log for threads whose context was
summarised (``Overwrite``).

The channel is rebuilt from the base class annotation rather than importing
DeepAgents' private ``deepagents._messages_reducer``, so the exact same reducer object
is reused and only the cadence differs.
"""

from __future__ import annotations

import typing
from typing import Annotated, Required

from deepagents.graph import DeepAgentState
from langchain_core.messages import AnyMessage
from langgraph.channels.delta import DeltaChannel

#: Full-snapshot cadence for the ``messages`` channel, in message updates.
SNAPSHOT_FREQUENCY = 500


def _base_messages_channel() -> DeltaChannel:
    """Return the base schema's ``messages`` channel so its reducer is reused verbatim."""
    hints = typing.get_type_hints(DeepAgentState, include_extras=True)
    annotated = typing.get_args(hints["messages"])[0]
    for meta in typing.get_args(annotated):
        if isinstance(meta, DeltaChannel):
            return meta
    raise RuntimeError("DeepAgentState.messages is no longer a DeltaChannel")


class SynapseAgentState(DeepAgentState):
    """``DeepAgentState`` with a cheaper snapshot cadence on the ``messages`` channel.

    Subagents must inherit this schema too: DeepAgents only defaults ``state_schema``
    for the top-level graph, so without an explicit schema every subagent step rewrites
    its full message list into its own checkpoint namespace.
    """

    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(
                _base_messages_channel().reducer,
                snapshot_frequency=SNAPSHOT_FREQUENCY,
            ),
        ]
    ]
