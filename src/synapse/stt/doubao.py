"""Doubao (Volcengine) streaming speech recognition: the cloud engine.

The service speaks a **binary** WebSocket protocol, not JSON-RPC: every frame is a
4-byte header plus a big-endian payload size and the payload.  The layout is taken
from the vendor's own protocol section so the framing can be reviewed here rather
than guessed at:

    byte 0   protocol version (4 bits, 0b0001) | header size (4 bits, 0b0001 = 4 B)
    byte 1   message type (4 bits) | message-type-specific flags (4 bits)
    byte 2   serialization method (4 bits) | compression (4 bits)
    byte 3   reserved
    then     payload size (uint32, big-endian) + payload

Message types: ``0b0001`` full client request (the JSON config), ``0b0010`` audio
only request, ``0b1001`` full server response, ``0b1111`` error.
Flags: ``0b0000`` none, ``0b0001`` positive sequence, ``0b0010`` last packet,
``0b0011`` negative sequence (last packet, numbered).

The endpoint choice is the cloud twin of this project's two-pass design.  With
``bigmodel_async`` (the vendor's recommended mode) and ``enable_nonstream`` the
service streams text while the reader speaks *and* re-decodes each finished
sentence with its non-streaming model -- and only that second output carries
``"definite": true``.  So this module maps ``definite`` utterances to *finalized*
and the remaining tail to *partial*, which is exactly the contract the console
already renders.

Framing is pure and unit-tested offline; the transport is verified against the live
service, because a wire protocol is not something a fake can vouch for.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import queue
import struct
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from synapse.stt.models import SAMPLE_RATE
from synapse.stt.session import SttUpdate

__all__ = [
    "DEFAULT_ENDPOINT",
    "DEFAULT_RESOURCE_ID",
    "ENDPOINTS",
    "DoubaoResponse",
    "DoubaoSession",
    "build_audio_request",
    "build_full_client_request",
    "parse_response",
]

#: ``bigmodel_async`` is the vendor's recommended mode: it returns a frame only when
#: the result changes, and it is the only mode that supports the second pass.
DEFAULT_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
ENDPOINTS = {
    "async": DEFAULT_ENDPOINT,
    "bidirectional": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    "nostream": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
}

#: 2.0, hourly billing -- the resource the vendor's own sample defaults to.
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR = 0b1111
FLAG_NONE = 0b0000
FLAG_LAST = 0b0010
FLAG_LAST_SEQUENCED = 0b0011
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

#: The vendor asks for 100-200 ms packets, calls 200 ms optimal for the
#: bidirectional mode, and warns that other sizes hurt performance.
PACKET_MS = 200
SAMPLES_PER_PACKET = SAMPLE_RATE * PACKET_MS // 1000

#: Silence that closes a sentence.  The vendor's default; it is what makes a
#: correction arrive while the reader still expects it.
END_WINDOW_MS = 800

#: How long ``finish`` waits for the server's last frame.
FINISH_TIMEOUT_SECONDS = 20.0


def _header(message_type: int, flags: int, *, gzip_payload: bool) -> bytes:
    return bytes(
        (
            (PROTOCOL_VERSION << 4) | HEADER_SIZE,
            (message_type << 4) | flags,
            (SERIALIZATION_JSON << 4) | (COMPRESSION_GZIP if gzip_payload else COMPRESSION_NONE),
            0,
        )
    )


def build_full_client_request(payload: dict[str, Any], *, gzip_payload: bool = True) -> bytes:
    """The first frame: the JSON config that opens the session."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if gzip_payload:
        raw = gzip.compress(raw)
    return (
        _header(FULL_CLIENT_REQUEST, FLAG_NONE, gzip_payload=gzip_payload)
        + struct.pack(">I", len(raw))
        + raw
    )


def build_audio_request(pcm: bytes, *, last: bool = False, gzip_payload: bool = True) -> bytes:
    """One audio frame; the last one carries the flag that ends the session.

    The header must agree with what was actually done to the payload -- a mismatch
    is a protocol error, not a tolerated variant.
    """
    payload = gzip.compress(pcm) if gzip_payload else pcm
    flags = FLAG_LAST if last else FLAG_NONE
    return (
        _header(AUDIO_ONLY_REQUEST, flags, gzip_payload=gzip_payload)
        + struct.pack(">I", len(payload))
        + payload
    )


