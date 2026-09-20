"""Runtime local speech-to-text surface: DTOs, service, wire, and ACL.

Nothing here builds a real model: the service is driven by a fake engine, so what
is pinned is the wire shape, the bounded chunk, the dictation lifecycle, the
"never build on a status read" rule, and the off-event-loop decode.  The real
models stay behind ``tests/test_stt_smoke.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime import stt_config_persist
from synapse.runtime.service.access import (
    _REQUIRED_DELEGATE_METHODS,
    STT_CONTROL,
    AclAuthorizer,
    AclGrant,
    Principal,
    bind_access,
)
from synapse.runtime.service.errors import (
    InvalidRequestError,
    PermissionDeniedError,
    SttChunkTooLargeError,
    SttUnavailableError,
)
from synapse.runtime.service.local import LocalAgentRuntimeService
from synapse.runtime.service.stt import (
    MAX_STT_CHUNK_BASE64_CHARS,
    MAX_STT_CHUNK_BYTES,
    STT_SAMPLE_RATE,
    SttAppendCommand,
    SttAppendResult,
    SttBeginCommand,
    SttCancelCommand,
    SttFinishCommand,
    SttService,
    SttSetEngineCommand,
    SttStatusQuery,
    SttStatusView,
    SttWarmUpCommand,
    decode_stt_chunk,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, decode_params, dispatch
from synapse.stt.engine import LocalSttEngine, SttStatus
from synapse.stt.models import OFFLINE_DIR, STREAMING_DIR, SttUnavailable
from synapse.stt.session import SttUpdate

REF = SessionRef(project_id="p1", thread_id="t1")


# --- helpers ----------------------------------------------------------------


def _pcm_chunk(samples: list[int]) -> str:
    """Base64 int16 little-endian PCM, exactly what ``append`` accepts."""
    return base64.b64encode(struct.pack(f"<{len(samples)}h", *samples)).decode("ascii")


def _write_model_tree(root: Path) -> None:
    """A complete (empty) model set, so ``status`` resolves without building."""
    streaming = root / STREAMING_DIR
    offline = root / OFFLINE_DIR
    streaming.mkdir(parents=True, exist_ok=True)
    offline.mkdir(parents=True, exist_ok=True)
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "tokens.txt"):
        (streaming / name).write_bytes(b"x")
    for name in ("model.int8.onnx", "tokens.txt"):
        (offline / name).write_bytes(b"x")
    (root / "silero_vad.onnx").write_bytes(b"x")


class _FakeSession:
    """A scripted dictation: one partial per accept, a tail for finish."""

    def __init__(
        self,
        *,
        partials: list[str] | None = None,
        finalized: list[tuple[str, ...]] | None = None,
        finish_text: str = "",
    ) -> None:
        self._partials = list(partials or [])
        self._finalized = list(finalized or [])
        self._finish_text = finish_text
        self.accepted: list[list[float]] = []
        self.finished = False

    def accept(self, samples: list[float]) -> SttUpdate:
        self.accepted.append(list(samples))
        partial = self._partials.pop(0) if self._partials else ""
        finalized = self._finalized.pop(0) if self._finalized else ()
        return SttUpdate(partial=partial, finalized=tuple(finalized))

    def finish(self) -> SttUpdate:
        self.finished = True
        tail = (self._finish_text,) if self._finish_text else ()
        return SttUpdate(partial="", finalized=tail)


class _FakeEngine:
    """A local engine double: a scripted status and a queue of dictations."""

    def __init__(
        self,
        *,
        status: SttStatus | None = None,
        sessions: list[_FakeSession] | None = None,
        fail_begin: str | None = None,
    ) -> None:
        self._status = status or SttStatus(
            available=True, reason=None, model_dir="/models", loaded=False
        )
        self._sessions = list(sessions or [])
        self._fail_begin = fail_begin
        self.new_session_calls = 0
        self.created: list[_FakeSession] = []

    def status(self) -> SttStatus:
        return self._status

    def new_session(self) -> _FakeSession:
        self.new_session_calls += 1
        if self._fail_begin is not None:
            raise SttUnavailable(self._fail_begin)
        session = self._sessions.pop(0) if self._sessions else _FakeSession()
        self.created.append(session)
        return session


class _BrokenStatusEngine(_FakeEngine):
    """A local engine whose probe itself raises (a broken optional wheel)."""

    def status(self) -> SttStatus:
        raise RuntimeError("broken optional extra")


def _service(engine: _FakeEngine) -> SttService:
    return SttService(engine_factory=lambda model_dir: engine)


# --- decoding ---------------------------------------------------------------


def test_decode_scales_int16_little_endian_to_unit_floats() -> None:
    samples = decode_stt_chunk(_pcm_chunk([0, 32767, -32768, -1]))
    assert samples == pytest.approx([0.0, 32767 / 32768, -1.0, -1 / 32768])


def test_decode_refuses_a_chunk_over_the_byte_budget() -> None:
    # One sample over the decoded byte bound still fits the base64 character
    # bound, so this exercises the decoded-byte check, not the character check.
    oversized = base64.b64encode(bytes(MAX_STT_CHUNK_BYTES + 2)).decode("ascii")
    assert len(oversized) <= MAX_STT_CHUNK_BASE64_CHARS
    with pytest.raises(SttChunkTooLargeError):
        decode_stt_chunk(oversized)


def test_decode_refuses_a_chunk_over_the_base64_budget() -> None:
    with pytest.raises(SttChunkTooLargeError):
        decode_stt_chunk("A" * (MAX_STT_CHUNK_BASE64_CHARS + 4))


def test_decode_refuses_malformed_and_odd_length_chunks() -> None:
    with pytest.raises(InvalidRequestError):
        decode_stt_chunk("not base64!!")
    with pytest.raises(InvalidRequestError):
        decode_stt_chunk(base64.b64encode(b"abc").decode("ascii"))  # 3 bytes: not int16
    with pytest.raises(InvalidRequestError):
        decode_stt_chunk("")


def test_the_wire_sample_rate_mirrors_the_engine() -> None:
    from synapse.stt.models import SAMPLE_RATE

    assert STT_SAMPLE_RATE == SAMPLE_RATE == 16_000


# --- status -----------------------------------------------------------------


def test_status_reports_the_missing_extra_without_building(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("synapse.stt.engine.sherpa_onnx_version", lambda: None)
    service = SttService(
        engine_factory=lambda model_dir: LocalSttEngine(Path(model_dir) if model_dir else tmp_path)
    )
    view = service.status(engine="local", model_dir=str(tmp_path))
    assert view.available is False
    assert view.reason is not None and "stt-local" in view.reason
    assert view.loaded is False
    assert view.model_dir == str(tmp_path)
    assert view.engine == "local"
    assert view.sample_rate == STT_SAMPLE_RATE


def test_status_is_available_for_a_complete_model_set_without_building(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("synapse.stt.engine.sherpa_onnx_version", lambda: "1.13.8")
    _write_model_tree(tmp_path)
    service = SttService(
        engine_factory=lambda model_dir: LocalSttEngine(Path(model_dir) if model_dir else tmp_path)
    )
    view = service.status(engine="browser", model_dir=str(tmp_path))
    assert view.available is True
    assert view.reason is None
    assert view.loaded is False, "a status read must never build the models"
    assert view.model_dir == str(tmp_path)
    assert view.engine == "browser"


def test_status_never_builds_a_dictation() -> None:
    engine = _FakeEngine()
    view = _service(engine).status(engine="local", model_dir=None)
    assert view.available is True
    assert view.loaded is False
    assert view.model_dir == "/models"
    assert engine.new_session_calls == 0


def test_status_degrades_when_the_probe_itself_raises() -> None:
    view = _service(_BrokenStatusEngine()).status(engine="local", model_dir=None)
    assert view.available is False
    assert view.reason is not None
    assert view.sample_rate == STT_SAMPLE_RATE


# --- dictation lifecycle ----------------------------------------------------


def test_begin_append_finish_cancel_happy_path() -> None:
    session = _FakeSession(
        partials=["你", "你好"],
        finalized=[(), ("你好。",)],
        finish_text="结束。",
    )
    engine = _FakeEngine(sessions=[session])
    service = _service(engine)

    begun = service.begin(REF, engine="local", model_dir=None)
    assert begun.sample_rate == STT_SAMPLE_RATE

    chunk = _pcm_chunk([1, 2, 3])
    first = service.append(REF, data_base64=chunk, model_dir=None)
    assert first.partial == "你"
    assert first.finalized == ()
    second = service.append(REF, data_base64=chunk, model_dir=None)
    assert second.partial == "你好"
    assert second.finalized == ("你好。",)
    assert session.accepted[0] == pytest.approx([1 / 32768, 2 / 32768, 3 / 32768])

    finished = service.finish(REF, model_dir=None)
    assert finished.finalized == ("结束。",)
    assert session.finished is True

    assert service.cancel(REF).cancelled is False  # already finished
    assert engine.new_session_calls == 1


def test_begin_replaces_an_open_dictation() -> None:
    first = _FakeSession(partials=["first"])
    second = _FakeSession(partials=["second"])
    engine = _FakeEngine(sessions=[first, second])
    service = _service(engine)

    service.begin(REF, engine="local", model_dir=None)
    service.begin(REF, engine="local", model_dir=None)
    update = service.append(REF, data_base64=_pcm_chunk([0]), model_dir=None)
    assert update.partial == "second"
    assert first.accepted == []  # the replaced dictation never saw audio
    assert engine.new_session_calls == 2


def test_begin_reports_unavailable_when_the_models_cannot_be_built() -> None:
    engine = _FakeEngine(fail_begin="本地语音模型目录不存在")
    with pytest.raises(SttUnavailableError):
        _service(engine).begin(REF, engine="local", model_dir=None)


def test_finish_cancel_and_append_without_begin_are_noops() -> None:
    engine = _FakeEngine()
    service = _service(engine)

    assert service.finish(REF, model_dir=None).finalized == ()
    assert service.cancel(REF).cancelled is False
    assert service.append(REF, data_base64=_pcm_chunk([1]), model_dir=None).partial == ""
    assert engine.new_session_calls == 0, "a no-op must not build the engine"


def test_cancel_drops_the_open_dictation_and_is_idempotent() -> None:
    session = _FakeSession()
    service = _service(_FakeEngine(sessions=[session]))
    service.begin(REF, engine="local", model_dir=None)
    assert service.cancel(REF).cancelled is True
    assert service.cancel(REF).cancelled is False
    assert service.finish(REF, model_dir=None).finalized == ()


def test_append_refuses_an_oversized_chunk_for_an_open_dictation() -> None:
    service = _service(_FakeEngine(sessions=[_FakeSession()]))
    service.begin(REF, engine="local", model_dir=None)
    oversized = base64.b64encode(bytes(MAX_STT_CHUNK_BYTES + 2)).decode("ascii")
    with pytest.raises(SttChunkTooLargeError):
        service.append(REF, data_base64=oversized, model_dir=None)


# --- wire -------------------------------------------------------------------


_WIRE = {"session": {"project_id": "p1", "thread_id": "t1"}}


def test_decode_params_routes_every_stt_method() -> None:
    assert isinstance(decode_params("runtime.stt.status", dict(_WIRE)), SttStatusQuery)
    assert isinstance(decode_params("runtime.stt.begin", dict(_WIRE)), SttBeginCommand)
    assert isinstance(decode_params("runtime.stt.finish", dict(_WIRE)), SttFinishCommand)
    assert isinstance(decode_params("runtime.stt.cancel", dict(_WIRE)), SttCancelCommand)

    append = decode_params("runtime.stt.append", {**_WIRE, "data_base64": _pcm_chunk([1])})
    assert isinstance(append, SttAppendCommand)
    assert append.data_base64 == _pcm_chunk([1])


def test_decode_params_bounds_the_append_chunk() -> None:
    with pytest.raises(ProtocolError):
        decode_params("runtime.stt.append", dict(_WIRE))  # data_base64 is required
    with pytest.raises(ProtocolError):
        decode_params(
            "runtime.stt.append",
            {**_WIRE, "data_base64": "A" * (MAX_STT_CHUNK_BASE64_CHARS + 4)},
        )


def test_dispatch_routes_every_stt_method() -> None:
    class _WireService:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_stt_status(self, query: Any) -> str:
            self.calls.append("status")
            return "status"

        async def begin_stt_dictation(self, command: Any) -> str:
            self.calls.append("begin")
            return "begin"

        async def append_stt_audio(self, command: Any) -> str:
            self.calls.append("append")
            return "append"

        async def finish_stt_dictation(self, command: Any) -> str:
            self.calls.append("finish")
            return "finish"

        async def cancel_stt_dictation(self, command: Any) -> str:
            self.calls.append("cancel")
            return "cancel"

    service = _WireService()
    append_wire = {**_WIRE, "data_base64": _pcm_chunk([1])}
    assert asyncio.run(dispatch(service, "runtime.stt.status", dict(_WIRE))) == "status"
    assert asyncio.run(dispatch(service, "runtime.stt.begin", dict(_WIRE))) == "begin"
    assert asyncio.run(dispatch(service, "runtime.stt.append", append_wire)) == "append"
    assert asyncio.run(dispatch(service, "runtime.stt.finish", dict(_WIRE))) == "finish"
    assert asyncio.run(dispatch(service, "runtime.stt.cancel", dict(_WIRE))) == "cancel"
    assert service.calls == ["status", "begin", "append", "finish", "cancel"]


# --- authorization ----------------------------------------------------------


class _SttDelegate:
    """A delegate exposing the required set plus the five optional stt methods."""

    def __init__(self) -> None:
        self.calls: list[str] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def get_stt_status(query: Any) -> str:
            self.calls.append("status")
            return "status"

        async def begin_stt_dictation(command: Any) -> str:
            self.calls.append("begin")
            return "begin"

        async def append_stt_audio(command: Any) -> str:
            self.calls.append("append")
            return "append"

        async def finish_stt_dictation(command: Any) -> str:
            self.calls.append("finish")
            return "finish"

        async def cancel_stt_dictation(command: Any) -> str:
            self.calls.append("cancel")
            return "cancel"

        self.get_stt_status = get_stt_status  # type: ignore[method-assign]
        self.begin_stt_dictation = begin_stt_dictation  # type: ignore[method-assign]
        self.append_stt_audio = append_stt_audio  # type: ignore[method-assign]
        self.finish_stt_dictation = finish_stt_dictation  # type: ignore[method-assign]
        self.cancel_stt_dictation = cancel_stt_dictation  # type: ignore[method-assign]


class _OldDelegate:
    """A delegate from before this feature: none of the five methods exists."""

    def __init__(self) -> None:
        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)


def _authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer([AclGrant("subject-a", REF.project_id, frozenset(capabilities), None)])


def test_every_stt_method_needs_stt_control() -> None:
    delegate = _SttDelegate()
    principal = Principal("subject-a")

    # A grant of a neighbouring capability must not authorize the stt surface.
    denied = bind_access(delegate, principal, _authorizer("session.read"))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.get_stt_status(SttStatusQuery(session=REF)))
    assert delegate.calls == []

    allowed = bind_access(delegate, principal, _authorizer(STT_CONTROL))
    assert asyncio.run(allowed.get_stt_status(SttStatusQuery(session=REF))) == "status"
    assert asyncio.run(allowed.begin_stt_dictation(SttBeginCommand(session=REF))) == "begin"
    assert asyncio.run(allowed.cancel_stt_dictation(SttCancelCommand(session=REF))) == "cancel"


def test_old_delegate_reports_stt_unavailable() -> None:
    service = bind_access(_OldDelegate(), Principal("subject-a"), _authorizer(STT_CONTROL))
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.get_stt_status(SttStatusQuery(session=REF)))


# --- local service wiring ---------------------------------------------------


class _SttManager(RuntimeManager):
    """A manager that resolves any ref to a settings-only stub session."""

    def get_session_ref(self, ref: SessionRef) -> Any:
        del ref
        return SimpleNamespace(settings=self.settings)


def _local_service(
    stt: Any,
    *,
    engine: Any = None,
    model_dir: Any = None,
    mapping: Any = None,
) -> LocalAgentRuntimeService:
    """Build the local service over a stub manager.

    ``engine``/``model_dir`` populate the flat speech settings; ``mapping`` replaces
    the whole settings object with a dict-like one, which is the shape a
    ``settings.json``-sourced object can arrive in.
    """
    settings = (
        mapping
        if mapping is not None
        else SimpleNamespace(
            max_concurrency=2, model="test", stt_engine=engine, stt_model_dir=model_dir
        )
    )
    manager = _SttManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=REF.project_id,
    )
    return LocalAgentRuntimeService(
        lambda project_id: manager if project_id == REF.project_id else None,
        stt_service=stt,
    )


def test_local_service_reads_the_configured_engine_and_model_dir() -> None:
    calls: list[tuple[str, str | None]] = []

    class _Spy:
        def status(self, *, engine: str, model_dir: str | None) -> SttStatusView:
            calls.append((engine, model_dir))
            return SttStatusView(
                available=True,
                engine=engine,
                reason=None,
                loaded=False,
                model_dir=model_dir or "/default",
                sample_rate=STT_SAMPLE_RATE,
            )

    service = _local_service(
        _Spy(), engine="local", model_dir="/models/stt"
    )
    view = asyncio.run(service.get_stt_status(SttStatusQuery(session=REF)))
    assert calls == [("local", "/models/stt")]
    assert view.engine == "local"
    assert view.model_dir == "/models/stt"


def test_local_service_defaults_an_unknown_engine_to_browser() -> None:
    calls: list[tuple[str, str | None]] = []

    class _Spy:
        def status(self, *, engine: str, model_dir: str | None) -> SttStatusView:
            calls.append((engine, model_dir))
            return SttStatusView(
                available=False,
                engine=engine,
                reason=None,
                loaded=False,
                model_dir="",
                sample_rate=STT_SAMPLE_RATE,
            )

    service = _local_service(_Spy(), engine="not-a-mode", model_dir="")
    asyncio.run(service.get_stt_status(SttStatusQuery(session=REF)))
    assert calls == [("browser", None)]


def test_local_service_reads_a_mapping_stt_section() -> None:
    calls: list[tuple[str, str | None]] = []

    class _Spy:
        def status(self, *, engine: str, model_dir: str | None) -> SttStatusView:
            calls.append((engine, model_dir))
            return SttStatusView(
                available=False,
                engine=engine,
                reason=None,
                loaded=False,
                model_dir="",
                sample_rate=STT_SAMPLE_RATE,
            )

    service = _local_service(_Spy(), mapping={"stt_engine": "local", "stt_model_dir": "/from/dict"})
    asyncio.run(service.get_stt_status(SttStatusQuery(session=REF)))
    assert calls == [("local", "/from/dict")]


def test_local_service_reports_unavailable_without_a_service() -> None:
    service = LocalAgentRuntimeService(lambda project_id: None)
    with pytest.raises(SttUnavailableError):
        asyncio.run(service.get_stt_status(SttStatusQuery(session=REF)))


def test_append_runs_off_the_event_loop() -> None:
    threads: list[threading.Thread] = []

    class _Spy:
        def append(
            self, session: SessionRef, *, data_base64: str, model_dir: str | None
        ) -> SttAppendResult:
            threads.append(threading.current_thread())
            return SttAppendResult(partial="ok", finalized=())

    service = _local_service(_Spy(), engine="local", model_dir=None)
    command = SttAppendCommand(session=REF, data_base64=_pcm_chunk([0]))
    result = asyncio.run(service.append_stt_audio(command))

    assert result.partial == "ok"
    assert threads, "append must reach the service"
    assert threads[0] is not threading.main_thread(), "the decode must leave the event loop"


def test_warm_up_builds_the_configured_models_off_the_event_loop() -> None:
    """The build is asked for up front, so the first dictation is not the one that pays."""
    calls: list[tuple[str, str | None]] = []
    threads: list[threading.Thread] = []

    class _Spy:
        def warm_up(self, *, engine: str, model_dir: str | None) -> SttStatusView:
            calls.append((engine, model_dir))
            threads.append(threading.current_thread())
            return SttStatusView(
                available=True,
                engine=engine,
                reason=None,
                loaded=True,
                model_dir=model_dir or "",
                sample_rate=STT_SAMPLE_RATE,
            )

    service = _local_service(_Spy(), engine="local", model_dir="/models/stt")
    view = asyncio.run(service.warm_up_stt_models(SttWarmUpCommand(session=REF)))

    assert calls == [("local", "/models/stt")]
    assert threads[0] is not threading.main_thread(), "the build must leave the event loop"
    assert view.engine == "local"
    assert view.available is True
    assert view.loaded is True


def test_a_broken_engine_degrades_the_warm_up_instead_of_raising() -> None:
    # The warm-up is a convenience: a broken optional wheel must not turn it into an
    # error the console cannot render, it must report why local speech is unavailable.
    class _Engine:
        def status(self) -> SttStatus:
            raise RuntimeError("broken wheel")

        def warm_up(self) -> SttStatus:
            raise RuntimeError("broken wheel")

        def new_session(self) -> SttUpdate:  # pragma: no cover - never reached
            raise RuntimeError("broken wheel")

    service = SttService(engine_factory=lambda model_dir: _Engine())
    view = service.warm_up(engine="local", model_dir=None)

    assert view.available is False
    assert view.reason
    assert view.loaded is False


def test_set_engine_persists_and_answers_with_the_effective_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One round trip: the choice is stored, applied live, and read back as truth."""
    monkeypatch.setattr(stt_config_persist, "user_config_dir", lambda: tmp_path)
    calls: list[tuple[str, str | None]] = []

    class _Spy:
        def status(self, *, engine: str, model_dir: str | None) -> SttStatusView:
            calls.append((engine, model_dir))
            return SttStatusView(
                available=True,
                engine=engine,
                reason=None,
                loaded=False,
                model_dir=model_dir or "",
                sample_rate=STT_SAMPLE_RATE,
            )

    service = _local_service(_Spy(), engine="browser", model_dir=None)
    view = asyncio.run(
        service.set_stt_engine(
            SttSetEngineCommand(session=REF, engine="local", model_dir="/models/stt")
        )
    )

    assert calls == [("local", str(Path("/models/stt")))], (
        "the status is read after the change, not echoed"
    )
    assert view.engine == "local"
    written = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert written["stt_engine"] == "local"
    assert written["stt_model_dir"] == str(Path("/models/stt"))


def test_set_engine_rejects_an_unknown_mode() -> None:
    service = _local_service(object(), engine="browser", model_dir=None)
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            service.set_stt_engine(SttSetEngineCommand(session=REF, engine="quantum"))
        )


def test_set_engine_rejects_an_unbounded_model_dir() -> None:
    service = _local_service(object(), engine="browser", model_dir=None)
    with pytest.raises(InvalidRequestError):
        asyncio.run(
            service.set_stt_engine(
                SttSetEngineCommand(session=REF, engine="local", model_dir="x" * 5000)
            )
        )


def test_set_engine_surfaces_a_refused_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An unreadable settings file is the reader's to fix, so the reason has to reach
    # the console instead of the write being skipped silently.
    monkeypatch.setattr(stt_config_persist, "user_config_dir", lambda: tmp_path)
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    service = _local_service(object(), engine="browser", model_dir=None)
    with pytest.raises(InvalidRequestError) as raised:
        asyncio.run(
            service.set_stt_engine(SttSetEngineCommand(session=REF, engine="local"))
        )
    assert "refusing to overwrite" in str(raised.value)
