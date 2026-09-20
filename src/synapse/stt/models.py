"""Which files a local speech model set needs, and where that set lives.

The local engine is an optional extra (``stt-local``) whose models are a separate
download, so "speech input is unavailable" has several distinct causes: the extra
is not installed, the model directory does not exist, or one model file is
missing.  This module answers all of them with one message a reader can act on,
and it is the only place that knows the file names -- the engine asks it, so the
capability probe and the engine cannot disagree about what "available" means.

Two models, on purpose (see ``docs`` for the measurements):

- the **streaming** paraformer produces text while the reader is still speaking,
  which is what the composer's live caption shows;
- the **offline** paraformer re-decodes each finished sentence, which is what
  corrects the streaming text before it lands in the draft.

The VAD model is what decides where a sentence ends, i.e. when the second pass
runs at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ModelSet",
    "SttUnavailable",
    "default_model_dir",
    "resolve_model_set",
]

#: Model directory names inside a model root, as the sherpa-onnx model zoo ships them.
STREAMING_DIR = "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
OFFLINE_DIR = "sherpa-onnx-paraformer-zh-2023-09-14"
VAD_FILE = "silero_vad.onnx"

#: File names per role, in preference order: the int8 export is the one to use on
#: a CPU (a third of the size, several times faster), the fp32 export is accepted
#: so a reader who downloaded only that one is not told their models are wrong.
STREAMING_ENCODER = ("encoder.int8.onnx", "encoder.onnx")
STREAMING_DECODER = ("decoder.int8.onnx", "decoder.onnx")
OFFLINE_MODEL = ("model.int8.onnx", "model.onnx")

#: The sample rate every model here is trained on; the console resamples to it.
SAMPLE_RATE = 16000


class SttUnavailable(RuntimeError):
    """Speech input cannot run here, with the reason a reader can act on.

    Carried to the console instead of a stack trace: a missing extra and a missing
    model file are both ordinary states of a source checkout, not failures.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def default_model_dir() -> Path:
    """Where the models live unless the settings say otherwise.

    Beside the rest of this user's Synapse state (``~/.synapse``), because a model
    set is a per-user download that outlives any one checkout.
    """
    return Path.home() / ".synapse" / "stt" / "models"


@dataclass(frozen=True)
class ModelSet:
    """The resolved files of one complete model set."""

    root: Path
    streaming_encoder: Path
    streaming_decoder: Path
    streaming_tokens: Path
    offline_model: Path
    offline_tokens: Path
    vad: Path


def _find_file(directory: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def resolve_model_set(root: Path | None = None) -> ModelSet:
    """Resolve a model root into files, or explain what is missing.

    Raises:
        SttUnavailable: the directory or one of its files is absent.  The message
            names every missing file at once, so a reader fixes the set in one
            pass instead of one error per restart.
    """
    base = Path(root).expanduser() if root is not None else default_model_dir()
    if not base.is_dir():
        raise SttUnavailable(
            f"本地语音模型目录不存在：{base}（下载模型到该目录，或配置 synapse.stt.model_dir）"
        )

    streaming_dir = base / STREAMING_DIR
    offline_dir = base / OFFLINE_DIR
    missing: list[str] = []
    found: dict[str, Path] = {}

    def take(key: str, directory: Path, names: tuple[str, ...]) -> None:
        path = _find_file(directory, names)
        if path is None:
            missing.append(f"{directory.name}/{' 或 '.join(names)}")
        else:
            found[key] = path

    take("streaming_encoder", streaming_dir, STREAMING_ENCODER)
    take("streaming_decoder", streaming_dir, STREAMING_DECODER)
    take("streaming_tokens", streaming_dir, ("tokens.txt",))
    take("offline_model", offline_dir, OFFLINE_MODEL)
    take("offline_tokens", offline_dir, ("tokens.txt",))
    take("vad", base, (VAD_FILE,))
    if missing:
        raise SttUnavailable("本地语音模型不完整，缺少：" + "、".join(missing))

    return ModelSet(
        root=base,
        streaming_encoder=found["streaming_encoder"],
        streaming_decoder=found["streaming_decoder"],
        streaming_tokens=found["streaming_tokens"],
        offline_model=found["offline_model"],
        offline_tokens=found["offline_tokens"],
        vad=found["vad"],
    )
