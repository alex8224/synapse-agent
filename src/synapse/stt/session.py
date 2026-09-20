"""One dictation session: live text from the streaming model, corrected text from the offline one.

The two-pass shape is the whole point of this module.  While the reader speaks, a
*streaming* model produces text at ~600 ms granularity -- fast enough to show, but
it only ever appends: it cannot take back a word it got wrong.  When the VAD
decides a sentence has ended, an *offline* model re-decodes exactly that sentence
and its answer replaces the live text.

So a session hands out two different things, and the console treats them
differently on purpose:

- ``partial`` is provisional.  It is shown in the composer's caption and may be
  replaced at any moment; it must never be inserted into the draft.
- ``finalized`` is authoritative.  Each item is one finished sentence, already
  corrected, and it is what the draft receives.

A session owns the per-stream state (the streaming stream and the VAD detector,
which buffers audio and queues sentences); the recognizers themselves are shared
and the caller passes the lock that guards them, so two dictations cannot decode
through the same C++ object at once.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from synapse.stt.models import SAMPLE_RATE

__all__ = ["SttSession", "SttUpdate"]


@dataclass(frozen=True)
class SttUpdate:
    """What one chunk of audio produced.

    ``partial`` is the sentence being spoken as the streaming model currently
    hears it (possibly empty).  ``finalized`` holds the sentences that ended in
    this chunk, each already re-decoded by the offline model, in order.
    """

    partial: str
    finalized: tuple[str, ...]


class SttSession:
    """A single dictation: feed audio in, get provisional and corrected text out."""

    def __init__(
        self,
        models: Any,
        *,
        sample_rate: int = SAMPLE_RATE,
        lock: threading.RLock | None = None,
    ) -> None:
        self._models = models
        self._sample_rate = sample_rate
        # The lock guards the *shared* recognizers, so it is the engine's when a
        # session comes from one, and a private one for a session built directly
        # (tests, and a future per-session engine).
        self._lock = lock if lock is not None else threading.RLock()
        self._stream = models.streaming.create_stream()
        self._vad = models.create_vad()
        self._partial = ""

    def accept(self, samples: Sequence[float]) -> SttUpdate:
        """Feed one chunk of 16 kHz mono audio and report what changed.

        Blocking, and the expensive call of the two: the streaming decode costs
        roughly a fifth of the audio it is given.  Callers on an event loop must
        run it in a worker thread.
        """
        if len(samples) == 0:
            return SttUpdate(partial=self._partial, finalized=())
        with self._lock:
            self._stream.accept_waveform(self._sample_rate, samples)
            while self._models.streaming.is_ready(self._stream):
                self._models.streaming.decode_stream(self._stream)
            self._partial = self._models.streaming.get_result(self._stream)
            self._vad.accept_waveform(samples)
            closed = self._drain(restart=True)
            return SttUpdate(partial=self._partial, finalized=closed)

    def finish(self) -> SttUpdate:
        """Flush both models and report the last sentence, if any.

        The streaming model's tail is used when the VAD never closed a sentence --
        a very short utterance, or audio that never crossed its speech threshold.
        Dropping it would silently lose what the reader said.
        """
        with self._lock:
            self._stream.input_finished()
            while self._models.streaming.is_ready(self._stream):
                self._models.streaming.decode_stream(self._stream)
            tail = self._models.streaming.get_result(self._stream).strip()
            self._partial = tail
            self._vad.flush()
            closed = self._drain(restart=False)
            if not closed and tail:
                closed = (tail,)
            self._partial = ""
            return SttUpdate(partial="", finalized=closed)

    def reset(self) -> None:
        """Forget everything buffered; the next chunk starts a new dictation."""
        with self._lock:
            self._models.streaming.reset(self._stream)
            self._vad.reset()
            self._partial = ""

    @property
    def partial(self) -> str:
        """The current provisional sentence, without touching the models."""
        return self._partial

    def _drain(self, *, restart: bool) -> tuple[str, ...]:
        """Close every sentence the VAD has finished since the last call.

        ``restart`` resets the streaming stream between sentences: a session is one
        continuous audio stream, so without it the next sentence's live text would
        still carry the previous sentence's words.
        """
        closed: list[str] = []
        while not self._vad.empty:
            segment = self._vad.front
            text = self._correct(segment.samples)
            self._vad.pop()
            if restart:
                self._models.streaming.reset(self._stream)
            if text:
                closed.append(text)
        if closed:
            self._partial = ""
        return tuple(closed)

    def _correct(self, samples: Sequence[float]) -> str:
        """Re-decode one finished sentence with the offline model."""
        text = self._decode_offline(samples).strip()
        if text:
            return text
        # The offline pass found nothing (a fragment too short for it to score, or
        # pure silence the VAD still cut): the live text is the only evidence of
        # what was said, so it is kept rather than dropped.
        return self._partial.strip()

    def _decode_offline(self, samples: Sequence[float]) -> str:
        stream = self._models.offline.create_stream()
        stream.accept_waveform(self._sample_rate, samples)
        self._models.offline.decode_stream(stream)
        return str(stream.result.text)