@dataclass(frozen=True)
class DoubaoResponse:
    """One decoded server frame."""

    message_type: int
    flags: int
    payload: dict[str, Any] | None = None
    error_code: int | None = None
    error_message: str | None = None

    @property
    def is_last(self) -> bool:
        return self.flags in (FLAG_LAST, FLAG_LAST_SEQUENCED)


def parse_response(data: bytes) -> DoubaoResponse:
    """Decode one server frame.

    Raises:
        ValueError: the frame is shorter than its own header or payload size, which
            means the stream is out of sync and nothing after it can be trusted.
    """
    if len(data) < 4:
        raise ValueError("doubao frame shorter than its header")
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    compression = data[2] & 0x0F
    header_size = (data[0] & 0x0F) * 4
    body = data[header_size:]
    # A frame whose flags carry bit 0b0001 puts a sequence number between the header
    # and the payload size (observed on the live service:
    # ``11 91 10 00 | <seq:4B> | <size:4B> | {"result": ...}``).  Reading it as the
    # size is what made every response look like a corrupt payload.
    if flags & 0b0001 and len(body) >= 4:
        body = body[4:]
    if message_type == SERVER_ERROR:
        # An error frame carries the code, then the payload size, then the message:
        # ``11 f0 10 00 | <code:4B> | <size:4B> | {"error": "..."}`` (observed on the
        # live service, and consistent with the protocol's request layout).
        code = struct.unpack(">I", body[:4])[0] if len(body) >= 4 else None
        text = body[4:]
        if len(text) >= 4:
            size = struct.unpack(">I", text[:4])[0]
            if 0 < size <= len(text) - 4:
                text = text[4 : 4 + size]
        message = text.decode("utf-8", errors="replace")
        return DoubaoResponse(message_type, flags, error_code=code, error_message=message)
    if len(body) < 4:
        raise ValueError("doubao frame carries no payload size")
    size = struct.unpack(">I", body[:4])[0]
    payload = body[4 : 4 + size]
    if compression == COMPRESSION_GZIP and payload:
        payload = gzip.decompress(payload)
    if message_type == FULL_SERVER_RESPONSE and payload:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Never guess at a payload: a frame we cannot read means the stream is
            # not what this module thinks it is, and the head bytes are the evidence.
            raise ValueError(
                f"doubao response is not JSON ({exc}); head={bytes(data[:16]).hex()}"
            ) from exc
        if isinstance(decoded, dict):
            return DoubaoResponse(message_type, flags, payload=decoded)
    return DoubaoResponse(message_type, flags)


def response_text(payload: dict[str, Any]) -> str:
    """The recognized text of one response, or ''."""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    return ""


