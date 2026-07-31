"""Session usage restore + aggregate helpers."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.stream import aggregate_usage_from_messages


def test_aggregate_usage_sums_and_keeps_last() -> None:
    m1 = SimpleNamespace(
        type="ai",
        id="a1",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
        },
    )
    m2 = SimpleNamespace(
        type="ai",
        id="a2",
        usage_metadata={
            "input_tokens": 250,
            "output_tokens": 20,
            "total_tokens": 270,
        },
    )
    human = SimpleNamespace(type="human", id="h1", content="hi")
    agg = aggregate_usage_from_messages([human, m1, m2])
    assert agg["input_tokens"] == 350
    assert agg["output_tokens"] == 30
    assert agg["last_input_tokens"] == 250
    assert agg["last_output_tokens"] == 20


def test_aggregate_usage_empty() -> None:
    assert aggregate_usage_from_messages([])["input_tokens"] == 0
    assert aggregate_usage_from_messages(None)["last_input_tokens"] == 0
