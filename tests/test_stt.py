"""Offline tests for the local speech engine's rules.

No model is loaded here: the session runs against fakes, so what is pinned is the
*shape* of the two-pass contract -- what is provisional, what is authoritative,
when the second pass runs, and what happens when it finds nothing.  The real
models are covered by `tests/test_stt_smoke.py`, which is opt-in because building
them costs about a minute.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from synapse.stt.engine import LocalSttEngine
from synapse.stt.models import (
    OFFLINE_DIR,
    STREAMING_DIR,
    SttUnavailable,
    default_model_dir,
    resolve_model_set,
)
from synapse.stt.session import SttSession, SttUpdate

# --- model set resolution ----------------------------------------------------


def _write_model_tree(
    root: Path,
    *,
    encoder: str = "encoder.int8.onnx",
    decoder: str = "decoder.int8.onnx",
    offline: str = "model.int8.onnx",
) -> None:
    streaming_dir = root / STREAMING_DIR
    offline_dir = root / OFFLINE_DIR
    streaming_dir.mkdir(parents=True, exist_ok=True)
    offline_dir.mkdir(parents=True, exist_ok=True)
    for name in (encoder, decoder, "tokens.txt"):
        (streaming_dir / name).write_bytes(b"x")
    for name in (offline, "tokens.txt"):
        (offline_dir / name).write_bytes(b"x")
    (root / "silero_vad.onnx").write_bytes(b"x")


def test_a_complete_int8_set_resolves(tmp_path: Path) -> None:
    _write_model_tree(tmp_path)
    models = resolve_model_set(tmp_path)
    assert models.streaming_encoder.name == "encoder.int8.onnx"
    assert models.streaming_decoder.name == "decoder.int8.onnx"
    assert models.offline_model.name == "model.int8.onnx"
    assert models.vad.name == "silero_vad.onnx"
    assert models.root == tmp_path


def test_the_fp32_export_is_accepted_when_it_is_all_there_is(tmp_path: Path) -> None:
    # A reader who downloaded only the fp32 export must not be told the set is
    # broken: int8 is a preference, not a requirement.
    _write_model_tree(
        tmp_path, encoder="encoder.onnx", decoder="decoder.onnx", offline="model.onnx"
    )
    models = resolve_model_set(tmp_path)
    assert models.streaming_encoder.name == "encoder.onnx"
    assert models.streaming_decoder.name == "decoder.onnx"
    assert models.offline_model.name == "model.onnx"


def test_a_missing_directory_says_where_the_models_belong() -> None:
    with pytest.raises(SttUnavailable) as raised:
        resolve_model_set(Path("definitely/not/here"))
    assert "本地语音模型目录不存在" in raised.value.reason
    assert "definitely" in raised.value.reason


def test_every_missing_file_is_named_at_once(tmp_path: Path) -> None:
    # One restart per missing file is the failure mode this avoids.
    (tmp_path / "silero_vad.onnx").write_bytes(b"x")
    with pytest.raises(SttUnavailable) as raised:
        resolve_model_set(tmp_path)
    reason = raised.value.reason
    assert "encoder.int8.onnx" in reason
    assert "model.int8.onnx" in reason
    assert "tokens.txt" in reason


def test_the_default_model_dir_is_per_user_state() -> None:
    assert default_model_dir() == Path.home() / ".synapse" / "stt" / "models"


# --- the engine's availability answer ---------------------------------------


def test_status_reports_the_missing_extra_without_importing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("synapse.stt.engine.sherpa_onnx_version", lambda: None)
    status = LocalSttEngine(Path("nowhere")).status()
    assert status.available is False
    assert status.reason is not None and "stt-local" in status.reason
    assert status.loaded is False


def test_status_reports_incomplete_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("synapse.stt.engine.sherpa_onnx_version", lambda: "1.13.8")
    status = LocalSttEngine(tmp_path).status()
    assert status.available is False
    assert status.reason is not None and "模型不完整" in status.reason


def test_status_is_available_for_a_complete_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("synapse.stt.engine.sherpa_onnx_version", lambda: "1.13.8")
    _write_model_tree(tmp_path)
    status = LocalSttEngine(tmp_path).status()
    assert status.available is True
    assert status.reason is None
    assert status.loaded is False, "probing must not build the models"


# --- the two-pass session ----------------------------------------------------


class _FakeStream:
    """The streaming side's per-session state: what it was fed, and whether it was flushed."""

    def __init__(self) -> None:
        self.samples: list[float] = []
        self.finished = False

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        self.samples.extend(samples)

    def input_finished(self) -> None:
        self.finished = True


