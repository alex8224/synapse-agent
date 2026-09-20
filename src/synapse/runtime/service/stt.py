"""Runtime wire surface for local speech-to-text (composer dictation).

The console's composer can dictate with the browser's own recognizer or with the
host's *local* engine (the optional ``stt-local`` extra).  This module owns the
transport-neutral half of the local path: the frozen request/result DTOs, the
per-chunk byte bound, and the session-scoped :class:`SttService` that drives one
dictation at a time.

Design rules:

- **A status read never builds the models.**  ``runtime.stt.status`` folds the
  engine's own :meth:`LocalSttEngine.status` probe into its answer, so a missing
  extra or an incomplete model directory is an ordinary ``available=False`` with
  a reason a reader can act on -- never an exception and never a recognizer build.
- **One dictation per session.**  ``begin`` replaces any dictation already open
  for the session, so a double activation cannot leave two streams decoding the
  same microphone.
- **Audio is bounded and explicit.**  ``append`` accepts one base64 chunk of
  int16 little-endian mono PCM at the announced sample rate; anything larger than
  :data:`MAX_STT_CHUNK_BYTES` is refused with :class:`SttChunkTooLargeError`
  before it reaches the engine.
- **The engine is blocking CPU work.**  Every method here is synchronous; the
  runtime service layer runs them through ``asyncio.to_thread`` so a decode never
  stalls the event loop (a chunk costs roughly a fifth of its audio duration).
"""

from __future__ import annotations

