"""Tests for clipboard reading helpers (multimodal).

Focuses on Linux/WSLg clipboard image/text reads: WSLg syncs Windows bitmap
data as ``image/bmp``, which the reader must accept and normalize to PNG.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from synapse.content import multimodal


def _make_bmp_bytes() -> bytes:
    im = Image.new("RGB", (2, 2), (255, 0, 0))
    buf = BytesIO()
    im.save(buf, format="BMP")
    return buf.getvalue()


def test_normalize_image_bytes_png_passthrough() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    data, mime = multimodal._normalize_image_bytes(png, "image/png")
    assert data == png
    assert mime == "image/png"


def test_normalize_image_bytes_bmp_to_png() -> None:
    bmp = _make_bmp_bytes()
    data, mime = multimodal._normalize_image_bytes(bmp, "image/bmp")
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_linux_clipboard_image_reads_bmp(monkeypatch) -> None:
    bmp = _make_bmp_bytes()
    seen: list[list[str]] = []

    def fake_run(cmd, timeout=3.0):
        seen.append(list(cmd))
        if cmd[0] == "wl-paste" and cmd[2] == "image/bmp":
            return bmp
        return None

    monkeypatch.setattr(multimodal, "_run_capture", fake_run)

    result = multimodal._linux_clipboard_image()

    assert result is not None
    data, mime, name = result
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert name == "clipboard.png"
    assert any(c[2] == "image/bmp" for c in seen)


def test_read_clipboard_text_linux_requests_explicit_text_type(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd, timeout=3.0):
        seen.append(list(cmd))
        if cmd[0] == "wl-paste":
            return b"hello"
        return None

    monkeypatch.setattr(multimodal.os, "name", "posix")
    monkeypatch.setattr(multimodal, "sys_platform", lambda: "linux")
    monkeypatch.setattr(multimodal, "_run_capture", fake_run)

    assert multimodal._read_clipboard_text() == "hello"
    assert seen[0][0:3] == ["wl-paste", "-n", "-t"]
    assert "text/plain" in seen[0]


def test_looks_like_image_data_detects_bmp_bytes() -> None:
    bmp = _make_bmp_bytes()
    # Textual decodes terminal bytes lossily; non-ASCII bytes become U+FFFD.
    text = bmp.decode("utf-8", errors="replace")
    assert multimodal.looks_like_image_data(text) is True


def test_looks_like_image_data_detects_terminal_mangled_bmp_prefix() -> None:
    # Some terminal paste paths expose a BMP prefix as BP instead of BM.
    text = "BP" + "\ufffd" * 250
    assert multimodal.looks_like_image_data(text) is True


def test_looks_like_image_data_rejects_normal_text() -> None:
    assert multimodal.looks_like_image_data("BMW is a car brand") is False
    assert multimodal.looks_like_image_data("BP is an energy company") is False
    assert multimodal.looks_like_image_data("hello world") is False
    assert multimodal.looks_like_image_data("") is False


def _osascript_output(type_code: str, data: bytes) -> bytes:
    return f"«data {type_code}{data.hex()}»\n".encode()


def test_parse_osascript_data_pngf() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    assert multimodal._parse_osascript_data(_osascript_output("PNGf", png)) == png


def test_parse_osascript_data_ignores_surrounding_noise() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    out = b"log line\n" + _osascript_output("TIFF", png) + b"trailing"
    assert multimodal._parse_osascript_data(out) == png


def test_parse_osascript_data_rejects_malformed() -> None:
    assert multimodal._parse_osascript_data(b"no data here") is None
    assert multimodal._parse_osascript_data("«data PNGfZZ»".encode()) is None


def test_mac_clipboard_image_osascript_fallback(monkeypatch) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    seen: list[list[str]] = []

    def fake_run(cmd, timeout=3.0):
        seen.append(list(cmd))
        if cmd[0] == "pngpaste":
            return None
        if cmd[0] == "osascript":
            return _osascript_output("PNGf", png)
        return None

    monkeypatch.setattr(multimodal, "_run_capture", fake_run)

    result = multimodal._mac_clipboard_image()

    assert result is not None
    data, mime, name = result
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert mime == "image/png"
    assert name == "clipboard.png"
    assert any(c[0] == "osascript" for c in seen)


def test_mac_clipboard_image_osascript_tiff_to_png(monkeypatch) -> None:
    tiff_buf = BytesIO()
    Image.new("RGB", (2, 2), (0, 255, 0)).save(tiff_buf, format="TIFF")
    tiff = tiff_buf.getvalue()

    def fake_run(cmd, timeout=3.0):
        if cmd[0] == "pngpaste":
            return None
        if cmd[0] == "osascript":
            return _osascript_output("TIFF", tiff)
        return None

    monkeypatch.setattr(multimodal, "_run_capture", fake_run)

    result = multimodal._mac_clipboard_image()

    assert result is not None
    data, mime, name = result
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert name == "clipboard.png"
