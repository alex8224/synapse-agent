"""End-to-end check of the local engine against the real models.

Opt-in, because it is the one test here that loads ~460 MB of ONNX models and
takes about a minute to build them -- too slow for the normal suite, and it needs
a model set that a checkout does not have by default:

    uv sync --extra stt-local
    # models under ~/.synapse/stt/models (see synapse.stt.models)
    $env:SYNAPSE_STT_SMOKE = "1"
    uv run --no-sync pytest tests/test_stt_smoke.py -q -s

What it proves is the contract the console depends on, on real audio: live text
appears while the audio is still being fed, a sentence ends, and the offline pass
*replaces* that sentence with its own reading of it.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from synapse.stt.engine import LocalSttEngine
from synapse.stt.models import SAMPLE_RATE

FIXTURES = Path(__file__).parent / "fixtures" / "stt"
CLIP = FIXTURES / "speech.wav"
EXPECTED = (FIXTURES / "speech.txt").read_text(encoding="utf-8").strip()

pytestmark = pytest.mark.skipif(
    os.environ.get("SYNAPSE_STT_SMOKE") != "1",
    reason="loads the real models (~1 min); set SYNAPSE_STT_SMOKE=1 to run",
)


def _read_clip() -> list[float]:
    import array

    with wave.open(str(CLIP), "rb") as handle:
        assert handle.getframerate() == SAMPLE_RATE
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    return [value / 32768.0 for value in samples]


def test_the_two_pass_engine_transcribes_real_audio() -> None:
    engine = LocalSttEngine()
    status = engine.warm_up()
    if not status.available:
        pytest.skip(f"no usable local model set: {status.reason}")

    audio = _read_clip()
    session = engine.new_session()
    chunk = int(SAMPLE_RATE * 0.6)  # the streaming model's own 600 ms granularity

    partials: list[str] = []
    finalized: list[str] = []
    for start in range(0, len(audio), chunk):
        update = session.accept(audio[start : start + chunk])
        if update.partial:
            partials.append(update.partial)
        finalized.extend(update.finalized)
    update = session.finish()
    finalized.extend(update.finalized)

    print(f"\npartials: {partials}")
    print(f"finalized: {finalized}")

    assert partials, "the streaming pass must produce live text, that is what the caption shows"
    # The first partial is a prefix of the last one: the streaming model appends,
    # it does not rewrite -- which is exactly why the second pass exists.
    assert partials[-1].startswith(partials[0])
    assert finalized, "the second pass must close at least one sentence"

    corrected = "".join(finalized)
    assert "重复提交" in corrected, corrected
    # The English terms are the reason this feature is worth having: the browser's
    # recognizer and the streaming model both mangle them.
    assert "attach" in corrected.lower(), corrected
    # The correction must stay close to what was actually said.
    assert _similarity(EXPECTED, corrected) > 0.7, corrected


def _similarity(reference: str, hypothesis: str) -> float:
    """1 - character error rate, so the assertion reads as "mostly the same"."""
    import re

    def normalize(text: str) -> str:
        return re.sub(r"[\s，。、？！,.?!:；;]", "", text).lower()

    ref, hyp = normalize(reference), normalize(hypothesis)
    previous = list(range(len(hyp) + 1))
    for i, rchar in enumerate(ref, start=1):
        current = [i]
        for j, hchar in enumerate(hyp, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (rchar != hchar))
            )
        previous = current
    return 1 - previous[-1] / len(ref)