import base64
import binascii
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from synapse.runtime.service.errors import (
    InvalidRequestError,
    SttChunkTooLargeError,
    SttUnavailableError,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.stt.doubao import DoubaoSession
from synapse.stt.engine import LocalSttEngine, SttStatus
from synapse.stt.models import SttUnavailable
from synapse.stt.providers import PROVIDERS, SttProviderInfo, provider_ids, provider_info
from synapse.stt.session import SttSession

__all__ = [
    "MAX_STT_CHUNK_BASE64_CHARS",
    "MAX_STT_CHUNK_BYTES",
    "MAX_STT_ENGINE_CHARS",
    "MAX_STT_API_KEY_CHARS",
    "MAX_STT_MODEL_DIR_CHARS",
    "STT_SAMPLE_RATE",
    "SttAppendCommand",
    "SttAppendResult",
    "SttBeginCommand",
    "SttBeginResult",
    "SttCancelCommand",
    "SttCancelResult",
    "SttFinishCommand",
    "SttFinishResult",
    "SttService",
    "SttStatusQuery",
    "SttStatusView",
    "SttProviderView",
    "SttWarmUpCommand",
    "SttSetEngineCommand",
    "SttSetApiKeyCommand",
    "decode_stt_chunk",
]

# --- limits -----------------------------------------------------------------

#: The rate every local model is trained on; the console resamples to it and the
#: ``append`` payload is int16 mono PCM at this rate.  It mirrors
#: ``synapse.stt.models.SAMPLE_RATE``; ``tests/test_runtime_stt.py`` asserts the
#: two never drift.
STT_SAMPLE_RATE = 16_000
#: One audio chunk decodes to at most 64 KiB -- about two seconds of 16 kHz mono
#: int16 PCM.  The wire decoder bounds the base64 length and the service bounds
#: the decoded bytes, so an oversized chunk is refused before it is decoded.
MAX_STT_CHUNK_BYTES = 64 * 1024
#: Worst-case base64 length of one chunk (padding-free, no line breaks).
MAX_STT_CHUNK_BASE64_CHARS = 4 * ((MAX_STT_CHUNK_BYTES + 2) // 3)

#: Longest accepted model directory.  It is a filesystem path, not a payload: the
#: bound exists so a console bug cannot store an unbounded string in the user's
#: settings file.
MAX_STT_MODEL_DIR_CHARS = 1024

#: Longest accepted engine name.  It is one of two short words, so the bound only
#: exists to keep a malformed frame from reaching the settings writer at all.
MAX_STT_ENGINE_CHARS = 32

#: Longest accepted credential, mirroring ``synapse.stt.credentials``.  The wire
#: layer rejects anything longer before the value can reach a file.
MAX_STT_API_KEY_CHARS = 512

#: The engines a client may select, derived from the provider registry so it can
#: never go stale: a hard-coded tuple here is what made "doubao" unusable while the
#: settings screen offered it (the registry knew it, this list did not).
STT_ENGINES = provider_ids()
#: PCM samples are scaled to floats in ``[-1, 1]``; int16 full scale is 2**15.
_PCM_INT16_SCALE = 32768.0


# --- DTOs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SttStatusQuery:
    """Ask whether local speech input can run for the calling session."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SttStatusView:
    """The local speech engine's availability, without building the models.

    ``engine`` is the configured engine mode (``browser`` or ``local``); the other
    fields describe the *local* engine, so a client can decide in one read whether
    to ask this host to transcribe or to fall back to the browser's recognizer.
    """

    available: bool
    engine: str
    reason: str | None
    loaded: bool
    model_dir: str
    sample_rate: int
    #: Every engine this host can run, so the console renders the choices instead of
    #: knowing them.  Appended last so the existing fields keep their positions.
    providers: tuple[SttProviderView, ...] = ()


@dataclass(frozen=True, slots=True)
class SttProviderView:
    """One selectable engine, as the console needs it.

    ``available``/``reason`` describe *this* provider, not the selected one, so a
    reader can see that a second engine exists but needs a key or a model set.
    """

    id: str
    label: str
    kind: str
    needs_key: bool
    key_configured: bool
    available: bool
    reason: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class SttBeginCommand:
    """Start (or restart) one dictation for the calling session."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SttWarmUpCommand:
    """Build the local models now, so the first dictation does not pay for them."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SttSetEngineCommand:
    """Choose which engine the console's microphone runs.

    ``model_dir`` is the local engine's model directory, or None/empty for the
    engine's own default.  The result is the *effective* status after the change,
    so a client learns in one round trip whether the newly chosen engine can
    actually run here.
    """

    session: SessionRef
    engine: str
    model_dir: str | None = None


@dataclass(frozen=True, slots=True)
class SttSetApiKeyCommand:
    """Store one provider's credential.

    The key travels *to* the daemon and is never returned: the answer is the status
    view, whose provider entries carry only ``key_configured``.
    """

    session: SessionRef
    provider: str
    api_key: str


@dataclass(frozen=True, slots=True)
class SttBeginResult:
    """The announced sample rate for the dictation's PCM chunks."""

    sample_rate: int


@dataclass(frozen=True, slots=True)
class SttAppendCommand:
    """Feed one base64 chunk of int16 little-endian mono PCM."""

    session: SessionRef
    data_base64: str


@dataclass(frozen=True, slots=True)
class SttAppendResult:
    """What one audio chunk produced: provisional text plus finished sentences."""

    partial: str
    finalized: tuple[str, ...]
    #: Why the engine stopped producing text (a dropped connection, a refused key),
    #: or None.  A local engine fails loudly on the next call; a cloud one can fail
    #: mid-stream, and silence there would look like "it just stopped hearing me".
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SttFinishCommand:
    """Flush the calling session's dictation and collect its last sentence."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SttFinishResult:
    """The sentences finished by the flush, in order."""

    finalized: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SttCancelCommand:
    """Drop the calling session's dictation and its buffered audio."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class SttCancelResult:
    """Whether a dictation was open and got dropped."""

    cancelled: bool


# --- decoding ---------------------------------------------------------------


def decode_stt_chunk(data_base64: object) -> list[float]:
    """Decode one bounded base64 chunk of int16 little-endian mono PCM to floats.

    The decoded byte budget is enforced here so an in-process caller cannot bypass
    it, and the wire decoder enforces the matching base64 length separately.  A
    chunk with an odd byte count is not int16 PCM and is rejected.
    """
    if type(data_base64) is not str:
        raise InvalidRequestError("stt chunk must be a base64 string")
    if not data_base64:
        raise InvalidRequestError("stt chunk is empty")
    if len(data_base64) > MAX_STT_CHUNK_BASE64_CHARS:
        raise SttChunkTooLargeError(
            f"stt chunk must not exceed {MAX_STT_CHUNK_BASE64_CHARS} base64 characters"
        )
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidRequestError("stt chunk is not valid base64") from exc
    if not data:
        raise InvalidRequestError("stt chunk is empty")
    if len(data) > MAX_STT_CHUNK_BYTES:
        raise SttChunkTooLargeError(f"stt chunk must not exceed {MAX_STT_CHUNK_BYTES} bytes")
    if len(data) % 2 != 0:
        raise InvalidRequestError("stt chunk must be int16 little-endian PCM")
    count = len(data) // 2
    return [value / _PCM_INT16_SCALE for value in struct.unpack(f"<{count}h", data)]


# --- service ----------------------------------------------------------------


class SttEngine(Protocol):
    """The blocking engine surface this service depends on.

    :class:`synapse.stt.engine.LocalSttEngine` satisfies it; tests supply a fake
    so no model is ever built.
    """

    def status(self) -> SttStatus: ...

    def warm_up(self) -> SttStatus: ...

    def new_session(self) -> SttSession: ...


def _new_engine(model_dir: str | None) -> LocalSttEngine:
    """Build the default local engine for one configured model directory."""
    return LocalSttEngine(Path(model_dir).expanduser() if model_dir else None)


class SttService:
    """The daemon-resident dictation scheduler (shared by every connection).

    The warm engine is expensive and must survive a reconnect, so one service is
    built for the daemon's lifetime and keyed by model directory: a settings
    change that points at another model set gets its own engine instead of
    silently reusing the first one.
    """

    def __init__(
        self,
        engine_factory: Callable[[str | None], SttEngine] | None = None,
        key_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        self._engine_factory = engine_factory if engine_factory is not None else _new_engine
        # Injected rather than imported: this module is part of the contract layer,
        # which may not reach into settings or the credential store.  The caller
        # (`service/local.py`) supplies the real lookup; tests supply a fake.
        self._key_lookup = key_lookup if key_lookup is not None else (lambda _provider: None)
        self._engines: dict[str, SttEngine] = {}
        self._sessions: dict[SessionRef, SttSession] = {}

    def _engine(self, model_dir: str | None) -> SttEngine:
        key = model_dir or ""
        engine = self._engines.get(key)
        if engine is None:
            engine = self._engine_factory(model_dir)
            self._engines[key] = engine
        return engine

    # -- status ------------------------------------------------------------

    def status(self, *, engine: str, model_dir: str | None) -> SttStatusView:
        """Report local availability without building the models.

        The engine's own probe already treats a missing extra or model set as an
        ordinary answer; the broad catch below is the last-resort degradation
        boundary so a broken optional wheel can never turn a status read into an
        error the console cannot render.
        """
        # One local probe for the whole read: the engine's own answer is what the
        # console shows (it resolves the default directory), and asking twice would
        # be two file-system walks for one screen.
        local = self._local_probe(model_dir)
        providers = tuple(self._provider_view(info, local=local) for info in PROVIDERS)
        selected = next((item for item in providers if item.id == engine), None)
        if selected is None:
            # An id nothing implements (a settings file from a newer build, a typo):
            # reported rather than raised, and the console keeps the browser engine.
            return SttStatusView(
                available=False,
                engine=engine,
                reason=f"未知的语音引擎：{engine}",
                loaded=False,
                model_dir=model_dir or "",
                sample_rate=STT_SAMPLE_RATE,
                providers=providers,
            )
        return SttStatusView(
            available=selected.available,
            engine=selected.id,
            reason=selected.reason,
            # `loaded` is a property of the local engine, not part of the protocol
            # every engine implements, so it is read defensively.
            loaded=selected.kind == "local"
            and bool(getattr(self._engine(model_dir), "loaded", False)),
            model_dir=local[2] if selected.kind == "local" else (model_dir or ""),
            sample_rate=STT_SAMPLE_RATE,
            providers=providers,
        )

    def _local_probe(self, model_dir: str | None) -> tuple[bool, str | None, str]:
        """``(available, reason, resolved model directory)`` for the local engine."""
        try:
            probe = self._engine(model_dir).status()
        except Exception:  # noqa: BLE001 - degradation boundary for the optional extra
            return False, "本地语音引擎不可用", model_dir or ""
        return probe.available, probe.reason, probe.model_dir

    def _provider_view(
        self,
        info: SttProviderInfo,
        *,
        local: tuple[bool, str | None, str],
    ) -> SttProviderView:
        """One provider's availability, without opening anything.

        The probe stays cheap on purpose: a status read happens whenever the
        settings screen or the composer mounts, so it may not build a local model or
        open a cloud connection.  For a cloud engine the honest answer is therefore
        "a key is configured" -- reachability is learned when a dictation starts.
        """
        key = self._key_lookup(info.id) if info.needs_key else None
        configured = key is not None
        available = True
        reason: str | None = None
        if info.kind == "local":
            available, reason = local[0], local[1]
        elif info.needs_key and not configured:
            available = False
            reason = f"未配置 {info.label} 的 API Key"
        return SttProviderView(
            id=info.id,
            label=info.label,
            kind=info.kind,
            needs_key=info.needs_key,
            key_configured=configured,
            available=available,
            reason=reason,
            detail=info.detail,
        )

    def warm_up(self, *, engine: str, model_dir: str | None) -> SttStatusView:
        """Build the local models now and report the resulting status.

        Without this the whole build lands inside a live microphone -- measured at
        over a minute on a CPU -- and the button simply looks stuck.  The console
        asks for it from the action that needs it (pressing the microphone, or the
        settings screen's explicit "load now" button) rather than on mount: building
        models nobody asked for also blocked switching to another engine.  Idempotent:
        a warm engine answers from memory, and the same degradation boundary as
        :meth:`status` applies.
        """
        local = self._engine(model_dir)
        try:
            probe = local.warm_up()
        except Exception:  # noqa: BLE001 - degradation boundary for the optional extra
            return self._unavailable_view(engine, model_dir)
        return self._view(engine, probe)

    @staticmethod
    def _view(engine: str, probe: SttStatus) -> SttStatusView:
        return SttStatusView(
            available=probe.available,
            engine=engine,
            reason=probe.reason,
            loaded=probe.loaded,
            model_dir=probe.model_dir,
            sample_rate=STT_SAMPLE_RATE,
        )

    @staticmethod
    def _unavailable_view(engine: str, model_dir: str | None) -> SttStatusView:
        return SttStatusView(
            available=False,
            engine=engine,
            reason="本地语音引擎不可用",
            loaded=False,
            model_dir=model_dir or "",
            sample_rate=STT_SAMPLE_RATE,
        )

    # -- dictation lifecycle -----------------------------------------------

    def begin(self, session: SessionRef, *, engine: str, model_dir: str | None) -> SttBeginResult:
        """Start one dictation, replacing any dictation already open for the session.

        Idempotent per session by replacement: a second ``begin`` while one is
        open drops the previous stream (and its buffered audio) and starts fresh,
        so a double activation can never leave two streams on one microphone.
        """
        self._sessions.pop(session, None)
        info = provider_info(engine)
        if info is None:
            raise SttUnavailableError(f"未知的语音引擎：{engine}")
        if info.kind == "cloud":
            key = self._key_lookup(info.id)
            if key is None:
                raise SttUnavailableError(f"未配置 {info.label} 的 API Key")
            dictation = self._cloud_session(info.id, key)
            if dictation is None:
                raise SttUnavailableError(f"{info.label} 尚未实现")
        else:
            local = self._engine(model_dir)
            try:
                dictation = local.new_session()
            except SttUnavailable as exc:
                raise SttUnavailableError(exc.reason) from exc
        self._sessions[session] = dictation
        return SttBeginResult(sample_rate=STT_SAMPLE_RATE)

    @staticmethod
    def _cloud_session(provider_id: str, api_key: str) -> Any:
        """The client for one cloud provider, or None when it is not implemented.

        The single place that knows which hosted services exist: adding another one
        means one entry in ``synapse.stt.providers`` and one branch here, with no
        change to the wire, the service or the console.
        """
        if provider_id == "doubao":
            return DoubaoSession(api_key=api_key)
        return None

    def append(
        self, session: SessionRef, *, data_base64: str, model_dir: str | None
    ) -> SttAppendResult:
        """Decode one bounded PCM chunk and feed it to the session's dictation.

        A chunk with no open dictation is a no-op: the browser may race a cancel,
        and dropping audio is safer than failing a live microphone.
        """
        dictation = self._sessions.get(session)
        if dictation is None:
            return SttAppendResult(partial="", finalized=())
        samples = decode_stt_chunk(data_base64)
        update = dictation.accept(samples)
        return SttAppendResult(
            partial=update.partial,
            finalized=tuple(update.finalized),
            error=getattr(dictation, "error", None),
        )

    def finish(self, session: SessionRef, *, model_dir: str | None) -> SttFinishResult:
        """Flush the session's dictation, or return an empty result if none is open."""
        dictation = self._sessions.pop(session, None)
        if dictation is None:
            return SttFinishResult(finalized=())
        update = dictation.finish()
        return SttFinishResult(finalized=tuple(update.finalized))

    def cancel(self, session: SessionRef) -> SttCancelResult:
        """Drop the session's dictation (idempotent)."""
        return SttCancelResult(cancelled=self._sessions.pop(session, None) is not None)
