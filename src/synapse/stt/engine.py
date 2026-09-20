"""The local ONNX speech engine: guarded import, lazy load, one warm instance.

Two properties shape this module.

**The app runs without it.**  ``sherpa-onnx`` is an optional extra, so importing
it is guarded and every failure becomes an :class:`SttUnavailable` carrying a
reason the console can print.  Nothing here is imported at application start-up:
a checkout without the extra pays nothing until someone asks for local speech.

**Loading is the expensive part, not inference.**  Building the recognizers costs
tens of seconds on a CPU (measured: ~40 s per model), while decoding runs at
5-13x real time.  So the models are built once, kept, and shared: the engine is
the warm process, and the first dictation pays the build.  Every entry point that
touches the models therefore holds a lock -- one build at a time, and one decode
at a time inside the shared C++ objects.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.stt.models import SAMPLE_RATE, ModelSet, SttUnavailable, resolve_model_set
from synapse.stt.session import SttSession

__all__ = ["LocalSttEngine", "SttModels", "SttStatus", "sherpa_onnx_version"]

#: VAD window/buffer sizing: 30 s of audio is far more than one sentence, and the
#: detector is per session, so the buffer is the session's own memory.
_VAD_BUFFER_SECONDS = 30.0

#: Sentence-end detection: a pause this long closes a sentence and runs the second
#: pass.  Long enough that a breath inside a sentence does not split it, short
#: enough that the correction arrives while the reader still expects it.
_VAD_MIN_SILENCE_SECONDS = 0.6
_VAD_MIN_SPEECH_SECONDS = 0.25
_VAD_MAX_SPEECH_SECONDS = 20.0


def sherpa_onnx_version() -> str | None:
    """The installed ``sherpa-onnx`` version, or None when the extra is absent."""
    try:
        import sherpa_onnx  # noqa: PLC0415 - the optional dependency, by design
    except Exception:  # noqa: BLE001 - a missing or broken wheel is the same answer here
        return None
    return str(getattr(sherpa_onnx, "version", "") or "")


@dataclass(frozen=True)
class SttStatus:
    """Whether local speech input can run, and why not when it cannot."""

    available: bool
    reason: str | None
    model_dir: str
    loaded: bool


@dataclass
class SttModels:
    """The warm, shared recognizers plus a factory for per-session state.

    The recognizers are shared (they are the expensive objects); the VAD is not,
    because a detector owns the audio it has buffered and the sentences it has
    queued -- one per dictation session.
    """

    streaming: Any
    offline: Any
    vad_config: Any
    sherpa: Any

    def create_vad(self) -> Any:
        return self.sherpa.VoiceActivityDetector(
            self.vad_config, buffer_size_in_seconds=_VAD_BUFFER_SECONDS
        )


class LocalSttEngine:
    """Owns the model set: availability probe, lazy build, and the shared lock."""

    def __init__(self, model_dir: Path | None = None) -> None:
        self._model_dir = Path(model_dir).expanduser() if model_dir is not None else None
        self._lock = threading.RLock()
        self._models: SttModels | None = None

    @property
    def model_dir(self) -> Path:
        from synapse.stt.models import default_model_dir

        return self._model_dir if self._model_dir is not None else default_model_dir()

    def status(self) -> SttStatus:
        """Whether a session can be started, without building anything."""
        version = sherpa_onnx_version()
        if version is None:
            return SttStatus(
                available=False,
                reason="未安装本地语音引擎：执行 uv sync --extra stt-local 安装 sherpa-onnx",
                model_dir=str(self.model_dir),
                loaded=False,
            )
        try:
            resolve_model_set(self.model_dir)
        except SttUnavailable as unavailable:
            return SttStatus(
                available=False,
                reason=unavailable.reason,
                model_dir=str(self.model_dir),
                loaded=self._models is not None,
            )
        return SttStatus(
            available=True,
            reason=None,
            model_dir=str(self.model_dir),
            loaded=self._models is not None,
        )

    @property
    def loaded(self) -> bool:
        """Whether the recognizers are already in memory."""
        return self._models is not None

    def models(self) -> SttModels:
        """The warm recognizers, building them on first use.

        Blocking and slow on the first call: callers on an event loop must run this
        in a worker thread (the runtime service does).

        Raises:
            SttUnavailable: the extra is missing or the model set is incomplete.
        """
        with self._lock:
            if self._models is not None:
                return self._models
            self._models = self._build()
            return self._models

    def warm_up(self) -> SttStatus:
        """Build the models now, and answer with the resulting status."""
        try:
            self.models()
        except SttUnavailable as unavailable:
            return SttStatus(
                available=False,
                reason=unavailable.reason,
                model_dir=str(self.model_dir),
                loaded=False,
            )
        return self.status()

    def new_session(self) -> SttSession:
        """One dictation over the warm models, sharing their lock.

        Raises:
            SttUnavailable: the extra is missing or the model set is incomplete.
        """
        return SttSession(self.models(), lock=self._lock)

    def release(self) -> None:
        """Drop the warm models (tests, and a settings change that moves them)."""
        with self._lock:
            self._models = None

    def _build(self) -> SttModels:
        version = sherpa_onnx_version()
        if version is None:
            raise SttUnavailable(
                "未安装本地语音引擎：执行 uv sync --extra stt-local 安装 sherpa-onnx"
            )
        import sherpa_onnx  # noqa: PLC0415 - guarded above; the optional dependency, by design

        models = resolve_model_set(self.model_dir)
        return SttModels(
            streaming=sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=str(models.streaming_tokens),
                encoder=str(models.streaming_encoder),
                decoder=str(models.streaming_decoder),
                # One thread per recognizer: the runtime daemon is not the only
                # process on this machine, and a dictation must not starve it.
                num_threads=2,
                sample_rate=SAMPLE_RATE,
            ),
            offline=sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(models.offline_model),
                tokens=str(models.offline_tokens),
                num_threads=4,
                sample_rate=SAMPLE_RATE,
            ),
            vad_config=_vad_config(sherpa_onnx, models),
            sherpa=sherpa_onnx,
        )


def _vad_config(sherpa_onnx: Any, models: ModelSet) -> Any:
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(models.vad)
    config.silero_vad.threshold = 0.5
    config.silero_vad.min_silence_duration = _VAD_MIN_SILENCE_SECONDS
    config.silero_vad.min_speech_duration = _VAD_MIN_SPEECH_SECONDS
    config.silero_vad.max_speech_duration = _VAD_MAX_SPEECH_SECONDS
    config.sample_rate = SAMPLE_RATE
    return config