def definite_sentences(payload: dict[str, Any]) -> list[str]:
    """The sentences this response marks as finished.

    ``definite`` is the vendor's own "this sentence is final" mark and appears only
    on the non-streaming (second-pass) output -- precisely the boundary this
    project's two-pass design draws.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        return []
    sentences: list[str] = []
    for utterance in utterances:
        if not isinstance(utterance, dict) or not utterance.get("definite"):
            continue
        text = utterance.get("text")
        if isinstance(text, str) and text.strip():
            sentences.append(text.strip())
    return sentences


def _pcm_bytes(samples: Sequence[float]) -> bytes:
    """16-bit little-endian mono, which is what ``format: pcm`` means here."""
    clamped = [int(max(-1.0, min(1.0, value)) * 32767) for value in samples]
    return struct.pack(f"<{len(clamped)}h", *clamped)


async def _serve(
    *,
    api_key: str,
    endpoint: str,
    resource_id: str,
    payload: dict[str, Any],
    audio: queue.Queue,
    results: queue.Queue,
    state: dict[str, Any],
) -> None:
    """Open the socket, pump audio out and results in, then close.

    Runs inside the session's own loop.  The queues are thread-safe on purpose: the
    daemon calls the session from a worker thread, and this coroutine is the only
    thing that touches the socket.
    """
    import websockets  # noqa: PLC0415 - only the cloud path needs the client

    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    try:
        async with websockets.connect(
            endpoint, additional_headers=headers, max_size=8 * 1024 * 1024
        ) as socket:
            await socket.send(build_full_client_request(payload))

            async def send() -> None:
                while True:
                    packet = await asyncio.to_thread(audio.get)
                    if packet is None:
                        await socket.send(build_audio_request(b"", last=True))
                        return
                    await socket.send(build_audio_request(packet))

            async def receive() -> None:
                async for frame in socket:
                    if isinstance(frame, (bytes, bytearray)):
                        results.put(parse_response(bytes(frame)))

            await asyncio.gather(send(), receive())
    except Exception as exc:  # noqa: BLE001 - the reason travels to the console
        state["error"] = f"豆包连接失败：{type(exc).__name__}: {exc}"
    finally:
        state["finished"] = True
        state["done"].set()


class DoubaoSession:
    """One dictation against Doubao, shaped like the local session.

    ``accept``/``finish`` are blocking calls the daemon already makes from a worker
    thread, so the async connection lives on its own event loop in a background
    thread and this class only moves bytes and results across.  Keeping the
    signature identical to :class:`synapse.stt.session.SttSession` is what lets the
    service treat every engine the same way.
    """

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        resource_id: str = DEFAULT_RESOURCE_ID,
        context: str | None = None,
    ) -> None:
        self._state: dict[str, Any] = {"error": None, "finished": False, "done": threading.Event()}
        self._audio: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._pending: list[float] = []
        self._sentences: list[str] = []
        self._partial = ""
        self._thread = threading.Thread(
            target=self._run,
            args=(api_key, endpoint, resource_id, context),
            name="doubao-stt",
            daemon=True,
        )
        self._thread.start()

    # -- the session surface the service uses ------------------------------

    def accept(self, samples: Sequence[float]) -> SttUpdate:
        """Feed audio and report whatever the service has produced so far.

        Audio is buffered until a full 200 ms packet exists, because the vendor asks
        for 100-200 ms packets and warns that other sizes hurt performance.
        """
        self._pending.extend(samples)
        while len(self._pending) >= SAMPLES_PER_PACKET:
            packet = self._pending[:SAMPLES_PER_PACKET]
            del self._pending[:SAMPLES_PER_PACKET]
            self._audio.put(_pcm_bytes(packet))
        return self._drain()

    def finish(self) -> SttUpdate:
        """Flush the tail, close the stream, and return the last corrections."""
        if self._pending:
            self._audio.put(_pcm_bytes(self._pending))
            self._pending = []
        self._audio.put(None)
        self._state["done"].wait(timeout=FINISH_TIMEOUT_SECONDS)
        update = self._drain()
        if update.partial and not update.finalized:
            # The service never marked the tail definite (a very short utterance,
            # or a stream that ended early): the text it did produce is still what
            # the reader said, so it is finalized rather than dropped.
            return SttUpdate(partial="", finalized=(update.partial,))
        return update

    def reset(self) -> None:
        """Nothing to reset: one cloud session is one connection."""
        self._pending.clear()
        self._partial = ""

    @property
    def error(self) -> str | None:
        value = self._state.get("error")
        return value if isinstance(value, str) and value else None

    # -- internals ----------------------------------------------------------

    def _run(self, api_key: str, endpoint: str, resource_id: str, context: str | None) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _serve(
                    api_key=api_key,
                    endpoint=endpoint,
                    resource_id=resource_id,
                    payload=self._request_payload(context),
                    audio=self._audio,
                    results=self._results,
                    state=self._state,
                )
            )
        finally:
            loop.close()

    def _request_payload(self, context: str | None) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            # The second pass is the reason to pick this endpoint at all: the
            # non-streaming model re-decodes each sentence, and only its output
            # carries `definite`.
            "enable_nonstream": True,
            "show_utterances": True,
            "end_window_size": END_WINDOW_MS,
            # Incremental text: with the default ("full") every frame repeats the
            # whole transcript, so the in-progress tail would have to be recovered by
            # string surgery -- and the second-pass sentence never matches the
            # streaming text character for character, so that surgery is unreliable.
            # "single" returns only what is new, which is exactly the partial.
            "result_type": "single",
        }
        if context:
            request["corpus"] = {"context": context}
        return {
            "user": {"uid": "synapse-console"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": SAMPLE_RATE,
                "bits": 16,
                "channel": 1,
                "language": "zh-CN",
            },
            "request": request,
        }

    def _drain(self) -> SttUpdate:
        """Turn every frame that has arrived into the console's update shape."""
        partial = self._partial
        finalized: list[str] = []
        while True:
            try:
                response = self._results.get_nowait()
            except queue.Empty:
                break
            if response.error_message:
                self._state["error"] = f"豆包错误：{response.error_message}"
                continue
            payload = response.payload or {}
            text = response_text(payload)
            if text:
                partial = text
            for sentence in definite_sentences(payload):
                if sentence in self._sentences:
                    continue
                self._sentences.append(sentence)
                finalized.append(sentence)
        self._partial = partial
        return SttUpdate(partial=partial, finalized=tuple(finalized))
