"""Tests for the RaTeX renderer exported by synapse-core-tool."""

from __future__ import annotations

import io

import pytest
from PIL import Image as PILImage
from synapse_core_tool import render_math_png


def test_render_math_png_returns_transparent_rgba_png() -> None:
    png = render_math_png(
        r"\frac{-b \pm \sqrt{b^2 - 4ac}}{2a}",
        display=True,
        color="#e8eaed",
        device_pixel_ratio=2.0,
    )

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    image = PILImage.open(io.BytesIO(png))
    image.load()
    assert image.format == "PNG"
    assert image.mode == "RGBA"
    assert image.width > 1
    assert image.height > 1
    assert image.getpixel((0, 0))[3] == 0


def test_render_math_png_supports_text_style_and_background() -> None:
    png = render_math_png(
        r"x_i^2",
        display=False,
        font_size=24.0,
        color="#112233",
        background="#ffffff",
    )

    image = PILImage.open(io.BytesIO(png)).convert("RGBA")
    assert image.getpixel((0, 0)) == (255, 255, 255, 255)


@pytest.mark.parametrize(
    ("source", "kwargs", "message"),
    [
        ("", {}, "source must not be empty"),
        ("x", {"font_size": 0.0}, "font_size must be finite"),
        ("x", {"color": "red"}, "color must be a hex color"),
        ("x" * 4_001, {}, "source exceeds the 4000-character limit"),
    ],
)
def test_render_math_png_rejects_invalid_input(
    source: str, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        render_math_png(source, **kwargs)
