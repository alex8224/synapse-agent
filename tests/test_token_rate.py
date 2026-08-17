from __future__ import annotations

from synapse.runtime.token_rate import TokenRateBasis, TokenRateTracker
from synapse.ui.formatters import format_token_rate


def test_token_rate_final_uses_decode_duration_and_records_ttft() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed("answer", now=12.0)
    tracker.output_observed(now=17.0)

    snapshot = tracker.model_finished(100, now=17.0)

    assert snapshot.output_tokens == 100
    assert snapshot.elapsed_s == 7.0
    assert snapshot.tokens_per_second == 100 / 5
    assert snapshot.ttft_s == 2.0
    assert snapshot.basis is TokenRateBasis.GENERATION
    assert snapshot.estimated is False


def test_token_rate_falls_back_to_end_to_end_without_stream_output() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)

    snapshot = tracker.model_finished(40, now=18.0)

    assert snapshot.elapsed_s == 8.0
    assert snapshot.tokens_per_second == 5.0
    assert snapshot.ttft_s is None
    assert snapshot.basis is TokenRateBasis.END_TO_END


def test_token_rate_completed_snapshot_is_reset_after_finish() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed("1234", now=12.0)
    tracker.output_observed("5678", now=14.0)

    first = tracker.model_finished(20, now=15.0)
    second = tracker.model_finished(20, now=16.0)

    assert first.tokens_per_second == 20 / 3
    assert second.tokens_per_second is None


def test_token_rate_live_snapshot_is_estimated_in_real_time() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed("abcdefgh", now=12.0)

    snapshot = tracker.live_snapshot(now=14.0)

    assert snapshot.output_tokens == 2
    assert snapshot.elapsed_s == 4.0
    assert snapshot.tokens_per_second == 1.0
    assert snapshot.ttft_s == 2.0
    assert snapshot.basis is TokenRateBasis.GENERATION
    assert snapshot.estimated is True


def test_token_rate_single_output_uses_decode_duration() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed(now=12.0)

    snapshot = tracker.model_finished(100, now=17.0)

    assert snapshot.elapsed_s == 7.0
    assert snapshot.tokens_per_second == 100 / 5
    assert snapshot.ttft_s == 2.0
    assert snapshot.basis is TokenRateBasis.GENERATION


def test_token_rate_handles_zero_duration_and_invalid_tokens() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed(now=10.0)

    snapshot = tracker.model_finished(-1, now=10.0)

    assert snapshot.output_tokens == 0
    assert snapshot.tokens_per_second is None
    assert snapshot.basis is TokenRateBasis.GENERATION


def test_token_rate_finished_without_start_is_empty() -> None:
    snapshot = TokenRateTracker().model_finished(100, now=10.0)

    assert snapshot.output_tokens == 100
    assert snapshot.tokens_per_second is None
    assert snapshot.ttft_s is None


def test_format_token_rate() -> None:
    assert format_token_rate(None) == ""
    assert format_token_rate(0) == ""
    assert format_token_rate(4.25) == "4.2 tok/s"
    assert format_token_rate(42.6) == "43 tok/s"
    assert format_token_rate(42.6, estimated=True) == "~43 tok/s"


def test_token_rate_ensure_started_sets_start_when_idle() -> None:
    tracker = TokenRateTracker()
    tracker.ensure_started(now=10.0)
    tracker.output_observed("x", now=12.0)

    snapshot = tracker.model_finished(100, now=17.0)

    assert snapshot.ttft_s == 2.0
    assert snapshot.elapsed_s == 7.0
    assert snapshot.tokens_per_second == 100 / 5


def test_token_rate_ensure_started_does_not_override_in_flight_start() -> None:
    tracker = TokenRateTracker()
    tracker.model_started(now=10.0)
    tracker.output_observed("x", now=12.0)

    tracker.ensure_started(now=99.0)

    snapshot = tracker.model_finished(100, now=17.0)

    assert snapshot.ttft_s == 2.0
    assert snapshot.elapsed_s == 7.0
    assert snapshot.tokens_per_second == 100 / 5