class _FakeStreaming:
    """Hands out one scripted partial per `accept` (and one for `finish`)."""

    def __init__(self, partials: list[str]) -> None:
        self.partials = list(partials)
        self.resets = 0

    def create_stream(self) -> _FakeStream:
        return _FakeStream()

    def is_ready(self, stream: _FakeStream) -> bool:
        return False

    def decode_stream(self, stream: _FakeStream) -> None:  # pragma: no cover - never ready
        raise AssertionError("the fake is never ready")

    def get_result(self, stream: _FakeStream) -> str:
        return self.partials.pop(0) if self.partials else ""

    def reset(self, stream: _FakeStream) -> None:
        self.resets += 1


class _FakeOfflineResult:
    """The real API exposes the text as `stream.result.text`."""

    def __init__(self) -> None:
        self.text = ""


class _FakeOfflineStream:
    def __init__(self) -> None:
        self.samples: list[float] = []
        self.result = _FakeOfflineResult()

    def accept_waveform(self, sample_rate: int, samples: list[float]) -> None:
        self.samples.extend(samples)


class _FakeOffline:
    """Answers with one scripted sentence per re-decode, and records its input."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.decoded: list[list[float]] = []

    def create_stream(self) -> _FakeOfflineStream:
        return _FakeOfflineStream()

    def decode_stream(self, stream: _FakeOfflineStream) -> None:
        self.decoded.append(list(stream.samples))
        stream.result.text = self.answers.pop(0) if self.answers else ""


class _FakeVad:
    """Holds sentences that are already finished; `accept` only records that it ran."""

    def __init__(self, sentences: list[list[float]]) -> None:
        self.queue = [list(sentence) for sentence in sentences]
        self.flushed = 0

    def accept_waveform(self, samples: list[float]) -> None:
        return None

    @property
    def empty(self) -> bool:
        return not self.queue

    @property
    def front(self) -> SimpleNamespace:
        return SimpleNamespace(samples=self.queue[0])

    def pop(self) -> None:
        self.queue.pop(0)

    def flush(self) -> None:
        self.flushed += 1

    def reset(self) -> None:
        self.queue.clear()


class _FakeModels:
    """What `SttSession` needs: two recognizers and a per-session VAD."""

    def __init__(
        self, partials: list[str], answers: list[str], sentences: list[list[float]]
    ) -> None:
        self.streaming = _FakeStreaming(partials)
        self.offline = _FakeOffline(answers)
        self.vad = _FakeVad(sentences)

    def create_vad(self) -> _FakeVad:
        return self.vad


def test_live_text_is_partial_and_never_finalized() -> None:
    models = _FakeModels(["帮我检查", "帮我检查一下"], [], [])
    session = SttSession(models)
    session.accept([0.1] * 10)
    update = session.accept([0.1] * 10)
    # The newest partial is what the caption shows; nothing is authoritative yet.
    assert update == SttUpdate(partial="帮我检查一下", finalized=())


def test_a_finished_sentence_is_corrected_by_the_offline_model() -> None:
    models = _FakeModels(["帮我检查一下这个函数"], ["帮我检查一下这个函数。"], [[0.5] * 8])
    session = SttSession(models)
    update = session.accept([0.1] * 10)
    assert update.finalized == ("帮我检查一下这个函数。",)
    assert update.partial == "", (
        "the corrected sentence replaces the live text, it does not sit beside it"
    )
    assert models.offline.decoded == [[0.5] * 8], "the second pass decodes the sentence the VAD cut"
    assert models.streaming.resets == 1, "the next sentence starts from a clean stream"


def test_an_empty_second_pass_keeps_what_the_live_pass_heard() -> None:
    # A fragment the offline model scores as nothing must not delete the words the
    # streaming model already produced.
    models = _FakeModels(["你"], [""], [[0.5] * 8])
    session = SttSession(models)
    assert session.accept([0.1] * 10).finalized == ("你",)


def test_finish_flushes_the_vad_and_reports_the_last_sentence() -> None:
    models = _FakeModels(["最后一句"], ["最后一句。"], [[0.5] * 8])
    session = SttSession(models)
    update = session.finish()
    assert models.vad.flushed == 1
    assert update == SttUpdate(partial="", finalized=("最后一句。",))


def test_finish_keeps_the_streaming_tail_when_no_sentence_closed() -> None:
    # A very short utterance may never cross the VAD's speech threshold; the
    # streaming model still heard it.
    models = _FakeModels(["你好"], [], [])
    session = SttSession(models)
    assert session.finish().finalized == ("你好",)


def test_reset_forgets_the_buffered_sentences() -> None:
    models = _FakeModels([], [], [[0.5] * 8])
    session = SttSession(models)
    session.reset()
    assert models.vad.empty is True


def test_an_empty_chunk_changes_nothing() -> None:
    models = _FakeModels(["帮我"], [], [])
    session = SttSession(models)
    assert session.accept([]) == SttUpdate(partial="", finalized=())
    assert models.streaming.partials == ["帮我"], "an empty chunk must not consume a decode"
