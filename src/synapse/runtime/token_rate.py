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
        # Whether any streamed reasoning delta was observed for the active call.
        # When True the decode span starts at the reasoning start and therefore
        # covers reasoning + visible output honestly.
        self._observed_reasoning = False
        # Tool-call chunks can finish immediately after the first chunk while
        # provider usage still includes the complete structured output. Their
        # first-chunk-to-finish span is therefore not a reliable decode rate.
        self._observed_tool_call = False
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
            self._observed_reasoning = False
            self._observed_tool_call = False

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
                self._observed_reasoning = False
                self._observed_tool_call = False

    def output_observed(
        self,
        text: str = "",
        now: float | None = None,
        *,
        reasoning: bool = False,
        tool_call: bool = False,
    ) -> None:
        """Record the first streamed reasoning, answer, or tool-call output.

        ``reasoning=True`` marks the delta as streamed model reasoning, which
        means the decode span begins before any visible answer/tool-call text.
        """
        with self._lock:
            observed_at = self._now(now)
            if self._started_at is None:
                self._started_at = observed_at
            if self._first_output_at is None:
                self._first_output_at = observed_at
            if reasoning:
                self._observed_reasoning = True
            if tool_call:
                self._observed_tool_call = True
            self._estimated_chars += len(text or "")

    def live_snapshot(self, now: float | None = None) -> TokenRateSnapshot:
        """Return a local, explicitly estimated rate for the active call.

        Like ``model_finished``, the estimated rate divides by the decode span
        (first output -> now) once output has been observed. Tool-call-only
        output instead uses the end-to-end span because its visible structured
        output can end immediately before the final usage message.
        """
        with self._lock:
            observed_at = self._now(now)
            started_at = self._started_at
            if started_at is None or self._estimated_chars <= 0:
                return TokenRateSnapshot(estimated=True)
            elapsed = max(0.0, observed_at - started_at)
            estimated_tokens = max(1, (self._estimated_chars + 3) // 4)
            first_output_at = self._first_output_at
            if self._observed_reasoning:
                decode_span = max(0.0, observed_at - first_output_at)
                rate = estimated_tokens / decode_span if decode_span > 0 else None
                basis = TokenRateBasis.GENERATION
            elif self._observed_tool_call:
                rate = estimated_tokens / elapsed if elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END
            elif first_output_at is not None:
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
        *,
        hidden_reasoning_tokens: int = 0,
    ) -> TokenRateSnapshot:
        """Finish the active call using provider-reported output tokens.

        Throughput is measured over the decode span (first output -> completion)
        when a first output was observed, keeping prefill and graph overhead out
        of the rate denominator. When the provider reports reasoning tokens that
        were never streamed (``hidden_reasoning_tokens > 0`` and no reasoning
        delta observed), the decode span under-counts that hidden work, so the
        tracker falls back to the end-to-end call duration. Without a streamed
        first output it also falls back to end-to-end. ``elapsed_s`` always
        reports the end-to-end duration for reference.
        """
        with self._lock:
            finished_at = self._now(now)
            started_at = self._started_at
            first_output_at = self._first_output_at
            observed_reasoning = self._observed_reasoning
            observed_tool_call = self._observed_tool_call
            hidden_reasoning = max(0, int(hidden_reasoning_tokens or 0))
            self._started_at = None
            self._first_output_at = None
            self._estimated_chars = 0
            self._observed_reasoning = False
            self._observed_tool_call = False

            tokens = max(0, int(output_tokens or 0))
            if started_at is None:
                return TokenRateSnapshot(output_tokens=tokens)

            elapsed = max(0.0, finished_at - started_at)
            ttft = (
                max(0.0, first_output_at - started_at)
                if first_output_at is not None
                else None
            )

            if first_output_at is None:
                rate = tokens / elapsed if tokens > 0 and elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END
            elif observed_reasoning:
                # Streamed reasoning started the decode clock, so the span
                # covers reasoning + visible output honestly.
                decode_span = max(0.0, finished_at - first_output_at)
                rate = tokens / decode_span if tokens > 0 and decode_span > 0 else None
                basis = TokenRateBasis.GENERATION
            elif observed_tool_call:
                # Structured tool-call output often has a very short visible
                # span even though provider output usage includes more work.
                # Use the full model-call duration instead of an inflated rate.
                rate = tokens / elapsed if tokens > 0 and elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END
            elif hidden_reasoning > 0:
                # The provider counted reasoning tokens in output_tokens but
                # never streamed them. The decode span (first visible output ->
                # completion) excludes that hidden work, so divide by the full
                # end-to-end duration instead of the decode span.
                rate = tokens / elapsed if tokens > 0 and elapsed > 0 else None
                basis = TokenRateBasis.END_TO_END
            else:
                decode_span = max(0.0, finished_at - first_output_at)
                rate = tokens / decode_span if tokens > 0 and decode_span > 0 else None
                basis = TokenRateBasis.GENERATION

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
