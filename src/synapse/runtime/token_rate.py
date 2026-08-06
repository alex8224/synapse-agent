"""Token output-rate tracking for one model call."""

from __future__ import annotations

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

    def model_started(self, now: float | None = None) -> None:
        """Start or restart timing for the active model call."""
        started_at = self._now(now)
        self._started_at = started_at
        self._first_output_at = None
        self._estimated_chars = 0

    def output_observed(self, text: str = "", now: float | None = None) -> None:
        """Record the first streamed reasoning, answer, or tool-call output."""
        if self._started_at is None:
            self.model_started(now)
        observed_at = self._now(now)
        if self._first_output_at is None:
            self._first_output_at = observed_at
        self._estimated_chars += len(text or "")

    def live_snapshot(self, now: float | None = None) -> TokenRateSnapshot:
        """Return a local, explicitly estimated rate for the active call."""
        observed_at = self._now(now)
        started_at = self._started_at
        if started_at is None or self._estimated_chars <= 0:
            return TokenRateSnapshot(estimated=True)
        elapsed = max(0.0, observed_at - started_at)
        estimated_tokens = max(1, (self._estimated_chars + 3) // 4)
        rate = estimated_tokens / elapsed if elapsed > 0 else None
        ttft = (
            max(0.0, self._first_output_at - started_at)
            if self._first_output_at is not None
            else None
        )
        return TokenRateSnapshot(
            output_tokens=estimated_tokens,
            elapsed_s=elapsed,
            tokens_per_second=rate,
            ttft_s=ttft,
            basis=TokenRateBasis.END_TO_END,
            estimated=True,
        )

    def model_finished(
        self,
        output_tokens: int,
        now: float | None = None,
    ) -> TokenRateSnapshot:
        """Finish the active call using provider-reported output tokens."""
        finished_at = self._now(now)
        started_at = self._started_at
        first_output_at = self._first_output_at
        self._started_at = None
        self._first_output_at = None
        self._estimated_chars = 0

        tokens = max(0, int(output_tokens or 0))
        if started_at is None:
            return TokenRateSnapshot(output_tokens=tokens)

        # Provider output_tokens may include hidden reasoning. Use the complete
        # call duration so hidden work is represented in the denominator and the
        # final rate cannot be inflated by timing visible chunks only.
        elapsed = max(0.0, finished_at - started_at)
        ttft = (
            max(0.0, first_output_at - started_at)
            if first_output_at is not None
            else None
        )

        rate = tokens / elapsed if tokens > 0 and elapsed > 0 else None
        return TokenRateSnapshot(
            output_tokens=tokens,
            elapsed_s=elapsed,
            tokens_per_second=rate,
            ttft_s=ttft,
            basis=TokenRateBasis.END_TO_END,
            estimated=False,
        )

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else float(now)


__all__ = ["TokenRateBasis", "TokenRateSnapshot", "TokenRateTracker"]
