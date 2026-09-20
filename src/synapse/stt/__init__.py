"""Local speech-to-text: the offline ONNX engine behind the composer's microphone.

An optional capability, not a core one.  The console always has the browser's own
recognizer, so this package exists to be *better* than it, and it must never be
the reason the app fails to start:

- ``sherpa-onnx`` is an extra (``uv sync --extra stt-local``) and is imported only
  when a dictation actually starts;
- the models are a separate download, and their absence is reported as a reason
  rather than raised as a crash;
- nothing here is wired into the application until a settings value asks for it.

See :mod:`synapse.stt.models` for what a model set is, :mod:`synapse.stt.engine`
for the warm process, and :mod:`synapse.stt.session` for the two-pass decoding
that turns live text into corrected text.
"""

from __future__ import annotations

from synapse.stt.engine import LocalSttEngine, SttStatus, sherpa_onnx_version
from synapse.stt.models import ModelSet, SttUnavailable, default_model_dir, resolve_model_set
from synapse.stt.session import SttSession, SttUpdate

__all__ = [
    "LocalSttEngine",
    "ModelSet",
    "SttSession",
    "SttStatus",
    "SttUnavailable",
    "SttUpdate",
    "default_model_dir",
    "resolve_model_set",
    "sherpa_onnx_version",
]
