"""Token output-rate tracking for one model call."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic


class TokenRateBasis(StrEnum):
    """Elapsed-time basis used for an output-rate measurement."""

    GENERATION = "generation"
    END_TO_END = "end_to_end"


@dataclass(frozen=True, slots=True)
class TokenRateSnapshot:
    """Completed output-rate measurement for one model call."""

    output_tokens: int = 0
    elapsed_s: float = 0.0
    tokens_per_second: float | None = None
    ttft_s: float | None = None
    basis: TokenRateBasis = TokenRateBasis.END_TO_END
    estimated: bool = False


class TokenRateTracker:
    """Track first output and completion times for one active model call.

    The tracker deliberately does not estimate tokens from text chunks. The
    provider's final usage metadata remains the source of truth for output
    tokens; if no streamed output was observed, the measurement falls back to
    the end-to-end model-call duration.
    """

    def __init__(self, *, clock=monotonic) -> None:
        self._clock = clock
        self._started_at: float | None = None
        self._first_output_at: float | None = None
        self._estimated_chars = 0
        # The innermost model middleware fires ``model_started`` from the async
        # runtime loop while the parser thread drives ``output_observed`` /
        # ``model_finished``. Keep every state transition atomic across those
        # two threads.
        self._lock = threading.Lock()

    def model_started(self, now: float | None = None) -> None:
        """Start or restart timing for the active model call."""
        with self._lock:
            started_at = self._now(now)
            self._started_at = started_at
            self._first_output_at = None
            self._estimated_chars = 0

    def ensure_started(self, now: float | None = None) -> None:
        """Record the call start only when one has not been recorded yet.

        Unlike ``model_started`` this never overrides an in-flight start, so a
        parser-side fallback cannot clobber the precise dispatch timestamp the
        middleware notifier already installed.
        """
        with self._lock:
            if self._started_at is None:
                started_at = self._now(now)
                self._started_at = started_at
                self._first_output_at = None
                self._estimated_chars = 0

    def output_observed(self, text: str = "", now: float | None = None) -> None:
        """Record the first streamed reasoning, answer, or tool-call output."""
        with self._lock:
            observed_at = self._now(now)
            if self._started_at is None:
                self._started_at = observed_at
            if self._first_output_at is None:
                self._first_output_at = observed_at
            self._estimated_chars += len(text or "")

    def live_snapshot(self, now: float | None = None) -> TokenRateSnapshot:
        """Return a local, explicitly estimated rate for the active call.

        Like ``model_finished``, the estimated rate divides by the decode span
        (first output -> now) once output has been observed, so the live figure
        tracks generation speed rather than end-to-end wall time.
        """
        with self._lock:
            observed_at = self._now(now)
            started_at = self._started_at
            if started_at is None or self._estimated_chars <= 0:
                return TokenRateSnapshot(estimated=True)
            elapsed = max(0.0, observed_at - started_at)
            estimated_tokens = max(1, (self._estimated_chars + 3) // 4)
            first_output_at = self._first_output_at
            if first_output_at is not None:
                decode_span = max(0.0, observed_at - first_output_at)
                rate = estimated_tokens / decode_span if decode_span > 0 else None
                basis = TokenRateBasis.GENERATION
            else:
                rate = estimated_tokens / elapsed if elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END
            ttft = (
                max(0.0, first_output_at - started_at)
                if first_output_at is not None
                else None
            )
            return TokenRateSnapshot(
                output_tokens=estimated_tokens,
                elapsed_s=elapsed,
                tokens_per_second=rate,
                ttft_s=ttft,
                basis=basis,
                estimated=True,
            )

    def model_finished(
        self,
        output_tokens: int,
        now: float | None = None,
    ) -> TokenRateSnapshot:
        """Finish the active call using provider-reported output tokens.

        Throughput is measured over the decode span (first output -> completion)
        when a first output was observed, keeping prefill and graph overhead out
        of the rate denominator. Without a streamed first output the tracker
        falls back to the end-to-end call duration. ``elapsed_s`` always reports
        the end-to-end duration for reference.
        """
        with self._lock:
            finished_at = self._now(now)
            started_at = self._started_at
            first_output_at = self._first_output_at
            self._started_at = None
            self._first_output_at = None
            self._estimated_chars = 0

            tokens = max(0, int(output_tokens or 0))
            if started_at is None:
                return TokenRateSnapshot(output_tokens=tokens)

            elapsed = max(0.0, finished_at - started_at)
            ttft = (
                max(0.0, first_output_at - started_at)
                if first_output_at is not None
                else None
            )

            if first_output_at is not None:
                # Provider output_tokens may include hidden reasoning, so the
                # decode span (first visible output -> completion) can
                # under-count that hidden work when the provider hides
                # reasoning deltas. Providers that stream reasoning (e.g.
                # DeepSeek reasoning_content) make the first output the
                # reasoning start and keep this denominator honest.
                decode_span = max(0.0, finished_at - first_output_at)
                rate = tokens / decode_span if tokens > 0 and decode_span > 0 else None
                basis = TokenRateBasis.GENERATION
            else:
                rate = tokens / elapsed if tokens > 0 and elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END

            return TokenRateSnapshot(
                output_tokens=tokens,
                elapsed_s=elapsed,
                tokens_per_second=rate,
                ttft_s=ttft,
                basis=basis,
                estimated=False,
            )

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)


__all__ = ["TokenRateBasis", "TokenRateSnapshot", "TokenRateTracker"]
