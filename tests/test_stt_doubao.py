"""Tests for the Doubao (Volcengine) cloud engine.

The framing cases are transcriptions of frames observed on the **live service**,
including the one that is not in the prose documentation: a response whose flags
carry bit ``0b0001`` puts a sequence number between the header and the payload size,
and reading it as the size makes every response look like a corrupt payload.

The routing cases cover the daemon side: which provider is reported as available,
and what a dictation does when the key is missing.
"""

from __future__ import annotations

import gzip
import struct

import pytest

from synapse.runtime.service.errors import SttUnavailableError
from synapse.runtime.service.stt import SttService
from synapse.stt.doubao import (
    AUDIO_ONLY_REQUEST,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    SERVER_ERROR,
    build_audio_request,
    build_full_client_request,
    definite_sentences,
    parse_response,
    response_text,
)

# --- framing -----------------------------------------------------------------


def test_the_config_frame_is_a_json_full_client_request() -> None:
    frame = build_full_client_request({"audio": {"format": "pcm"}})
    assert frame[0] == 0x11, "protocol version 1, header size 4 bytes"
    assert frame[1] >> 4 == FULL_CLIENT_REQUEST
    assert frame[1] & 0x0F == 0x00, "no sequence on the first frame"
    assert frame[2] == 0x11, "JSON payload, gzip compressed"
    size = struct.unpack(">I", frame[4:8])[0]
    assert size == len(frame) - 8
    assert gzip.decompress(frame[8:]) == b'{"audio": {"format": "pcm"}}'


def test_the_last_audio_frame_carries_the_last_packet_flag() -> None:
    mid = build_audio_request(b"\x00\x01" * 10, last=False)
    last = build_audio_request(b"\x00\x01" * 10, last=True)
    assert mid[1] >> 4 == AUDIO_ONLY_REQUEST
    assert mid[1] & 0x0F == 0x00
    assert last[1] & 0x0F == 0x02, "0b0010 marks the last packet"


def test_a_response_without_a_sequence_is_read_as_size_then_payload() -> None:
    # Observed: `11 90 10 00 | 00 00 00 48 | {"result": ...}` -- uncompressed.
    payload = b'{"result": {"text": "\xe4\xbd\xa0\xe5\xa5\xbd"}}'
    frame = bytes((0x11, 0x90, 0x10, 0x00)) + struct.pack(">I", len(payload)) + payload
    response = parse_response(frame)
    assert response.message_type == FULL_SERVER_RESPONSE
    assert response.payload == {"result": {"text": "你好"}}
    assert response_text(response.payload or {}) == "你好"


def test_a_response_with_a_sequence_reads_it_before_the_size() -> None:
    # Observed: `11 91 10 00 | 00 00 00 01 | 00 00 01 44 | {"...}`
    # The sequence sits between the header and the payload size; skipping it is what
    # makes every streamed response parseable at all.
    payload = b'{"result": {"text": "\xe4\xbd\xa0"}}'
    frame = (
        bytes((0x11, 0x91, 0x10, 0x00))
        + struct.pack(">I", 1)
        + struct.pack(">I", len(payload))
        + payload
    )
    response = parse_response(frame)
    assert response.flags & 0x01, "the sequence flag is preserved"
    assert response_text(response.payload or {}) == "你"


def test_a_gzipped_response_is_decompressed() -> None:
    payload = gzip.compress(b'{"result": {"text": "ok"}}')
    frame = bytes((0x11, 0x90, 0x11, 0x00)) + struct.pack(">I", len(payload)) + payload
    assert parse_response(frame).payload == {"result": {"text": "ok"}}


def test_an_error_frame_carries_code_size_and_message() -> None:
    # Observed: `11 f0 10 00 | <code> | <size> | {"error": "..."}`
    message = b'{"error": "[Timeout waiting next packet]"}'
    frame = (
        bytes((0x11, 0xF0, 0x10, 0x00))
        + struct.pack(">I", 45000001)
        + struct.pack(">I", len(message))
        + message
    )
    response = parse_response(frame)
    assert response.message_type == SERVER_ERROR
    assert response.error_code == 45000001
    assert "Timeout waiting next packet" in (response.error_message or "")


