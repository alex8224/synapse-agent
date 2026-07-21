"""Test multimodal attachment bank, placeholder parsing, content composition."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from synapse.multimodal import (
    Attachment,
    AttachmentError,
    ImageBank,
    compose_user_content,
    extract_image_payloads,
    find_placeholders,
    insert_at,
    normalize_provider_family,
    provider_from_settings,
    strip_placeholder,
)

# -- ImageBank ----------------------------------------------------------

def test_bank_add_bytes_png():
    bank = ImageBank()
    # 1x1 white PNG (minimal valid)

    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    att = bank.add_bytes(data, mime="image/png", name="test.png")
    assert att.id == 1
    assert att.name == "test.png"
    assert att.mime == "image/png"
    assert att.size == len(data)
    assert att.placeholder == "[image#1]"
    assert att.source == "clipboard"


def test_bank_id_increment():
    bank = ImageBank()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    a1 = bank.add_bytes(data, mime="image/png")
    a2 = bank.add_bytes(data, mime="image/png")
    assert a1.id == 1
    assert a2.id == 2


def test_bank_reject_empty():
    bank = ImageBank()
    with pytest.raises(AttachmentError, match="empty"):
        bank.add_bytes(b"", mime="image/png")


def test_bank_reject_wrong_mime():
    bank = ImageBank()
    with pytest.raises(AttachmentError, match="unsupported"):
        bank.add_bytes(b"abc", mime="text/plain")


def test_bank_reject_too_large():
    bank = ImageBank(max_bytes=10)
    with pytest.raises(AttachmentError, match="too large"):
        bank.add_bytes(b"\x89" * 11, mime="image/png")


def test_bank_reject_too_many():
    bank = ImageBank(max_images=2)
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    bank.add_bytes(data, mime="image/png")
    bank.add_bytes(data, mime="image/png")
    with pytest.raises(AttachmentError, match="too many"):
        bank.add_bytes(data, mime="image/png")


def test_bank_clear():
    bank = ImageBank()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    bank.add_bytes(data, mime="image/png")
    bank.add_bytes(data, mime="image/png")
    assert len(bank) == 2
    bank.clear()
    assert len(bank) == 0
    assert bank.next_id() == 1


def test_bank_add_path(tmp_path: Path):
    p = tmp_path / "test.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    bank = ImageBank()
    att = bank.add_path(p)
    assert att.name == "test.png"
    assert att.source == "file"


def test_bank_add_path_reject_unknown_ext(tmp_path: Path):
    p = tmp_path / "readme.txt"
    p.write_text("hello")
    bank = ImageBank()
    with pytest.raises(AttachmentError, match="unsupported extension"):
        bank.add_path(p)


def test_data_url_roundtrip():
    bank = ImageBank()
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    att = bank.add_bytes(raw, mime="image/png", name="x.png")
    url = att.data_url()
    assert url.startswith("data:image/png;base64,")
    encoded = url[len("data:image/png;base64,") :]
    assert base64.standard_b64decode(encoded) == raw


def test_tags_human_text() -> None:
    """compose_user_content 返回 user-facing 标签文本和原始 input id。"""
    bank = ImageBank()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    a1 = bank.add_bytes(data, mime="image/png", name="a.png")
    a2 = bank.add_bytes(data, mime="image/jpeg", name="b.jpg")
    result = compose_user_content(
        "这是什么错误？",
        attachments=[a1, a2],
        provider="openai",
    )
    assert isinstance(result, list)
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "这是什么错误？"
    assert result[1]["type"] == "image_url"
    assert result[2]["type"] == "image_url"


# -- Placeholder parsing ------------------------------------------------

def test_find_placeholders():
    assert find_placeholders("") == []
    assert find_placeholders("hello") == []
    assert find_placeholders("[image#1]") == [1]
    assert find_placeholders("a [image#1] b [image#3] c") == [1, 3]
    # no leading #
    assert find_placeholders("[image1]") == []
    # duplicate
    assert find_placeholders("[image#1] [image#1]") == [1, 1]


def test_insert_at():
    s, pos = insert_at("hello world", 3, "#TAG")
    # insert_at is character-index based: pos 3 = after 3 chars "hel"
    assert s == "hel#TAGlo world"
    assert pos == 7


# -- Content composition with blocks ------------------------------------

def _mk_img(data: bytes | None = None) -> Attachment:
    d = data or (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    bank = ImageBank()
    return bank.add_bytes(d, mime="image/png", name="x.png")


def test_compose_text_only():
    result = compose_user_content("hello", provider="openai")
    assert isinstance(result, str)
    assert result == "hello"


def test_compose_text_only_none_attachments():
    result = compose_user_content("hello", attachments=None, provider="openai")
    assert isinstance(result, str)
    assert result == "hello"


def test_compose_text_with_image_openai():
    a = _mk_img()
    result = compose_user_content("看这张图", attachments=[a], provider="openai")
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"type": "text", "text": "看这张图"}
    assert result[1]["type"] == "image_url"
    url = result[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_compose_image_only_adds_placeholder_text():
    a = _mk_img()
    result = compose_user_content("", attachments=[a], provider="openai")
    assert isinstance(result, list)
    # The block list must contain a text block so empty prompt is still valid.
    assert result[0]["type"] == "text"
    assert "image" in result[0]["text"].lower()


def test_compose_multiple_images_openai():
    a1 = _mk_img()
    a2 = _mk_img()
    result = compose_user_content("查看两张图", attachments=[a1, a2], provider="openai")
    assert len(result) == 3  # text + image + image
    assert result[0]["type"] == "text"
    assert result[1]["type"] == "image_url"
    assert result[2]["type"] == "image_url"


def test_compose_anthropic_maps_to_image_block():
    a = _mk_img()
    result = compose_user_content("look", attachments=[a], provider="anthropic")
    assert isinstance(result, list)
    assert result[1]["type"] == "image"
    assert result[1]["source"]["type"] == "base64"
    assert result[1]["source"]["media_type"] == "image/png"


# -- provider_from_settings is tested implicitly via compose_user_content here
# because there's no langchain model import in multimodal.py.


def test_image_jpg_mime_fixed():
    bank = ImageBank()
    att = bank.add_bytes(b"\xff\xd8\xff" + b"\x00" * 128, mime="image/jpg", name="x.jpg")
    assert att.mime == "image/jpeg"
    url = att.data_url()
    assert url.startswith("data:image/jpeg;base64,")


def test_compose_empty_attachments_keeps_string():
    """TUI passes attachments=[] for text-only turns; keep legacy string content."""
    result = compose_user_content("hello", attachments=[], provider="openai")
    assert result == "hello"
    assert isinstance(result, str)


def test_compose_attachments_interleave_placeholders():
    bank = ImageBank()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    a1 = bank.add_bytes(data, mime="image/png", name="a.png")
    result = compose_user_content(
        "before [image#1] after",
        attachments=[a1],
        provider="openai",
    )
    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "before "}
    assert result[1]["type"] == "image_url"
    assert result[2] == {"type": "text", "text": " after"}
    # Placeholder tokens must not remain in text blocks.
    text_parts = [b["text"] for b in result if b.get("type") == "text"]
    assert all("[image#" not in t for t in text_parts)


def test_compose_bank_missing_placeholder_raises():
    bank = ImageBank()
    with pytest.raises(AttachmentError, match="missing images"):
        compose_user_content("see [image#9]", bank=bank, provider="openai")


def test_compose_google_provider_uses_image_url():
    a = _mk_img()
    result = compose_user_content("look", attachments=[a], provider="google_genai")
    assert isinstance(result, list)
    assert result[1]["type"] == "image_url"


def test_normalize_provider_family_prefixes():
    assert normalize_provider_family("anthropic") == "anthropic"
    assert normalize_provider_family("anthropic:claude-sonnet-4-6") == "anthropic"
    assert normalize_provider_family("google_genai") == "google"
    assert normalize_provider_family("google_vertexai:gemini-2.0") == "google"
    assert normalize_provider_family("openai") == "openai"
    assert normalize_provider_family("azure_openai") == "openai"
    assert normalize_provider_family("deepseek") == "openai"


def test_provider_from_settings_uses_model_prefix():
    class S:
        def __init__(self, model, active_model=None):
            self.model = model
            self.active_model = active_model

    assert provider_from_settings(None) == "openai"
    assert provider_from_settings(S("openai:gpt-4.1")) == "openai"
    assert provider_from_settings(S("anthropic:claude-sonnet-4-6")) == "anthropic"
    assert provider_from_settings(S("google_genai:gemini-2.0-flash")) == "google"
    # Gateway: OpenAI-compatible endpoint serving a Claude-named model.
    assert provider_from_settings(S("openai:claude-sonnet-4-6")) == "openai"
    assert provider_from_settings(S("azure_openai:gpt-4o")) == "openai"


def test_bank_remove_and_strip_placeholder():
    bank = ImageBank()
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    a1 = bank.add_bytes(data, mime="image/png", name="a.png")
    a2 = bank.add_bytes(data, mime="image/png", name="b.png")
    assert bank.remove(a1.id) is not None
    assert a1.id not in bank.items
    assert a2.id in bank.items
    assert bank.remove(999) is None
    text = "see [image#1] and [image#2] please"
    assert "image#1" not in strip_placeholder(text, 1)
    assert "[image#2]" in strip_placeholder(text, 1)


def test_extract_image_payloads_openai_and_anthropic():
    raw = b"hello-image-bytes"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    openai_blocks = [
        {"type": "text", "text": "hi"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        },
    ]
    got = extract_image_payloads(openai_blocks)
    assert len(got) == 1
    assert got[0][0] == raw
    assert got[0][1] == "image/png"

    anth = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        }
    ]
    got2 = extract_image_payloads(anth)
    assert len(got2) == 1
    assert got2[0][0] == raw
    assert got2[0][1] == "image/jpeg"

