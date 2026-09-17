"""Contract for the coding agent's tuned LangGraph state schema.

``SynapseAgentState`` must stay a drop-in replacement for DeepAgents'
``DeepAgentState``: same channels, same ``messages`` reducer, only a cheaper snapshot
cadence. These assertions are what makes the snapshot cadence safe to tune, so they
are intentionally coupled to the installed DeepAgents/LangGraph versions.
"""

from __future__ import annotations

from deepagents.graph import DeepAgentState
from langgraph.channels.delta import DeltaChannel
from langgraph.graph.state import _get_channels

from synapse.app.state_schema import SNAPSHOT_FREQUENCY, SynapseAgentState


def _channels(schema: type) -> dict:
    return _get_channels(schema)[0]


def test_snapshot_frequency_is_tuned_above_upstream_default() -> None:
    base = _channels(DeepAgentState)["messages"]
    tuned = _channels(SynapseAgentState)["messages"]

    assert isinstance(base, DeltaChannel)
    assert isinstance(tuned, DeltaChannel)
    # Upstream hardcodes 50; anything at or below it makes the tuning a no-op.
    assert base.snapshot_frequency == 50
    assert SNAPSHOT_FREQUENCY == 500
    assert tuned.snapshot_frequency == SNAPSHOT_FREQUENCY


def test_messages_reducer_is_reused_verbatim() -> None:
    """The cadence is the only difference; the reducer must be the same object."""
    base = _channels(DeepAgentState)["messages"]
    tuned = _channels(SynapseAgentState)["messages"]

    assert tuned.reducer is base.reducer


def test_schema_exposes_the_same_channels_as_upstream() -> None:
    assert sorted(_channels(SynapseAgentState)) == sorted(_channels(DeepAgentState))


def test_channel_equality_tracks_the_cadence() -> None:
    """``DeltaChannel.__eq__`` compares the cadence, so the two channels differ."""
    assert _channels(SynapseAgentState)["messages"] != _channels(DeepAgentState)["messages"]