def test_a_truncated_frame_is_refused_instead_of_guessed() -> None:
    with pytest.raises(ValueError):
        parse_response(b"\x11\x90")
    with pytest.raises(ValueError):
        parse_response(bytes((0x11, 0x90, 0x10, 0x00)))


# --- the definite/final mapping ----------------------------------------------


def test_only_definite_utterances_are_final() -> None:
    payload = {
        "result": {
            "text": "你好世界",
            "utterances": [
                {"text": "你好", "definite": True},
                {"text": "世界", "definite": False},
            ],
        }
    }
    assert definite_sentences(payload) == ["你好"]
    assert response_text(payload) == "你好世界"


def test_a_payload_without_utterances_still_yields_text() -> None:
    assert definite_sentences({"result": {"text": "你好"}}) == []
    assert response_text({"result": {"text": "你好"}}) == "你好"
    assert response_text({}) == ""


# --- provider routing --------------------------------------------------------


def test_every_registered_provider_is_selectable() -> None:
    """The list the wire validates against must *be* the registry.

    A hard-coded copy is what made "doubao" unusable while the settings screen
    offered it: the registry knew the provider, the validation list did not, and the
    reader got a generic service error from a button that was drawn as available.
    """
    from synapse.runtime.service.stt import STT_ENGINES
    from synapse.stt.providers import provider_ids

    assert set(STT_ENGINES) == set(provider_ids())
    assert "doubao" in STT_ENGINES


class _FakeEngine:
    def __init__(self) -> None:
        self.loaded = False

    def status(self):
        from synapse.stt.engine import SttStatus

        return SttStatus(available=True, reason=None, model_dir="/models", loaded=False)

    def warm_up(self):
        return self.status()

    def new_session(self):
        raise AssertionError("a cloud dictation must not build the local engine")


def test_status_reports_every_provider_with_its_own_availability() -> None:
    service = SttService(engine_factory=lambda _dir: _FakeEngine())
    view = service.status(engine="browser", model_dir=None)
    by_id = {item.id: item for item in view.providers}
    assert set(by_id) == {"browser", "local", "doubao"}
    assert by_id["local"].available is True
    # A cloud provider without a key is *listed* and unusable, with the reason: the
    # reader learns why instead of wondering where the option went.
    assert by_id["doubao"].available is False
    assert by_id["doubao"].needs_key is True
    assert by_id["doubao"].key_configured is False
    assert by_id["doubao"].reason is not None and "API Key" in by_id["doubao"].reason


def test_status_honours_a_configured_key() -> None:
    service = SttService(
        engine_factory=lambda _dir: _FakeEngine(),
        key_lookup=lambda provider: "secret" if provider == "doubao" else None,
    )
    view = service.status(engine="doubao", model_dir=None)
    by_id = {item.id: item for item in view.providers}
    assert by_id["doubao"].available is True
    assert by_id["doubao"].key_configured is True
    assert view.available is True
    # The key itself is never part of the view.
    assert "secret" not in repr(view)


def test_an_unknown_engine_is_reported_rather_than_raised() -> None:
    service = SttService(engine_factory=lambda _dir: _FakeEngine())
    view = service.status(engine="quantum", model_dir=None)
    assert view.available is False
    assert view.reason is not None and "quantum" in view.reason


def test_a_cloud_dictation_without_a_key_is_refused() -> None:
    from synapse.runtime.sessions.ref import SessionRef

    service = SttService(engine_factory=lambda _dir: _FakeEngine())
    with pytest.raises(SttUnavailableError):
        service.begin(
            SessionRef(project_id="p", thread_id="t"), engine="doubao", model_dir=None
        )
