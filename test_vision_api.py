"""
Test vision/multimodal API using httpx with OpenAI-compatible format.

Target: vision_model from ~/.synapse/models.json
  - model: glm-4.6v-flash
  - base_url: https://open.bigmodel.cn/api/paas/v4
"""

import base64
import json
import struct
import sys
import zlib

import httpx

# --- Vision model config (from ~/.synapse/models.json) ---
MODEL = "glm-4.6v-flash"
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
API_KEY = "9852dbd8baee42b981d1abc9462c9d37.SbQPiRmleSPqQ48T"
TIMEOUT = 45


def make_png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Build a PNG chunk: length + type + data + crc32."""
    chunk = chunk_type + data
    crc = struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    return struct.pack(">I", len(data)) + chunk + crc


def create_test_png(width: int = 200, height: int = 150) -> bytes:
    """Generate a minimal PNG: red rectangle with a blue stripe."""
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR: 8-bit RGB
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = make_png_chunk(b"IHDR", ihdr_data)

    # Build raw pixel data (filter byte 0x00 per row, then RGB)
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter: none
        for x in range(width):
            if width // 3 < x < 2 * width // 3:
                raw += b"\x00\x00\xff"  # blue stripe in the middle
            else:
                raw += b"\xff\x00\x00"  # red background

    idat = make_png_chunk(b"IDAT", zlib.compress(raw))
    iend = make_png_chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


def test_vision_api() -> int:
    """Send an image to the vision model and print the response."""
    print(f"Model   : {MODEL}")
    print(f"Base URL: {BASE_URL}")
    print()

    # Generate a small test image
    png_bytes = create_test_png()
    img_b64 = base64.b64encode(png_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{img_b64}"
    print(f"Image   : {len(png_bytes)} bytes PNG (red bg + blue stripe)")
    print()

    # Build OpenAI-compatible request body
    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请描述这张图片的内容，包括颜色和布局。用中文简短回答。",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
        "max_tokens": 256,
        "temperature": 0.3,
    }

    url = f"{BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }

    print("Sending request...")
    print(f"  POST {url}")
    print()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=body)

        print(f"HTTP {resp.status_code}")
        print()

        if resp.status_code == 200:
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage", {})

            print("--- Model Response ---")
            print(content)
            print()
            print("--- Usage ---")
            print(
                f"  prompt_tokens    : {usage.get('prompt_tokens', 'N/A')}"
            )
            print(
                f"  completion_tokens: {usage.get('completion_tokens', 'N/A')}"
            )
            print(
                f"  total_tokens     : {usage.get('total_tokens', 'N/A')}"
            )
            return 0
        else:
            print("--- Error Response ---")
            print(f"  Status: {resp.status_code}")
            print(f"  Body  : {resp.text[:1000]}")
            return 1

    except httpx.HTTPError as exc:
        print(f"HTTP error: {exc}")
        return 2
    except Exception as exc:
        print(f"Unexpected error: {type(exc).__name__}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(test_vision_api())
