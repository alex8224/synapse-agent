"""Bounded image verification for stored attachments.

This is the implementation half of the attachment contract: it owns the magic
sniffing, the bounded container-header parsing, the pixel-dimension caps that
guard against decompression bombs, and the optional Pillow decode.  The pure
DTOs/validators live in :mod:`synapse.runtime.service.attachments`, which this
module imports one way only.

Pillow is an optional dependency of this project, so verification is reported
at an explicit level instead of being silently skipped:

``magic``
    Only the container signature matched.  This level is never produced by a
    successful :func:`validate_image_payload`; it is the floor recorded for
    attachments finalized before the level vocabulary existed.
``structure``
    The signature matched, a bounded header parse produced positive pixel
    dimensions, and those dimensions are inside the caps.  No pixel data was
    decoded, so this is a structural check, not a full decode.
``decode``
    Everything above plus the optional Pillow decoder accepted the payload.

When Pillow is absent and the bounded header parse cannot produce dimensions,
the payload is rejected as :class:`AttachmentUnavailableError` (a typed
"cannot verify" result) rather than being accepted on its magic bytes alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from synapse.runtime.service.attachments import (
    VERIFICATION_DECODE,
    VERIFICATION_STRUCTURE,
    AttachmentUnavailableError,
    AttachmentUnsafeError,
    normalize_mime,
)

__all__ = [
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "ImageVerification",
    "sniff_image_mime",
    "validate_image_payload",
]

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Bounded header scan window for variable-length containers (JPEG segments).
_MAX_HEADER_SCAN_BYTES = 64 * 1024
#: Largest accepted single axis, in pixels.
MAX_IMAGE_DIMENSION = 16_384
#: Largest accepted total pixel count (decompression-bomb guard).
MAX_IMAGE_PIXELS = 40_000_000


@dataclass(frozen=True, slots=True)
class ImageVerification:
    """Normalized MIME type plus the level of checking that actually ran."""

    mime: str
    level: str


def sniff_image_mime(data: bytes) -> str | None:
    """Detect the real image type from magic bytes; never from a file name."""
    if len(data) >= 8 and data[:8] == _PNG_SIGNATURE:
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


# --- bounded structural header parsing --------------------------------------


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        return None
    if int.from_bytes(data[8:12], "big") != 13 or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")


def _bmp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or data[:2] != b"BM":
        return None
    dib_size = int.from_bytes(data[14:18], "little")
    if dib_size == 12:
        return int.from_bytes(data[18:20], "little"), int.from_bytes(data[20:22], "little")
    if dib_size < 40:
        return None
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    return abs(width), abs(height)


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        if len(data) < 30:
            return None
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    if fourcc == b"VP8 ":
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            return None
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    if fourcc == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:3] != b"\xff\xd8\xff":
        return None
    limit = min(len(data), _MAX_HEADER_SCAN_BYTES)
    index = 2
    while index + 1 < limit:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker in (0x01,) or 0xD0 <= marker <= 0xD8:
            index += 2
            continue
        if marker == 0xDA:
            return None
        if index + 4 > limit:
            return None
        segment = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if index + 9 > limit:
                return None
            return (
                int.from_bytes(data[index + 7 : index + 9], "big"),
                int.from_bytes(data[index + 5 : index + 7], "big"),
            )
        index += 2 + segment
    return None


def _structure_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    if mime == "image/png":
        return _png_dimensions(data)
    if mime == "image/jpeg":
        return _jpeg_dimensions(data)
    if mime == "image/gif":
        return _gif_dimensions(data)
    if mime == "image/webp":
        return _webp_dimensions(data)
    if mime == "image/bmp":
        return _bmp_dimensions(data)
    return None


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise AttachmentUnsafeError("attachment image dimensions are not usable")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise AttachmentUnsafeError("attachment image dimensions exceed the limit")
    if width * height > MAX_IMAGE_PIXELS:
        raise AttachmentUnsafeError("attachment image pixel count exceeds the limit")


def _pillow_verify(data: bytes) -> bool:
    """Run the optional Pillow decoder; return whether it was available.

    A missing Pillow is not an error (the caller falls back to the bounded
    structural check), but a payload Pillow rejects is a hard rejection.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - Pillow is an optional dependency
        return False
    try:
        image = Image.open(BytesIO(data))
        try:
            width, height = image.size
            _check_dimensions(int(width), int(height))
            image.verify()
        finally:
            image.close()
    except AttachmentUnsafeError:
        raise
    except Exception as exc:  # noqa: BLE001 - any decode failure is unsafe input
        raise AttachmentUnsafeError("attachment image data failed the integrity check") from exc
    return True


def validate_image_payload(data: bytes, *, expected_mime: str) -> ImageVerification:
    """Verify actual bytes against the declared type; report the check level.

    The declared type is never trusted on its own: the payload is sniffed, the
    sniffed type must equal the declared one, a bounded header parse must yield
    in-range pixel dimensions, and (when Pillow is installed) the payload must
    survive a structural decode.
    """
    normalized = normalize_mime(expected_mime)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise AttachmentUnsafeError("attachment image data is empty")
    payload = bytes(data)
    detected = sniff_image_mime(payload)
    if detected is None:
        raise AttachmentUnsafeError("attachment image data is not a recognized image type")
    if detected != normalized:
        raise AttachmentUnsafeError("attachment image data does not match the declared type")
    dimensions = _structure_dimensions(payload, detected)
    if dimensions is not None:
        _check_dimensions(*dimensions)
    if _pillow_verify(payload):
        return ImageVerification(mime=normalized, level=VERIFICATION_DECODE)
    if dimensions is None:
        raise AttachmentUnavailableError(
            "attachment image structure cannot be verified without the optional decoder"
        )
    return ImageVerification(mime=normalized, level=VERIFICATION_STRUCTURE)
